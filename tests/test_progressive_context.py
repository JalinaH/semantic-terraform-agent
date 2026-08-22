from __future__ import annotations

from pathlib import Path

from semantic_terraform_agent.models import (
    ModelDiagnosis,
    ProviderResponse,
    SchemaRecord,
    TerraformInfo,
    TokenUsage,
    VerificationAttempt,
    VerificationCommand,
    VerificationCommands,
)
from semantic_terraform_agent.orchestration.diagnose import diagnose_repository
from semantic_terraform_agent.reasoning.prompts import (
    build_prompt_parts,
    build_repair_prompt_parts,
)


INITIAL_PATCH = (
    "--- a/infrastructure/main.tf\n+++ b/infrastructure/main.tf\n"
    "@@ -2 +2 @@\n-  mode = \"fast\"\n+  mode = \"safe\""
)
SECOND_PATCH = (
    "--- a/infrastructure/main.tf\n+++ b/infrastructure/main.tf\n"
    "@@ -2 +2 @@\n-  mode = \"fast\"\n+  mode = \"slow\""
)


def _diagnosis(patch: str, *, schema: bool = False) -> ModelDiagnosis:
    evidence = [
        {"source": "terraform_error", "detail": "mode is invalid"},
        {"source": "terraform_source", "detail": "resource sets mode"},
        {"source": "git_diff", "detail": "mode changed"},
    ]
    if schema:
        evidence.append(
            {"source": "provider_schema", "detail": "mode is required"}
        )
    return ModelDiagnosis(
        root_cause="The mode violates a provider constraint.",
        affected_resources=["example_widget.primary"],
        violated_constraint="mode must be safe or slow",
        suggested_patch=patch,
        confidence=0.8,
        evidence=evidence,
    )


class ProgressiveProvider:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.initial_request = None
        self.second_request = None
        self.diagnose_calls = 0
        self.repair_calls = 0

    def diagnose(self, request):
        self.events.append("diagnose")
        self.initial_request = request
        self.diagnose_calls += 1
        return ProviderResponse(
            diagnosis=_diagnosis(INITIAL_PATCH),
            token_usage=TokenUsage(
                input_tokens=100,
                output_tokens=20,
                total_tokens=120,
            ),
        )

    def repair(self, request):
        self.events.append("second_model")
        self.second_request = request
        self.repair_calls += 1
        return ProviderResponse(
            diagnosis=_diagnosis(
                SECOND_PATCH,
                schema=request.second_attempt_reason.value == "context_escalation",
            ),
            token_usage=TokenUsage(
                input_tokens=180,
                output_tokens=25,
                total_tokens=205,
            ),
        )


def _command(status: str, output: str = "") -> VerificationCommand:
    return VerificationCommand(
        command=["terraform", "plan"],
        status=status,
        exit_code=0 if status == "passed" else 1,
        stderr=output,
    )


def _attempt(
    patch: str,
    attempt: int,
    *,
    status: str,
    stage: str | None = None,
    output: str = "",
) -> VerificationAttempt:
    failed = _command("failed", output)
    commands = VerificationCommands(
        patch_check=failed if stage == "patch_check" else _command("passed"),
        patch_apply=failed if stage == "patch_apply" else _command("passed"),
        fmt=failed if stage == "fmt" else _command("passed"),
        init=failed if stage == "init" else _command("passed"),
        validate=failed if stage == "validate" else _command("passed"),
        plan=failed if stage == "plan" else _command("passed"),
    )
    return VerificationAttempt(
        attempt=attempt,
        patch=patch,
        status=status,
        failed_stage=stage,
        changed_files=["infrastructure/main.tf"],
        commands=commands,
        temporary_copy_cleaned=True,
        warnings=[] if status == "verified" else ["verification failed"],
    )


def _schema_info() -> TerraformInfo:
    return TerraformInfo(
        version="1.9.0",
        schema_extraction_status="ok",
        schemas=[
            SchemaRecord(
                resource_type="example_widget",
                provider_source="registry.terraform.io/example/example",
                provider_version="1.2.3",
                extraction_status="ok",
                schema={
                    "version": 1,
                    "block": {
                        "attributes": {
                            "mode": {"type": "string", "required": True},
                            "unrelated": {
                                "type": "string",
                                "optional": True,
                                "description": "FULL_SCHEMA_ONLY",
                            },
                        }
                    },
                },
            )
        ],
    )


def _run(
    terraform_repo: Path,
    failure_log: Path,
    diff_file: Path,
    provider: ProgressiveProvider,
    verifier,
    *,
    mode: str = "auto",
):
    return diagnose_repository(
        repo_path=terraform_repo,
        terraform_dir=Path("infrastructure"),
        log_file=failure_log,
        diff_file=diff_file,
        provider_name="gemini",
        model="same-model",
        context_mode=mode,
        llm_provider=provider,
        patch_verifier=verifier,
    )


def test_auto_first_pass_success_avoids_schema_and_second_call(
    terraform_repo: Path,
    failure_log: Path,
    diff_file: Path,
    monkeypatch,
) -> None:
    events: list[str] = []
    provider = ProgressiveProvider(events)

    def forbidden_schema(*args, **kwargs):
        raise AssertionError("minimal first-pass success must not retrieve schema")

    monkeypatch.setattr(
        "semantic_terraform_agent.orchestration.diagnose.inspect_schemas",
        forbidden_schema,
    )

    def verifier(patch, layout, *, attempt):
        events.append(f"verify_{attempt}")
        return _attempt(patch, attempt, status="verified")

    result = _run(terraform_repo, failure_log, diff_file, provider, verifier)

    assert events == ["diagnose", "verify_1"]
    assert provider.initial_request.context.selected_mode == "progressive"
    assert provider.initial_request.context_level.value == "minimal"
    assert provider.initial_request.schemas == []
    assert "RELEVANT PROVIDER SCHEMA" not in build_prompt_parts(
        provider.initial_request
    ).user
    assert result.context_progression.initial_level.value == "minimal"
    assert result.context_progression.final_level.value == "minimal"
    assert result.context_progression.escalated is False
    assert result.context_progression.schema_avoided is True
    assert result.context_progression.schema_retrieved is False
    assert result.context_progression.second_attempt_reason.value == "none"
    assert result.llm_usage.call_count == 1
    assert result.llm_calls[0].context_level.value == "minimal"


def test_auto_semantic_plan_failure_retrieves_schema_then_escalates_once(
    terraform_repo: Path,
    failure_log: Path,
    diff_file: Path,
    monkeypatch,
) -> None:
    events: list[str] = []
    provider = ProgressiveProvider(events)

    def inspect(layout, resource_types, *, enabled):
        events.append("schema_retrieval")
        assert enabled is True
        assert resource_types == ["example_widget"]
        return _schema_info(), []

    monkeypatch.setattr(
        "semantic_terraform_agent.orchestration.diagnose.inspect_schemas",
        inspect,
    )

    def verifier(patch, layout, *, attempt):
        events.append(f"verify_{attempt}")
        if attempt == 1:
            return _attempt(
                patch,
                attempt,
                status="failed",
                stage="plan",
                output='''Error: Invalid value for argument
with example_widget.primary,
Argument "mode" must be one of "safe" or "slow".''',
            )
        return _attempt(patch, attempt, status="verified")

    result = _run(terraform_repo, failure_log, diff_file, provider, verifier)

    assert events == [
        "diagnose",
        "verify_1",
        "schema_retrieval",
        "second_model",
        "verify_2",
    ]
    assert provider.initial_request.schemas == []
    assert provider.second_request.second_attempt_reason.value == "context_escalation"
    assert provider.second_request.original.context_level.value == "schema"
    assert provider.second_request.original.schema_slices
    repair_prompt = build_repair_prompt_parts(provider.second_request)
    assert "CONTEXT ESCALATION DECISION" in repair_prompt.user
    assert "RELEVANT PROVIDER SCHEMA" in repair_prompt.user
    assert '"mode":{"required":true,"type":"string"}' in repair_prompt.user
    assert "FULL_SCHEMA_ONLY" not in repair_prompt.user
    assert result.context_progression.escalated is True
    assert result.context_progression.levels_used == ["minimal", "schema"]
    assert result.context_progression.reason_code == "provider_constraint_unresolved"
    assert result.context_progression.schema_retrieval_attempted is True
    assert result.context_progression.schema_retrieved is True
    assert result.context_progression.schema_avoided is False
    assert result.context_progression.same_model is True
    assert result.diagnosis.second_attempt_reason.value == "context_escalation"
    assert result.diagnosis.verification_status == "verified_after_retry"
    assert result.llm_usage.call_count == 2
    assert result.llm_usage.initial_input_tokens == 100
    assert result.llm_usage.escalation_input_tokens == 180
    assert [call.context_level.value for call in result.llm_calls] == [
        "minimal",
        "schema",
    ]
    assert [call.context_level.value for call in result.context_telemetry.calls] == [
        "minimal",
        "schema",
    ]
    assert result.context_telemetry.calls[0].schema_characters == 0
    assert result.context_telemetry.calls[1].schema_characters > 0
    assert result.context_telemetry.calls[1].schema_path_count == 1
    assert result.timing["schema_retrieval_seconds"] >= 0
    assert result.timing["schema_slice_seconds"] >= 0
    assert result.timing["escalation_decision_seconds"] >= 0
    assert len(result.llm_calls) == 2


def test_auto_fmt_failure_repairs_with_minimal_context_and_no_schema(
    terraform_repo: Path,
    failure_log: Path,
    diff_file: Path,
    monkeypatch,
) -> None:
    events: list[str] = []
    provider = ProgressiveProvider(events)

    def forbidden_schema(*args, **kwargs):
        raise AssertionError("fmt repair must stay schema-free")

    monkeypatch.setattr(
        "semantic_terraform_agent.orchestration.diagnose.inspect_schemas",
        forbidden_schema,
    )

    def verifier(patch, layout, *, attempt):
        events.append(f"verify_{attempt}")
        if attempt == 1:
            return _attempt(
                patch,
                attempt,
                status="failed",
                stage="fmt",
                output="terraform fmt -check failed",
            )
        return _attempt(patch, attempt, status="verified")

    result = _run(terraform_repo, failure_log, diff_file, provider, verifier)

    assert events == ["diagnose", "verify_1", "second_model", "verify_2"]
    assert provider.second_request.second_attempt_reason.value == "repair"
    assert provider.second_request.original.schema_slices == []
    assert "RELEVANT PROVIDER SCHEMA" not in build_repair_prompt_parts(
        provider.second_request
    ).user
    assert result.context_progression.escalated is False
    assert result.context_progression.schema_avoided is True
    assert result.diagnosis.second_attempt_reason.value == "repair"
    assert [call.context_level.value for call in result.llm_calls] == [
        "minimal",
        "minimal",
    ]
    assert result.llm_usage.escalation_input_tokens is None


def test_environment_failure_stops_without_schema_or_second_model(
    terraform_repo: Path,
    failure_log: Path,
    diff_file: Path,
    monkeypatch,
) -> None:
    events: list[str] = []
    provider = ProgressiveProvider(events)

    def forbidden_schema(*args, **kwargs):
        raise AssertionError("environment failure must not retrieve schema")

    monkeypatch.setattr(
        "semantic_terraform_agent.orchestration.diagnose.inspect_schemas",
        forbidden_schema,
    )

    def verifier(patch, layout, *, attempt):
        events.append(f"verify_{attempt}")
        return _attempt(
            patch,
            attempt,
            status="failed",
            stage="plan",
            output="No valid credential sources found",
        )

    result = _run(terraform_repo, failure_log, diff_file, provider, verifier)

    assert events == ["diagnose", "verify_1"]
    assert provider.repair_calls == 0
    assert result.context_progression.reason_code == "credentials_unavailable"
    assert result.context_progression.schema_avoided is True
    assert result.diagnosis.verification_status == "verification_unavailable"


def test_schema_unavailable_after_semantic_signal_stops_without_unchanged_rerun(
    terraform_repo: Path,
    failure_log: Path,
    diff_file: Path,
    monkeypatch,
) -> None:
    events: list[str] = []
    provider = ProgressiveProvider(events)

    def inspect(layout, resource_types, *, enabled):
        events.append("schema_retrieval")
        return _schema_info().model_copy(
            update={"schema_extraction_status": "unavailable", "schemas": []}
        ), ["schema unavailable"]

    monkeypatch.setattr(
        "semantic_terraform_agent.orchestration.diagnose.inspect_schemas",
        inspect,
    )

    def verifier(patch, layout, *, attempt):
        events.append(f"verify_{attempt}")
        return _attempt(
            patch,
            attempt,
            status="failed",
            stage="plan",
            output="Error: Invalid value for argument mode",
        )

    result = _run(terraform_repo, failure_log, diff_file, provider, verifier)

    assert events == ["diagnose", "verify_1", "schema_retrieval"]
    assert provider.repair_calls == 0
    assert result.context_progression.reason_code == "schema_unavailable"
    assert result.context_progression.schema_retrieval_attempted is True
    assert result.context_progression.schema_retrieved is False
    assert result.context_progression.schema_avoided is False
    assert result.llm_usage.call_count == 1


def test_explicit_schema_starts_with_slice_and_explicit_lightweight_never_escalates(
    terraform_repo: Path,
    failure_log: Path,
    diff_file: Path,
    monkeypatch,
) -> None:
    schema_events: list[str] = []
    schema_provider = ProgressiveProvider(schema_events)

    def inspect(layout, resource_types, *, enabled):
        schema_events.append("schema_retrieval")
        return _schema_info(), []

    monkeypatch.setattr(
        "semantic_terraform_agent.orchestration.diagnose.inspect_schemas",
        inspect,
    )

    def verified(patch, layout, *, attempt):
        schema_events.append(f"verify_{attempt}")
        return _attempt(patch, attempt, status="verified")

    schema_result = _run(
        terraform_repo,
        failure_log,
        diff_file,
        schema_provider,
        verified,
        mode="schema-aware",
    )
    assert schema_events == ["schema_retrieval", "diagnose", "verify_1"]
    assert schema_provider.initial_request.context_level.value == "schema"
    assert schema_provider.initial_request.schema_slices
    assert schema_result.context_progression.strategy == "explicit_schema"
    assert schema_result.context_progression.escalated is False

    light_events: list[str] = []
    light_provider = ProgressiveProvider(light_events)

    def forbidden_schema(*args, **kwargs):
        raise AssertionError("explicit lightweight must not retrieve schema")

    monkeypatch.setattr(
        "semantic_terraform_agent.orchestration.diagnose.inspect_schemas",
        forbidden_schema,
    )

    def semantic_then_verified(patch, layout, *, attempt):
        light_events.append(f"verify_{attempt}")
        if attempt == 1:
            return _attempt(
                patch,
                attempt,
                status="failed",
                stage="plan",
                output="Error: Invalid value for argument mode",
            )
        return _attempt(patch, attempt, status="verified")

    light_result = _run(
        terraform_repo,
        failure_log,
        diff_file,
        light_provider,
        semantic_then_verified,
        mode="lightweight",
    )
    assert light_events == ["diagnose", "verify_1", "second_model", "verify_2"]
    assert light_provider.second_request.second_attempt_reason.value == "repair"
    assert light_result.context_progression.strategy == "explicit_lightweight"
    assert light_result.context_progression.escalated is False
    assert light_result.context_progression.schema_avoided is None


def test_progressive_second_failure_never_creates_a_third_call(
    terraform_repo: Path,
    failure_log: Path,
    diff_file: Path,
    monkeypatch,
) -> None:
    events: list[str] = []
    provider = ProgressiveProvider(events)

    monkeypatch.setattr(
        "semantic_terraform_agent.orchestration.diagnose.inspect_schemas",
        lambda *args, **kwargs: (_schema_info(), []),
    )

    def verifier(patch, layout, *, attempt):
        events.append(f"verify_{attempt}")
        return _attempt(
            patch,
            attempt,
            status="failed",
            stage="plan",
            output="Error: Invalid value for argument mode",
        )

    result = _run(terraform_repo, failure_log, diff_file, provider, verifier)

    assert provider.diagnose_calls == 1
    assert provider.repair_calls == 1
    assert len(result.llm_calls) == 2
    assert len(result.diagnosis.attempts) == 2
    assert result.diagnosis.verification_status == "verification_failed"
