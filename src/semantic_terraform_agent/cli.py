"""Command-line entry point."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from pydantic import ValidationError

from semantic_terraform_agent.config import AgentError
from semantic_terraform_agent.models import ResultDocument
from semantic_terraform_agent.orchestration.diagnose import diagnose_repository


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="semantic-terraform-agent",
        description="Diagnose Terraform failures using repository, diff, and selective schema evidence.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    diagnose = subparsers.add_parser("diagnose", help="diagnose one Terraform failure")
    diagnose.add_argument("--repo-path", required=True, type=Path)
    diagnose.add_argument("--terraform-dir", required=True, type=Path)
    diagnose.add_argument("--log-file", required=True, type=Path)
    diagnose.add_argument("--diff-file", type=Path)
    diagnose.add_argument("--provider", choices=("gemini",), default="gemini")
    diagnose.add_argument("--model", default="gemini-2.5-flash")
    diagnose.add_argument(
        "--context-mode",
        choices=("lightweight", "schema-aware", "auto"),
        default="auto",
    )
    diagnose.add_argument("--output", required=True, type=Path)
    return parser


def _write_result(path: Path, result: ResultDocument) -> None:
    destination = path.expanduser().resolve(strict=False)
    if not destination.parent.is_dir():
        raise OSError(f"output directory does not exist: {destination.parent}")
    destination.write_text(
        result.model_dump_json(indent=2, exclude_none=True, by_alias=True) + "\n",
        encoding="utf-8",
    )


def _print_summary(result: ResultDocument, output: Path) -> None:
    assert result.diagnosis and result.context and result.failure
    affected = ", ".join(result.diagnosis.affected_resources) or "not identified"
    print(f"Diagnosis: {result.diagnosis.root_cause}")
    print(f"Affected resources: {affected}")
    print(
        f"Context: {result.context.selected_mode} ({result.context.selection_reason})"
    )
    print(
        f"Scores: model={result.diagnosis.model_confidence:.2f}, "
        f"evidence={result.diagnosis.evidence_score:.2f}"
    )
    print(f"Result: {output.expanduser().resolve(strict=False)}")
    if result.warnings:
        print(f"Warnings: {len(result.warnings)}")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command != "diagnose":
        parser.error("a command is required")
    try:
        result = diagnose_repository(
            repo_path=args.repo_path,
            terraform_dir=args.terraform_dir,
            log_file=args.log_file,
            diff_file=args.diff_file,
            provider_name=args.provider,
            model=args.model,
            context_mode=args.context_mode,
        )
        _write_result(args.output, result)
    except (AgentError, OSError, ValidationError, ValueError) as exc:
        error_result = ResultDocument(status="error", error=str(exc), warnings=[])
        try:
            _write_result(args.output, error_result)
        except OSError:
            pass
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    _print_summary(result, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
