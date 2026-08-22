"""Fixed-cheap, fixed-strong, and routed model-policy evaluation."""

from __future__ import annotations

import json
from pathlib import Path
from statistics import mean
from typing import Any

from semantic_terraform_agent.evaluation import CASE_IDS
from semantic_terraform_agent.models import (
    LLMProviderName,
    ModelDefinition,
    ModelRoutingMode,
    ModelTier,
    ResultDocument,
    SecondAttemptReason,
)
from semantic_terraform_agent.reasoning.model_registry import ModelRegistry
from semantic_terraform_agent.reasoning.routing import ModelRoutingPolicy


STRATEGIES = ("fixed_cheap", "fixed_strong", "routed")
VERIFIED_STATUSES = {"verified_first_attempt", "verified_after_retry"}


def build_routing_comparison(
    *,
    provider: LLMProviderName,
    cheap_model: str,
    strong_model: str,
    cheap_tier: ModelTier,
    strong_tier: ModelTier,
    result_directories: dict[str, Path] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    registry = _pair_registry(
        provider,
        cheap_model,
        strong_model,
        cheap_tier,
        strong_tier,
    )
    policy = ModelRoutingPolicy(registry)
    policy_routes = _policy_routes(
        policy,
        provider=provider,
        cheap_model=cheap_model,
        strong_model=strong_model,
        strong_tier=strong_tier,
    )
    directories = result_directories or {}
    rows: list[dict[str, Any]] = []
    for case_id in CASE_IDS:
        for strategy in STRATEGIES:
            result = _load_result(directories.get(strategy), case_id)
            metrics = _result_metrics(result)
            initial, escalation = policy_routes[strategy]
            rows.append(
                {
                    "case_id": case_id,
                    "strategy": strategy,
                    "provider": provider.value,
                    "policy_initial_model": initial.selected_model,
                    "policy_initial_tier": (
                        initial.selected_tier.value if initial.selected_tier else None
                    ),
                    "policy_context_escalation_model": escalation.selected_model,
                    "policy_context_escalation_tier": (
                        escalation.selected_tier.value
                        if escalation.selected_tier
                        else None
                    ),
                    "initial_model": _metric(metrics, "initial_model"),
                    "final_model": _metric(metrics, "final_model"),
                    "initial_tier": _metric(metrics, "initial_tier"),
                    "final_tier": _metric(metrics, "final_tier"),
                    "model_escalated": _metric(metrics, "model_escalated"),
                    "model_calls": _metric(metrics, "model_calls"),
                    "context_progression": _metric(metrics, "context_progression"),
                    "verification_status": _metric(metrics, "verification_status"),
                    "input_tokens": _metric(metrics, "input_tokens"),
                    "output_tokens": _metric(metrics, "output_tokens"),
                    "total_tokens": _metric(metrics, "total_tokens"),
                    "cost_usd": _metric(metrics, "cost_usd"),
                    "cost_complete": _metric(metrics, "cost_complete"),
                    "latency_seconds": _metric(metrics, "latency_seconds"),
                    "first_call_verified": _metric(metrics, "first_call_verified"),
                    "higher_tier_used": _metric(metrics, "higher_tier_used"),
                }
            )
    return rows, aggregate_routing_results(rows)


def aggregate_routing_results(rows: list[dict[str, Any]]) -> dict[str, Any]:
    strategies: dict[str, dict[str, Any]] = {}
    for strategy in STRATEGIES:
        live = [
            row
            for row in rows
            if row["strategy"] == strategy and row.get("model_calls") is not None
        ]
        verified_count = sum(
            row.get("verification_status") in VERIFIED_STATUSES for row in live
        )
        complete_costs = bool(live) and all(
            row.get("cost_complete") is True and row.get("cost_usd") is not None
            for row in live
        )
        strategies[strategy] = {
            "run_count": len(live),
            "overall_verified_fix_rate": _rate(
                [row.get("verification_status") in VERIFIED_STATUSES for row in live]
            ),
            "first_call_verified_rate": _rate(
                [bool(row.get("first_call_verified")) for row in live]
            ),
            "model_escalation_rate": _rate(
                [bool(row.get("model_escalated")) for row in live]
            ),
            "mean_input_tokens": _mean_known(
                row.get("input_tokens") for row in live
            ),
            "mean_output_tokens": _mean_known(
                row.get("output_tokens") for row in live
            ),
            "mean_total_tokens": _mean_known(
                row.get("total_tokens") for row in live
            ),
            "mean_reported_cost_usd": _mean_known(
                row.get("cost_usd") for row in live
            ),
            "mean_latency_seconds": _mean_known(
                row.get("latency_seconds") for row in live
            ),
            "cost_per_verified_fix": (
                round(sum(row["cost_usd"] for row in live) / verified_count, 12)
                if complete_costs and verified_count
                else None
            ),
        }
    routed = [
        row
        for row in rows
        if row["strategy"] == "routed" and row.get("model_calls") is not None
    ]
    return {
        "strong_model_avoidance_rate": _rate(
            [not bool(row.get("higher_tier_used")) for row in routed]
        ),
        "strategies": strategies,
    }


def write_routing_comparison(
    rows: list[dict[str, Any]], aggregates: dict[str, Any], output_dir: Path
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    document = {
        "comparison": "fixed-cheap versus fixed-strong versus routed",
        "offline_policy": (
            "Policy routes are deterministic local selections, not model-quality, cost, "
            "latency, or verification measurements."
        ),
        "cost_policy": (
            "Cost is provider-reported. Cost per verified fix is null unless every "
            "included run has complete cost telemetry."
        ),
        "aggregates": aggregates,
        "rows": rows,
    }
    (output_dir / "results.json").write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (output_dir / "results.jsonl").write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )
    (output_dir / "comparison.md").write_text(
        _markdown(rows, aggregates), encoding="utf-8"
    )


def _pair_registry(
    provider: LLMProviderName,
    cheap_model: str,
    strong_model: str,
    cheap_tier: ModelTier,
    strong_tier: ModelTier,
) -> ModelRegistry:
    return ModelRegistry(
        [
            ModelDefinition(
                provider=provider,
                model_id=cheap_model,
                tier=cheap_tier,
                priority=10,
                supports_structured_output=True,
                supports_json_fallback=provider is LLMProviderName.OPENROUTER,
                notes="Evaluation lower-tier model configured by the caller.",
            ),
            ModelDefinition(
                provider=provider,
                model_id=strong_model,
                tier=strong_tier,
                priority=10,
                supports_structured_output=True,
                supports_json_fallback=provider is LLMProviderName.OPENROUTER,
                notes="Evaluation higher-tier model configured by the caller.",
            ),
        ]
    )


def _policy_routes(
    policy: ModelRoutingPolicy,
    *,
    provider: LLMProviderName,
    cheap_model: str,
    strong_model: str,
    strong_tier: ModelTier,
):
    result = {}
    for strategy, mode, requested in (
        ("fixed_cheap", ModelRoutingMode.FIXED, cheap_model),
        ("fixed_strong", ModelRoutingMode.FIXED, strong_model),
        ("routed", ModelRoutingMode.AUTO, cheap_model),
    ):
        initial = policy.select_initial(
            provider=provider,
            routing_mode=mode,
            requested_model=requested,
            max_allowed_tier=strong_tier,
        )
        second = policy.select_second(
            initial=initial,
            second_attempt_reason=SecondAttemptReason.CONTEXT_ESCALATION,
        )
        result[strategy] = (initial, second)
    return result


def _load_result(directory: Path | None, case_id: str) -> ResultDocument | None:
    if directory is None:
        return None
    for candidate in (
        directory / f"{case_id}.json",
        directory / case_id / "result.json",
    ):
        if candidate.is_file():
            return ResultDocument.model_validate_json(
                candidate.read_text(encoding="utf-8")
            )
    return None


def _result_metrics(result: ResultDocument | None) -> dict[str, Any] | None:
    if result is None:
        return None
    progression = result.model_progression
    context = result.context_progression
    diagnosis = result.diagnosis
    return {
        "initial_model": progression.initial_model if progression else None,
        "final_model": progression.final_model if progression else None,
        "initial_tier": (
            progression.initial_tier.value
            if progression and progression.initial_tier
            else None
        ),
        "final_tier": (
            progression.final_tier.value
            if progression and progression.final_tier
            else None
        ),
        "model_escalated": progression.model_escalated if progression else None,
        "model_calls": result.llm_usage.call_count,
        "context_progression": (
            " -> ".join(level.value for level in context.levels_used)
            if context
            else None
        ),
        "verification_status": diagnosis.verification_status if diagnosis else None,
        "input_tokens": result.llm_usage.input_tokens,
        "output_tokens": result.llm_usage.output_tokens,
        "total_tokens": result.llm_usage.total_tokens,
        "cost_usd": result.llm_usage.cost_usd,
        "cost_complete": result.llm_usage.cost_complete,
        "latency_seconds": result.timing.get("total_seconds"),
        "first_call_verified": (
            diagnosis.verification_status == "verified_first_attempt"
            if diagnosis
            else None
        ),
        "higher_tier_used": progression.tier_escalated if progression else None,
    }


def _metric(metrics: dict[str, Any] | None, key: str) -> Any:
    return metrics.get(key) if metrics is not None else None


def _rate(values: list[bool]) -> float | None:
    return round(sum(values) / len(values), 6) if values else None


def _mean_known(values) -> float | None:
    known = [value for value in values if value is not None]
    return round(mean(known), 6) if known else None


def _display(value: Any) -> str:
    if value is None:
        return "not collected"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return f"{value:.6f}"
    return str(value)


def _markdown(rows: list[dict[str, Any]], aggregates: dict[str, Any]) -> str:
    lines = [
        "# v0.9 model-routing comparison",
        "",
        "Offline rows show deterministic policy selections. Model calls, outcomes, usage, "
        "cost, and latency remain `not collected` without live result documents.",
        "",
        "| Case | Strategy | Policy initial | Policy schema escalation | Actual initial | Actual final | Model escalated | Calls | Context | Verification | Input | Output | Total | Cost | Latency |",
        "| --- | --- | --- | --- | --- | --- | --- | ---: | --- | --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in rows:
        lines.append(
            "| {case} | {strategy} | {policy_initial} | {policy_second} | {initial} | "
            "{final} | {escalated} | {calls} | {context} | {verification} | {input} | "
            "{output} | {total} | {cost} | {latency} |".format(
                case=row["case_id"],
                strategy=row["strategy"],
                policy_initial=row["policy_initial_model"],
                policy_second=row["policy_context_escalation_model"],
                initial=_display(row["initial_model"]),
                final=_display(row["final_model"]),
                escalated=_display(row["model_escalated"]),
                calls=_display(row["model_calls"]),
                context=_display(row["context_progression"]),
                verification=_display(row["verification_status"]),
                input=_display(row["input_tokens"]),
                output=_display(row["output_tokens"]),
                total=_display(row["total_tokens"]),
                cost=_display(row["cost_usd"]),
                latency=_display(row["latency_seconds"]),
            )
        )
    routed = aggregates["strategies"]["routed"]
    lines.extend(
        (
            "",
            "## Routed aggregates",
            "",
            f"- Overall verified-fix rate: {_display(routed['overall_verified_fix_rate'])}",
            f"- First-call verified rate: {_display(routed['first_call_verified_rate'])}",
            f"- Model escalation rate: {_display(routed['model_escalation_rate'])}",
            "- Higher-tier avoidance rate: "
            f"{_display(aggregates['strong_model_avoidance_rate'])}",
            f"- Cost per verified fix: {_display(routed['cost_per_verified_fix'])}",
            "",
        )
    )
    return "\n".join(lines)
