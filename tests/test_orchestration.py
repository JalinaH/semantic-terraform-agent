from __future__ import annotations

import subprocess
import hashlib
from pathlib import Path

import pytest

from semantic_terraform_agent.cli import _print_summary
from semantic_terraform_agent.config import InputError, ProviderError
from semantic_terraform_agent.models import (
    ModelDiagnosis,
    ProviderResponse,
    TokenUsage,
    SchemaRecord,
    TerraformInfo,
    VerificationAttempt,
    VerificationCommand,
    VerificationCommands,
)
from semantic_terraform_agent.orchestration.diagnose import diagnose_repository
from semantic_terraform_agent.reasoning.prompts import build_prompt_parts
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
        if self.repair_result.suggested_patch and ".env" in self.repair_result.suggested_patch:
            edit = {"file": ".env", "old_text": "a", "new_text": "b"}
        else:
            edit = {
                "file": "infrastructure/main.tf",
                "old_text": 'mode = "fast"',
                "new_text": 'mode = "slow"',
            }
        return ProviderResponse(
            candidate_edit={"edits": [edit]},
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
        patch_check=failed if failed_stage == "patch_check" else command(),
        patch_apply=failed if failed_stage == "patch_apply" else command(),
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
    failed_stage: str | None = None,
    source_revision: str | None = None,
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
        failed_stage=failed_stage,
        source_revision=source_revision,
    )


def test_source_revision_mismatch_fails_before_model_inference(
    terraform_repo: Path, failure_log: Path, diff_file: Path
) -> None:
    for command in (
        ["git", "init", "-b", "main"],
        ["git", "config", "user.email", "tests@example.invalid"],
        ["git", "config", "user.name", "Tests"],
        ["git", "add", "."],
        ["git", "commit", "-m", "fixture"],
    ):
        subprocess.run(command, cwd=terraform_repo, check=True, capture_output=True)
    provider = FakeProvider()
    with pytest.raises(InputError, match="does not match"):
        run_diagnosis(
            terraform_repo,
            failure_log,
            diff_file,
            provider,
            lambda patch, layout, *, attempt: attempt_result(
                patch, attempt, status="verified"
            ),
            source_revision="f" * 40,
        )
    assert provider.diagnose_calls == 0


def test_successful_first_attempt_has_no_repair(
    terraform_repo: Path, failure_log: Path, diff_file: Path, capsys
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
    assert result.llm_usage.call_count == 1
    assert result.llm_calls[0].call_type.value == "diagnosis"
    assert result.context_telemetry.mode == "lightweight"
    assert result.context_telemetry.git_diff_included is True
    assert result.context_telemetry.source_file_count == 1
    assert result.context_telemetry.source_block_count == 1
    assert result.context_telemetry.sections["terraform_error"].characters > 0
    assert result.context_telemetry.sections["terraform_source"].characters > 0
    assert result.context_telemetry.calls[0].call_type.value == "diagnosis"
    assert result.context_manifest.included_resources == ["example_widget.primary"]
    assert result.context_manifest.ambiguous is False
    assert result.context_optimization.strategy == "deterministic_minimal_v1"
    assert result.context_optimization.input_token_reduction_ratio is None
    assert "context_build_seconds" in result.timing
    assert "schema_slice_seconds" in result.timing
    assert result.schema_optimization is None
    assert result.schema_slice_manifest == []
    assert result.context_telemetry.sections["provider_schema"].characters == 0
    _print_summary(result, Path("result.json"))
    rendered = capsys.readouterr().out
    assert "Provider schema:     not used" in rendered
    assert "Model routing:" in rendered
    assert "Mode:              fixed" in rendered
    assert "Model escalated:   no" in rendered


def test_schema_aware_orchestration_slices_locally_and_persists_only_manifest(
    terraform_repo: Path,
    failure_log: Path,
    diff_file: Path,
    monkeypatch,
    capsys,
) -> None:
    provider = FakeProvider()
    full_schema = {
        "version": 1,
        "block": {
            "attributes": {
                "mode": {"type": "string", "required": True},
                "unrelated": {
                    "type": "string",
                    "optional": True,
                    "description": "x" * 1_000 + " FULL_SCHEMA_ONLY",
                },
            }
        },
    }

    def fake_inspect(layout, resource_types, *, enabled):
        assert enabled is True
        assert resource_types == ["example_widget"]
        return (
            TerraformInfo(
                version="1.9.0",
                schema_extraction_status="ok",
                schemas=[
                    SchemaRecord(
                        resource_type="example_widget",
                        provider_source="registry.terraform.io/example/example",
                        provider_version="1.2.3",
                        extraction_status="ok",
                        schema=full_schema,
                    )
                ],
            ),
            [],
        )

    monkeypatch.setattr(
        "semantic_terraform_agent.orchestration.diagnose.inspect_schemas",
        fake_inspect,
    )

    def verifier(patch, layout, *, attempt):
        return attempt_result(patch, attempt, status="verified")

    result = diagnose_repository(
        repo_path=terraform_repo,
        terraform_dir=Path("infrastructure"),
        log_file=failure_log,
        diff_file=diff_file,
        provider_name="gemini",
        model="fake",
        context_mode="schema-aware",
        llm_provider=provider,
        patch_verifier=verifier,
    )

    assert provider.request.schema_strategy == "sliced"
    assert len(provider.request.schema_slices) == 1
    assert provider.request.schema_slices[0].manifest.selected_paths == [
        "block.attributes.mode"
    ]
    prompt = build_prompt_parts(provider.request)
    assert "FULL_SCHEMA_ONLY" not in prompt.user
    assert '"mode":{"required":true,"type":"string"}' in prompt.user
    assert result.terraform.schemas[0].resource_schema == full_schema
    assert result.schema_slice_manifest == [
        provider.request.schema_slices[0].manifest
    ]
    assert result.schema_optimization.selected_path_count == 1
    assert result.schema_optimization.input_token_reduction_ratio is None
    assert "schema_slice_seconds" in result.timing
    schema_section = result.context_telemetry.sections["provider_schema"]
    assert schema_section.full_available_characters > (
        schema_section.selected_schema_characters
    )
    assert result.model_dump_json(by_alias=True).count("FULL_SCHEMA_ONLY") == 1
    _print_summary(result, Path("result.json"))
    rendered = capsys.readouterr().out
    assert "Schema strategy:     deterministic_schema_slice_v1" in rendered
    assert "Provider schema:" in rendered
    assert "Selected paths:      1" in rendered
    assert "Schema fallback:     no" in rendered


def test_explicit_failed_stage_overrides_log_inference(
    terraform_repo: Path, failure_log: Path, diff_file: Path
) -> None:
    provider = FakeProvider()

    def verifier(patch, layout, *, attempt):
        return attempt_result(patch, attempt, status="verified")

    result = run_diagnosis(
        terraform_repo,
        failure_log,
        diff_file,
        provider,
        verifier,
        failed_stage="validate",
    )
    assert result.failure.stage == "validate"
    assert provider.request.failure.stage == "validate"


def test_final_patch_is_the_canonical_patch_used_by_verifier(
    terraform_repo: Path, failure_log: Path, diff_file: Path
) -> None:
    provider = FakeProvider()

    def verifier(patch, layout, *, attempt):
        return attempt_result(REPAIR_PATCH, attempt, status="verified")

    result = run_diagnosis(terraform_repo, failure_log, diff_file, provider, verifier)
    assert result.diagnosis.initial.suggested_patch == INITIAL_PATCH
    assert result.diagnosis.final_patch == REPAIR_PATCH
    assert result.verified_patch.patch_sha256 == hashlib.sha256(
        REPAIR_PATCH.encode("utf-8")
    ).hexdigest()


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
    assert result.diagnosis.model_confidence == 0.9
    assert result.diagnosis.model_confidence != 1.0
    assert 'mode = "slow"' in result.diagnosis.final_patch
    assert [item.attempt for item in result.diagnosis.attempts] == [1, 2]
    assert result.diagnosis.attempts[0].patch == INITIAL_PATCH
    assert result.diagnosis.attempts[1].patch == result.diagnosis.final_patch
    assert provider.diagnose_calls + provider.repair_calls == 2
    assert verifier_calls == 2
    assert result.token_usage.total_tokens == 30
    assert result.llm_usage.call_count == 2
    assert result.llm_usage.total_tokens == 30
    assert result.llm_usage.cost_usd is None
    assert result.llm_usage.cost_complete is False
    assert [call.call_type.value for call in result.llm_calls] == ["diagnosis", "repair"]
    assert [call.call_type.value for call in result.context_telemetry.calls] == [
        "diagnosis",
        "repair",
    ]
    assert (
        result.context_telemetry.calls[1]
        .sections["verification_evidence"]
        .characters
        > 0
    )


def test_patch_check_failure_can_trigger_one_safe_repair(
    terraform_repo: Path, failure_log: Path, diff_file: Path
) -> None:
    provider = FakeProvider(diagnosis(REPAIR_PATCH, 0.72))

    def verifier(patch, layout, *, attempt):
        if attempt == 1:
            return attempt_result(
                patch, attempt, status="failed", failed_stage="patch_check"
            )
        return attempt_result(patch, attempt, status="verified")

    result = run_diagnosis(terraform_repo, failure_log, diff_file, provider, verifier)
    assert result.diagnosis.verification_status == "verified_after_retry"
    assert [attempt.failed_stage for attempt in result.diagnosis.attempts] == [
        "patch_check",
        None,
    ]
    assert provider.repair_calls == 1
    assert provider.repair_request.failed_attempt.commands.patch_check.status == "failed"


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
    assert result.diagnosis.final_patch == ""
    assert result.diagnosis.attempts[1].failure_reason_code == "invalid_edit_path"


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
