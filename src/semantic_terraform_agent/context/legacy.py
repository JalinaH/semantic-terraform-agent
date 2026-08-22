"""v0.5 prompt measurement helpers retained only for evaluation comparisons."""

from __future__ import annotations

import json

from semantic_terraform_agent.config import DEFAULT_LIMITS
from semantic_terraform_agent.models import DiagnosisRequest
from semantic_terraform_agent.reasoning.prompt_models import PromptParts
from semantic_terraform_agent.security import redact_secrets


def legacy_relevant_sources(
    all_sources: dict[str, str],
    resources: list,
    changed_files: tuple[str, ...],
    failure_file: str | None,
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
        selected.extend(
            path
            for path in all_sources
            if path == failure_file or path.endswith(f"/{failure_file}")
        )
    if not selected:
        selected = list(all_sources)
    return {
        path: all_sources[path]
        for path in dict.fromkeys(selected)
        if path in all_sources
    }


def build_legacy_prompt_parts(request: DiagnosisRequest) -> PromptParts:
    failure = request.failure.model_dump(mode="json", exclude={"original_log"})
    failure["log_excerpt"] = request.failure.original_log[
        : DEFAULT_LIMITS.max_prompt_log_chars
    ]
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
        "relevant_provider_schemas": _legacy_schema_context(request),
    }
    encoded = redact_secrets(json.dumps(payload, indent=2, sort_keys=True))
    system = """You are diagnosing a Terraform failure from an arbitrary repository.

Use only the evidence supplied below. Do not invent files, resource addresses, provider
constraints, or successful verification. Return a minimal candidate patch as a unified
diff when the evidence supports one. Use the exact repository-relative paths shown in
relevant_terraform_source, with `a/` and `b/` prefixes in patch headers. Every hunk
header's old/new line counts must exactly match its context, removed, and added lines.
Never suggest running terraform apply or destroy.

Return JSON matching the supplied response schema. Evidence source must be one of:
terraform_error, terraform_source, git_diff, provider_schema. Confidence is your model
estimate only, between 0 and 1.

Return only the JSON response with no Markdown fence, preamble, or trailing commentary."""
    return PromptParts(
        system=system,
        user=f"""DIAGNOSTIC CONTEXT
{encoded}
""",
    )


def _legacy_schema_context(request: DiagnosisRequest) -> list[dict]:
    if request.context.selected_mode != "schema-aware":
        return []
    return [
        record.model_dump(mode="json", by_alias=True)
        for record in request.schemas
        if record.extraction_status == "ok" and record.resource_schema is not None
    ]
