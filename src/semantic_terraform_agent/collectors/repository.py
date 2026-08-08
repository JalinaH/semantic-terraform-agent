"""Safe discovery and bounded reading of Terraform source files."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from semantic_terraform_agent.config import DEFAULT_LIMITS, InputError


@dataclass(frozen=True)
class RepositoryLayout:
    root: Path
    terraform_root: Path
    terraform_dir: str
    terraform_files: tuple[str, ...]


def _contained_path(root: Path, candidate: Path, *, label: str) -> Path:
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise InputError(f"{label} escapes the repository root: {candidate}") from exc
    return candidate


def discover_repository(repo_path: Path, terraform_dir: Path) -> RepositoryLayout:
    try:
        root = repo_path.expanduser().resolve(strict=True)
    except FileNotFoundError as exc:
        raise InputError(f"repository path does not exist: {repo_path}") from exc
    if not root.is_dir():
        raise InputError(f"repository path is not a directory: {repo_path}")
    if terraform_dir.is_absolute():
        raise InputError("terraform directory must be relative to the repository root")

    try:
        tf_root = (root / terraform_dir).resolve(strict=True)
    except FileNotFoundError as exc:
        raise InputError(f"Terraform directory does not exist: {terraform_dir}") from exc
    _contained_path(root, tf_root, label="Terraform directory")
    if not tf_root.is_dir():
        raise InputError(f"Terraform directory is not a directory: {terraform_dir}")

    # Terraform loads configuration from one working directory. Nested .tf files are
    # local modules and are discovered separately when that directory is selected.
    files: list[str] = []
    for path in tf_root.iterdir():
        if not path.is_file() or not (path.suffix == ".tf" or path.name.endswith(".tf.json")):
            continue
        resolved_file = path.resolve(strict=True)
        _contained_path(root, resolved_file, label="Terraform source")
        files.append(path.relative_to(root).as_posix())
    files.sort()
    if not files:
        raise InputError(f"no Terraform configuration files found in {terraform_dir}")
    return RepositoryLayout(
        root=root,
        terraform_root=tf_root,
        terraform_dir=tf_root.relative_to(root).as_posix(),
        terraform_files=tuple(files),
    )


def safe_repo_file(root: Path, relative_path: str) -> Path:
    normalized = relative_path.replace("\\", "/")
    while normalized.startswith("a/") or normalized.startswith("b/"):
        normalized = normalized[2:]
    if normalized == "/dev/null":
        raise InputError("/dev/null is not a repository file")
    candidate = (root / normalized).resolve(strict=False)
    return _contained_path(root, candidate, label="file path")


def read_source_files(
    layout: RepositoryLayout,
    relative_paths: list[str] | tuple[str, ...],
) -> dict[str, str]:
    result: dict[str, str] = {}
    allowed = set(layout.terraform_files)
    for relative in dict.fromkeys(relative_paths):
        if relative not in allowed:
            continue
        path = safe_repo_file(layout.root, relative)
        if not path.is_file():
            continue
        size = path.stat().st_size
        if size > DEFAULT_LIMITS.max_source_bytes:
            raise InputError(f"Terraform source exceeds size limit: {relative}")
        result[relative] = path.read_text(encoding="utf-8", errors="replace")
    return result
