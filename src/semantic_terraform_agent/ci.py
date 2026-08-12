"""Bounded, redacted rendering for GitHub Actions integration."""

from __future__ import annotations

import re
from dataclasses import dataclass

from semantic_terraform_agent.models import ResultDocument, VerificationCommand
from semantic_terraform_agent.security import redact_secrets


COMMENT_MARKER = "<!-- semantic-terraform-agent -->"
MAX_TEXT_CHARS = 2_000
MAX_PATCH_CHARS = 12_000


@dataclass(frozen=True)
class CIRenderContext:
    repository: str
    commit: str
    terraform_dir: str
    failed_stage: str
    diff_comparison: str | None = None


def _bounded(value: object, limit: int = MAX_TEXT_CHARS) -> str:
    text = redact_secrets(str(value or "unavailable"))
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n...[truncated]"


def _final_candidate(result: ResultDocument):
    if result.diagnosis is None:
        return None
    return result.diagnosis.repair or result.diagnosis.initial


def _command_line(label: str, command: VerificationCommand | None) -> str:
    if command is None:
        return f"- — {label}: not run"
    symbols = {"passed": "✓", "failed": "✗", "skipped": "—", "error": "!"}
    return f"- {symbols[command.status]} {label}: {command.status}"


def _status_label(value: str | None) -> str:
    return (value or "unavailable").replace("_", " ").upper()


def _patch_block(patch: str) -> str:
    redacted = redact_secrets(patch)
    if len(redacted) > MAX_PATCH_CHARS:
        redacted = (
            f"{redacted[:MAX_PATCH_CHARS]}\n"
            f"...[suggested patch truncated to {MAX_PATCH_CHARS} characters]"
        )
    longest = max((len(run) for run in re.findall(r"`+", redacted)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}diff\n{redacted.rstrip()}\n{fence}"


def render_pr_comment(result: ResultDocument, context: CIRenderContext) -> str:
    lines = [COMMENT_MARKER, "## Semantic Terraform Failure Agent", ""]
    if result.status != "ok" or result.diagnosis is None:
        lines.extend(
            [
                "The agent could not complete because of an infrastructure or configuration error.",
                "",
                f"**Error:** {_bounded(result.error)}",
                "",
                "Human review is required.",
                "",
            ]
        )
        return "\n".join(lines)

    diagnosis = result.diagnosis
    candidate = _final_candidate(result)
    assert candidate is not None
    affected = ", ".join(candidate.affected_resources) or "not identified"
    final_attempt = diagnosis.attempts[-1] if diagnosis.attempts else None
    commands = final_attempt.commands if final_attempt is not None else None

    lines.extend(
        [
            "### Root cause",
            _bounded(candidate.root_cause),
            "",
            "### Affected resource",
            _bounded(affected),
            "",
            "### Suggested change",
            _bounded(candidate.violated_constraint),
            "",
            "### Terraform verification",
            _command_line("patch applied in isolated workspace", commands.patch_apply if commands else None),
            _command_line("terraform fmt", commands.fmt if commands else None),
            _command_line("terraform init", commands.init if commands else None),
            _command_line(
                "terraform validate", commands.terraform_validate if commands else None
            ),
            _command_line("terraform plan", commands.plan if commands else None),
            "",
            f"**Final status:** {_status_label(diagnosis.verification_status)}",
            f"**Model confidence:** {diagnosis.model_confidence:.2f}",
            f"**Evidence score:** {diagnosis.evidence_score:.2f}",
            "",
            "Terraform verification passed." if diagnosis.verification.passed else "Terraform verification did not pass.",
            "Human review is still required; verification does not establish developer intent.",
            "",
            "<details>",
            "<summary>Suggested patch</summary>",
            "",
            _patch_block(diagnosis.final_patch),
            "",
            "</details>",
            "",
        ]
    )
    return "\n".join(lines)


def render_step_summary(result: ResultDocument, context: CIRenderContext) -> str:
    diagnosis = result.diagnosis
    candidate = _final_candidate(result)
    affected = (
        ", ".join(candidate.affected_resources)
        if candidate is not None and candidate.affected_resources
        else "not identified"
    )
    context_mode = result.context.selected_mode if result.context else "unavailable"
    verification = diagnosis.verification_status if diagnosis else "unavailable"
    repair_used = bool(diagnosis and diagnosis.repair is not None)
    error = _bounded(result.error) if result.status == "error" else None
    lines = [
        "# Semantic Terraform Failure Agent",
        "",
        f"- Repository: `{_bounded(context.repository)}`",
        f"- Commit: `{_bounded(context.commit)}`",
        f"- Terraform directory: `{_bounded(context.terraform_dir)}`",
        f"- Failed stage: `{_bounded(context.failed_stage)}`",
        f"- Affected resource: `{_bounded(affected)}`",
        f"- Context mode: `{context_mode}`",
        f"- Verification status: `{verification}`",
        f"- Repair used: `{'yes' if repair_used else 'no'}`",
        f"- Total runtime: `{result.timing.get('total_seconds', 'unavailable')} seconds`",
        f"- Input tokens: `{result.token_usage.input_tokens if result.token_usage.input_tokens is not None else 'unavailable'}`",
        f"- Output tokens: `{result.token_usage.output_tokens if result.token_usage.output_tokens is not None else 'unavailable'}`",
    ]
    if context.diff_comparison:
        lines.append(f"- Diff comparison: `{_bounded(context.diff_comparison)}`")
    if error:
        lines.extend(["", f"**Agent error:** {error}"])
    lines.extend(
        [
            "",
            "No source files were changed. Human review and an explicit application of any suggested patch are required.",
            "",
        ]
    )
    return "\n".join(lines)
