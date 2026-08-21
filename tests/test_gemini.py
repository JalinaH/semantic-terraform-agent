from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from semantic_terraform_agent.config import ProviderError
from semantic_terraform_agent.models import (
    ContextSelection,
    DiagnosisRequest,
    FailureInfo,
    ModelDiagnosis,
    RepairRequest,
    VerificationAttempt,
    VerificationCommand,
    VerificationCommands,
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


def repair_request() -> RepairRequest:
    previous = ModelDiagnosis.model_validate(valid_payload())
    return RepairRequest(
        original=request(),
        previous_diagnosis=previous,
        failed_attempt=VerificationAttempt(
            attempt=1,
            patch=previous.suggested_patch,
            status="failed",
            failed_stage="plan",
            commands=VerificationCommands(
                plan=VerificationCommand(
                    command=["terraform", "plan"],
                    status="failed",
                    exit_code=1,
                    stderr="plan rejected patch",
                )
            ),
            temporary_copy_cleaned=True,
        ),
    )


def test_structured_gemini_response_is_validated() -> None:
    models = FakeModels(json.dumps(valid_payload()))
    provider = GeminiProvider(
        "gemini-test", api_key="not-a-real-key", client_factory=lambda **_: SimpleNamespace(models=models)
    )
    response = provider.diagnose(request())
    assert response.diagnosis.confidence == 0.8
    assert response.token_usage.total_tokens == 15
    assert response.llm_call is not None
    assert response.llm_call.provider.value == "gemini"
    assert response.llm_call.call_type.value == "diagnosis"
    assert response.llm_call.requested_model == "gemini-test"
    assert response.llm_call.cost_usd is None
    assert response.llm_call.system_prompt_characters == 0
    assert response.llm_call.prompt_characters == response.llm_call.user_prompt_characters
    assert models.last_kwargs["config"]["response_mime_type"] == "application/json"
    assert "response_schema" in models.last_kwargs["config"]
    assert not _contains_key(
        models.last_kwargs["config"]["response_schema"], "additionalProperties"
    )


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


def test_gemini_repair_uses_dedicated_prompt_and_same_strict_schema() -> None:
    models = FakeModels(json.dumps(valid_payload()))
    provider = GeminiProvider(
        "gemini-test",
        api_key="x",
        client_factory=lambda **_: SimpleNamespace(models=models),
    )
    response = provider.repair(repair_request())
    assert response.diagnosis.root_cause == "mode is invalid"
    assert response.llm_call is not None
    assert response.llm_call.call_type.value == "repair"
    assert "previous candidate patch did not pass Terraform verification" in models.last_kwargs[
        "contents"
    ]


def test_missing_api_key_is_reported(monkeypatch) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    provider = GeminiProvider("gemini-test")
    with pytest.raises(ProviderError, match="GEMINI_API_KEY"):
        provider.diagnose(request())


def _contains_key(value: object, key: str) -> bool:
    if isinstance(value, dict):
        return key in value or any(_contains_key(child, key) for child in value.values())
    if isinstance(value, list):
        return any(_contains_key(child, key) for child in value)
    return False
