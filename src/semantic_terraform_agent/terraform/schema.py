"""Safe, temporary-copy Terraform schema inspection and selective extraction."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from semantic_terraform_agent.collectors.repository import RepositoryLayout
from semantic_terraform_agent.config import DEFAULT_LIMITS
from semantic_terraform_agent.models import SchemaRecord, TerraformInfo
from semantic_terraform_agent.terraform.workspace import (
    create_safe_terraform_copy,
    sanitized_environment,
)


def find_terraform() -> str | None:
    return shutil.which("terraform")


def _terraform_version(executable: str, cwd: Path, env: dict[str, str]) -> str | None:
    try:
        completed = subprocess.run(
            (executable, "version", "-json"),
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode == 0:
        try:
            value = json.loads(completed.stdout).get("terraform_version")
            return str(value) if value else None
        except json.JSONDecodeError:
            pass
    match = re.search(r"Terraform v([^\s]+)", completed.stdout + completed.stderr)
    return match.group(1) if match else None


def _provider_versions(lock_file: Path) -> dict[str, str]:
    if not lock_file.is_file():
        return {}
    text = lock_file.read_text(encoding="utf-8", errors="replace")
    versions: dict[str, str] = {}
    pattern = re.compile(
        r'provider\s+"(?P<source>[^"]+)"\s*\{(?P<body>.*?)\}', re.DOTALL
    )
    for match in pattern.finditer(text):
        version = re.search(r'version\s*=\s*"([^"]+)"', match.group("body"))
        if version:
            versions[match.group("source")] = version.group(1)
    return versions


def inspect_terraform_version(layout: RepositoryLayout) -> str | None:
    """Read only the local Terraform CLI version without provider initialization."""
    executable = find_terraform()
    if executable is None:
        return None
    with tempfile.TemporaryDirectory(
        prefix="semantic-terraform-agent-version-"
    ) as temporary:
        home = Path(temporary)
        return _terraform_version(
            executable,
            layout.terraform_root,
            sanitized_environment(home),
        )


def extract_resource_schemas(
    document: dict, resource_types: list[str], provider_versions: dict[str, str] | None = None
) -> list[SchemaRecord]:
    provider_versions = provider_versions or {}
    providers = document.get("provider_schemas", {})
    records: list[SchemaRecord] = []
    for resource_type in dict.fromkeys(resource_types):
        found = False
        for provider_source, provider_data in providers.items():
            schemas = provider_data.get("resource_schemas", {})
            if resource_type not in schemas:
                continue
            records.append(
                SchemaRecord(
                    resource_type=resource_type,
                    provider_source=provider_source,
                    provider_version=provider_versions.get(provider_source),
                    extraction_status="ok",
                    schema=schemas[resource_type],
                )
            )
            found = True
            break
        if not found:
            records.append(
                SchemaRecord(resource_type=resource_type, extraction_status="resource-not-found")
            )
    return records


def inspect_schemas(
    layout: RepositoryLayout, resource_types: list[str], *, enabled: bool
) -> tuple[TerraformInfo, list[str]]:
    command = ["terraform", "providers", "schema", "-json"]
    if not enabled:
        executable = find_terraform()
        version = None
        if executable is not None:
            with tempfile.TemporaryDirectory(prefix="semantic-terraform-agent-version-") as temporary:
                home = Path(temporary)
                version = _terraform_version(
                    executable, layout.terraform_root, sanitized_environment(home)
                )
        return (
            TerraformInfo(
                version=version, schema_extraction_status="not-requested", schemas=[]
            ),
            [],
        )
    executable = find_terraform()
    if executable is None:
        records = [
            SchemaRecord(resource_type=item, extraction_status="unavailable")
            for item in dict.fromkeys(resource_types)
        ]
        return (
            TerraformInfo(
                schema_retrieval_command=command,
                schema_extraction_status="terraform-cli-missing",
                schemas=records,
            ),
            ["Terraform CLI was not found; schema-aware context is continuing without schemas."],
        )

    warnings: list[str] = []
    with tempfile.TemporaryDirectory(prefix="semantic-terraform-agent-") as temporary:
        temp_root = Path(temporary)
        workdir = create_safe_terraform_copy(layout, temp_root / "repository")
        home = temp_root / "home"
        home.mkdir()
        env = sanitized_environment(home)
        version = _terraform_version(executable, workdir, env)
        init_command = [executable, "init", "-backend=false", "-input=false", "-no-color"]
        try:
            initialized = subprocess.run(
                init_command,
                cwd=workdir,
                env=env,
                capture_output=True,
                text=True,
                timeout=DEFAULT_LIMITS.command_timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            warnings.append(f"Terraform initialization could not complete in the temporary copy: {exc}")
            initialized = None
        if initialized is None or initialized.returncode != 0:
            detail = ""
            if initialized is not None:
                detail = (initialized.stderr or initialized.stdout).strip()[-500:]
            if detail:
                warnings.append(f"Terraform init failed in the temporary copy: {detail}")
            records = [
                SchemaRecord(resource_type=item, extraction_status="unavailable")
                for item in dict.fromkeys(resource_types)
            ]
            return (
                TerraformInfo(
                    version=version,
                    schema_retrieval_command=command,
                    schema_extraction_status="init-failed",
                    schemas=records,
                ),
                warnings,
            )
        try:
            completed = subprocess.run(
                (executable, "providers", "schema", "-json"),
                cwd=workdir,
                env=env,
                capture_output=True,
                timeout=DEFAULT_LIMITS.command_timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            warnings.append(f"Terraform provider schema command failed: {exc}")
            completed = None
        if completed is None or completed.returncode != 0:
            detail = completed.stderr.decode(errors="replace")[-500:] if completed else ""
            if detail:
                warnings.append(f"Terraform provider schema command failed: {detail}")
            records = [
                SchemaRecord(resource_type=item, extraction_status="unavailable")
                for item in dict.fromkeys(resource_types)
            ]
            return (
                TerraformInfo(
                    version=version,
                    schema_retrieval_command=command,
                    schema_extraction_status="command-failed",
                    schemas=records,
                ),
                warnings,
            )
        if len(completed.stdout) > DEFAULT_LIMITS.max_command_output_bytes:
            warnings.append("Provider schema output exceeded the configured in-memory limit.")
            return (
                TerraformInfo(
                    version=version,
                    schema_retrieval_command=command,
                    schema_extraction_status="output-too-large",
                ),
                warnings,
            )
        try:
            document = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            warnings.append(f"Terraform returned malformed provider schema JSON: {exc}")
            return (
                TerraformInfo(
                    version=version,
                    schema_retrieval_command=command,
                    schema_extraction_status="invalid-json",
                ),
                warnings,
            )
        versions = _provider_versions(workdir / ".terraform.lock.hcl")
        records = extract_resource_schemas(document, resource_types, versions)
        status = "ok" if records and all(r.extraction_status == "ok" for r in records) else "partial"
        if not resource_types:
            status = "no-resource-types"
            warnings.append("No resource types were detected, so no provider schema was sent to the model.")
        return (
            TerraformInfo(
                version=version,
                schema_retrieval_command=command,
                schema_extraction_status=status,
                schemas=records,
            ),
            warnings,
        )
