"""OpenRouter chat-completions adapter with safe telemetry and bounded retries."""

from __future__ import annotations

import json
import math
import os
import socket
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from semantic_terraform_agent.config import (
    DEFAULT_LIMITS,
    DEFAULT_OPENROUTER_MODEL,
    ProviderError,
    validate_model_id,
)
from semantic_terraform_agent.models import (
    DiagnosisRequest,
    LLMCallType,
    LLMInvocation,
    LLMProviderName,
    ModelDiagnosis,
    ProviderFailureCategory,
    ProviderResponse,
    RepairRequest,
    SemanticEditSet,
    TokenUsage,
)
from semantic_terraform_agent.reasoning.prompts import (
    PromptParts,
    build_prompt_parts,
    build_repair_prompt_parts,
)
from semantic_terraform_agent.security import redact_secrets


DEFAULT_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_TIMEOUT_SECONDS = 60.0
DEFAULT_MAX_RETRIES = 2
MAX_OPENROUTER_RESPONSE_BYTES = 4 * 1024 * 1024
_RETRYABLE_STATUS_CODES = {408, 429, 500, 502, 503, 504}


@dataclass(frozen=True)
class HTTPResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


HTTPTransport = Callable[[str, Mapping[str, str], bytes, float], HTTPResponse]


def _default_transport(
    url: str, headers: Mapping[str, str], body: bytes, timeout: float
) -> HTTPResponse:
    request = urllib.request.Request(
        url=url,
        data=body,
        headers=dict(headers),
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return HTTPResponse(
                status=response.status,
                headers=dict(response.headers.items()),
                body=response.read(),
            )
    except urllib.error.HTTPError as exc:
        return HTTPResponse(
            status=exc.code,
            headers=dict(exc.headers.items()) if exc.headers else {},
            body=exc.read(),
        )


class OpenRouterProvider:
    def __init__(
        self,
        model: str,
        *,
        api_key: str | None = None,
        base_url: str = DEFAULT_OPENROUTER_BASE_URL,
        app_url: str | None = None,
        app_name: str | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff_seconds: float = 0.25,
        transport: HTTPTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.model = validate_model_id(LLMProviderName.OPENROUTER, model)
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if max_retries < 0 or max_retries > 5:
            raise ValueError("max_retries must be between 0 and 5")
        if backoff_seconds < 0:
            raise ValueError("backoff_seconds must not be negative")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._app_url = app_url
        self._app_name = app_name
        self._timeout_seconds = timeout_seconds
        self._max_retries = max_retries
        self._backoff_seconds = backoff_seconds
        self._transport = transport or _default_transport
        self._sleep = sleep

    @property
    def endpoint(self) -> str:
        return f"{self._base_url}/chat/completions"

    def diagnose(self, request: DiagnosisRequest) -> ProviderResponse:
        return self._generate(build_prompt_parts(request), LLMCallType.DIAGNOSIS)

    def repair(self, request: RepairRequest) -> ProviderResponse:
        return self._generate(build_repair_prompt_parts(request), LLMCallType.REPAIR)

    def _generate(
        self, prompt: PromptParts, call_type: LLMCallType
    ) -> ProviderResponse:
        api_key = self._api_key or os.environ.get("OPENROUTER_API_KEY")
        if not api_key:
            raise ProviderError(
                "OPENROUTER_API_KEY is required for the OpenRouter provider",
                category=ProviderFailureCategory.AUTHENTICATION_FAILED,
            )

        started = time.perf_counter()
        active_prompt = prompt
        response_model = (
            ModelDiagnosis if call_type is LLMCallType.DIAGNOSIS else SemanticEditSet
        )
        enforce_structured_output = self.model != DEFAULT_OPENROUTER_MODEL
        if not enforce_structured_output:
            active_prompt = _json_fallback_prompt(prompt, response_model)
        try:
            payload = self._request_completion(
                active_prompt,
                api_key=api_key,
                enforce_structured_output=enforce_structured_output,
                response_model=response_model,
                call_type=call_type,
            )
            latency_ms = round((time.perf_counter() - started) * 1000)
            return _provider_response(
                payload,
                requested_model=self.model,
                call_type=call_type,
                prompt=active_prompt,
                latency_ms=latency_ms,
            )
        except ProviderError as exc:
            if exc.category not in {
                ProviderFailureCategory.STRUCTURED_OUTPUT_UNSUPPORTED,
                ProviderFailureCategory.RESPONSE_INVALID,
            }:
                raise
            active_prompt = _json_fallback_prompt(prompt, response_model)
            payload = self._request_completion(
                active_prompt,
                api_key=api_key,
                enforce_structured_output=False,
                response_model=response_model,
                call_type=call_type,
            )
        latency_ms = round((time.perf_counter() - started) * 1000)
        return _provider_response(
            payload,
            requested_model=self.model,
            call_type=call_type,
            prompt=active_prompt,
            latency_ms=latency_ms,
        )

    def _request_completion(
        self,
        prompt: PromptParts,
        *,
        api_key: str,
        enforce_structured_output: bool,
        response_model: type[ModelDiagnosis] | type[SemanticEditSet],
        call_type: LLMCallType,
    ) -> dict[str, Any]:
        request_body: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": prompt.system},
                {"role": "user", "content": prompt.user},
            ],
            "temperature": 0.1,
        }
        if call_type is LLMCallType.REPAIR:
            request_body["max_tokens"] = DEFAULT_LIMITS.max_structured_repair_output_tokens
        if enforce_structured_output:
            request_body["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": (
                        "terraform_diagnosis"
                        if call_type is LLMCallType.DIAGNOSIS
                        else "terraform_candidate_edit"
                    ),
                    "strict": True,
                    "schema": response_model.model_json_schema(),
                },
            }
            request_body["provider"] = {"require_parameters": True}

        encoded = json.dumps(request_body, separators=(",", ":")).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        app_url = self._app_url or os.environ.get("OPENROUTER_APP_URL")
        app_name = self._app_name or os.environ.get("OPENROUTER_APP_NAME")
        if app_url:
            headers["HTTP-Referer"] = app_url
        if app_name:
            headers["X-OpenRouter-Title"] = app_name

        response = self._post_with_retries(headers, encoded)
        payload = _decode_response(response)
        error = _response_error(
            response.status,
            payload,
            requested_model=self.model,
        )
        if error is not None:
            raise error
        return payload

    def _post_with_retries(
        self, headers: Mapping[str, str], body: bytes
    ) -> HTTPResponse:
        for attempt in range(self._max_retries + 1):
            try:
                response = self._transport(
                    self.endpoint,
                    headers,
                    body,
                    self._timeout_seconds,
                )
            except (TimeoutError, socket.timeout):
                if attempt < self._max_retries:
                    self._sleep(self._retry_delay(attempt, {}))
                    continue
                raise ProviderError(
                    "OpenRouter request timed out after bounded retries.",
                    category=ProviderFailureCategory.TIMEOUT,
                ) from None
            except (urllib.error.URLError, OSError):
                if attempt < self._max_retries:
                    self._sleep(self._retry_delay(attempt, {}))
                    continue
                raise ProviderError(
                    "OpenRouter network request failed after bounded retries.",
                    category=ProviderFailureCategory.NETWORK_ERROR,
                ) from None

            if response.status in _RETRYABLE_STATUS_CODES and attempt < self._max_retries:
                self._sleep(self._retry_delay(attempt, response.headers))
                continue
            return response
        raise AssertionError("bounded OpenRouter retry loop exhausted unexpectedly")

    def _retry_delay(self, attempt: int, headers: Mapping[str, str]) -> float:
        retry_after = _header_value(headers, "retry-after")
        if retry_after is not None:
            try:
                return min(max(float(retry_after), 0.0), 2.0)
            except ValueError:
                pass
        return min(self._backoff_seconds * (2**attempt), 2.0)


def _json_fallback_prompt(
    prompt: PromptParts,
    response_model: type[ModelDiagnosis] | type[SemanticEditSet],
) -> PromptParts:
    schema = json.dumps(response_model.model_json_schema(), separators=(",", ":"))
    return PromptParts(
        system=(
            f"{prompt.system}\n\nThe selected model cannot use API-enforced structured "
            "output. Return exactly one JSON object matching this JSON Schema; do not "
            f"add fields, Markdown, or prose outside the object:\n{schema}"
        ),
        user=prompt.user,
    )


def _decode_response(response: HTTPResponse) -> dict[str, Any]:
    if len(response.body) > MAX_OPENROUTER_RESPONSE_BYTES:
        raise ProviderError(
            "OpenRouter response exceeded the bounded response size.",
            category=ProviderFailureCategory.RESPONSE_INVALID,
        )
    try:
        payload = json.loads(response.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        if response.status >= 400:
            payload = {}
        else:
            raise ProviderError(
                "OpenRouter returned a non-JSON response.",
                category=ProviderFailureCategory.RESPONSE_INVALID,
            ) from None
    if not isinstance(payload, dict):
        raise ProviderError(
            "OpenRouter returned an invalid response envelope.",
            category=ProviderFailureCategory.RESPONSE_INVALID,
        )
    return payload


def _response_error(
    status: int,
    payload: dict[str, Any],
    *,
    requested_model: str,
) -> ProviderError | None:
    error_payload = payload.get("error")
    choice_error = None
    choices = payload.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        choice_error = choices[0].get("error")
        if choice_error is None and choices[0].get("finish_reason") == "error":
            choice_error = {"code": 502, "message": "generation ended with an error"}
    if status < 400 and not isinstance(error_payload, dict) and not isinstance(choice_error, dict):
        return None

    provider_error = error_payload if isinstance(error_payload, dict) else choice_error
    provider_error = provider_error if isinstance(provider_error, dict) else {}
    code = _integer(provider_error.get("code")) or status
    detail = _safe_text(provider_error.get("message"))
    metadata = provider_error.get("metadata")
    error_type = None
    if isinstance(metadata, dict):
        error_type = _safe_text(metadata.get("error_type"))
    category = _classify_error(code, detail, error_type)
    return ProviderError(
        _error_message(category, requested_model),
        category=category,
    )


def _classify_error(
    status: int, detail: str | None, error_type: str | None
) -> ProviderFailureCategory:
    combined = " ".join(value for value in (detail, error_type) if value).lower()
    if any(
        marker in combined
        for marker in (
            "response_format",
            "structured output",
            "structured_output",
            "json_schema",
            "required parameters",
            "required parameter",
            "requested parameters",
            "requested parameter",
            "require_parameters",
            "no endpoints found that support",
        )
    ):
        return ProviderFailureCategory.STRUCTURED_OUTPUT_UNSUPPORTED
    if error_type == "authentication" or status == 401:
        return ProviderFailureCategory.AUTHENTICATION_FAILED
    if error_type in {"payment_required", "token_limit_exceeded"} or status == 402:
        return ProviderFailureCategory.QUOTA_EXCEEDED
    if error_type == "rate_limit_exceeded" or status == 429:
        return ProviderFailureCategory.RATE_LIMITED
    if error_type == "timeout" or status in {408, 504}:
        return ProviderFailureCategory.TIMEOUT
    if status == 404 or ("model" in combined and "not found" in combined):
        return ProviderFailureCategory.MODEL_NOT_FOUND
    if error_type in {"provider_overloaded", "provider_unavailable"} or status == 502:
        return ProviderFailureCategory.MODEL_UNAVAILABLE
    if status == 503 or status >= 500:
        return ProviderFailureCategory.PROVIDER_UNAVAILABLE
    if status in {400, 403, 422}:
        return ProviderFailureCategory.RESPONSE_INVALID
    return ProviderFailureCategory.PROVIDER_UNAVAILABLE


def _error_message(category: ProviderFailureCategory, model: str) -> str:
    if category is ProviderFailureCategory.MODEL_NOT_FOUND:
        return f"OpenRouter model was not found: {model}."
    if category is ProviderFailureCategory.MODEL_UNAVAILABLE:
        return f"OpenRouter model is temporarily unavailable: {model}."
    if category is ProviderFailureCategory.STRUCTURED_OUTPUT_UNSUPPORTED:
        return "The selected OpenRouter model does not support structured output."
    if category is ProviderFailureCategory.RATE_LIMITED:
        if model == "openrouter/free" or model.endswith(":free"):
            return (
                "OpenRouter free-model rate limit reached. Try again later or choose "
                "another available model."
            )
        return "OpenRouter rate limit reached. Try again later or choose another model."
    if category is ProviderFailureCategory.QUOTA_EXCEEDED:
        return "OpenRouter quota or credit limit was exceeded."
    if category is ProviderFailureCategory.AUTHENTICATION_FAILED:
        return "OpenRouter authentication failed; check OPENROUTER_API_KEY."
    if category is ProviderFailureCategory.TIMEOUT:
        return "OpenRouter request timed out after bounded retries."
    if category is ProviderFailureCategory.NETWORK_ERROR:
        return "OpenRouter network request failed after bounded retries."
    if category is ProviderFailureCategory.RESPONSE_INVALID:
        return "OpenRouter rejected the request or returned an invalid response."
    return "OpenRouter or its upstream provider is currently unavailable."


def _provider_response(
    payload: dict[str, Any],
    *,
    requested_model: str,
    call_type: LLMCallType,
    prompt: PromptParts,
    latency_ms: int,
) -> ProviderResponse:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise ProviderError(
            "OpenRouter returned no completion choice.",
            category=ProviderFailureCategory.RESPONSE_INVALID,
        )
    choice = choices[0]
    message = choice.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str) or not content.strip():
        raise ProviderError(
            "OpenRouter returned an empty completion.",
            category=ProviderFailureCategory.RESPONSE_INVALID,
        )
    try:
        decoded = json.loads(content)
        response_value = (
            ModelDiagnosis.model_validate(decoded)
            if call_type is LLMCallType.DIAGNOSIS
            else SemanticEditSet.model_validate(decoded)
        )
    except (json.JSONDecodeError, ValidationError):
        raise ProviderError(
            "OpenRouter returned invalid structured JSON.",
            category=ProviderFailureCategory.RESPONSE_INVALID,
        ) from None

    usage = payload.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    prompt_details = usage.get("prompt_tokens_details")
    prompt_details = prompt_details if isinstance(prompt_details, dict) else {}
    completion_details = usage.get("completion_tokens_details")
    completion_details = completion_details if isinstance(completion_details, dict) else {}
    input_tokens = _integer(usage.get("prompt_tokens"))
    output_tokens = _integer(usage.get("completion_tokens"))
    total_tokens = _integer(usage.get("total_tokens"))
    cached_input_tokens = _integer(prompt_details.get("cached_tokens"))
    reasoning_tokens = _integer(completion_details.get("reasoning_tokens"))
    cost_usd = _number(usage.get("cost"))
    reported_model = _safe_text(payload.get("model"), max_length=200)
    upstream_provider = _safe_text(payload.get("provider"), max_length=100)
    finish_reason = _safe_text(choice.get("finish_reason"), max_length=80)
    invocation = LLMInvocation(
        provider=LLMProviderName.OPENROUTER,
        requested_model=requested_model,
        reported_model=reported_model,
        upstream_provider=upstream_provider,
        input_tokens=input_tokens,
        cached_input_tokens=cached_input_tokens,
        output_tokens=output_tokens,
        reasoning_tokens=reasoning_tokens,
        total_tokens=total_tokens,
        cost_usd=cost_usd,
        latency_ms=latency_ms,
        cache_hit=(cached_input_tokens > 0 if cached_input_tokens is not None else None),
        call_type=call_type,
        prompt_characters=prompt.prompt_characters,
        system_prompt_characters=len(prompt.system),
        user_prompt_characters=len(prompt.user),
        finish_reason=finish_reason,
    )
    return ProviderResponse(
        diagnosis=(
            response_value if isinstance(response_value, ModelDiagnosis) else None
        ),
        candidate_edit=(
            response_value if isinstance(response_value, SemanticEditSet) else None
        ),
        token_usage=TokenUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
        ),
        llm_call=invocation,
    )


def _safe_text(value: Any, *, max_length: int = 240) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    safe = redact_secrets(value).replace("\r", " ").replace("\n", " ")
    safe = "".join(character for character in safe if ord(character) >= 32)
    return safe[:max_length] or None


def _integer(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, float) and math.isfinite(value) and value >= 0 and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _header_value(headers: Mapping[str, str], name: str) -> str | None:
    for key, value in headers.items():
        if key.lower() == name.lower():
            return value
    return None
