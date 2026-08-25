from __future__ import annotations

from semantic_terraform_agent.ci import (
    COMMENT_MARKER,
    MAX_PATCH_CHARS,
    CIRenderContext,
    render_pr_comment,
    render_step_summary,
)
from semantic_terraform_agent.models import ResultDocument, VerificationAssessment


def result_document(
    *,
    verification_status: str = "verified_after_retry",
    passed: bool = True,
    patch: str = "--- a/main.tf\n+++ b/main.tf\n@@ -1 +1 @@\n-old\n+new\n",
    root_cause: str = "The volume uses throughput with gp2.",
) -> ResultDocument:
    command_status = "passed" if passed else "failed"
    command = {"command": ["terraform"], "status": command_status, "exit_code": 0 if passed else 1}
    return ResultDocument.model_validate(
        {
            "status": "ok",
            "repository": {
                "root": "/workspace/repository",
                "terraform_dir": "infrastructure",
                "terraform_files": ["infrastructure/main.tf"],
                "changed_terraform_files": ["infrastructure/main.tf"],
                "diff_source": "/tmp/change.diff",
                "diff_comparison": "supplied diff file",
            },
            "terraform": {
                "version": "1.15.7",
                "schema_extraction_status": "ok",
                "schemas": [],
            },
            "failure": {
                "summary": "Invalid volume configuration",
                "detail": "throughput cannot be used with gp2",
                "stage": "plan",
                "original_log": "bounded failure",
            },
            "context": {
                "requested_mode": "auto",
                "selected_mode": "schema-aware",
                "selection_reason": "provider constraint",
            },
            "diagnosis": {
                "initial": {
                    "root_cause": root_cause,
                    "affected_resources": ["aws_ebs_volume.example"],
                    "violated_constraint": "Remove throughput or use gp3.",
                    "suggested_patch": patch,
                    "model_confidence": 0.94,
                    "evidence": [],
                },
                "repair": {
                    "root_cause": root_cause,
                    "affected_resources": ["aws_ebs_volume.example"],
                    "violated_constraint": "Remove throughput or use gp3.",
                    "suggested_patch": patch,
                    "model_confidence": 0.94,
                    "evidence": [],
                },
                "attempts": [
                    {
                        "attempt": 1,
                        "patch": patch,
                        "status": "failed",
                        "failed_stage": "plan",
                        "changed_files": ["infrastructure/main.tf"],
                        "commands": {"plan": {**command, "status": "failed", "exit_code": 1}},
                        "temporary_copy_cleaned": True,
                    },
                    {
                        "attempt": 2,
                        "patch": patch,
                        "status": "verified" if passed else "failed",
                        "failed_stage": None if passed else "plan",
                        "changed_files": ["infrastructure/main.tf"],
                        "commands": {
                            "patch_check": command,
                            "patch_apply": command,
                            "fmt": command,
                            "init": command,
                            "validate": command,
                            "plan": command,
                        },
                        "temporary_copy_cleaned": True,
                    },
                ],
                "final_patch": patch,
                "verification_status": verification_status,
                "model_confidence": 0.94,
                "evidence_score": 0.75,
                "verification": {
                    "passed": passed,
                    "status": verification_status,
                    "failed_stage": None if passed else "plan",
                },
            },
            "timing": {"total_seconds": 12.5},
            "token_usage": {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150},
        }
    )


def context() -> CIRenderContext:
    return CIRenderContext(
        repository="octo-org/service",
        commit="abc123",
        terraform_dir="infrastructure",
        failed_stage="plan",
        diff_comparison="pull request base..head",
    )


def test_pr_comment_contains_marker_and_verified_status() -> None:
    comment = render_pr_comment(result_document(), context())
    assert comment.startswith(COMMENT_MARKER)
    assert "aws_ebs_volume.example" in comment
    assert "VERIFIED AFTER RETRY" in comment
    assert "✅ terraform plan: passed" in comment
    assert "Human review is still required" in comment
    assert "<details>" in comment


def test_large_patch_is_truncated_in_pr_comment() -> None:
    patch = "--- a/main.tf\n+++ b/main.tf\n" + "+x\n" * (MAX_PATCH_CHARS + 100)
    comment = render_pr_comment(result_document(patch=patch), context())
    assert "suggested patch truncated" in comment
    assert len(comment) < MAX_PATCH_CHARS + 10_000


def test_failed_verification_status_is_rendered_without_success_claim() -> None:
    comment = render_pr_comment(
        result_document(verification_status="verification_failed", passed=False),
        context(),
    )
    assert "VERIFICATION FAILED" in comment
    assert "Terraform verification did not pass." in comment
    assert "Terraform verification passed." not in comment


def test_locally_validated_comment_is_successful_but_does_not_claim_plan_passed() -> None:
    result = result_document(
        verification_status="locally_validated_first_attempt", passed=True
    )
    attempt = result.diagnosis.attempts[-1]
    attempt.status = "locally_validated"
    attempt.verification_mode = "local"
    attempt.plan_requested = False
    attempt.plan_skip_reason = "cloud_verification_not_configured"
    attempt.commands.plan.status = "skipped"
    result.verification_assessment = VerificationAssessment.model_validate({
        "outcome": "locally_validated",
        "verification_mode": "local",
        "plan_requested": False,
        "patch_check_passed": True,
        "patch_apply_passed": True,
        "fmt_passed": True,
        "init_passed": True,
        "validate_passed": True,
        "plan_attempted": False,
        "plan_passed": None,
        "plan_skip_reason": "cloud_verification_not_configured",
        "full_verification_passed": False,
        "apply_safety": "conditionally_eligible",
    })

    comment = render_pr_comment(result, context())

    assert "LOCALLY VALIDATED" in comment
    assert "✅ patch check: passed" in comment
    assert "✅ patch apply: passed" in comment
    assert "⏭️ terraform plan: not requested" in comment
    assert "cloud verification is not configured" in comment
    assert "Conditionally eligible after explicit human approval" in comment
    assert "Terraform verification passed." not in comment


def test_comment_and_summary_redact_secret_shaped_values() -> None:
    result = result_document(
        root_cause="api_key=gemini-secret-value token=github-secret-value",
        patch="--- a/main.tf\n+++ b/main.tf\n-password=patch-secret\n+password=new-secret\n",
    )
    comment = render_pr_comment(result, context())
    summary = render_step_summary(result, context())
    assert "gemini-secret-value" not in comment
    assert "github-secret-value" not in comment
    assert "patch-secret" not in comment
    assert "new-secret" not in comment
    assert "gemini-secret-value" not in summary
    assert "[REDACTED]" in comment


def test_direct_push_summary_contains_required_metadata_without_patch() -> None:
    result = result_document()
    summary = render_step_summary(result, context())
    assert summary.startswith("# Semantic Terraform Failure Agent")
    assert "octo-org/service" in summary
    assert "abc123" in summary
    assert "infrastructure" in summary
    assert "plan" in summary
    assert "aws_ebs_volume.example" in summary
    assert "schema-aware" in summary
    assert "verified_after_retry" in summary
    assert "Input tokens: `100`" in summary
    assert result.diagnosis.final_patch not in summary
    assert "No source files were changed" in summary
