#!/usr/bin/env python3
"""Compare always-lightweight, always-schema, and progressive context."""

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
from semantic_terraform_agent.progressive_evaluation import (  # noqa: E402
    build_progressive_comparison,
    write_progressive_comparison,
)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description=(
            "Compare always-lightweight, always-schema, and v0.8 progressive "
            "minimal-then-schema context."
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
        / "v0.8-progressive-context",
    )
    result.add_argument("--always-lightweight-results", type=Path)
    result.add_argument("--always-schema-results", type=Path)
    result.add_argument("--progressive-results", type=Path)
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
        help="run all strategies with one fixed OpenRouter :free model",
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


def _run_live(args: argparse.Namespace) -> dict[str, Path]:
    if not args.model or not args.model.endswith(":free"):
        raise ValueError("--run-live requires a fixed OpenRouter model ending in :free")
    if not os.environ.get("OPENROUTER_API_KEY"):
        raise ValueError("--run-live requires OPENROUTER_API_KEY")

    directories = {
        "always_lightweight": args.output_dir
        / "raw-results"
        / "always-lightweight",
        "always_schema": args.output_dir / "raw-results" / "always-schema",
        "progressive": args.output_dir / "raw-results" / "progressive",
    }
    live_root = (args.live_repository_root or args.benchmark_root.parent).resolve()
    for case_id in CASE_IDS:
        case_dir = args.benchmark_root / case_id
        case = json.loads((case_dir / "case.json").read_text(encoding="utf-8"))
        error = case["error"]
        with tempfile.TemporaryDirectory(
            prefix="semantic-terraform-progressive-eval-"
        ) as temporary:
            temporary_root = Path(temporary)
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
                "verification_enabled": args.verify_patch,
                "max_repair_attempts": 1,
                "failed_stage": case.get("failed_stage"),
                "context_strategy": "deterministic-minimal-v1",
                "schema_strategy": "sliced",
            }
            for strategy, context_mode in (
                ("always_lightweight", "lightweight"),
                ("always_schema", "schema-aware"),
                ("progressive", "auto"),
            ):
                result = diagnose_repository(**common, context_mode=context_mode)
                _write_result(directories[strategy] / f"{case_id}.json", result)
    return directories


def main() -> int:
    args = parser().parse_args()
    try:
        directories = {
            "always_lightweight": args.always_lightweight_results,
            "always_schema": args.always_schema_results,
            "progressive": args.progressive_results,
        }
        if args.run_live:
            directories = _run_live(args)
        rows, aggregates = build_progressive_comparison(
            args.benchmark_root,
            result_directories={
                key: value for key, value in directories.items() if value is not None
            },
            model=args.model,
        )
        write_progressive_comparison(rows, aggregates, args.output_dir)
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    print(args.output_dir.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
