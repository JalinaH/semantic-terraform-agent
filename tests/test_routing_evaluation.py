from __future__ import annotations

import json
from pathlib import Path

from semantic_terraform_agent.models import LLMProviderName, ModelTier
from semantic_terraform_agent.routing_evaluation import (
    aggregate_routing_results,
    build_routing_comparison,
    write_routing_comparison,
)


def test_offline_routing_comparison_records_policy_without_fake_run_metrics(
    tmp_path: Path,
) -> None:
    rows, aggregates = build_routing_comparison(
        provider=LLMProviderName.OPENROUTER,
        cheap_model="test/free-a:free",
        strong_model="test/economy-a:free",
        cheap_tier=ModelTier.FREE,
        strong_tier=ModelTier.ECONOMY,
    )

    assert len(rows) == 9
    routed = [row for row in rows if row["strategy"] == "routed"]
    assert all(row["policy_initial_model"] == "test/free-a:free" for row in routed)
    assert all(
        row["policy_context_escalation_model"] == "test/economy-a:free"
        for row in routed
    )
    assert all(row["model_calls"] is None for row in rows)
    assert all(row["cost_usd"] is None for row in rows)
    assert aggregates["strong_model_avoidance_rate"] is None
    assert aggregates["strategies"]["routed"]["cost_per_verified_fix"] is None

    output = tmp_path / "routing"
    write_routing_comparison(rows, aggregates, output)
    document = json.loads((output / "results.json").read_text(encoding="utf-8"))
    assert len(document["rows"]) == 9
    assert "provider-reported" in document["cost_policy"]
    assert (output / "results.jsonl").read_text(encoding="utf-8").count("\n") == 9
    assert "not collected" in (output / "comparison.md").read_text(encoding="utf-8")


def test_routing_aggregates_and_cost_per_verified_fix_require_complete_costs() -> None:
    rows = [
        {
            "strategy": "fixed_cheap",
            "model_calls": 1,
            "verification_status": "verified_first_attempt",
            "first_call_verified": True,
            "model_escalated": False,
            "higher_tier_used": False,
            "input_tokens": 100,
            "output_tokens": 20,
            "total_tokens": 120,
            "cost_usd": 0.0,
            "cost_complete": False,
            "latency_seconds": 1.0,
        },
        {
            "strategy": "fixed_strong",
            "model_calls": 1,
            "verification_status": "verified_first_attempt",
            "first_call_verified": True,
            "model_escalated": False,
            "higher_tier_used": False,
            "input_tokens": 120,
            "output_tokens": 25,
            "total_tokens": 145,
            "cost_usd": 0.01,
            "cost_complete": True,
            "latency_seconds": 1.2,
        },
        {
            "strategy": "routed",
            "model_calls": 1,
            "verification_status": "verified_first_attempt",
            "first_call_verified": True,
            "model_escalated": False,
            "higher_tier_used": False,
            "input_tokens": 90,
            "output_tokens": 20,
            "total_tokens": 110,
            "cost_usd": 0.0,
            "cost_complete": True,
            "latency_seconds": 0.8,
        },
        {
            "strategy": "routed",
            "model_calls": 2,
            "verification_status": "verification_failed",
            "first_call_verified": False,
            "model_escalated": True,
            "higher_tier_used": True,
            "input_tokens": 250,
            "output_tokens": 50,
            "total_tokens": 300,
            "cost_usd": 0.004,
            "cost_complete": True,
            "latency_seconds": 2.2,
        },
    ]

    aggregates = aggregate_routing_results(rows)

    routed = aggregates["strategies"]["routed"]
    assert routed["overall_verified_fix_rate"] == 0.5
    assert routed["first_call_verified_rate"] == 0.5
    assert routed["model_escalation_rate"] == 0.5
    assert routed["mean_input_tokens"] == 170
    assert routed["mean_output_tokens"] == 35
    assert routed["mean_reported_cost_usd"] == 0.002
    assert routed["cost_per_verified_fix"] == 0.004
    assert aggregates["strong_model_avoidance_rate"] == 0.5
    assert aggregates["strategies"]["fixed_cheap"]["mean_reported_cost_usd"] == 0.0
    assert aggregates["strategies"]["fixed_cheap"]["cost_per_verified_fix"] is None
