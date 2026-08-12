"""Strict data contracts used at collection, reasoning, and output boundaries."""

from __future__ import annotations

from typing import Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", protected_namespaces=(), populate_by_name=True)


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


class ContextSelection(StrictModel):
    requested_mode: Literal["lightweight", "schema-aware", "auto"]
    selected_mode: Literal["lightweight", "schema-aware"]
    selection_reason: str


class EvidenceItem(StrictModel):
    source: Literal["terraform_error", "terraform_source", "git_diff", "provider_schema"]
    detail: str


class ModelDiagnosis(StrictModel):
    root_cause: str = Field(min_length=1)
    affected_resources: list[str]
    violated_constraint: str = Field(min_length=1)
    suggested_patch: str = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: list[EvidenceItem]


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


class DiagnosisCandidate(StrictModel):
    root_cause: str
    affected_resources: list[str]
    violated_constraint: str
    suggested_patch: str
    model_confidence: float
    evidence: list[EvidenceItem]


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


class TokenUsage(StrictModel):
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None


class DiagnosisRequest(StrictModel):
    failure: FailureInfo
    resources: list[ResourceCandidate]
    relevant_sources: dict[str, str]
    git_diff: str
    context: ContextSelection
    schemas: list[SchemaRecord]
    terraform_version: str | None = None


class RepairRequest(StrictModel):
    original: DiagnosisRequest
    previous_diagnosis: ModelDiagnosis
    failed_attempt: VerificationAttempt


class ProviderResponse(StrictModel):
    diagnosis: ModelDiagnosis
    token_usage: TokenUsage = Field(default_factory=TokenUsage)


class ResultDocument(StrictModel):
    status: Literal["ok", "error"]
    repository: RepositoryInfo | None = None
    terraform: TerraformInfo | None = None
    failure: FailureInfo | None = None
    context: ContextSelection | None = None
    diagnosis: Diagnosis | None = None
    timing: dict[str, float] = Field(default_factory=dict)
    token_usage: TokenUsage = Field(default_factory=TokenUsage)
    warnings: list[str] = Field(default_factory=list)
    error: str | None = None
