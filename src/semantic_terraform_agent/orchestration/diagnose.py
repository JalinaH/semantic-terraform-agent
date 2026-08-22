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
from semantic_terraform_agent.config import (
    DEFAULT_LIMITS,
    InputError,
    parse_provider_name,
    validate_model_id,
)
from semantic_terraform_agent.context import ContextBuilder
from semantic_terraform_agent.context.builder import (
    minimal_diff,
    minimal_sources,
    normalize_resource_address,
)
from semantic_terraform_agent.context.legacy import legacy_relevant_sources
from semantic_terraform_agent.models import (
    Diagnosis,
    DiagnosisCandidate,
    DiagnosisRequest,
    FailureStage,
    FinalVerificationStatus,
    LLMCallType,
    LLMInvocation,
    LLMProviderName,
    RepairRequest,
    RepositoryInfo,
    ResultDocument,
    VerificationAttempt,
    VerificationCommands,
    VerificationSignal,
)
from semantic_terraform_agent.reasoning.base import LLMProvider
from semantic_terraform_agent.reasoning.factory import create_llm_provider
from semantic_terraform_agent.reasoning.prompts import (
    build_prompt_parts,
    build_repair_prompt_parts,
)
from semantic_terraform_agent.reasoning.prompt_models import PromptParts
from semantic_terraform_agent.reasoning.usage import (
    aggregate_usage,
    build_context_telemetry,
    invocation_from_response,
    legacy_token_usage,
)
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
        and attempt.failed_stage in {"patch_check", "fmt", "validate", "plan"}
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
    provider_name: str | LLMProviderName,
    model: str,
    context_mode: Literal["lightweight", "schema-aware", "auto"],
    llm_provider: LLMProvider | None = None,
    verification_enabled: bool = True,
    patch_verifier: PatchVerifier | None = None,
    max_repair_attempts: int = 1,
    failed_stage: FailureStage | None = None,
    context_strategy: Literal[
        "deterministic-minimal-v1", "legacy-v0.5"
    ] = "deterministic-minimal-v1",
) -> ResultDocument:
    if max_repair_attempts not in (0, 1):
        raise InputError("max_repair_attempts must be 0 or 1 in version 0.6.0")
    selected_provider = parse_provider_name(provider_name)
    selected_model = validate_model_id(selected_provider, model)
    total_start = time.perf_counter()
    timing: dict[str, float] = {}
    warnings: list[str] = []

    started = time.perf_counter()
    layout = discover_repository(repo_path, terraform_dir)
    diff = collect_diff(layout, diff_file)
    failure = collect_failure_log(log_file)
    if failed_stage is not None:
        failure = failure.model_copy(update={"stage": failed_stage})
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
    if context_strategy == "legacy-v0.5":
        diagnosis_context = None
        relevant_sources = legacy_relevant_sources(
            all_sources, resources, diff.changed_files, failure.referenced_file
        )
        relevant_diff = diff.text
    else:
        diagnosis_context = ContextBuilder().build(
            repository=layout,
            failure=failure,
            diff=diff,
            all_sources=all_sources,
            detected_resources=resources,
            mode=context.selected_mode,
        )
        relevant_sources = minimal_sources(diagnosis_context)
        relevant_diff = minimal_diff(diagnosis_context)
    timing["context_build_seconds"] = _elapsed(started)

    started = time.perf_counter()
    if diagnosis_context is None:
        resource_types = [item.resource_type for item in resources]
    else:
        resource_types = []
        for block in diagnosis_context.resource_blocks:
            identity = normalize_resource_address(block.identifier)
            if identity and not identity.startswith("data."):
                resource_types.append(identity.split(".", 1)[0])
        if not resource_types:
            resource_types.extend(
                item.resource_type
                for item in resources[: DEFAULT_LIMITS.max_context_candidate_blocks]
            )
        resource_types = list(dict.fromkeys(resource_types))
    terraform_info, schema_warnings = inspect_schemas(
        layout, resource_types, enabled=context.selected_mode == "schema-aware"
    )
    warnings.extend(schema_warnings)
    timing["schema_seconds"] = _elapsed(started)

    request = DiagnosisRequest(
        failure=failure,
        resources=resources,
        relevant_sources=relevant_sources,
        git_diff=relevant_diff,
        context=context,
        schemas=terraform_info.schemas,
        terraform_version=terraform_info.version,
        diagnosis_context=diagnosis_context,
    )
    llm_calls: list[LLMInvocation] = []
    prompt_records: list[tuple[LLMInvocation, PromptParts]] = []
    started = time.perf_counter()
    if llm_provider is None:
        llm_provider = create_llm_provider(selected_provider, selected_model)
    diagnosis_prompt = build_prompt_parts(request)
    provider_response = llm_provider.diagnose(request)
    timing["llm_seconds"] = _elapsed(started)
    diagnosis_invocation = invocation_from_response(
        provider_response,
        provider=selected_provider,
        requested_model=selected_model,
        call_type=LLMCallType.DIAGNOSIS,
        prompt=diagnosis_prompt,
        latency_ms=round(timing["llm_seconds"] * 1000),
    )
    llm_calls.append(diagnosis_invocation)
    prompt_records.append((diagnosis_invocation, diagnosis_prompt))
    initial_model = provider_response.diagnosis
    final_model = initial_model
    repair_model = None

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
        repair_request = RepairRequest(
            original=request,
            previous_diagnosis=initial_model,
            failed_attempt=first_attempt,
        )
        repair_prompt = build_repair_prompt_parts(repair_request)
        try:
            repair_response = llm_provider.repair(repair_request)
            repair_model = repair_response.diagnosis
            final_model = repair_model
        except Exception as exc:  # A malformed/failed repair must preserve attempt one.
            repair_error = f"Repair model call failed: {exc}"
            warnings.append(repair_error)
        timing["repair_llm_seconds"] = _elapsed(started)

        if repair_model is not None:
            repair_invocation = invocation_from_response(
                repair_response,
                provider=selected_provider,
                requested_model=selected_model,
                call_type=LLMCallType.REPAIR,
                prompt=repair_prompt,
                latency_ms=round(timing["repair_llm_seconds"] * 1000),
            )
            llm_calls.append(repair_invocation)
            prompt_records.append((repair_invocation, repair_prompt))
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
        final_patch=final_attempt.patch,
        verification_status=verification_status,
        model_confidence=final_model.confidence,
        evidence_score=calculate_evidence_score(request, final_model),
        verification=_verification_signal(
            verification_status, final_attempt, reason=repair_error
        ),
    )
    timing["total_seconds"] = _elapsed(total_start)
    llm_usage = aggregate_usage(llm_calls)
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
        token_usage=legacy_token_usage(llm_usage),
        llm_usage=llm_usage,
        llm_calls=llm_calls,
        context_telemetry=build_context_telemetry(request, prompt_records),
        context_manifest=(diagnosis_context.manifest if diagnosis_context else None),
        context_optimization=(
            diagnosis_context.optimization if diagnosis_context else None
        ),
        warnings=warnings,
    )
