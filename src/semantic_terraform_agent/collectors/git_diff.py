"""Collection and parsing of supplied or local Git diffs."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from semantic_terraform_agent.config import DEFAULT_LIMITS, InputError, resolve_existing_file
from semantic_terraform_agent.collectors.repository import RepositoryLayout, safe_repo_file


@dataclass(frozen=True)
class DiffData:
    text: str
    source: str
    comparison: str | None
    changed_files: tuple[str, ...]
    changed_lines: dict[str, tuple[int, ...]]
    warnings: tuple[str, ...] = ()


_DIFF_PATH = re.compile(r"^\+\+\+\s+(?:b/)?(.+)$", re.MULTILINE)
_HUNK = re.compile(r"^@@\s+-\d+(?:,\d+)?\s+\+(\d+)(?:,(\d+))?\s+@@")


def parse_changed_files(diff: str, layout: RepositoryLayout) -> tuple[str, ...]:
    found: list[str] = []
    allowed = set(layout.terraform_files)
    for match in _DIFF_PATH.finditer(diff):
        relative = match.group(1).strip()
        if relative == "/dev/null":
            continue
        # Validate every path surfaced by a diff before using it.
        safe_repo_file(layout.root, relative)
        if relative.endswith((".tf", ".tf.json")) and relative in allowed:
            found.append(relative)
    return tuple(dict.fromkeys(found))


def parse_changed_lines(diff: str, layout: RepositoryLayout) -> dict[str, tuple[int, ...]]:
    changed: dict[str, set[int]] = {}
    current: str | None = None
    new_line = 0
    for raw in diff.splitlines():
        if raw.startswith("+++ "):
            candidate = re.sub(r"^\+\+\+\s+(?:b/)?", "", raw).strip()
            current = candidate if candidate in layout.terraform_files else None
            if candidate != "/dev/null":
                safe_repo_file(layout.root, candidate)
            continue
        hunk = _HUNK.match(raw)
        if hunk:
            new_line = int(hunk.group(1))
            continue
        if current is None or raw.startswith(("diff ", "index ", "--- ")):
            continue
        if raw.startswith("+") and not raw.startswith("+++"):
            changed.setdefault(current, set()).add(new_line)
            new_line += 1
        elif raw.startswith("-") and not raw.startswith("---"):
            continue
        elif not raw.startswith("\\"):
            new_line += 1
    return {path: tuple(sorted(lines)) for path, lines in changed.items()}


def _run_git_diff(root: Path) -> tuple[str, str, tuple[str, ...]]:
    if not (root / ".git").exists():
        return "", "none", ("No diff supplied and repository is not a local Git checkout.",)
    attempts = (
        (("git", "diff", "HEAD~1", "HEAD", "--"), "HEAD~1 HEAD"),
        (("git", "diff", "HEAD", "--"), "working tree vs HEAD"),
        (("git", "diff", "--cached", "--"), "index vs HEAD"),
    )
    failures: list[str] = []
    for command, label in attempts:
        try:
            completed = subprocess.run(
                command,
                cwd=root,
                capture_output=True,
                text=True,
                timeout=DEFAULT_LIMITS.command_timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            failures.append(f"{label}: {exc}")
            continue
        if completed.returncode == 0:
            if len(completed.stdout.encode()) > DEFAULT_LIMITS.max_diff_bytes:
                raise InputError("generated Git diff exceeds the configured size limit")
            return completed.stdout, label, tuple(failures)
        failures.append(f"{label}: {completed.stderr.strip() or 'Git command failed'}")
    return "", "unavailable", tuple(failures)


def collect_diff(layout: RepositoryLayout, diff_file: Path | None) -> DiffData:
    if diff_file is not None:
        path = resolve_existing_file(
            diff_file, label="diff file", max_bytes=DEFAULT_LIMITS.max_diff_bytes
        )
        text = path.read_text(encoding="utf-8", errors="replace")
        source = str(path)
        comparison = "supplied diff file"
        warnings: tuple[str, ...] = ()
    else:
        text, comparison, warnings = _run_git_diff(layout.root)
        source = "local git"
    return DiffData(
        text=text,
        source=source,
        comparison=comparison,
        changed_files=parse_changed_files(text, layout),
        changed_lines=parse_changed_lines(text, layout),
        warnings=warnings,
    )

