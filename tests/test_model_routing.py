from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from semantic_terraform_agent.config import ModelRoutingError
from semantic_terraform_agent.models import (
    LLMProviderName,
    ModelDefinition,
    ModelRoutingMode,
    ModelTier,
    SecondAttemptReason,
)
from semantic_terraform_agent.reasoning.model_registry import ModelRegistry
from semantic_terraform_agent.reasoning.routing import ModelRoutingPolicy


def definition(
    model_id: str,
    tier: ModelTier,
    *,
    priority: int = 10,
    enabled: bool = True,
    structured: bool = True,
    fallback: bool = False,
    provider: LLMProviderName = LLMProviderName.OPENROUTER,
) -> ModelDefinition:
    return ModelDefinition(
        provider=provider,
        model_id=model_id,
        tier=tier,
        priority=priority,
        enabled=enabled,
        supports_structured_output=structured,
        supports_json_fallback=fallback,
    )


def registry() -> ModelRegistry:
    return ModelRegistry(
        [
            definition("test/free-b:free", ModelTier.FREE, priority=20),
            definition("test/free-a:free", ModelTier.FREE, priority=10),
            definition("test/economy-a", ModelTier.ECONOMY),
            definition("test/balanced-a", ModelTier.BALANCED),
            definition("test/premium-a", ModelTier.PREMIUM),
            definition(
                "test/disabled:free",
                ModelTier.FREE,
                priority=1,
                enabled=False,
            ),
            definition(
                "test/incompatible:free",
                ModelTier.FREE,
                priority=0,
                structured=False,
                fallback=False,
            ),
        ]
    )


def policy() -> ModelRoutingPolicy:
    return ModelRoutingPolicy(registry())


@pytest.mark.parametrize(
    ("ceiling", "expected"),
    [
        (ModelTier.FREE, "test/free-a:free"),
        (ModelTier.ECONOMY, "test/free-a:free"),
        (ModelTier.BALANCED, "test/free-a:free"),
        (ModelTier.PREMIUM, "test/free-a:free"),
    ],
)
def test_auto_initial_uses_cheapest_tier_then_priority(
    ceiling: ModelTier, expected: str
) -> None:
    decision = policy().select_initial(
        provider=LLMProviderName.OPENROUTER,
        routing_mode=ModelRoutingMode.AUTO,
        requested_model=None,
        max_allowed_tier=ceiling,
    )
    assert decision.selected_model == expected
    assert decision.selected_tier is ModelTier.FREE
    assert decision.reason_code == "initial_cheapest_eligible"
    assert decision.candidate_count >= 2


def test_capability_and_disabled_models_are_filtered() -> None:
    selected = policy().select_initial(
        provider=LLMProviderName.OPENROUTER,
        routing_mode=ModelRoutingMode.AUTO,
        requested_model=None,
        max_allowed_tier=ModelTier.FREE,
    )
    assert selected.selected_model == "test/free-a:free"

    fallback_only = ModelRoutingPolicy(
        ModelRegistry(
            [
                definition(
                    "test/fallback-only:free",
                    ModelTier.FREE,
                    structured=False,
                    fallback=True,
                )
            ]
        )
    ).select_initial(
        provider=LLMProviderName.OPENROUTER,
        routing_mode=ModelRoutingMode.AUTO,
        requested_model=None,
        max_allowed_tier=ModelTier.FREE,
    )
    assert fallback_only.selected_model == "test/fallback-only:free"


def test_repair_reuses_same_model_even_when_higher_tiers_exist() -> None:
    initial = policy().select_initial(
        provider=LLMProviderName.OPENROUTER,
        routing_mode=ModelRoutingMode.AUTO,
        requested_model=None,
        max_allowed_tier=ModelTier.PREMIUM,
    )
    second = policy().select_second(
        initial=initial,
        second_attempt_reason=SecondAttemptReason.REPAIR,
    )
    assert second.selected_model == initial.selected_model
    assert second.reason_code == "repair_same_model"


@pytest.mark.parametrize(
    ("ceiling", "second_model", "second_tier"),
    [
        (ModelTier.FREE, "test/free-a:free", ModelTier.FREE),
        (ModelTier.ECONOMY, "test/economy-a", ModelTier.ECONOMY),
        (ModelTier.BALANCED, "test/economy-a", ModelTier.ECONOMY),
        (ModelTier.PREMIUM, "test/economy-a", ModelTier.ECONOMY),
    ],
)
def test_context_escalation_steps_to_next_available_tier_without_exceeding_ceiling(
    ceiling: ModelTier,
    second_model: str,
    second_tier: ModelTier,
) -> None:
    routing = policy()
    initial = routing.select_initial(
        provider=LLMProviderName.OPENROUTER,
        routing_mode=ModelRoutingMode.AUTO,
        requested_model=None,
        max_allowed_tier=ceiling,
    )
    second = routing.select_second(
        initial=initial,
        second_attempt_reason=SecondAttemptReason.CONTEXT_ESCALATION,
    )
    routing.assert_allowed(second)
    assert second.selected_model == second_model
    assert second.selected_tier is second_tier


def test_no_stronger_model_reuses_current_model() -> None:
    routing = ModelRoutingPolicy(
        ModelRegistry([definition("test/only:free", ModelTier.FREE)])
    )
    initial = routing.select_initial(
        provider=LLMProviderName.OPENROUTER,
        routing_mode=ModelRoutingMode.AUTO,
        requested_model=None,
        max_allowed_tier=ModelTier.PREMIUM,
    )
    second = routing.select_second(
        initial=initial,
        second_attempt_reason=SecondAttemptReason.CONTEXT_ESCALATION,
    )
    assert second.selected_model == "test/only:free"
    assert second.reason_code == "no_stronger_model_available"


def test_fixed_mode_preserves_unregistered_dynamic_model() -> None:
    decision = policy().select_initial(
        provider=LLMProviderName.OPENROUTER,
        routing_mode=ModelRoutingMode.FIXED,
        requested_model="dynamic/model:free",
        max_allowed_tier=ModelTier.FREE,
    )
    assert decision.selected_model == "dynamic/model:free"
    assert decision.selected_tier is None
    assert decision.reason_code == "fixed_model"


def test_fixed_registered_disabled_model_fails_safely() -> None:
    with pytest.raises(ModelRoutingError) as exc:
        policy().select_initial(
            provider=LLMProviderName.OPENROUTER,
            routing_mode=ModelRoutingMode.FIXED,
            requested_model="test/disabled:free",
            max_allowed_tier=ModelTier.PREMIUM,
        )
    assert exc.value.code == "explicit_model_disabled"


def test_auto_explicit_initial_model_can_route_upward() -> None:
    routing = policy()
    initial = routing.select_initial(
        provider=LLMProviderName.OPENROUTER,
        routing_mode=ModelRoutingMode.AUTO,
        requested_model="test/free-b:free",
        max_allowed_tier=ModelTier.ECONOMY,
    )
    second = routing.select_second(
        initial=initial,
        second_attempt_reason=SecondAttemptReason.CONTEXT_ESCALATION,
    )
    assert initial.reason_code == "explicit_model"
    assert initial.selected_model == "test/free-b:free"
    assert second.selected_model == "test/economy-a"


def test_auto_explicit_model_must_be_registered_enabled_capable_and_within_tier() -> None:
    routing = policy()
    cases = (
        ("dynamic/model:free", ModelTier.PREMIUM, "explicit_model_not_registered"),
        ("test/disabled:free", ModelTier.PREMIUM, "explicit_model_disabled"),
        (
            "test/incompatible:free",
            ModelTier.PREMIUM,
            "model_capability_unsupported",
        ),
        ("test/economy-a", ModelTier.FREE, "model_tier_violation"),
    )
    for model_id, ceiling, code in cases:
        with pytest.raises(ModelRoutingError) as exc:
            routing.select_initial(
                provider=LLMProviderName.OPENROUTER,
                routing_mode=ModelRoutingMode.AUTO,
                requested_model=model_id,
                max_allowed_tier=ceiling,
            )
        assert exc.value.code == code


def test_provider_scope_never_crosses_to_gemini() -> None:
    routing = ModelRoutingPolicy(
        ModelRegistry(
            [
                definition(
                    "gemini-test",
                    ModelTier.FREE,
                    provider=LLMProviderName.GEMINI,
                ),
                definition("test/openrouter", ModelTier.ECONOMY),
            ]
        )
    )
    decision = routing.select_initial(
        provider=LLMProviderName.OPENROUTER,
        routing_mode=ModelRoutingMode.AUTO,
        requested_model=None,
        max_allowed_tier=ModelTier.PREMIUM,
    )
    assert decision.selected_provider is LLMProviderName.OPENROUTER
    assert decision.selected_model == "test/openrouter"


def test_no_eligible_model_has_machine_readable_error() -> None:
    routing = ModelRoutingPolicy(ModelRegistry([]))
    with pytest.raises(ModelRoutingError) as exc:
        routing.select_initial(
            provider=LLMProviderName.OPENROUTER,
            routing_mode=ModelRoutingMode.AUTO,
            requested_model=None,
            max_allowed_tier=ModelTier.FREE,
        )
    assert exc.value.code == "no_eligible_model"


def test_defensive_tier_assertion_rejects_policy_bypass() -> None:
    routing = policy()
    valid = routing.select_initial(
        provider=LLMProviderName.OPENROUTER,
        routing_mode=ModelRoutingMode.AUTO,
        requested_model=None,
        max_allowed_tier=ModelTier.FREE,
    )
    invalid = valid.model_copy(update={"selected_tier": ModelTier.PREMIUM})
    with pytest.raises(ModelRoutingError) as exc:
        routing.assert_allowed(invalid)
    assert exc.value.code == "model_tier_violation"


def test_registry_rejects_duplicates_invalid_fields_and_secret_fields(
    tmp_path: Path,
) -> None:
    with pytest.raises(ModelRoutingError) as duplicate:
        ModelRegistry(
            [
                definition("test/same:free", ModelTier.FREE),
                definition("test/same:free", ModelTier.FREE),
            ]
        )
    assert duplicate.value.code == "invalid_model_registry"

    invalid = tmp_path / "invalid.json"
    invalid.write_text(
        json.dumps(
            {
                "models": [
                    {
                        "provider": "openrouter",
                        "model_id": "test/a:free",
                        "tier": "free",
                        "priority": "first",
                        "enabled": True,
                        "supports_structured_output": True,
                        "supports_json_fallback": False,
                        "supports_tools": False,
                        "api_key": "must-not-be-accepted",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ModelRoutingError) as malformed:
        ModelRegistry.from_path(invalid)
    assert malformed.value.code == "invalid_model_registry"
    assert "must-not-be-accepted" not in str(malformed.value)


def test_registry_model_fields_are_strict() -> None:
    with pytest.raises(ValidationError):
        ModelDefinition(
            provider="openrouter",
            model_id="test/a:free",
            tier="free",
            priority="10",
            enabled="true",
            supports_structured_output=True,
            supports_json_fallback=False,
        )
