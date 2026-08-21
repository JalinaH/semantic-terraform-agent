"""Central provider selection without provider-specific orchestration logic."""

from __future__ import annotations

from semantic_terraform_agent.config import parse_provider_name, validate_model_id
from semantic_terraform_agent.models import LLMProviderName
from semantic_terraform_agent.reasoning.base import LLMProvider
from semantic_terraform_agent.reasoning.gemini import GeminiProvider
from semantic_terraform_agent.reasoning.openrouter import OpenRouterProvider


def create_llm_provider(
    provider: str | LLMProviderName,
    model: str,
) -> LLMProvider:
    provider_name = parse_provider_name(provider)
    validated_model = validate_model_id(provider_name, model)
    if provider_name is LLMProviderName.GEMINI:
        return GeminiProvider(model=validated_model)
    if provider_name is LLMProviderName.OPENROUTER:
        return OpenRouterProvider(model=validated_model)
    raise AssertionError(f"unhandled provider: {provider_name.value}")
