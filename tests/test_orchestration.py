from __future__ import annotations

from pathlib import Path

from semantic_terraform_agent.config import ProviderError
from semantic_terraform_agent.models import (
    ModelDiagnosis,
    ProviderResponse,
    TokenUsage,
    VerificationAttempt,
    VerificationCommand,
    VerificationCommands,
)
from semantic_terraform_agent.orchestration.diagnose import diagnose_repository
from semantic_terraform_agent.terraform.verification import verify_candidate_patch


INITIAL_PATCH = (
    "--- a/infrastructure/main.tf\n+++ b/infrastructure/main.tf\n"
    "@@ -2 +2 @@\n-  mode = \"fast\"\n+  mode = \"safe\""
)
REPAIR_PATCH = (
    "--- a/infrastructure/main.tf\n+++ b/infrastructure/main.tf\n"
    "@@ -2 +2 @@\n-  mode = \"fast\"\n+  mode = \"slow\""
)


def diagnosis(patch: str, confidence: float) -> ModelDiagnosis:
    return ModelDiagnosis(
        root_cause="The mode value violates the provider constraint.",
        affected_resources=["example_widget.primary"],
        violated_constraint="mode must be safe or slow",
        suggested_patch=patch,
        confidence=confidence,
        evidence=[
            {"source": "terraform_error", "detail": "mode is invalid"},
            {"source": "git_diff", "detail": "mode changed to fast"},
            {"source": "terraform_source", "detail": "resource sets mode"},
        ],
    )


class FakeProvider:
    def __init__(self, repair_result: ModelDiagnosis | Exception | None = None) -> None:
        self.request = None
        self.repair_request = None
        self.diagnose_calls = 0
        self.repair_calls = 0
        self.repair_result = repair_result

    def diagnose(self, request):
        self.request = request
        self.diagnose_calls += 1
        return ProviderResponse(
            diagnosis=diagnosis(INITIAL_PATCH, 0.9),
            token_usage=TokenUsage(total_tokens=10),
        )

    def repair(self, request):
        self.repair_request = request
        self.repair_calls += 1
        if isinstance(self.repair_result, Exception):
            raise self.repair_result
        assert self.repair_result is not None
        return ProviderResponse(
            diagnosis=self.repair_result,
            token_usage=TokenUsage(total_tokens=20),
        )


def command(status: str = "passed", stderr: str = "") -> VerificationCommand:
    return VerificationCommand(
        command=["test"],
        status=status,
        exit_code=0 if status == "passed" else 1,
        stderr=stderr,
    )


def attempt_result(
    patch: str,
    attempt: int,
    *,
    status: str,
    failed_stage: str | None = None,
) -> VerificationAttempt:
    failed = command("failed", f"{failed_stage} rejected the candidate")
    commands = VerificationCommands(
        patch_check=command(),
        patch_apply=command(),
        fmt=failed if failed_stage == "fmt" else command(),
        init=command(),
        validate=failed if failed_stage == "validate" else command(),
        plan=failed if failed_stage == "plan" else command(),
    )
    return VerificationAttempt(
        attempt=attempt,
        patch=patch,
        status=status,
        failed_stage=failed_stage,
        changed_files=["infrastructure/main.tf"],
        commands=commands,
        temporary_copy_cleaned=True,
        warnings=[] if status == "verified" else [f"failed at {failed_stage}"],
    )


def run_diagnosis(
    terraform_repo: Path,
    failure_log: Path,
    diff_file: Path,
    provider: FakeProvider,
    verifier,
    *,
    max_repair_attempts: int = 1,
    verification_enabled: bool = True,
):
    return diagnose_repository(
        repo_path=terraform_repo,
        terraform_dir=Path("infrastructure"),
        log_file=failure_log,
        diff_file=diff_file,
        provider_name="gemini",
        model="fake",
        context_mode="lightweight",
        llm_provider=provider,
        patch_verifier=verifier,
        max_repair_attempts=max_repair_attempts,
        verification_enabled=verification_enabled,
    )


def test_successful_first_attempt_has_no_repair(
    terraform_repo: Path, failure_log: Path, diff_file: Path
) -> None:
    provider = FakeProvider()

    def verifier(patch, layout, *, attempt):
        return attempt_result(patch, attempt, status="verified")

    result = run_diagnosis(terraform_repo, failure_log, diff_file, provider, verifier)
    assert result.status == "ok"
    assert result.diagnosis.verification_status == "verified_first_attempt"
    assert result.diagnosis.verification.passed is True
    assert result.diagnosis.repair is None
    assert len(result.diagnosis.attempts) == 1
    assert provider.diagnose_calls == 1
    assert provider.repair_calls == 0
    assert result.diagnosis.model_confidence == 0.9
    assert result.diagnosis.evidence_score == 1.0


def test_successful_second_attempt_preserves_history_and_confidence(
    terraform_repo: Path, failure_log: Path, diff_file: Path
) -> None:
    provider = FakeProvider(diagnosis(REPAIR_PATCH, 0.72))
    verifier_calls = 0

    def verifier(patch, layout, *, attempt):
        nonlocal verifier_calls
        verifier_calls += 1
        if attempt == 1:
            return attempt_result(patch, attempt, status="failed", failed_stage="plan")
        return attempt_result(patch, attempt, status="verified")

    result = run_diagnosis(terraform_repo, failure_log, diff_file, provider, verifier)
    assert result.diagnosis.verification_status == "verified_after_retry"
    assert result.diagnosis.verification.passed is True
    assert result.diagnosis.model_confidence == 0.72
    assert result.diagnosis.model_confidence != 1.0
    assert result.diagnosis.final_patch == REPAIR_PATCH
    assert [item.attempt for item in result.diagnosis.attempts] == [1, 2]
    assert [item.patch for item in result.diagnosis.attempts] == [INITIAL_PATCH, REPAIR_PATCH]
    assert provider.diagnose_calls + provider.repair_calls == 2
    assert verifier_calls == 2
    assert result.token_usage.total_tokens == 30


def test_failed_second_attempt_never_triggers_third_call(
    terraform_repo: Path, failure_log: Path, diff_file: Path
) -> None:
    provider = FakeProvider(diagnosis(REPAIR_PATCH, 0.6))
    verifier_calls = 0

    def verifier(patch, layout, *, attempt):
        nonlocal verifier_calls
        verifier_calls += 1
        stage = "plan" if attempt == 1 else "validate"
        return attempt_result(patch, attempt, status="failed", failed_stage=stage)

    result = run_diagnosis(terraform_repo, failure_log, diff_file, provider, verifier)
    assert result.diagnosis.verification_status == "verification_failed"
    assert len(result.diagnosis.attempts) == 2
    assert provider.repair_calls == 1
    assert verifier_calls == 2


def test_retry_not_executed_when_disabled(
    terraform_repo: Path, failure_log: Path, diff_file: Path
) -> None:
    provider = FakeProvider(diagnosis(REPAIR_PATCH, 0.7))

    def verifier(patch, layout, *, attempt):
        return attempt_result(patch, attempt, status="failed", failed_stage="plan")

    result = run_diagnosis(
        terraform_repo,
        failure_log,
        diff_file,
        provider,
        verifier,
        max_repair_attempts=0,
    )
    assert result.diagnosis.verification_status == "verification_failed"
    assert len(result.diagnosis.attempts) == 1
    assert provider.repair_calls == 0


def test_retry_prohibited_for_rejected_and_unavailable_results(
    terraform_repo: Path, failure_log: Path, diff_file: Path
) -> None:
    for status, expected in (
        ("rejected", "patch_rejected"),
        ("unavailable", "verification_unavailable"),
    ):
        provider = FakeProvider(diagnosis(REPAIR_PATCH, 0.7))

        def verifier(patch, layout, *, attempt, result_status=status):
            stage = "patch_check" if result_status == "rejected" else "fmt"
            return attempt_result(
                patch, attempt, status=result_status, failed_stage=stage
            )

        result = run_diagnosis(terraform_repo, failure_log, diff_file, provider, verifier)
        assert result.diagnosis.verification_status == expected
        assert len(result.diagnosis.attempts) == 1
        assert provider.repair_calls == 0


def test_malformed_repair_response_preserves_first_attempt(
    terraform_repo: Path, failure_log: Path, diff_file: Path
) -> None:
    provider = FakeProvider(ProviderError("invalid structured JSON"))

    def verifier(patch, layout, *, attempt):
        return attempt_result(patch, attempt, status="failed", failed_stage="validate")

    result = run_diagnosis(terraform_repo, failure_log, diff_file, provider, verifier)
    assert result.diagnosis.verification_status == "verification_failed"
    assert result.diagnosis.repair is None
    assert len(result.diagnosis.attempts) == 1
    assert provider.repair_calls == 1
    assert "invalid structured JSON" in result.diagnosis.verification.reason


def test_second_patch_is_independently_rejected(
    terraform_repo: Path, failure_log: Path, diff_file: Path
) -> None:
    unsafe = diagnosis("--- a/.env\n+++ b/.env\n@@ -1 +1 @@\n-a\n+b", 0.4)
    provider = FakeProvider(unsafe)

    def verifier(patch, layout, *, attempt):
        if attempt == 1:
            return attempt_result(patch, attempt, status="failed", failed_stage="fmt")
        return verify_candidate_patch(patch, layout, attempt=attempt)

    result = run_diagnosis(terraform_repo, failure_log, diff_file, provider, verifier)
    assert result.diagnosis.verification_status == "patch_rejected"
    assert result.diagnosis.attempts[0].status == "failed"
    assert result.diagnosis.attempts[1].status == "rejected"
    assert result.diagnosis.final_patch == unsafe.suggested_patch


def test_verification_can_be_intentionally_skipped(
    terraform_repo: Path, failure_log: Path, diff_file: Path
) -> None:
    provider = FakeProvider(diagnosis(REPAIR_PATCH, 0.7))
    result = run_diagnosis(
        terraform_repo,
        failure_log,
        diff_file,
        provider,
        lambda *args, **kwargs: None,
        verification_enabled=False,
    )
    assert result.diagnosis.verification_status == "verification_skipped"
    assert result.diagnosis.attempts[0].status == "skipped"
    assert provider.repair_calls == 0
