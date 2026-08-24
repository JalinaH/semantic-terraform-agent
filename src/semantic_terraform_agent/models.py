"""Strict data contracts used at collection, reasoning, and output boundaries."""

from __future__ import annotations

from enum import Enum
from typing import Literal, TypeAlias

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    field_validator,
    model_validator,
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", protected_namespaces=(), populate_by_name=True)


class LLMProviderName(str, Enum):
    GEMINI = "gemini"
    OPENROUTER = "openrouter"


class LLMCallType(str, Enum):
    DIAGNOSIS = "diagnosis"
    REPAIR = "repair"


class ContextLevel(str, Enum):
    MINIMAL = "minimal"
    SCHEMA = "schema"
    EXPANDED = "expanded"


class ModelTier(str, Enum):
    FREE = "free"
    ECONOMY = "economy"
    BALANCED = "balanced"
    PREMIUM = "premium"


class ModelRoutingMode(str, Enum):
    FIXED = "fixed"
    AUTO = "auto"


class SecondAttemptReason(str, Enum):
    NONE = "none"
    REPAIR = "repair"
    CONTEXT_ESCALATION = "context_escalation"


class VerificationErrorRelation(str, Enum):
    SAME_FAILURE = "same_failure"
    NEW_SEMANTIC_FAILURE = "new_semantic_failure"
    NEW_SYNTACTIC_FAILURE = "new_syntactic_failure"
    ENVIRONMENT_FAILURE = "environment_failure"
    UNKNOWN = "unknown"


class PatchFailureCategory(str, Enum):
    MALFORMED_REPAIRABLE = "malformed_repairable"
    UNSAFE = "unsafe"
    SEMANTIC_VERIFICATION_FAILURE = "semantic_verification_failure"
    ENVIRONMENT_FAILURE = "environment_failure"
    UNKNOWN = "unknown"


PatchFailureReasonCode: TypeAlias = Literal[
    "missing_diff_headers",
    "malformed_hunk",
    "markdown_fence_leak",
    "concatenated_diff",
    "invalid_diff_structure",
    "unsafe_path",
    "non_terraform_path",
    "binary_patch",
    "symlink_escape",
    "file_creation",
    "file_deletion",
    "file_rename",
    "patch_does_not_apply",
    "terraform_verification_failure",
    "environment_failure",
    "unknown_patch_failure",
    "edit_target_not_found",
    "edit_target_ambiguous",
    "invalid_edit_path",
    "empty_edit",
    "structured_edit_invalid",
    "overlapping_edits",
    "duplicate_edits",
]


EscalationReasonCode: TypeAlias = Literal[
    "verification_passed",
    "provider_constraint_unresolved",
    "ambiguous_resource",
    "unresolved_schema_identifier",
    "verification_semantic_failure",
    "terraform_language_semantic_failure",
    "schema_unavailable_source_fallback",
    "multiple_candidate_resources",
    "unresolved_supporting_symbol",
    "minimal_patch_failed_semantically",
    "insufficient_evidence",
    "formatting_failure",
    "syntactic_patch_failure",
    "patch_check_failure",
    "malformed_patch",
    "patch_apply_failure",
    "unsafe_patch",
    "environment_unavailable",
    "credentials_unavailable",
    "provider_network_failure",
    "verification_skipped",
    "explicit_mode_repair",
    "second_attempt_disabled",
    "schema_unavailable",
    "no_actionable_failure",
]


RoutingReasonCode: TypeAlias = Literal[
    "fixed_model",
    "explicit_model",
    "initial_cheapest_eligible",
    "repair_same_model",
    "context_escalation_next_tier",
    "tier_ceiling_reuse",
    "no_stronger_model_available",
]

ModelRoutingErrorCode: TypeAlias = Literal[
    "no_eligible_model",
    "invalid_model_registry",
    "model_tier_violation",
    "explicit_model_disabled",
    "explicit_model_not_registered",
    "model_capability_unsupported",
]


class ProviderFailureCategory(str, Enum):
    MODEL_NOT_FOUND = "model_not_found"
    MODEL_UNAVAILABLE = "model_unavailable"
    STRUCTURED_OUTPUT_UNSUPPORTED = "structured_output_unsupported"
    RATE_LIMITED = "rate_limited"
    QUOTA_EXCEEDED = "quota_exceeded"
    AUTHENTICATION_FAILED = "authentication_failed"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    RESPONSE_INVALID = "response_invalid"
    TIMEOUT = "timeout"
    NETWORK_ERROR = "network_error"


FailureStage: TypeAlias = Literal[
    "init", "fmt", "validate", "plan", "apply", "unknown"
]


class RepositoryInfo(StrictModel):
    root: str
    terraform_dir: str
    terraform_files: list[str]
    changed_terraform_files: list[str]
    diff_source: str
    diff_comparison: str | None = None


class FailureInfo(StrictModel):
    summary: str
    detail: str
    referenced_file: str | None = None
    referenced_line: int | None = None
    stage: FailureStage = "unknown"
    resource_address: str | None = None
    original_log: str


class ResourceCandidate(StrictModel):
    address: str
    resource_type: str
    name: str
    file: str | None = None
    start_line: int | None = None
    end_line: int | None = None
    evidence: list[str]
    confidence: Literal["high", "medium", "low"]
    source: str


class SchemaRecord(StrictModel):
    resource_type: str
    provider_source: str | None = None
    provider_version: str | None = None
    extraction_status: Literal["ok", "resource-not-found", "not-requested", "unavailable"]
    resource_schema: dict | None = Field(default=None, alias="schema")


class TerraformInfo(StrictModel):
    version: str | None = None
    schema_retrieval_command: list[str] | None = None
    schema_extraction_status: str
    schemas: list[SchemaRecord] = Field(default_factory=list)


class ModelDefinition(StrictModel):
    provider: LLMProviderName
    model_id: str = Field(min_length=1, max_length=200)
    tier: ModelTier
    priority: StrictInt = 100
    enabled: StrictBool = True
    supports_structured_output: StrictBool = True
    supports_json_fallback: StrictBool = False
    supports_tools: StrictBool = False
    max_context_tokens: StrictInt | None = Field(default=None, gt=0)
    notes: str | None = Field(default=None, max_length=500)


class ModelRoutingDecision(StrictModel):
    call_number: int = Field(ge=1, le=2)
    routing_mode: ModelRoutingMode
    requested_model: str | None = None
    selected_provider: LLMProviderName
    selected_model: str
    selected_tier: ModelTier | None = None
    max_allowed_tier: ModelTier
    reason_code: RoutingReasonCode
    candidate_count: int = Field(default=0, ge=0)


class ModelProgression(StrictModel):
    routing_mode: ModelRoutingMode
    initial_model: str
    final_model: str
    initial_tier: ModelTier | None = None
    final_tier: ModelTier | None = None
    model_escalated: bool
    tier_escalated: bool
    max_allowed_tier: ModelTier
    models_used: list[str]
    decisions: list[ModelRoutingDecision]


class ContextSelection(StrictModel):
    requested_mode: Literal["lightweight", "schema-aware", "auto"]
    selected_mode: Literal["lightweight", "schema-aware", "progressive"]
    selection_reason: str


class ContextFailure(StrictModel):
    summary: str
    detail: str
    stage: FailureStage
    resource_address: str | None = None
    referenced_file: str | None = None
    referenced_line: int | None = None
    diagnostic_excerpt: str | None = None


class ChangedLineContext(StrictModel):
    file: str
    old_start: int = Field(ge=0)
    new_start: int = Field(ge=0)
    added_lines: list[str] = Field(default_factory=list)
    removed_lines: list[str] = Field(default_factory=list)
    context_lines: list[str] = Field(default_factory=list)
    rendered: str
    truncated: bool = False


ContextBlockKind: TypeAlias = Literal["resource", "data", "variable", "local"]


class ContextSourceBlock(StrictModel):
    kind: ContextBlockKind
    identifier: str
    file: str
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    source: str
    truncated: bool = False
    truncation_reason: str | None = None


class ContextManifest(StrictModel):
    included_files: list[str] = Field(default_factory=list)
    included_resources: list[str] = Field(default_factory=list)
    included_symbols: list[str] = Field(default_factory=list)
    referenced_symbols: list[str] = Field(default_factory=list)
    resolved_symbols: list[str] = Field(default_factory=list)
    unresolved_symbols: list[str] = Field(default_factory=list)
    changed_lines: int = Field(default=0, ge=0)
    truncated_sections: list[str] = Field(default_factory=list)
    ambiguous: bool = False


class ContextOptimization(StrictModel):
    strategy: Literal["deterministic_minimal_v1"] = "deterministic_minimal_v1"
    available_source_characters: int | None = Field(default=None, ge=0)
    selected_source_characters: int | None = Field(default=None, ge=0)
    characters_avoided: int | None = Field(default=None, ge=0)
    reduction_ratio: float | None = Field(default=None, ge=0.0, le=1.0)
    character_reduction_ratio: float | None = Field(default=None, ge=0.0, le=1.0)
    input_token_reduction_ratio: float | None = Field(default=None, ge=0.0, le=1.0)
    available_source_file_count: int | None = Field(default=None, ge=0)
    selected_source_file_count: int | None = Field(default=None, ge=0)
    available_resource_count: int | None = Field(default=None, ge=0)
    selected_resource_count: int | None = Field(default=None, ge=0)


class DiagnosisContext(StrictModel):
    failure: ContextFailure
    changed_lines: list[ChangedLineContext] = Field(default_factory=list)
    resource_blocks: list[ContextSourceBlock] = Field(default_factory=list)
    supporting_blocks: list[ContextSourceBlock] = Field(default_factory=list)
    referenced_symbols: list[str] = Field(default_factory=list)
    resolved_symbols: list[str] = Field(default_factory=list)
    unresolved_symbols: list[str] = Field(default_factory=list)
    metadata: dict[str, str | int | bool | None] = Field(default_factory=dict)
    manifest: ContextManifest
    optimization: ContextOptimization
    selected_context_characters: int = Field(ge=0)


class SchemaSliceTelemetry(StrictModel):
    strategy: Literal[
        "deterministic_schema_slice_v1",
        "full_schema_fallback",
        "full_schema_evaluation",
    ]
    full_schema_characters: int = Field(ge=0)
    selected_schema_characters: int = Field(ge=0)
    characters_avoided: int = Field(ge=0)
    reduction_ratio: float | None = Field(default=None, ge=0.0, le=1.0)
    character_reduction_ratio: float | None = Field(
        default=None, ge=0.0, le=1.0
    )
    input_token_reduction_ratio: float | None = Field(
        default=None, ge=0.0, le=1.0
    )
    selected_path_count: int = Field(default=0, ge=0)
    fallback_used: bool = False
    fallback_reason: str | None = None
    description_truncated_count: int = Field(default=0, ge=0)
    dropped_path_count: int = Field(default=0, ge=0)
    budget_exceeded: bool = False


class SchemaSliceManifest(StrictModel):
    resource_type: str
    provider_source: str | None = None
    provider_version: str | None = None
    selected_paths: list[str] = Field(default_factory=list)
    selection_reasons: dict[str, list[str]] = Field(default_factory=dict)
    unmatched_terms: list[str] = Field(default_factory=list)
    description_truncated_paths: list[str] = Field(default_factory=list)
    dropped_paths: list[str] = Field(default_factory=list)


class SchemaSlice(StrictModel):
    resource_type: str
    provider_source: str | None = None
    provider_version: str | None = None
    selected_schema: dict = Field(alias="schema")
    manifest: SchemaSliceManifest
    telemetry: SchemaSliceTelemetry


class SchemaOptimization(StrictModel):
    strategy: str
    full_schema_characters: int = Field(ge=0)
    selected_schema_characters: int = Field(ge=0)
    characters_avoided: int = Field(ge=0)
    reduction_ratio: float | None = Field(default=None, ge=0.0, le=1.0)
    character_reduction_ratio: float | None = Field(
        default=None, ge=0.0, le=1.0
    )
    input_token_reduction_ratio: float | None = Field(
        default=None, ge=0.0, le=1.0
    )
    selected_path_count: int = Field(default=0, ge=0)
    schema_count: int = Field(default=0, ge=0)
    fallback_used: bool = False
    fallback_reason: str | None = None
    repair_expanded: bool = False


class EscalationDecision(StrictModel):
    action: Literal["stop", "repair", "escalate"]
    should_escalate: bool
    should_repair: bool
    from_level: ContextLevel
    to_level: ContextLevel | None = None
    reason_code: EscalationReasonCode
    reason: str
    signals: list[str] = Field(default_factory=list, max_length=8)
    verification_error_relation: VerificationErrorRelation


class ContextProgression(StrictModel):
    strategy: Literal[
        "minimal_then_schema_v1",
        "explicit_lightweight",
        "explicit_schema",
    ]
    progressive_enabled: bool
    initial_level: ContextLevel
    final_level: ContextLevel
    levels_used: list[ContextLevel]
    escalated: bool
    escalation_count: int = Field(default=0, ge=0, le=1)
    reason_code: EscalationReasonCode | None = None
    reason: str | None = None
    signals: list[str] = Field(default_factory=list, max_length=8)
    verification_error_relation: VerificationErrorRelation | None = None
    second_attempt_reason: SecondAttemptReason = SecondAttemptReason.NONE
    repair_reason: str | None = None
    stop_reason: str | None = None
    schema_retrieval_attempted: bool = False
    schema_retrieved: bool = False
    schema_avoided: bool | None = None
    schema_avoidance_reason: str | None = None
    same_model: bool = True
    initial_input_tokens: int | None = Field(default=None, ge=0)
    escalation_input_tokens: int | None = Field(default=None, ge=0)
    total_input_tokens: int | None = Field(default=None, ge=0)


class EvidenceItem(StrictModel):
    source: Literal["terraform_error", "terraform_source", "git_diff", "provider_schema"]
    detail: str


class DiagnosisAnalysis(StrictModel):
    root_cause: str = Field(min_length=1)
    affected_resources: list[str]
    violated_constraint: str = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: list[EvidenceItem]


class SemanticEdit(StrictModel):
    file: str = Field(min_length=1, max_length=512)
    old_text: str = Field(min_length=1, max_length=8_000)
    new_text: str = Field(max_length=8_000)

    @field_validator("file", "old_text", "new_text")
    @classmethod
    def reject_unsafe_control_characters(cls, value: str) -> str:
        if any(
            ord(character) < 32 and character not in {"\t", "\n", "\r"}
            for character in value
        ) or "\x7f" in value:
            raise ValueError("structured edits contain unsupported control characters")
        return value

    @model_validator(mode="after")
    def reject_empty_replacement(self) -> "SemanticEdit":
        if self.old_text == self.new_text:
            raise ValueError("structured edit replacement must change the source")
        return self


class SemanticEditSet(StrictModel):
    edits: list[SemanticEdit] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def enforce_aggregate_budget(self) -> "SemanticEditSet":
        aggregate = sum(
            len(edit.file) + len(edit.old_text) + len(edit.new_text)
            for edit in self.edits
        )
        if aggregate > 24_000:
            raise ValueError("structured edits exceed the aggregate character budget")
        return self


class ModelDiagnosis(DiagnosisAnalysis):
    edits: list[SemanticEdit] = Field(default_factory=list, max_length=8)
    suggested_patch: str | None = Field(default=None, max_length=1024 * 1024)

    @model_validator(mode="after")
    def require_candidate_representation(self) -> "ModelDiagnosis":
        if not self.edits and not (self.suggested_patch and self.suggested_patch.strip()):
            raise ValueError("diagnosis must contain structured edits or a legacy patch")
        if self.edits:
            SemanticEditSet(edits=self.edits)
        return self


class VerificationCommand(StrictModel):
    command: list[str]
    status: Literal["passed", "failed", "skipped", "error"]
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    duration_seconds: float = 0.0


VerificationStage: TypeAlias = Literal[
    "patch_check", "patch_apply", "fmt", "init", "validate", "plan"
]
AttemptStatus: TypeAlias = Literal[
    "verified", "failed", "rejected", "unavailable", "skipped"
]
FinalVerificationStatus: TypeAlias = Literal[
    "verified_first_attempt",
    "verified_after_retry",
    "verification_failed",
    "patch_rejected",
    "verification_unavailable",
    "verification_skipped",
]

PlanFailureClassification: TypeAlias = Literal[
    "terraform_semantic",
    "credentials",
    "permissions",
    "network",
    "provider_unavailable",
    "external_service",
    "runtime_environment",
    "unknown",
]

PlanFailureReasonCode: TypeAlias = Literal[
    "resource_precondition_failed",
    "resource_postcondition_failed",
    "check_assertion_failed",
    "invalid_variable_value",
    "invalid_expression",
    "unsupported_argument",
    "conflicting_arguments",
    "invalid_index_or_key",
    "invalid_resource_configuration",
    "invalid_provider_configuration",
    "provider_schema_constraint",
    "missing_required_argument",
    "invalid_terraform_reference",
    "aws_no_credentials",
    "aws_expired_credentials",
    "aws_invalid_security_token",
    "authentication_failed",
    "aws_access_denied",
    "aws_unauthorized_operation",
    "explicit_deny",
    "permission_denied",
    "dns_resolution_failed",
    "connection_timeout",
    "connection_refused",
    "connection_reset",
    "tls_connectivity_failed",
    "network_unavailable",
    "provider_service_unavailable",
    "provider_plugin_unavailable",
    "external_rate_limited",
    "external_service_unavailable",
    "runtime_dependency_unavailable",
    "runtime_prerequisite_unavailable",
    "unclassified_plan_failure",
]

VerificationOutcome: TypeAlias = Literal[
    "fully_verified",
    "environment_blocked",
    "semantic_failure",
    "patch_invalid",
    "unknown_failure",
]

ApplySafety: TypeAlias = Literal[
    "verified",
    "conditionally_eligible",
    "ineligible",
]

MutationEligibilityReasonCode: TypeAlias = Literal[
    "verified_terraform_patch",
    "terraform_plan_environment_blocked",
    "not_verified",
    "patch_rejected",
    "verification_failed",
    "verification_unavailable",
    "verification_skipped",
    "no_patch",
    "unsafe_patch",
    "non_terraform_files",
    "source_revision_unknown",
    "affected_files_unknown",
    "patch_hash_unavailable",
]


class PlanFailure(StrictModel):
    classification: PlanFailureClassification
    reason_code: PlanFailureReasonCode
    summary: str = Field(min_length=1, max_length=500)
    detail: str = Field(min_length=1, max_length=2_000)
    source_file: str | None = Field(default=None, max_length=512)
    source_line: int | None = Field(default=None, ge=1)
    resource_address: str | None = Field(default=None, max_length=512)
    diagnostic_format: Literal["terraform_json", "bounded_text"]


class VerificationAssessment(StrictModel):
    outcome: VerificationOutcome
    patch_check_passed: bool
    patch_apply_passed: bool
    fmt_passed: bool
    init_passed: bool
    validate_passed: bool
    plan_attempted: bool
    plan_passed: bool
    full_verification_passed: bool
    apply_safety: ApplySafety
    plan_failure: PlanFailure | None = None

MutationEligibilityDetailCode: TypeAlias = Literal[
    "patch_empty",
    "patch_scope_invalid",
    "unsupported_file_operation",
    "working_tree_not_clean",
    "verification_provenance_incomplete",
    "affected_file_missing",
    "verified_patch_mismatch",
]


class VerificationCommands(StrictModel):
    patch_check: VerificationCommand | None = None
    patch_apply: VerificationCommand | None = None
    fmt: VerificationCommand | None = None
    init: VerificationCommand | None = None
    terraform_validate: VerificationCommand | None = Field(default=None, alias="validate")
    plan: VerificationCommand | None = None


class VerificationAttempt(StrictModel):
    attempt: int = Field(ge=1, le=2)
    patch: str
    status: AttemptStatus
    failed_stage: VerificationStage | None = None
    isolation: Literal["temporary-copy"] = "temporary-copy"
    changed_files: list[str] = Field(default_factory=list)
    commands: VerificationCommands = Field(default_factory=VerificationCommands)
    temporary_copy_cleaned: bool
    warnings: list[str] = Field(default_factory=list)
    candidate_source: Literal["llm", "verified_failure_memory"] = "llm"
    failure_category: PatchFailureCategory | None = None
    failure_reason_code: PatchFailureReasonCode | None = None
    failure_description: str | None = Field(default=None, max_length=500)
    candidate_representation: Literal["structured_edit", "legacy_diff"] | None = None
    patch_construction_strategy: str | None = Field(default=None, max_length=80)
    plan_failure: PlanFailure | None = None


class SourceProvenance(StrictModel):
    repository_scope: str
    terraform_dir: str
    git_commit_sha: str | None = None
    git_tree_sha: str | None = None
    caller_source_revision: str | None = None
    verified_against_commit_sha: str | None = None
    working_tree_mode: Literal["git_clean", "git_dirty", "non_git"]
    source_fingerprint_sha256: str | None = None


class VerificationProvenance(StrictModel):
    attempt_number: int = Field(ge=1, le=2)
    final_status: FinalVerificationStatus
    verified_in_isolated_workspace: bool
    patch_check_passed: bool
    patch_apply_passed: bool
    fmt_passed: bool
    init_passed: bool
    validate_passed: bool
    plan_required: bool = True
    plan_attempted: bool = False
    plan_passed: bool
    terraform_version: str | None = None
    provider_versions: dict[str, str] = Field(default_factory=dict)


class VerifiedPatchArtifact(StrictModel):
    patch_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    affected_files: list[str]
    repository_relative_paths_only: bool
    terraform_files_only: bool
    existing_files_only: bool
    verification_status: FinalVerificationStatus
    verification_passed: bool
    verification_attempt: int = Field(ge=1, le=2)
    verified_against_commit_sha: str | None = None
    source_fingerprint_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    candidate_source: Literal["llm", "verified_failure_memory"]


class MutationEligibility(StrictModel):
    eligible: bool
    eligibility_level: Literal["verified", "conditional", "ineligible"] = "ineligible"
    reason_code: MutationEligibilityReasonCode
    reasons: list[MutationEligibilityDetailCode] = Field(default_factory=list)
    requires_fresh_head_check: bool = True


class DiagnosisCandidate(StrictModel):
    root_cause: str
    affected_resources: list[str]
    violated_constraint: str
    suggested_patch: str
    model_confidence: float
    evidence: list[EvidenceItem]


PatchConstructionStrategy: TypeAlias = Literal[
    "deterministic_structured_edit_v1",
    "legacy_verified_diff",
    "legacy_diff_to_structured_repair",
]


class PatchConstruction(StrictModel):
    strategy: PatchConstructionStrategy
    edit_count: int = Field(ge=0, le=8)
    legacy_diff_repaired: bool = False


class VerificationSignal(StrictModel):
    passed: bool
    status: FinalVerificationStatus
    failed_stage: VerificationStage | None = None
    reason: str | None = None


class Diagnosis(StrictModel):
    initial: DiagnosisCandidate
    repair: DiagnosisCandidate | None = None
    attempts: list[VerificationAttempt]
    final_patch: str
    verification_status: FinalVerificationStatus
    model_confidence: float
    evidence_score: float
    verification: VerificationSignal
    second_attempt_reason: SecondAttemptReason = SecondAttemptReason.NONE
    repair_reason: str | None = None
    candidate_representation: Literal["structured_edit", "legacy_diff"] = "legacy_diff"
    patch_construction: PatchConstruction | None = None


class TokenUsage(StrictModel):
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None


class LLMInvocation(StrictModel):
    provider: LLMProviderName
    requested_model: str
    reported_model: str | None = None
    upstream_provider: str | None = None
    input_tokens: int | None = Field(default=None, ge=0)
    cached_input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    reasoning_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    cost_usd: float | None = Field(default=None, ge=0)
    latency_ms: int = Field(ge=0)
    cache_hit: bool | None = None
    call_type: LLMCallType
    context_level: ContextLevel | None = None
    routing_tier: ModelTier | None = None
    routing_reason: RoutingReasonCode | None = None
    call_number: int | None = Field(default=None, ge=1, le=2)
    repair_reason: str | None = None
    prompt_characters: int = Field(ge=0)
    system_prompt_characters: int = Field(ge=0)
    user_prompt_characters: int = Field(ge=0)
    finish_reason: str | None = None


class LLMUsage(StrictModel):
    call_count: int = Field(default=0, ge=0)
    input_tokens: int | None = Field(default=None, ge=0)
    cached_input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    reasoning_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    cost_usd: float | None = Field(default=None, ge=0)
    latency_ms: int | None = Field(default=None, ge=0)
    token_counts_complete: bool = True
    cost_complete: bool = True
    initial_input_tokens: int | None = Field(default=None, ge=0)
    escalation_input_tokens: int | None = Field(default=None, ge=0)


class ContextTelemetry(StrictModel):
    mode: Literal["lightweight", "schema-aware", "progressive"]
    prompt_characters: int = Field(ge=0)
    system_prompt_characters: int = Field(ge=0)
    user_prompt_characters: int = Field(ge=0)
    resource_schema_included: bool
    git_diff_included: bool
    source_file_count: int = Field(ge=0)
    source_block_count: int = Field(default=0, ge=0)
    changed_line_count: int = Field(default=0, ge=0)
    referenced_symbol_count: int = Field(default=0, ge=0)
    schema_included: bool = False
    selected_context_characters: int | None = Field(default=None, ge=0)
    rendered_user_prompt_characters: int | None = Field(default=None, ge=0)
    sections: dict[str, "ContextSectionTelemetry"] = Field(default_factory=dict)
    calls: list["ContextCallTelemetry"] = Field(default_factory=list)


class ContextSectionTelemetry(StrictModel):
    characters: int = Field(ge=0)
    full_available_characters: int | None = Field(default=None, ge=0)
    selected_schema_characters: int | None = Field(default=None, ge=0)
    reduction_ratio: float | None = Field(default=None, ge=0.0, le=1.0)


class ContextCallTelemetry(StrictModel):
    call_type: LLMCallType
    repair_reason: str | None = None
    context_level: ContextLevel | None = None
    prompt_characters: int = Field(ge=0)
    system_prompt_characters: int = Field(ge=0)
    user_prompt_characters: int = Field(ge=0)
    selected_context_characters: int | None = Field(default=None, ge=0)
    selected_source_characters: int | None = Field(default=None, ge=0)
    schema_characters: int = Field(default=0, ge=0)
    source_file_count: int = Field(default=0, ge=0)
    resource_count: int = Field(default=0, ge=0)
    schema_path_count: int = Field(default=0, ge=0)
    sections: dict[str, ContextSectionTelemetry] = Field(default_factory=dict)


class DiagnosisRequest(StrictModel):
    failure: FailureInfo
    resources: list[ResourceCandidate]
    relevant_sources: dict[str, str]
    git_diff: str
    context: ContextSelection
    schemas: list[SchemaRecord]
    terraform_version: str | None = None
    diagnosis_context: DiagnosisContext | None = None
    schema_slices: list[SchemaSlice] = Field(default_factory=list)
    schema_optimization: SchemaOptimization | None = None
    schema_strategy: Literal["sliced", "full"] = "sliced"
    context_level: ContextLevel | None = None


class RepairRequest(StrictModel):
    original: DiagnosisRequest
    previous_diagnosis: ModelDiagnosis
    failed_attempt: VerificationAttempt
    second_attempt_reason: SecondAttemptReason = SecondAttemptReason.REPAIR
    escalation_decision: EscalationDecision | None = None
    repair_reason: str | None = None


class ProviderResponse(StrictModel):
    diagnosis: ModelDiagnosis | None = None
    candidate_edit: SemanticEditSet | None = None
    token_usage: TokenUsage = Field(default_factory=TokenUsage)
    llm_call: LLMInvocation | None = None

    @model_validator(mode="after")
    def require_one_response_contract(self) -> "ProviderResponse":
        if (self.diagnosis is None) == (self.candidate_edit is None):
            raise ValueError("provider response must contain exactly one response contract")
        return self


CacheStatus: TypeAlias = Literal[
    "disabled",
    "ineligible",
    "miss",
    "hit",
    "hit_verified",
    "hit_environment_blocked",
    "hit_stale",
    "read_error",
    "write_error",
    "not_requested",
]


class CacheComponentTelemetry(StrictModel):
    status: CacheStatus
    format_version: str
    lookup_seconds: float = Field(default=0.0, ge=0.0)
    write_seconds: float = Field(default=0.0, ge=0.0)
    write_status: Literal["not_attempted", "stored", "duplicate", "write_error"] = (
        "not_attempted"
    )


class FailureMemoryTelemetry(CacheComponentTelemetry):
    fingerprint: str | None = None
    reused: bool = False
    fresh_verification_passed: bool | None = None
    reuse_attempt: VerificationAttempt | None = None
    llm_calls_avoided: int = Field(default=0, ge=0, le=1)
    historical_input_tokens_avoided: int | None = Field(default=None, ge=0)
    historical_total_tokens_avoided: int | None = Field(default=None, ge=0)
    historical_cost_avoided_usd: float | None = Field(default=None, ge=0.0)


class CacheTelemetry(StrictModel):
    failure_memory: FailureMemoryTelemetry
    provider_schema: CacheComponentTelemetry
    schema_slice: CacheComponentTelemetry


class ResultDocument(StrictModel):
    status: Literal["ok", "error"]
    repository: RepositoryInfo | None = None
    terraform: TerraformInfo | None = None
    failure: FailureInfo | None = None
    context: ContextSelection | None = None
    diagnosis: Diagnosis | None = None
    timing: dict[str, float] = Field(default_factory=dict)
    token_usage: TokenUsage = Field(default_factory=TokenUsage)
    llm_usage: LLMUsage = Field(default_factory=LLMUsage)
    llm_calls: list[LLMInvocation] = Field(default_factory=list)
    context_telemetry: ContextTelemetry | None = None
    context_manifest: ContextManifest | None = None
    context_optimization: ContextOptimization | None = None
    schema_slice_manifest: list[SchemaSliceManifest] = Field(default_factory=list)
    schema_optimization: SchemaOptimization | None = None
    context_progression: ContextProgression | None = None
    model_progression: ModelProgression | None = None
    resolution_source: Literal["llm", "verified_failure_memory"] | None = None
    cache: CacheTelemetry | None = None
    verified_patch: VerifiedPatchArtifact | None = None
    source_provenance: SourceProvenance | None = None
    verification_provenance: VerificationProvenance | None = None
    verification_assessment: VerificationAssessment | None = None
    mutation_eligibility: MutationEligibility | None = None
    warnings: list[str] = Field(default_factory=list)
    error: str | None = None
    error_code: ProviderFailureCategory | None = None
    routing_error_code: ModelRoutingErrorCode | None = None
