"""Provider-neutral LLM invocation and run-level usage aggregation."""

from __future__ import annotations

from collections.abc import Iterable

from semantic_terraform_agent.models import (
    ContextCallTelemetry,
    ContextSectionTelemetry,
    ContextTelemetry,
    DiagnosisRequest,
    LLMCallType,
    LLMInvocation,
    LLMProviderName,
    LLMUsage,
    ProviderResponse,
    TokenUsage,
)
from semantic_terraform_agent.reasoning.prompt_models import PromptParts


def invocation_from_response(
    response: ProviderResponse,
    *,
    provider: LLMProviderName,
    requested_model: str,
    call_type: LLMCallType,
    prompt: PromptParts,
    latency_ms: int,
) -> LLMInvocation:
    """Use provider telemetry, or adapt a legacy provider response safely."""
    if response.llm_call is not None:
        return response.llm_call
    return LLMInvocation(
        provider=provider,
        requested_model=requested_model,
        input_tokens=response.token_usage.input_tokens,
        output_tokens=response.token_usage.output_tokens,
        total_tokens=response.token_usage.total_tokens,
        latency_ms=latency_ms,
        call_type=call_type,
        prompt_characters=prompt.prompt_characters,
        system_prompt_characters=len(prompt.system),
        user_prompt_characters=len(prompt.user),
    )


def aggregate_usage(calls: Iterable[LLMInvocation]) -> LLMUsage:
    call_list = list(calls)

    def known_sum(attribute: str) -> int | None:
        values = [getattr(call, attribute) for call in call_list]
        known = [value for value in values if value is not None]
        return sum(known) if known else None

    costs = [call.cost_usd for call in call_list]
    known_costs = [cost for cost in costs if cost is not None]
    cost = round(sum(known_costs), 12) if known_costs else None
    core_token_fields = ("input_tokens", "output_tokens", "total_tokens")
    return LLMUsage(
        call_count=len(call_list),
        input_tokens=known_sum("input_tokens"),
        cached_input_tokens=known_sum("cached_input_tokens"),
        output_tokens=known_sum("output_tokens"),
        reasoning_tokens=known_sum("reasoning_tokens"),
        total_tokens=known_sum("total_tokens"),
        cost_usd=cost,
        latency_ms=sum(call.latency_ms for call in call_list) if call_list else None,
        token_counts_complete=all(
            getattr(call, field) is not None
            for call in call_list
            for field in core_token_fields
        ),
        cost_complete=all(cost is not None for cost in costs),
    )


def legacy_token_usage(usage: LLMUsage) -> TokenUsage:
    return TokenUsage(
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        total_tokens=usage.total_tokens,
    )


def build_context_telemetry(
    request: DiagnosisRequest,
    prompt_records: Iterable[tuple[LLMInvocation, PromptParts]],
) -> ContextTelemetry:
    records = list(prompt_records)
    if not records:
        raise ValueError("at least one prompt record is required")
    invocation, prompt = records[0]
    diagnosis_context = request.diagnosis_context
    schema_included = (
        request.context.selected_mode == "schema-aware"
        and any(
            record.extraction_status == "ok" and record.resource_schema is not None
            for record in request.schemas
        )
    )

    def sections(parts: PromptParts) -> dict[str, ContextSectionTelemetry]:
        return {
            name: ContextSectionTelemetry(characters=characters)
            for name, characters in parts.section_characters.items()
        }

    return ContextTelemetry(
        mode=request.context.selected_mode,
        prompt_characters=invocation.prompt_characters,
        system_prompt_characters=invocation.system_prompt_characters,
        user_prompt_characters=invocation.user_prompt_characters,
        resource_schema_included=schema_included,
        git_diff_included=bool(request.git_diff.strip()),
        source_file_count=(
            len(diagnosis_context.manifest.included_files)
            if diagnosis_context is not None
            else len(request.relevant_sources)
        ),
        source_block_count=(
            len(diagnosis_context.resource_blocks)
            + len(diagnosis_context.supporting_blocks)
            if diagnosis_context is not None
            else 0
        ),
        changed_line_count=(
            diagnosis_context.manifest.changed_lines
            if diagnosis_context is not None
            else 0
        ),
        referenced_symbol_count=(
            len(diagnosis_context.referenced_symbols)
            if diagnosis_context is not None
            else 0
        ),
        schema_included=schema_included,
        selected_context_characters=prompt.selected_context_characters,
        rendered_user_prompt_characters=len(prompt.user),
        sections=sections(prompt),
        calls=[
            ContextCallTelemetry(
                call_type=call.call_type,
                prompt_characters=call.prompt_characters,
                system_prompt_characters=call.system_prompt_characters,
                user_prompt_characters=call.user_prompt_characters,
                selected_context_characters=parts.selected_context_characters,
                sections=sections(parts),
            )
            for call, parts in records
        ],
    )
