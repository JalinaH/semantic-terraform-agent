from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest

from semantic_terraform_agent.collectors.repository import discover_repository
from semantic_terraform_agent.config import InputError
from semantic_terraform_agent.models import (
    Diagnosis,
    DiagnosisCandidate,
    PlanFailure,
    TerraformInfo,
    VerificationAttempt,
    VerificationCommand,
    VerificationCommands,
    VerificationSignal,
)
from semantic_terraform_agent.terraform.provenance import (
    build_verified_patch_contract,
    collect_source_provenance,
)
from semantic_terraform_agent.terraform.verification import inspect_patch


PATCH = (
    "--- a/infrastructure/main.tf\n"
    "+++ b/infrastructure/main.tf\n"
    "@@ -2 +2 @@\n"
    '-  mode = "fast"\n'
    '+  mode = "safe"\n'
)


def _passed() -> VerificationCommand:
    return VerificationCommand(command=["test"], status="passed", exit_code=0)


def _diagnosis(
    patch: str,
    *,
    status: str = "verified_first_attempt",
    attempt_number: int = 1,
    attempt_status: str = "verified",
    candidate_source: str = "llm",
    complete_commands: bool = True,
) -> Diagnosis:
    command = _passed() if complete_commands else None
    attempt = VerificationAttempt(
        attempt=attempt_number,
        patch=patch,
        status=attempt_status,
        failed_stage=None if attempt_status == "verified" else "patch_check",
        changed_files=["infrastructure/main.tf"],
        commands=VerificationCommands(
            patch_check=command,
            patch_apply=command,
            fmt=command,
            init=command,
            validate=command,
            plan=command,
        ),
        temporary_copy_cleaned=True,
        candidate_source=candidate_source,
    )
    candidate = DiagnosisCandidate(
        root_cause="invalid mode",
        affected_resources=["example_widget.primary"],
        violated_constraint="mode must be safe",
        suggested_patch=patch,
        model_confidence=0.9,
        evidence=[],
    )
    passed = status in {"verified_first_attempt", "verified_after_retry"}
    return Diagnosis(
        initial=candidate,
        attempts=[attempt],
        final_patch=patch,
        verification_status=status,
        model_confidence=0.9,
        evidence_score=1.0,
        verification=VerificationSignal(passed=passed, status=status),
    )


def _git_commit(root: Path) -> str:
    commands = (
        ["git", "init", "-b", "main"],
        ["git", "config", "user.email", "tests@example.invalid"],
        ["git", "config", "user.name", "Tests"],
        ["git", "add", "."],
        ["git", "commit", "-m", "fixture"],
    )
    for command in commands:
        subprocess.run(command, cwd=root, check=True, capture_output=True)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _contract(terraform_repo: Path, diagnosis: Diagnosis):
    layout = discover_repository(terraform_repo, Path("infrastructure"))
    source = collect_source_provenance(layout)
    return build_verified_patch_contract(
        diagnosis=diagnosis,
        layout=layout,
        source=source,
        terraform=TerraformInfo(schema_extraction_status="not-requested"),
    )


def test_exact_patch_sha256_preserves_line_endings(terraform_repo: Path) -> None:
    _git_commit(terraform_repo)
    lf_artifact, *_ = _contract(terraform_repo, _diagnosis(PATCH))
    crlf = PATCH.replace("\n", "\r\n")
    crlf_artifact, *_ = _contract(terraform_repo, _diagnosis(crlf))
    assert lf_artifact.patch_sha256 == hashlib.sha256(PATCH.encode("utf-8")).hexdigest()
    assert crlf_artifact.patch_sha256 == hashlib.sha256(crlf.encode("utf-8")).hexdigest()
    assert crlf_artifact.patch_sha256 != lf_artifact.patch_sha256


def test_verified_existing_terraform_patch_is_eligible(terraform_repo: Path) -> None:
    commit = _git_commit(terraform_repo)
    artifact, source, verification, eligibility = _contract(
        terraform_repo, _diagnosis(PATCH)
    )
    assert artifact.affected_files == ["infrastructure/main.tf"]
    assert artifact.terraform_files_only is True
    assert artifact.existing_files_only is True
    assert artifact.verified_against_commit_sha == commit
    assert artifact.source_fingerprint_sha256 == source.source_fingerprint_sha256
    assert verification.plan_passed is True
    assert eligibility.model_dump() == {
        "eligible": True,
        "eligibility_level": "verified",
        "reason_code": "verified_terraform_patch",
        "reasons": [],
        "requires_fresh_head_check": True,
    }


def test_repaired_final_patch_and_memory_candidate_use_current_exact_artifact(
    terraform_repo: Path,
) -> None:
    _git_commit(terraform_repo)
    repaired = PATCH.replace('mode = "safe"', 'mode = "slow"')
    artifact, *_ = _contract(
        terraform_repo,
        _diagnosis(
            repaired,
            status="verified_after_retry",
            attempt_number=2,
            candidate_source="verified_failure_memory",
        ),
    )
    assert artifact.patch_sha256 == hashlib.sha256(repaired.encode()).hexdigest()
    assert artifact.verification_attempt == 2
    assert artifact.candidate_source == "verified_failure_memory"


def test_final_patch_must_match_the_patch_recorded_by_the_verifier(
    terraform_repo: Path,
) -> None:
    _git_commit(terraform_repo)
    diagnosis = _diagnosis(PATCH)
    mismatched = diagnosis.model_copy(
        update={"final_patch": PATCH.replace('mode = "safe"', 'mode = "slow"')}
    )
    *_, eligibility = _contract(terraform_repo, mismatched)
    assert eligibility.reason_code == "unsafe_patch"
    assert "verified_patch_mismatch" in eligibility.reasons


@pytest.mark.parametrize(
    ("status", "attempt_status", "reason"),
    [
        ("patch_rejected", "rejected", "patch_rejected"),
        ("verification_failed", "failed", "verification_failed"),
        ("verification_unavailable", "unavailable", "verification_unavailable"),
        ("verification_skipped", "skipped", "verification_skipped"),
    ],
)
def test_unverified_statuses_are_never_eligible(
    terraform_repo: Path, status: str, attempt_status: str, reason: str
) -> None:
    _git_commit(terraform_repo)
    *_, eligibility = _contract(
        terraform_repo,
        _diagnosis(PATCH, status=status, attempt_status=attempt_status),
    )
    assert eligibility.eligible is False
    assert eligibility.reason_code == reason


def test_empty_and_incomplete_verified_candidates_are_ineligible(
    terraform_repo: Path,
) -> None:
    _git_commit(terraform_repo)
    empty, *_rest, empty_eligibility = _contract(
        terraform_repo,
        _diagnosis("", status="patch_rejected", attempt_status="rejected"),
    )
    assert empty.patch_sha256 is None
    assert empty_eligibility.reason_code == "no_patch"

    *_, incomplete = _contract(
        terraform_repo, _diagnosis(PATCH, complete_commands=False)
    )
    assert incomplete.reason_code == "not_verified"


def test_non_git_and_dirty_git_sources_are_not_mutation_eligible(
    terraform_repo: Path,
) -> None:
    *_, non_git = _contract(terraform_repo, _diagnosis(PATCH))
    assert non_git.reason_code == "source_revision_unknown"

    _git_commit(terraform_repo)
    (terraform_repo / "README.md").write_text("dirty", encoding="utf-8")
    *_, dirty = _contract(terraform_repo, _diagnosis(PATCH))
    assert dirty.reason_code == "source_revision_unknown"
    assert "working_tree_not_clean" in dirty.reasons


def test_source_revision_match_is_recorded_and_mismatch_fails(terraform_repo: Path) -> None:
    commit = _git_commit(terraform_repo)
    layout = discover_repository(terraform_repo, Path("infrastructure"))
    source = collect_source_provenance(layout, source_revision=commit.upper())
    assert source.caller_source_revision == commit
    assert source.verified_against_commit_sha == commit
    with pytest.raises(InputError, match="does not match"):
        collect_source_provenance(layout, source_revision="f" * 40)
    with pytest.raises(InputError, match="full 40- or 64-character"):
        collect_source_provenance(layout, source_revision="abc")


def test_source_revision_cannot_anchor_non_git_repository(terraform_repo: Path) -> None:
    layout = discover_repository(terraform_repo, Path("infrastructure"))
    with pytest.raises(InputError, match="HEAD could not be verified"):
        collect_source_provenance(layout, source_revision="a" * 40)


def test_multiple_files_are_deduplicated_and_sorted(terraform_repo: Path) -> None:
    tf_json = terraform_repo / "infrastructure/settings.tf.json"
    tf_json.write_text('{}\n', encoding="utf-8")
    _git_commit(terraform_repo)
    patch = (
        PATCH
        + "--- a/infrastructure/settings.tf.json\n"
        + "+++ b/infrastructure/settings.tf.json\n"
        + "@@ -1 +1 @@\n-{}\n+{\"terraform\": {}}\n"
        + "--- a/infrastructure/main.tf\n"
        + "+++ b/infrastructure/main.tf\n"
        + "@@ -2 +2 @@\n-  mode = \"fast\"\n+  mode = \"slow\"\n"
    )
    artifact, *_ = _contract(terraform_repo, _diagnosis(patch))
    assert artifact.affected_files == [
        "infrastructure/main.tf",
        "infrastructure/settings.tf.json",
    ]


@pytest.mark.parametrize(
    "patch",
    [
        "--- a/../../secret.tf\n+++ b/../../secret.tf\n@@ -1 +1 @@\n-a\n+b\n",
        "--- /absolute/main.tf\n+++ /absolute/main.tf\n@@ -1 +1 @@\n-a\n+b\n",
    ],
)
def test_unsafe_paths_never_produce_repository_relative_artifact(
    terraform_repo: Path, patch: str
) -> None:
    _git_commit(terraform_repo)
    artifact, *_, eligibility = _contract(
        terraform_repo,
        _diagnosis(patch, status="patch_rejected", attempt_status="rejected"),
    )
    assert artifact.repository_relative_paths_only is False
    assert eligibility.eligible is False


def test_non_terraform_new_delete_and_rename_are_conservatively_ineligible(
    terraform_repo: Path,
) -> None:
    _git_commit(terraform_repo)
    cases = (
        (
            "--- a/README.md\n+++ b/README.md\n@@ -1 +1 @@\n-old\n+new\n",
            "non_terraform_files",
        ),
        (
            "--- /dev/null\n+++ b/infrastructure/new.tf\n@@ -0,0 +1 @@\n+locals {}\n",
            "unsafe_patch",
        ),
        (
            "--- a/infrastructure/main.tf\n+++ /dev/null\n@@ -1 +0,0 @@\n-resource \"x\" \"y\" {}\n",
            "unsafe_patch",
        ),
        (
            "--- a/infrastructure/main.tf\n+++ b/infrastructure/renamed.tf\n@@ -1 +1 @@\n-a\n+b\n",
            "unsafe_patch",
        ),
    )
    for patch, reason in cases:
        *_, eligibility = _contract(terraform_repo, _diagnosis(patch))
        assert eligibility.eligible is False
        assert eligibility.reason_code == reason


def test_environment_blocked_plan_cannot_override_unsafe_patch_scope(
    terraform_repo: Path,
) -> None:
    _git_commit(terraform_repo)
    patch = "--- a/README.md\n+++ b/README.md\n@@ -1 +1 @@\n-old\n+new\n"
    diagnosis = _diagnosis(
        patch,
        status="verification_unavailable",
        attempt_status="unavailable",
    )
    passed = _passed()
    plan = VerificationCommand(
        command=["terraform", "plan", "-json"],
        status="failed",
        exit_code=1,
        stderr="AccessDenied",
    )
    failure = PlanFailure(
        classification="permissions",
        reason_code="aws_access_denied",
        summary="AccessDenied",
        detail="The caller is not authorized to perform the requested action.",
        diagnostic_format="bounded_text",
    )
    attempt = diagnosis.attempts[0].model_copy(
        update={
            "failed_stage": "plan",
            "commands": VerificationCommands(
                patch_check=passed,
                patch_apply=passed,
                fmt=passed,
                init=passed,
                validate=passed,
                plan=plan,
            ),
            "plan_failure": failure,
        }
    )
    diagnosis = diagnosis.model_copy(update={"attempts": [attempt]})
    *_, eligibility = _contract(terraform_repo, diagnosis)
    assert eligibility.eligible is False
    assert eligibility.eligibility_level == "ineligible"
    assert eligibility.reason_code == "non_terraform_files"


def test_patch_inspection_reports_create_delete_and_rename() -> None:
    created = inspect_patch(
        "--- /dev/null\n+++ b/new.tf\n@@ -0,0 +1 @@\n+locals {}\n"
    )
    deleted = inspect_patch(
        "--- a/old.tf\n+++ /dev/null\n@@ -1 +0,0 @@\n-locals {}\n"
    )
    renamed = inspect_patch(
        "--- a/old.tf\n+++ b/new.tf\n@@ -1 +1 @@\n-a\n+b\n"
    )
    assert created.creates_files is True
    assert deleted.deletes_files is True
    assert renamed.renames_files is True
