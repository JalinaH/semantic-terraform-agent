from __future__ import annotations

from semantic_terraform_agent.models import (
    LLMCallType,
    LLMInvocation,
    LLMProviderName,
)
from semantic_terraform_agent.reasoning.usage import aggregate_usage, legacy_token_usage


def invocation(
    call_type: LLMCallType,
    *,
    input_tokens: int | None,
    cached_tokens: int | None,
    output_tokens: int | None,
    reasoning_tokens: int | None,
    total_tokens: int | None,
    cost: float | None,
    latency_ms: int,
) -> LLMInvocation:
    return LLMInvocation(
        provider=LLMProviderName.OPENROUTER,
        requested_model="openrouter/free",
        input_tokens=input_tokens,
        cached_input_tokens=cached_tokens,
        output_tokens=output_tokens,
        reasoning_tokens=reasoning_tokens,
        total_tokens=total_tokens,
        cost_usd=cost,
        latency_ms=latency_ms,
        call_type=call_type,
        prompt_characters=100,
        system_prompt_characters=40,
        user_prompt_characters=60,
    )


def test_single_call_usage_and_legacy_token_compatibility() -> None:
    usage = aggregate_usage(
        [
            invocation(
                LLMCallType.DIAGNOSIS,
                input_tokens=100,
                cached_tokens=20,
                output_tokens=30,
                reasoning_tokens=5,
                total_tokens=130,
                cost=0.0,
                latency_ms=400,
            )
        ]
    )
    assert usage.call_count == 1
    assert usage.cost_usd == 0.0
    assert usage.cost_complete is True
    assert usage.token_counts_complete is True
    assert legacy_token_usage(usage).model_dump() == {
        "input_tokens": 100,
        "output_tokens": 30,
        "total_tokens": 130,
    }


def test_diagnosis_and_repair_usage_is_aggregated() -> None:
    calls = [
        invocation(
            LLMCallType.DIAGNOSIS,
            input_tokens=100,
            cached_tokens=20,
            output_tokens=30,
            reasoning_tokens=5,
            total_tokens=130,
            cost=0.001,
            latency_ms=400,
        ),
        invocation(
            LLMCallType.REPAIR,
            input_tokens=200,
            cached_tokens=0,
            output_tokens=50,
            reasoning_tokens=7,
            total_tokens=250,
            cost=0.002,
            latency_ms=600,
        ),
    ]
    usage = aggregate_usage(calls)
    assert usage.call_count == 2
    assert usage.input_tokens == 300
    assert usage.cached_input_tokens == 20
    assert usage.output_tokens == 80
    assert usage.reasoning_tokens == 12
    assert usage.total_tokens == 380
    assert usage.cost_usd == 0.003
    assert usage.latency_ms == 1000


def test_incomplete_cost_and_tokens_are_explicit() -> None:
    calls = [
        invocation(
            LLMCallType.DIAGNOSIS,
            input_tokens=100,
            cached_tokens=None,
            output_tokens=30,
            reasoning_tokens=None,
            total_tokens=130,
            cost=0.001,
            latency_ms=400,
        ),
        invocation(
            LLMCallType.REPAIR,
            input_tokens=None,
            cached_tokens=None,
            output_tokens=None,
            reasoning_tokens=None,
            total_tokens=None,
            cost=None,
            latency_ms=600,
        ),
    ]
    usage = aggregate_usage(calls)
    assert usage.input_tokens == 100
    assert usage.cached_input_tokens is None
    assert usage.cost_usd == 0.001
    assert usage.cost_complete is False
    assert usage.token_counts_complete is False
