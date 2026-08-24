from __future__ import annotations

import json
import subprocess
from pathlib import Path

from semantic_terraform_agent.cache import LocalCacheStore
from semantic_terraform_agent.cli import _print_summary
from semantic_terraform_agent.models import (
    ModelDiagnosis,
    PatchFailureCategory,
    ProviderResponse,
    TokenUsage,
    VerificationAttempt,
    VerificationCommand,
    VerificationCommands,
)
from semantic_terraform_agent.orchestration.diagnose import diagnose_repository
from semantic_terraform_agent.reasoning.prompts import build_repair_prompt_parts
from semantic_terraform_agent.terraform.plan_diagnostics import (
    classify_plan_failure,
    is_environmental_plan_failure,
)


PATCH = (
    "--- a/infrastructure/main.tf\n"
    "+++ b/infrastructure/main.tf\n"
    "@@ -2 +2 @@\n"
    '-  mode = "fast"\n'
    '+  mode = "safe"\n'
)


class PlanProvider:
    def __init__(self) -> None:
        self.diagnose_calls = 0
        self.repair_calls = 0

    def diagnose(self, request):
        self.diagnose_calls += 1
        return ProviderResponse(
            diagnosis=ModelDiagnosis(
                root_cause="The Terraform mode violates the provider constraint.",
                affected_resources=["example_widget.primary"],
                violated_constraint="mode must be safe",
                suggested_patch=PATCH,
                confidence=0.81,
                evidence=[
                    {"source": "terraform_error", "detail": "mode is invalid"},
                    {"source": "terraform_source", "detail": "resource sets mode"},
                    {"source": "git_diff", "detail": "mode changed"},
                ],
            ),
            token_usage=TokenUsage(input_tokens=10, output_tokens=5, total_tokens=15),
        )

    def repair(self, request):
        self.repair_calls += 1
        return ProviderResponse(
            candidate_edit={
                "edits": [
                    {
                        "file": "infrastructure/main.tf",
                        "old_text": 'mode = "fast"',
                        "new_text": 'mode = "slow"',
                    }
                ]
            },
            token_usage=TokenUsage(input_tokens=8, output_tokens=4, total_tokens=12),
        )


def _passed() -> VerificationCommand:
    return VerificationCommand(command=["test"], status="passed", exit_code=0)


def _verified_attempt(patch: str, attempt: int) -> VerificationAttempt:
    passed = _passed()
    return VerificationAttempt(
        attempt=attempt,
        patch=patch,
        status="verified",
        changed_files=["infrastructure/main.tf"],
        commands=VerificationCommands(
            patch_check=passed,
            patch_apply=passed,
            fmt=passed,
            init=passed,
            validate=passed,
            plan=passed,
        ),
        temporary_copy_cleaned=True,
    )


def _failed_plan_attempt(
    patch: str, attempt: int, message: str
) -> VerificationAttempt:
    passed = _passed()
    plan = VerificationCommand(
        command=["terraform", "plan", "-json"],
        status="failed",
        exit_code=1,
        stderr=message,
    )
    failure = classify_plan_failure(plan)
    environmental = is_environmental_plan_failure(failure)
    semantic = failure.classification == "terraform_semantic"
    return VerificationAttempt(
        attempt=attempt,
        patch=patch,
        status="unavailable" if environmental else "failed",
        failed_stage="plan",
        changed_files=["infrastructure/main.tf"],
        commands=VerificationCommands(
            patch_check=passed,
            patch_apply=passed,
            fmt=passed,
            init=passed,
            validate=passed,
            plan=plan,
        ),
        temporary_copy_cleaned=True,
        failure_category=(
            PatchFailureCategory.ENVIRONMENT_FAILURE
            if environmental
            else (
                PatchFailureCategory.SEMANTIC_VERIFICATION_FAILURE
                if semantic
                else PatchFailureCategory.UNKNOWN
            )
        ),
        failure_reason_code=(
            "environment_failure"
            if environmental
            else (
                "terraform_verification_failure"
                if semantic
                else "unknown_patch_failure"
            )
        ),
        failure_description=failure.detail,
        plan_failure=failure,
    )


def _commit(root: Path) -> str:
    for command in (
        ["git", "init", "-b", "main"],
        ["git", "config", "user.email", "tests@example.invalid"],
        ["git", "config", "user.name", "Tests"],
        ["git", "add", "."],
        ["git", "commit", "-m", "fixture"],
    ):
        subprocess.run(command, cwd=root, check=True, capture_output=True)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _run(
    terraform_repo: Path,
    failure_log: Path,
    diff_file: Path,
    provider: PlanProvider,
    verifier,
    *,
    source_revision: str | None = None,
    cache_store: LocalCacheStore | None = None,
    failure_memory_enabled: bool = False,
    max_repair_attempts: int = 1,
):
    return diagnose_repository(
        repo_path=terraform_repo,
        terraform_dir=Path("infrastructure"),
        log_file=failure_log,
        diff_file=diff_file,
        provider_name="openrouter",
        model="test/model:free",
        context_mode="lightweight",
        llm_provider=provider,
        patch_verifier=verifier,
        max_repair_attempts=max_repair_attempts,
        source_revision=source_revision,
        cache_store=cache_store,
        failure_memory_enabled=failure_memory_enabled,
        repository_id="owner/repository",
    )


def test_full_plan_pass_is_fully_verified_and_normally_eligible(
    terraform_repo: Path, failure_log: Path, diff_file: Path
) -> None:
    commit = _commit(terraform_repo)
    provider = PlanProvider()
    result = _run(
        terraform_repo,
        failure_log,
        diff_file,
        provider,
        lambda patch, layout, *, attempt: _verified_attempt(patch, attempt),
        source_revision=commit,
    )
    assert result.verification_assessment.outcome == "fully_verified"
    assert result.verification_assessment.full_verification_passed is True
    assert result.verification_assessment.plan_passed is True
    assert result.mutation_eligibility.eligible is True
    assert result.mutation_eligibility.eligibility_level == "verified"
    assert result.mutation_eligibility.reason_code == "verified_terraform_patch"
    assert provider.diagnose_calls == 1
    assert provider.repair_calls == 0


def test_access_denied_is_environment_blocked_conditional_and_not_remembered(
    terraform_repo: Path,
    failure_log: Path,
    diff_file: Path,
    tmp_path: Path,
    capsys,
) -> None:
    commit = _commit(terraform_repo)
    provider = PlanProvider()
    store = LocalCacheStore(tmp_path / "cache")
    result = _run(
        terraform_repo,
        failure_log,
        diff_file,
        provider,
        lambda patch, layout, *, attempt: _failed_plan_attempt(
            patch,
            attempt,
            "AccessDenied: The assumed TerraFix role is not authorized to perform ec2:DescribeInstances",
        ),
        source_revision=commit,
        cache_store=store,
        failure_memory_enabled=True,
    )
    assessment = result.verification_assessment
    assert assessment.outcome == "environment_blocked"
    assert assessment.plan_failure.classification == "permissions"
    assert assessment.plan_failure.reason_code == "aws_access_denied"
    assert assessment.plan_attempted is True
    assert assessment.plan_passed is False
    assert assessment.full_verification_passed is False
    assert result.mutation_eligibility.eligible is True
    assert result.mutation_eligibility.eligibility_level == "conditional"
    assert result.mutation_eligibility.reason_code == (
        "terraform_plan_environment_blocked"
    )
    assert result.cache.failure_memory.write_status == "not_attempted"
    assert store.stats()["failure_memory_entries"] == 0
    assert provider.diagnose_calls == 1
    assert provider.repair_calls == 0
    assert result.llm_usage.call_count == 1
    assert result.diagnosis.initial.root_cause == (
        "The Terraform mode violates the provider constraint."
    )
    assert result.diagnosis.model_confidence == 0.81

    _print_summary(result, Path("result.json"))
    rendered = capsys.readouterr().out
    assert "Terraform verification: ENVIRONMENT BLOCKED" in rendered
    assert "Terraform plan: FAILED" in rendered
    assert "Class:  permissions" in rendered
    assert "Apply eligibility: CONDITIONAL" in rendered


def test_semantic_plan_failure_is_ineligible_and_can_use_one_repair(
    terraform_repo: Path, failure_log: Path, diff_file: Path
) -> None:
    commit = _commit(terraform_repo)
    provider = PlanProvider()
    calls = 0

    def verifier(patch, layout, *, attempt):
        nonlocal calls
        calls += 1
        if attempt == 1:
            return _failed_plan_attempt(
                patch,
                attempt,
                'Error: Invalid value for variable "environment"',
            )
        return _verified_attempt(patch, attempt)

    result = _run(
        terraform_repo,
        failure_log,
        diff_file,
        provider,
        verifier,
        source_revision=commit,
    )
    assert provider.diagnose_calls == provider.repair_calls == 1
    assert result.llm_usage.call_count == 2
    assert calls == 2
    assert result.diagnosis.attempts[0].plan_failure.classification == (
        "terraform_semantic"
    )
    assert result.verification_assessment.outcome == "fully_verified"
    assert result.diagnosis.initial.root_cause == result.diagnosis.repair.root_cause
    assert result.diagnosis.initial.model_confidence == (
        result.diagnosis.repair.model_confidence
    )


def test_resource_precondition_plan_failure_uses_second_semantic_call(
    terraform_repo: Path, failure_log: Path, diff_file: Path
) -> None:
    (terraform_repo / "infrastructure/main.tf").write_text(
        '''variable "environment" {
  type = number
}

resource "example_widget" "primary" {
  mode = "fast"

  lifecycle {
    precondition {
      condition     = self.mode == "slow"
      error_message = "mode must be slow"
    }
  }
}
''',
        encoding="utf-8",
    )
    commit = _commit(terraform_repo)

    class PreconditionProvider:
        def __init__(self) -> None:
            self.diagnose_calls = 0
            self.repair_calls = 0
            self.repair_request = None

        def diagnose(self, request):
            self.diagnose_calls += 1
            return ProviderResponse(
                diagnosis=ModelDiagnosis(
                    root_cause="The environment variable has the wrong Terraform type.",
                    affected_resources=["example_widget.primary"],
                    violated_constraint="environment must accept a string",
                    edits=[
                        {
                            "file": "infrastructure/main.tf",
                            "old_text": "  type = number",
                            "new_text": "  type = string",
                        }
                    ],
                    confidence=0.88,
                    evidence=[
                        {"source": "terraform_error", "detail": "invalid variable type"},
                        {"source": "terraform_source", "detail": "type is number"},
                    ],
                ),
                token_usage=TokenUsage(
                    input_tokens=10, output_tokens=5, total_tokens=15
                ),
            )

        def repair(self, request):
            self.repair_calls += 1
            self.repair_request = request
            return ProviderResponse(
                candidate_edit={
                    "edits": [
                        {
                            "file": "infrastructure/main.tf",
                            "old_text": "  type = number",
                            "new_text": "  type = string",
                        },
                        {
                            "file": "infrastructure/main.tf",
                            "old_text": '  mode = "fast"',
                            "new_text": '  mode = "slow"',
                        },
                    ]
                },
                token_usage=TokenUsage(
                    input_tokens=12, output_tokens=6, total_tokens=18
                ),
            )

    provider = PreconditionProvider()
    diagnostic = json.dumps(
        {
            "type": "diagnostic",
            "diagnostic": {
                "severity": "error",
                "summary": "Resource precondition failed",
                "detail": "mode must be slow",
                "range": {
                    "filename": "infrastructure/main.tf",
                    "start": {"line": 10, "column": 7, "byte": 150},
                },
                "address": "example_widget.primary",
            },
        }
    )

    def verifier(patch, layout, *, attempt):
        assert '+  type = string' in patch
        if attempt == 1:
            assert '+  mode = "slow"' not in patch
            return _failed_plan_attempt(patch, attempt, diagnostic)
        assert '+  mode = "slow"' in patch
        return _verified_attempt(patch, attempt)

    result = _run(
        terraform_repo,
        failure_log,
        diff_file,
        provider,
        verifier,
        source_revision=commit,
    )

    first_failure = result.diagnosis.attempts[0].plan_failure
    assert first_failure.classification == "terraform_semantic"
    assert first_failure.reason_code == "resource_precondition_failed"
    assert result.diagnosis.verification_status == "verified_after_retry"
    assert result.verification_assessment.outcome == "fully_verified"
    assert provider.diagnose_calls == provider.repair_calls == 1
    assert result.llm_usage.call_count == 2
    assert len(result.diagnosis.attempts) == 2
    assert '+  type = string' in result.diagnosis.final_patch
    assert '+  mode = "slow"' in result.diagnosis.final_patch

    repair_prompt = build_repair_prompt_parts(provider.repair_request).user
    assert '"summary":"Resource precondition failed"' in repair_prompt
    assert '"detail":"mode must be slow"' in repair_prompt
    assert '"source_file":"infrastructure/main.tf"' in repair_prompt
    assert '"source_line":10' in repair_prompt
    assert '"resource_address":"example_widget.primary"' in repair_prompt
    assert "precondition {" in repair_prompt


def test_semantic_and_unknown_terminal_plan_failures_are_ineligible(
    terraform_repo: Path, failure_log: Path, diff_file: Path
) -> None:
    for message, expected in (
        ('Error: Unsupported argument "mode"', "semantic_failure"),
        ("the provider returned an unexplained planning failure", "unknown_failure"),
    ):
        provider = PlanProvider()
        result = _run(
            terraform_repo,
            failure_log,
            diff_file,
            provider,
            lambda patch, layout, *, attempt, value=message: _failed_plan_attempt(
                patch, attempt, value
            ),
            max_repair_attempts=0 if expected == "semantic_failure" else 1,
        )
        assert result.verification_assessment.outcome == expected
        assert result.mutation_eligibility.eligible is False
        assert result.mutation_eligibility.eligibility_level == "ineligible"
        assert provider.diagnose_calls == 1
        assert provider.repair_calls == 0
        assert result.llm_usage.call_count == 1
