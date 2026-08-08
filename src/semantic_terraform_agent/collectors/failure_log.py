"""Generic parsing of human-readable and JSON Terraform diagnostics."""

from __future__ import annotations

import json
import re
from pathlib import Path

from semantic_terraform_agent.config import DEFAULT_LIMITS, resolve_existing_file
from semantic_terraform_agent.models import FailureInfo


ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
ERROR_HEADER = re.compile(r"(?:^|[│|]\s*)Error:\s*(.+?)\s*$", re.MULTILINE | re.IGNORECASE)
FILE_LINE = re.compile(r"\bon\s+(.+?\.tf(?:\.json)?)\s+line\s+(\d+)\b", re.IGNORECASE)
RESOURCE_WITH = re.compile(
    r"\bwith\s+((?:module\.[A-Za-z0-9_-]+\.)*(?:data\.)?[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+(?:\[[^\]]+\])?)\s*,?",
    re.IGNORECASE,
)
RESOURCE_INLINE = re.compile(
    r"\b((?:module\.[A-Za-z0-9_-]+\.)*(?:data\.)?[A-Za-z][A-Za-z0-9_-]*\.[A-Za-z0-9_-]+(?:\[[^\]]+\])?)\b"
)


def _clean_line(line: str) -> str:
    line = ANSI.sub("", line).strip()
    return re.sub(r"^[│|╷╵]\s?", "", line).strip()


def _parse_json_diagnostic(text: str) -> tuple[str, str, str | None, int | None, str | None] | None:
    for line in text.splitlines():
        try:
            item = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        diagnostic = item.get("diagnostic", item) if isinstance(item, dict) else {}
        if not isinstance(diagnostic, dict) or diagnostic.get("severity") not in (None, "error"):
            continue
        summary = diagnostic.get("summary")
        detail = diagnostic.get("detail", "")
        if not isinstance(summary, str):
            continue
        range_data = diagnostic.get("range") or {}
        filename = range_data.get("filename") if isinstance(range_data, dict) else None
        start = range_data.get("start") or {} if isinstance(range_data, dict) else {}
        line_no = start.get("line") if isinstance(start, dict) else None
        address = diagnostic.get("address")
        return summary, str(detail), filename, line_no, address
    return None


def _infer_stage(text: str) -> str:
    lowered = text.lower()
    explicit = re.search(r"terraform[ _-](init|fmt|validate|plan|apply)\b", lowered)
    if explicit:
        return explicit.group(1)
    signatures = (
        ("init", ("failed to install provider", "initializing the backend", "terraform init")),
        ("validate", ("terraform validate", "validate failed", "validation failed")),
        ("plan", ("planning failed", "terraform plan", "failed to create plan")),
        ("fmt", ("terraform fmt", "formatting check")),
        ("apply", ("error applying plan", "terraform apply")),
    )
    for stage, phrases in signatures:
        if any(phrase in lowered for phrase in phrases):
            return stage
    return "unknown"


def parse_failure_log(text: str) -> FailureInfo:
    clean = ANSI.sub("", text)
    parsed_json = _parse_json_diagnostic(clean)
    if parsed_json:
        summary, detail, filename, line_no, address = parsed_json
    else:
        header = ERROR_HEADER.search(clean)
        summary = _clean_line(header.group(1)) if header else "Unstructured Terraform failure"
        file_match = FILE_LINE.search(clean)
        filename = file_match.group(1).strip() if file_match else None
        line_no = int(file_match.group(2)) if file_match else None
        resource_match = RESOURCE_WITH.search(clean)
        address = resource_match.group(1) if resource_match else None

        detail_lines: list[str] = []
        if header:
            tail = clean[header.end() :]
            for line in tail.splitlines():
                value = _clean_line(line)
                if not value:
                    continue
                if FILE_LINE.search(value) or RESOURCE_WITH.search(value) or value.startswith(("on ", "with ")):
                    continue
                if value.startswith("Error:"):
                    break
                if re.match(r"^\d+:\s", value):
                    continue
                detail_lines.append(value)
        detail = "\n".join(dict.fromkeys(detail_lines)).strip()
        if not detail:
            nonempty = [_clean_line(line) for line in clean.splitlines() if _clean_line(line)]
            detail = "\n".join(nonempty[:10]) or "No diagnostic detail was present."

    if not address:
        # Only search near diagnostic wording; provider package versions and URLs can
        # otherwise resemble Terraform addresses.
        for line in clean.splitlines():
            if any(word in line.lower() for word in ("resource", "with ", "for ")):
                match = RESOURCE_INLINE.search(line)
                if match:
                    address = match.group(1)
                    break
    return FailureInfo(
        summary=summary.strip() or "Unstructured Terraform failure",
        detail=detail.strip() or "No diagnostic detail was present.",
        referenced_file=filename,
        referenced_line=line_no,
        stage=_infer_stage(clean),
        resource_address=address,
        original_log=text,
    )


def collect_failure_log(path: Path) -> FailureInfo:
    resolved = resolve_existing_file(
        path, label="failure log", max_bytes=DEFAULT_LIMITS.max_log_bytes
    )
    return parse_failure_log(resolved.read_text(encoding="utf-8", errors="replace"))
