from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from semantic_terraform_agent.config import ProviderError
from semantic_terraform_agent.models import (
    ContextSelection,
    DiagnosisRequest,
    FailureInfo,
)
from semantic_terraform_agent.reasoning.gemini import GeminiProvider


def request() -> DiagnosisRequest:
    return DiagnosisRequest(
        failure=FailureInfo(
            summary="Bad mode",
            detail="mode must be safe",
            original_log="Error: Bad mode",
        ),
        resources=[],
        relevant_sources={},
        git_diff="",
        context=ContextSelection(
            requested_mode="lightweight",
            selected_mode="lightweight",
            selection_reason="test",
        ),
        schemas=[],
    )


class FakeModels:
    def __init__(self, text: str) -> None:
        self.text = text
        self.last_kwargs = None

    def generate_content(self, **kwargs):
        self.last_kwargs = kwargs
        return SimpleNamespace(
            text=self.text,
            usage_metadata=SimpleNamespace(
                prompt_token_count=10, candidates_token_count=5, total_token_count=15
            ),
        )


def valid_payload() -> dict:
    return {
        "root_cause": "mode is invalid",
        "affected_resources": ["example_widget.primary"],
        "violated_constraint": "mode must be safe",
        "suggested_patch": "--- a/main.tf\n+++ b/main.tf\n@@ -1 +1 @@",
        "confidence": 0.8,
        "evidence": [{"source": "terraform_error", "detail": "Bad mode"}],
    }


def test_structured_gemini_response_is_validated() -> None:
    models = FakeModels(json.dumps(valid_payload()))
    provider = GeminiProvider(
        "gemini-test", api_key="not-a-real-key", client_factory=lambda **_: SimpleNamespace(models=models)
    )
    response = provider.diagnose(request())
    assert response.diagnosis.confidence == 0.8
    assert response.token_usage.total_tokens == 15
    assert models.last_kwargs["config"]["response_mime_type"] == "application/json"
    assert "response_schema" in models.last_kwargs["config"]


def test_extra_gemini_fields_are_rejected() -> None:
    payload = valid_payload()
    payload["untrusted"] = True
    provider = GeminiProvider(
        "gemini-test",
        api_key="x",
        client_factory=lambda **_: SimpleNamespace(models=FakeModels(json.dumps(payload))),
    )
    with pytest.raises(ProviderError, match="invalid structured JSON"):
        provider.diagnose(request())


def test_missing_api_key_is_reported(monkeypatch) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    provider = GeminiProvider("gemini-test")
    with pytest.raises(ProviderError, match="GEMINI_API_KEY"):
        provider.diagnose(request())
