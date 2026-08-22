"""Strict persisted cache contracts with bounded provenance."""

from __future__ import annotations

from datetime import datetime, timezone

from pydantic import Field

from semantic_terraform_agent.models import DiagnosisCandidate, StrictModel


class VerifiedFailureEntry(StrictModel):
    fingerprint_version: str
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    repository_scope: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: str
    agent_version: str
    failure_signature: str = Field(max_length=2_000)
    failed_stage: str
    resource_type: str | None = None
    resource_address: str | None = None
    terraform_version: str | None = None
    provider_lock_fingerprint: str | None = None
    candidate_patch: str
    diagnosis: DiagnosisCandidate
    evidence_score: float = Field(ge=0.0, le=1.0)
    verification_status: str
    historical_input_tokens: int | None = Field(default=None, ge=0)
    historical_total_tokens: int | None = Field(default=None, ge=0)
    historical_cost_usd: float | None = Field(default=None, ge=0)
    rejection_count: int = Field(default=0, ge=0)

    @classmethod
    def timestamp(cls) -> str:
        return datetime.now(timezone.utc).isoformat()


class SchemaSliceCachePayload(StrictModel):
    slices: list[dict]
    optimization: dict | None = None
