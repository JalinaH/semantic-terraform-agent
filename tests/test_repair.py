from __future__ import annotations

from semantic_terraform_agent.config import DEFAULT_LIMITS
from semantic_terraform_agent.models import (
    ContextSelection,
    DiagnosisRequest,
    FailureInfo,
    ModelDiagnosis,
    RepairRequest,
    SchemaRecord,
    VerificationAttempt,
    VerificationCommand,
    VerificationCommands,
)
from semantic_terraform_agent.reasoning.prompts import build_repair_prompt


def test_repair_prompt_contains_only_bounded_failed_command_evidence() -> None:
    original = DiagnosisRequest(
        failure=FailureInfo(
            summary="Invalid widget mode",
            detail="mode is rejected",
            original_log="Error: invalid widget mode",
        ),
        resources=[],
        relevant_sources={"infrastructure/main.tf": "resource \"acme_widget\" \"main\" {}"},
        git_diff="+++ b/infrastructure/main.tf",
        context=ContextSelection(
            requested_mode="schema-aware",
            selected_mode="schema-aware",
            selection_reason="ambiguous provider validation",
        ),
        schemas=[
            SchemaRecord(
                resource_type="acme_widget",
                provider_source="registry.terraform.io/acme/acme",
                extraction_status="ok",
                schema={"version": 1, "block": {}},
            )
        ],
    )
    diagnosis = ModelDiagnosis(
        root_cause="The mode is incompatible.",
        affected_resources=["acme_widget.main"],
        violated_constraint="mode must be supported",
        suggested_patch="--- a/infrastructure/main.tf\n+++ b/infrastructure/main.tf",
        confidence=0.8,
        evidence=[{"source": "terraform_error", "detail": "mode rejected"}],
    )
    commands = VerificationCommands(
        fmt=VerificationCommand(
            command=["terraform", "fmt"],
            status="passed",
            stdout="UNRELATED_FMT_OUTPUT",
        ),
        plan=VerificationCommand(
            command=["terraform", "plan"],
            status="failed",
            exit_code=1,
            stderr="token=top-secret " + "x" * 20_000,
        ),
    )
    failed_attempt = VerificationAttempt(
        attempt=1,
        patch=diagnosis.suggested_patch,
        status="failed",
        failed_stage="plan",
        commands=commands,
        temporary_copy_cleaned=True,
    )
    prompt = build_repair_prompt(
        RepairRequest(
            original=original,
            previous_diagnosis=diagnosis,
            failed_attempt=failed_attempt,
        )
    )
    assert "previous candidate patch did not pass Terraform verification" in prompt
    assert "hunk" in prompt and "old/new line counts must exactly" in prompt
    assert "exact repository-relative paths" in prompt
    assert "The mode is incompatible" in prompt
    assert '"failed_verification_stage": "plan"' in prompt
    assert "registry.terraform.io/acme/acme" in prompt
    assert "top-secret" not in prompt
    assert "[REDACTED]" in prompt
    assert "UNRELATED_FMT_OUTPUT" not in prompt
    assert prompt.count("x") <= DEFAULT_LIMITS.max_verification_output_chars
