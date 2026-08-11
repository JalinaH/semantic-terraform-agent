"""Prompt construction with bounded, redacted diagnostic context."""

from __future__ import annotations

import json

from semantic_terraform_agent.config import DEFAULT_LIMITS
from semantic_terraform_agent.models import DiagnosisRequest, RepairRequest, VerificationCommand
from semantic_terraform_agent.security import redact_secrets


def _schema_context(request: DiagnosisRequest) -> list[dict]:
    if request.context.selected_mode != "schema-aware":
        return []
    return [
        record.model_dump(mode="json", by_alias=True)
        for record in request.schemas
        if record.extraction_status == "ok" and record.resource_schema is not None
    ]


def build_prompt(request: DiagnosisRequest) -> str:
    failure = request.failure.model_dump(mode="json", exclude={"original_log"})
    failure["log_excerpt"] = request.failure.original_log[: DEFAULT_LIMITS.max_prompt_log_chars]
    payload = {
        "selected_context_mode": request.context.selected_mode,
        "failure": failure,
        "candidate_resources": [
            candidate.model_dump(mode="json", exclude={"source"})
            for candidate in request.resources
        ],
        "relevant_terraform_source": request.relevant_sources,
        "git_diff": request.git_diff,
        "terraform_version": request.terraform_version,
        "relevant_provider_schemas": _schema_context(request),
    }
    encoded = redact_secrets(json.dumps(payload, indent=2, sort_keys=True))
    return f"""You are diagnosing a Terraform failure from an arbitrary repository.

Use only the evidence supplied below. Do not invent files, resource addresses, provider
constraints, or successful verification. Return a minimal candidate patch as a unified
diff when the evidence supports one. Never suggest running terraform apply or destroy.

Return JSON matching the supplied response schema. Evidence source must be one of:
terraform_error, terraform_source, git_diff, provider_schema. Confidence is your model
estimate only, between 0 and 1.

DIAGNOSTIC CONTEXT
{encoded}
"""


def _failed_command(request: RepairRequest) -> VerificationCommand | None:
    stage = request.failed_attempt.failed_stage
    if stage is None:
        return None
    attribute = "terraform_validate" if stage == "validate" else stage
    return getattr(request.failed_attempt.commands, attribute)


def build_repair_prompt(request: RepairRequest) -> str:
    original = request.original
    failure = original.failure.model_dump(mode="json", exclude={"original_log"})
    failure["log_excerpt"] = original.failure.original_log[
        : DEFAULT_LIMITS.max_prompt_log_chars
    ]
    failed = _failed_command(request)
    failed_evidence = None
    if failed is not None:
        combined_output = redact_secrets(
            f"STDOUT:\n{failed.stdout}\nSTDERR:\n{failed.stderr}"
        )[: DEFAULT_LIMITS.max_verification_output_chars]
        failed_evidence = {
            "command": failed.command,
            "status": failed.status,
            "exit_code": failed.exit_code,
            "output_excerpt": combined_output,
        }
    payload = {
        "failure": failure,
        "relevant_terraform_source": original.relevant_sources,
        "git_diff": original.git_diff,
        "original_diagnosis": request.previous_diagnosis.model_dump(mode="json"),
        "original_candidate_patch": request.previous_diagnosis.suggested_patch,
        "failed_verification_stage": request.failed_attempt.failed_stage,
        "failed_command_evidence": failed_evidence,
        "relevant_provider_schemas": _schema_context(original),
    }
    encoded = redact_secrets(json.dumps(payload, indent=2, sort_keys=True))
    return f"""The previous candidate patch did not pass Terraform verification. Produce
one revised patch that addresses the verification evidence while preserving the intended
root-cause fix.

Use only the evidence supplied below. Preserve the original diagnosis unless the
verification evidence directly contradicts it. Return a complete diagnosis using the same
strict JSON response schema, with a minimal unified diff in suggested_patch. Do not invent
successful verification and never suggest terraform apply or destroy.

Only one repair is allowed. Evidence source must be one of: terraform_error,
terraform_source, git_diff, provider_schema. Confidence remains a model estimate between
0 and 1, not a verification result.

REPAIR CONTEXT
{encoded}
"""
