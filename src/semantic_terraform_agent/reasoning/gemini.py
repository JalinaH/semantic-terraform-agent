"""Gemini implementation of the provider-neutral reasoning interface."""

from __future__ import annotations

import json
import os
import time
from typing import Any, Callable

from pydantic import ValidationError

from semantic_terraform_agent.config import ProviderError
from semantic_terraform_agent.models import (
    DiagnosisRequest,
    LLMCallType,
    LLMInvocation,
    LLMProviderName,
    ModelDiagnosis,
    ProviderFailureCategory,
    ProviderResponse,
    RepairRequest,
    TokenUsage,
)
from semantic_terraform_agent.reasoning.prompts import (
    PromptParts,
    build_prompt_parts,
    build_repair_prompt_parts,
)


class GeminiProvider:
    def __init__(
        self,
        model: str,
        *,
        api_key: str | None = None,
        client_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.model = model
        self._api_key = api_key
        self._client_factory = client_factory

    def _client(self) -> Any:
        api_key = self._api_key or os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise ProviderError(
                "GEMINI_API_KEY is required for the Gemini provider",
                category=ProviderFailureCategory.AUTHENTICATION_FAILED,
            )
        if self._client_factory is not None:
            return self._client_factory(api_key=api_key)
        try:
            from google import genai
        except ImportError as exc:
            raise ProviderError(
                "Gemini support is not installed; install the google-genai dependency"
            ) from exc
        return genai.Client(api_key=api_key)

    def diagnose(self, request: DiagnosisRequest) -> ProviderResponse:
        return self._generate(build_prompt_parts(request), LLMCallType.DIAGNOSIS)

    def repair(self, request: RepairRequest) -> ProviderResponse:
        return self._generate(build_repair_prompt_parts(request), LLMCallType.REPAIR)

    def _generate(self, prompt: PromptParts, call_type: LLMCallType) -> ProviderResponse:
        client = self._client()
        schema = _gemini_response_schema()
        started = time.perf_counter()
        try:
            response = client.models.generate_content(
                model=self.model,
                contents=prompt.combined,
                config={
                    "response_mime_type": "application/json",
                    "response_schema": schema,
                    "temperature": 0.1,
                },
            )
        except Exception:  # SDK transports expose several provider-specific types.
            raise ProviderError(
                "Gemini request failed.",
                category=ProviderFailureCategory.PROVIDER_UNAVAILABLE,
            ) from None
        latency_ms = round((time.perf_counter() - started) * 1000)
        text = getattr(response, "text", None)
        if not text:
            raise ProviderError(
                "Gemini returned an empty response",
                category=ProviderFailureCategory.RESPONSE_INVALID,
            )
        try:
            diagnosis = ModelDiagnosis.model_validate(json.loads(text))
        except (json.JSONDecodeError, ValidationError):
            raise ProviderError(
                "Gemini returned invalid structured JSON",
                category=ProviderFailureCategory.RESPONSE_INVALID,
            ) from None

        usage = getattr(response, "usage_metadata", None)
        input_tokens = _usage_value(usage, "prompt_token_count")
        output_tokens = _usage_value(usage, "candidates_token_count")
        total_tokens = _usage_value(usage, "total_token_count")
        cached_input_tokens = _usage_value(usage, "cached_content_token_count")
        reasoning_tokens = _usage_value(usage, "thoughts_token_count")
        token_usage = TokenUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
        )
        invocation = LLMInvocation(
            provider=LLMProviderName.GEMINI,
            requested_model=self.model,
            reported_model=_string_value(response, "model_version"),
            upstream_provider="Google",
            input_tokens=input_tokens,
            cached_input_tokens=cached_input_tokens,
            output_tokens=output_tokens,
            reasoning_tokens=reasoning_tokens,
            total_tokens=total_tokens,
            latency_ms=latency_ms,
            cache_hit=(cached_input_tokens > 0 if cached_input_tokens is not None else None),
            call_type=call_type,
            prompt_characters=len(prompt.combined),
            system_prompt_characters=0,
            user_prompt_characters=len(prompt.combined),
            finish_reason=_finish_reason(response),
        )
        return ProviderResponse(
            diagnosis=diagnosis,
            token_usage=token_usage,
            llm_call=invocation,
        )


def _gemini_response_schema() -> dict[str, Any]:
    """Return the strict diagnosis schema using Gemini-supported keywords."""
    schema = ModelDiagnosis.model_json_schema()
    _remove_additional_properties(schema)
    return schema


def _remove_additional_properties(value: Any) -> None:
    if isinstance(value, dict):
        value.pop("additionalProperties", None)
        for child in value.values():
            _remove_additional_properties(child)
    elif isinstance(value, list):
        for child in value:
            _remove_additional_properties(child)


def _usage_value(usage: Any, name: str) -> int | None:
    if usage is None:
        return None
    value = usage.get(name) if isinstance(usage, dict) else getattr(usage, name, None)
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _string_value(value: Any, name: str) -> str | None:
    item = value.get(name) if isinstance(value, dict) else getattr(value, name, None)
    return item if isinstance(item, str) and item else None


def _finish_reason(response: Any) -> str | None:
    candidates = getattr(response, "candidates", None)
    if not isinstance(candidates, list) or not candidates:
        return None
    reason = getattr(candidates[0], "finish_reason", None)
    if reason is None:
        return None
    value = getattr(reason, "value", reason)
    return str(value)[:80]
