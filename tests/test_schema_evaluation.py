from __future__ import annotations

import json
from pathlib import Path

import pytest

from semantic_terraform_agent.schema_evaluation import (
    EXPECTED_SCHEMA_PATHS,
    build_schema_comparison,
    write_schema_comparison,
)


def test_three_case_offline_schema_comparison_has_golden_paths_and_gates(
    tmp_path: Path,
) -> None:
    benchmark_root = (
        Path(__file__).resolve().parents[2]
        / "terraform-failure-benchmarks"
        / "diagnostic-packages"
    )
    if not benchmark_root.is_dir():
        pytest.skip("terraform-failure-benchmarks checkout is unavailable")

    rows = build_schema_comparison(benchmark_root)

    assert [row["v0_6_full_schema_characters"] for row in rows] == [
        8_303,
        2_170,
        11_744,
    ]
    assert [row["v0_7_selected_schema_characters"] for row in rows] == [
        392,
        239,
        244,
    ]
    assert [row["v0_6_prompt_characters"] for row in rows] == [
        10_995,
        4_982,
        14_157,
    ]
    assert [row["v0_7_prompt_characters"] for row in rows] == [
        3_059,
        3_026,
        2_632,
    ]
    assert all(row["input_token_reduction_ratio"] is None for row in rows)
    assert all(row["v0_6_cost_usd"] is None for row in rows)
    for row in rows:
        assert set(row["selected_schema_paths"]) == EXPECTED_SCHEMA_PATHS[
            row["case_id"]
        ]
        assert row["schema_fallback_used"] is False
        assert row["gates"]["expected_paths_retained"] is True
        assert row["gates"]["unrelated_paths_excluded"] is True
        assert row["gates"]["selected_schema_is_valid_json"] is True
        assert row["gates"]["parent_structure_retained"] is True
        assert row["gates"]["prompt_is_smaller"] is True

    output = tmp_path / "comparison"
    write_schema_comparison(rows, output)
    payload = json.loads((output / "comparison.json").read_text(encoding="utf-8"))
    assert len(payload["rows"]) == 3
    assert "not collected" in (output / "README.md").read_text(encoding="utf-8")
    assert (output / "comparison.jsonl").read_text(encoding="utf-8").count("\n") == 3
