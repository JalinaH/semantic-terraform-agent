"""Minimal prompt rendering over structured deterministic diagnosis context."""

from __future__ import annotations

import json

from semantic_terraform_agent.config import DEFAULT_LIMITS
from semantic_terraform_agent.context.legacy import build_legacy_prompt_parts
from semantic_terraform_agent.models import (
    ContextFailure,
    ContextSourceBlock,
    DiagnosisRequest,
    RepairRequest,
    VerificationCommand,
)
from semantic_terraform_agent.reasoning.prompt_models import PromptParts
from semantic_terraform_agent.security import redact_secrets


_SECTION_NAMES = (
    "terraform_error",
    "git_diff",
    "terraform_source",
    "supporting_context",
    "metadata",
    "provider_schema",
    "original_diagnosis",
    "original_patch",
    "verification_evidence",
)


def _schema_context(request: DiagnosisRequest) -> list[dict]:
    if request.context.selected_mode != "schema-aware":
        return []
    if request.schema_strategy == "sliced" and request.schema_slices:
        return [
            item.model_dump(
                mode="json",
                by_alias=True,
                include={
                    "resource_type",
                    "provider_source",
                    "provider_version",
                    "selected_schema",
                },
            )
            for item in request.schema_slices
        ]
    return [
        record.model_dump(mode="json", by_alias=True)
        for record in request.schemas
        if record.extraction_status == "ok" and record.resource_schema is not None
    ]


def build_prompt_parts(request: DiagnosisRequest) -> PromptParts:
    context = request.diagnosis_context
    if context is None:
        return build_legacy_prompt_parts(request)

    sections: dict[str, str] = {
        "terraform_error": _render_failure(context.failure),
        "git_diff": _render_changes(context.changed_lines),
        "terraform_source": _render_blocks(
            "AFFECTED TERRAFORM BLOCK",
            context.resource_blocks,
        ),
        "supporting_context": _render_blocks(
            "SUPPORTING TERRAFORM DEFINITIONS",
            context.supporting_blocks,
        ),
        "metadata": _render_metadata(request),
        "provider_schema": _render_schema(request),
    }
    system = """You are diagnosing a Terraform failure from an arbitrary repository.

Use only the evidence supplied below. Do not invent files, resource addresses, provider
constraints, or successful verification. Critical Terraform source and diff lines are
exact excerpts; a section explicitly marked truncated is incomplete. Return a minimal
candidate patch as a unified diff when the evidence supports one. Use the exact
repository-relative paths shown below, with `a/` and `b/` prefixes in patch headers.
Every hunk header's old/new line counts must exactly match its context, removed, and added
lines. Never suggest running terraform apply or destroy.

Return JSON matching the supplied response schema. Evidence source must be one of:
terraform_error, terraform_source, git_diff, provider_schema. Confidence is your model
estimate only, between 0 and 1. Return only the JSON response with no Markdown fence,
preamble, or trailing commentary."""
    return _parts_from_sections(
        system,
        sections,
        selected_context_characters=context.selected_context_characters,
    )


def build_prompt(request: DiagnosisRequest) -> str:
    return build_prompt_parts(request).combined


def _failed_command(request: RepairRequest) -> VerificationCommand | None:
    stage = request.failed_attempt.failed_stage
    if stage is None:
        return None
    attribute = "terraform_validate" if stage == "validate" else stage
    return getattr(request.failed_attempt.commands, attribute)


def build_repair_prompt_parts(request: RepairRequest) -> PromptParts:
    context = request.original.diagnosis_context
    if context is None:
        return _build_legacy_repair_prompt_parts(request)

    previous = request.previous_diagnosis.model_dump(
        mode="json", exclude={"suggested_patch"}
    )
    sections: dict[str, str] = {
        "terraform_error": _render_failure(context.failure),
        "git_diff": _render_changes(context.changed_lines),
        "terraform_source": _render_blocks(
            "AFFECTED TERRAFORM BLOCK",
            context.resource_blocks,
        ),
        "supporting_context": _render_blocks(
            "SUPPORTING TERRAFORM DEFINITIONS",
            context.supporting_blocks,
        ),
        "original_diagnosis": (
            "ORIGINAL DIAGNOSIS\n"
            + json.dumps(previous, separators=(",", ":"), sort_keys=True)
        ),
        "original_patch": (
            "ORIGINAL CANDIDATE PATCH\n" + request.previous_diagnosis.suggested_patch
        ),
        "verification_evidence": _render_verification_evidence(request),
        "metadata": _render_metadata(request.original),
        "provider_schema": _render_schema(request.original),
    }
    system = """The previous candidate patch did not pass Terraform verification. Produce
one revised patch that addresses the verification evidence while preserving the intended
root-cause fix.

Use only the evidence supplied below. Preserve the original diagnosis unless verification
directly contradicts it. Return a complete diagnosis using the same strict JSON response
schema, with a minimal unified diff in suggested_patch. Critical Terraform source and diff
lines are exact excerpts. Use the exact repository-relative paths shown below, with `a/`
and `b/` patch prefixes and exact hunk line counts. Never suggest terraform apply or
destroy. Only one repair is allowed.

Evidence source must be one of terraform_error, terraform_source, git_diff,
provider_schema. Confidence remains a model estimate between 0 and 1, not a verification
result. Return only the JSON response with no Markdown fence, preamble, or trailing
commentary."""
    return _parts_from_sections(
        system,
        sections,
        selected_context_characters=context.selected_context_characters,
    )


def build_repair_prompt(request: RepairRequest) -> str:
    return build_repair_prompt_parts(request).combined


def _parts_from_sections(
    system: str,
    sections: dict[str, str],
    *,
    selected_context_characters: int | None,
) -> PromptParts:
    safe_system = redact_secrets(system)
    safe_sections = {
        name: redact_secrets(value).strip()
        for name, value in sections.items()
        if value.strip()
    }
    user = "\n\n".join(safe_sections.values()) + "\n"
    measurements = {name: 0 for name in _SECTION_NAMES}
    measurements.update({name: len(value) for name, value in safe_sections.items()})
    return PromptParts(
        system=safe_system,
        user=user,
        section_characters=measurements,
        selected_context_characters=selected_context_characters,
    )


def _render_failure(failure: ContextFailure) -> str:
    lines = [
        "TERRAFORM FAILURE",
        f"Stage: {failure.stage}",
        f"Summary: {failure.summary}",
        f"Detail: {failure.detail}",
    ]
    if failure.resource_address:
        lines.append(f"Resource address: {failure.resource_address}")
    if failure.referenced_file:
        location = failure.referenced_file
        if failure.referenced_line:
            location += f":{failure.referenced_line}"
        lines.append(f"Source location: {location}")
    if failure.diagnostic_excerpt:
        lines.extend(("Diagnostic excerpt:", failure.diagnostic_excerpt))
    return "\n".join(lines)


def _render_changes(changes: list) -> str:
    if not changes:
        return ""
    rendered: list[str] = ["RELEVANT TERRAFORM CHANGE"]
    for change in changes:
        if change.truncated:
            rendered.append(
                f"File: {change.file} | Truncated: relevant_diff_exceeded_limit"
            )
        rendered.append(change.rendered)
    return "\n\n".join(rendered)


def _render_blocks(title: str, blocks: list[ContextSourceBlock]) -> str:
    if not blocks:
        return ""
    rendered: list[str] = [title]
    for block in blocks:
        metadata = (
            f"File: {block.file} | Lines: {block.start_line}-{block.end_line} | "
            f"{block.kind}: {block.identifier}"
        )
        if block.truncated:
            metadata += f" | Truncated: {block.truncation_reason}"
        rendered.extend((metadata, block.source))
    return "\n".join(rendered)


def _render_metadata(request: DiagnosisRequest) -> str:
    context = request.diagnosis_context
    if context is None or not context.metadata:
        return ""
    lines = [
        "CONTEXT METADATA",
        f"Mode: {request.context.selected_mode}",
        f"Strategy: {context.optimization.strategy}",
        f"Ambiguous candidates: {'yes' if context.manifest.ambiguous else 'no'}",
    ]
    if request.terraform_version:
        lines.append(f"Terraform version: {request.terraform_version}")
    if context.unresolved_symbols:
        lines.append("Unresolved symbols: " + ", ".join(context.unresolved_symbols))
    return "\n".join(lines)


def _render_schema(request: DiagnosisRequest) -> str:
    schemas = _schema_context(request)
    if not schemas:
        return ""
    return "RELEVANT PROVIDER SCHEMA\n" + json.dumps(
        schemas, separators=(",", ":"), sort_keys=True
    )


def _render_verification_evidence(request: RepairRequest) -> str:
    failed = _failed_command(request)
    lines = [
        "FAILED VERIFICATION EVIDENCE",
        f"Stage: {request.failed_attempt.failed_stage}",
    ]
    if failed is None:
        return "\n".join(lines)
    output = redact_secrets(
        f"STDOUT:\n{failed.stdout}\nSTDERR:\n{failed.stderr}"
    )[: DEFAULT_LIMITS.max_verification_output_chars]
    lines.extend(
        (
            "Command: " + " ".join(failed.command),
            f"Status: {failed.status}",
            f"Exit code: {failed.exit_code}",
            "Output excerpt:",
            output,
        )
    )
    return "\n".join(lines)


def _build_legacy_repair_prompt_parts(request: RepairRequest) -> PromptParts:
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
    system = """The previous candidate patch did not pass Terraform verification. Produce
one revised patch that addresses the verification evidence while preserving the intended
root-cause fix.

Use only the evidence supplied below. Preserve the original diagnosis unless the
verification evidence directly contradicts it. Return a complete diagnosis using the same
strict JSON response schema, with a minimal unified diff in suggested_patch. Do not invent
successful verification. Use the exact repository-relative paths shown in
relevant_terraform_source, with `a/` and `b/` prefixes in patch headers. Every hunk
header's old/new line counts must exactly match its context, removed, and added lines.
Never suggest terraform apply or destroy.

Only one repair is allowed. Evidence source must be one of: terraform_error,
terraform_source, git_diff, provider_schema. Confidence remains a model estimate between
0 and 1, not a verification result.

Return only the JSON response with no Markdown fence, preamble, or trailing commentary."""
    return PromptParts(
        system=system,
        user=f"""REPAIR CONTEXT
{encoded}
""",
    )
