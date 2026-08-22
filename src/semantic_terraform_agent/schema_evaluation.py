"""Deterministic v0.6 full-schema versus v0.7 sliced-schema evaluation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from semantic_terraform_agent.context import ContextBuilder, slice_schema_records
from semantic_terraform_agent.context.builder import minimal_diff, minimal_sources
from semantic_terraform_agent.evaluation import CASE_IDS, _load_case
from semantic_terraform_agent.models import DiagnosisRequest, ResultDocument
from semantic_terraform_agent.reasoning.prompts import build_prompt_parts


EXPECTED_SCHEMA_PATHS = {
    "dynamodb-key-schema-failure": {
        "block.attributes.hash_key",
        "block.block_types.attribute.block.attributes.name",
        "block.block_types.attribute.block.attributes.type",
    },
    "ebs-throughput-volume-type-failure": {
        "block.attributes.throughput",
        "block.attributes.type",
    },
    "s3-bucket-naming-conflict-failure": {
        "block.attributes.bucket",
        "block.attributes.bucket_prefix",
    },
}


def build_schema_comparison(
    benchmark_root: Path,
    *,
    v0_6_results: Path | None = None,
    v0_7_results: Path | None = None,
    model: str | None = None,
) -> list[dict[str, Any]]:
    """Build three comparable rows without inferring live usage from characters."""
    rows: list[dict[str, Any]] = []
    for case_id in CASE_IDS:
        full_request, sliced_request = _schema_request_pair(
            benchmark_root / case_id
        )
        full_prompt = build_prompt_parts(full_request)
        sliced_prompt = build_prompt_parts(sliced_request)
        full_result = _load_result(v0_6_results, case_id)
        sliced_result = _load_result(v0_7_results, case_id)
        full_metrics = _result_metrics(full_result)
        sliced_metrics = _result_metrics(sliced_result)
        full_prompt_characters = _prefer(
            full_metrics, "prompt_characters", full_prompt.prompt_characters
        )
        sliced_prompt_characters = _prefer(
            sliced_metrics, "prompt_characters", sliced_prompt.prompt_characters
        )
        full_input = _metric(full_metrics, "input_tokens")
        sliced_input = _metric(sliced_metrics, "input_tokens")
        full_cost = _metric(full_metrics, "cost_usd")
        sliced_cost = _metric(sliced_metrics, "cost_usd")
        optimization = sliced_request.schema_optimization
        assert optimization is not None
        selected_paths = [
            path
            for schema_slice in sliced_request.schema_slices
            for path in schema_slice.manifest.selected_paths
        ]
        expected_paths = EXPECTED_SCHEMA_PATHS[case_id]
        selected_set = set(selected_paths)
        row = {
            "case_id": case_id,
            "resource_type": (
                sliced_request.schema_slices[0].resource_type
                if sliced_request.schema_slices
                else None
            ),
            "provider": (
                sliced_request.schema_slices[0].provider_source
                if sliced_request.schema_slices
                else None
            ),
            "provider_version": (
                sliced_request.schema_slices[0].provider_version
                if sliced_request.schema_slices
                else None
            ),
            "model": (
                _metric(sliced_metrics, "model")
                or _metric(full_metrics, "model")
                or model
            ),
            "context_mode": "schema-aware",
            "v0_6_schema_strategy": "full_schema",
            "v0_7_schema_strategy": "deterministic_schema_slice_v1",
            "v0_6_full_schema_characters": optimization.full_schema_characters,
            "v0_7_selected_schema_characters": (
                optimization.selected_schema_characters
            ),
            "schema_character_reduction_ratio": optimization.reduction_ratio,
            "v0_6_prompt_characters": full_prompt_characters,
            "v0_7_prompt_characters": sliced_prompt_characters,
            "prompt_character_reduction_ratio": _reduction(
                full_prompt_characters, sliced_prompt_characters
            ),
            "v0_6_input_tokens": full_input,
            "v0_7_input_tokens": sliced_input,
            "input_token_reduction_ratio": _reduction(full_input, sliced_input),
            "v0_6_output_tokens": _metric(full_metrics, "output_tokens"),
            "v0_7_output_tokens": _metric(sliced_metrics, "output_tokens"),
            "v0_6_total_tokens": _metric(full_metrics, "total_tokens"),
            "v0_7_total_tokens": _metric(sliced_metrics, "total_tokens"),
            "v0_6_cost_usd": full_cost,
            "v0_7_cost_usd": sliced_cost,
            "cost_reduction_ratio": _reduction(full_cost, sliced_cost),
            "v0_6_verification_status": _metric(
                full_metrics, "verification_status"
            ),
            "v0_7_verification_status": _metric(
                sliced_metrics, "verification_status"
            ),
            "v0_6_repair_used": _metric(full_metrics, "repair_used"),
            "v0_7_repair_used": _metric(sliced_metrics, "repair_used"),
            "v0_6_latency_seconds": _metric(full_metrics, "latency_seconds"),
            "v0_7_latency_seconds": _metric(sliced_metrics, "latency_seconds"),
            "selected_schema_paths": selected_paths,
            "selected_path_count": len(selected_paths),
            "schema_fallback_used": optimization.fallback_used,
            "schema_fallback_reason": optimization.fallback_reason,
            "gates": {
                "expected_paths_retained": expected_paths <= selected_set,
                "unrelated_paths_excluded": selected_set <= expected_paths,
                "selected_schema_is_valid_json": all(
                    _is_json_serializable(item.selected_schema)
                    for item in sliced_request.schema_slices
                ),
                "parent_structure_retained": all(
                    _selected_paths_exist(item.selected_schema, item.manifest.selected_paths)
                    for item in sliced_request.schema_slices
                ),
                "prompt_is_smaller": (
                    sliced_prompt_characters < full_prompt_characters
                ),
                "diagnosis_structurally_valid": _metric(
                    sliced_metrics, "diagnosis_structurally_valid"
                ),
                "candidate_patch_generated": _metric(
                    sliced_metrics, "candidate_patch_generated"
                ),
                "verification_success_did_not_regress": _verification_gate(
                    full_metrics, sliced_metrics
                ),
            },
        }
        rows.append(row)
    return rows


def write_schema_comparison(rows: list[dict[str, Any]], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    document = {
        "comparison": (
            "v0.6 deterministic minimal Terraform context with full resource schema "
            "versus v0.7 deterministic provider schema slicing"
        ),
        "schema_character_definition": (
            "Full schema characters and selected schema characters are compact, "
            "sorted JSON character counts before prompt serialization."
        ),
        "token_policy": (
            "Token and cost values are provider-reported only; null means no comparable "
            "live run was supplied. Character reductions are not token estimates."
        ),
        "rows": rows,
    }
    (output_dir / "comparison.json").write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (output_dir / "comparison.jsonl").write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )
    (output_dir / "README.md").write_text(
        _markdown_report(rows), encoding="utf-8"
    )


def _schema_request_pair(case_dir: Path) -> tuple[DiagnosisRequest, DiagnosisRequest]:
    case, layout, sources, diff, failure, resources, context, schemas = _load_case(
        case_dir
    )
    schema_context = context.model_copy(
        update={
            "selected_mode": "schema-aware",
            "selection_reason": "forced schema-aware comparison",
        }
    )
    diagnosis_context = ContextBuilder().build(
        repository=layout,
        failure=failure,
        diff=diff,
        all_sources=sources,
        detected_resources=resources,
        mode="schema-aware",
    )
    common = {
        "failure": failure,
        "resources": resources,
        "relevant_sources": minimal_sources(diagnosis_context),
        "git_diff": minimal_diff(diagnosis_context),
        "context": schema_context,
        "schemas": schemas,
        "terraform_version": case.get("terraform_version"),
        "diagnosis_context": diagnosis_context,
    }
    full_slices, full_optimization = slice_schema_records(
        schemas,
        failure=failure,
        diagnosis_context=diagnosis_context,
        strategy="full",
    )
    sliced, sliced_optimization = slice_schema_records(
        schemas,
        failure=failure,
        diagnosis_context=diagnosis_context,
        strategy="sliced",
    )
    return (
        DiagnosisRequest(
            **common,
            schema_slices=full_slices,
            schema_optimization=full_optimization,
            schema_strategy="full",
        ),
        DiagnosisRequest(
            **common,
            schema_slices=sliced,
            schema_optimization=sliced_optimization,
            schema_strategy="sliced",
        ),
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
    call = result.llm_calls[0] if result.llm_calls else None
    diagnosis = result.diagnosis
    final = diagnosis.repair or diagnosis.initial if diagnosis is not None else None
    return {
        "model": call.reported_model or call.requested_model if call else None,
        "prompt_characters": (
            result.context_telemetry.prompt_characters
            if result.context_telemetry
            else (call.prompt_characters if call else None)
        ),
        "input_tokens": result.llm_usage.input_tokens,
        "output_tokens": result.llm_usage.output_tokens,
        "total_tokens": result.llm_usage.total_tokens,
        "cost_usd": result.llm_usage.cost_usd,
        "verification_status": (
            diagnosis.verification_status if diagnosis is not None else None
        ),
        "repair_used": diagnosis.repair is not None if diagnosis else None,
        "latency_seconds": result.timing.get("total_seconds"),
        "diagnosis_structurally_valid": diagnosis is not None and final is not None,
        "candidate_patch_generated": bool(final and final.suggested_patch.strip()),
    }


def _selected_paths_exist(schema: dict[str, Any], paths: list[str]) -> bool:
    for path in paths:
        current: Any = schema
        for part in path.split("."):
            if not isinstance(current, dict) or part not in current:
                return False
            current = current[part]
    return True


def _is_json_serializable(value: Any) -> bool:
    try:
        json.dumps(value)
    except (TypeError, ValueError):
        return False
    return True


def _metric(metrics: dict[str, Any] | None, key: str) -> Any:
    return metrics.get(key) if metrics is not None else None


def _prefer(
    metrics: dict[str, Any] | None, key: str, fallback: int
) -> int:
    value = _metric(metrics, key)
    return value if isinstance(value, int) else fallback


def _reduction(old: int | float | None, new: int | float | None) -> float | None:
    if old is None or new is None or old == 0:
        return None
    return round((old - new) / old, 6)


def _verification_gate(
    full: dict[str, Any] | None,
    sliced: dict[str, Any] | None,
) -> bool | None:
    if full is None or sliced is None:
        return None
    old = full.get("verification_status")
    new = sliced.get("verification_status")
    if old is None or new is None:
        return None
    verified = {"verified_first_attempt", "verified_after_retry"}
    return old not in verified or new in verified


def _markdown_report(rows: list[dict[str, Any]]) -> str:
    lines = [
        "# v0.7 provider-schema comparison",
        "",
        "Character measurements are deterministic pre-call measurements. Token and cost "
        "values are provider-reported only; `not collected` is never estimated.",
        "",
        "| Case | Strategy | Schema chars | Prompt chars | Input tokens | Output tokens | Total tokens | Cost | Latency | Verification | Repair |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |",
    ]
    for row in rows:
        for version, strategy, schema_key in (
            ("v0_6", "full_schema", "v0_6_full_schema_characters"),
            ("v0_7", "deterministic_schema_slice_v1", "v0_7_selected_schema_characters"),
        ):
            lines.append(
                "| {case} | {strategy} | {schema} | {prompt} | {input} | {output} | "
                "{total} | {cost} | {latency} | {verification} | {repair} |".format(
                    case=row["case_id"],
                    strategy=strategy,
                    schema=_display(row[schema_key]),
                    prompt=_display(row[f"{version}_prompt_characters"]),
                    input=_display(row[f"{version}_input_tokens"]),
                    output=_display(row[f"{version}_output_tokens"]),
                    total=_display(row[f"{version}_total_tokens"]),
                    cost=_display(row[f"{version}_cost_usd"]),
                    latency=_display(row[f"{version}_latency_seconds"]),
                    verification=_display(row[f"{version}_verification_status"]),
                    repair=_display(row[f"{version}_repair_used"]),
                )
            )
    lines.extend(
        (
            "",
            "## Reductions and deterministic gates",
            "",
            "| Case | Schema reduction | Prompt reduction | Token reduction | Cost reduction | Paths | Expected paths | Unrelated paths | Parent structure | Verification regression |",
            "| --- | ---: | ---: | ---: | ---: | ---: | --- | --- | --- | --- |",
        )
    )
    for row in rows:
        gates = row["gates"]
        lines.append(
            "| {case} | {schema} | {prompt} | {tokens} | {cost} | {paths} | {expected} | "
            "{unrelated} | {parents} | {verification} |".format(
                case=row["case_id"],
                schema=_percentage(row["schema_character_reduction_ratio"]),
                prompt=_percentage(row["prompt_character_reduction_ratio"]),
                tokens=_percentage(row["input_token_reduction_ratio"]),
                cost=_percentage(row["cost_reduction_ratio"]),
                paths=_display(row["selected_path_count"]),
                expected=_display(gates["expected_paths_retained"]),
                unrelated=_display(gates["unrelated_paths_excluded"]),
                parents=_display(gates["parent_structure_retained"]),
                verification=_display(
                    gates["verification_success_did_not_regress"]
                ),
            )
        )
    lines.extend(
        (
            "",
            "Live diagnosis, verification, token, cost, and latency fields remain `not "
            "collected` until comparable result JSON is supplied.",
            "",
        )
    )
    return "\n".join(lines)


def _display(value: Any) -> str:
    if value is None:
        return "not collected"
    if isinstance(value, bool):
        return "pass" if value else "fail"
    if isinstance(value, int):
        return f"{value:,}"
    if isinstance(value, float):
        return f"{value:.6f}"
    return str(value)


def _percentage(value: float | None) -> str:
    return f"{value:.1%}" if value is not None else "not collected"
