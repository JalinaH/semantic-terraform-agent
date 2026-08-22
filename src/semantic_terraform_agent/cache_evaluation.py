"""Cold-versus-warm Verified Failure Memory evaluation reporting."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from statistics import mean
from typing import Any

from semantic_terraform_agent.evaluation import CASE_IDS
from semantic_terraform_agent.models import ResultDocument


VERIFIED = {"verified_first_attempt", "verified_after_retry"}


def build_cache_comparison(
    *, cold_results: Path | None = None, warm_results: Path | None = None
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for case_id in CASE_IDS:
        for run_kind, directory in (("cold", cold_results), ("warm", warm_results)):
            result = _load(directory, case_id)
            diagnosis = result.diagnosis if result else None
            memory = result.cache.failure_memory if result and result.cache else None
            rows.append(
                {
                    "case_id": case_id,
                    "run_kind": run_kind,
                    "data_source": "live_result" if result else None,
                    "fingerprint_status": memory.status if memory else None,
                    "memory_hit": bool(memory and memory.status in {"hit", "hit_verified", "hit_stale"}) if result else None,
                    "resolution_source": result.resolution_source if result else None,
                    "fresh_verification_status": diagnosis.verification_status if diagnosis else None,
                    "llm_calls": result.llm_usage.call_count if result else None,
                    "input_tokens": result.llm_usage.input_tokens if result else None,
                    "output_tokens": result.llm_usage.output_tokens if result else None,
                    "total_tokens": result.llm_usage.total_tokens if result else None,
                    "cost_usd": result.llm_usage.cost_usd if result else None,
                    "cost_complete": result.llm_usage.cost_complete if result else None,
                    "latency_seconds": result.timing.get("total_seconds") if result else None,
                    "memory_lookup_seconds": result.timing.get("failure_memory_lookup_seconds") if result else None,
                    "verification_seconds": result.timing.get("verification_seconds") if result else None,
                    "llm_calls_avoided": memory.llm_calls_avoided if memory else None,
                }
            )
    return rows, aggregate_cache_results(rows)


def aggregate_cache_results(rows: list[dict[str, Any]]) -> dict[str, Any]:
    warm = [row for row in rows if row["run_kind"] == "warm" and row["data_source"]]
    cold = [row for row in rows if row["run_kind"] == "cold" and row["data_source"]]
    complete_cost = bool(warm) and all(
        row["cost_complete"] is True and row["cost_usd"] is not None for row in warm
    )
    return {
        "cold_run_count": len(cold),
        "warm_run_count": len(warm),
        "memory_hit_rate": _rate([bool(row["memory_hit"]) for row in warm]),
        "zero_llm_call_rate": _rate([row["llm_calls"] == 0 for row in warm]),
        "fresh_verification_pass_rate": _rate(
            [row["fresh_verification_status"] in VERIFIED for row in warm]
        ),
        "mean_warm_input_tokens": _mean(row["input_tokens"] for row in warm),
        "mean_warm_latency_seconds": _mean(row["latency_seconds"] for row in warm),
        "mean_warm_cost_usd": (
            _mean(row["cost_usd"] for row in warm) if complete_cost else None
        ),
        "cost_metrics_complete": complete_cost,
        "cost_per_verified_fix": (
            round(
                sum(row["cost_usd"] for row in warm)
                / sum(row["fresh_verification_status"] in VERIFIED for row in warm),
                12,
            )
            if complete_cost
            and any(row["fresh_verification_status"] in VERIFIED for row in warm)
            else None
        ),
    }


def write_cache_comparison(
    rows: list[dict[str, Any]],
    aggregates: dict[str, Any],
    output_dir: Path,
    *,
    offline_validations: list[dict[str, Any]] | None = None,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    document = {
        "comparison": "cold versus warm verified failure memory",
        "measurement_policy": (
            "Null values mean no live result was supplied. Token and cost values are "
            "never inferred from deterministic policy fixtures."
        ),
        "aggregates": aggregates,
        "offline_validations": offline_validations or [],
        "rows": rows,
    }
    (output_dir / "results.json").write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (output_dir / "results.jsonl").write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )
    lines = [
        "# v1.0 Verified Failure Memory comparison",
        "",
        "Missing live values are reported as `null`; no token, cost, or quality metrics are fabricated.",
        "",
        "| Case | Run | Memory status | Resolution | Verification | LLM calls | Input tokens | Cost (USD) |",
        "|---|---|---|---|---|---:|---:|---:|",
    ]
    for row in rows:
        lines.append(
            "| {case_id} | {run_kind} | {fingerprint_status} | {resolution_source} | "
            "{fresh_verification_status} | {llm_calls} | {input_tokens} | {cost_usd} |".format(
                **{key: ("null" if value is None else value) for key, value in row.items()}
            )
        )
    if offline_validations:
        lines.extend(
            [
                "",
                "## Offline policy/architecture validation",
                "",
                "These use mocked LLM and verification adapters; they are not live performance results.",
                "",
                "| Scenario | Passed |",
                "|---|---|",
            ]
        )
        lines.extend(
            f"| {item['scenario']} | {'yes' if item['passed'] else 'no'} |"
            for item in offline_validations
        )
    (output_dir / "comparison.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_offline_cache_validation() -> list[dict[str, Any]]:
    """Exercise cache orchestration with mock LLM/verification, not live performance."""
    from semantic_terraform_agent.cache.fingerprint import schema_cache_key
    from semantic_terraform_agent.cache.store import LocalCacheStore
    from semantic_terraform_agent.models import (
        ModelDiagnosis,
        ProviderResponse,
        TokenUsage,
        VerificationAttempt,
        VerificationCommands,
    )
    from semantic_terraform_agent.orchestration.diagnose import diagnose_repository

    patch = (
        "--- a/infrastructure/main.tf\n+++ b/infrastructure/main.tf\n"
        "@@ -2 +2 @@\n-  mode = \"fast\"\n+  mode = \"safe\""
    )

    class Provider:
        def __init__(self) -> None:
            self.calls = 0

        def diagnose(self, request):
            self.calls += 1
            return ProviderResponse(
                diagnosis=ModelDiagnosis(
                    root_cause="mode is invalid",
                    affected_resources=["example_widget.primary"],
                    violated_constraint="mode must be safe",
                    suggested_patch=patch,
                    confidence=0.9,
                    evidence=[
                        {"source": "terraform_error", "detail": "exact error"},
                        {"source": "terraform_source", "detail": "exact block"},
                        {"source": "git_diff", "detail": "exact change"},
                    ],
                ),
                token_usage=TokenUsage(input_tokens=10, output_tokens=5, total_tokens=15),
            )

        def repair(self, request):
            raise AssertionError("offline fixture does not repair")

    def verified(candidate, layout, *, attempt):
        return VerificationAttempt(
            attempt=attempt,
            patch=candidate,
            status="verified",
            commands=VerificationCommands(),
            temporary_copy_cleaned=True,
        )

    with tempfile.TemporaryDirectory(prefix="semantic-tf-cache-eval-") as temporary:
        root = Path(temporary)
        repository = root / "repository"
        infrastructure = repository / "infrastructure"
        infrastructure.mkdir(parents=True)
        (infrastructure / "main.tf").write_text(
            'resource "example_widget" "primary" {\n  mode = "fast"\n}\n',
            encoding="utf-8",
        )
        (infrastructure / ".terraform.lock.hcl").write_text(
            'provider "registry.terraform.io/example/example" {\n  version = "1.2.3"\n}\n',
            encoding="utf-8",
        )
        log = root / "failure.log"
        log.write_text(
            'Terraform plan failed.\nError: Invalid value\n\n'
            '  with example_widget.primary,\n'
            '  on main.tf line 2, in resource "example_widget" "primary":\n'
            '   2: mode = "fast"\n\nmode must be safe\n',
            encoding="utf-8",
        )
        diff = root / "change.patch"
        diff.write_text(
            "diff --git a/infrastructure/main.tf b/infrastructure/main.tf\n"
            "--- a/infrastructure/main.tf\n+++ b/infrastructure/main.tf\n"
            "@@ -1,3 +1,3 @@\n resource \"example_widget\" \"primary\" {\n"
            "-  mode = \"safe\"\n+  mode = \"fast\"\n }\n",
            encoding="utf-8",
        )
        store = LocalCacheStore(root / "cache")
        provider = Provider()
        common = dict(
            repo_path=repository,
            terraform_dir=Path("infrastructure"),
            log_file=log,
            diff_file=diff,
            provider_name="openrouter",
            model="fixture/model:free",
            context_mode="lightweight",
            llm_provider=provider,
            patch_verifier=verified,
            max_repair_attempts=0,
            cache_store=store,
            failure_memory_enabled=True,
            repository_id="offline/repository",
        )
        cold = diagnose_repository(**common)
        warm = diagnose_repository(**common)
        source = infrastructure / "main.tf"
        source.write_text(source.read_text().replace("fast", "slow"), encoding="utf-8")
        changed = diagnose_repository(**common)
        stale_verifications = 0

        def stale_then_verified(candidate, layout, *, attempt):
            nonlocal stale_verifications
            stale_verifications += 1
            return VerificationAttempt(
                attempt=attempt,
                patch=candidate,
                status="rejected" if stale_verifications == 1 else "verified",
                failed_stage=("patch_check" if stale_verifications == 1 else None),
                commands=VerificationCommands(),
                temporary_copy_cleaned=True,
            )

        stale = diagnose_repository(
            **{**common, "patch_verifier": stale_then_verified}
        )

    version_a = schema_cache_key(
        terraform_version="1.9.0",
        provider_lock_hash="a" * 64,
        source_fingerprint="c" * 64,
        resource_types=["example_widget"],
    )
    version_b = schema_cache_key(
        terraform_version="1.9.0",
        provider_lock_hash="b" * 64,
        source_fingerprint="c" * 64,
        resource_types=["example_widget"],
    )
    return [
        {"scenario": "cold_miss", "passed": cold.cache.failure_memory.status == "miss" and cold.cache.failure_memory.write_status == "stored" and cold.llm_usage.call_count == 1},
        {"scenario": "warm_hit", "passed": warm.cache.failure_memory.status == "hit_verified"},
        {"scenario": "changed_source_miss", "passed": changed.resolution_source == "llm"},
        {"scenario": "provider_version_miss", "passed": version_a != version_b},
        {"scenario": "stale_hit_fallback", "passed": stale.cache.failure_memory.status == "hit_stale" and stale.resolution_source == "llm" and stale.llm_usage.call_count == 1},
        {"scenario": "zero_provider_call_warm_success", "passed": warm.llm_usage.call_count == 0 and provider.calls == 3},
    ]


def _load(directory: Path | None, case_id: str) -> ResultDocument | None:
    if directory is None:
        return None
    for path in (directory / f"{case_id}.json", directory / case_id / "result.json"):
        if path.is_file():
            return ResultDocument.model_validate_json(path.read_text(encoding="utf-8"))
    return None


def _rate(values: list[bool]) -> float | None:
    return round(sum(values) / len(values), 4) if values else None


def _mean(values) -> float | None:
    known = [value for value in values if value is not None]
    return round(mean(known), 6) if known else None
