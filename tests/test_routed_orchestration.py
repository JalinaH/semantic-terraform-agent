from __future__ import annotations

from pathlib import Path

import pytest

from semantic_terraform_agent.config import ProviderError
from semantic_terraform_agent.models import (
    ModelDefinition,
    ModelDiagnosis,
    ModelTier,
    PatchFailureCategory,
    ProviderResponse,
    ProviderFailureCategory,
    SchemaRecord,
    TerraformInfo,
    TokenUsage,
    VerificationAttempt,
    VerificationCommand,
    VerificationCommands,
)
from semantic_terraform_agent.orchestration.diagnose import diagnose_repository
from semantic_terraform_agent.reasoning.model_registry import ModelRegistry


INITIAL_PATCH = (
    "--- a/infrastructure/main.tf\n+++ b/infrastructure/main.tf\n"
    '@@ -2 +2 @@\n-  mode = "fast"\n+  mode = "safe"'
)
SECOND_PATCH = (
    "--- a/infrastructure/main.tf\n+++ b/infrastructure/main.tf\n"
    '@@ -2 +2 @@\n-  mode = "fast"\n+  mode = "slow"'
)


def _diagnosis(patch: str, *, schema: bool = False) -> ModelDiagnosis:
    evidence = [
        {"source": "terraform_error", "detail": "mode is invalid"},
        {"source": "terraform_source", "detail": "resource sets mode"},
        {"source": "git_diff", "detail": "mode changed"},
    ]
    if schema:
        evidence.append({"source": "provider_schema", "detail": "mode is required"})
    return ModelDiagnosis(
        root_cause="The mode violates a provider constraint.",
        affected_resources=["example_widget.primary"],
        violated_constraint="mode must be safe or slow",
        suggested_patch=patch,
        confidence=0.8,
        evidence=evidence,
    )


class RoutedProvider:
    def __init__(self, model: str, events: list[str]) -> None:
        self.model = model
        self.events = events

    def diagnose(self, request):
        self.events.append(f"diagnose:{self.model}")
        return ProviderResponse(
            diagnosis=_diagnosis(INITIAL_PATCH),
            token_usage=TokenUsage(input_tokens=100, output_tokens=20, total_tokens=120),
        )

    def repair(self, request):
        self.events.append(f"second:{self.model}")
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
            token_usage=TokenUsage(input_tokens=180, output_tokens=25, total_tokens=205),
        )


class RoutedFactory:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.created: list[str] = []

    def __call__(self, provider, model):
        self.created.append(model)
        return RoutedProvider(model, self.events)


def _registry() -> ModelRegistry:
    return ModelRegistry(
        [
            ModelDefinition(
                provider="openrouter",
                model_id="test/free-a:free",
                tier="free",
                priority=10,
                supports_structured_output=True,
            ),
            ModelDefinition(
                provider="openrouter",
                model_id="test/economy-a",
                tier="economy",
                priority=10,
                supports_structured_output=True,
            ),
        ]
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
                            "mode": {"type": "string", "required": True}
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
    factory: RoutedFactory,
    verifier,
    *,
    ceiling: str = "economy",
):
    return diagnose_repository(
        repo_path=terraform_repo,
        terraform_dir=Path("infrastructure"),
        log_file=failure_log,
        diff_file=diff_file,
        provider_name="openrouter",
        model=None,
        context_mode="auto",
        model_routing="auto",
        max_model_tier=ceiling,
        model_registry=_registry(),
        provider_factory=factory,
        patch_verifier=verifier,
    )


def test_auto_routing_first_pass_success_uses_cheapest_model_only(
    terraform_repo: Path,
    failure_log: Path,
    diff_file: Path,
    monkeypatch,
) -> None:
    events: list[str] = []
    factory = RoutedFactory(events)
    monkeypatch.setattr(
        "semantic_terraform_agent.orchestration.diagnose.inspect_schemas",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("first-pass success must not retrieve schema")
        ),
    )

    def verifier(patch, layout, *, attempt):
        events.append(f"verify:{attempt}")
        return _attempt(patch, attempt, status="verified")

    result = _run(terraform_repo, failure_log, diff_file, factory, verifier)

    assert events == ["diagnose:test/free-a:free", "verify:1"]
    assert factory.created == ["test/free-a:free"]
    assert result.model_progression.initial_tier is ModelTier.FREE
    assert result.model_progression.final_model == "test/free-a:free"
    assert result.model_progression.model_escalated is False
    assert result.context_progression.levels_used == ["minimal"]
    assert result.llm_calls[0].routing_reason == "initial_cheapest_eligible"
    assert result.llm_calls[0].call_number == 1


def test_context_escalation_independently_routes_to_next_tier(
    terraform_repo: Path,
    failure_log: Path,
    diff_file: Path,
    monkeypatch,
) -> None:
    events: list[str] = []
    factory = RoutedFactory(events)

    def inspect(layout, resource_types, *, enabled):
        events.append("schema")
        return _schema_info(), []

    monkeypatch.setattr(
        "semantic_terraform_agent.orchestration.diagnose.inspect_schemas", inspect
    )

    def verifier(patch, layout, *, attempt):
        events.append(f"verify:{attempt}")
        if attempt == 1:
            return _attempt(
                patch,
                attempt,
                status="failed",
                stage="plan",
                output='Error: Invalid value for argument "mode"',
            )
        return _attempt(patch, attempt, status="verified")

    result = _run(terraform_repo, failure_log, diff_file, factory, verifier)

    assert events == [
        "diagnose:test/free-a:free",
        "verify:1",
        "schema",
        "second:test/economy-a",
        "verify:2",
    ]
    assert result.context_progression.levels_used == ["minimal", "schema"]
    assert result.model_progression.models_used == [
        "test/free-a:free",
        "test/economy-a",
    ]
    assert result.model_progression.model_escalated is True
    assert result.model_progression.tier_escalated is True
    assert result.model_progression.decisions[1].reason_code == (
        "context_escalation_next_tier"
    )
    assert result.context_progression.same_model is False
    assert result.llm_calls[1].routing_tier is ModelTier.ECONOMY
    assert result.llm_calls[1].call_number == 2
    assert result.llm_usage.call_count == 2
    assert result.timing["model_routing_seconds"] >= 0
    assert len(result.llm_calls) == 2


def test_malformed_patch_repair_stays_on_initial_free_model_and_tier(
    terraform_repo: Path,
    failure_log: Path,
    diff_file: Path,
    monkeypatch,
) -> None:
    events: list[str] = []
    factory = RoutedFactory(events)
    monkeypatch.setattr(
        "semantic_terraform_agent.orchestration.diagnose.inspect_schemas",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("malformed patch repair must not retrieve schema")
        ),
    )

    def verifier(patch, layout, *, attempt):
        events.append(f"verify:{attempt}")
        if attempt == 1:
            return _attempt(
                patch, attempt, status="rejected", stage="patch_check"
            ).model_copy(
                update={
                    "failure_category": PatchFailureCategory.MALFORMED_REPAIRABLE,
                    "failure_reason_code": "concatenated_diff",
                    "failure_description": "headers were concatenated",
                }
            )
        return _attempt(patch, attempt, status="verified")

    result = _run(terraform_repo, failure_log, diff_file, factory, verifier)

    assert factory.created == ["test/free-a:free", "test/free-a:free"]
    assert result.model_progression.models_used == [
        "test/free-a:free",
        "test/free-a:free",
    ]
    assert result.model_progression.decisions[1].reason_code == "repair_same_model"
    assert result.model_progression.model_escalated is False
    assert result.model_progression.initial_tier is ModelTier.FREE
    assert result.model_progression.final_tier is ModelTier.FREE
    assert result.llm_calls[1].repair_reason == "malformed_patch_to_structured_edit"
    assert result.context_progression.levels_used == ["minimal"]


def test_free_only_context_escalation_never_selects_higher_tier(
    terraform_repo: Path,
    failure_log: Path,
    diff_file: Path,
    monkeypatch,
) -> None:
    events: list[str] = []
    factory = RoutedFactory(events)
    monkeypatch.setattr(
        "semantic_terraform_agent.orchestration.diagnose.inspect_schemas",
        lambda *args, **kwargs: (_schema_info(), []),
    )

    def verifier(patch, layout, *, attempt):
        if attempt == 1:
            return _attempt(
                patch,
                attempt,
                status="failed",
                stage="plan",
                output='Error: Invalid value for argument "mode"',
            )
        return _attempt(patch, attempt, status="verified")

    result = _run(
        terraform_repo,
        failure_log,
        diff_file,
        factory,
        verifier,
        ceiling="free",
    )

    assert factory.created == ["test/free-a:free", "test/free-a:free"]
    assert result.model_progression.final_tier is ModelTier.FREE
    assert result.model_progression.decisions[1].reason_code == "tier_ceiling_reuse"
    assert all(call.routing_tier is ModelTier.FREE for call in result.llm_calls)


def test_unavailable_initial_model_does_not_create_model_hopping_loop(
    terraform_repo: Path,
    failure_log: Path,
    diff_file: Path,
) -> None:
    created: list[str] = []

    class UnavailableProvider:
        def diagnose(self, request):
            raise ProviderError(
                "selected model unavailable",
                category=ProviderFailureCategory.MODEL_UNAVAILABLE,
            )

        def repair(self, request):
            raise AssertionError("no semantic fallback call is allowed")

    def factory(provider, model):
        created.append(model)
        return UnavailableProvider()

    with pytest.raises(ProviderError) as exc:
        diagnose_repository(
            repo_path=terraform_repo,
            terraform_dir=Path("infrastructure"),
            log_file=failure_log,
            diff_file=diff_file,
            provider_name="openrouter",
            model=None,
            context_mode="auto",
            model_routing="auto",
            max_model_tier="economy",
            model_registry=_registry(),
            provider_factory=factory,
        )

    assert exc.value.category is ProviderFailureCategory.MODEL_UNAVAILABLE
    assert created == ["test/free-a:free"]
