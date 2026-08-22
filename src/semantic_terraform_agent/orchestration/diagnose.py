"""End-to-end orchestration for one bounded progressive diagnosis."""

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
from semantic_terraform_agent.context import (
    ContextBuilder,
    ContextEscalationPolicy,
    slice_schema_records,
)
from semantic_terraform_agent.context.builder import (
    minimal_diff,
    minimal_sources,
    normalize_resource_address,
)
from semantic_terraform_agent.context.legacy import legacy_relevant_sources
from semantic_terraform_agent.models import (
    ContextLevel,
    ContextProgression,
    Diagnosis,
    DiagnosisCandidate,
    DiagnosisRequest,
    EscalationDecision,
    FailureStage,
    FinalVerificationStatus,
    LLMCallType,
    LLMInvocation,
    LLMProviderName,
    RepairRequest,
    RepositoryInfo,
    ResultDocument,
    SecondAttemptReason,
    TerraformInfo,
    VerificationAttempt,
    VerificationCommands,
    VerificationErrorRelation,
    VerificationSignal,
)
from semantic_terraform_agent.reasoning.base import LLMProvider
from semantic_terraform_agent.reasoning.factory import create_llm_provider
from semantic_terraform_agent.reasoning.prompt_models import PromptParts
from semantic_terraform_agent.reasoning.prompts import (
    build_prompt_parts,
    build_repair_prompt_parts,
)
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
    if request.schema_slices:
        checks.append(
            bool(
                "provider_schema" in evidence_sources
                and any(
                    item.extraction_status == "ok"
                    and item.resource_schema is not None
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


def _unavailable_attempt(
    patch: str, attempt: int, error: Exception
) -> VerificationAttempt:
    return VerificationAttempt(
        attempt=attempt,
        patch=patch,
        status="unavailable",
        failed_stage="patch_check",
        commands=VerificationCommands(),
        temporary_copy_cleaned=False,
        warnings=[f"Patch verifier could not complete: {error}"],
    )


def _final_status(attempts: list[VerificationAttempt]) -> FinalVerificationStatus:
    final = attempts[-1]
    if final.status == "verified":
        return (
            "verified_first_attempt"
            if len(attempts) == 1
            else "verified_after_retry"
        )
    if final.status == "rejected":
        return "patch_rejected"
    if final.status == "unavailable":
        return "verification_unavailable"
    if final.status == "skipped":
        return "verification_skipped"
    return "verification_failed"


def _verification_signal(
    status: FinalVerificationStatus,
    attempt: VerificationAttempt,
    reason: str | None = None,
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


def _resource_types(diagnosis_context, resources) -> list[str]:
    if diagnosis_context is None:
        return list(dict.fromkeys(item.resource_type for item in resources))[
            : DEFAULT_LIMITS.max_context_candidate_blocks
        ]
    result: list[str] = []
    for block in diagnosis_context.resource_blocks:
        identity = normalize_resource_address(block.identifier)
        if identity and not identity.startswith("data."):
            result.append(identity.split(".", 1)[0])
    if not result:
        result.extend(
            item.resource_type
            for item in resources[: DEFAULT_LIMITS.max_context_candidate_blocks]
        )
    return list(dict.fromkeys(result))[: DEFAULT_LIMITS.max_context_candidate_blocks]


def _empty_terraform_info() -> TerraformInfo:
    return TerraformInfo(
        version=None,
        schema_extraction_status="not-requested",
        schemas=[],
    )


def _schema_fallback_warnings(schema_slices, schema_strategy: str) -> list[str]:
    if schema_strategy != "sliced":
        return []
    return [
        "Provider schema slicing used full-schema fallback for "
        f"{item.resource_type}: {item.telemetry.fallback_reason}."
        for item in schema_slices
        if item.telemetry.strategy == "full_schema_fallback"
    ]


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
    schema_strategy: Literal["sliced", "full"] = "sliced",
) -> ResultDocument:
    if max_repair_attempts not in (0, 1):
        raise InputError("max_repair_attempts must be 0 or 1 in version 0.8.0")
    selected_provider = parse_provider_name(provider_name)
    selected_model = validate_model_id(selected_provider, model)
    total_start = time.perf_counter()
    timing: dict[str, float] = {
        "initial_context_build_seconds": 0.0,
        "initial_llm_seconds": 0.0,
        "initial_verification_seconds": 0.0,
        "escalation_decision_seconds": 0.0,
        "schema_retrieval_seconds": 0.0,
        "schema_slice_seconds": 0.0,
        "second_llm_seconds": 0.0,
        "second_verification_seconds": 0.0,
    }
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
        warnings.append(
            "No affected Terraform resource could be identified from the log and diff."
        )

    initial_level = (
        ContextLevel.SCHEMA
        if context_mode == "schema-aware"
        else ContextLevel.MINIMAL
    )
    builder_mode = (
        "schema-aware" if initial_level is ContextLevel.SCHEMA else "lightweight"
    )
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
            mode=builder_mode,
        )
        relevant_sources = minimal_sources(diagnosis_context)
        relevant_diff = minimal_diff(diagnosis_context)
    context_build_seconds = _elapsed(started)
    timing["initial_context_build_seconds"] = context_build_seconds
    timing["context_build_seconds"] = context_build_seconds
    resource_types = _resource_types(diagnosis_context, resources)

    terraform_info = _empty_terraform_info()
    schema_slices = []
    schema_optimization = None
    schema_retrieval_attempted = False
    schema_retrieved = False

    def retrieve_schema() -> None:
        nonlocal terraform_info
        nonlocal schema_slices
        nonlocal schema_optimization
        nonlocal schema_retrieval_attempted
        nonlocal schema_retrieved
        schema_retrieval_attempted = True
        retrieval_started = time.perf_counter()
        terraform_info, schema_warnings = inspect_schemas(
            layout, resource_types, enabled=True
        )
        timing["schema_retrieval_seconds"] = _elapsed(retrieval_started)
        timing["schema_seconds"] = timing["schema_retrieval_seconds"]
        warnings.extend(schema_warnings)
        slicing_started = time.perf_counter()
        schema_slices, schema_optimization = slice_schema_records(
            terraform_info.schemas,
            failure=failure,
            diagnosis_context=diagnosis_context,
            strategy=schema_strategy,
        )
        timing["schema_slice_seconds"] = _elapsed(slicing_started)
        schema_retrieved = bool(schema_slices)
        warnings.extend(_schema_fallback_warnings(schema_slices, schema_strategy))

    if initial_level is ContextLevel.SCHEMA:
        retrieve_schema()
    else:
        timing["schema_seconds"] = 0.0

    initial_request = DiagnosisRequest(
        failure=failure,
        resources=resources,
        relevant_sources=relevant_sources,
        git_diff=relevant_diff,
        context=context,
        schemas=terraform_info.schemas,
        terraform_version=terraform_info.version,
        diagnosis_context=diagnosis_context,
        schema_slices=schema_slices,
        schema_optimization=schema_optimization,
        schema_strategy=schema_strategy,
        context_level=initial_level,
    )
    active_request = initial_request
    llm_calls: list[LLMInvocation] = []
    prompt_records: list[tuple[LLMInvocation, PromptParts, DiagnosisRequest]] = []
    if llm_provider is None:
        llm_provider = create_llm_provider(selected_provider, selected_model)

    started = time.perf_counter()
    diagnosis_prompt = build_prompt_parts(initial_request)
    provider_response = llm_provider.diagnose(initial_request)
    timing["initial_llm_seconds"] = _elapsed(started)
    timing["llm_seconds"] = timing["initial_llm_seconds"]
    diagnosis_invocation = invocation_from_response(
        provider_response,
        provider=selected_provider,
        requested_model=selected_model,
        call_type=LLMCallType.DIAGNOSIS,
        prompt=diagnosis_prompt,
        latency_ms=round(timing["initial_llm_seconds"] * 1000),
        context_level=initial_level,
    )
    llm_calls.append(diagnosis_invocation)
    prompt_records.append((diagnosis_invocation, diagnosis_prompt, initial_request))
    initial_model = provider_response.diagnosis
    final_model = initial_model
    second_model = None

    verifier = patch_verifier or verify_candidate_patch
    started = time.perf_counter()
    if verification_enabled:
        try:
            first_attempt = verifier(initial_model.suggested_patch, layout, attempt=1)
        except Exception as exc:
            first_attempt = _unavailable_attempt(initial_model.suggested_patch, 1, exc)
    else:
        first_attempt = skipped_verification(
            "Patch verification was disabled by the caller.",
            initial_model.suggested_patch,
            attempt=1,
        )
    timing["initial_verification_seconds"] = _elapsed(started)
    timing["verification_seconds"] = timing["initial_verification_seconds"]
    attempts = [first_attempt]

    decision_started = time.perf_counter()
    decision = ContextEscalationPolicy().decide(
        requested_mode=context_mode,
        failure=failure,
        diagnosis_context=diagnosis_context,
        initial_diagnosis=initial_model,
        verification=first_attempt,
        schema_eligible=bool(resource_types),
        second_attempt_enabled=max_repair_attempts == 1,
    )
    timing["escalation_decision_seconds"] = _elapsed(decision_started)
    if (
        first_attempt.status == "failed"
        and decision.verification_error_relation
        is VerificationErrorRelation.ENVIRONMENT_FAILURE
    ):
        first_attempt = first_attempt.model_copy(update={"status": "unavailable"})
        attempts[0] = first_attempt

    second_attempt_reason = SecondAttemptReason.NONE
    if decision.action == "escalate":
        retrieve_schema()
        if schema_retrieved:
            second_attempt_reason = SecondAttemptReason.CONTEXT_ESCALATION
            active_request = initial_request.model_copy(
                update={
                    "schemas": terraform_info.schemas,
                    "terraform_version": terraform_info.version,
                    "schema_slices": schema_slices,
                    "schema_optimization": schema_optimization,
                    "context_level": ContextLevel.SCHEMA,
                }
            )
        else:
            decision = EscalationDecision(
                action="stop",
                should_escalate=False,
                should_repair=False,
                from_level=ContextLevel.MINIMAL,
                reason_code="schema_unavailable",
                reason=(
                    "Schema escalation was indicated, but no usable provider resource "
                    "schema could be retrieved."
                ),
                signals=[*decision.signals, "schema retrieval returned no usable slice"][:8],
                verification_error_relation=decision.verification_error_relation,
            )
    elif decision.action == "repair":
        second_attempt_reason = SecondAttemptReason.REPAIR

    repair_error: str | None = None
    if second_attempt_reason is not SecondAttemptReason.NONE:
        repair_request = RepairRequest(
            original=active_request,
            previous_diagnosis=initial_model,
            failed_attempt=first_attempt,
            second_attempt_reason=second_attempt_reason,
            escalation_decision=(
                decision
                if second_attempt_reason is SecondAttemptReason.CONTEXT_ESCALATION
                else None
            ),
        )
        repair_prompt = build_repair_prompt_parts(repair_request)
        started = time.perf_counter()
        try:
            repair_response = llm_provider.repair(repair_request)
            second_model = repair_response.diagnosis
            final_model = second_model
        except Exception as exc:
            repair_error = f"Second model call failed: {exc}"
            warnings.append(repair_error)
        timing["second_llm_seconds"] = _elapsed(started)
        timing["repair_llm_seconds"] = timing["second_llm_seconds"]

        if second_model is not None:
            repair_invocation = invocation_from_response(
                repair_response,
                provider=selected_provider,
                requested_model=selected_model,
                call_type=LLMCallType.REPAIR,
                prompt=repair_prompt,
                latency_ms=round(timing["second_llm_seconds"] * 1000),
                context_level=active_request.context_level,
            )
            llm_calls.append(repair_invocation)
            prompt_records.append((repair_invocation, repair_prompt, active_request))
            started = time.perf_counter()
            try:
                second_attempt = verifier(
                    second_model.suggested_patch, layout, attempt=2
                )
            except Exception as exc:
                second_attempt = _unavailable_attempt(
                    second_model.suggested_patch, 2, exc
                )
            timing["second_verification_seconds"] = _elapsed(started)
            timing["verification_seconds"] = round(
                timing["initial_verification_seconds"]
                + timing["second_verification_seconds"],
                6,
            )
            attempts.append(second_attempt)

    assert len(llm_calls) <= 2, "v0.8 permits at most two model calls"
    same_model = all(
        call.provider == diagnosis_invocation.provider
        and call.requested_model == diagnosis_invocation.requested_model
        for call in llm_calls
    )
    assert same_model, "progressive calls must use the same provider and requested model"

    for attempt in attempts:
        warnings.extend(
            f"Patch verification attempt {attempt.attempt}: {item}"
            for item in attempt.warnings
        )

    verification_status = _final_status(attempts)
    final_attempt = attempts[-1]
    evidence_request = active_request if second_model is not None else initial_request
    diagnosis = Diagnosis(
        initial=_candidate(initial_model),
        repair=_candidate(second_model) if second_model is not None else None,
        attempts=attempts,
        final_patch=final_attempt.patch,
        verification_status=verification_status,
        model_confidence=final_model.confidence,
        evidence_score=calculate_evidence_score(evidence_request, final_model),
        verification=_verification_signal(
            verification_status, final_attempt, reason=repair_error
        ),
        second_attempt_reason=second_attempt_reason,
    )

    llm_usage = aggregate_usage(llm_calls)
    actual_escalation = (
        second_attempt_reason is SecondAttemptReason.CONTEXT_ESCALATION
        and schema_retrieved
    )
    final_level = ContextLevel.SCHEMA if actual_escalation else initial_level
    levels_used = [initial_level]
    if actual_escalation and initial_level is not ContextLevel.SCHEMA:
        levels_used.append(ContextLevel.SCHEMA)
    progression = ContextProgression(
        strategy=(
            "minimal_then_schema_v1"
            if context_mode == "auto"
            else (
                "explicit_schema"
                if context_mode == "schema-aware"
                else "explicit_lightweight"
            )
        ),
        progressive_enabled=context_mode == "auto",
        initial_level=initial_level,
        final_level=final_level,
        levels_used=levels_used,
        escalated=actual_escalation,
        escalation_count=1 if actual_escalation else 0,
        reason_code=decision.reason_code,
        reason=decision.reason,
        signals=decision.signals,
        verification_error_relation=decision.verification_error_relation,
        second_attempt_reason=second_attempt_reason,
        schema_retrieval_attempted=schema_retrieval_attempted,
        schema_retrieved=schema_retrieved,
        schema_avoided=(
            not schema_retrieval_attempted if context_mode == "auto" else None
        ),
        same_model=same_model,
        initial_input_tokens=llm_usage.initial_input_tokens,
        escalation_input_tokens=llm_usage.escalation_input_tokens,
        total_input_tokens=llm_usage.input_tokens,
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
        token_usage=legacy_token_usage(llm_usage),
        llm_usage=llm_usage,
        llm_calls=llm_calls,
        context_telemetry=build_context_telemetry(initial_request, prompt_records),
        context_manifest=(diagnosis_context.manifest if diagnosis_context else None),
        context_optimization=(
            diagnosis_context.optimization if diagnosis_context else None
        ),
        schema_slice_manifest=[item.manifest for item in schema_slices],
        schema_optimization=schema_optimization,
        context_progression=progression,
        warnings=warnings,
    )
