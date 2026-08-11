from __future__ import annotations

from pathlib import Path

from semantic_terraform_agent.terraform.workspace import sanitized_environment


def test_sanitized_environment_passes_only_terraform_and_explicit_oidc_values(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("TF_VAR_region", "example-region-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "temporary-access-key")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "temporary-secret-key")
    monkeypatch.setenv("UNRELATED_SECRET", "must-not-pass")
    monkeypatch.setenv(
        "SEMANTIC_TERRAFORM_AGENT_PASSTHROUGH_ENV",
        "AWS_ACCESS_KEY_ID,AWS_SECRET_ACCESS_KEY",
    )

    env = sanitized_environment(tmp_path)
    assert env["TF_VAR_region"] == "example-region-1"
    assert env["AWS_ACCESS_KEY_ID"] == "temporary-access-key"
    assert env["AWS_SECRET_ACCESS_KEY"] == "temporary-secret-key"
    assert "UNRELATED_SECRET" not in env
    assert env["HOME"] == str(tmp_path)

