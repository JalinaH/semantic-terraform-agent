from __future__ import annotations

import re
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from semantic_terraform_agent.collectors.repository import discover_repository
from semantic_terraform_agent.models import PatchFailureCategory, VerificationCommand
from semantic_terraform_agent.terraform import verification as verification_module
from semantic_terraform_agent.terraform.verification import (
    UnsafePatchError,
    validate_patch_scope,
    verify_candidate_patch,
)


def candidate_patch(value: str = "safe") -> str:
    return f'''diff --git a/infrastructure/main.tf b/infrastructure/main.tf
--- a/infrastructure/main.tf
+++ b/infrastructure/main.tf
@@ -1,3 +1,3 @@
 resource "example_widget" "primary" {{
-  mode = "fast"
+  mode = "{value}"
 }}
'''


def test_production_code_contains_no_repository_or_terraform_mutation_commands() -> None:
    source_root = Path(__file__).parents[1] / "src" / "semantic_terraform_agent"
    production = "\n".join(
        path.read_text(encoding="utf-8") for path in source_root.rglob("*.py")
    )
    for executable, command in (
        ("git", "commit"),
        ("git", "push"),
        ("git", "merge"),
        ("terraform", "apply"),
        ("terraform", "destroy"),
    ):
        pattern = rf"[\"']{executable}[\"']\s*,\s*[\"']{command}[\"']"
        assert re.search(pattern, production) is None


def passed(recorded: list[str]) -> VerificationCommand:
    return VerificationCommand(command=recorded, status="passed", exit_code=0)


def test_patch_scope_accepts_only_selected_terraform_directory(
    terraform_repo: Path,
) -> None:
    layout = discover_repository(terraform_repo, Path("infrastructure"))
    assert validate_patch_scope(candidate_patch(), layout) == ["infrastructure/main.tf"]


def test_patch_scope_resolves_exact_terraform_directory_relative_file(
    terraform_repo: Path,
) -> None:
    layout = discover_repository(terraform_repo, Path("infrastructure"))
    relative_patch = candidate_patch().replace("infrastructure/main.tf", "main.tf")
    assert validate_patch_scope(relative_patch, layout) == ["infrastructure/main.tf"]


@pytest.mark.parametrize(
    "patch, message",
    [
        (
            "--- a/../../secret.tf\n+++ b/../../secret.tf\n@@ -1 +1 @@\n-a\n+b\n",
            "safe repository-relative",
        ),
        (
            "--- a/README.md\n+++ b/README.md\n@@ -1 +1 @@\n-a\n+b\n",
            "only Terraform",
        ),
        (
            "--- a/other/main.tf\n+++ b/other/main.tf\n@@ -1 +1 @@\n-a\n+b\n",
            "outside the selected Terraform directory",
        ),
        ("not a patch", "not a unified diff"),
    ],
)
def test_patch_scope_rejects_unsafe_or_malformed_changes(
    terraform_repo: Path, patch: str, message: str
) -> None:
    layout = discover_repository(terraform_repo, Path("infrastructure"))
    with pytest.raises(UnsafePatchError, match=message):
        validate_patch_scope(patch, layout)


def test_rejected_patch_never_runs_commands(
    monkeypatch, terraform_repo: Path
) -> None:
    monkeypatch.setattr(
        verification_module,
        "find_git",
        lambda: pytest.fail("Git lookup must not run for a rejected patch"),
    )
    layout = discover_repository(terraform_repo, Path("infrastructure"))
    result = verify_candidate_patch(
        "--- a/.env\n+++ b/.env\n@@ -1 +1 @@\n-secret=a\n+secret=b\n", layout
    )
    assert result.status == "rejected"
    assert result.failed_stage == "patch_check"
    assert result.commands.patch_apply is None


def test_successful_plan_verification_uses_exact_safe_flags(
    monkeypatch, terraform_repo: Path
) -> None:
    layout = discover_repository(terraform_repo, Path("infrastructure"))
    calls: list[tuple[list[str], Path]] = []

    def fake_run(actual, recorded, *, cwd, env):
        calls.append((recorded, cwd))
        assert cwd != layout.root
        assert layout.root not in cwd.parents
        assert env["TF_IN_AUTOMATION"] == "1"
        return passed(recorded)

    monkeypatch.setattr(verification_module, "find_git", lambda: "/usr/bin/git")
    monkeypatch.setattr(verification_module, "find_terraform", lambda: "/usr/bin/terraform")
    monkeypatch.setattr(verification_module, "_run_command", fake_run)

    result = verify_candidate_patch(candidate_patch(), layout)
    assert result.status == "verified"
    assert result.failed_stage is None
    assert result.commands.plan.status == "passed"
    assert result.commands.plan.command == [
        "terraform",
        "plan",
        "-input=false",
        "-lock=false",
        "-refresh=false",
        "-no-color",
    ]
    assert [command[0] for command, _ in calls] == [
        "git",
        "git",
        "terraform",
        "terraform",
        "terraform",
        "terraform",
    ]
    assert all(cwd != layout.terraform_root for _, cwd in calls)


def test_outer_markdown_fence_is_classified_as_repairable(
    monkeypatch, terraform_repo: Path
) -> None:
    layout = discover_repository(terraform_repo, Path("infrastructure"))
    monkeypatch.setattr(verification_module, "find_git", lambda: "/usr/bin/git")
    monkeypatch.setattr(verification_module, "find_terraform", lambda: "/usr/bin/terraform")
    monkeypatch.setattr(
        verification_module,
        "_run_command",
        lambda actual, recorded, **kwargs: passed(recorded),
    )
    result = verify_candidate_patch(f"```diff\n{candidate_patch()}```\n", layout)
    assert result.status == "rejected"
    assert result.failure_category is PatchFailureCategory.MALFORMED_REPAIRABLE
    assert result.failure_reason_code == "markdown_fence_leak"


def test_ansi_patch_is_rejected_before_commands(
    monkeypatch, terraform_repo: Path
) -> None:
    layout = discover_repository(terraform_repo, Path("infrastructure"))
    monkeypatch.setattr(
        verification_module,
        "find_git",
        lambda: pytest.fail("Git lookup must not run for an ANSI patch"),
    )
    result = verify_candidate_patch(f"\x1b[31m{candidate_patch()}", layout)
    assert result.status == "rejected"
    assert "ANSI" in result.warnings[0]
    assert result.failure_category is PatchFailureCategory.UNSAFE


@pytest.mark.parametrize(
    ("patch", "reason"),
    [
        ('resource "example_widget" "primary" { mode = "safe" }', "missing_diff_headers"),
        (
            "--- a/infrastructure/main.tf+++ b/infrastructure/main.tf@@ -1 +1 @@-a+b",
            "concatenated_diff",
        ),
        (
            "--- a/infrastructure/main.tf\n+++ b/infrastructure/main.tf\n@@ broken @@\n-a\n+b\n",
            "malformed_hunk",
        ),
        (
            "--- a/infrastructure/main.tf\n+++ b/infrastructure/main.tf\n@@ -1 +1 @@\ninvalid\n",
            "invalid_diff_structure",
        ),
    ],
)
def test_malformed_patch_shapes_are_repairable(
    terraform_repo: Path, patch: str, reason: str
) -> None:
    layout = discover_repository(terraform_repo, Path("infrastructure"))
    result = verify_candidate_patch(patch, layout)
    assert result.status == "rejected"
    assert result.failure_category is PatchFailureCategory.MALFORMED_REPAIRABLE
    assert result.failure_reason_code == reason


@pytest.mark.parametrize(
    ("patch", "reason"),
    [
        ("--- a/../../secret.tf\n+++ b/../../secret.tf\n@@ -1 +1 @@\n-a\n+b\n", "unsafe_path"),
        ("--- /absolute/main.tf\n+++ /absolute/main.tf\n@@ -1 +1 @@\n-a\n+b\n", "unsafe_path"),
        ("--- a/README.md\n+++ b/README.md\n@@ -1 +1 @@\n-a\n+b\n", "non_terraform_path"),
        ("GIT binary patch\nliteral 1\nA", "binary_patch"),
        ("--- /dev/null\n+++ b/infrastructure/new.tf\n@@ -0,0 +1 @@\n+x\n", "file_creation"),
        ("--- a/infrastructure/main.tf\n+++ /dev/null\n@@ -1 +0,0 @@\n-x\n", "file_deletion"),
        ("--- a/infrastructure/main.tf\n+++ b/infrastructure/other.tf\n@@ -1 +1 @@\n-a\n+b\n", "file_rename"),
    ],
)
def test_unsafe_patch_shapes_never_become_repairable(
    terraform_repo: Path, patch: str, reason: str
) -> None:
    layout = discover_repository(terraform_repo, Path("infrastructure"))
    result = verify_candidate_patch(patch, layout)
    assert result.status == "rejected"
    assert result.failure_category is PatchFailureCategory.UNSAFE
    assert result.failure_reason_code == reason


def test_symlink_target_is_unsafe(terraform_repo: Path) -> None:
    linked = terraform_repo / "infrastructure/linked.tf"
    linked.symlink_to(terraform_repo / "infrastructure/main.tf")
    layout = discover_repository(terraform_repo, Path("infrastructure"))
    patch = "--- a/infrastructure/linked.tf\n+++ b/infrastructure/linked.tf\n@@ -1 +1 @@\n-a\n+b\n"
    result = verify_candidate_patch(patch, layout)
    assert result.failure_category is PatchFailureCategory.UNSAFE
    assert result.failure_reason_code == "symlink_escape"


def test_plan_failure_records_first_failing_stage(
    monkeypatch, terraform_repo: Path
) -> None:
    layout = discover_repository(terraform_repo, Path("infrastructure"))

    def fake_run(actual, recorded, **kwargs):
        if recorded[:2] == ["terraform", "plan"]:
            return VerificationCommand(
                command=recorded, status="failed", exit_code=1, stderr="invalid value"
            )
        return passed(recorded)

    monkeypatch.setattr(verification_module, "find_git", lambda: "/usr/bin/git")
    monkeypatch.setattr(verification_module, "find_terraform", lambda: "/usr/bin/terraform")
    monkeypatch.setattr(verification_module, "_run_command", fake_run)
    result = verify_candidate_patch(candidate_patch(), layout)
    assert result.status == "failed"
    assert result.failed_stage == "plan"
    assert result.commands.plan.status == "failed"
    assert result.failure_category is PatchFailureCategory.SEMANTIC_VERIFICATION_FAILURE


def test_plan_not_executed_when_validate_fails(
    monkeypatch, terraform_repo: Path
) -> None:
    layout = discover_repository(terraform_repo, Path("infrastructure"))
    calls: list[list[str]] = []

    def fake_run(actual, recorded, **kwargs):
        calls.append(recorded)
        if recorded[:2] == ["terraform", "validate"]:
            return VerificationCommand(command=recorded, status="failed", exit_code=1)
        return passed(recorded)

    monkeypatch.setattr(verification_module, "find_git", lambda: "/usr/bin/git")
    monkeypatch.setattr(verification_module, "find_terraform", lambda: "/usr/bin/terraform")
    monkeypatch.setattr(verification_module, "_run_command", fake_run)
    result = verify_candidate_patch(candidate_patch(), layout)
    assert result.failed_stage == "validate"
    assert result.commands.plan.status == "skipped"
    assert not any(command[:2] == ["terraform", "plan"] for command in calls)


def test_missing_terraform_is_unavailable_and_does_not_plan(
    monkeypatch, terraform_repo: Path
) -> None:
    layout = discover_repository(terraform_repo, Path("infrastructure"))
    monkeypatch.setattr(verification_module, "find_git", lambda: "/usr/bin/git")
    monkeypatch.setattr(verification_module, "find_terraform", lambda: None)
    monkeypatch.setattr(
        verification_module,
        "_run_command",
        lambda actual, recorded, **kwargs: passed(recorded),
    )
    result = verify_candidate_patch(candidate_patch(), layout)
    assert result.status == "unavailable"
    assert result.failed_stage == "fmt"
    assert result.commands.patch_apply.status == "passed"
    assert result.commands.plan.status == "skipped"
    assert result.warnings == ["Terraform executable was not found."]
    assert result.failure_category is PatchFailureCategory.ENVIRONMENT_FAILURE


def test_command_execution_error_is_classified_as_environment_failure(
    monkeypatch, terraform_repo: Path
) -> None:
    layout = discover_repository(terraform_repo, Path("infrastructure"))
    monkeypatch.setattr(verification_module, "find_git", lambda: "/usr/bin/git")
    monkeypatch.setattr(
        verification_module,
        "_run_command",
        lambda actual, recorded, **kwargs: VerificationCommand(
            command=recorded,
            status="error",
            stderr="Command timed out.",
        ),
    )

    result = verify_candidate_patch(candidate_patch(), layout)

    assert result.status == "unavailable"
    assert result.failed_stage == "patch_check"
    assert result.failure_category is PatchFailureCategory.ENVIRONMENT_FAILURE
    assert result.failure_reason_code == "environment_failure"


def test_state_terraform_directory_and_plan_files_are_excluded(
    monkeypatch, terraform_repo: Path
) -> None:
    infra = terraform_repo / "infrastructure"
    (infra / "terraform.tfstate").write_text("secret state", encoding="utf-8")
    (infra / "terraform.tfstate.backup").write_text("secret state", encoding="utf-8")
    (infra / "saved.tfplan").write_text("plan", encoding="utf-8")
    (infra / ".terraform").mkdir()
    (infra / ".terraform/provider-secret").write_text("secret", encoding="utf-8")
    layout = discover_repository(terraform_repo, Path("infrastructure"))

    def fake_run(actual, recorded, *, cwd, env):
        copied_infra = cwd / "infrastructure" if (cwd / "infrastructure").is_dir() else cwd
        assert not (copied_infra / "terraform.tfstate").exists()
        assert not (copied_infra / "terraform.tfstate.backup").exists()
        assert not (copied_infra / "saved.tfplan").exists()
        assert not (copied_infra / ".terraform").exists()
        return passed(recorded)

    monkeypatch.setattr(verification_module, "find_git", lambda: "/usr/bin/git")
    monkeypatch.setattr(verification_module, "find_terraform", lambda: "/usr/bin/terraform")
    monkeypatch.setattr(verification_module, "_run_command", fake_run)
    assert verify_candidate_patch(candidate_patch(), layout).status == "verified"


def test_command_output_is_bounded_and_redacted(monkeypatch, tmp_path: Path) -> None:
    secret = "token=super-secret-value"
    monkeypatch.setattr(
        verification_module.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=1,
            stdout=secret + "\n" + "x" * 20_000,
            stderr="api_key=another-secret",
        ),
    )
    command = verification_module._run_command(
        ["terraform", "plan"],
        ["terraform", "plan"],
        cwd=tmp_path,
        env={},
    )
    assert "super-secret-value" not in command.stdout
    assert "another-secret" not in command.stderr
    assert "[REDACTED]" in command.stdout
    assert "truncated" in command.stdout


def test_explicit_oidc_environment_values_are_redacted_from_output(
    monkeypatch, tmp_path: Path
) -> None:
    temporary_credential = "temporary-oidc-secret-value"
    monkeypatch.setattr(
        verification_module.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=1,
            stdout=f"provider repeated {temporary_credential}",
            stderr="",
        ),
    )
    command = verification_module._run_command(
        ["terraform", "plan"],
        ["terraform", "plan"],
        cwd=tmp_path,
        env={
            "AWS_SESSION_TOKEN": temporary_credential,
            "SEMANTIC_TERRAFORM_AGENT_PASSTHROUGH_ENV": "AWS_SESSION_TOKEN",
        },
    )
    assert temporary_credential not in command.stdout
    assert "[REDACTED]" in command.stdout


def test_real_git_application_does_not_modify_original_repository(
    monkeypatch, terraform_repo: Path
) -> None:
    git = shutil.which("git")
    if git is None:
        pytest.skip("Git is not installed")
    original = (terraform_repo / "infrastructure/main.tf").read_text(encoding="utf-8")
    layout = discover_repository(terraform_repo, Path("infrastructure"))
    monkeypatch.setattr(verification_module, "find_git", lambda: git)
    monkeypatch.setattr(verification_module, "find_terraform", lambda: None)

    result = verify_candidate_patch(candidate_patch(), layout)
    assert result.commands.patch_check.status == "passed"
    assert result.commands.patch_apply.status == "passed"
    assert result.status == "unavailable"
    assert (terraform_repo / "infrastructure/main.tf").read_text(encoding="utf-8") == original


@pytest.mark.parametrize("path_style", ["repo-relative-without-prefix", "terraform-relative"])
def test_git_application_canonicalizes_safe_model_path_styles(
    monkeypatch, terraform_repo: Path, path_style: str
) -> None:
    git = shutil.which("git")
    if git is None:
        pytest.skip("Git is not installed")
    layout = discover_repository(terraform_repo, Path("infrastructure"))
    patch = candidate_patch()
    if path_style == "repo-relative-without-prefix":
        patch = patch.replace("a/infrastructure/main.tf", "infrastructure/main.tf")
        patch = patch.replace("b/infrastructure/main.tf", "infrastructure/main.tf")
    else:
        patch = patch.replace("a/infrastructure/main.tf", "a/main.tf")
        patch = patch.replace("b/infrastructure/main.tf", "b/main.tf")

    monkeypatch.setattr(verification_module, "find_git", lambda: git)
    monkeypatch.setattr(verification_module, "find_terraform", lambda: None)
    result = verify_candidate_patch(patch, layout)

    assert result.commands.patch_check.status == "passed"
    assert result.commands.patch_apply.status == "passed"
    assert result.patch == candidate_patch()


def test_git_application_adds_missing_patch_document_newline(
    monkeypatch, terraform_repo: Path
) -> None:
    git = shutil.which("git")
    if git is None:
        pytest.skip("Git is not installed")
    layout = discover_repository(terraform_repo, Path("infrastructure"))
    monkeypatch.setattr(verification_module, "find_git", lambda: git)
    monkeypatch.setattr(verification_module, "find_terraform", lambda: None)

    result = verify_candidate_patch(candidate_patch().rstrip("\n"), layout)

    assert result.commands.patch_check.status == "passed"
    assert result.commands.patch_apply.status == "passed"
    assert result.patch.endswith("\n")
