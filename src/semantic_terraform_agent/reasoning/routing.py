"""Deterministic cost-tier model selection independent of context selection."""

from __future__ import annotations

from semantic_terraform_agent.config import ModelRoutingError, validate_model_id
from semantic_terraform_agent.models import (
    LLMProviderName,
    ModelDefinition,
    ModelRoutingDecision,
    ModelRoutingMode,
    ModelTier,
    RoutingReasonCode,
    SecondAttemptReason,
)
from semantic_terraform_agent.reasoning.model_registry import ModelRegistry


TIER_ORDER = {
    ModelTier.FREE: 0,
    ModelTier.ECONOMY: 1,
    ModelTier.BALANCED: 2,
    ModelTier.PREMIUM: 3,
}


class ModelRoutingPolicy:
    """Filter and deterministically rank configured models within one provider."""

    def __init__(self, registry: ModelRegistry) -> None:
        self.registry = registry

    def select_initial(
        self,
        *,
        provider: LLMProviderName,
        routing_mode: ModelRoutingMode,
        requested_model: str | None,
        max_allowed_tier: ModelTier,
    ) -> ModelRoutingDecision:
        if routing_mode is ModelRoutingMode.FIXED:
            if requested_model is None:
                raise ModelRoutingError(
                    "fixed model routing requires an explicit model",
                    code="no_eligible_model",
                )
            validated = validate_model_id(provider, requested_model)
            definition = self.registry.find(provider, validated)
            self._validate_explicit(definition)
            return ModelRoutingDecision(
                call_number=1,
                routing_mode=routing_mode,
                requested_model=validated,
                selected_provider=provider,
                selected_model=validated,
                selected_tier=definition.tier if definition else None,
                max_allowed_tier=max_allowed_tier,
                reason_code="fixed_model",
                candidate_count=1,
            )

        candidates = self._eligible(provider, max_allowed_tier)
        if requested_model is not None:
            validated = validate_model_id(provider, requested_model)
            definition = self.registry.find(provider, validated)
            if definition is None:
                raise ModelRoutingError(
                    "an explicit model used with auto routing must be registered",
                    code="explicit_model_not_registered",
                )
            self._validate_explicit(definition)
            if TIER_ORDER[definition.tier] > TIER_ORDER[max_allowed_tier]:
                raise ModelRoutingError(
                    "the explicit model exceeds the configured maximum tier",
                    code="model_tier_violation",
                )
            return ModelRoutingDecision(
                call_number=1,
                routing_mode=routing_mode,
                requested_model=validated,
                selected_provider=provider,
                selected_model=validated,
                selected_tier=definition.tier,
                max_allowed_tier=max_allowed_tier,
                reason_code="explicit_model",
                candidate_count=len(candidates),
            )
        if not candidates:
            raise ModelRoutingError(
                "no enabled model satisfies provider, tier, and response capabilities",
                code="no_eligible_model",
            )
        selected = candidates[0]
        return ModelRoutingDecision(
            call_number=1,
            routing_mode=routing_mode,
            requested_model=None,
            selected_provider=provider,
            selected_model=selected.model_id,
            selected_tier=selected.tier,
            max_allowed_tier=max_allowed_tier,
            reason_code="initial_cheapest_eligible",
            candidate_count=len(candidates),
        )

    def select_second(
        self,
        *,
        initial: ModelRoutingDecision,
        second_attempt_reason: SecondAttemptReason,
    ) -> ModelRoutingDecision:
        if initial.routing_mode is ModelRoutingMode.FIXED:
            return self._reuse(initial, "fixed_model")
        if second_attempt_reason is SecondAttemptReason.REPAIR:
            return self._reuse(initial, "repair_same_model")
        if second_attempt_reason is not SecondAttemptReason.CONTEXT_ESCALATION:
            raise ModelRoutingError(
                "a second model decision requires repair or context escalation",
                code="no_eligible_model",
            )
        if initial.selected_tier is None:
            return self._reuse(initial, "no_stronger_model_available")

        eligible = self._eligible(
            initial.selected_provider,
            initial.max_allowed_tier,
        )
        stronger = [
            item
            for item in eligible
            if TIER_ORDER[item.tier] > TIER_ORDER[initial.selected_tier]
        ]
        if stronger:
            selected = stronger[0]
            return ModelRoutingDecision(
                call_number=2,
                routing_mode=initial.routing_mode,
                requested_model=initial.requested_model,
                selected_provider=initial.selected_provider,
                selected_model=selected.model_id,
                selected_tier=selected.tier,
                max_allowed_tier=initial.max_allowed_tier,
                reason_code="context_escalation_next_tier",
                candidate_count=len(eligible),
            )
        reason = (
            "tier_ceiling_reuse"
            if initial.selected_tier is initial.max_allowed_tier
            else "no_stronger_model_available"
        )
        return self._reuse(initial, reason)

    def assert_allowed(self, decision: ModelRoutingDecision) -> None:
        if decision.routing_mode is not ModelRoutingMode.AUTO:
            return
        if decision.selected_tier is None or (
            TIER_ORDER[decision.selected_tier]
            > TIER_ORDER[decision.max_allowed_tier]
        ):
            raise ModelRoutingError(
                "selected model violates the configured tier ceiling",
                code="model_tier_violation",
            )

    def _eligible(
        self, provider: LLMProviderName, ceiling: ModelTier
    ) -> list[ModelDefinition]:
        candidates = [
            item
            for item in self.registry.for_provider(provider)
            if item.enabled
            and (item.supports_structured_output or item.supports_json_fallback)
            and TIER_ORDER[item.tier] <= TIER_ORDER[ceiling]
        ]
        return sorted(
            candidates,
            key=lambda item: (
                TIER_ORDER[item.tier],
                item.priority,
                item.model_id,
            ),
        )

    @staticmethod
    def _validate_explicit(definition: ModelDefinition | None) -> None:
        if definition is None:
            return
        if not definition.enabled:
            raise ModelRoutingError(
                "the explicitly selected model is disabled in the registry",
                code="explicit_model_disabled",
            )
        if not (
            definition.supports_structured_output
            or definition.supports_json_fallback
        ):
            raise ModelRoutingError(
                "the explicitly selected model lacks a supported JSON response path",
                code="model_capability_unsupported",
            )

    @staticmethod
    def _reuse(
        initial: ModelRoutingDecision, reason: RoutingReasonCode
    ) -> ModelRoutingDecision:
        return initial.model_copy(
            update={
                "call_number": 2,
                "reason_code": reason,
            }
        )
