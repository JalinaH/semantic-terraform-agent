"""Gemini implementation of the provider-neutral reasoning interface."""

from __future__ import annotations

import json
import os
from typing import Any, Callable

from pydantic import ValidationError

from semantic_terraform_agent.config import ProviderError
from semantic_terraform_agent.models import (
    DiagnosisRequest,
    ModelDiagnosis,
    ProviderResponse,
    RepairRequest,
    TokenUsage,
)
from semantic_terraform_agent.reasoning.prompts import build_prompt, build_repair_prompt


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
            raise ProviderError("GEMINI_API_KEY is required for the Gemini provider")
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
        return self._generate(build_prompt(request))

    def repair(self, request: RepairRequest) -> ProviderResponse:
        return self._generate(build_repair_prompt(request))

    def _generate(self, prompt: str) -> ProviderResponse:
        client = self._client()
        schema = ModelDiagnosis.model_json_schema()
        try:
            response = client.models.generate_content(
                model=self.model,
                contents=prompt,
                config={
                    "response_mime_type": "application/json",
                    "response_schema": schema,
                    "temperature": 0.1,
                },
            )
        except Exception as exc:  # SDK transports expose several provider-specific types.
            raise ProviderError(f"Gemini request failed: {exc}") from exc
        text = getattr(response, "text", None)
        if not text:
            raise ProviderError("Gemini returned an empty response")
        try:
            diagnosis = ModelDiagnosis.model_validate(json.loads(text))
        except (json.JSONDecodeError, ValidationError) as exc:
            raise ProviderError(f"Gemini returned invalid structured JSON: {exc}") from exc

        usage = getattr(response, "usage_metadata", None)
        token_usage = TokenUsage(
            input_tokens=_usage_value(usage, "prompt_token_count"),
            output_tokens=_usage_value(usage, "candidates_token_count"),
            total_tokens=_usage_value(usage, "total_token_count"),
        )
        return ProviderResponse(diagnosis=diagnosis, token_usage=token_usage)


def _usage_value(usage: Any, name: str) -> int | None:
    if usage is None:
        return None
    value = usage.get(name) if isinstance(usage, dict) else getattr(usage, name, None)
    return int(value) if isinstance(value, (int, float)) else None
