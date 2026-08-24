"""Read-only source and verified-patch provenance for platform mutation decisions."""

from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path

from semantic_terraform_agent.cache import derive_repository_scope
from semantic_terraform_agent.collectors.repository import (
    RepositoryLayout,
    safe_repo_file,
)
from semantic_terraform_agent.config import DEFAULT_LIMITS, InputError
from semantic_terraform_agent.models import (
    Diagnosis,
    MutationEligibility,
    SourceProvenance,
    TerraformInfo,
    VerificationAssessment,
    VerificationProvenance,
    VerifiedPatchArtifact,
)
from semantic_terraform_agent.terraform.assessment import assess_verification
from semantic_terraform_agent.terraform.verification import (
    UnsafePatchError,
    inspect_patch,
    validate_patch_scope,
)


_GIT_SHA = re.compile(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}")


def normalize_source_revision(value: str) -> str:
    revision = value.strip()
    if not _GIT_SHA.fullmatch(revision):
        raise InputError("source revision must be a full 40- or 64-character Git SHA")
    return revision.lower()


def collect_source_provenance(
    layout: RepositoryLayout,
    *,
    source_revision: str | None = None,
    repository_id: str | None = None,
) -> SourceProvenance:
    """Detect immutable Git identity without modifying or contacting the repository."""
    caller_revision = (
        normalize_source_revision(source_revision) if source_revision is not None else None
    )
    commit = _git_value(layout.root, ["rev-parse", "--verify", "HEAD^{commit}"])
    commit = commit.lower() if commit and _GIT_SHA.fullmatch(commit) else None
    if caller_revision is not None and commit is None:
        raise InputError(
            "source revision was supplied but the repository HEAD could not be verified"
        )
    if caller_revision is not None and caller_revision != commit:
        raise InputError(
            "source revision does not match the checked-out repository HEAD"
        )

    tree = _git_value(layout.root, ["rev-parse", "--verify", "HEAD^{tree}"])
    tree = tree.lower() if tree and _GIT_SHA.fullmatch(tree) else None
    status = _git_status(layout.root) if commit else None
    mode = "non_git" if commit is None else ("git_clean" if status == "" else "git_dirty")
    verified_commit = commit if mode == "git_clean" else None
    return SourceProvenance(
        repository_scope=derive_repository_scope(layout.root, repository_id),
        terraform_dir=layout.terraform_dir,
        git_commit_sha=commit,
        git_tree_sha=tree,
        caller_source_revision=caller_revision,
        verified_against_commit_sha=verified_commit,
        working_tree_mode=mode,
    )


def build_verified_patch_contract(
    *,
    diagnosis: Diagnosis,
    layout: RepositoryLayout,
    source: SourceProvenance,
    terraform: TerraformInfo,
    assessment: VerificationAssessment | None = None,
) -> tuple[
    VerifiedPatchArtifact,
    SourceProvenance,
    VerificationProvenance,
    MutationEligibility,
]:
    """Build the additive artifact and conservative platform-eligibility contract."""
    final_attempt = diagnosis.attempts[-1]
    assessment = assessment or assess_verification(final_attempt)
    patch = diagnosis.final_patch
    patch_hash = hashlib.sha256(patch.encode("utf-8")).hexdigest() if patch else None
    details: list[str] = []
    verified_patch_mismatch = patch != final_attempt.patch
    if verified_patch_mismatch:
        details.append("verified_patch_mismatch")

    inspection = None
    affected_files: list[str] = []
    repository_relative = False
    try:
        inspection = inspect_patch(patch)
        affected_files = sorted(set(inspection.affected_files))
        repository_relative = True
    except (UnsafePatchError, UnicodeError):
        details.append("patch_scope_invalid")

    terraform_files_only = False
    try:
        scoped_files = validate_patch_scope(patch, layout)
        affected_files = sorted(set(scoped_files))
        terraform_files_only = bool(affected_files)
        repository_relative = True
    except (UnsafePatchError, UnicodeError) as exc:
        if "only Terraform source files" in str(exc) or "selected Terraform directory" in str(exc):
            terraform_files_only = False
        if "patch_scope_invalid" not in details:
            details.append("patch_scope_invalid")

    unsupported_operation = bool(
        inspection
        and (
            inspection.creates_files
            or inspection.deletes_files
            or inspection.renames_files
        )
    )
    if unsupported_operation:
        details.append("unsupported_file_operation")

    existing_files_only = bool(affected_files) and all(
        path in layout.terraform_files
        and (layout.root / path).is_file()
        and not (layout.root / path).is_symlink()
        for path in affected_files
    )
    if affected_files and not existing_files_only:
        details.append("affected_file_missing")

    source_fingerprint = _source_fingerprint(
        layout,
        affected_files,
        source.verified_against_commit_sha,
    )
    source = source.model_copy(
        update={"source_fingerprint_sha256": source_fingerprint}
    )
    verification = _verification_provenance(diagnosis, terraform)
    if not (
        _verification_complete(verification)
        or _conditional_verification_complete(verification, assessment)
    ):
        details.append("verification_provenance_incomplete")
    if source.working_tree_mode == "git_dirty":
        details.append("working_tree_not_clean")

    artifact = VerifiedPatchArtifact(
        patch_sha256=patch_hash,
        affected_files=affected_files,
        repository_relative_paths_only=repository_relative,
        terraform_files_only=terraform_files_only,
        existing_files_only=existing_files_only,
        verification_status=diagnosis.verification_status,
        verification_passed=diagnosis.verification.passed,
        verification_attempt=final_attempt.attempt,
        verified_against_commit_sha=source.verified_against_commit_sha,
        source_fingerprint_sha256=source_fingerprint,
        candidate_source=final_attempt.candidate_source,
    )
    eligibility = _mutation_eligibility(
        artifact=artifact,
        source=source,
        verification=verification,
        patch=patch,
        unsupported_operation=unsupported_operation,
        verified_patch_mismatch=verified_patch_mismatch,
        details=details,
        assessment=assessment,
    )
    return artifact, source, verification, eligibility


def _verification_provenance(
    diagnosis: Diagnosis, terraform: TerraformInfo
) -> VerificationProvenance:
    attempt = diagnosis.attempts[-1]
    commands = attempt.commands
    provider_versions = {
        record.provider_source or record.resource_type: record.provider_version
        for record in terraform.schemas
        if record.provider_version is not None
    }
    return VerificationProvenance(
        attempt_number=attempt.attempt,
        final_status=diagnosis.verification_status,
        verified_in_isolated_workspace=(
            attempt.status == "verified" and attempt.isolation == "temporary-copy"
        ),
        patch_check_passed=_passed(commands.patch_check),
        patch_apply_passed=_passed(commands.patch_apply),
        fmt_passed=_passed(commands.fmt),
        init_passed=_passed(commands.init),
        validate_passed=_passed(commands.terraform_validate),
        plan_attempted=bool(
            commands.plan is not None and commands.plan.status != "skipped"
        ),
        plan_passed=_passed(commands.plan),
        terraform_version=terraform.version,
        provider_versions=dict(sorted(provider_versions.items())),
    )


def _mutation_eligibility(
    *,
    artifact: VerifiedPatchArtifact,
    source: SourceProvenance,
    verification: VerificationProvenance,
    patch: str,
    unsupported_operation: bool,
    verified_patch_mismatch: bool,
    details: list[str],
    assessment: VerificationAssessment,
) -> MutationEligibility:
    status = artifact.verification_status
    if not patch:
        reason = "no_patch"
        details.append("patch_empty")
    elif artifact.patch_sha256 is None:
        reason = "patch_hash_unavailable"
    elif not artifact.affected_files or not artifact.repository_relative_paths_only:
        reason = "affected_files_unknown"
    elif verified_patch_mismatch:
        reason = "unsafe_patch"
    elif unsupported_operation:
        reason = "unsafe_patch"
    elif not artifact.terraform_files_only:
        reason = "non_terraform_files"
    elif not artifact.existing_files_only:
        reason = "unsafe_patch"
    elif source.verified_against_commit_sha is None:
        reason = "source_revision_unknown"
    elif (
        assessment.outcome == "fully_verified"
        and artifact.verification_passed
        and _verification_complete(verification)
    ):
        reason = "verified_terraform_patch"
    elif (
        assessment.outcome == "environment_blocked"
        and _conditional_verification_complete(verification, assessment)
    ):
        reason = "terraform_plan_environment_blocked"
    elif status == "patch_rejected":
        reason = "patch_rejected"
    elif status == "verification_failed":
        reason = "verification_failed"
    elif status == "verification_unavailable":
        reason = "verification_unavailable"
    elif status == "verification_skipped":
        reason = "verification_skipped"
    else:
        reason = "not_verified"
    eligibility_level = {
        "verified_terraform_patch": "verified",
        "terraform_plan_environment_blocked": "conditional",
    }.get(reason, "ineligible")
    eligible = eligibility_level in {"verified", "conditional"}
    return MutationEligibility(
        eligible=eligible,
        eligibility_level=eligibility_level,
        reason_code=reason,
        reasons=list(dict.fromkeys(details)),
        requires_fresh_head_check=True,
    )


def _verification_complete(value: VerificationProvenance) -> bool:
    return all(
        (
            value.verified_in_isolated_workspace,
            value.patch_check_passed,
            value.patch_apply_passed,
            value.fmt_passed,
            value.init_passed,
            value.validate_passed,
            value.plan_passed,
        )
    )


def _conditional_verification_complete(
    value: VerificationProvenance,
    assessment: VerificationAssessment,
) -> bool:
    plan_failure = assessment.plan_failure
    if plan_failure is None:
        return False
    return all(
        (
            assessment.outcome == "environment_blocked",
            plan_failure.classification
            in {
                "credentials",
                "permissions",
                "network",
                "provider_unavailable",
                "external_service",
                "runtime_environment",
            },
            value.patch_check_passed,
            value.patch_apply_passed,
            value.fmt_passed,
            value.init_passed,
            value.validate_passed,
            value.plan_attempted,
            not value.plan_passed,
        )
    )


def _passed(command) -> bool:
    return command is not None and command.status == "passed"


def _source_fingerprint(
    layout: RepositoryLayout,
    affected_files: list[str],
    revision: str | None,
) -> str | None:
    if not affected_files:
        return None
    digest = hashlib.sha256()
    digest.update(b"semantic-terraform-source-v1\0")
    digest.update((revision or "").encode("ascii"))
    digest.update(b"\0")
    try:
        for relative in sorted(affected_files):
            unresolved = layout.root / relative
            if unresolved.is_symlink():
                return None
            path = safe_repo_file(layout.root, relative)
            if not path.is_file() or path.stat().st_size > DEFAULT_LIMITS.max_source_bytes:
                return None
            content = path.read_bytes()
            encoded_path = relative.encode("utf-8")
            digest.update(len(encoded_path).to_bytes(8, "big"))
            digest.update(encoded_path)
            digest.update(len(content).to_bytes(8, "big"))
            digest.update(content)
    except (OSError, InputError, UnicodeError):
        return None
    return digest.hexdigest()


def _git_value(root: Path, arguments: list[str]) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "-c", "core.fsmonitor=false", "-C", str(root), *arguments],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    value = completed.stdout.strip()
    return value if completed.returncode == 0 and value else None


def _git_status(root: Path) -> str | None:
    try:
        completed = subprocess.run(
            [
                "git",
                "-c",
                "core.fsmonitor=false",
                "-C",
                str(root),
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
            ],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return completed.stdout if completed.returncode == 0 else None
