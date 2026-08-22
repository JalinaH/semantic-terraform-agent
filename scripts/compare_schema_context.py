#!/usr/bin/env python3
"""Generate the three-case v0.6 full-schema versus v0.7 sliced report."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from semantic_terraform_agent.evaluation import CASE_IDS  # noqa: E402
from semantic_terraform_agent.orchestration.diagnose import (  # noqa: E402
    diagnose_repository,
)
from semantic_terraform_agent.schema_evaluation import (  # noqa: E402
    build_schema_comparison,
    write_schema_comparison,
)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description=(
            "Compare v0.6 minimal Terraform context with full provider schema against "
            "v0.7 deterministic provider schema slicing."
        )
    )
    result.add_argument(
        "--benchmark-root",
        type=Path,
        default=PROJECT_ROOT.parent
        / "terraform-failure-benchmarks"
        / "diagnostic-packages",
    )
    result.add_argument(
        "--output-dir",
        type=Path,
        default=PROJECT_ROOT
        / "evaluation-results"
        / "v0.7-schema-comparison",
    )
    result.add_argument("--v0-6-results", type=Path)
    result.add_argument("--v0-7-results", type=Path)
    result.add_argument(
        "--live-repository-root",
        type=Path,
        help=(
            "complete terraform-failure-benchmarks checkout used by --run-live; "
            "defaults to the parent of --benchmark-root"
        ),
    )
    result.add_argument("--model")
    result.add_argument(
        "--run-live",
        action="store_true",
        help="run both strategies with one fixed OpenRouter :free model",
    )
    result.add_argument(
        "--verify-patch",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    return result


def _write_result(path: Path, result) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        result.model_dump_json(indent=2, exclude_none=False, by_alias=True) + "\n",
        encoding="utf-8",
    )


def _run_live(args: argparse.Namespace) -> tuple[Path, Path]:
    if not args.model or not args.model.endswith(":free"):
        raise ValueError("--run-live requires a fixed OpenRouter model ending in :free")
    if not os.environ.get("OPENROUTER_API_KEY"):
        raise ValueError("--run-live requires OPENROUTER_API_KEY")
    full_dir = args.output_dir / "raw-results" / "v0.6-full-schema"
    sliced_dir = args.output_dir / "raw-results" / "v0.7-sliced-schema"
    live_root = (args.live_repository_root or args.benchmark_root.parent).resolve()
    for case_id in CASE_IDS:
        case_dir = args.benchmark_root / case_id
        case = json.loads((case_dir / "case.json").read_text(encoding="utf-8"))
        error = case["error"]
        with tempfile.TemporaryDirectory(prefix="semantic-terraform-schema-eval-") as temp:
            temporary_root = Path(temp)
            collected_log = (
                live_root
                / "collected-runs"
                / f"terraform-logs-{case_id}"
                / "plan.stderr.log"
            )
            log_file = (
                collected_log
                if collected_log.is_file()
                else temporary_root / "failure.log"
            )
            if not collected_log.is_file():
                log_file.write_text(error.get("stderr", ""), encoding="utf-8")
            terraform_dir = Path("cases") / case_id
            source_diff = (case_dir / "git_diff.patch").read_text(encoding="utf-8")
            repository_prefix = terraform_dir.as_posix()
            live_diff = source_diff.replace(
                "a/terraform/", f"a/{repository_prefix}/"
            ).replace("b/terraform/", f"b/{repository_prefix}/")
            diff_file = temporary_root / "git_diff.patch"
            diff_file.write_text(live_diff, encoding="utf-8")
            common = {
                "repo_path": live_root,
                "terraform_dir": terraform_dir,
                "log_file": log_file,
                "diff_file": diff_file,
                "provider_name": "openrouter",
                "model": args.model,
                "context_mode": "schema-aware",
                "verification_enabled": args.verify_patch,
                "max_repair_attempts": 1,
                "failed_stage": case.get("failed_stage"),
                "context_strategy": "deterministic-minimal-v1",
            }
            full = diagnose_repository(**common, schema_strategy="full")
            sliced = diagnose_repository(**common, schema_strategy="sliced")
        _write_result(full_dir / f"{case_id}.json", full)
        _write_result(sliced_dir / f"{case_id}.json", sliced)
    return full_dir, sliced_dir


def main() -> int:
    args = parser().parse_args()
    try:
        if args.run_live:
            args.v0_6_results, args.v0_7_results = _run_live(args)
        rows = build_schema_comparison(
            args.benchmark_root,
            v0_6_results=args.v0_6_results,
            v0_7_results=args.v0_7_results,
            model=args.model,
        )
        write_schema_comparison(rows, args.output_dir)
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    print(args.output_dir.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
