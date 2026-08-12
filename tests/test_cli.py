from __future__ import annotations

import pytest

from semantic_terraform_agent.cli import build_parser


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
