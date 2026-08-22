from __future__ import annotations

import json
from pathlib import Path

import pytest

from semantic_terraform_agent.evaluation import (
    build_context_comparison,
    write_context_comparison,
)


def test_three_case_offline_comparison_matches_captured_v0_5_baseline(
    tmp_path: Path,
) -> None:
    benchmark_root = (
        Path(__file__).resolve().parents[2]
        / "terraform-failure-benchmarks"
        / "diagnostic-packages"
    )
    if not benchmark_root.is_dir():
        pytest.skip("terraform-failure-benchmarks checkout is unavailable")
    rows = build_context_comparison(benchmark_root)
    assert [row["v0_5_prompt_characters"] for row in rows] == [
        22_854,
        3_177,
        31_501,
    ]
    assert all(
        row["v0_6_prompt_characters"] < row["v0_5_prompt_characters"]
        for row in rows
    )
    assert all(row["v0_5_input_tokens"] is None for row in rows)
    assert all(row["input_token_reduction_ratio"] is None for row in rows)
    assert all(
        row["gates"]["minimal_prompt_contains_exact_diagnostic_terms"]
        for row in rows
    )

    output = tmp_path / "comparison"
    write_context_comparison(rows, output)
    payload = json.loads((output / "comparison.json").read_text(encoding="utf-8"))
    assert len(payload["rows"]) == 3
    assert payload["rows"][0]["v0_5_cost_usd"] is None
    assert "not collected" in (output / "README.md").read_text(encoding="utf-8")
    assert (output / "comparison.jsonl").read_text(encoding="utf-8").count("\n") == 3
