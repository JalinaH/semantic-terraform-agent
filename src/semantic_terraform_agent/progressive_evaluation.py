"""Three-strategy progressive-context evaluation and aggregate metrics."""

from __future__ import annotations

import json
from pathlib import Path
from statistics import mean
from typing import Any

from semantic_terraform_agent.context import ContextBuilder, slice_schema_records
from semantic_terraform_agent.context.builder import minimal_diff, minimal_sources
from semantic_terraform_agent.evaluation import CASE_IDS, _load_case
from semantic_terraform_agent.models import (
    ContextLevel,
    ContextSelection,
    DiagnosisRequest,
    EscalationDecision,
    ModelDiagnosis,
    RepairRequest,
    ResultDocument,
    SecondAttemptReason,
    VerificationAttempt,
    VerificationCommand,
    VerificationCommands,
    VerificationErrorRelation,
)
from semantic_terraform_agent.reasoning.prompts import (
    build_prompt_parts,
    build_repair_prompt_parts,
)


STRATEGIES = ("always_lightweight", "always_schema", "progressive")


def build_progressive_comparison(
    benchmark_root: Path,
    *,
    result_directories: dict[str, Path] | None = None,
    model: str | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    result_directories = result_directories or {}
    for case_id in CASE_IDS:
        minimal, schema, escalation = _offline_prompts(benchmark_root / case_id)
        offline_characters = {
            "always_lightweight": minimal.prompt_characters,
            "always_schema": schema.prompt_characters,
            "progressive": minimal.prompt_characters,
        }
        for strategy in STRATEGIES:
            result = _load_result(result_directories.get(strategy), case_id)
            metrics = _result_metrics(result)
            row = {
                "case_id": case_id,
                "strategy": strategy,
                "provider": _metric(metrics, "provider"),
                "model": _metric(metrics, "model") or model,
                "requested_model": _metric(metrics, "requested_model") or model,
                "reported_model": _metric(metrics, "reported_model"),
                "model_calls": _metric(metrics, "model_calls"),
                "schema_retrieved": (
                    _metric(metrics, "schema_retrieved")
                    if metrics is not None
                    else (
                        False
                        if strategy == "always_lightweight"
                        else (True if strategy == "always_schema" else None)
                    )
                ),
                "schema_avoided": _metric(metrics, "schema_avoided"),
                "schema_avoided_initial_call": strategy in {
                    "always_lightweight",
                    "progressive",
                },
                "escalated": _metric(metrics, "escalated"),
                "second_attempt_reason": _metric(
                    metrics, "second_attempt_reason"
                ),
                "initial_prompt_characters": (
                    _metric(metrics, "initial_prompt_characters")
                    or offline_characters[strategy]
                ),
                "schema_escalation_prompt_characters": (
                    escalation.prompt_characters
                    if strategy == "progressive"
                    else None
                ),
                "always_schema_prompt_characters": schema.prompt_characters,
                "input_tokens": _metric(metrics, "input_tokens"),
                "output_tokens": _metric(metrics, "output_tokens"),
                "total_tokens": _metric(metrics, "total_tokens"),
                "cost_usd": _metric(metrics, "cost_usd"),
                "latency_seconds": _metric(metrics, "latency_seconds"),
                "verification_status": _metric(metrics, "verification_status"),
                "repair_used": _metric(metrics, "repair_used"),
            }
            rows.append(row)
    _add_progressive_reductions(rows)
    return rows, aggregate_progressive_results(rows)


def aggregate_progressive_results(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_strategy: dict[str, dict[str, Any]] = {}
    for strategy in STRATEGIES:
        strategy_rows = [row for row in rows if row["strategy"] == strategy]
        live_rows = [row for row in strategy_rows if row.get("model_calls") is not None]
        verified = {
            "verified_first_attempt",
            "verified_after_retry",
        }
        by_strategy[strategy] = {
            "run_count": len(live_rows),
            "overall_verified_fix_rate": _rate(
                [row.get("verification_status") in verified for row in live_rows]
            ),
            "mean_input_tokens": _mean_known(
                row.get("input_tokens") for row in live_rows
            ),
            "mean_total_tokens": _mean_known(
                row.get("total_tokens") for row in live_rows
            ),
            "mean_reported_cost_usd": _mean_known(
                row.get("cost_usd") for row in live_rows
            ),
            "mean_latency_seconds": _mean_known(
                row.get("latency_seconds") for row in live_rows
            ),
        }
    progressive = [
        row
        for row in rows
        if row["strategy"] == "progressive" and row.get("model_calls") is not None
    ]
    return {
        "schema_escalation_rate": _rate(
            [bool(row.get("escalated")) for row in progressive]
        ),
        "schema_avoidance_rate": _rate(
            [bool(row.get("schema_avoided")) for row in progressive]
        ),
        "minimal_first_pass_verification_rate": _rate(
            [
                row.get("verification_status") == "verified_first_attempt"
                for row in progressive
            ]
        ),
        "overall_verified_fix_rate": by_strategy["progressive"][
            "overall_verified_fix_rate"
        ],
        "strategies": by_strategy,
    }


def write_progressive_comparison(
    rows: list[dict[str, Any]],
    aggregates: dict[str, Any],
    output_dir: Path,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    document = {
        "comparison": "always-lightweight versus always-schema versus progressive",
        "character_policy": (
            "Prompt characters are deterministic pre-provider measurements. The offline "
            "schema-escalation prompt uses a fixed synthetic diagnosis and verification "
            "failure solely to measure rendering; it is not a correctness result."
        ),
        "token_policy": (
            "Tokens and cost are provider-reported only. Null values are not inferred "
            "from prompt characters."
        ),
        "aggregates": aggregates,
        "rows": rows,
    }
    (output_dir / "results.json").write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (output_dir / "results.jsonl").write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )
    (output_dir / "comparison.md").write_text(
        _markdown(rows, aggregates), encoding="utf-8"
    )


def _offline_prompts(case_dir: Path):
    case, layout, sources, diff, failure, resources, _, schemas = _load_case(case_dir)
    diagnosis_context = ContextBuilder().build(
        repository=layout,
        failure=failure,
        diff=diff,
        all_sources=sources,
        detected_resources=resources,
        mode="lightweight",
    )
    common = {
        "failure": failure,
        "resources": resources,
        "relevant_sources": minimal_sources(diagnosis_context),
        "git_diff": minimal_diff(diagnosis_context),
        "terraform_version": case.get("terraform_version"),
        "diagnosis_context": diagnosis_context,
        "schema_strategy": "sliced",
    }
    minimal = DiagnosisRequest(
        **common,
        context=ContextSelection(
            requested_mode="auto",
            selected_mode="progressive",
            selection_reason="Offline progressive initial prompt measurement.",
        ),
        schemas=[],
        context_level=ContextLevel.MINIMAL,
    )
    slices, optimization = slice_schema_records(
        schemas,
        failure=failure,
        diagnosis_context=diagnosis_context,
        strategy="sliced",
    )
    schema = DiagnosisRequest(
        **common,
        context=ContextSelection(
            requested_mode="schema-aware",
            selected_mode="schema-aware",
            selection_reason="Offline always-schema prompt measurement.",
        ),
        schemas=schemas,
        schema_slices=slices,
        schema_optimization=optimization,
        context_level=ContextLevel.SCHEMA,
    )
    progressive_schema = schema.model_copy(
        update={
            "context": minimal.context,
        }
    )
    model_diagnosis = ModelDiagnosis(
        root_cause="Deterministic offline placeholder diagnosis.",
        affected_resources=[failure.resource_address] if failure.resource_address else [],
        violated_constraint="Initial candidate did not pass semantic verification.",
        suggested_patch="--- a/terraform/main.tf\n+++ b/terraform/main.tf\n@@ -1 +1 @@\n-old\n+new",
        confidence=0.5,
        evidence=[{"source": "terraform_error", "detail": failure.summary}],
    )
    command = VerificationCommand(
        command=["terraform", "plan"],
        status="failed",
        exit_code=1,
        stderr=f"Error: {failure.summary}\n{failure.detail}",
    )
    attempt = VerificationAttempt(
        attempt=1,
        patch=model_diagnosis.suggested_patch,
        status="failed",
        failed_stage="plan",
        commands=VerificationCommands(plan=command),
        temporary_copy_cleaned=True,
        warnings=["synthetic offline semantic verification failure"],
    )
    decision = EscalationDecision(
        action="escalate",
        should_escalate=True,
        should_repair=False,
        from_level=ContextLevel.MINIMAL,
        to_level=ContextLevel.SCHEMA,
        reason_code="verification_semantic_failure",
        reason="Synthetic offline escalation rendering measurement.",
        signals=["verification failed at plan"],
        verification_error_relation=VerificationErrorRelation.NEW_SEMANTIC_FAILURE,
    )
    escalation = RepairRequest(
        original=progressive_schema,
        previous_diagnosis=model_diagnosis,
        failed_attempt=attempt,
        second_attempt_reason=SecondAttemptReason.CONTEXT_ESCALATION,
        escalation_decision=decision,
    )
    return (
        build_prompt_parts(minimal),
        build_prompt_parts(schema),
        build_repair_prompt_parts(escalation),
    )


def _load_result(directory: Path | None, case_id: str) -> ResultDocument | None:
    if directory is None:
        return None
    for candidate in (
        directory / f"{case_id}.json",
        directory / case_id / "result.json",
    ):
        if candidate.is_file():
            return ResultDocument.model_validate_json(
                candidate.read_text(encoding="utf-8")
            )
    return None


def _result_metrics(result: ResultDocument | None) -> dict[str, Any] | None:
    if result is None:
        return None
    progression = result.context_progression
    diagnosis = result.diagnosis
    call = result.llm_calls[0] if result.llm_calls else None
    return {
        "provider": call.provider.value if call else None,
        "model": call.reported_model or call.requested_model if call else None,
        "requested_model": call.requested_model if call else None,
        "reported_model": call.reported_model if call else None,
        "model_calls": result.llm_usage.call_count,
        "schema_retrieved": progression.schema_retrieved if progression else None,
        "schema_avoided": progression.schema_avoided if progression else None,
        "escalated": progression.escalated if progression else None,
        "second_attempt_reason": (
            progression.second_attempt_reason.value if progression else None
        ),
        "initial_prompt_characters": (
            result.context_telemetry.calls[0].prompt_characters
            if result.context_telemetry and result.context_telemetry.calls
            else (call.prompt_characters if call else None)
        ),
        "input_tokens": result.llm_usage.input_tokens,
        "output_tokens": result.llm_usage.output_tokens,
        "total_tokens": result.llm_usage.total_tokens,
        "cost_usd": result.llm_usage.cost_usd,
        "latency_seconds": result.timing.get("total_seconds"),
        "verification_status": diagnosis.verification_status if diagnosis else None,
        "repair_used": diagnosis.repair is not None if diagnosis else None,
    }


def _add_progressive_reductions(rows: list[dict[str, Any]]) -> None:
    """Add paired savings only for comparable live provider/model results."""
    for progressive in (row for row in rows if row["strategy"] == "progressive"):
        progressive["input_token_reduction_vs_always_schema"] = None
        progressive["cost_reduction_vs_always_schema"] = None
        always_schema = next(
            (
                row
                for row in rows
                if row["case_id"] == progressive["case_id"]
                and row["strategy"] == "always_schema"
            ),
            None,
        )
        if not _comparable(progressive, always_schema):
            continue
        schema_tokens = always_schema.get("input_tokens")
        progressive_tokens = progressive.get("input_tokens")
        if schema_tokens is not None and schema_tokens > 0 and progressive_tokens is not None:
            progressive["input_token_reduction_vs_always_schema"] = round(
                (schema_tokens - progressive_tokens) / schema_tokens,
                6,
            )
        schema_cost = always_schema.get("cost_usd")
        progressive_cost = progressive.get("cost_usd")
        if schema_cost is not None and schema_cost > 0 and progressive_cost is not None:
            progressive["cost_reduction_vs_always_schema"] = round(
                (schema_cost - progressive_cost) / schema_cost,
                6,
            )


def _comparable(
    progressive: dict[str, Any], always_schema: dict[str, Any] | None
) -> bool:
    if always_schema is None:
        return False
    return bool(
        progressive.get("model_calls") is not None
        and always_schema.get("model_calls") is not None
        and progressive.get("provider")
        and progressive.get("provider") == always_schema.get("provider")
        and progressive.get("requested_model")
        and progressive.get("requested_model")
        == always_schema.get("requested_model")
        and (
            not progressive.get("reported_model")
            and not always_schema.get("reported_model")
            or progressive.get("reported_model")
            == always_schema.get("reported_model")
        )
    )


def _metric(metrics: dict[str, Any] | None, key: str) -> Any:
    return metrics.get(key) if metrics is not None else None


def _rate(values: list[bool]) -> float | None:
    return round(sum(values) / len(values), 6) if values else None


def _mean_known(values) -> float | None:
    known = [value for value in values if value is not None]
    return round(mean(known), 6) if known else None


def _display(value: Any) -> str:
    if value is None:
        return "not collected"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, int):
        return f"{value:,}"
    if isinstance(value, float):
        return f"{value:.6f}"
    return str(value)


def _markdown(rows: list[dict[str, Any]], aggregates: dict[str, Any]) -> str:
    lines = [
        "# v0.8 progressive-context comparison",
        "",
        "Offline prompt characters are deterministic. Token, cost, latency, escalation, "
        "and verification aggregates remain `not collected` without live results.",
        "",
        "| Case | Strategy | Model | Initial prompt chars | Escalation prompt chars | Initial schema avoided | Calls | Schema retrieved | Escalated | Input tokens | Output tokens | Total tokens | Cost | Latency | Verification | Repair |",
        "| --- | --- | --- | ---: | ---: | --- | ---: | --- | --- | ---: | ---: | ---: | ---: | ---: | --- | --- |",
    ]
    for row in rows:
        lines.append(
            "| {case} | {strategy} | {model} | {initial} | {escalation} | "
            "{initial_schema_avoided} | {calls} | {schema} | {escalated} | {input} | "
            "{output} | {total} | {cost} | {latency} | {verification} | {repair} |".format(
                case=row["case_id"],
                strategy=row["strategy"],
                model=_display(row["model"]),
                initial=_display(row["initial_prompt_characters"]),
                escalation=_display(row["schema_escalation_prompt_characters"]),
                initial_schema_avoided=_display(
                    row["schema_avoided_initial_call"]
                ),
                calls=_display(row["model_calls"]),
                schema=_display(row["schema_retrieved"]),
                escalated=_display(row["escalated"]),
                input=_display(row["input_tokens"]),
                output=_display(row["output_tokens"]),
                total=_display(row["total_tokens"]),
                cost=_display(row["cost_usd"]),
                latency=_display(row["latency_seconds"]),
                verification=_display(row["verification_status"]),
                repair=_display(row["repair_used"]),
            )
        )
    lines.extend(
        (
            "",
            "## Progressive aggregates",
            "",
            f"- Schema escalation rate: {_display(aggregates['schema_escalation_rate'])}",
            f"- Schema avoidance rate: {_display(aggregates['schema_avoidance_rate'])}",
            "- Minimal first-pass verification rate: "
            f"{_display(aggregates['minimal_first_pass_verification_rate'])}",
            "- Overall progressive verified-fix rate: "
            f"{_display(aggregates['overall_verified_fix_rate'])}",
            "",
        )
    )
    return "\n".join(lines)
