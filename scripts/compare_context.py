#!/usr/bin/env python3
"""Generate the three-case v0.5 versus v0.6 context report."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from semantic_terraform_agent.evaluation import (  # noqa: E402
    CASE_IDS,
    build_context_comparison,
    write_context_comparison,
)
from semantic_terraform_agent.orchestration.diagnose import (  # noqa: E402
    diagnose_repository,
)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Compare legacy v0.5 and deterministic-minimal v0.6 context."
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
        / "v0.6-context-comparison",
    )
    result.add_argument("--v0-5-results", type=Path)
    result.add_argument("--v0-6-results", type=Path)
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
        result.model_dump_json(indent=2, exclude_none=False) + "\n",
        encoding="utf-8",
    )


def _run_live(args: argparse.Namespace) -> tuple[Path, Path]:
    if not args.model or not args.model.endswith(":free"):
        raise ValueError("--run-live requires a fixed OpenRouter model ending in :free")
    if not os.environ.get("OPENROUTER_API_KEY"):
        raise ValueError("--run-live requires OPENROUTER_API_KEY")
    legacy_dir = args.output_dir / "raw-results" / "v0.5"
    minimal_dir = args.output_dir / "raw-results" / "v0.6"
    live_root = (args.live_repository_root or args.benchmark_root.parent).resolve()
    for case_id in CASE_IDS:
        case_dir = args.benchmark_root / case_id
        case = json.loads((case_dir / "case.json").read_text(encoding="utf-8"))
        error = case["error"]
        log_text = error.get("stderr", "")
        with tempfile.TemporaryDirectory(prefix="semantic-terraform-context-eval-") as temp:
            temp_root = Path(temp)
            collected_log = (
                live_root
                / "collected-runs"
                / f"terraform-logs-{case_id}"
                / "plan.stderr.log"
            )
            log_file = collected_log if collected_log.is_file() else temp_root / "failure.log"
            if not collected_log.is_file():
                log_file.write_text(log_text, encoding="utf-8")
            terraform_dir = Path("cases") / case_id
            source_diff = (case_dir / "git_diff.patch").read_text(encoding="utf-8")
            repository_prefix = terraform_dir.as_posix()
            live_diff = source_diff.replace(
                "a/terraform/", f"a/{repository_prefix}/"
            ).replace("b/terraform/", f"b/{repository_prefix}/")
            diff_file = temp_root / "git_diff.patch"
            diff_file.write_text(live_diff, encoding="utf-8")
            common = {
                "repo_path": live_root,
                "terraform_dir": terraform_dir,
                "log_file": log_file,
                "diff_file": diff_file,
                "provider_name": "openrouter",
                "model": args.model,
                "context_mode": "auto",
                "verification_enabled": args.verify_patch,
                "max_repair_attempts": 1,
                "failed_stage": case.get("failed_stage"),
            }
            legacy = diagnose_repository(
                **common, context_strategy="legacy-v0.5"
            )
            minimal = diagnose_repository(
                **common, context_strategy="deterministic-minimal-v1"
            )
        _write_result(legacy_dir / f"{case_id}.json", legacy)
        _write_result(minimal_dir / f"{case_id}.json", minimal)
    return legacy_dir, minimal_dir


def main() -> int:
    args = parser().parse_args()
    try:
        if args.run_live:
            args.v0_5_results, args.v0_6_results = _run_live(args)
        rows = build_context_comparison(
            args.benchmark_root,
            v0_5_results=args.v0_5_results,
            v0_6_results=args.v0_6_results,
            model=args.model,
        )
        write_context_comparison(rows, args.output_dir)
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    print(args.output_dir.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
