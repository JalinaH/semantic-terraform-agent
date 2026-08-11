"""Shared helpers for isolated, credential-reduced Terraform workspaces."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from semantic_terraform_agent.collectors.repository import RepositoryLayout


SAFE_TERRAFORM_NAMES = {".terraform.lock.hcl"}
SAFE_TERRAFORM_SUFFIXES = (".tf", ".tf.json")
EXCLUDED_PARTS = {".git", ".terraform", ".hg", ".svn", "node_modules", ".venv"}


def create_safe_terraform_copy(layout: RepositoryLayout, destination: Path) -> Path:
    """Copy only Terraform configuration and lock files, preserving repo paths."""
    for source in layout.root.rglob("*"):
        relative = source.relative_to(layout.root)
        if any(part in EXCLUDED_PARTS for part in relative.parts):
            continue
        if source.is_symlink() or not source.is_file():
            continue
        if source.name not in SAFE_TERRAFORM_NAMES and not source.name.endswith(
            SAFE_TERRAFORM_SUFFIXES
        ):
            continue
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    return destination / layout.terraform_dir


def sanitized_environment(temp_home: Path) -> dict[str, str]:
    """Return a small environment that does not expose cloud credential variables."""
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(temp_home),
        "TF_IN_AUTOMATION": "1",
        "CHECKPOINT_DISABLE": "1",
        "TF_INPUT": "0",
        "LC_ALL": "C",
    }
    for name in ("SSL_CERT_FILE", "SSL_CERT_DIR", "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY"):
        if value := os.environ.get(name):
            env[name] = value
    return env

