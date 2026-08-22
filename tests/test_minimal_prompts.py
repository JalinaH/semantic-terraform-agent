from __future__ import annotations

from semantic_terraform_agent.models import (
    ChangedLineContext,
    ContextFailure,
    ContextManifest,
    ContextOptimization,
    ContextSelection,
    ContextSourceBlock,
    DiagnosisContext,
    DiagnosisRequest,
    FailureInfo,
    LLMCallType,
    LLMInvocation,
    LLMProviderName,
    ModelDiagnosis,
    RepairRequest,
    SchemaRecord,
    VerificationAttempt,
    VerificationCommand,
    VerificationCommands,
)
from semantic_terraform_agent.reasoning.prompts import (
    build_prompt_parts,
    build_repair_prompt_parts,
)
from semantic_terraform_agent.reasoning.usage import build_context_telemetry


RESOURCE_SOURCE = '''resource "example_widget" "main" {
  mode = var.mode
}'''
VARIABLE_SOURCE = '''variable "mode" {
  default = "unsafe"
}'''
DIFF_SOURCE = '''--- a/infra/main.tf
+++ b/infra/main.tf
@@ -1,3 +1,3 @@
 resource "example_widget" "main" {
-  mode = "safe"
+  mode = var.mode
 }'''


def _request(*, schema_aware: bool = False, broad_legacy: bool = False):
    mode = "schema-aware" if schema_aware else "lightweight"
    failure = FailureInfo(
        summary="Unique diagnostic summary",
        detail="mode must be safe",
        stage="plan",
        resource_address="example_widget.main",
        referenced_file="main.tf",
        referenced_line=2,
        original_log="HUGE_ORIGINAL_LOG\n" * 10_000,
    )
    resource = ContextSourceBlock(
        kind="resource",
        identifier="example_widget.main",
        file="infra/main.tf",
        start_line=1,
        end_line=3,
        source=RESOURCE_SOURCE,
    )
    supporting = ContextSourceBlock(
        kind="variable",
        identifier="var.mode",
        file="infra/variables.tf",
        start_line=1,
        end_line=3,
        source=VARIABLE_SOURCE,
    )
    context = DiagnosisContext(
        failure=ContextFailure(
            summary=failure.summary,
            detail=failure.detail,
            stage=failure.stage,
            resource_address=failure.resource_address,
            referenced_file=failure.referenced_file,
            referenced_line=failure.referenced_line,
        ),
        changed_lines=[
            ChangedLineContext(
                file="infra/main.tf",
                old_start=1,
                new_start=1,
                added_lines=["  mode = var.mode"],
                removed_lines=['  mode = "safe"'],
                context_lines=[
                    'resource "example_widget" "main" {',
                    "}",
                ],
                rendered=DIFF_SOURCE,
            )
        ],
        resource_blocks=[resource],
        supporting_blocks=[supporting],
        referenced_symbols=["var.mode"],
        resolved_symbols=["var.mode"],
        metadata={
            "mode": mode,
            "strategy": "deterministic_minimal_v1",
            "ambiguous": False,
            "reference_depth": 1,
        },
        manifest=ContextManifest(
            included_files=["infra/main.tf", "infra/variables.tf"],
            included_resources=["example_widget.main"],
            included_symbols=["var.mode"],
            referenced_symbols=["var.mode"],
            resolved_symbols=["var.mode"],
            changed_lines=2,
        ),
        optimization=ContextOptimization(
            available_source_characters=10_000,
            selected_source_characters=len(RESOURCE_SOURCE) + len(VARIABLE_SOURCE),
            characters_avoided=(
                10_000 - len(RESOURCE_SOURCE) - len(VARIABLE_SOURCE)
            ),
            reduction_ratio=0.99,
            character_reduction_ratio=0.99,
            available_source_file_count=8,
            selected_source_file_count=2,
            available_resource_count=12,
            selected_resource_count=1,
        ),
        selected_context_characters=(
            len(failure.summary)
            + len(failure.detail)
            + len(RESOURCE_SOURCE)
            + len(VARIABLE_SOURCE)
            + len(DIFF_SOURCE)
        ),
    )
    schemas = (
        [
            SchemaRecord(
                resource_type="example_widget",
                provider_source="registry.terraform.io/example/example",
                extraction_status="ok",
                schema={"version": 1, "block": {"attributes": {"mode": {}}}},
            )
        ]
        if schema_aware
        else []
    )
    relevant_sources = {
        "infra/main.tf": RESOURCE_SOURCE,
        "infra/variables.tf": VARIABLE_SOURCE,
    }
    if broad_legacy:
        relevant_sources["infra/unrelated.tf"] = (
            'resource "unrelated_widget" "many" {}\n' * 500
        )
    return DiagnosisRequest(
        failure=failure,
        resources=[],
        relevant_sources=relevant_sources,
        git_diff=DIFF_SOURCE,
        context=ContextSelection(
            requested_mode=mode,
            selected_mode=mode,
            selection_reason="test",
        ),
        schemas=schemas,
        terraform_version="1.9.0",
        diagnosis_context=context,
    )


def _diagnosis() -> ModelDiagnosis:
    return ModelDiagnosis(
        root_cause="The mode violates the provider constraint.",
        affected_resources=["example_widget.main"],
        violated_constraint="mode must be safe",
        suggested_patch=(
            "--- a/infra/main.tf\n+++ b/infra/main.tf\n"
            "@@ -2 +2 @@\n-  mode = var.mode\n+  mode = \"safe\""
        ),
        confidence=0.8,
        evidence=[
            {"source": "terraform_error", "detail": "mode must be safe"}
        ],
    )


def test_minimal_prompt_has_exact_deduplicated_sections() -> None:
    prompt = build_prompt_parts(_request())
    assert prompt.user.count("Unique diagnostic summary") == 1
    assert prompt.user.count(RESOURCE_SOURCE) == 1
    assert prompt.user.count("HUGE_ORIGINAL_LOG") == 0
    assert prompt.section_characters["terraform_error"] > 0
    assert prompt.section_characters["git_diff"] > 0
    assert prompt.section_characters["terraform_source"] > 0
    assert prompt.section_characters["supporting_context"] > 0
    assert prompt.section_characters["provider_schema"] == 0
    assert sum(prompt.section_characters.values()) <= len(prompt.user)


def test_lightweight_minimal_prompt_is_smaller_than_legacy_broad_fixture() -> None:
    minimal_request = _request(broad_legacy=True)
    legacy_request = minimal_request.model_copy(update={"diagnosis_context": None})
    minimal = build_prompt_parts(minimal_request)
    legacy = build_prompt_parts(legacy_request)
    assert minimal.prompt_characters < legacy.prompt_characters
    assert "unrelated_widget" not in minimal.user
    assert "unrelated_widget" in legacy.user


def test_schema_aware_prompt_keeps_existing_resource_schema() -> None:
    prompt = build_prompt_parts(_request(schema_aware=True))
    assert prompt.section_characters["provider_schema"] > 0
    assert "registry.terraform.io/example/example" in prompt.user
    assert '"mode":{}' in prompt.user


def test_repair_prompt_reuses_minimal_context_without_original_prompt() -> None:
    request = _request(schema_aware=True)
    diagnosis = _diagnosis()
    failed = VerificationCommand(
        command=["terraform", "plan"],
        status="failed",
        exit_code=1,
        stderr="token=super-secret " + "x" * 20_000,
    )
    attempt = VerificationAttempt(
        attempt=1,
        patch=diagnosis.suggested_patch,
        status="failed",
        failed_stage="plan",
        commands=VerificationCommands(plan=failed),
        temporary_copy_cleaned=True,
    )
    repair = build_repair_prompt_parts(
        RepairRequest(
            original=request,
            previous_diagnosis=diagnosis,
            failed_attempt=attempt,
        )
    )
    assert repair.user.count(RESOURCE_SOURCE) == 1
    assert repair.user.count(diagnosis.suggested_patch) == 1
    assert "HUGE_ORIGINAL_LOG" not in repair.user
    assert "super-secret" not in repair.user
    assert "[REDACTED]" in repair.user
    assert "terraform plan" in repair.user
    assert repair.section_characters["verification_evidence"] > 0
    assert repair.section_characters["provider_schema"] > 0


def test_prompt_section_telemetry_tracks_diagnosis_and_repair_separately() -> None:
    request = _request()
    diagnosis_prompt = build_prompt_parts(request)
    diagnosis = _diagnosis()
    attempt = VerificationAttempt(
        attempt=1,
        patch=diagnosis.suggested_patch,
        status="failed",
        failed_stage="plan",
        commands=VerificationCommands(
            plan=VerificationCommand(
                command=["terraform", "plan"],
                status="failed",
                exit_code=1,
                stderr="plan failed",
            )
        ),
        temporary_copy_cleaned=True,
    )
    repair_prompt = build_repair_prompt_parts(
        RepairRequest(
            original=request,
            previous_diagnosis=diagnosis,
            failed_attempt=attempt,
        )
    )
    diagnosis_call = LLMInvocation(
        provider=LLMProviderName.OPENROUTER,
        requested_model="example/model:free",
        latency_ms=10,
        call_type=LLMCallType.DIAGNOSIS,
        prompt_characters=diagnosis_prompt.prompt_characters,
        system_prompt_characters=len(diagnosis_prompt.system),
        user_prompt_characters=len(diagnosis_prompt.user),
    )
    repair_call = LLMInvocation(
        provider=LLMProviderName.OPENROUTER,
        requested_model="example/model:free",
        latency_ms=11,
        call_type=LLMCallType.REPAIR,
        prompt_characters=repair_prompt.prompt_characters,
        system_prompt_characters=len(repair_prompt.system),
        user_prompt_characters=len(repair_prompt.user),
    )
    telemetry = build_context_telemetry(
        request,
        [
            (diagnosis_call, diagnosis_prompt),
            (repair_call, repair_prompt),
        ],
    )
    assert telemetry.prompt_characters == diagnosis_prompt.prompt_characters
    assert telemetry.rendered_user_prompt_characters == len(diagnosis_prompt.user)
    assert telemetry.source_file_count == 2
    assert telemetry.source_block_count == 2
    assert telemetry.changed_line_count == 2
    assert telemetry.referenced_symbol_count == 1
    assert telemetry.sections["terraform_error"].characters == (
        diagnosis_prompt.section_characters["terraform_error"]
    )
    assert [call.call_type for call in telemetry.calls] == [
        LLMCallType.DIAGNOSIS,
        LLMCallType.REPAIR,
    ]
    assert telemetry.calls[1].sections["verification_evidence"].characters > 0
