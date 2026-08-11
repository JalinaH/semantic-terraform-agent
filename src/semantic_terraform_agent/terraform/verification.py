"""Isolated application and Terraform verification of candidate patches."""

from __future__ import annotations

import shlex
import shutil
import subprocess
import tempfile
import time
from pathlib import Path, PurePosixPath

from semantic_terraform_agent.collectors.repository import RepositoryLayout
from semantic_terraform_agent.config import DEFAULT_LIMITS
from semantic_terraform_agent.models import (
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


def find_git() -> str | None:
    return shutil.which("git")


def find_terraform() -> str | None:
    return shutil.which("terraform")


def _normalize_patch_path(raw: str, layout: RepositoryLayout) -> str | None:
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
    if not value.endswith((".tf", ".tf.json")):
        raise UnsafePatchError(f"patch may modify only Terraform source files: {value}")
    terraform_dir = PurePosixPath(layout.terraform_dir)
    if layout.terraform_dir not in ("", "."):
        try:
            path.relative_to(terraform_dir)
        except ValueError as exc:
            raise UnsafePatchError(
                f"patch path is outside the selected Terraform directory: {value}"
            ) from exc
    return path.as_posix()


def validate_patch_scope(patch: str, layout: RepositoryLayout) -> list[str]:
    encoded_size = len(patch.encode("utf-8"))
    if encoded_size > DEFAULT_LIMITS.max_patch_bytes:
        raise UnsafePatchError(
            f"candidate patch exceeds the {DEFAULT_LIMITS.max_patch_bytes}-byte limit"
        )
    if "\x00" in patch:
        raise UnsafePatchError("candidate patch contains a NUL byte")
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
    return list(dict.fromkeys(changed))


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
    output = f"{command.stdout}\n{command.stderr}".lower()
    return any(marker in output for marker in _UNAVAILABLE_MARKERS)


def verify_candidate_patch(
    patch: str, layout: RepositoryLayout, *, attempt: int = 1
) -> VerificationAttempt:
    """Verify one candidate in a fresh filtered temporary repository copy."""
    commands = VerificationCommands()
    try:
        changed_files = validate_patch_scope(patch, layout)
    except UnsafePatchError as exc:
        return _attempt_result(
            attempt=attempt,
            patch=patch,
            status="rejected",
            failed_stage="patch_check",
            changed_files=[],
            commands=commands,
            warnings=[str(exc)],
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
            patch_file.write_text(patch, encoding="utf-8")
        except OSError as exc:
            return _attempt_result(
                attempt=attempt,
                patch=patch,
                status="unavailable",
                failed_stage="patch_check",
                changed_files=changed_files,
                commands=commands,
                warnings=[f"Could not create the isolated verification copy: {exc}"],
            )
        env = sanitized_environment(home)

        commands.patch_check = _run_command(
            [git, "apply", "--check", "--whitespace=nowarn", str(patch_file)],
            ["git", "apply", "--check", "candidate.patch"],
            cwd=copied_root,
            env=env,
        )
        if commands.patch_check.status != "passed":
            reason = "Patch check did not pass; later verification commands were not run."
            _skip_after(commands, "patch_apply", reason)
            return _attempt_result(
                attempt=attempt,
                patch=patch,
                status="failed",
                failed_stage="patch_check",
                changed_files=changed_files,
                commands=commands,
                warnings=[reason],
            )

        commands.patch_apply = _run_command(
            [git, "apply", "--whitespace=nowarn", str(patch_file)],
            ["git", "apply", "candidate.patch"],
            cwd=copied_root,
            env=env,
        )
        if commands.patch_apply.status != "passed":
            reason = "Patch application did not pass; Terraform verification was not run."
            _skip_after(commands, "fmt", reason)
            return _attempt_result(
                attempt=attempt,
                patch=patch,
                status="failed",
                failed_stage="patch_apply",
                changed_files=changed_files,
                commands=commands,
                warnings=[reason],
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
            )

        commands.fmt = _run_command(
            [terraform, "fmt", "-check"],
            ["terraform", "fmt", "-check"],
            cwd=workdir,
            env=env,
        )
        if commands.fmt.status != "passed":
            reason = "terraform fmt -check did not pass."
            _skip_after(commands, "init", reason)
            return _attempt_result(
                attempt=attempt,
                patch=patch,
                status="failed",
                failed_stage="fmt",
                changed_files=changed_files,
                commands=commands,
                warnings=[reason],
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
            )

        commands.terraform_validate = _run_command(
            [terraform, "validate", "-no-color"],
            ["terraform", "validate", "-no-color"],
            cwd=workdir,
            env=env,
        )
        if commands.terraform_validate.status != "passed":
            reason = "terraform validate did not pass; plan was not run."
            _skip_after(commands, "plan", reason)
            return _attempt_result(
                attempt=attempt,
                patch=patch,
                status="failed",
                failed_stage="validate",
                changed_files=changed_files,
                commands=commands,
                warnings=[reason],
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
