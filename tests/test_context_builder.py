from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from semantic_terraform_agent.collectors.failure_log import parse_failure_log
from semantic_terraform_agent.collectors.git_diff import (
    DiffData,
    parse_changed_files,
    parse_changed_lines,
)
from semantic_terraform_agent.collectors.repository import RepositoryLayout
from semantic_terraform_agent.config import DEFAULT_LIMITS
from semantic_terraform_agent.context import ContextBuilder
from semantic_terraform_agent.context.builder import normalize_resource_address
from semantic_terraform_agent.terraform.resources import detect_resources


def _layout(tmp_path: Path, sources: dict[str, str]) -> RepositoryLayout:
    root = tmp_path / "repository"
    for relative, source in sources.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
    terraform_root = root / "infra"
    return RepositoryLayout(
        root=root,
        terraform_root=terraform_root,
        terraform_dir="infra",
        terraform_files=tuple(sorted(sources)),
    )


def _context(
    tmp_path: Path,
    sources: dict[str, str],
    log: str,
    diff_text: str = "",
    *,
    limits=DEFAULT_LIMITS,
):
    layout = _layout(tmp_path, sources)
    failure = parse_failure_log(log)
    diff = DiffData(
        text=diff_text,
        source="test",
        comparison="test",
        changed_files=parse_changed_files(diff_text, layout),
        changed_lines=parse_changed_lines(diff_text, layout),
    )
    resources = detect_resources(
        failure, sources, diff.changed_files, diff.changed_lines
    )
    built = ContextBuilder(limits).build(
        repository=layout,
        failure=failure,
        diff=diff,
        all_sources=sources,
        detected_resources=resources,
        mode="lightweight",
    )
    return built, failure, diff, resources, layout


def test_golden_context_selects_failure_var_and_diff_only(tmp_path: Path) -> None:
    sources = {
        "infra/main.tf": '''resource "aws_ebs_volume" "example" {
  type = var.volume_type
  size = 10
}

resource "aws_s3_bucket" "unrelated" {
  bucket = "do-not-include"
}
''',
        "infra/variables.tf": '''variable "volume_type" {
  type    = string
  default = "gp2"
}

variable "unused" {
  default = "do-not-include"
}
''',
        "infra/outputs.tf": 'output "bucket" { value = aws_s3_bucket.unrelated.id }\n',
        "infra/unrelated.tf": 'resource "random_pet" "unused" {}\n',
    }
    diff = '''diff --git a/infra/main.tf b/infra/main.tf
--- a/infra/main.tf
+++ b/infra/main.tf
@@ -1,4 +1,4 @@
 resource "aws_ebs_volume" "example" {
-  type = "gp3"
+  type = var.volume_type
   size = 10
 }
diff --git a/README.md b/README.md
--- a/README.md
+++ b/README.md
@@ -1 +1 @@
-old
+new
'''
    built, *_ = _context(
        tmp_path,
        sources,
        '''Error: throughput must not be set
with aws_ebs_volume.example,
on main.tf line 2, in resource "aws_ebs_volume" "example":
throughput conflicts with gp2
''',
        diff,
    )

    assert built.manifest.included_resources == ["aws_ebs_volume.example"]
    assert built.manifest.included_symbols == ["var.volume_type"]
    assert built.manifest.included_files == [
        "infra/main.tf",
        "infra/variables.tf",
    ]
    assert len(built.resource_blocks) == 1
    assert "aws_s3_bucket" not in built.resource_blocks[0].source
    assert "do-not-include" not in "".join(
        block.source for block in built.supporting_blocks
    )
    assert len(built.changed_lines) == 1
    assert built.changed_lines[0].file == "infra/main.tf"
    assert built.manifest.changed_lines == 2


def test_multiple_resources_in_one_file_only_includes_failing_block(
    tmp_path: Path,
) -> None:
    sources = {
        "infra/main.tf": '''resource "aws_instance" "first" {
  ami = "ami-first"
}

resource "aws_instance" "web" {
  ami = "ami-web"
}

resource "aws_instance" "last" {
  ami = "ami-last"
}
'''
    }
    built, *_ = _context(
        tmp_path,
        sources,
        '''Error: invalid ami
with aws_instance.web[0],
on main.tf line 6, in resource "aws_instance" "web":
ami is invalid
''',
    )
    assert len(built.resource_blocks) == 1
    source = built.resource_blocks[0].source
    assert 'resource "aws_instance" "web"' in source
    assert "ami-first" not in source
    assert "ami-last" not in source


def test_multifile_selection_resolves_local_and_excludes_s3(tmp_path: Path) -> None:
    sources = {
        "infra/main.tf": 'terraform { required_version = ">= 1.5" }\n',
        "infra/ebs.tf": '''resource "aws_ebs_volume" "example" {
  type = local.volume_type
}
''',
        "infra/s3.tf": 'resource "aws_s3_bucket" "other" { bucket = "other" }\n',
        "infra/locals.tf": '''locals {
  volume_type = "gp2"
  unused      = "not-selected"
}
''',
        "infra/variables.tf": 'variable "unused" { default = true }\n',
    }
    built, *_ = _context(
        tmp_path,
        sources,
        '''Error: invalid volume type
with aws_ebs_volume.example,
on ebs.tf line 2, in resource "aws_ebs_volume" "example":
type must be gp3
''',
    )
    assert built.manifest.included_files == ["infra/ebs.tf", "infra/locals.tf"]
    assert built.resolved_symbols == ["local.volume_type"]
    assert built.supporting_blocks[0].source.strip() == 'volume_type = "gp2"'
    assert "infra/s3.tf" not in built.manifest.included_files


@pytest.mark.parametrize(
    ("address", "expected"),
    [
        ("aws_instance.web[0]", "aws_instance.web"),
        ('aws_instance.web["blue"]', "aws_instance.web"),
        (
            "module.network.aws_subnet.private[0]",
            "aws_subnet.private",
        ),
        (
            'module.root.module.network.aws_subnet.private["blue"]',
            "aws_subnet.private",
        ),
    ],
)
def test_resource_address_normalization(address: str, expected: str) -> None:
    assert normalize_resource_address(address) == expected


def test_module_prefixed_address_matches_source_without_losing_identity(
    tmp_path: Path,
) -> None:
    sources = {
        "infra/main.tf": '''resource "aws_subnet" "private" {
  cidr_block = "10.0.1.0/24"
}
'''
    }
    address = "module.network.aws_subnet.private[0]"
    built, *_ = _context(
        tmp_path,
        sources,
        f'''Error: invalid subnet
with {address},
on main.tf line 2, in resource "aws_subnet" "private":
cidr is rejected
''',
    )
    assert built.resource_blocks[0].identifier == address
    assert 'resource "aws_subnet" "private"' in built.resource_blocks[0].source


def test_one_hop_symbol_resolution_is_conservative(tmp_path: Path) -> None:
    sources = {
        "infra/main.tf": '''resource "aws_instance" "web" {
  ami                    = data.aws_ami.selected.id
  vpc_security_group_ids = [aws_security_group.web.id]
  subnet_id              = module.network.public_subnet_id
  user_data              = file("../../secret.txt")
  instance_type          = var.missing
}

resource "aws_security_group" "web" {
  name   = "web"
  vpc_id = var.vpc_id
}
''',
        "infra/data.tf": '''data "aws_ami" "selected" {
  most_recent = true
}
''',
        "infra/variables.tf": '''variable "vpc_id" {
  default = "vpc-not-recursed"
}
''',
    }
    built, *_ = _context(
        tmp_path,
        sources,
        '''Error: invalid instance
with aws_instance.web,
on main.tf line 2, in resource "aws_instance" "web":
configuration is invalid
''',
    )
    assert "data.aws_ami.selected" in built.resolved_symbols
    assert "aws_security_group.web" in built.resolved_symbols
    assert "module.network.public_subnet_id" in built.unresolved_symbols
    assert "file()" in built.unresolved_symbols
    assert "var.missing" in built.unresolved_symbols
    assert "var.vpc_id" not in built.referenced_symbols
    assert "vpc-not-recursed" not in "".join(
        block.source for block in built.supporting_blocks
    )


def test_no_diff_falls_back_to_explicit_diagnostic_resource(tmp_path: Path) -> None:
    sources = {
        "infra/ebs.tf": 'resource "aws_ebs_volume" "example" { type = "gp2" }\n',
        "infra/s3.tf": 'resource "aws_s3_bucket" "other" { bucket = "other" }\n',
    }
    built, *_ = _context(
        tmp_path,
        sources,
        '''Error: invalid throughput
with aws_ebs_volume.example,
on ebs.tf line 1, in resource "aws_ebs_volume" "example":
throughput conflicts
''',
    )
    assert built.resource_blocks[0].identifier == "aws_ebs_volume.example"
    assert built.changed_lines == []
    assert built.manifest.changed_lines == 0
    assert "infra/s3.tf" not in built.manifest.included_files


def test_changed_file_prioritizes_resource_without_diagnostic_address(
    tmp_path: Path,
) -> None:
    sources = {
        "infra/ebs.tf": '''resource "aws_ebs_volume" "example" {
  type = "gp2"
}
''',
        "infra/s3.tf": '''resource "aws_s3_bucket" "other" {
  bucket = "other"
}
''',
    }
    diff = '''--- a/infra/ebs.tf
+++ b/infra/ebs.tf
@@ -1,3 +1,3 @@
 resource "aws_ebs_volume" "example" {
-  type = "gp3"
+  type = "gp2"
 }
'''
    built, *_ = _context(
        tmp_path,
        sources,
        "Error: provider configuration is invalid",
        diff,
    )
    assert built.manifest.included_resources == ["aws_ebs_volume.example"]
    assert "infra/s3.tf" not in built.manifest.included_files
    assert "aws_s3_bucket" not in built.resource_blocks[0].source


def test_ambiguous_changed_resources_are_bounded_and_recorded(
    tmp_path: Path,
) -> None:
    sources = {
        "infra/main.tf": "\n\n".join(
            f'''resource "example_widget" "item_{index}" {{
  mode = "bad-{index}"
}}'''
            for index in range(5)
        )
        + "\n"
    }
    hunks = []
    for index, line in enumerate((1, 5, 9, 13, 17)):
        hunks.append(
            f'''@@ -{line},3 +{line},3 @@
 resource "example_widget" "item_{index}" {{
-  mode = "good-{index}"
+  mode = "bad-{index}"
 }}'''
        )
    diff = (
        "--- a/infra/main.tf\n+++ b/infra/main.tf\n" + "\n".join(hunks) + "\n"
    )
    built, *_ = _context(
        tmp_path,
        sources,
        "Error: multiple changed widget configurations are invalid",
        diff,
    )
    assert built.manifest.ambiguous is True
    assert len(built.resource_blocks) == DEFAULT_LIMITS.max_context_candidate_blocks
    assert len(built.resource_blocks) < 5


def test_budget_keeps_diagnostic_and_resource_before_diff(tmp_path: Path) -> None:
    attributes = "".join(f"  value_{index} = {index}\n" for index in range(40))
    source = f'resource "example_widget" "large" {{\n{attributes}}}\n'
    diff = '''--- a/infra/main.tf
+++ b/infra/main.tf
@@ -20,3 +20,3 @@
   value_18 = 18
-  value_19 = 0
+  value_19 = 19
   value_20 = 20
'''
    limits = replace(
        DEFAULT_LIMITS,
        max_diagnostic_context_chars=120,
        max_resource_block_chars=100,
        max_relevant_diff_chars=20,
        max_supporting_context_chars=20,
        max_total_context_chars=220,
    )
    built, *_ = _context(
        tmp_path,
        {"infra/main.tf": source},
        '''Error: invalid value
with example_widget.large,
on main.tf line 21, in resource "example_widget" "large":
value_19 must be nonzero
''',
        diff,
        limits=limits,
    )
    assert built.failure.summary == "invalid value"
    assert built.resource_blocks
    assert built.resource_blocks[0].truncated is True
    assert "value_19" in built.resource_blocks[0].source
    assert built.changed_lines == []
    assert any("resource_block_exceeded_limit" in item for item in built.manifest.truncated_sections)
    assert "git_diff:relevant_diff_exceeded_limit" in built.manifest.truncated_sections
    assert built.metadata == {}
    assert "metadata:total_context_limit" in built.manifest.truncated_sections


def test_oversized_diff_keeps_exact_changed_lines_with_bounded_context(
    tmp_path: Path,
) -> None:
    attributes = "".join(f"  value_{index} = {index}\n" for index in range(15))
    source = f'resource "example_widget" "large" {{\n{attributes}}}\n'
    context_before = "\n".join(
        f"   value_{index} = {index}" for index in range(10)
    )
    context_after = "\n".join(
        f"   value_{index} = {index}" for index in range(11, 15)
    )
    diff = f'''--- a/infra/main.tf
+++ b/infra/main.tf
@@ -1,17 +1,17 @@
 resource "example_widget" "large" {{
{context_before}
-  value_10 = 0
+  value_10 = 10
{context_after}
 }}
'''
    limits = replace(
        DEFAULT_LIMITS,
        max_relevant_diff_chars=180,
        max_total_context_chars=2_000,
    )
    built, *_ = _context(
        tmp_path,
        {"infra/main.tf": source},
        "Error: invalid value\nwith example_widget.large,",
        diff,
        limits=limits,
    )
    change = built.changed_lines[0]
    assert change.truncated is True
    assert "-  value_10 = 0" in change.rendered
    assert "+  value_10 = 10" in change.rendered
    assert len(change.rendered) <= limits.max_relevant_diff_chars
    assert "git_diff:relevant_diff_exceeded_limit" in built.manifest.truncated_sections


def test_context_optimization_uses_terraform_source_only(tmp_path: Path) -> None:
    selected = 'resource "example_widget" "main" { mode = "bad" }\n'
    unrelated = 'resource "other_widget" "other" { mode = "safe" }\n'
    sources = {
        "infra/main.tf": selected,
        "infra/unrelated.tf": unrelated,
    }
    built, *_ = _context(
        tmp_path,
        sources,
        "Error: bad mode\nwith example_widget.main,",
    )
    optimization = built.optimization
    assert optimization.available_source_characters == len(selected) + len(unrelated)
    assert optimization.selected_source_characters == len(selected.strip())
    assert optimization.characters_avoided == (
        optimization.available_source_characters
        - optimization.selected_source_characters
    )
    assert optimization.character_reduction_ratio is not None
    assert optimization.reduction_ratio == optimization.character_reduction_ratio
    assert optimization.input_token_reduction_ratio is None


def test_arbitrary_file_reference_is_unresolved_and_never_loaded(
    tmp_path: Path,
) -> None:
    sources = {
        "infra/main.tf": '''resource "example_widget" "main" {
  payload = templatefile("../../secret.txt", {})
}
'''
    }
    root = tmp_path / "repository"
    root.mkdir()
    (root / ".env").write_text("TOKEN=never-expose", encoding="utf-8")
    (root / "terraform.tfstate").write_text("state-secret", encoding="utf-8")
    built, *_ = _context(
        tmp_path,
        sources,
        "Error: invalid payload\nwith example_widget.main,",
    )
    rendered = "".join(block.source for block in built.supporting_blocks)
    assert "templatefile()" in built.unresolved_symbols
    assert "never-expose" not in rendered
    assert "state-secret" not in rendered
