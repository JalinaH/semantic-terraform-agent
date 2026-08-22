"""Deterministic v0.5 versus v0.6 context comparison helpers."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from semantic_terraform_agent.collectors.failure_log import parse_failure_log
from semantic_terraform_agent.collectors.git_diff import (
    DiffData,
    parse_changed_files,
    parse_changed_lines,
)
from semantic_terraform_agent.collectors.repository import (
    RepositoryLayout,
    read_source_files,
)
from semantic_terraform_agent.context import ContextBuilder
from semantic_terraform_agent.context.builder import minimal_diff, minimal_sources
from semantic_terraform_agent.context.legacy import legacy_relevant_sources
from semantic_terraform_agent.models import (
    ContextSelection,
    DiagnosisRequest,
    ResultDocument,
    SchemaRecord,
)
from semantic_terraform_agent.reasoning.prompts import build_prompt_parts
from semantic_terraform_agent.terraform.resources import detect_resources


CASE_IDS = (
    "dynamodb-key-schema-failure",
    "ebs-throughput-volume-type-failure",
    "s3-bucket-naming-conflict-failure",
)
KNOWN_ROOT_CAUSE_TERMS = {
    "dynamodb-key-schema-failure": ("customer_id", "customerId"),
    "ebs-throughput-volume-type-failure": ("throughput", "gp2"),
    "s3-bucket-naming-conflict-failure": ("bucket", "bucket_prefix"),
}
_LEGACY_ARGUMENT_SIGNAL = re.compile(
    r"(?:argument|attribute|field|parameter)\s+[\"'`]?([A-Za-z_][A-Za-z0-9_-]*)|"
    r"[\"'`]([A-Za-z_][A-Za-z0-9_-]*)[\"'`]\s+(?:is|required|cannot|must|conflicts)",
    re.IGNORECASE,
)
_LEGACY_AMBIGUOUS_SIGNAL = re.compile(
    r"provider produced|provider validation|invalid configuration|invalid value|"
    r"failed validation|unexpected state|inconsistent result|unsupported combination",
    re.IGNORECASE,
)


def build_context_comparison(
    benchmark_root: Path,
    *,
    v0_5_results: Path | None = None,
    v0_6_results: Path | None = None,
    model: str | None = None,
) -> list[dict[str, Any]]:
    """Build three comparison rows without inventing unavailable live metrics."""
    rows: list[dict[str, Any]] = []
    for case_id in CASE_IDS:
        legacy, minimal, expected_address = _prompt_pair(
            benchmark_root / case_id
        )
        legacy_result = _load_result(v0_5_results, case_id)
        minimal_result = _load_result(v0_6_results, case_id)
        legacy_metrics = _result_metrics(
            legacy_result, case_id, expected_address
        )
        minimal_metrics = _result_metrics(
            minimal_result, case_id, expected_address
        )
        legacy_characters = (
            legacy_metrics["prompt_characters"]
            if legacy_metrics is not None
            else legacy.prompt_characters
        )
        minimal_characters = (
            minimal_metrics["prompt_characters"]
            if minimal_metrics is not None
            else minimal.prompt_characters
        )
        legacy_tokens = _metric(legacy_metrics, "input_tokens")
        minimal_tokens = _metric(minimal_metrics, "input_tokens")
        legacy_cost = _metric(legacy_metrics, "cost_usd")
        minimal_cost = _metric(minimal_metrics, "cost_usd")
        terms = KNOWN_ROOT_CAUSE_TERMS[case_id]
        row = {
            "case_id": case_id,
            "model": (
                _metric(minimal_metrics, "model")
                or _metric(legacy_metrics, "model")
                or model
            ),
            "context_mode": minimal_metrics["context_mode"]
            if minimal_metrics is not None
            else _context_mode(benchmark_root / case_id),
            "v0_5_prompt_characters": legacy_characters,
            "v0_6_prompt_characters": minimal_characters,
            "character_reduction_ratio": _reduction(
                legacy_characters, minimal_characters
            ),
            "v0_5_input_tokens": legacy_tokens,
            "v0_6_input_tokens": minimal_tokens,
            "input_token_reduction_ratio": _reduction(
                legacy_tokens, minimal_tokens
            ),
            "v0_5_output_tokens": _metric(legacy_metrics, "output_tokens"),
            "v0_6_output_tokens": _metric(minimal_metrics, "output_tokens"),
            "v0_5_total_tokens": _metric(legacy_metrics, "total_tokens"),
            "v0_6_total_tokens": _metric(minimal_metrics, "total_tokens"),
            "v0_5_cost_usd": legacy_cost,
            "v0_6_cost_usd": minimal_cost,
            "cost_reduction_ratio": _reduction(legacy_cost, minimal_cost),
            "v0_5_verification_status": _metric(
                legacy_metrics, "verification_status"
            ),
            "v0_6_verification_status": _metric(
                minimal_metrics, "verification_status"
            ),
            "v0_5_repair_used": _metric(legacy_metrics, "repair_used"),
            "v0_6_repair_used": _metric(minimal_metrics, "repair_used"),
            "v0_5_latency_seconds": _metric(
                legacy_metrics, "latency_seconds"
            ),
            "v0_6_latency_seconds": _metric(
                minimal_metrics, "latency_seconds"
            ),
            "root_cause_correctness": _metric(
                minimal_metrics, "root_cause_correctness"
            ),
            "patch_correctness": _metric(
                minimal_metrics, "patch_correctness"
            ),
            "gates": {
                "minimal_prompt_contains_exact_diagnostic_terms": all(
                    term in minimal.user for term in terms
                ),
                "diagnosis_structurally_valid": _metric(
                    minimal_metrics, "diagnosis_structurally_valid"
                ),
                "affected_resource_correct": _metric(
                    minimal_metrics, "affected_resource_correct"
                ),
                "known_root_cause_terms_retained": _metric(
                    minimal_metrics, "root_cause_correctness"
                ),
                "candidate_patch_generated": _metric(
                    minimal_metrics, "candidate_patch_generated"
                ),
                "verification_success_did_not_regress": _verification_gate(
                    legacy_metrics, minimal_metrics
                ),
            },
        }
        rows.append(row)
    return rows


def write_context_comparison(
    rows: list[dict[str, Any]], output_dir: Path
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    document = {
        "comparison": "v0.5 legacy prompt versus v0.6 deterministic minimal prompt",
        "source_character_definition": (
            "Prompt characters are measured before provider serialization. Terraform "
            "source reduction compares all Terraform source discoverable in the selected "
            "working directory with exact selected HCL."
        ),
        "token_policy": (
            "Token and cost values are provider-reported only; null means no comparable "
            "live result was supplied. Character ratios are not token estimates."
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


def _prompt_pair(case_dir: Path):
    case, layout, all_sources, diff, failure, resources, context, schemas = (
        _load_case(case_dir)
    )
    legacy_request = DiagnosisRequest(
        failure=failure,
        resources=resources,
        relevant_sources=legacy_relevant_sources(
            all_sources, resources, diff.changed_files, failure.referenced_file
        ),
        git_diff=diff.text,
        context=context,
        schemas=schemas,
        terraform_version=case.get("terraform_version"),
    )
    diagnosis_context = ContextBuilder().build(
        repository=layout,
        failure=failure,
        diff=diff,
        all_sources=all_sources,
        detected_resources=resources,
        mode=context.selected_mode,
    )
    minimal_request = DiagnosisRequest(
        failure=failure,
        resources=resources,
        relevant_sources=minimal_sources(diagnosis_context),
        git_diff=minimal_diff(diagnosis_context),
        context=context,
        schemas=schemas,
        terraform_version=case.get("terraform_version"),
        diagnosis_context=diagnosis_context,
    )
    expected_address = failure.resource_address or (
        resources[0].address if resources else None
    )
    return (
        build_prompt_parts(legacy_request),
        build_prompt_parts(minimal_request),
        expected_address,
    )


def _load_case(case_dir: Path):
    case = json.loads((case_dir / "case.json").read_text(encoding="utf-8"))
    terraform_root = case_dir / "terraform"
    terraform_files = tuple(
        sorted(
            path.relative_to(case_dir).as_posix()
            for path in terraform_root.iterdir()
            if path.is_file()
            and (path.suffix == ".tf" or path.name.endswith(".tf.json"))
        )
    )
    layout = RepositoryLayout(
        root=case_dir.resolve(),
        terraform_root=terraform_root.resolve(),
        terraform_dir="terraform",
        terraform_files=terraform_files,
    )
    all_sources = read_source_files(layout, layout.terraform_files)
    diff_text = (case_dir / "git_diff.patch").read_text(encoding="utf-8")
    diff = DiffData(
        text=diff_text,
        source=str(case_dir / "git_diff.patch"),
        comparison="packaged corrected-to-failing diff",
        changed_files=parse_changed_files(diff_text, layout),
        changed_lines=parse_changed_lines(diff_text, layout),
    )
    error = case["error"]
    # The v0.5 baseline used the actual failing stderr artifact, not successful
    # stage output or the Terraform plan preamble from stdout.
    raw_log = error.get("stderr", "")
    failure = parse_failure_log(raw_log).model_copy(
        update={"stage": case.get("failed_stage", "unknown")}
    )
    resources = detect_resources(
        failure, all_sources, diff.changed_files, diff.changed_lines
    )
    context = _v0_6_auto_context(failure, resources)
    schema = json.loads(
        (case_dir / "resource_schema.json").read_text(encoding="utf-8")
    )
    metadata = json.loads(
        (case_dir / "metadata.json").read_text(encoding="utf-8")
    )
    provider = metadata.get("provider", case.get("provider", {}))
    schemas = [
        SchemaRecord(
            resource_type=case["resource_type"],
            provider_source=provider.get("address") or provider.get("source"),
            provider_version=provider.get("version"),
            extraction_status="ok",
            schema=schema,
        )
    ]
    return case, layout, all_sources, diff, failure, resources, context, schemas


def _v0_6_auto_context(failure, resources) -> ContextSelection:
    """Preserve the historical pre-v0.8 policy for comparison fixtures only."""
    selected = "schema-aware"
    reason = "Historical v0.6 auto-mode schema selection."
    combined = f"{failure.summary}\n{failure.detail}"
    if (
        len(resources) == 1
        and resources[0].confidence == "high"
        and _LEGACY_ARGUMENT_SIGNAL.search(combined)
        and not (
            _LEGACY_AMBIGUOUS_SIGNAL.search(combined)
            and not _LEGACY_ARGUMENT_SIGNAL.search(combined)
        )
    ):
        selected = "lightweight"
        reason = "Historical v0.6 auto-mode lightweight selection."
    return ContextSelection(
        requested_mode="auto",
        selected_mode=selected,
        selection_reason=reason,
    )


def _context_mode(case_dir: Path) -> str:
    return _load_case(case_dir)[6].selected_mode


def _load_result(directory: Path | None, case_id: str) -> ResultDocument | None:
    if directory is None:
        return None
    candidates = (
        directory / f"{case_id}.json",
        directory / case_id / "result.json",
    )
    for path in candidates:
        if path.is_file():
            return ResultDocument.model_validate_json(path.read_text(encoding="utf-8"))
    return None


def _result_metrics(
    result: ResultDocument | None,
    case_id: str,
    expected_address: str | None,
) -> dict[str, Any] | None:
    if result is None:
        return None
    diagnosis = result.diagnosis
    context = result.context
    call = result.llm_calls[0] if result.llm_calls else None
    final = diagnosis.repair or diagnosis.initial if diagnosis is not None else None
    verification_status = diagnosis.verification_status if diagnosis else None
    verified = verification_status in {
        "verified_first_attempt",
        "verified_after_retry",
    }
    diagnosis_text = ""
    if final is not None:
        diagnosis_text = "\n".join(
            (final.root_cause, final.violated_constraint, final.suggested_patch)
        )
    terms = KNOWN_ROOT_CAUSE_TERMS[case_id]
    affected = final.affected_resources if final is not None else []
    return {
        "model": call.reported_model or call.requested_model if call else None,
        "context_mode": context.selected_mode if context else None,
        "prompt_characters": (
            result.context_telemetry.prompt_characters
            if result.context_telemetry
            else (call.prompt_characters if call else None)
        ),
        "input_tokens": result.llm_usage.input_tokens,
        "output_tokens": result.llm_usage.output_tokens,
        "total_tokens": result.llm_usage.total_tokens,
        "cost_usd": result.llm_usage.cost_usd,
        "verification_status": verification_status,
        "repair_used": diagnosis.repair is not None if diagnosis else None,
        "latency_seconds": result.timing.get("total_seconds"),
        "diagnosis_structurally_valid": diagnosis is not None and final is not None,
        "affected_resource_correct": (
            any(
                item == expected_address
                or (expected_address is not None and item.endswith(expected_address))
                for item in affected
            )
            if expected_address
            else None
        ),
        "root_cause_correctness": all(term in diagnosis_text for term in terms),
        "candidate_patch_generated": bool(
            final and final.suggested_patch.strip()
        ),
        "patch_correctness": verified,
    }


def _metric(metrics: dict[str, Any] | None, name: str) -> Any:
    return metrics.get(name) if metrics is not None else None


def _reduction(old: float | int | None, new: float | int | None) -> float | None:
    if old is None or new is None or old == 0:
        return None
    return round((old - new) / old, 6)


def _verification_gate(
    legacy: dict[str, Any] | None, minimal: dict[str, Any] | None
) -> bool | None:
    if legacy is None or minimal is None:
        return None
    old = legacy.get("verification_status")
    new = minimal.get("verification_status")
    if old is None or new is None:
        return None
    verified = {"verified_first_attempt", "verified_after_retry"}
    return old not in verified or new in verified


def _markdown_report(rows: list[dict[str, Any]]) -> str:
    lines = [
        "# v0.6 context comparison",
        "",
        "Character measurements are deterministic pre-call measurements. Token and cost "
        "values are provider-reported only; `not collected` is never estimated from characters.",
        "",
        "| Case | Strategy | Prompt chars | Input tokens | Output tokens | Total tokens | Cost | Latency | Verification | Repair |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |",
    ]
    for row in rows:
        for version, strategy in (
            ("v0_5", "legacy_v0_5"),
            ("v0_6", "deterministic_minimal_v1"),
        ):
            lines.append(
                "| {case} | {strategy} | {prompt} | {input_tokens} | "
                "{output_tokens} | {total_tokens} | {cost} | {latency} | "
                "{verification} | {repair} |".format(
                    case=row["case_id"],
                    strategy=strategy,
                    prompt=_display(row[f"{version}_prompt_characters"]),
                    input_tokens=_display(row[f"{version}_input_tokens"]),
                    output_tokens=_display(row[f"{version}_output_tokens"]),
                    total_tokens=_display(row[f"{version}_total_tokens"]),
                    cost=_display(row[f"{version}_cost_usd"]),
                    latency=_display(row[f"{version}_latency_seconds"]),
                    verification=_display(row[f"{version}_verification_status"]),
                    repair=_display(row[f"{version}_repair_used"]),
                )
            )
    lines.extend(
        (
            "",
            "## Reductions and gates",
            "",
            "| Case | Character reduction | Token reduction | Cost reduction | Context evidence gate | Verification regression gate |",
            "| --- | ---: | ---: | ---: | --- | --- |",
        )
    )
    for row in rows:
        gates = row["gates"]
        lines.append(
            "| {case} | {chars} | {tokens} | {cost} | {evidence} | {verification} |".format(
                case=row["case_id"],
                chars=_percentage(row["character_reduction_ratio"]),
                tokens=_percentage(row["input_token_reduction_ratio"]),
                cost=_percentage(row["cost_reduction_ratio"]),
                evidence=_display(
                    gates["minimal_prompt_contains_exact_diagnostic_terms"]
                ),
                verification=_display(
                    gates["verification_success_did_not_regress"]
                ),
            )
        )
    lines.extend(
        (
            "",
            "The verification, diagnosis, patch, token, cost, and latency gates remain "
            "`not collected` until comparable live result JSON is supplied.",
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
