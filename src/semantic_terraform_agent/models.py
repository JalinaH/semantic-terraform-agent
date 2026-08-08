"""Strict data contracts used at collection, reasoning, and output boundaries."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", protected_namespaces=(), populate_by_name=True)


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
    stage: Literal["init", "fmt", "validate", "plan", "apply", "unknown"] = "unknown"
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


class Diagnosis(StrictModel):
    root_cause: str
    affected_resources: list[str]
    violated_constraint: str
    suggested_patch: str
    model_confidence: float
    evidence_score: float
    evidence: list[EvidenceItem]


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
