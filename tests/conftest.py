from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def terraform_repo(tmp_path: Path) -> Path:
    root = tmp_path / "arbitrary project"
    infra = root / "infrastructure"
    infra.mkdir(parents=True)
    (infra / "main.tf").write_text(
        '''resource "example_widget" "primary" {
  mode = "fast"
}

resource "other_gadget" "secondary" {
  size = 2
}
''',
        encoding="utf-8",
    )
    (infra / "providers.tf").write_text(
        'terraform { required_providers { example = { source = "example/example" } } }\n',
        encoding="utf-8",
    )
    (root / "README.md").write_text("unrelated", encoding="utf-8")
    return root


@pytest.fixture
def failure_log(tmp_path: Path) -> Path:
    path = tmp_path / "plan.log"
    path.write_text(
        """Terraform plan failed.
╷
│ Error: Invalid value for argument
│
│   with example_widget.primary,
│   on main.tf line 2, in resource "example_widget" "primary":
│    2:   mode = "fast"
│
│ Argument "mode" must be one of "slow" or "safe".
╵
""",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def diff_file(tmp_path: Path) -> Path:
    path = tmp_path / "change.patch"
    path.write_text(
        """diff --git a/infrastructure/main.tf b/infrastructure/main.tf
index 0000000..1111111 100644
--- a/infrastructure/main.tf
+++ b/infrastructure/main.tf
@@ -1,3 +1,3 @@
 resource "example_widget" "primary" {
-  mode = "safe"
+  mode = "fast"
 }
""",
        encoding="utf-8",
    )
    return path

