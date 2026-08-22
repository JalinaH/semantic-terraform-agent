"""Command-line entry point."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from pydantic import ValidationError

from semantic_terraform_agent.config import (
    DEFAULT_GEMINI_MODEL,
    AgentError,
    InputError,
    ProviderError,
    provider_names,
)
from semantic_terraform_agent.ci import CIRenderContext, render_pr_comment, render_step_summary
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
    diagnose.add_argument(
        "--failed-stage",
        choices=("init", "fmt", "validate", "plan", "apply", "unknown"),
        help="explicit Terraform stage that produced the supplied failure log",
    )
    diagnose.add_argument("--provider", choices=provider_names(), default="gemini")
    diagnose.add_argument(
        "--model",
        help=(
            "provider model ID; defaults to gemini-2.5-flash for Gemini and is "
            "required for OpenRouter"
        ),
    )
    diagnose.add_argument(
        "--context-mode",
        choices=("lightweight", "schema-aware", "auto"),
        default="auto",
    )
    diagnose.add_argument(
        "--context-strategy",
        choices=("deterministic-minimal-v1", "legacy-v0.5"),
        default="deterministic-minimal-v1",
        help=argparse.SUPPRESS,
    )
    diagnose.add_argument(
        "--schema-strategy",
        choices=("sliced", "full"),
        default="sliced",
        help=argparse.SUPPRESS,
    )
    diagnose.add_argument(
        "--verify-patch",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="verify the candidate in a temporary copy (default: enabled)",
    )
    diagnose.add_argument(
        "--max-repair-attempts",
        type=int,
        choices=(0, 1),
        default=1,
        help=(
            "maximum second model attempts (repair or context escalation) after "
            "verification failure (default: 1)"
        ),
    )
    diagnose.add_argument("--output", required=True, type=Path)

    render = subparsers.add_parser(
        "render-ci", help="render a bounded CI summary or pull-request comment"
    )
    render.add_argument("--result", required=True, type=Path)
    render.add_argument("--format", required=True, choices=("summary", "comment"))
    render.add_argument("--repository", required=True)
    render.add_argument("--commit", required=True)
    render.add_argument("--terraform-dir", required=True)
    render.add_argument(
        "--failed-stage",
        required=True,
        choices=("init", "fmt", "validate", "plan", "apply", "unknown"),
    )
    render.add_argument("--diff-comparison")
    render.add_argument("--output", required=True, type=Path)
    render.add_argument("--append", action="store_true")
    return parser


def _write_result(path: Path, result: ResultDocument) -> None:
    destination = path.expanduser().resolve(strict=False)
    if not destination.parent.is_dir():
        raise OSError(f"output directory does not exist: {destination.parent}")
    destination.write_text(
        result.model_dump_json(indent=2, exclude_none=False, by_alias=True) + "\n",
        encoding="utf-8",
    )


def _stage_label(stage: str) -> str:
    return {
        "patch_check": "git apply --check",
        "patch_apply": "git apply",
        "fmt": "terraform fmt",
        "init": "terraform init",
        "validate": "terraform validate",
        "plan": "terraform plan",
    }[stage]


def _print_summary(result: ResultDocument, output: Path) -> None:
    assert result.diagnosis and result.context and result.failure
    diagnosis = result.diagnosis
    final_candidate = diagnosis.repair or diagnosis.initial
    affected = ", ".join(final_candidate.affected_resources) or "not identified"
    print("Root cause:")
    print(f"  {final_candidate.root_cause}")
    print("Affected resources:")
    print(f"  {affected}")
    print(
        f"Context: {result.context.selected_mode} ({result.context.selection_reason})"
    )
    print(
        f"Scores: model={diagnosis.model_confidence:.2f}, "
        f"evidence={diagnosis.evidence_score:.2f}"
    )
    first = diagnosis.attempts[0]
    first_description = first.status
    if first.failed_stage:
        first_description = f"verification failed at {_stage_label(first.failed_stage)}"
    print("Initial patch:")
    print(f"  {first_description}")
    print("Repair attempt:")
    print("  generated" if diagnosis.repair else "  not generated")
    progression = result.context_progression
    if progression is not None:
        print("Progressive context:")
        print(f"  Initial level:     {progression.initial_level.value}")
        print(f"  Final level:       {progression.final_level.value}")
        print(f"  Escalated:         {'yes' if progression.escalated else 'no'}")
        print(f"  Reason:            {progression.reason_code or 'not reported'}")
        print(
            "  Schema retrieved:  "
            f"{'yes' if progression.schema_retrieved else 'no'}"
        )
        print(f"  Second attempt:    {progression.second_attempt_reason.value}")
    print("Final verification:")
    print(f"  {diagnosis.verification.status.replace('_', ' ').upper()}")
    if diagnosis.verification.reason:
        print("Reason:")
        print(f"  {diagnosis.verification.reason}")
    final_attempt = diagnosis.attempts[-1]
    for name, label in (
        ("fmt", "terraform fmt"),
        ("init", "terraform init"),
        ("terraform_validate", "terraform validate"),
        ("plan", "terraform plan"),
    ):
        command = getattr(final_attempt.commands, name)
        if command is not None:
            display = {
                "passed": "PASS",
                "failed": "FAIL",
                "skipped": "SKIP",
                "error": "ERROR",
            }[command.status]
            print(f"{label:<18} {display}")
    usage = result.llm_usage
    requested_model = result.llm_calls[0].requested_model if result.llm_calls else "not reported"
    provider = result.llm_calls[0].provider.value if result.llm_calls else "not reported"
    provider_display = {"openrouter": "OpenRouter", "gemini": "Gemini"}.get(
        provider, provider
    )
    print("LLM usage:")
    print(f"  Provider:       {provider_display}")
    print(f"  Model:          {requested_model}")
    print(f"  Calls:          {usage.call_count}")
    print(f"  Input tokens:   {_format_tokens(usage.input_tokens)}")
    print(f"  Output tokens:  {_format_tokens(usage.output_tokens)}")
    print(f"  Total tokens:   {_format_tokens(usage.total_tokens)}")
    print(
        f"  Cost:           ${usage.cost_usd:.6f}"
        if usage.cost_usd is not None and usage.cost_complete
        else (
            f"  Cost:           ${usage.cost_usd:.6f} (incomplete)"
            if usage.cost_usd is not None
            else "  Cost:           not reported"
        )
    )
    optimization = result.context_optimization
    manifest = result.context_manifest
    schema_optimization = result.schema_optimization
    if (optimization is not None and manifest is not None) or schema_optimization:
        print("Context optimization:")
    if optimization is not None and manifest is not None:
        available_files = optimization.available_source_file_count
        selected_files = optimization.selected_source_file_count
        available_resources = optimization.available_resource_count
        selected_resources = optimization.selected_resource_count
        available_characters = optimization.available_source_characters
        selected_characters = optimization.selected_source_characters
        reduction = optimization.character_reduction_ratio
        print(f"  Source strategy:     {optimization.strategy}")
        print(
            "  Terraform files:     "
            f"{_format_count(available_files)} available / "
            f"{_format_count(selected_files)} included"
        )
        print(
            "  Resources:           "
            f"{_format_count(available_resources)} available / "
            f"{_format_count(selected_resources)} included"
        )
        print(f"  Supporting symbols:  {len(manifest.included_symbols)}")
        if available_characters is not None and selected_characters is not None:
            print(
                "  Source characters:   "
                f"{available_characters:,} → {selected_characters:,}"
            )
        else:
            print("  Source characters:   not comparable")
        print(
            f"  Source reduction:    {reduction:.1%}"
            if reduction is not None
            else "  Source reduction:    not comparable"
        )
    if schema_optimization is not None:
        print(f"  Schema strategy:     {schema_optimization.strategy}")
        print(
            "  Provider schema:     "
            f"{schema_optimization.full_schema_characters:,} → "
            f"{schema_optimization.selected_schema_characters:,} characters"
        )
        print(
            f"  Schema reduction:    {schema_optimization.reduction_ratio:.1%}"
            if schema_optimization.reduction_ratio is not None
            else "  Schema reduction:    not comparable"
        )
        print(f"  Selected paths:      {schema_optimization.selected_path_count}")
        if schema_optimization.fallback_used:
            reason = schema_optimization.fallback_reason or "unspecified"
            print(f"  Schema fallback:     yes ({reason})")
        else:
            print("  Schema fallback:     no")
    elif result.context is not None and result.context.selected_mode in {
        "lightweight",
        "progressive",
    }:
        print("  Provider schema:     not used")
    print(f"Result: {output.expanduser().resolve(strict=False)}")
    if result.warnings:
        print(f"Warnings: {len(result.warnings)}")


def _format_tokens(value: int | None) -> str:
    return f"{value:,}" if value is not None else "not reported"


def _format_count(value: int | None) -> str:
    return f"{value:,}" if value is not None else "unknown"


def _render_ci(args: argparse.Namespace) -> int:
    result = ResultDocument.model_validate_json(
        args.result.expanduser().resolve(strict=True).read_text(encoding="utf-8")
    )
    context = CIRenderContext(
        repository=args.repository,
        commit=args.commit,
        terraform_dir=args.terraform_dir,
        failed_stage=args.failed_stage,
        diff_comparison=args.diff_comparison,
    )
    rendered = (
        render_pr_comment(result, context)
        if args.format == "comment"
        else render_step_summary(result, context)
    )
    destination = args.output.expanduser().resolve(strict=False)
    if not destination.parent.is_dir():
        raise OSError(f"output directory does not exist: {destination.parent}")
    mode = "a" if args.append else "w"
    with destination.open(mode, encoding="utf-8") as output:
        output.write(rendered)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "render-ci":
        try:
            return _render_ci(args)
        except (OSError, ValidationError, ValueError) as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 2
    if args.command != "diagnose":
        parser.error("a command is required")
    try:
        if args.model is None and args.provider != "gemini":
            raise InputError("--model is required when --provider openrouter is selected")
        model = args.model or DEFAULT_GEMINI_MODEL
        result = diagnose_repository(
            repo_path=args.repo_path,
            terraform_dir=args.terraform_dir,
            log_file=args.log_file,
            diff_file=args.diff_file,
            provider_name=args.provider,
            model=model,
            context_mode=args.context_mode,
            verification_enabled=args.verify_patch,
            max_repair_attempts=args.max_repair_attempts,
            failed_stage=args.failed_stage,
            context_strategy=args.context_strategy,
            schema_strategy=args.schema_strategy,
        )
        _write_result(args.output, result)
    except (AgentError, OSError, ValidationError, ValueError) as exc:
        error_result = ResultDocument(
            status="error",
            error=str(exc),
            error_code=exc.category if isinstance(exc, ProviderError) else None,
            warnings=[],
        )
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
