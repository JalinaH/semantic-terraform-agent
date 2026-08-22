#!/usr/bin/env python3
"""Compare fixed lower-tier, fixed higher-tier, and routed model policies."""

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
from semantic_terraform_agent.config import ModelRoutingError  # noqa: E402
from semantic_terraform_agent.models import (  # noqa: E402
    LLMProviderName,
    ModelTier,
)
from semantic_terraform_agent.orchestration.diagnose import (  # noqa: E402
    diagnose_repository,
)
from semantic_terraform_agent.routing_evaluation import (  # noqa: E402
    _pair_registry,
    build_routing_comparison,
    write_routing_comparison,
)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Compare fixed-cheap, fixed-strong, and routed model policy."
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
        default=PROJECT_ROOT / "evaluation-results" / "v0.9-model-routing",
    )
    result.add_argument("--fixed-cheap-results", type=Path)
    result.add_argument("--fixed-strong-results", type=Path)
    result.add_argument("--routed-results", type=Path)
    result.add_argument("--provider", choices=("openrouter", "gemini"), default="openrouter")
    result.add_argument("--cheap-model", default="example/cheap-model:free")
    result.add_argument("--strong-model", default="example/strong-model:free")
    result.add_argument(
        "--cheap-tier",
        choices=("free", "economy", "balanced", "premium"),
        default="free",
    )
    result.add_argument(
        "--strong-tier",
        choices=("free", "economy", "balanced", "premium"),
        default="economy",
    )
    result.add_argument("--live-repository-root", type=Path)
    result.add_argument("--run-live", action="store_true")
    result.add_argument("--allow-paid-models", action="store_true")
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
    if args.provider != "openrouter":
        raise ValueError("--run-live currently supports provider=openrouter")
    if not os.environ.get("OPENROUTER_API_KEY"):
        raise ValueError("--run-live requires OPENROUTER_API_KEY")
    if not args.allow_paid_models and not all(
        model.endswith(":free") for model in (args.cheap_model, args.strong_model)
    ):
        raise ValueError(
            "live models must end in :free unless --allow-paid-models is explicit"
        )
    provider = LLMProviderName(args.provider)
    cheap_tier = ModelTier(args.cheap_tier)
    strong_tier = ModelTier(args.strong_tier)
    registry = _pair_registry(
        provider,
        args.cheap_model,
        args.strong_model,
        cheap_tier,
        strong_tier,
    )
    directories = {
        "fixed_cheap": args.output_dir / "raw-results" / "fixed-cheap",
        "fixed_strong": args.output_dir / "raw-results" / "fixed-strong",
        "routed": args.output_dir / "raw-results" / "routed",
    }
    live_root = (args.live_repository_root or args.benchmark_root.parent).resolve()
    for case_id in CASE_IDS:
        case_dir = args.benchmark_root / case_id
        case = json.loads((case_dir / "case.json").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory(
            prefix="semantic-terraform-routing-eval-"
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
                log_file.write_text(
                    case["error"].get("stderr", ""), encoding="utf-8"
                )
            terraform_dir = Path("cases") / case_id
            source_diff = (case_dir / "git_diff.patch").read_text(encoding="utf-8")
            prefix = terraform_dir.as_posix()
            diff_file = temporary_root / "git_diff.patch"
            diff_file.write_text(
                source_diff.replace("a/terraform/", f"a/{prefix}/").replace(
                    "b/terraform/", f"b/{prefix}/"
                ),
                encoding="utf-8",
            )
            common = {
                "repo_path": live_root,
                "terraform_dir": terraform_dir,
                "log_file": log_file,
                "diff_file": diff_file,
                "provider_name": provider,
                "context_mode": "auto",
                "verification_enabled": args.verify_patch,
                "max_repair_attempts": 1,
                "failed_stage": case.get("failed_stage"),
                "schema_strategy": "sliced",
                "model_registry": registry,
            }
            settings = {
                "fixed_cheap": {
                    "model": args.cheap_model,
                    "model_routing": "fixed",
                    "max_model_tier": args.strong_tier,
                },
                "fixed_strong": {
                    "model": args.strong_model,
                    "model_routing": "fixed",
                    "max_model_tier": args.strong_tier,
                },
                "routed": {
                    "model": args.cheap_model,
                    "model_routing": "auto",
                    "max_model_tier": args.strong_tier,
                },
            }
            for strategy, strategy_settings in settings.items():
                result = diagnose_repository(**common, **strategy_settings)
                _write_result(directories[strategy] / f"{case_id}.json", result)
    return directories


def main() -> int:
    args = parser().parse_args()
    try:
        directories = {
            "fixed_cheap": args.fixed_cheap_results,
            "fixed_strong": args.fixed_strong_results,
            "routed": args.routed_results,
        }
        if args.run_live:
            directories = _run_live(args)
        rows, aggregates = build_routing_comparison(
            provider=LLMProviderName(args.provider),
            cheap_model=args.cheap_model,
            strong_model=args.strong_model,
            cheap_tier=ModelTier(args.cheap_tier),
            strong_tier=ModelTier(args.strong_tier),
            result_directories={
                key: value for key, value in directories.items() if value is not None
            },
        )
        write_routing_comparison(rows, aggregates, args.output_dir)
    except (
        OSError,
        ValueError,
        KeyError,
        json.JSONDecodeError,
        ModelRoutingError,
    ) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    print(args.output_dir.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
