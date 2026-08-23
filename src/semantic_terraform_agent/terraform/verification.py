"""Isolated application and Terraform verification of candidate patches."""

from __future__ import annotations

import json
import re
import shlex
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from semantic_terraform_agent.collectors.repository import RepositoryLayout
from semantic_terraform_agent.config import DEFAULT_LIMITS
from semantic_terraform_agent.models import (
    PatchFailureCategory,
    PatchFailureReasonCode,
    VerificationAttempt,
    VerificationCommand,
    VerificationCommands,
    VerificationStage,
)
from semantic_terraform_agent.security import redact_secrets
from semantic_terraform_agent.terraform.workspace import (
    create_safe_terraform_copy,
    sanitized_environment,
)


class UnsafePatchError(ValueError):
    """A candidate patch is malformed or exceeds the verifier's path scope."""


@dataclass(frozen=True)
class PatchFailureClassification:
    category: PatchFailureCategory
    reason_code: PatchFailureReasonCode
    description: str


@dataclass(frozen=True)
class PatchInspection:
    affected_files: tuple[str, ...]
    creates_files: bool
    deletes_files: bool
    renames_files: bool


_HUNK_HEADER = re.compile(
    r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?: .*)?$"
)
_CONCATENATED_HEADERS = re.compile(
    r"---\s+(?P<old>[^\s+]+)\+\+\+\s+(?P<new>[^\s@]+)@@"
)


def find_git() -> str | None:
    return shutil.which("git")


def find_terraform() -> str | None:
    return shutil.which("terraform")


def _safe_repository_patch_path(raw: str) -> str | None:
    value = raw.strip()
    if value.startswith('"'):
        try:
            parsed = shlex.split(value)
        except ValueError as exc:
            raise UnsafePatchError(f"invalid quoted patch path: {value}") from exc
        if not parsed:
            raise UnsafePatchError("patch contains an empty file path")
        value = parsed[0]
    else:
        value = value.split("\t", 1)[0]
    if value == "/dev/null":
        return None
    if "\\" in value:
        raise UnsafePatchError(f"patch path uses unsupported backslashes: {value}")
    if value.startswith(("a/", "b/")):
        value = value[2:]
    path = PurePosixPath(value)
    if not value or path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
        raise UnsafePatchError(f"patch path is not a safe repository-relative path: {value}")
    return path.as_posix()


def _normalize_patch_path(raw: str, layout: RepositoryLayout) -> str | None:
    value = _safe_repository_patch_path(raw)
    if value is None:
        return None
    path = PurePosixPath(value)
    if not value.endswith((".tf", ".tf.json")):
        raise UnsafePatchError(f"patch may modify only Terraform source files: {value}")
    terraform_dir = PurePosixPath(layout.terraform_dir)
    if layout.terraform_dir not in ("", "."):
        try:
            path.relative_to(terraform_dir)
        except ValueError as exc:
            scoped_path = terraform_dir / path
            if scoped_path.as_posix() not in layout.terraform_files:
                raise UnsafePatchError(
                    f"patch path is outside the selected Terraform directory: {value}"
                ) from exc
            path = scoped_path
    return path.as_posix()


def inspect_patch(patch: str) -> PatchInspection:
    """Parse unified-diff paths once using the verifier's path-safety rules."""
    encoded_size = len(patch.encode("utf-8"))
    if encoded_size > DEFAULT_LIMITS.max_patch_bytes:
        raise UnsafePatchError(
            f"candidate patch exceeds the {DEFAULT_LIMITS.max_patch_bytes}-byte limit"
        )
    if "\x00" in patch:
        raise UnsafePatchError("candidate patch contains a NUL byte")
    if "\x1b" in patch:
        raise UnsafePatchError("candidate patch contains ANSI escape codes")
    if any(line.strip().startswith("```") for line in patch.splitlines()):
        raise UnsafePatchError("candidate patch contains a Markdown fence")
    if _CONCATENATED_HEADERS.search(patch):
        raise UnsafePatchError("candidate patch contains concatenated diff headers")
    if "GIT binary patch" in patch or "Binary files " in patch:
        raise UnsafePatchError("binary patches are not supported")

    affected: list[str] = []
    creates_files = False
    deletes_files = False
    renames_files = False
    header_pairs = 0
    lines = patch.splitlines()
    for index, line in enumerate(lines):
        if line.startswith("diff --git "):
            try:
                fields = shlex.split(line)
            except ValueError as exc:
                raise UnsafePatchError("invalid diff --git header") from exc
            if len(fields) != 4:
                raise UnsafePatchError("invalid diff --git header")
            old_path = _safe_repository_patch_path(fields[2])
            new_path = _safe_repository_patch_path(fields[3])
            if old_path is None or new_path is None:
                raise UnsafePatchError("diff --git paths cannot be /dev/null")
            affected.extend((old_path, new_path))
            renames_files = renames_files or old_path != new_path
        if line.startswith(("rename from ", "rename to ", "copy from ", "copy to ")):
            renames_files = True
        if not line.startswith("--- ") or index + 1 >= len(lines):
            continue
        plus = lines[index + 1]
        if not plus.startswith("+++ "):
            continue
        header_pairs += 1
        old_path = _safe_repository_patch_path(line[4:])
        new_path = _safe_repository_patch_path(plus[4:])
        creates_files = creates_files or old_path is None
        deletes_files = deletes_files or new_path is None
        if old_path is not None:
            affected.append(old_path)
        if new_path is not None:
            affected.append(new_path)
        renames_files = renames_files or (
            old_path is not None and new_path is not None and old_path != new_path
        )
    if header_pairs == 0:
        raise UnsafePatchError("candidate patch is not a unified diff with ---/+++ headers")
    if not affected:
        raise UnsafePatchError("candidate patch does not identify a repository file")
    _validate_hunk_structure(lines)
    return PatchInspection(
        affected_files=tuple(dict.fromkeys(affected)),
        creates_files=creates_files,
        deletes_files=deletes_files,
        renames_files=renames_files,
    )


def _validate_hunk_structure(lines: list[str]) -> None:
    hunk_count = 0
    index = 0
    while index < len(lines):
        line = lines[index]
        if not line.startswith("@@"):
            index += 1
            continue
        match = _HUNK_HEADER.fullmatch(line)
        if match is None:
            raise UnsafePatchError("candidate patch contains a malformed hunk header")
        hunk_count += 1
        expected_old = int(match.group(2) or 1)
        expected_new = int(match.group(4) or 1)
        actual_old = 0
        actual_new = 0
        index += 1
        while index < len(lines):
            body = lines[index]
            if body.startswith(("@@", "diff --git ")) or (
                body.startswith("--- ")
                and index + 1 < len(lines)
                and lines[index + 1].startswith("+++ ")
            ):
                break
            if body.startswith("\\ No newline at end of file"):
                index += 1
                continue
            if not body or body[0] not in {" ", "+", "-"}:
                raise UnsafePatchError("candidate patch contains invalid unified-diff lines")
            if body[0] in {" ", "-"}:
                actual_old += 1
            if body[0] in {" ", "+"}:
                actual_new += 1
            index += 1
        if actual_old != expected_old or actual_new != expected_new:
            raise UnsafePatchError("candidate patch hunk line counts do not match its body")
    if hunk_count == 0:
        raise UnsafePatchError("candidate patch contains no valid unified-diff hunk")


def _format_patch_path(path: str, prefix: str) -> str:
    if path == "/dev/null":
        return path
    value = f"{prefix}/{path}"
    return (
        json.dumps(value)
        if '"' in value or any(character.isspace() for character in value)
        else value
    )


def _canonicalize_patch_headers(patch: str, layout: RepositoryLayout) -> str:
    """Use standard a/b headers with paths relative to the repository root."""
    lines = patch.splitlines()
    canonical: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.startswith("diff --git "):
            try:
                fields = shlex.split(line)
            except ValueError as exc:
                raise UnsafePatchError("invalid diff --git header") from exc
            if len(fields) != 4:
                raise UnsafePatchError("invalid diff --git header")
            old_path = _normalize_patch_path(fields[2], layout)
            new_path = _normalize_patch_path(fields[3], layout)
            if old_path is None or new_path is None:
                raise UnsafePatchError("diff --git paths cannot be /dev/null")
            canonical.append(
                f"diff --git {_format_patch_path(old_path, 'a')} "
                f"{_format_patch_path(new_path, 'b')}"
            )
            index += 1
            continue
        if (
            line.startswith("--- ")
            and index + 1 < len(lines)
            and lines[index + 1].startswith("+++ ")
        ):
            old_path = _normalize_patch_path(line[4:], layout)
            new_path = _normalize_patch_path(lines[index + 1][4:], layout)
            canonical.append(
                "--- /dev/null"
                if old_path is None
                else f"--- {_format_patch_path(old_path, 'a')}"
            )
            canonical.append(
                "+++ /dev/null"
                if new_path is None
                else f"+++ {_format_patch_path(new_path, 'b')}"
            )
            index += 2
            continue
        canonical.append(line)
        index += 1
    result = "\n".join(canonical)
    return f"{result}\n"


def validate_patch_scope(patch: str, layout: RepositoryLayout) -> list[str]:
    inspection = inspect_patch(patch)
    forbidden_markers = (
        "GIT binary patch",
        "Binary files ",
        "rename from ",
        "rename to ",
        "copy from ",
        "copy to ",
        "new file mode 120000",
        "new file mode 160000",
    )
    if any(marker in patch for marker in forbidden_markers):
        raise UnsafePatchError("binary, symlink, submodule, rename, and copy patches are not supported")

    lines = patch.splitlines()
    changed: list[str] = []
    header_pairs = 0
    for index, line in enumerate(lines):
        if line.startswith("diff --git "):
            try:
                fields = shlex.split(line)
            except ValueError as exc:
                raise UnsafePatchError("invalid diff --git header") from exc
            if len(fields) != 4:
                raise UnsafePatchError("invalid diff --git header")
            for raw in fields[2:]:
                normalized = _normalize_patch_path(raw, layout)
                if normalized:
                    changed.append(normalized)
        if not line.startswith("--- ") or index + 1 >= len(lines):
            continue
        plus = lines[index + 1]
        if not plus.startswith("+++ "):
            continue
        header_pairs += 1
        for raw in (line[4:], plus[4:]):
            normalized = _normalize_patch_path(raw, layout)
            if normalized:
                changed.append(normalized)
    if header_pairs == 0:
        raise UnsafePatchError("candidate patch is not a unified diff with ---/+++ headers")
    if not changed:
        raise UnsafePatchError("candidate patch does not identify a Terraform source file")
    if inspection.renames_files:
        raise UnsafePatchError("rename and copy patches are not supported")
    if inspection.creates_files:
        raise UnsafePatchError("file creation patches are not supported")
    if inspection.deletes_files:
        raise UnsafePatchError("file deletion patches are not supported")
    for relative in dict.fromkeys(changed):
        unresolved = layout.root / relative
        if unresolved.is_symlink():
            raise UnsafePatchError("patch may not modify a symbolic link")
        if relative not in layout.terraform_files:
            raise UnsafePatchError("patch may modify only existing Terraform source files")
    return list(dict.fromkeys(changed))


def classify_patch_failure(
    patch: str,
    layout: RepositoryLayout,
    error: str | Exception | None = None,
) -> PatchFailureClassification:
    """Classify patch representation and scope locally without model inference."""
    message = str(error or "candidate patch failed validation")
    lowered = message.lower()

    if "git binary patch" in patch.lower() or "binary files " in patch.lower():
        return _classification(PatchFailureCategory.UNSAFE, "binary_patch", message)
    if any(marker in patch for marker in ("new file mode 120000", "new file mode 160000")):
        return _classification(PatchFailureCategory.UNSAFE, "symlink_escape", message)
    if any(marker in patch for marker in ("rename from ", "rename to ", "copy from ", "copy to ")):
        return _classification(PatchFailureCategory.UNSAFE, "file_rename", message)
    if "\x00" in patch or "\x1b" in patch or "backslashes" in lowered:
        return _classification(PatchFailureCategory.UNSAFE, "unsafe_path", message)

    raw_paths, creates, deletes, renames, path_error = _candidate_patch_paths(patch)
    if path_error is not None:
        return _classification(PatchFailureCategory.UNSAFE, "unsafe_path", path_error)
    if creates:
        return _classification(PatchFailureCategory.UNSAFE, "file_creation", message)
    if deletes:
        return _classification(PatchFailureCategory.UNSAFE, "file_deletion", message)
    if renames:
        return _classification(PatchFailureCategory.UNSAFE, "file_rename", message)
    for raw in raw_paths:
        try:
            normalized = _normalize_patch_path(raw, layout)
        except UnsafePatchError as exc:
            reason: PatchFailureReasonCode = (
                "non_terraform_path"
                if "only terraform" in str(exc).lower()
                else "unsafe_path"
            )
            return _classification(PatchFailureCategory.UNSAFE, reason, str(exc))
        if normalized is None:
            continue
        unresolved = layout.root / normalized
        if unresolved.is_symlink():
            return _classification(
                PatchFailureCategory.UNSAFE,
                "symlink_escape",
                "candidate patch targets a symbolic link",
            )
        if normalized not in layout.terraform_files:
            return _classification(
                PatchFailureCategory.UNSAFE,
                "file_creation",
                "candidate patch targets a file outside the existing Terraform manifest",
            )

    if "markdown fence" in lowered or any(
        line.strip().startswith("```") for line in patch.splitlines()
    ):
        return _classification(
            PatchFailureCategory.MALFORMED_REPAIRABLE,
            "markdown_fence_leak",
            message,
        )
    if "concatenated" in lowered or _CONCATENATED_HEADERS.search(patch):
        return _classification(
            PatchFailureCategory.MALFORMED_REPAIRABLE,
            "concatenated_diff",
            message,
        )
    if "hunk" in lowered:
        return _classification(
            PatchFailureCategory.MALFORMED_REPAIRABLE,
            "malformed_hunk",
            message,
        )
    if "unified-diff lines" in lowered or "line structure" in lowered:
        return _classification(
            PatchFailureCategory.MALFORMED_REPAIRABLE,
            "invalid_diff_structure",
            message,
        )
    if "not a unified diff" in lowered or "no valid unified-diff hunk" in lowered:
        return _classification(
            PatchFailureCategory.MALFORMED_REPAIRABLE,
            "missing_diff_headers",
            message,
        )
    if not raw_paths and any(marker in patch.lower() for marker in (".env", "readme")):
        return _classification(
            PatchFailureCategory.UNSAFE,
            "non_terraform_path",
            "candidate patch references a non-Terraform path",
        )
    if not raw_paths and any(marker in patch.lower() for marker in ("../", "/dev/null")):
        return _classification(
            PatchFailureCategory.UNSAFE,
            "unsafe_path",
            "candidate patch contains an unsafe path marker",
        )
    if not raw_paths:
        return _classification(
            PatchFailureCategory.MALFORMED_REPAIRABLE,
            "missing_diff_headers",
            message,
        )
    return _classification(
        PatchFailureCategory.UNKNOWN,
        "unknown_patch_failure",
        message,
    )


def _candidate_patch_paths(
    patch: str,
) -> tuple[list[str], bool, bool, bool, str | None]:
    paths: list[str] = []
    creates = False
    deletes = False
    renames = False
    lines = patch.splitlines()
    for index, line in enumerate(lines):
        if line.startswith("diff --git "):
            try:
                fields = shlex.split(line)
            except ValueError:
                return paths, creates, deletes, renames, "invalid quoted patch path header"
            if len(fields) != 4:
                return paths, creates, deletes, renames, "invalid diff --git path header"
            paths.extend(fields[2:])
            renames = renames or fields[2].removeprefix("a/") != fields[3].removeprefix("b/")
        if not line.startswith("--- ") or index + 1 >= len(lines):
            continue
        plus = lines[index + 1]
        if not plus.startswith("+++ "):
            continue
        old_path = line[4:].strip().split("\t", 1)[0]
        new_path = plus[4:].strip().split("\t", 1)[0]
        creates = creates or old_path == "/dev/null"
        deletes = deletes or new_path == "/dev/null"
        if old_path != "/dev/null":
            paths.append(old_path)
        if new_path != "/dev/null":
            paths.append(new_path)
        renames = renames or (
            old_path != "/dev/null"
            and new_path != "/dev/null"
            and old_path.removeprefix("a/") != new_path.removeprefix("b/")
        )
    concatenated = _CONCATENATED_HEADERS.search(patch)
    if concatenated:
        paths.extend((concatenated.group("old"), concatenated.group("new")))
        renames = renames or (
            concatenated.group("old").removeprefix("a/")
            != concatenated.group("new").removeprefix("b/")
        )
    return list(dict.fromkeys(paths)), creates, deletes, renames, None


def _classification(
    category: PatchFailureCategory,
    reason_code: PatchFailureReasonCode,
    description: str,
) -> PatchFailureClassification:
    return PatchFailureClassification(
        category=category,
        reason_code=reason_code,
        description=description[:500],
    )


def _bounded_output(
    value: str | bytes | None, *, sensitive_values: tuple[str, ...] = ()
) -> str:
    if value is None:
        return ""
    text = value.decode(errors="replace") if isinstance(value, bytes) else str(value)
    text = redact_secrets(text)
    for secret in sensitive_values:
        if len(secret) >= 4:
            text = text.replace(secret, "[REDACTED]")
    limit = DEFAULT_LIMITS.max_verification_output_chars
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n...[verification output truncated]"


def _run_command(
    actual_command: list[str],
    recorded_command: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
) -> VerificationCommand:
    started = time.perf_counter()
    sensitive_values = tuple(
        value
        for name, value in env.items()
        if name.startswith("TF_VAR_")
        or name
        in {
            item.strip()
            for item in env.get(
                "SEMANTIC_TERRAFORM_AGENT_PASSTHROUGH_ENV", ""
            ).split(",")
            if item.strip()
        }
    )
    try:
        completed = subprocess.run(
            actual_command,
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=DEFAULT_LIMITS.command_timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return VerificationCommand(
            command=recorded_command,
            status="error",
            stdout=_bounded_output(exc.stdout, sensitive_values=sensitive_values),
            stderr=_bounded_output(exc.stderr, sensitive_values=sensitive_values)
            or "Command timed out.",
            duration_seconds=round(time.perf_counter() - started, 6),
        )
    except OSError as exc:
        return VerificationCommand(
            command=recorded_command,
            status="error",
            stderr=str(exc),
            duration_seconds=round(time.perf_counter() - started, 6),
        )
    return VerificationCommand(
        command=recorded_command,
        status="passed" if completed.returncode == 0 else "failed",
        exit_code=completed.returncode,
        stdout=_bounded_output(completed.stdout, sensitive_values=sensitive_values),
        stderr=_bounded_output(completed.stderr, sensitive_values=sensitive_values),
        duration_seconds=round(time.perf_counter() - started, 6),
    )


def _skipped(command: list[str], reason: str) -> VerificationCommand:
    return VerificationCommand(command=command, status="skipped", stderr=reason)


def _skip_after(
    commands: VerificationCommands, stage: VerificationStage, reason: str
) -> None:
    displays = {
        "patch_apply": ["git", "apply", "candidate.patch"],
        "fmt": ["terraform", "fmt", "-check"],
        "init": [
            "terraform",
            "init",
            "-backend=false",
            "-input=false",
            "-no-color",
        ],
        "validate": ["terraform", "validate", "-no-color"],
        "plan": [
            "terraform",
            "plan",
            "-input=false",
            "-lock=false",
            "-refresh=false",
            "-no-color",
        ],
    }
    order = ("patch_apply", "fmt", "init", "validate", "plan")
    start = order.index(stage) if stage in order else 0
    for name in order[start:]:
        attribute = "terraform_validate" if name == "validate" else name
        if getattr(commands, attribute) is None:
            setattr(commands, attribute, _skipped(displays[name], reason))


def _attempt_result(
    *,
    attempt: int,
    patch: str,
    status: str,
    failed_stage: VerificationStage | None,
    changed_files: list[str],
    commands: VerificationCommands,
    warnings: list[str],
    cleaned: bool = True,
    failure: PatchFailureClassification | None = None,
) -> VerificationAttempt:
    return VerificationAttempt(
        attempt=attempt,
        patch=patch,
        status=status,
        failed_stage=failed_stage,
        changed_files=changed_files,
        commands=commands,
        temporary_copy_cleaned=cleaned,
        warnings=warnings,
        failure_category=failure.category if failure else None,
        failure_reason_code=failure.reason_code if failure else None,
        failure_description=failure.description if failure else None,
    )


def skipped_verification(
    reason: str, patch: str, *, attempt: int = 1
) -> VerificationAttempt:
    return _attempt_result(
        attempt=attempt,
        patch=patch,
        status="skipped",
        failed_stage=None,
        changed_files=[],
        commands=VerificationCommands(),
        warnings=[reason],
    )


_UNAVAILABLE_MARKERS = (
    "no valid credential",
    "could not find credential",
    "failed to retrieve credential",
    "authentication required",
    "failed to query available provider packages",
    "no such host",
    "network is unreachable",
    "connection refused",
    "context deadline exceeded",
)


def _environment_unavailable(command: VerificationCommand) -> bool:
    if command.status == "error":
        return True
    output = f"{command.stdout}\n{command.stderr}".lower()
    return any(marker in output for marker in _UNAVAILABLE_MARKERS)


def verify_candidate_patch(
    patch: str, layout: RepositoryLayout, *, attempt: int = 1
) -> VerificationAttempt:
    """Verify one candidate in a fresh filtered temporary repository copy."""
    commands = VerificationCommands()
    try:
        changed_files = validate_patch_scope(patch, layout)
        patch = _canonicalize_patch_headers(patch, layout)
        changed_files = validate_patch_scope(patch, layout)
    except UnsafePatchError as exc:
        failure = classify_patch_failure(patch, layout, exc)
        return _attempt_result(
            attempt=attempt,
            patch=patch,
            status="rejected",
            failed_stage="patch_check",
            changed_files=[],
            commands=commands,
            warnings=[str(exc)],
            failure=failure,
        )

    git = find_git()
    if git is None:
        reason = "Git CLI was not found; the candidate patch could not be applied safely."
        commands.patch_check = _skipped(["git", "apply", "--check", "candidate.patch"], reason)
        _skip_after(commands, "patch_apply", reason)
        return _attempt_result(
            attempt=attempt,
            patch=patch,
            status="unavailable",
            failed_stage="patch_check",
            changed_files=changed_files,
            commands=commands,
            warnings=[reason],
            failure=_classification(
                PatchFailureCategory.ENVIRONMENT_FAILURE,
                "environment_failure",
                reason,
            ),
        )

    warnings: list[str] = []
    with tempfile.TemporaryDirectory(prefix="semantic-terraform-verifier-") as temporary:
        temp_root = Path(temporary)
        copied_root = temp_root / "repository"
        try:
            workdir = create_safe_terraform_copy(layout, copied_root)
            home = temp_root / "home"
            home.mkdir()
            patch_file = temp_root / "candidate.patch"
            patch_file.write_bytes(patch.encode("utf-8"))
        except OSError as exc:
            reason = f"Could not create the isolated verification copy: {exc}"
            return _attempt_result(
                attempt=attempt,
                patch=patch,
                status="unavailable",
                failed_stage="patch_check",
                changed_files=changed_files,
                commands=commands,
                warnings=[reason],
                failure=_classification(
                    PatchFailureCategory.ENVIRONMENT_FAILURE,
                    "environment_failure",
                    reason,
                ),
            )
        env = sanitized_environment(home)

        commands.patch_check = _run_command(
            [git, "apply", "--check", "--whitespace=nowarn", str(patch_file)],
            ["git", "apply", "--check", "candidate.patch"],
            cwd=copied_root,
            env=env,
        )
        if commands.patch_check.status != "passed":
            unavailable = _environment_unavailable(commands.patch_check)
            reason = (
                "Patch check could not run in the current environment."
                if unavailable
                else "Patch check did not pass; later verification commands were not run."
            )
            failure = _classification(
                (
                    PatchFailureCategory.ENVIRONMENT_FAILURE
                    if unavailable
                    else PatchFailureCategory.SEMANTIC_VERIFICATION_FAILURE
                ),
                "environment_failure" if unavailable else "patch_does_not_apply",
                _bounded_output(
                    f"{commands.patch_check.stderr}\n{commands.patch_check.stdout}"
                )
                or reason,
            )
            _skip_after(commands, "patch_apply", reason)
            return _attempt_result(
                attempt=attempt,
                patch=patch,
                status="unavailable" if unavailable else "failed",
                failed_stage="patch_check",
                changed_files=changed_files,
                commands=commands,
                warnings=[reason],
                failure=failure,
            )

        commands.patch_apply = _run_command(
            [git, "apply", "--whitespace=nowarn", str(patch_file)],
            ["git", "apply", "candidate.patch"],
            cwd=copied_root,
            env=env,
        )
        if commands.patch_apply.status != "passed":
            unavailable = _environment_unavailable(commands.patch_apply)
            reason = (
                "Patch application could not run in the current environment."
                if unavailable
                else "Patch application did not pass; Terraform verification was not run."
            )
            _skip_after(commands, "fmt", reason)
            return _attempt_result(
                attempt=attempt,
                patch=patch,
                status="unavailable" if unavailable else "failed",
                failed_stage="patch_apply",
                changed_files=changed_files,
                commands=commands,
                warnings=[reason],
                failure=_classification(
                    (
                        PatchFailureCategory.ENVIRONMENT_FAILURE
                        if unavailable
                        else PatchFailureCategory.SEMANTIC_VERIFICATION_FAILURE
                    ),
                    "environment_failure" if unavailable else "patch_does_not_apply",
                    reason,
                ),
            )

        terraform = find_terraform()
        if terraform is None:
            reason = "Terraform executable was not found."
            _skip_after(commands, "fmt", reason)
            return _attempt_result(
                attempt=attempt,
                patch=patch,
                status="unavailable",
                failed_stage="fmt",
                changed_files=changed_files,
                commands=commands,
                warnings=[reason],
                failure=_classification(
                    PatchFailureCategory.ENVIRONMENT_FAILURE,
                    "environment_failure",
                    reason,
                ),
            )

        commands.fmt = _run_command(
            [terraform, "fmt", "-check"],
            ["terraform", "fmt", "-check"],
            cwd=workdir,
            env=env,
        )
        if commands.fmt.status != "passed":
            unavailable = _environment_unavailable(commands.fmt)
            reason = (
                "terraform fmt -check could not run in the current environment."
                if unavailable
                else "terraform fmt -check did not pass."
            )
            _skip_after(commands, "init", reason)
            return _attempt_result(
                attempt=attempt,
                patch=patch,
                status="unavailable" if unavailable else "failed",
                failed_stage="fmt",
                changed_files=changed_files,
                commands=commands,
                warnings=[reason],
                failure=_classification(
                    (
                        PatchFailureCategory.ENVIRONMENT_FAILURE
                        if unavailable
                        else PatchFailureCategory.SEMANTIC_VERIFICATION_FAILURE
                    ),
                    (
                        "environment_failure"
                        if unavailable
                        else "terraform_verification_failure"
                    ),
                    reason,
                ),
            )

        commands.init = _run_command(
            [
                terraform,
                "init",
                "-backend=false",
                "-input=false",
                "-no-color",
            ],
            [
                "terraform",
                "init",
                "-backend=false",
                "-input=false",
                "-no-color",
            ],
            cwd=workdir,
            env=env,
        )
        if commands.init.status != "passed":
            reason = "terraform init did not pass; validation and plan were not run."
            _skip_after(commands, "validate", reason)
            unavailable = _environment_unavailable(commands.init)
            return _attempt_result(
                attempt=attempt,
                patch=patch,
                status="unavailable" if unavailable else "failed",
                failed_stage="init",
                changed_files=changed_files,
                commands=commands,
                warnings=[reason],
                failure=_classification(
                    (
                        PatchFailureCategory.ENVIRONMENT_FAILURE
                        if unavailable
                        else PatchFailureCategory.UNKNOWN
                    ),
                    (
                        "environment_failure"
                        if unavailable
                        else "unknown_patch_failure"
                    ),
                    reason,
                ),
            )

        commands.terraform_validate = _run_command(
            [terraform, "validate", "-no-color"],
            ["terraform", "validate", "-no-color"],
            cwd=workdir,
            env=env,
        )
        if commands.terraform_validate.status != "passed":
            unavailable = _environment_unavailable(commands.terraform_validate)
            reason = (
                "terraform validate could not run in the current environment."
                if unavailable
                else "terraform validate did not pass; plan was not run."
            )
            _skip_after(commands, "plan", reason)
            return _attempt_result(
                attempt=attempt,
                patch=patch,
                status="unavailable" if unavailable else "failed",
                failed_stage="validate",
                changed_files=changed_files,
                commands=commands,
                warnings=[reason],
                failure=_classification(
                    (
                        PatchFailureCategory.ENVIRONMENT_FAILURE
                        if unavailable
                        else PatchFailureCategory.SEMANTIC_VERIFICATION_FAILURE
                    ),
                    (
                        "environment_failure"
                        if unavailable
                        else "terraform_verification_failure"
                    ),
                    reason,
                ),
            )

        commands.plan = _run_command(
            [
                terraform,
                "plan",
                "-input=false",
                "-lock=false",
                "-refresh=false",
                "-no-color",
            ],
            [
                "terraform",
                "plan",
                "-input=false",
                "-lock=false",
                "-refresh=false",
                "-no-color",
            ],
            cwd=workdir,
            env=env,
        )
        if commands.plan.status != "passed":
            unavailable = _environment_unavailable(commands.plan)
            reason = (
                "terraform plan could not run because required environment access was unavailable."
                if unavailable
                else "terraform plan did not pass."
            )
            return _attempt_result(
                attempt=attempt,
                patch=patch,
                status="unavailable" if unavailable else "failed",
                failed_stage="plan",
                changed_files=changed_files,
                commands=commands,
                warnings=[reason],
                failure=_classification(
                    (
                        PatchFailureCategory.ENVIRONMENT_FAILURE
                        if unavailable
                        else PatchFailureCategory.SEMANTIC_VERIFICATION_FAILURE
                    ),
                    (
                        "environment_failure"
                        if unavailable
                        else "terraform_verification_failure"
                    ),
                    reason,
                ),
            )

        return _attempt_result(
            attempt=attempt,
            patch=patch,
            status="verified",
            failed_stage=None,
            changed_files=changed_files,
            commands=commands,
            warnings=warnings,
        )
