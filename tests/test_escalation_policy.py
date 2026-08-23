from __future__ import annotations

from semantic_terraform_agent.context.escalation import (
    ContextEscalationPolicy,
    classify_verification_error,
)
from semantic_terraform_agent.models import (
    ContextFailure,
    ContextManifest,
    ContextOptimization,
    ContextSourceBlock,
    DiagnosisContext,
    FailureInfo,
    ModelDiagnosis,
    PatchFailureCategory,
    VerificationAttempt,
    VerificationCommand,
    VerificationCommands,
    VerificationErrorRelation,
)


def _failure() -> FailureInfo:
    return FailureInfo(
        summary="Invalid mode combination",
        detail='Argument "mode" must be one of "safe" or "slow".',
        stage="plan",
        resource_address="example_widget.primary",
        referenced_file="main.tf",
        referenced_line=2,
        original_log="original",
    )


def _diagnosis() -> ModelDiagnosis:
    return ModelDiagnosis(
        root_cause="mode is incompatible",
        affected_resources=["example_widget.primary"],
        violated_constraint="mode must be safe",
        suggested_patch="--- a/main.tf\n+++ b/main.tf\n@@ -1 +1 @@\n-a\n+b",
        confidence=0.7,
        evidence=[{"source": "terraform_error", "detail": "mode invalid"}],
    )


def _context(*, ambiguous: bool = False, unresolved: bool = False) -> DiagnosisContext:
    source = '''resource "example_widget" "primary" {
  mode = module.settings.mode
}'''
    return DiagnosisContext(
        failure=ContextFailure(
            summary="Invalid mode combination",
            detail='Argument "mode" must be one of "safe" or "slow".',
            stage="plan",
            resource_address="example_widget.primary",
        ),
        resource_blocks=[
            ContextSourceBlock(
                kind="resource",
                identifier="example_widget.primary",
                file="main.tf",
                start_line=1,
                end_line=3,
                source=source,
            )
        ],
        unresolved_symbols=["module.settings.mode"] if unresolved else [],
        manifest=ContextManifest(
            included_files=["main.tf"],
            included_resources=(
                ["example_widget.primary", "example_widget.secondary"]
                if ambiguous
                else ["example_widget.primary"]
            ),
            unresolved_symbols=["module.settings.mode"] if unresolved else [],
            ambiguous=ambiguous,
        ),
        optimization=ContextOptimization(
            available_source_characters=len(source),
            selected_source_characters=len(source),
            characters_avoided=0,
            reduction_ratio=0,
            character_reduction_ratio=0,
        ),
        selected_context_characters=len(source),
    )


def _attempt(
    *,
    status: str = "failed",
    stage: str | None = "plan",
    output: str = "",
    failure_category: PatchFailureCategory | None = None,
    failure_reason_code: str | None = None,
) -> VerificationAttempt:
    command = VerificationCommand(
        command=["terraform", stage or "plan"],
        status="failed" if status == "failed" else "error",
        exit_code=1,
        stderr=output,
    )
    commands = VerificationCommands()
    if stage:
        setattr(
            commands,
            "terraform_validate" if stage == "validate" else stage,
            command,
        )
    return VerificationAttempt(
        attempt=1,
        patch=_diagnosis().suggested_patch,
        status=status,
        failed_stage=stage,
        commands=commands,
        temporary_copy_cleaned=True,
        warnings=[] if status == "verified" else ["verification failed"],
        failure_category=failure_category,
        failure_reason_code=failure_reason_code,
    )


def _decide(
    attempt: VerificationAttempt,
    *,
    mode: str = "auto",
    context: DiagnosisContext | None = None,
    schema_eligible: bool = True,
    enabled: bool = True,
):
    return ContextEscalationPolicy().decide(
        requested_mode=mode,
        failure=_failure(),
        diagnosis_context=context or _context(),
        initial_diagnosis=_diagnosis(),
        verification=attempt,
        schema_eligible=schema_eligible,
        second_attempt_enabled=enabled,
    )


def test_same_original_semantic_plan_failure_escalates() -> None:
    attempt = _attempt(
        output='''Error: Invalid mode combination
with example_widget.primary,
Argument "mode" must be one of "safe" or "slow".'''
    )
    decision = _decide(attempt)

    assert classify_verification_error(_failure(), attempt) is (
        VerificationErrorRelation.SAME_FAILURE
    )
    assert decision.action == "escalate"
    assert decision.reason_code == "provider_constraint_unresolved"
    assert decision.to_level.value == "schema"


def test_new_provider_semantic_plan_failure_escalates() -> None:
    attempt = _attempt(output='Error: Unsupported argument\nArgument "size" is unsupported.')
    decision = _decide(attempt)

    assert decision.verification_error_relation is (
        VerificationErrorRelation.NEW_SEMANTIC_FAILURE
    )
    assert decision.reason_code == "verification_semantic_failure"
    assert decision.should_escalate is True


def test_new_syntax_and_fmt_failures_repair_without_schema() -> None:
    syntax = _decide(_attempt(output="Error: Invalid expression syntax"))
    formatting = _decide(_attempt(stage="fmt", output="terraform fmt -check failed"))

    assert syntax.action == "repair"
    assert syntax.reason_code == "syntactic_patch_failure"
    assert formatting.action == "repair"
    assert formatting.reason_code == "formatting_failure"


def test_credentials_and_network_failures_stop_without_second_call() -> None:
    credentials = _decide(
        _attempt(status="unavailable", output="No valid credential sources found")
    )
    network = _decide(
        _attempt(status="unavailable", output="network is unreachable")
    )

    assert credentials.action == "stop"
    assert credentials.reason_code == "credentials_unavailable"
    assert network.action == "stop"
    assert network.reason_code == "provider_network_failure"


def test_rejected_patch_and_patch_apply_failure_never_escalate() -> None:
    rejected = _decide(_attempt(status="rejected", stage="patch_check"))
    apply_failure = _decide(_attempt(stage="patch_apply"))

    assert rejected.reason_code == "unsafe_patch"
    assert rejected.action == "stop"
    assert apply_failure.reason_code == "patch_apply_failure"
    assert apply_failure.action == "stop"


def test_repairable_malformed_patch_consumes_repair_but_unsafe_patch_stops() -> None:
    malformed = _decide(
        _attempt(
            status="rejected",
            stage="patch_check",
            failure_category=PatchFailureCategory.MALFORMED_REPAIRABLE,
            failure_reason_code="concatenated_diff",
        )
    )
    unsafe = _decide(
        _attempt(
            status="rejected",
            stage="patch_check",
            failure_category=PatchFailureCategory.UNSAFE,
            failure_reason_code="unsafe_path",
        )
    )
    assert malformed.action == "repair"
    assert malformed.reason_code == "malformed_patch"
    assert unsafe.action == "stop"
    assert unsafe.reason_code == "unsafe_patch"


def test_ambiguity_and_relevant_unresolved_symbol_can_escalate_unknown_plan() -> None:
    ambiguity = _decide(_attempt(output="plan failed"), context=_context(ambiguous=True))
    unresolved = _decide(
        _attempt(output="plan failed"), context=_context(unresolved=True)
    )

    assert ambiguity.reason_code == "ambiguous_resource"
    assert ambiguity.action == "escalate"
    assert unresolved.reason_code == "unresolved_supporting_symbol"
    assert unresolved.action == "escalate"


def test_schema_ineligible_semantic_failure_uses_bounded_repair() -> None:
    decision = _decide(
        _attempt(output="Error: Invalid value for argument mode"),
        schema_eligible=False,
    )
    assert decision.action == "repair"
    assert decision.should_escalate is False


def test_explicit_modes_preserve_repair_and_do_not_escalate() -> None:
    output = "Error: Invalid value for argument mode"
    lightweight = _decide(_attempt(output=output), mode="lightweight")
    schema = _decide(_attempt(output=output), mode="schema-aware")

    assert lightweight.action == "repair"
    assert lightweight.from_level.value == "minimal"
    assert schema.action == "repair"
    assert schema.from_level.value == "schema"
    assert lightweight.should_escalate is False
    assert schema.should_escalate is False


def test_verified_and_disabled_second_attempt_stop() -> None:
    verified = _decide(_attempt(status="verified", stage=None))
    disabled = _decide(_attempt(output="Error: Invalid value for mode"), enabled=False)

    assert verified.reason_code == "verification_passed"
    assert verified.action == "stop"
    assert disabled.reason_code == "second_attempt_disabled"
    assert disabled.action == "stop"
