import json
from pathlib import Path
from types import SimpleNamespace

from semantic_terraform_agent.collectors.repository import discover_repository
from semantic_terraform_agent.terraform import schema as schema_module
from semantic_terraform_agent.terraform.schema import extract_resource_schemas, inspect_schemas


def test_selectively_extracts_provider_schema() -> None:
    document = {
        "provider_schemas": {
            "registry.terraform.io/acme/acme": {
                "resource_schemas": {
                    "acme_widget": {"version": 1, "block": {"attributes": {"mode": {}}}},
                    "acme_secret": {"version": 1, "block": {"attributes": {"value": {}}}},
                }
            }
        }
    }
    records = extract_resource_schemas(
        document,
        ["acme_widget"],
        {"registry.terraform.io/acme/acme": "2.3.4"},
    )
    assert len(records) == 1
    assert records[0].resource_type == "acme_widget"
    assert records[0].provider_version == "2.3.4"
    assert "acme_secret" not in str(records[0].resource_schema)


def test_unknown_resource_schema_is_explicit() -> None:
    records = extract_resource_schemas({"provider_schemas": {}}, ["unknown_resource"])
    assert records[0].extraction_status == "resource-not-found"


def test_missing_terraform_cli_is_reported(monkeypatch, terraform_repo: Path) -> None:
    monkeypatch.setattr(schema_module, "find_terraform", lambda: None)
    layout = discover_repository(terraform_repo, Path("infrastructure"))
    info, warnings = inspect_schemas(layout, ["example_widget"], enabled=True)
    assert info.schema_extraction_status == "terraform-cli-missing"
    assert info.schemas[0].extraction_status == "unavailable"
    assert warnings


def test_lightweight_skips_schema_command(terraform_repo: Path) -> None:
    layout = discover_repository(terraform_repo, Path("infrastructure"))
    info, warnings = inspect_schemas(layout, ["example_widget"], enabled=False)
    assert info.schema_extraction_status == "not-requested"
    assert info.schema_retrieval_command is None
    assert warnings == []


def test_schema_inspection_runs_only_in_temporary_copy(
    monkeypatch, terraform_repo: Path
) -> None:
    calls = []
    document = {
        "provider_schemas": {
            "registry.terraform.io/example/example": {
                "resource_schemas": {"example_widget": {"version": 0, "block": {}}}
            }
        }
    }

    def fake_run(command, **kwargs):
        calls.append((tuple(command), Path(kwargs["cwd"])))
        if "version" in command:
            return SimpleNamespace(
                returncode=0, stdout='{"terraform_version":"1.12.0"}', stderr=""
            )
        if "schema" in command:
            return SimpleNamespace(
                returncode=0, stdout=json.dumps(document).encode(), stderr=b""
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(schema_module, "find_terraform", lambda: "/usr/bin/terraform")
    monkeypatch.setattr(schema_module.subprocess, "run", fake_run)
    layout = discover_repository(terraform_repo, Path("infrastructure"))
    info, warnings = inspect_schemas(layout, ["example_widget"], enabled=True)

    assert info.version == "1.12.0"
    assert info.schema_extraction_status == "ok"
    assert info.schemas[0].resource_schema == {"version": 0, "block": {}}
    assert warnings == []
    assert all(cwd != layout.terraform_root for _, cwd in calls)
    assert not (layout.terraform_root / ".terraform").exists()
