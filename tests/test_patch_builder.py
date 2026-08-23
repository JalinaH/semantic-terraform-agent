from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from semantic_terraform_agent.collectors.repository import discover_repository
from semantic_terraform_agent.models import SemanticEditSet
from semantic_terraform_agent.terraform.patch_builder import (
    StructuredEditFailure,
    build_patch_from_edits,
)


def _build(root: Path, edits: list[dict[str, str]]):
    layout = discover_repository(root, Path("infrastructure"))
    return build_patch_from_edits(SemanticEditSet(edits=edits), layout)


def _assert_git_applies(root: Path, patch: str) -> None:
    result = subprocess.run(
        ["git", "apply", "--check", "-"],
        cwd=root,
        input=patch,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_single_line_replacement_builds_git_applicable_patch(
    terraform_repo: Path,
) -> None:
    source = (terraform_repo / "infrastructure/main.tf").read_bytes()
    built = _build(
        terraform_repo,
        [
            {
                "file": "main.tf",
                "old_text": 'mode = "fast"',
                "new_text": 'mode = "safe"',
            }
        ],
    )

    assert built.edit_count == 1
    assert built.affected_files == ("infrastructure/main.tf",)
    assert "--- a/infrastructure/main.tf\n" in built.patch
    assert "+++ b/infrastructure/main.tf\n" in built.patch
    assert "@@ " in built.patch
    assert "--- a/infrastructure/main.tf+++" not in built.patch
    assert (terraform_repo / "infrastructure/main.tf").read_bytes() == source
    _assert_git_applies(terraform_repo, built.patch)


def test_multiline_and_same_file_edits_use_original_source_offsets(
    terraform_repo: Path,
) -> None:
    built = _build(
        terraform_repo,
        [
            {
                "file": "infrastructure/main.tf",
                "old_text": 'resource "example_widget" "primary" {\n  mode = "fast"\n}',
                "new_text": 'resource "example_widget" "primary" {\n  mode = "safe"\n}',
            },
            {
                "file": "main.tf",
                "old_text": "size = 2",
                "new_text": "size = 3",
            },
        ],
    )

    assert built.edit_count == 2
    assert 'mode = "safe"' in built.patch
    assert "size = 3" in built.patch
    _assert_git_applies(terraform_repo, built.patch)


def test_multiple_files_and_tf_json_are_supported(terraform_repo: Path) -> None:
    tf_json = terraform_repo / "infrastructure/settings.tf.json"
    tf_json.write_text('{"variable":{"region":{"default":"old"}}}\n', encoding="utf-8")
    built = _build(
        terraform_repo,
        [
            {
                "file": "providers.tf",
                "old_text": "example/example",
                "new_text": "example/corrected",
            },
            {
                "file": "settings.tf.json",
                "old_text": '"default":"old"',
                "new_text": '"default":"new"',
            },
        ],
    )

    assert built.affected_files == (
        "infrastructure/providers.tf",
        "infrastructure/settings.tf.json",
    )
    _assert_git_applies(terraform_repo, built.patch)


@pytest.mark.parametrize(
    ("old_text", "code"),
    [
        ("target does not exist", "edit_target_not_found"),
        ("resource", "edit_target_ambiguous"),
    ],
)
def test_target_match_count_is_deterministic(
    terraform_repo: Path, old_text: str, code: str
) -> None:
    with pytest.raises(StructuredEditFailure) as exc:
        _build(
            terraform_repo,
            [{"file": "main.tf", "old_text": old_text, "new_text": "replacement"}],
        )
    assert exc.value.code == code
    assert exc.value.repairable is True


def test_duplicate_edits_are_rejected(terraform_repo: Path) -> None:
    edit = {
        "file": "main.tf",
        "old_text": 'mode = "fast"',
        "new_text": 'mode = "safe"',
    }
    with pytest.raises(StructuredEditFailure) as exc:
        _build(terraform_repo, [edit, edit])
    assert exc.value.code == "duplicate_edits"


def test_overlapping_edits_are_rejected(terraform_repo: Path) -> None:
    with pytest.raises(StructuredEditFailure) as exc:
        _build(
            terraform_repo,
            [
                {
                    "file": "main.tf",
                    "old_text": 'mode = "fast"',
                    "new_text": 'mode = "safe"',
                },
                {
                    "file": "main.tf",
                    "old_text": '"fast"',
                    "new_text": '"slow"',
                },
            ],
        )
    assert exc.value.code == "overlapping_edits"


@pytest.mark.parametrize("path", ["../secret.tf", "/tmp/secret.tf", ".env", "README.md"])
def test_unsafe_or_non_terraform_paths_are_rejected(
    terraform_repo: Path, path: str
) -> None:
    with pytest.raises(StructuredEditFailure) as exc:
        _build(
            terraform_repo,
            [{"file": path, "old_text": "unrelated", "new_text": "changed"}],
        )
    assert exc.value.code == "invalid_edit_path"


def test_structured_edit_output_is_bounded() -> None:
    with pytest.raises(ValidationError):
        SemanticEditSet(
            edits=[
                {"file": "main.tf", "old_text": "a", "new_text": "x" * 8_001}
            ]
        )
    with pytest.raises(ValidationError):
        SemanticEditSet(
            edits=[
                {"file": "main.tf", "old_text": "a", "new_text": "b"}
                for _ in range(9)
            ]
        )
    with pytest.raises(ValidationError):
        SemanticEditSet(
            edits=[{"file": "main.tf", "old_text": "a\x00", "new_text": "b"}]
        )


def test_symlink_target_is_rejected(terraform_repo: Path) -> None:
    target = terraform_repo / "infrastructure/target.tf"
    target.write_text('variable "x" { default = "old" }\n', encoding="utf-8")
    link = terraform_repo / "infrastructure/link.tf"
    link.symlink_to(target.name)

    with pytest.raises(StructuredEditFailure) as exc:
        _build(
            terraform_repo,
            [{"file": "link.tf", "old_text": '"old"', "new_text": '"new"'}],
        )
    assert exc.value.code == "invalid_edit_path"
    assert exc.value.repairable is False


def test_non_utf8_source_fails_without_mutation(terraform_repo: Path) -> None:
    path = terraform_repo / "infrastructure/invalid.tf"
    original = b'variable "x" { default = "old" }\n\xff'
    path.write_bytes(original)

    with pytest.raises(StructuredEditFailure) as exc:
        _build(
            terraform_repo,
            [{"file": "invalid.tf", "old_text": '"old"', "new_text": '"new"'}],
        )
    assert exc.value.code == "structured_edit_invalid"
    assert exc.value.repairable is False
    assert path.read_bytes() == original
