"""Deterministic unified-diff construction from bounded exact semantic edits."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from semantic_terraform_agent.collectors.repository import (
    RepositoryLayout,
    safe_repo_file,
)
from semantic_terraform_agent.config import DEFAULT_LIMITS
from semantic_terraform_agent.models import SemanticEditSet
from semantic_terraform_agent.terraform.verification import validate_patch_scope


@dataclass(frozen=True)
class StructuredEditFailure(ValueError):
    code: str
    description: str
    repairable: bool

    def __str__(self) -> str:
        return self.description


@dataclass(frozen=True)
class BuiltPatch:
    patch: str
    affected_files: tuple[str, ...]
    edit_count: int


@dataclass(frozen=True)
class _ResolvedEdit:
    file: str
    old_text: str
    new_text: str
    start: int
    end: int


def build_patch_from_edits(
    edit_set: SemanticEditSet,
    layout: RepositoryLayout,
) -> BuiltPatch:
    """Apply exact, non-overlapping edits to snapshots and ask Git to serialize them."""
    if len(edit_set.edits) > DEFAULT_LIMITS.max_semantic_edits:
        raise StructuredEditFailure(
            "structured_edit_invalid", "too many structured edits", True
        )

    originals: dict[str, str] = {}
    originals_bytes: dict[str, bytes] = {}
    resolved: list[_ResolvedEdit] = []
    seen: set[tuple[str, str, str]] = set()
    for edit in edit_set.edits:
        relative = _resolve_edit_path(edit.file, layout)
        identity = (relative, edit.old_text, edit.new_text)
        if identity in seen:
            raise StructuredEditFailure(
                "duplicate_edits", "structured edits contain a duplicate edit", True
            )
        seen.add(identity)
        if relative not in originals:
            repository_path = layout.root / relative
            if repository_path.is_symlink():
                raise StructuredEditFailure(
                    "invalid_edit_path", "structured edits may not target symlinks", False
                )
            path = safe_repo_file(layout.root, relative)
            try:
                raw = path.read_bytes()
                source = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise StructuredEditFailure(
                    "structured_edit_invalid",
                    "Terraform source is not valid UTF-8",
                    False,
                ) from exc
            originals[relative] = source
            originals_bytes[relative] = raw
        source = originals[relative]
        matches = _match_offsets(source, edit.old_text)
        if not matches:
            raise StructuredEditFailure(
                "edit_target_not_found",
                f"structured edit target was not found in {relative}",
                True,
            )
        if len(matches) > 1:
            raise StructuredEditFailure(
                "edit_target_ambiguous",
                f"structured edit target is ambiguous in {relative}",
                True,
            )
        start = matches[0]
        resolved.append(
            _ResolvedEdit(
                file=relative,
                old_text=edit.old_text,
                new_text=edit.new_text,
                start=start,
                end=start + len(edit.old_text),
            )
        )

    modified: dict[str, str] = {}
    for relative, source in originals.items():
        file_edits = sorted(
            (edit for edit in resolved if edit.file == relative),
            key=lambda item: (item.start, item.end),
        )
        for previous, current in zip(file_edits, file_edits[1:], strict=False):
            if current.start < previous.end:
                raise StructuredEditFailure(
                    "overlapping_edits",
                    f"structured edits overlap in {relative}",
                    True,
                )
        updated = source
        for edit in reversed(file_edits):
            updated = updated[: edit.start] + edit.new_text + updated[edit.end :]
        modified[relative] = updated

    patch = _git_diff(originals_bytes, modified)
    if not patch.strip():
        raise StructuredEditFailure(
            "empty_edit", "structured edits produced no repository change", True
        )
    try:
        affected = validate_patch_scope(patch, layout)
    except ValueError as exc:
        raise StructuredEditFailure(
            "structured_edit_invalid",
            "deterministic patch failed the existing patch-scope validator",
            False,
        ) from exc
    return BuiltPatch(
        patch=patch,
        affected_files=tuple(affected),
        edit_count=len(resolved),
    )


def _resolve_edit_path(raw: str, layout: RepositoryLayout) -> str:
    value = raw.strip().replace("\\", "/")
    path = PurePosixPath(value)
    if (
        not value
        or value.startswith("/")
        or "\\" in raw
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise StructuredEditFailure(
            "invalid_edit_path", "structured edit path is unsafe", False
        )
    candidates = [path.as_posix()]
    if layout.terraform_dir not in {"", "."}:
        candidates.append((PurePosixPath(layout.terraform_dir) / path).as_posix())
    allowed = set(layout.terraform_files)
    for candidate in dict.fromkeys(candidates):
        if candidate in allowed:
            return candidate
    if not value.endswith((".tf", ".tf.json")):
        raise StructuredEditFailure(
            "invalid_edit_path", "structured edits may target only Terraform files", False
        )
    raise StructuredEditFailure(
        "invalid_edit_path",
        "structured edit path is not an existing file in the selected Terraform directory",
        True,
    )


def _match_offsets(source: str, target: str) -> list[int]:
    result: list[int] = []
    offset = 0
    while True:
        match = source.find(target, offset)
        if match < 0:
            return result
        result.append(match)
        offset = match + max(1, len(target))


def _git_diff(originals: dict[str, bytes], modified: dict[str, str]) -> str:
    git = shutil.which("git")
    if git is None:
        raise StructuredEditFailure(
            "structured_edit_invalid",
            "Git is required for deterministic patch construction",
            False,
        )
    with tempfile.TemporaryDirectory(prefix="semantic-terraform-edit-") as temporary:
        root = Path(temporary)
        _run_git([git, "init", "-q"], root)
        _run_git([git, "config", "core.autocrlf", "false"], root)
        for relative, raw in originals.items():
            destination = root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(raw)
        _run_git([git, "add", "--", *sorted(originals)], root)
        for relative, text in modified.items():
            (root / relative).write_bytes(text.encode("utf-8"))
        result = subprocess.run(
            [
                git,
                "diff",
                "--no-ext-diff",
                "--no-color",
                "--no-renames",
                "--src-prefix=a/",
                "--dst-prefix=b/",
                "--",
                *sorted(originals),
            ],
            cwd=root,
            check=False,
            capture_output=True,
        )
        if result.returncode != 0:
            raise StructuredEditFailure(
                "structured_edit_invalid",
                "Git could not construct a deterministic patch",
                False,
            )
        try:
            patch = result.stdout.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise StructuredEditFailure(
                "structured_edit_invalid",
                "Git produced a non-UTF-8 patch",
                False,
            ) from exc
    if len(patch.encode("utf-8")) > DEFAULT_LIMITS.max_patch_bytes:
        raise StructuredEditFailure(
            "structured_edit_invalid", "deterministic patch exceeds the patch budget", False
        )
    return patch


def _run_git(command: list[str], cwd: Path) -> None:
    result = subprocess.run(command, cwd=cwd, check=False, capture_output=True)
    if result.returncode != 0:
        raise StructuredEditFailure(
            "structured_edit_invalid",
            "Git could not prepare deterministic patch construction",
            False,
        )
