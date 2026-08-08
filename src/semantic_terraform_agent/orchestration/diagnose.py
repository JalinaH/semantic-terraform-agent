"""End-to-end orchestration for a single local diagnosis."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Literal

from semantic_terraform_agent.collectors.failure_log import collect_failure_log
from semantic_terraform_agent.collectors.git_diff import collect_diff
from semantic_terraform_agent.collectors.repository import (
    discover_repository,
    read_source_files,
)
from semantic_terraform_agent.models import (
    Diagnosis,
    DiagnosisRequest,
    RepositoryInfo,
    ResultDocument,
)
from semantic_terraform_agent.reasoning.base import LLMProvider
from semantic_terraform_agent.reasoning.gemini import GeminiProvider
from semantic_terraform_agent.terraform.discovery import select_context_mode
from semantic_terraform_agent.terraform.resources import detect_resources
from semantic_terraform_agent.terraform.schema import inspect_schemas


def _elapsed(start: float) -> float:
    return round(time.perf_counter() - start, 6)


def _relevant_sources(
    all_sources: dict[str, str], resources: list, changed_files: tuple[str, ...], failure_file: str | None
) -> dict[str, str]:
    if resources:
        result: dict[str, str] = {}
        for resource in resources:
            if not resource.file or not resource.source:
                continue
            result.setdefault(resource.file, "")
            if result[resource.file]:
                result[resource.file] += "\n\n"
            result[resource.file] += resource.source
        if result:
            return result
    selected = list(changed_files)
    if failure_file:
        matches = [
            path for path in all_sources if path == failure_file or path.endswith(f"/{failure_file}")
        ]
        selected.extend(matches)
    if not selected:
        selected = list(all_sources)
    return {path: all_sources[path] for path in dict.fromkeys(selected) if path in all_sources}


def calculate_evidence_score(request: DiagnosisRequest, diagnosis) -> float:
    evidence_sources = {item.source for item in diagnosis.evidence}
    checks = [
        bool(diagnosis.affected_resources and request.resources),
        bool(request.failure.summary and "terraform_error" in evidence_sources),
        bool(request.git_diff.strip() and "git_diff" in evidence_sources),
        bool(diagnosis.suggested_patch.strip()),
    ]
    if request.context.selected_mode == "schema-aware":
        checks.append(
            bool(
                "provider_schema" in evidence_sources
                and any(
                    item.extraction_status == "ok" and item.resource_schema is not None
                    for item in request.schemas
                )
            )
        )
    return round(sum(checks) / len(checks), 2)


def diagnose_repository(
    *,
    repo_path: Path,
    terraform_dir: Path,
    log_file: Path,
    diff_file: Path | None,
    provider_name: Literal["gemini"],
    model: str,
    context_mode: Literal["lightweight", "schema-aware", "auto"],
    llm_provider: LLMProvider | None = None,
) -> ResultDocument:
    total_start = time.perf_counter()
    timing: dict[str, float] = {}
    warnings: list[str] = []

    started = time.perf_counter()
    layout = discover_repository(repo_path, terraform_dir)
    diff = collect_diff(layout, diff_file)
    failure = collect_failure_log(log_file)
    all_sources = read_source_files(layout, layout.terraform_files)
    timing["collection_seconds"] = _elapsed(started)
    warnings.extend(diff.warnings)
    if not diff.text.strip():
        warnings.append(f"Git diff is empty; comparison used: {diff.comparison}.")

    started = time.perf_counter()
    resources = detect_resources(
        failure, all_sources, diff.changed_files, diff.changed_lines
    )
    context = select_context_mode(context_mode, failure, resources)
    timing["discovery_seconds"] = _elapsed(started)
    if not resources:
        warnings.append("No affected Terraform resource could be identified from the log and diff.")

    started = time.perf_counter()
    resource_types = [item.resource_type for item in resources]
    terraform_info, schema_warnings = inspect_schemas(
        layout, resource_types, enabled=context.selected_mode == "schema-aware"
    )
    warnings.extend(schema_warnings)
    timing["schema_seconds"] = _elapsed(started)

    request = DiagnosisRequest(
        failure=failure,
        resources=resources,
        relevant_sources=_relevant_sources(
            all_sources, resources, diff.changed_files, failure.referenced_file
        ),
        git_diff=diff.text,
        context=context,
        schemas=terraform_info.schemas,
        terraform_version=terraform_info.version,
    )
    started = time.perf_counter()
    if llm_provider is None:
        if provider_name != "gemini":
            raise ValueError(f"unsupported provider: {provider_name}")
        llm_provider = GeminiProvider(model=model)
    provider_response = llm_provider.diagnose(request)
    timing["llm_seconds"] = _elapsed(started)
    model_diagnosis = provider_response.diagnosis
    diagnosis = Diagnosis(
        root_cause=model_diagnosis.root_cause,
        affected_resources=model_diagnosis.affected_resources,
        violated_constraint=model_diagnosis.violated_constraint,
        suggested_patch=model_diagnosis.suggested_patch,
        model_confidence=model_diagnosis.confidence,
        evidence_score=calculate_evidence_score(request, model_diagnosis),
        evidence=model_diagnosis.evidence,
    )
    timing["total_seconds"] = _elapsed(total_start)
    return ResultDocument(
        status="ok",
        repository=RepositoryInfo(
            root=str(layout.root),
            terraform_dir=layout.terraform_dir,
            terraform_files=list(layout.terraform_files),
            changed_terraform_files=list(diff.changed_files),
            diff_source=diff.source,
            diff_comparison=diff.comparison,
        ),
        terraform=terraform_info,
        failure=failure,
        context=context,
        diagnosis=diagnosis,
        timing=timing,
        token_usage=provider_response.token_usage,
        warnings=warnings,
    )
