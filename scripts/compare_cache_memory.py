#!/usr/bin/env python3
"""Generate v1.0 cold/warm Verified Failure Memory comparison artifacts."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from semantic_terraform_agent.cache_evaluation import (  # noqa: E402
    build_cache_comparison,
    run_offline_cache_validation,
    write_cache_comparison,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cold-results", type=Path)
    parser.add_argument("--warm-results", type=Path)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=PROJECT_ROOT / "evaluation-results/v1.0-cache-memory",
    )
    args = parser.parse_args()
    try:
        rows, aggregates = build_cache_comparison(
            cold_results=args.cold_results, warm_results=args.warm_results
        )
        validations = run_offline_cache_validation()
        if not all(item["passed"] for item in validations):
            raise ValueError("one or more offline cache validations failed")
        write_cache_comparison(
            rows,
            aggregates,
            args.output_dir,
            offline_validations=validations,
        )
    except (OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    print(args.output_dir.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
