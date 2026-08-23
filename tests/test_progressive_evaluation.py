from __future__ import annotations

import json
from pathlib import Path

import pytest

from semantic_terraform_agent.progressive_evaluation import (
    _add_progressive_reductions,
    aggregate_progressive_results,
    build_progressive_comparison,
    write_progressive_comparison,
)


def test_offline_progressive_comparison_is_deterministic_and_never_fakes_usage(
    tmp_path: Path,
) -> None:
    benchmark_root = (
        Path(__file__).resolve().parents[2]
        / "terraform-failure-benchmarks"
        / "diagnostic-packages"
    )
    if not benchmark_root.is_dir():
        pytest.skip("terraform-failure-benchmarks checkout is unavailable")

    rows, aggregates = build_progressive_comparison(benchmark_root)

    assert len(rows) == 9
    assert [
        row["initial_prompt_characters"]
        for row in rows
        if row["strategy"] == "progressive"
    ] == [2_528, 2_652, 2_254]
    assert [
        row["schema_escalation_prompt_characters"]
        for row in rows
        if row["strategy"] == "progressive"
    ] == [3_630, 3_662, 3_095]
    assert all(row["input_tokens"] is None for row in rows)
    assert all(row["cost_usd"] is None for row in rows)
    assert all(
        row.get("input_token_reduction_vs_always_schema") is None
        for row in rows
        if row["strategy"] == "progressive"
    )
    assert aggregates["schema_escalation_rate"] is None
    assert aggregates["overall_verified_fix_rate"] is None

    output = tmp_path / "comparison"
    write_progressive_comparison(rows, aggregates, output)
    document = json.loads((output / "results.json").read_text(encoding="utf-8"))
    assert len(document["rows"]) == 9
    assert "provider-reported only" in document["token_policy"]
    assert (output / "results.jsonl").read_text(encoding="utf-8").count("\n") == 9
    assert "not collected" in (output / "comparison.md").read_text(encoding="utf-8")


def test_progressive_aggregates_use_only_live_rows_and_known_values() -> None:
    rows = [
        {
            "strategy": "always_lightweight",
            "model_calls": 1,
            "verification_status": "verified_first_attempt",
            "input_tokens": 100,
            "total_tokens": 130,
            "cost_usd": 0.0,
            "latency_seconds": 1.0,
        },
        {
            "strategy": "always_schema",
            "model_calls": 2,
            "verification_status": "verification_failed",
            "input_tokens": 400,
            "total_tokens": 500,
            "cost_usd": None,
            "latency_seconds": 4.0,
        },
        {
            "strategy": "progressive",
            "model_calls": 1,
            "verification_status": "verified_first_attempt",
            "schema_avoided": True,
            "escalated": False,
            "input_tokens": 90,
            "total_tokens": 120,
            "cost_usd": 0.0,
            "latency_seconds": 0.8,
        },
        {
            "strategy": "progressive",
            "model_calls": 2,
            "verification_status": "verified_after_retry",
            "schema_avoided": False,
            "escalated": True,
            "input_tokens": 250,
            "total_tokens": 310,
            "cost_usd": None,
            "latency_seconds": 2.2,
        },
        {
            "strategy": "progressive",
            "model_calls": None,
            "verification_status": None,
            "schema_avoided": None,
            "escalated": None,
            "input_tokens": None,
            "total_tokens": None,
            "cost_usd": None,
            "latency_seconds": None,
        },
    ]

    aggregates = aggregate_progressive_results(rows)

    assert aggregates["schema_escalation_rate"] == 0.5
    assert aggregates["schema_avoidance_rate"] == 0.5
    assert aggregates["minimal_first_pass_verification_rate"] == 0.5
    assert aggregates["overall_verified_fix_rate"] == 1.0
    assert aggregates["strategies"]["progressive"]["run_count"] == 2
    assert aggregates["strategies"]["progressive"]["mean_input_tokens"] == 170
    assert aggregates["strategies"]["progressive"]["mean_reported_cost_usd"] == 0.0


def test_paired_savings_require_same_live_provider_and_model() -> None:
    rows = [
        {
            "case_id": "case",
            "strategy": "always_schema",
            "provider": "openrouter",
            "model": "example/fixed:free",
            "requested_model": "example/fixed:free",
            "reported_model": "example/fixed:free",
            "model_calls": 1,
            "input_tokens": 1_000,
            "cost_usd": 0.01,
        },
        {
            "case_id": "case",
            "strategy": "progressive",
            "provider": "openrouter",
            "model": "example/fixed:free",
            "requested_model": "example/fixed:free",
            "reported_model": "example/fixed:free",
            "model_calls": 1,
            "input_tokens": 600,
            "cost_usd": 0.004,
        },
        {
            "case_id": "different-model",
            "strategy": "always_schema",
            "provider": "openrouter",
            "model": "example/a:free",
            "requested_model": "example/a:free",
            "reported_model": "example/a:free",
            "model_calls": 1,
            "input_tokens": 1_000,
            "cost_usd": 0.01,
        },
        {
            "case_id": "different-model",
            "strategy": "progressive",
            "provider": "openrouter",
            "model": "example/b:free",
            "requested_model": "example/b:free",
            "reported_model": "example/b:free",
            "model_calls": 1,
            "input_tokens": 500,
            "cost_usd": 0.005,
        },
    ]

    _add_progressive_reductions(rows)

    assert rows[1]["input_token_reduction_vs_always_schema"] == 0.4
    assert rows[1]["cost_reduction_vs_always_schema"] == 0.6
    assert rows[3]["input_token_reduction_vs_always_schema"] is None
    assert rows[3]["cost_reduction_vs_always_schema"] is None
