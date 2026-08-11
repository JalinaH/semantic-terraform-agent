"""End-to-end orchestration for a single local diagnosis."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Literal, Protocol

from semantic_terraform_agent.collectors.failure_log import collect_failure_log
from semantic_terraform_agent.collectors.git_diff import collect_diff
from semantic_terraform_agent.collectors.repository import (
    RepositoryLayout,
    discover_repository,
    read_source_files,
)
from semantic_terraform_agent.config import InputError
from semantic_terraform_agent.models import (
    Diagnosis,
    DiagnosisCandidate,
    DiagnosisRequest,
    FinalVerificationStatus,
    RepairRequest,
    RepositoryInfo,
    ResultDocument,
    TokenUsage,
    VerificationAttempt,
    VerificationCommands,
    VerificationSignal,
)
from semantic_terraform_agent.reasoning.base import LLMProvider
from semantic_terraform_agent.reasoning.gemini import GeminiProvider
from semantic_terraform_agent.terraform.discovery import select_context_mode
from semantic_terraform_agent.terraform.resources import detect_resources
from semantic_terraform_agent.terraform.schema import inspect_schemas
from semantic_terraform_agent.terraform.verification import (
    skipped_verification,
    verify_candidate_patch,
)


class PatchVerifier(Protocol):
    def __call__(
        self, patch: str, layout: RepositoryLayout, *, attempt: int
    ) -> VerificationAttempt: ...


def _elapsed(start: float) -> float:
    return round(time.perf_counter() - start, 6)


def _relevant_sources(
    all_sources: dict[str, str], resources: list, changed_files: tuple[str, ...], failure_file: str | None
) -> dict[str, str]:
    if resources:
        result: dict[str, str] = {}
        for resource in resources:
            if not resource.file or not resource.source:
                continue
            result.setdefault(resource.file, "")
            if result[resource.file]:
                result[resource.file] += "\n\n"
            result[resource.file] += resource.source
        if result:
            return result
    selected = list(changed_files)
    if failure_file:
        matches = [
            path for path in all_sources if path == failure_file or path.endswith(f"/{failure_file}")
        ]
        selected.extend(matches)
    if not selected:
        selected = list(all_sources)
    return {path: all_sources[path] for path in dict.fromkeys(selected) if path in all_sources}


def calculate_evidence_score(request: DiagnosisRequest, diagnosis) -> float:
    evidence_sources = {item.source for item in diagnosis.evidence}
    checks = [
        bool(diagnosis.affected_resources and request.resources),
        bool(request.failure.summary and "terraform_error" in evidence_sources),
        bool(request.git_diff.strip() and "git_diff" in evidence_sources),
        bool(diagnosis.suggested_patch.strip()),
    ]
    if request.context.selected_mode == "schema-aware":
        checks.append(
            bool(
                "provider_schema" in evidence_sources
                and any(
                    item.extraction_status == "ok" and item.resource_schema is not None
                    for item in request.schemas
                )
            )
        )
    return round(sum(checks) / len(checks), 2)


def _candidate(diagnosis) -> DiagnosisCandidate:
    return DiagnosisCandidate(
        root_cause=diagnosis.root_cause,
        affected_resources=diagnosis.affected_resources,
        violated_constraint=diagnosis.violated_constraint,
        suggested_patch=diagnosis.suggested_patch,
        model_confidence=diagnosis.confidence,
        evidence=diagnosis.evidence,
    )


def _add_token_usage(left: TokenUsage, right: TokenUsage | None) -> TokenUsage:
    if right is None:
        return left

    def add(first: int | None, second: int | None) -> int | None:
        if first is None and second is None:
            return None
        return (first or 0) + (second or 0)

    return TokenUsage(
        input_tokens=add(left.input_tokens, right.input_tokens),
        output_tokens=add(left.output_tokens, right.output_tokens),
        total_tokens=add(left.total_tokens, right.total_tokens),
    )


def _unavailable_attempt(patch: str, attempt: int, error: Exception) -> VerificationAttempt:
    return VerificationAttempt(
        attempt=attempt,
        patch=patch,
        status="unavailable",
        failed_stage="patch_check",
        commands=VerificationCommands(),
        temporary_copy_cleaned=False,
        warnings=[f"Patch verifier could not complete: {error}"],
    )


def _can_repair(attempt: VerificationAttempt, max_repair_attempts: int) -> bool:
    return (
        max_repair_attempts == 1
        and attempt.status == "failed"
        and attempt.failed_stage in {"fmt", "validate", "plan"}
    )


def _final_status(attempts: list[VerificationAttempt]) -> FinalVerificationStatus:
    final = attempts[-1]
    if final.status == "verified":
        return "verified_first_attempt" if len(attempts) == 1 else "verified_after_retry"
    if final.status == "rejected":
        return "patch_rejected"
    if final.status == "unavailable":
        return "verification_unavailable"
    if final.status == "skipped":
        return "verification_skipped"
    return "verification_failed"


def _verification_signal(
    status: FinalVerificationStatus, attempt: VerificationAttempt, reason: str | None = None
) -> VerificationSignal:
    passed = status in {"verified_first_attempt", "verified_after_retry"}
    if reason is None and not passed and attempt.warnings:
        reason = attempt.warnings[0]
    return VerificationSignal(
        passed=passed,
        status=status,
        failed_stage=None if passed else attempt.failed_stage,
        reason=reason,
    )


def diagnose_repository(
    *,
    repo_path: Path,
    terraform_dir: Path,
    log_file: Path,
    diff_file: Path | None,
    provider_name: Literal["gemini"],
    model: str,
    context_mode: Literal["lightweight", "schema-aware", "auto"],
    llm_provider: LLMProvider | None = None,
    verification_enabled: bool = True,
    patch_verifier: PatchVerifier | None = None,
    max_repair_attempts: int = 1,
) -> ResultDocument:
    if max_repair_attempts not in (0, 1):
        raise InputError("max_repair_attempts must be 0 or 1 in version 0.3.0")
    total_start = time.perf_counter()
    timing: dict[str, float] = {}
    warnings: list[str] = []

    started = time.perf_counter()
    layout = discover_repository(repo_path, terraform_dir)
    diff = collect_diff(layout, diff_file)
    failure = collect_failure_log(log_file)
    all_sources = read_source_files(layout, layout.terraform_files)
    timing["collection_seconds"] = _elapsed(started)
    warnings.extend(diff.warnings)
    if not diff.text.strip():
        warnings.append(f"Git diff is empty; comparison used: {diff.comparison}.")

    started = time.perf_counter()
    resources = detect_resources(
        failure, all_sources, diff.changed_files, diff.changed_lines
    )
    context = select_context_mode(context_mode, failure, resources)
    timing["discovery_seconds"] = _elapsed(started)
    if not resources:
        warnings.append("No affected Terraform resource could be identified from the log and diff.")

    started = time.perf_counter()
    resource_types = [item.resource_type for item in resources]
    terraform_info, schema_warnings = inspect_schemas(
        layout, resource_types, enabled=context.selected_mode == "schema-aware"
    )
    warnings.extend(schema_warnings)
    timing["schema_seconds"] = _elapsed(started)

    request = DiagnosisRequest(
        failure=failure,
        resources=resources,
        relevant_sources=_relevant_sources(
            all_sources, resources, diff.changed_files, failure.referenced_file
        ),
        git_diff=diff.text,
        context=context,
        schemas=terraform_info.schemas,
        terraform_version=terraform_info.version,
    )
    started = time.perf_counter()
    if llm_provider is None:
        if provider_name != "gemini":
            raise ValueError(f"unsupported provider: {provider_name}")
        llm_provider = GeminiProvider(model=model)
    provider_response = llm_provider.diagnose(request)
    timing["llm_seconds"] = _elapsed(started)
    initial_model = provider_response.diagnosis
    final_model = initial_model
    repair_model = None
    token_usage = provider_response.token_usage

    started = time.perf_counter()
    verifier = patch_verifier or verify_candidate_patch
    if verification_enabled:
        try:
            first_attempt = verifier(initial_model.suggested_patch, layout, attempt=1)
        except Exception as exc:  # Verification must not discard an otherwise valid diagnosis.
            first_attempt = _unavailable_attempt(initial_model.suggested_patch, 1, exc)
    else:
        first_attempt = skipped_verification(
            "Patch verification was disabled by the caller.",
            initial_model.suggested_patch,
            attempt=1,
        )
    attempts = [first_attempt]
    timing["verification_seconds"] = _elapsed(started)
    repair_error: str | None = None

    if _can_repair(first_attempt, max_repair_attempts):
        started = time.perf_counter()
        try:
            repair_response = llm_provider.repair(
                RepairRequest(
                    original=request,
                    previous_diagnosis=initial_model,
                    failed_attempt=first_attempt,
                )
            )
            repair_model = repair_response.diagnosis
            final_model = repair_model
            token_usage = _add_token_usage(token_usage, repair_response.token_usage)
        except Exception as exc:  # A malformed/failed repair must preserve attempt one.
            repair_error = f"Repair model call failed: {exc}"
            warnings.append(repair_error)
        timing["repair_llm_seconds"] = _elapsed(started)

        if repair_model is not None:
            started = time.perf_counter()
            try:
                second_attempt = verifier(repair_model.suggested_patch, layout, attempt=2)
            except Exception as exc:
                second_attempt = _unavailable_attempt(repair_model.suggested_patch, 2, exc)
            attempts.append(second_attempt)
            timing["verification_seconds"] = round(
                timing["verification_seconds"] + _elapsed(started), 6
            )

    for attempt in attempts:
        warnings.extend(
            f"Patch verification attempt {attempt.attempt}: {item}"
            for item in attempt.warnings
        )

    verification_status = _final_status(attempts)
    final_attempt = attempts[-1]

    diagnosis = Diagnosis(
        initial=_candidate(initial_model),
        repair=_candidate(repair_model) if repair_model is not None else None,
        attempts=attempts,
        final_patch=final_model.suggested_patch,
        verification_status=verification_status,
        model_confidence=final_model.confidence,
        evidence_score=calculate_evidence_score(request, final_model),
        verification=_verification_signal(
            verification_status, final_attempt, reason=repair_error
        ),
    )
    timing["total_seconds"] = _elapsed(total_start)
    return ResultDocument(
        status="ok",
        repository=RepositoryInfo(
            root=str(layout.root),
            terraform_dir=layout.terraform_dir,
            terraform_files=list(layout.terraform_files),
            changed_terraform_files=list(diff.changed_files),
            diff_source=diff.source,
            diff_comparison=diff.comparison,
        ),
        terraform=terraform_info,
        failure=failure,
        context=context,
        diagnosis=diagnosis,
        timing=timing,
        token_usage=token_usage,
        warnings=warnings,
    )
