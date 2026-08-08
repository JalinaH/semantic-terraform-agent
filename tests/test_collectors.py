from __future__ import annotations

from pathlib import Path

import pytest

from semantic_terraform_agent.collectors.failure_log import parse_failure_log
from semantic_terraform_agent.collectors.git_diff import collect_diff, parse_changed_files
from semantic_terraform_agent.collectors.repository import discover_repository
from semantic_terraform_agent.config import InputError


def test_discovers_arbitrary_repository_path(terraform_repo: Path) -> None:
    layout = discover_repository(terraform_repo, Path("infrastructure"))
    assert layout.root == terraform_repo.resolve()
    assert layout.terraform_dir == "infrastructure"
    assert layout.terraform_files == (
        "infrastructure/main.tf",
        "infrastructure/providers.tf",
    )


def test_rejects_terraform_directory_path_traversal(terraform_repo: Path) -> None:
    with pytest.raises(InputError, match="escapes"):
        discover_repository(terraform_repo, Path("../"))


def test_rejects_absolute_terraform_directory(terraform_repo: Path) -> None:
    with pytest.raises(InputError, match="relative"):
        discover_repository(terraform_repo, terraform_repo / "infrastructure")


def test_changed_tf_files_and_lines(terraform_repo: Path, diff_file: Path) -> None:
    layout = discover_repository(terraform_repo, Path("infrastructure"))
    diff = collect_diff(layout, diff_file)
    assert diff.changed_files == ("infrastructure/main.tf",)
    assert diff.changed_lines == {"infrastructure/main.tf": (2,)}
    assert diff.comparison == "supplied diff file"


def test_diff_path_traversal_is_rejected(terraform_repo: Path) -> None:
    layout = discover_repository(terraform_repo, Path("infrastructure"))
    with pytest.raises(InputError, match="escapes"):
        parse_changed_files("+++ b/../../secret.tf\n", layout)


def test_parse_standard_terraform_error_reference() -> None:
    result = parse_failure_log(
        '''Terraform plan failed.
│ Error: Invalid combination of arguments
│
│ "throughput": cannot be set with "mode"
│
│   with custom_volume.data,
│   on volume.tf line 17, in resource "custom_volume" "data":
'''
    )
    assert result.summary == "Invalid combination of arguments"
    assert result.referenced_file == "volume.tf"
    assert result.referenced_line == 17
    assert result.resource_address == "custom_volume.data"
    assert result.stage == "plan"
    assert '"throughput": cannot be set with "mode"' in result.detail
    assert result.original_log.startswith("Terraform plan")


def test_parse_json_terraform_diagnostic() -> None:
    result = parse_failure_log(
        '{"diagnostic":{"severity":"error","summary":"Bad widget","detail":"Wrong mode",'
        '"address":"example_widget.primary","range":{"filename":"main.tf",'
        '"start":{"line":9}}}}\n'
    )
    assert result.summary == "Bad widget"
    assert result.detail == "Wrong mode"
    assert result.referenced_line == 9
    assert result.resource_address == "example_widget.primary"


def test_malformed_log_has_safe_fallback() -> None:
    result = parse_failure_log("something exited 1 without diagnostics")
    assert result.summary == "Unstructured Terraform failure"
    assert result.stage == "unknown"
    assert "something exited" in result.detail
