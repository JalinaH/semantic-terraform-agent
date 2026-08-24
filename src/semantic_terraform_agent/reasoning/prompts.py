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
    "diagnostic_source",
    "patch_failure",
    "escalation_evidence",
)


def _schema_context(request: DiagnosisRequest) -> list[dict]:
    if request.context.selected_mode == "lightweight":
        return []
    if request.context.selected_mode == "progressive" and not request.schema_slices:
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
candidate as structured exact-source edits when the evidence supports one. Each edit must
contain only file, old_text, and new_text. Use exact repository-relative Terraform paths
and exact source excerpts. Do not generate unified-diff headers (`---`, `+++`, or `@@`);
the agent constructs the Git patch deterministically. Never suggest running terraform
apply or destroy. Set suggested_patch to null for the normal structured-edit path.

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
        mode="json", exclude={"suggested_patch", "edits"}
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
            "ORIGINAL CANDIDATE REPRESENTATION\n"
            + (
                request.previous_diagnosis.suggested_patch
                or json.dumps(
                    {"edits": [edit.model_dump() for edit in request.previous_diagnosis.edits]},
                    separators=(",", ":"),
                    sort_keys=True,
                )
            )
        ),
        "verification_evidence": _render_verification_evidence(request),
        "diagnostic_source": _render_diagnostic_source(request),
        "patch_failure": _render_patch_failure(request),
        "escalation_evidence": _render_escalation_evidence(request),
        "metadata": _render_metadata(request.original),
        "provider_schema": _render_schema(request.original),
    }
    if request.repair_reason == "malformed_patch_to_structured_edit":
        opening = """The Terraform diagnosis is already complete, but its legacy unified
diff is malformed. Convert only the intended change into corrected structured exact-source
edits. Do not serialize another unified diff."""
    elif request.repair_reason == "structured_edit_repair":
        opening = """The Terraform diagnosis is already complete, but deterministic edit
construction rejected the candidate. Correct only the structured exact-source edits."""
    elif request.second_attempt_reason.value == "context_escalation":
        opening = """The immutable Terraform diagnosis remains authoritative. Additional
deterministically selected provider-schema context is available. Return only corrected
structured edits that address the verification evidence."""
    else:
        opening = """The immutable Terraform diagnosis remains authoritative. Return only
corrected structured edits that address the verification evidence."""
    system = f"""{opening}

Use only the evidence supplied below. Do not rediagnose the Terraform failure. Do not
return root_cause, affected_resources, violated_constraint, confidence, evidence,
suggested_patch, explanation, Markdown, or unified-diff syntax. Return exactly one JSON
object with an `edits` array; each edit contains only `file`, `old_text`, and `new_text`.
Use exact source text and existing Terraform files only. Do not add files. Only one bounded
second attempt is allowed."""
    return _parts_from_sections(
        system,
        sections,
        selected_context_characters=context.selected_context_characters,
    )


def _render_patch_failure(request: RepairRequest) -> str:
    attempt = request.failed_attempt
    if attempt.failure_category is None and attempt.failure_reason_code is None:
        return ""
    payload = {
        "category": (
            attempt.failure_category.value if attempt.failure_category else None
        ),
        "reason_code": attempt.failure_reason_code,
        "description": attempt.failure_description,
    }
    return "PATCH PARSER FAILURE\n" + json.dumps(
        payload, separators=(",", ":"), sort_keys=True
    )


def _render_diagnostic_source(request: RepairRequest) -> str:
    failure = request.failed_attempt.plan_failure
    if failure is None or failure.source_file is None or failure.source_line is None:
        return ""
    normalized = failure.source_file.replace("\\", "/")
    matches = [
        (path, source)
        for path, source in request.original.relevant_sources.items()
        if path == normalized or path.endswith(f"/{normalized}")
    ]
    if len(matches) != 1:
        return ""
    path, source = matches[0]
    return (
        "TERRAFORM SOURCE AT PLAN DIAGNOSTIC LOCATION\n"
        f"File: {path}\nReported line: {failure.source_line}\n{source}"
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
    if request.context_level is not None:
        lines.insert(2, f"Context level: {request.context_level.value}")
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
    if request.failed_attempt.plan_failure is not None:
        lines.extend(
            (
                "Bounded structured plan diagnostic:",
                json.dumps(
                    request.failed_attempt.plan_failure.model_dump(mode="json"),
                    separators=(",", ":"),
                    sort_keys=True,
                ),
            )
        )
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


def _render_escalation_evidence(request: RepairRequest) -> str:
    decision = request.escalation_decision
    if decision is None or request.second_attempt_reason.value != "context_escalation":
        return ""
    lines = [
        "CONTEXT ESCALATION DECISION",
        f"From: {decision.from_level.value}",
        f"To: {(decision.to_level.value if decision.to_level else 'none')}",
        f"Reason code: {decision.reason_code}",
        f"Reason: {decision.reason}",
        "Signals:",
    ]
    lines.extend(f"- {signal}" for signal in decision.signals)
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
        "second_attempt_reason": request.second_attempt_reason.value,
        "repair_reason": request.repair_reason,
        "patch_failure": {
            "category": (
                request.failed_attempt.failure_category.value
                if request.failed_attempt.failure_category
                else None
            ),
            "reason_code": request.failed_attempt.failure_reason_code,
            "description": request.failed_attempt.failure_description,
        },
        "plan_failure": (
            request.failed_attempt.plan_failure.model_dump(mode="json")
            if request.failed_attempt.plan_failure is not None
            else None
        ),
        "escalation_decision": (
            request.escalation_decision.model_dump(mode="json")
            if request.escalation_decision
            else None
        ),
    }
    encoded = redact_secrets(json.dumps(payload, indent=2, sort_keys=True))
    system = """The first Terraform diagnosis is immutable and authoritative. Return only
corrected structured exact-source edits for the existing Terraform files shown in the
context. Do not rediagnose the failure. Do not return diagnosis fields, explanation,
Markdown, suggested_patch, or unified-diff syntax (`---`, `+++`, `@@`). Return exactly one
JSON object with an `edits` array; each edit contains only file, old_text, and new_text.
Only one bounded second attempt is allowed."""
    return PromptParts(
        system=system,
        user=f"""REPAIR CONTEXT
{encoded}
""",
    )
