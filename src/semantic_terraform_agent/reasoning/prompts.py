"""Prompt construction with bounded, redacted diagnostic context."""

from __future__ import annotations

import json
import re

from semantic_terraform_agent.config import DEFAULT_LIMITS
from semantic_terraform_agent.models import DiagnosisRequest


SECRET_PATTERNS = (
    re.compile(r"(?i)(api[_-]?key|secret|token|password)(\s*[:=]\s*)([^\s,;]+)"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"),
)


def redact_secrets(value: str) -> str:
    redacted = value
    for index, pattern in enumerate(SECRET_PATTERNS):
        if index == 0:
            redacted = pattern.sub(r"\1\2[REDACTED]", redacted)
        else:
            redacted = pattern.sub("[REDACTED]", redacted)
    return redacted


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
