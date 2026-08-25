"""End-to-end orchestration for one bounded progressive diagnosis."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from pydantic import TypeAdapter, ValidationError

from semantic_terraform_agent import __version__
from semantic_terraform_agent.cache import (
    VERIFIED_FAILURE_FINGERPRINT_VERSION,
    FailureMemoryPolicy,
    LocalCacheStore,
    build_failure_fingerprint,
    derive_repository_scope,
)
from semantic_terraform_agent.cache.fingerprint import (
    PROVIDER_SCHEMA_CACHE_VERSION,
    SCHEMA_SLICE_CACHE_VERSION,
    canonical_hash,
    provider_lock_fingerprint,
    schema_cache_key,
    schema_slice_cache_key,
)
from semantic_terraform_agent.cache.models import VerifiedFailureEntry
from semantic_terraform_agent.cache.store import CacheStoreError
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
    ModelRoutingError,
    parse_provider_name,
)
from semantic_terraform_agent.context import (
    ContextBuilder,
    ContextEscalationPolicy,
    slice_schema_records,
)
from semantic_terraform_agent.context.builder import (
    diagnostic_location_sources,
    minimal_diff,
    minimal_sources,
    normalize_resource_address,
)
from semantic_terraform_agent.context.legacy import legacy_relevant_sources
from semantic_terraform_agent.models import (
    CacheComponentTelemetry,
    CacheTelemetry,
    ContextLevel,
    ContextProgression,
    Diagnosis,
    DiagnosisCandidate,
    DiagnosisRequest,
    EscalationDecision,
    FailureStage,
    FailureMemoryTelemetry,
    FinalVerificationStatus,
    LLMCallType,
    LLMInvocation,
    LLMProviderName,
    LLMUsage,
    ModelProgression,
    ModelRoutingMode,
    ModelTier,
    PatchFailureCategory,
    PatchConstruction,
    PatchFailureReasonCode,
    RepairRequest,
    RepositoryInfo,
    ResultDocument,
    SchemaOptimization,
    SchemaSlice,
    SecondAttemptReason,
    SemanticEditSet,
    TerraformInfo,
    TokenUsage,
    VerificationAttempt,
    VerificationCommands,
    VerificationErrorRelation,
    VerificationMode,
    VerificationSignal,
)
from semantic_terraform_agent.reasoning.base import LLMProvider
from semantic_terraform_agent.reasoning.factory import create_llm_provider
from semantic_terraform_agent.reasoning.model_registry import ModelRegistry
from semantic_terraform_agent.reasoning.prompt_models import PromptParts
from semantic_terraform_agent.reasoning.prompts import (
    build_prompt_parts,
    build_repair_prompt_parts,
)
from semantic_terraform_agent.reasoning.routing import ModelRoutingPolicy, TIER_ORDER
from semantic_terraform_agent.reasoning.usage import (
    aggregate_usage,
    build_context_telemetry,
    invocation_from_response,
    legacy_token_usage,
)
from semantic_terraform_agent.security import redact_secrets
from semantic_terraform_agent.terraform.discovery import select_context_mode
from semantic_terraform_agent.terraform.assessment import assess_verification
from semantic_terraform_agent.terraform.resources import detect_resources
from semantic_terraform_agent.terraform.schema import (
    inspect_schemas,
    inspect_terraform_version,
)
from semantic_terraform_agent.terraform.provenance import (
    build_verified_patch_contract,
    collect_source_provenance,
)
from semantic_terraform_agent.terraform.patch_builder import (
    BuiltPatch,
    StructuredEditFailure,
    build_patch_from_edits,
)
from semantic_terraform_agent.terraform.verification import (
    skipped_verification,
    verify_candidate_patch,
)


class PatchVerifier(Protocol):
    def __call__(
        self, patch: str, layout: RepositoryLayout, *, attempt: int
    ) -> VerificationAttempt: ...


ProviderFactory = Callable[[LLMProviderName, str], LLMProvider]


def _elapsed(start: float) -> float:
    return round(time.perf_counter() - start, 6)


def _with_verification_mode(
    attempt: VerificationAttempt, verification_mode: VerificationMode
) -> VerificationAttempt:
    return attempt.model_copy(
        update={
            "verification_mode": verification_mode,
            "plan_requested": verification_mode == "full",
            "plan_skip_reason": (
                "cloud_verification_not_configured"
                if verification_mode == "local"
                else None
            ),
        }
    )


def calculate_evidence_score(
    request: DiagnosisRequest, diagnosis, candidate_patch: str
) -> float:
    evidence_sources = {item.source for item in diagnosis.evidence}
    checks = [
        bool(diagnosis.affected_resources and request.resources),
        bool(request.failure.summary and "terraform_error" in evidence_sources),
        bool(request.git_diff.strip() and "git_diff" in evidence_sources),
        bool(candidate_patch.strip() or diagnosis.edits),
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


def _candidate(diagnosis, patch: str) -> DiagnosisCandidate:
    return DiagnosisCandidate(
        root_cause=diagnosis.root_cause,
        affected_resources=diagnosis.affected_resources,
        violated_constraint=diagnosis.violated_constraint,
        suggested_patch=patch,
        model_confidence=diagnosis.confidence,
        evidence=diagnosis.evidence,
    )


def _repair_candidate(
    original: DiagnosisCandidate, patch: str
) -> DiagnosisCandidate:
    """Carry immutable semantic analysis forward with only a new patch artifact."""
    return original.model_copy(update={"suggested_patch": patch})


@dataclass(frozen=True)
class _CandidatePatch:
    patch: str
    representation: Literal["structured_edit", "legacy_diff"]
    construction: PatchConstruction
    failure: StructuredEditFailure | None = None


def _construct_candidate(model, layout: RepositoryLayout) -> _CandidatePatch:
    if model.edits:
        try:
            built = build_patch_from_edits(SemanticEditSet(edits=model.edits), layout)
        except StructuredEditFailure as exc:
            return _CandidatePatch(
                patch="",
                representation="structured_edit",
                construction=PatchConstruction(
                    strategy="deterministic_structured_edit_v1",
                    edit_count=len(model.edits),
                ),
                failure=exc,
            )
        return _built_candidate(built)
    return _CandidatePatch(
        patch=model.suggested_patch or "",
        representation="legacy_diff",
        construction=PatchConstruction(
            strategy="legacy_verified_diff",
            edit_count=0,
        ),
    )


def _built_candidate(
    built: BuiltPatch, *, legacy_diff_repaired: bool = False
) -> _CandidatePatch:
    return _CandidatePatch(
        patch=built.patch,
        representation="structured_edit",
        construction=PatchConstruction(
            strategy=(
                "legacy_diff_to_structured_repair"
                if legacy_diff_repaired
                else "deterministic_structured_edit_v1"
            ),
            edit_count=built.edit_count,
            legacy_diff_repaired=legacy_diff_repaired,
        ),
    )


def _construction_attempt(
    candidate: _CandidatePatch, *, attempt: int
) -> VerificationAttempt:
    assert candidate.failure is not None
    reason_code = TypeAdapter(PatchFailureReasonCode).validate_python(
        candidate.failure.code
    )
    return VerificationAttempt(
        attempt=attempt,
        patch=candidate.patch,
        status="rejected",
        failed_stage="patch_check",
        commands=VerificationCommands(),
        temporary_copy_cleaned=True,
        warnings=[candidate.failure.description],
        failure_category=(
            PatchFailureCategory.MALFORMED_REPAIRABLE
            if candidate.failure.repairable
            else PatchFailureCategory.UNSAFE
        ),
        failure_reason_code=reason_code,
        failure_description=candidate.failure.description,
        candidate_representation=candidate.representation,
        patch_construction_strategy=candidate.construction.strategy,
    )


def _response_edit_set(response) -> SemanticEditSet:
    if response.candidate_edit is not None:
        return response.candidate_edit
    if response.diagnosis is not None and response.diagnosis.edits:
        return SemanticEditSet(edits=response.diagnosis.edits)
    raise StructuredEditFailure(
        "structured_edit_invalid",
        "the second model response did not contain corrected structured edits",
        False,
    )


def _redacted_candidate(candidate: DiagnosisCandidate) -> DiagnosisCandidate:
    """Persist bounded diagnosis provenance without secret-shaped values."""
    return candidate.model_copy(
        update={
            "root_cause": redact_secrets(candidate.root_cause),
            "violated_constraint": redact_secrets(candidate.violated_constraint),
            "affected_resources": [
                redact_secrets(item) for item in candidate.affected_resources
            ],
            "evidence": [
                item.model_copy(update={"detail": redact_secrets(item.detail)})
                for item in candidate.evidence
            ],
        }
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
        failure_category=PatchFailureCategory.ENVIRONMENT_FAILURE,
        failure_reason_code="environment_failure",
        failure_description="Patch verifier could not complete in the current environment.",
    )


def _final_status(attempts: list[VerificationAttempt]) -> FinalVerificationStatus:
    final = attempts[-1]
    if final.status == "verified":
        return (
            "verified_first_attempt"
            if len(attempts) == 1
            else "verified_after_retry"
        )
    if final.status == "locally_validated":
        return (
            "locally_validated_first_attempt"
            if len(attempts) == 1
            else "locally_validated_after_retry"
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
    passed = status in {
        "verified_first_attempt",
        "verified_after_retry",
        "locally_validated_first_attempt",
        "locally_validated_after_retry",
    }
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
    model: str | None,
    context_mode: Literal["lightweight", "schema-aware", "auto"],
    llm_provider: LLMProvider | None = None,
    provider_factory: ProviderFactory | None = None,
    model_routing: Literal["fixed", "auto"] = "fixed",
    max_model_tier: Literal["free", "economy", "balanced", "premium"] = "premium",
    model_registry_path: Path | None = None,
    model_registry: ModelRegistry | None = None,
    verification_enabled: bool = True,
    verification_mode: VerificationMode = "full",
    patch_verifier: PatchVerifier | None = None,
    max_repair_attempts: int = 1,
    failed_stage: FailureStage | None = None,
    context_strategy: Literal[
        "deterministic-minimal-v1", "legacy-v0.5"
    ] = "deterministic-minimal-v1",
    schema_strategy: Literal["sliced", "full"] = "sliced",
    cache_dir: Path | None = None,
    failure_memory_enabled: bool = False,
    repository_id: str | None = None,
    source_revision: str | None = None,
    cache_store: LocalCacheStore | None = None,
) -> ResultDocument:
    if verification_mode not in {"local", "full"}:
        raise InputError("verification_mode must be local or full")
    if max_repair_attempts not in (0, 1):
        raise InputError("max_repair_attempts must be 0 or 1 in version 1.2.0")
    selected_provider = parse_provider_name(provider_name)
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
        "initial_model_routing_seconds": 0.0,
        "second_model_routing_seconds": 0.0,
        "model_routing_seconds": 0.0,
        "failure_memory_lookup_seconds": 0.0,
        "failure_memory_write_seconds": 0.0,
        "cache_lookup_seconds": 0.0,
        "source_provenance_seconds": 0.0,
    }
    warnings: list[str] = []

    try:
        routing_mode = ModelRoutingMode(model_routing)
        tier_ceiling = ModelTier(max_model_tier)
    except ValueError as exc:
        raise InputError("invalid model routing mode or maximum model tier") from exc
    started = time.perf_counter()
    layout = discover_repository(repo_path, terraform_dir)
    source_started = time.perf_counter()
    source_provenance = collect_source_provenance(
        layout,
        source_revision=source_revision,
        repository_id=repository_id,
    )
    timing["source_provenance_seconds"] = _elapsed(source_started)
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

    active_cache = cache_store
    if active_cache is None and cache_dir is not None:
        try:
            active_cache = LocalCacheStore(cache_dir, repository_root=layout.root)
        except (CacheStoreError, ValueError, OSError) as exc:
            warnings.append(f"Local cache is unavailable: {exc}")
    memory_telemetry = FailureMemoryTelemetry(
        status=(
            "disabled"
            if not failure_memory_enabled
            else ("read_error" if active_cache is None else "ineligible")
        ),
        format_version=VERIFIED_FAILURE_FINGERPRINT_VERSION,
    )
    schema_cache_telemetry = CacheComponentTelemetry(
        status="not_requested",
        format_version=PROVIDER_SCHEMA_CACHE_VERSION,
    )
    slice_cache_telemetry = CacheComponentTelemetry(
        status="not_requested",
        format_version=SCHEMA_SLICE_CACHE_VERSION,
    )
    cache_telemetry = CacheTelemetry(
        failure_memory=memory_telemetry,
        provider_schema=schema_cache_telemetry,
        schema_slice=slice_cache_telemetry,
    )
    terraform_version_hint = (
        inspect_terraform_version(layout) if active_cache is not None else None
    )
    lock_fingerprint = provider_lock_fingerprint(layout.terraform_root)
    failure_fingerprint = None
    memory_policy = FailureMemoryPolicy()
    def verifier(
        patch: str, candidate_layout: RepositoryLayout, *, attempt: int
    ) -> VerificationAttempt:
        if patch_verifier is None:
            return verify_candidate_patch(
                patch,
                candidate_layout,
                attempt=attempt,
                verification_mode=verification_mode,
            )
        result = patch_verifier(patch, candidate_layout, attempt=attempt)
        return _with_verification_mode(result, verification_mode)
    if failure_memory_enabled and active_cache is not None:
        if memory_policy.eligible_for_lookup(
            diagnosis_context,
            verification_enabled=verification_enabled,
        ):
            assert diagnosis_context is not None
            repository_scope = derive_repository_scope(layout.root, repository_id)
            failure_fingerprint = build_failure_fingerprint(
                failure=failure,
                context=diagnosis_context,
                repository_scope=repository_scope,
                terraform_version=terraform_version_hint,
                provider_lock_fingerprint=lock_fingerprint,
                terraform_source_fingerprint=canonical_hash(all_sources),
            )
            lookup_started = time.perf_counter()
            try:
                memory_entry = active_cache.get_failure(failure_fingerprint.value)
                lookup_seconds = _elapsed(lookup_started)
                timing["failure_memory_lookup_seconds"] = lookup_seconds
                timing["cache_lookup_seconds"] = lookup_seconds
                memory_telemetry = memory_telemetry.model_copy(
                    update={
                        "status": "hit" if memory_entry else "miss",
                        "fingerprint": failure_fingerprint.value,
                        "lookup_seconds": lookup_seconds,
                    }
                )
            except CacheStoreError as exc:
                memory_entry = None
                lookup_seconds = _elapsed(lookup_started)
                timing["failure_memory_lookup_seconds"] = lookup_seconds
                timing["cache_lookup_seconds"] = lookup_seconds
                memory_telemetry = memory_telemetry.model_copy(
                    update={
                        "status": "read_error",
                        "fingerprint": failure_fingerprint.value,
                        "lookup_seconds": lookup_seconds,
                    }
                )
                warnings.append(f"Verified failure memory lookup was skipped: {exc}")
            if memory_entry is not None:
                verification_started = time.perf_counter()
                try:
                    reuse_attempt = verifier(
                        memory_entry.candidate_patch, layout, attempt=1
                    )
                except Exception as exc:
                    reuse_attempt = _unavailable_attempt(
                        memory_entry.candidate_patch, 1, exc
                    )
                reuse_attempt = reuse_attempt.model_copy(
                    update={
                        "candidate_source": "verified_failure_memory",
                        "candidate_representation": "legacy_diff",
                        "patch_construction_strategy": "legacy_verified_diff",
                    }
                )
                reuse_attempt = _with_verification_mode(
                    reuse_attempt, verification_mode
                )
                timing["initial_verification_seconds"] = _elapsed(
                    verification_started
                )
                timing["verification_seconds"] = timing[
                    "initial_verification_seconds"
                ]
                reuse_assessment = assess_verification(reuse_attempt)
                if reuse_assessment.outcome in {
                    "fully_verified",
                    "locally_validated",
                    "environment_blocked",
                }:
                    memory_fully_verified = (
                        reuse_assessment.outcome == "fully_verified"
                    )
                    memory_locally_validated = (
                        reuse_assessment.outcome == "locally_validated"
                    )
                    memory_verification_status = _final_status([reuse_attempt])
                    memory_telemetry = memory_telemetry.model_copy(
                        update={
                            "status": (
                                "hit_verified"
                                if memory_fully_verified
                                else (
                                    "hit_locally_validated"
                                    if memory_locally_validated
                                    else "hit_environment_blocked"
                                )
                            ),
                            "reused": True,
                            "fresh_verification_passed": (
                                memory_fully_verified or memory_locally_validated
                            ),
                            "reuse_attempt": reuse_attempt,
                            "llm_calls_avoided": 1,
                            "historical_input_tokens_avoided": memory_entry.historical_input_tokens,
                            "historical_total_tokens_avoided": memory_entry.historical_total_tokens,
                            "historical_cost_avoided_usd": memory_entry.historical_cost_usd,
                        }
                    )
                    cache_telemetry = cache_telemetry.model_copy(
                        update={"failure_memory": memory_telemetry}
                    )
                    timing["cache_read_seconds"] = memory_telemetry.lookup_seconds
                    timing["cache_write_seconds"] = 0.0
                    diagnosis = Diagnosis(
                        initial=memory_entry.diagnosis,
                        attempts=[reuse_attempt],
                        final_patch=reuse_attempt.patch,
                        verification_status=memory_verification_status,
                        model_confidence=memory_entry.diagnosis.model_confidence,
                        evidence_score=memory_entry.evidence_score,
                        verification=_verification_signal(
                            memory_verification_status, reuse_attempt
                        ),
                        candidate_representation="legacy_diff",
                        patch_construction=PatchConstruction(
                            strategy="legacy_verified_diff",
                            edit_count=0,
                        ),
                    )
                    verification_assessment = reuse_assessment
                    timing["total_seconds"] = _elapsed(total_start)
                    zero_usage = LLMUsage(
                        call_count=0,
                        input_tokens=0,
                        output_tokens=0,
                        total_tokens=0,
                        cost_usd=0.0,
                        latency_ms=0,
                    )
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
                        initial_level=ContextLevel.MINIMAL,
                        final_level=ContextLevel.MINIMAL,
                        levels_used=[ContextLevel.MINIMAL],
                        escalated=False,
                        reason_code=(
                            "verification_passed"
                            if memory_fully_verified or memory_locally_validated
                            else "environment_unavailable"
                        ),
                        reason=(
                            "An exact repository-scoped verified-memory candidate "
                            + (
                                "passed fresh isolated verification."
                                if memory_fully_verified
                                else (
                                    "passed fresh isolated local verification."
                                    if memory_locally_validated
                                    else "passed all pre-plan gates, but fresh plan was environment-blocked."
                                )
                            )
                        ),
                        schema_retrieval_attempted=False,
                        schema_retrieved=False,
                        schema_avoided=(
                            memory_fully_verified or memory_locally_validated
                            if context_mode == "auto"
                            else None
                        ),
                        schema_avoidance_reason=(
                            (
                                "successful_minimal_verification"
                                if memory_fully_verified or memory_locally_validated
                                else "verification_stopped_before_schema_decision"
                            )
                            if context_mode == "auto"
                            else None
                        ),
                        initial_input_tokens=0,
                        total_input_tokens=0,
                    )
                    memory_terraform = TerraformInfo(
                        version=terraform_version_hint,
                        schema_extraction_status="not-requested",
                        schemas=[],
                    )
                    (
                        verified_patch,
                        source_provenance,
                        verification_provenance,
                        mutation_eligibility,
                    ) = build_verified_patch_contract(
                        diagnosis=diagnosis,
                        layout=layout,
                        source=source_provenance,
                        terraform=memory_terraform,
                        assessment=verification_assessment,
                    )
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
                        terraform=memory_terraform,
                        failure=failure,
                        context=context,
                        diagnosis=diagnosis,
                        timing=timing,
                        token_usage=TokenUsage(
                            input_tokens=0, output_tokens=0, total_tokens=0
                        ),
                        llm_usage=zero_usage,
                        llm_calls=[],
                        context_manifest=diagnosis_context.manifest,
                        context_optimization=diagnosis_context.optimization,
                        context_progression=progression,
                        model_progression=None,
                        resolution_source="verified_failure_memory",
                        cache=cache_telemetry,
                        verified_patch=verified_patch,
                        source_provenance=source_provenance,
                        verification_provenance=verification_provenance,
                        verification_assessment=verification_assessment,
                        verification_mode=verification_assessment.verification_mode,
                        plan_requested=verification_assessment.plan_requested,
                        plan_attempted=verification_assessment.plan_attempted,
                        plan_skip_reason=verification_assessment.plan_skip_reason,
                        mutation_eligibility=mutation_eligibility,
                        warnings=warnings,
                    )
                memory_telemetry = memory_telemetry.model_copy(
                    update={
                        "status": "hit_stale",
                        "fresh_verification_passed": False,
                        "reuse_attempt": reuse_attempt,
                    }
                )
                try:
                    active_cache.record_rejection(
                        failure_fingerprint.value,
                        reuse_attempt.status,
                    )
                except CacheStoreError as exc:
                    warnings.append(f"Memory rejection telemetry was not stored: {exc}")
                warnings.append(
                    "A verified-memory candidate did not pass fresh verification; "
                    "normal diagnosis retained its full two-call budget."
                )
        else:
            memory_telemetry = memory_telemetry.model_copy(
                update={"status": "ineligible"}
            )
    cache_telemetry = cache_telemetry.model_copy(
        update={"failure_memory": memory_telemetry}
    )

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
        nonlocal schema_cache_telemetry
        nonlocal slice_cache_telemetry
        schema_retrieval_attempted = True
        retrieval_started = time.perf_counter()
        schema_warnings: list[str] = []
        schema_key = schema_cache_key(
            terraform_version=terraform_version_hint,
            provider_lock_hash=lock_fingerprint,
            source_fingerprint=canonical_hash(all_sources),
            resource_types=resource_types,
        )
        cached_schema = None
        if active_cache is not None:
            try:
                cached_schema = active_cache.get_artifact("provider_schema", schema_key)
                if cached_schema is not None:
                    terraform_info = TerraformInfo.model_validate(cached_schema)
                    if terraform_info.schema_extraction_status != "ok":
                        raise ValueError("cached provider schema is incomplete")
                schema_cache_telemetry = schema_cache_telemetry.model_copy(
                    update={
                        "status": "hit" if cached_schema is not None else "miss",
                        "lookup_seconds": _elapsed(retrieval_started),
                    }
                )
            except (CacheStoreError, ValidationError, ValueError) as exc:
                cached_schema = None
                schema_cache_telemetry = schema_cache_telemetry.model_copy(
                    update={
                        "status": "read_error",
                        "lookup_seconds": _elapsed(retrieval_started),
                    }
                )
                warnings.append(f"Provider schema cache was ignored: {exc}")
        if cached_schema is None:
            terraform_info, schema_warnings = inspect_schemas(
                layout, resource_types, enabled=True
            )
            if (
                active_cache is not None
                and terraform_info.schema_extraction_status == "ok"
            ):
                write_started = time.perf_counter()
                try:
                    active_cache.put_artifact(
                        "provider_schema",
                        schema_key,
                        terraform_info.model_dump(mode="json"),
                    )
                    schema_cache_telemetry = schema_cache_telemetry.model_copy(
                        update={
                            "write_status": "stored",
                            "write_seconds": _elapsed(write_started),
                        }
                    )
                except CacheStoreError as exc:
                    schema_cache_telemetry = schema_cache_telemetry.model_copy(
                        update={
                            "status": "write_error",
                            "write_status": "write_error",
                            "write_seconds": _elapsed(write_started),
                        }
                    )
                    warnings.append(f"Provider schema cache write was skipped: {exc}")
        timing["schema_retrieval_seconds"] = _elapsed(retrieval_started)
        timing["schema_seconds"] = timing["schema_retrieval_seconds"]
        warnings.extend(schema_warnings)
        slicing_started = time.perf_counter()
        slice_payload = None
        slice_key = None
        if active_cache is not None and diagnosis_context is not None:
            slice_key = schema_slice_cache_key(
                schemas=[item.model_dump(mode="json") for item in terraform_info.schemas],
                failure=failure,
                context=diagnosis_context,
                strategy=schema_strategy,
            )
            try:
                slice_payload = active_cache.get_artifact("schema_slice", slice_key)
                if slice_payload is not None:
                    schema_slices = TypeAdapter(list[SchemaSlice]).validate_python(
                        slice_payload.get("slices", [])
                    )
                    raw_optimization = slice_payload.get("optimization")
                    schema_optimization = (
                        SchemaOptimization.model_validate(raw_optimization)
                        if raw_optimization is not None
                        else None
                    )
                slice_cache_telemetry = slice_cache_telemetry.model_copy(
                    update={
                        "status": "hit" if slice_payload is not None else "miss",
                        "lookup_seconds": _elapsed(slicing_started),
                    }
                )
            except (CacheStoreError, ValidationError, ValueError, TypeError) as exc:
                slice_payload = None
                slice_cache_telemetry = slice_cache_telemetry.model_copy(
                    update={
                        "status": "read_error",
                        "lookup_seconds": _elapsed(slicing_started),
                    }
                )
                warnings.append(f"Schema-slice cache was ignored: {exc}")
        if slice_payload is None:
            schema_slices, schema_optimization = slice_schema_records(
                terraform_info.schemas,
                failure=failure,
                diagnosis_context=diagnosis_context,
                strategy=schema_strategy,
            )
            if active_cache is not None and slice_key is not None and schema_slices:
                write_started = time.perf_counter()
                try:
                    active_cache.put_artifact(
                        "schema_slice",
                        slice_key,
                        {
                            "slices": [
                                item.model_dump(mode="json") for item in schema_slices
                            ],
                            "optimization": (
                                schema_optimization.model_dump(mode="json")
                                if schema_optimization is not None
                                else None
                            ),
                        },
                    )
                    slice_cache_telemetry = slice_cache_telemetry.model_copy(
                        update={
                            "write_status": "stored",
                            "write_seconds": _elapsed(write_started),
                        }
                    )
                except CacheStoreError as exc:
                    slice_cache_telemetry = slice_cache_telemetry.model_copy(
                        update={
                            "status": "write_error",
                            "write_status": "write_error",
                            "write_seconds": _elapsed(write_started),
                        }
                    )
                    warnings.append(f"Schema-slice cache write was skipped: {exc}")
        timing["schema_slice_seconds"] = _elapsed(slicing_started)
        schema_retrieved = bool(schema_slices)
        warnings.extend(_schema_fallback_warnings(schema_slices, schema_strategy))

    if initial_level is ContextLevel.SCHEMA:
        retrieve_schema()
    else:
        timing["schema_seconds"] = 0.0

    # Verified Failure Memory must resolve before registry loading, routing, or
    # provider construction. Only cache misses enter the model policy path.
    started = time.perf_counter()
    routing_registry = model_registry or ModelRegistry.configured(model_registry_path)
    routing_policy = ModelRoutingPolicy(routing_registry)
    initial_routing = routing_policy.select_initial(
        provider=selected_provider,
        routing_mode=routing_mode,
        requested_model=model,
        max_allowed_tier=tier_ceiling,
    )
    routing_policy.assert_allowed(initial_routing)
    timing["initial_model_routing_seconds"] = _elapsed(started)
    timing["model_routing_seconds"] = timing["initial_model_routing_seconds"]
    selected_model = initial_routing.selected_model

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
    routing_decisions = [initial_routing]
    semantic_call_attempts = 0
    factory = provider_factory or create_llm_provider
    initial_provider = llm_provider or factory(selected_provider, selected_model)

    started = time.perf_counter()
    diagnosis_prompt = build_prompt_parts(initial_request)
    semantic_call_attempts += 1
    provider_response = initial_provider.diagnose(initial_request)
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
        routing_decision=initial_routing,
    )
    llm_calls.append(diagnosis_invocation)
    prompt_records.append((diagnosis_invocation, diagnosis_prompt, initial_request))
    initial_model = provider_response.diagnosis
    if initial_model is None:
        raise InputError("the diagnosis call returned no semantic diagnosis")
    initial_candidate_patch = _construct_candidate(initial_model, layout)
    original_analysis = (
        initial_model.root_cause,
        tuple(initial_model.affected_resources),
        initial_model.violated_constraint,
        initial_model.confidence,
        tuple((item.source, item.detail) for item in initial_model.evidence),
    )
    second_candidate_patch: _CandidatePatch | None = None
    second_response_received = False

    started = time.perf_counter()
    if initial_candidate_patch.failure is not None:
        first_attempt = _construction_attempt(initial_candidate_patch, attempt=1)
    elif verification_enabled:
        try:
            first_attempt = verifier(
                initial_candidate_patch.patch, layout, attempt=1
            ).model_copy(
                update={
                    "candidate_representation": initial_candidate_patch.representation,
                    "patch_construction_strategy": initial_candidate_patch.construction.strategy,
                }
            )
        except Exception as exc:
            first_attempt = _unavailable_attempt(
                initial_candidate_patch.patch, 1, exc
            ).model_copy(
                update={
                    "candidate_representation": initial_candidate_patch.representation,
                    "patch_construction_strategy": initial_candidate_patch.construction.strategy,
                }
            )
    else:
        first_attempt = skipped_verification(
            "Patch verification was disabled by the caller.",
            initial_candidate_patch.patch,
            attempt=1,
        ).model_copy(
            update={
                "candidate_representation": initial_candidate_patch.representation,
                "patch_construction_strategy": initial_candidate_patch.construction.strategy,
            }
        )
    first_attempt = _with_verification_mode(first_attempt, verification_mode)
    timing["initial_verification_seconds"] = _elapsed(started)
    timing["verification_seconds"] = timing["initial_verification_seconds"]
    attempts = [first_attempt]

    plan_source_context: dict[str, str] = {}
    if first_attempt.plan_failure is not None:
        plan_source_context = diagnostic_location_sources(
            all_sources,
            first_attempt.plan_failure.source_file,
            first_attempt.plan_failure.source_line,
        )
        if plan_source_context:
            active_request = active_request.model_copy(
                update={
                    "relevant_sources": {
                        **active_request.relevant_sources,
                        **plan_source_context,
                    }
                }
            )

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
    repair_reason: str | None = None
    if decision.action == "escalate":
        retrieve_schema()
        if schema_retrieved:
            second_attempt_reason = SecondAttemptReason.CONTEXT_ESCALATION
            active_request = active_request.model_copy(
                update={
                    "schemas": terraform_info.schemas,
                    "terraform_version": terraform_info.version,
                    "schema_slices": schema_slices,
                    "schema_optimization": schema_optimization,
                    "context_level": ContextLevel.SCHEMA,
                }
            )
        else:
            if (
                plan_source_context
                and decision.verification_error_relation
                in {
                    VerificationErrorRelation.SAME_FAILURE,
                    VerificationErrorRelation.NEW_SEMANTIC_FAILURE,
                }
            ):
                decision = EscalationDecision(
                    action="repair",
                    should_escalate=False,
                    should_repair=True,
                    from_level=ContextLevel.MINIMAL,
                    to_level=ContextLevel.MINIMAL,
                    reason_code="schema_unavailable_source_fallback",
                    reason=(
                        "Provider schema was unavailable; continue with bounded "
                        "Terraform source at the semantic diagnostic location."
                    ),
                    signals=[
                        *decision.signals,
                        "schema unavailable; diagnostic source selected safely",
                    ][:8],
                    verification_error_relation=decision.verification_error_relation,
                )
                second_attempt_reason = SecondAttemptReason.REPAIR
                repair_reason = decision.reason_code
            else:
                decision = EscalationDecision(
                    action="stop",
                    should_escalate=False,
                    should_repair=False,
                    from_level=ContextLevel.MINIMAL,
                    reason_code="schema_unavailable",
                    reason=(
                        "Schema escalation was indicated, but no usable provider resource "
                        "schema or safe diagnostic source could be retrieved."
                    ),
                    signals=[
                        *decision.signals,
                        "schema retrieval returned no usable slice",
                    ][:8],
                    verification_error_relation=decision.verification_error_relation,
                )
    elif decision.action == "repair":
        second_attempt_reason = SecondAttemptReason.REPAIR
        if (
            first_attempt.failure_category is PatchFailureCategory.MALFORMED_REPAIRABLE
            and initial_candidate_patch.representation == "legacy_diff"
        ):
            repair_reason = "malformed_patch_to_structured_edit"
        elif first_attempt.failure_category is PatchFailureCategory.MALFORMED_REPAIRABLE:
            repair_reason = "structured_edit_repair"
        else:
            repair_reason = decision.reason_code

    repair_error: str | None = None
    if second_attempt_reason is not SecondAttemptReason.NONE:
        started = time.perf_counter()
        second_routing = routing_policy.select_second(
            initial=initial_routing,
            second_attempt_reason=second_attempt_reason,
        )
        routing_policy.assert_allowed(second_routing)
        timing["second_model_routing_seconds"] = _elapsed(started)
        timing["model_routing_seconds"] = round(
            timing["initial_model_routing_seconds"]
            + timing["second_model_routing_seconds"],
            6,
        )
        routing_decisions.append(second_routing)
        if (
            llm_provider is not None
            and second_routing.selected_model == initial_routing.selected_model
            and second_routing.selected_provider is initial_routing.selected_provider
        ):
            second_provider = initial_provider
        elif provider_factory is not None or llm_provider is None:
            second_provider = factory(
                second_routing.selected_provider,
                second_routing.selected_model,
            )
        else:
            raise ModelRoutingError(
                "auto routing selected a different model but no provider factory was supplied",
                code="no_eligible_model",
            )
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
            repair_reason=repair_reason,
        )
        repair_prompt = build_repair_prompt_parts(repair_request)
        started = time.perf_counter()
        try:
            semantic_call_attempts += 1
            repair_response = second_provider.repair(repair_request)
            second_response_received = True
            try:
                repair_edits = _response_edit_set(repair_response)
                built = build_patch_from_edits(repair_edits, layout)
                second_candidate_patch = _built_candidate(
                    built,
                    legacy_diff_repaired=(
                        initial_candidate_patch.representation == "legacy_diff"
                    ),
                )
            except StructuredEditFailure as exc:
                second_candidate_patch = _CandidatePatch(
                    patch="",
                    representation="structured_edit",
                    construction=PatchConstruction(
                        strategy=(
                            "legacy_diff_to_structured_repair"
                            if initial_candidate_patch.representation == "legacy_diff"
                            else "deterministic_structured_edit_v1"
                        ),
                        edit_count=(
                            len(repair_response.candidate_edit.edits)
                            if repair_response.candidate_edit is not None
                            else (
                                len(repair_response.diagnosis.edits)
                                if repair_response.diagnosis is not None
                                else 0
                            )
                        ),
                        legacy_diff_repaired=(
                            initial_candidate_patch.representation == "legacy_diff"
                        ),
                    ),
                    failure=exc,
                )
        except Exception as exc:
            repair_error = f"Second model call failed: {exc}"
            warnings.append(repair_error)
        timing["second_llm_seconds"] = _elapsed(started)
        timing["repair_llm_seconds"] = timing["second_llm_seconds"]

        if second_response_received:
            repair_invocation = invocation_from_response(
                repair_response,
                provider=selected_provider,
                requested_model=second_routing.selected_model,
                call_type=LLMCallType.REPAIR,
                prompt=repair_prompt,
                latency_ms=round(timing["second_llm_seconds"] * 1000),
                context_level=active_request.context_level,
                routing_decision=second_routing,
                repair_reason=repair_reason,
            )
            llm_calls.append(repair_invocation)
            prompt_records.append((repair_invocation, repair_prompt, active_request))
            started = time.perf_counter()
            assert second_candidate_patch is not None
            if second_candidate_patch.failure is not None:
                second_attempt = _construction_attempt(
                    second_candidate_patch, attempt=2
                )
            else:
                try:
                    second_attempt = verifier(
                        second_candidate_patch.patch, layout, attempt=2
                    ).model_copy(
                        update={
                            "candidate_representation": "structured_edit",
                            "patch_construction_strategy": second_candidate_patch.construction.strategy,
                        }
                    )
                except Exception as exc:
                    second_attempt = _unavailable_attempt(
                        second_candidate_patch.patch, 2, exc
                    ).model_copy(
                        update={
                            "candidate_representation": "structured_edit",
                            "patch_construction_strategy": second_candidate_patch.construction.strategy,
                        }
                    )
            timing["second_verification_seconds"] = _elapsed(started)
            timing["verification_seconds"] = round(
                timing["initial_verification_seconds"]
                + timing["second_verification_seconds"],
                6,
            )
            second_attempt = _with_verification_mode(
                second_attempt, verification_mode
            )
            attempts.append(second_attempt)

    assert semantic_call_attempts <= 2, "the agent permits at most two semantic model calls"
    assert len(llm_calls) <= 2, "the agent permits at most two validated model responses"
    for invocation, routing_decision in zip(llm_calls, routing_decisions, strict=False):
        assert invocation.provider is routing_decision.selected_provider
        assert invocation.requested_model == routing_decision.selected_model
    same_model = all(
        item.selected_provider is initial_routing.selected_provider
        and item.selected_model == initial_routing.selected_model
        for item in routing_decisions
    )
    if routing_mode is ModelRoutingMode.FIXED:
        assert same_model, "fixed routing must preserve the requested model"
    else:
        for routing_decision in routing_decisions:
            routing_policy.assert_allowed(routing_decision)
            assert routing_decision.selected_provider is selected_provider

    for attempt in attempts:
        warnings.extend(
            f"Patch verification attempt {attempt.attempt}: {item}"
            for item in attempt.warnings
        )

    verification_status = _final_status(attempts)
    final_attempt = attempts[-1]
    final_candidate_patch = second_candidate_patch or initial_candidate_patch
    initial_candidate = _candidate(initial_model, initial_candidate_patch.patch)
    repair_candidate = (
        _repair_candidate(initial_candidate, second_candidate_patch.patch)
        if second_candidate_patch is not None
        else None
    )
    final_analysis = (
        initial_candidate.root_cause,
        tuple(initial_candidate.affected_resources),
        initial_candidate.violated_constraint,
        initial_candidate.model_confidence,
        tuple((item.source, item.detail) for item in initial_candidate.evidence),
    )
    assert final_analysis == original_analysis, (
        "semantic diagnosis fields changed after the authoritative first response"
    )
    if repair_candidate is not None:
        assert (
            repair_candidate.root_cause,
            tuple(repair_candidate.affected_resources),
            repair_candidate.violated_constraint,
            repair_candidate.model_confidence,
            tuple((item.source, item.detail) for item in repair_candidate.evidence),
        ) == original_analysis, "candidate repair overwrote immutable diagnosis fields"
    diagnosis = Diagnosis(
        initial=initial_candidate,
        repair=repair_candidate,
        attempts=attempts,
        final_patch=final_attempt.patch,
        verification_status=verification_status,
        model_confidence=initial_model.confidence,
        evidence_score=calculate_evidence_score(
            initial_request, initial_model, initial_candidate_patch.patch
        ),
        verification=_verification_signal(
            verification_status, final_attempt, reason=repair_error
        ),
        second_attempt_reason=second_attempt_reason,
        repair_reason=repair_reason,
        candidate_representation=final_candidate_patch.representation,
        patch_construction=final_candidate_patch.construction,
    )
    verification_assessment = assess_verification(final_attempt)

    llm_usage = aggregate_usage(llm_calls)
    if (
        failure_memory_enabled
        and active_cache is not None
        and diagnosis_context is not None
        and memory_policy.eligible_for_store(
            diagnosis_context,
            verification_status=verification_status,
            verification_outcome=verification_assessment.outcome,
            patch=diagnosis.final_patch,
        )
    ):
        if failure_fingerprint is None:
            repository_scope = derive_repository_scope(layout.root, repository_id)
            failure_fingerprint = build_failure_fingerprint(
                failure=failure,
                context=diagnosis_context,
                repository_scope=repository_scope,
                terraform_version=terraform_version_hint,
                provider_lock_fingerprint=lock_fingerprint,
                terraform_source_fingerprint=canonical_hash(all_sources),
            )
        write_started = time.perf_counter()
        try:
            stored = active_cache.put_failure(
                VerifiedFailureEntry(
                    fingerprint_version=failure_fingerprint.version,
                    fingerprint=failure_fingerprint.value,
                    repository_scope=failure_fingerprint.repository_scope,
                    created_at=VerifiedFailureEntry.timestamp(),
                    agent_version=__version__,
                    failure_signature=failure_fingerprint.failure_signature,
                    failed_stage=failure.stage,
                    resource_type=resource_types[0] if resource_types else None,
                    resource_address=failure.resource_address,
                    terraform_version=terraform_version_hint,
                    provider_lock_fingerprint=lock_fingerprint,
                    candidate_patch=diagnosis.final_patch,
                    diagnosis=_redacted_candidate(
                        diagnosis.repair
                        if diagnosis.repair is not None
                        else diagnosis.initial
                    ),
                    evidence_score=diagnosis.evidence_score,
                    verification_status=verification_status,
                    historical_input_tokens=(
                        llm_usage.input_tokens
                        if llm_usage.token_counts_complete
                        else None
                    ),
                    historical_total_tokens=(
                        llm_usage.total_tokens
                        if llm_usage.token_counts_complete
                        else None
                    ),
                    historical_cost_usd=(
                        llm_usage.cost_usd if llm_usage.cost_complete else None
                    ),
                )
            )
            write_seconds = _elapsed(write_started)
            timing["failure_memory_write_seconds"] = write_seconds
            memory_telemetry = memory_telemetry.model_copy(
                update={
                    "write_status": "stored" if stored else "duplicate",
                    "fingerprint": failure_fingerprint.value,
                    "write_seconds": write_seconds,
                }
            )
        except CacheStoreError as exc:
            write_seconds = _elapsed(write_started)
            timing["failure_memory_write_seconds"] = write_seconds
            memory_telemetry = memory_telemetry.model_copy(
                update={
                    "status": "write_error",
                    "write_status": "write_error",
                    "fingerprint": failure_fingerprint.value,
                    "write_seconds": write_seconds,
                }
            )
            warnings.append(f"Verified failure memory was not stored: {exc}")
        cache_telemetry = cache_telemetry.model_copy(
            update={"failure_memory": memory_telemetry}
        )
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
        repair_reason=repair_reason,
        stop_reason=(decision.reason_code if decision.action == "stop" else None),
        schema_retrieval_attempted=schema_retrieval_attempted,
        schema_retrieved=schema_retrieved,
        schema_avoided=(
            (not schema_retrieval_attempted)
            if context_mode == "auto"
            and verification_status
            in {
                "verified_first_attempt",
                "verified_after_retry",
                "locally_validated_first_attempt",
                "locally_validated_after_retry",
            }
            else None
        ),
        schema_avoidance_reason=(
            (
                "successful_minimal_verification"
                if not schema_retrieval_attempted
                else (
                    "schema_retrieved"
                    if schema_retrieved
                    else "schema_unavailable_source_fallback"
                )
            )
            if context_mode == "auto"
            and verification_status
            in {
                "verified_first_attempt",
                "verified_after_retry",
                "locally_validated_first_attempt",
                "locally_validated_after_retry",
            }
            else (
                (
                    "verification_not_successful"
                    if schema_retrieval_attempted
                    else "verification_stopped_before_schema_decision"
                )
                if context_mode == "auto"
                else None
            )
        ),
        same_model=same_model,
        initial_input_tokens=llm_usage.initial_input_tokens,
        escalation_input_tokens=llm_usage.escalation_input_tokens,
        total_input_tokens=llm_usage.input_tokens,
    )
    final_routing = routing_decisions[-1]
    tier_escalated = bool(
        initial_routing.selected_tier is not None
        and final_routing.selected_tier is not None
        and TIER_ORDER[final_routing.selected_tier]
        > TIER_ORDER[initial_routing.selected_tier]
    )
    model_progression = ModelProgression(
        routing_mode=routing_mode,
        initial_model=initial_routing.selected_model,
        final_model=final_routing.selected_model,
        initial_tier=initial_routing.selected_tier,
        final_tier=final_routing.selected_tier,
        model_escalated=final_routing.selected_model != initial_routing.selected_model,
        tier_escalated=tier_escalated,
        max_allowed_tier=tier_ceiling,
        models_used=[item.selected_model for item in routing_decisions],
        decisions=routing_decisions,
    )

    timing["cache_read_seconds"] = round(
        memory_telemetry.lookup_seconds
        + schema_cache_telemetry.lookup_seconds
        + slice_cache_telemetry.lookup_seconds,
        6,
    )
    timing["cache_write_seconds"] = round(
        memory_telemetry.write_seconds
        + schema_cache_telemetry.write_seconds
        + slice_cache_telemetry.write_seconds,
        6,
    )
    timing["total_seconds"] = _elapsed(total_start)
    (
        verified_patch,
        source_provenance,
        verification_provenance,
        mutation_eligibility,
    ) = build_verified_patch_contract(
        diagnosis=diagnosis,
        layout=layout,
        source=source_provenance,
        terraform=terraform_info,
        assessment=verification_assessment,
    )
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
        model_progression=model_progression,
        resolution_source="llm",
        cache=CacheTelemetry(
            failure_memory=memory_telemetry,
            provider_schema=schema_cache_telemetry,
            schema_slice=slice_cache_telemetry,
        ),
        verified_patch=verified_patch,
        source_provenance=source_provenance,
        verification_provenance=verification_provenance,
        verification_assessment=verification_assessment,
        verification_mode=verification_assessment.verification_mode,
        plan_requested=verification_assessment.plan_requested,
        plan_attempted=verification_assessment.plan_attempted,
        plan_skip_reason=verification_assessment.plan_skip_reason,
        mutation_eligibility=mutation_eligibility,
        warnings=warnings,
    )
