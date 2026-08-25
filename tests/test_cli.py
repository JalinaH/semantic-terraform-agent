from __future__ import annotations

from pathlib import Path

import pytest

from semantic_terraform_agent.cli import _write_result, build_parser, main
from semantic_terraform_agent.models import (
    LLMCallType,
    LLMInvocation,
    LLMProviderName,
    LLMUsage,
    ResultDocument,
)


def test_cli_rejects_more_than_one_repair_attempt(capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(
            [
                "diagnose",
                "--repo-path",
                ".",
                "--terraform-dir",
                "infrastructure",
                "--log-file",
                "plan.log",
                "--output",
                "result.json",
                "--max-repair-attempts",
                "2",
            ]
        )
    assert exc.value.code == 2
    assert "invalid choice: '2'" in capsys.readouterr().err


def test_cli_defaults_to_one_repair_attempt() -> None:
    args = build_parser().parse_args(
        [
            "diagnose",
            "--repo-path",
            ".",
            "--terraform-dir",
            "infrastructure",
            "--log-file",
            "plan.log",
            "--output",
            "result.json",
        ]
    )
    assert args.max_repair_attempts == 1
    assert args.failed_stage is None
    assert args.provider == "openrouter"
    assert args.model is None
    assert args.context_strategy == "deterministic-minimal-v1"
    assert args.schema_strategy == "sliced"
    assert args.model_routing == "fixed"
    assert args.max_model_tier == "premium"
    assert args.model_registry is None
    assert args.source_revision is None
    assert args.verification_mode == "full"


def test_cli_accepts_explicit_local_verification_mode() -> None:
    args = build_parser().parse_args(
        [
            "diagnose",
            "--repo-path",
            ".",
            "--terraform-dir",
            "infrastructure",
            "--log-file",
            "plan.log",
            "--verification-mode",
            "local",
            "--output",
            "result.json",
        ]
    )
    assert args.verification_mode == "local"


def test_cli_accepts_full_source_revision() -> None:
    revision = "a" * 40
    args = build_parser().parse_args(
        [
            "diagnose",
            "--repo-path",
            ".",
            "--terraform-dir",
            "infrastructure",
            "--log-file",
            "plan.log",
            "--source-revision",
            revision,
            "--output",
            "result.json",
        ]
    )
    assert args.source_revision == revision


def test_cli_accepts_auto_routing_without_explicit_openrouter_model() -> None:
    args = build_parser().parse_args(
        [
            "diagnose",
            "--repo-path",
            ".",
            "--terraform-dir",
            "infrastructure",
            "--log-file",
            "plan.log",
            "--provider",
            "openrouter",
            "--model-routing",
            "auto",
            "--max-model-tier",
            "free",
            "--model-registry",
            "models.json",
            "--output",
            "result.json",
        ]
    )
    assert args.model is None
    assert args.model_routing == "auto"
    assert args.max_model_tier == "free"
    assert args.model_registry == Path("models.json")


def test_cli_accepts_hidden_full_schema_evaluation_strategy() -> None:
    args = build_parser().parse_args(
        [
            "diagnose",
            "--repo-path",
            ".",
            "--terraform-dir",
            "infrastructure",
            "--log-file",
            "plan.log",
            "--schema-strategy",
            "full",
            "--output",
            "result.json",
        ]
    )
    assert args.schema_strategy == "full"


def test_cli_accepts_openrouter_and_dynamic_free_model() -> None:
    args = build_parser().parse_args(
        [
            "diagnose",
            "--repo-path",
            ".",
            "--terraform-dir",
            "infrastructure",
            "--log-file",
            "plan.log",
            "--provider",
            "openrouter",
            "--model",
            "new-provider/new-model:free",
            "--output",
            "result.json",
        ]
    )
    assert args.provider == "openrouter"
    assert args.model == "new-provider/new-model:free"


def test_cli_defaults_fixed_openrouter_to_free_router(tmp_path, capsys, monkeypatch) -> None:
    captured = {}

    def diagnose(**kwargs):
        captured.update(kwargs)
        from semantic_terraform_agent.config import InputError

        raise InputError("sentinel after model resolution")

    monkeypatch.setattr(
        "semantic_terraform_agent.cli.diagnose_repository", diagnose
    )
    output = tmp_path / "result.json"
    exit_code = main(
        [
            "diagnose",
            "--repo-path",
            ".",
            "--terraform-dir",
            "infrastructure",
            "--log-file",
            "plan.log",
            "--provider",
            "openrouter",
            "--output",
            str(output),
        ]
    )
    assert exit_code == 2
    assert "sentinel after model resolution" in capsys.readouterr().err
    assert captured["provider_name"] == "openrouter"
    assert captured["model"] == "openrouter/free"
    assert '"status": "error"' in output.read_text()


def test_result_json_preserves_unknown_cost_as_null(tmp_path) -> None:
    output = tmp_path / "result.json"
    call = LLMInvocation(
        provider=LLMProviderName.OPENROUTER,
        requested_model="openrouter/free",
        latency_ms=1,
        call_type=LLMCallType.DIAGNOSIS,
        prompt_characters=10,
        system_prompt_characters=4,
        user_prompt_characters=6,
    )
    _write_result(
        output,
        ResultDocument(
            status="ok",
            llm_calls=[call],
            llm_usage=LLMUsage(call_count=1, cost_usd=None, cost_complete=False),
        ),
    )
    payload = output.read_text()
    assert '"cost_usd": null' in payload
    assert '"cost_complete": false' in payload


def test_openrouter_cli_error_is_categorized_without_gemini_fallback(
    terraform_repo: Path,
    failure_log: Path,
    diff_file: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "must-not-be-used")
    output = tmp_path / "result.json"
    exit_code = main(
        [
            "diagnose",
            "--repo-path",
            str(terraform_repo),
            "--terraform-dir",
            "infrastructure",
            "--log-file",
            str(failure_log),
            "--diff-file",
            str(diff_file),
            "--provider",
            "openrouter",
            "--model",
            "openrouter/free",
            "--context-mode",
            "lightweight",
            "--no-verify-patch",
            "--output",
            str(output),
        ]
    )
    assert exit_code == 2
    payload = output.read_text()
    assert '"error_code": "authentication_failed"' in payload
    assert "must-not-be-used" not in payload


def test_repository_validation_precedes_auto_routing_and_provider_call(
    tmp_path: Path, capsys
) -> None:
    registry = tmp_path / "models.json"
    registry.write_text('{"models": []}', encoding="utf-8")
    output = tmp_path / "result.json"

    exit_code = main(
        [
            "diagnose",
            "--repo-path",
            str(tmp_path / "missing-repository"),
            "--terraform-dir",
            "infrastructure",
            "--log-file",
            str(tmp_path / "missing.log"),
            "--provider",
            "openrouter",
            "--model-routing",
            "auto",
            "--max-model-tier",
            "free",
            "--model-registry",
            str(registry),
            "--output",
            str(output),
        ]
    )

    assert exit_code == 2
    assert '"routing_error_code": null' in output.read_text()
    assert "repository path does not exist" in output.read_text()
    assert "OPENROUTER_API_KEY" not in capsys.readouterr().err


@pytest.mark.parametrize("stage", ["validate", "plan"])
def test_cli_accepts_explicit_failed_stage(stage: str) -> None:
    args = build_parser().parse_args(
        [
            "diagnose",
            "--repo-path",
            ".",
            "--terraform-dir",
            "infrastructure",
            "--log-file",
            "failure.log",
            "--failed-stage",
            stage,
            "--output",
            "result.json",
        ]
    )
    assert args.failed_stage == stage
