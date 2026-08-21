from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import pytest

from semantic_terraform_agent.config import InputError, ProviderError
from semantic_terraform_agent.models import (
    ContextSelection,
    DiagnosisRequest,
    FailureInfo,
    LLMCallType,
    ModelDiagnosis,
    ProviderFailureCategory,
    RepairRequest,
    VerificationAttempt,
    VerificationCommand,
    VerificationCommands,
)
from semantic_terraform_agent.reasoning.openrouter import (
    DEFAULT_OPENROUTER_BASE_URL,
    HTTPResponse,
    OpenRouterProvider,
)


def diagnosis_request() -> DiagnosisRequest:
    return DiagnosisRequest(
        failure=FailureInfo(
            summary="Bad mode",
            detail="mode must be safe",
            original_log="Error: Bad mode",
        ),
        resources=[],
        relevant_sources={"main.tf": 'resource "example_widget" "primary" {}'},
        git_diff="+++ b/main.tf",
        context=ContextSelection(
            requested_mode="lightweight",
            selected_mode="lightweight",
            selection_reason="test",
        ),
        schemas=[],
    )


def valid_diagnosis() -> dict[str, Any]:
    return {
        "root_cause": "mode is invalid",
        "affected_resources": ["example_widget.primary"],
        "violated_constraint": "mode must be safe",
        "suggested_patch": "--- a/main.tf\n+++ b/main.tf\n@@ -1 +1 @@\n-old\n+new",
        "confidence": 0.8,
        "evidence": [{"source": "terraform_error", "detail": "Bad mode"}],
    }


def success_payload(*, cost: float | None = 0.0) -> dict[str, Any]:
    usage: dict[str, Any] = {
        "prompt_tokens": 1842,
        "completion_tokens": 218,
        "total_tokens": 2060,
        "prompt_tokens_details": {"cached_tokens": 17},
        "completion_tokens_details": {"reasoning_tokens": 11},
    }
    if cost is not None:
        usage["cost"] = cost
    return {
        "id": "gen-test",
        "model": "example/actual-model:free",
        "provider": "ExampleProvider",
        "choices": [
            {
                "message": {"role": "assistant", "content": json.dumps(valid_diagnosis())},
                "finish_reason": "stop",
            }
        ],
        "usage": usage,
    }


class FakeTransport:
    def __init__(self, *responses: HTTPResponse | Exception) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self,
        url: str,
        headers: Mapping[str, str],
        body: bytes,
        timeout: float,
    ) -> HTTPResponse:
        self.calls.append(
            {
                "url": url,
                "headers": dict(headers),
                "body": json.loads(body),
                "timeout": timeout,
            }
        )
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def response(payload: dict[str, Any], status: int = 200, **headers: str) -> HTTPResponse:
    return HTTPResponse(
        status=status,
        headers=headers,
        body=json.dumps(payload).encode(),
    )


def provider(transport: FakeTransport, **kwargs: Any) -> OpenRouterProvider:
    return OpenRouterProvider(
        "example/requested-model:free",
        api_key="test-openrouter-key",
        transport=transport,
        max_retries=0,
        **kwargs,
    )


def repair_request() -> RepairRequest:
    previous = ModelDiagnosis.model_validate(valid_diagnosis())
    return RepairRequest(
        original=diagnosis_request(),
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
                    stderr="candidate failed",
                )
            ),
            temporary_copy_cleaned=True,
        ),
    )


def test_openrouter_request_and_usage_telemetry() -> None:
    transport = FakeTransport(response(success_payload()))
    result = provider(
        transport,
        app_url="https://agent.example",
        app_name="Semantic Terraform Agent",
    ).diagnose(diagnosis_request())

    call = transport.calls[0]
    assert call["url"] == f"{DEFAULT_OPENROUTER_BASE_URL}/chat/completions"
    assert call["headers"]["Authorization"] == "Bearer test-openrouter-key"
    assert call["headers"]["HTTP-Referer"] == "https://agent.example"
    assert call["headers"]["X-OpenRouter-Title"] == "Semantic Terraform Agent"
    assert call["body"]["model"] == "example/requested-model:free"
    assert [message["role"] for message in call["body"]["messages"]] == [
        "system",
        "user",
    ]
    assert call["body"]["response_format"]["type"] == "json_schema"
    assert call["body"]["response_format"]["json_schema"]["strict"] is True
    assert call["body"]["provider"] == {"require_parameters": True}

    assert result.diagnosis.confidence == 0.8
    assert result.token_usage.total_tokens == 2060
    assert result.llm_call is not None
    assert result.llm_call.provider.value == "openrouter"
    assert result.llm_call.call_type is LLMCallType.DIAGNOSIS
    assert result.llm_call.requested_model == "example/requested-model:free"
    assert result.llm_call.reported_model == "example/actual-model:free"
    assert result.llm_call.upstream_provider == "ExampleProvider"
    assert result.llm_call.cached_input_tokens == 17
    assert result.llm_call.reasoning_tokens == 11
    assert result.llm_call.cost_usd == 0.0
    assert result.llm_call.cache_hit is True
    assert result.llm_call.prompt_characters == (
        result.llm_call.system_prompt_characters + result.llm_call.user_prompt_characters
    )


def test_openrouter_repair_is_tagged_separately() -> None:
    transport = FakeTransport(response(success_payload(cost=0.001)))
    result = provider(transport).repair(repair_request())
    assert result.llm_call is not None
    assert result.llm_call.call_type is LLMCallType.REPAIR
    assert "previous candidate patch did not pass" in transport.calls[0]["body"]["messages"][0][
        "content"
    ]


def test_structured_output_unsupported_falls_back_to_strict_json_prompt() -> None:
    transport = FakeTransport(
        response(
            {"error": {"code": 400, "message": "response_format is not supported"}},
            status=400,
        ),
        response(success_payload()),
    )
    result = provider(transport).diagnose(diagnosis_request())
    assert len(transport.calls) == 2
    assert "response_format" in transport.calls[0]["body"]
    assert "response_format" not in transport.calls[1]["body"]
    assert "Return exactly one JSON object matching this JSON Schema" in transport.calls[1][
        "body"
    ]["messages"][0]["content"]
    assert result.llm_call is not None
    assert result.llm_call.system_prompt_characters == len(
        transport.calls[1]["body"]["messages"][0]["content"]
    )


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        json.dumps({key: value for key, value in valid_diagnosis().items() if key != "root_cause"}),
        json.dumps({**valid_diagnosis(), "extra": True}),
    ],
)
def test_invalid_openrouter_diagnosis_is_rejected(content: str) -> None:
    payload = success_payload()
    payload["choices"][0]["message"]["content"] = content
    with pytest.raises(ProviderError) as exc:
        provider(FakeTransport(response(payload))).diagnose(diagnosis_request())
    assert exc.value.category is ProviderFailureCategory.RESPONSE_INVALID


@pytest.mark.parametrize(
    ("status", "payload", "expected"),
    [
        (404, {"error": {"code": 404, "message": "model not found"}}, "model_not_found"),
        (401, {"error": {"code": 401, "message": "bad key"}}, "authentication_failed"),
        (429, {"error": {"code": 429, "message": "rate limited"}}, "rate_limited"),
        (502, {"error": {"code": 502, "message": "model down"}}, "model_unavailable"),
        (503, {"error": {"code": 503, "message": "no provider"}}, "provider_unavailable"),
    ],
)
def test_openrouter_errors_are_classified(
    status: int, payload: dict[str, Any], expected: str
) -> None:
    with pytest.raises(ProviderError) as exc:
        provider(FakeTransport(response(payload, status=status))).diagnose(diagnosis_request())
    assert exc.value.category.value == expected
    assert "test-openrouter-key" not in str(exc.value)


def test_http_200_upstream_error_is_classified() -> None:
    payload = {
        "error": {
            "code": 429,
            "message": "Rate limit exceeded",
            "metadata": {"error_type": "rate_limit_exceeded"},
        }
    }
    with pytest.raises(ProviderError) as exc:
        provider(FakeTransport(response(payload))).diagnose(diagnosis_request())
    assert exc.value.category is ProviderFailureCategory.RATE_LIMITED


def test_timeout_retries_are_bounded_and_key_is_not_exposed(caplog) -> None:
    transport = FakeTransport(TimeoutError("test-openrouter-key"), TimeoutError(), TimeoutError())
    sleeps: list[float] = []
    openrouter = OpenRouterProvider(
        "openrouter/free",
        api_key="test-openrouter-key",
        transport=transport,
        max_retries=2,
        sleep=sleeps.append,
    )
    with pytest.raises(ProviderError) as exc:
        openrouter.diagnose(diagnosis_request())
    assert exc.value.category is ProviderFailureCategory.TIMEOUT
    assert len(transport.calls) == 3
    assert sleeps == [0.25, 0.5]
    assert "test-openrouter-key" not in str(exc.value)
    assert "test-openrouter-key" not in caplog.text
    assert exc.value.__suppress_context__ is True


def test_transient_statuses_retry_then_succeed() -> None:
    transport = FakeTransport(
        response({"error": {"code": 429, "message": "wait"}}, status=429),
        response({"error": {"code": 503, "message": "busy"}}, status=503),
        response(success_payload()),
    )
    sleeps: list[float] = []
    openrouter = OpenRouterProvider(
        "openrouter/free",
        api_key="x",
        transport=transport,
        max_retries=2,
        sleep=sleeps.append,
    )
    result = openrouter.diagnose(diagnosis_request())
    assert result.diagnosis.root_cause == "mode is invalid"
    assert len(transport.calls) == 3
    assert sleeps == [0.25, 0.5]


def test_unknown_cost_remains_none_and_key_is_absent_from_result_json() -> None:
    result = provider(FakeTransport(response(success_payload(cost=None)))).diagnose(
        diagnosis_request()
    )
    assert result.llm_call is not None
    assert result.llm_call.cost_usd is None
    assert "test-openrouter-key" not in result.model_dump_json()


def test_model_ids_are_dynamic_but_conservatively_validated() -> None:
    OpenRouterProvider("openrouter/free", api_key="x", transport=FakeTransport())
    OpenRouterProvider("vendor/new-model.v7:free", api_key="x", transport=FakeTransport())
    with pytest.raises(InputError, match="provider/model"):
        OpenRouterProvider("not-a-qualified-model", api_key="x", transport=FakeTransport())
    with pytest.raises(InputError, match="whitespace or control"):
        OpenRouterProvider("vendor/model\nsecret", api_key="x", transport=FakeTransport())


def test_missing_key_is_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(ProviderError) as exc:
        OpenRouterProvider("openrouter/free", transport=FakeTransport()).diagnose(
            diagnosis_request()
        )
    assert exc.value.category is ProviderFailureCategory.AUTHENTICATION_FAILED
    assert "OPENROUTER_API_KEY" in str(exc.value)
