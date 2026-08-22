"""Conservative repository-scoped SHA-256 failure fingerprints."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlsplit

from semantic_terraform_agent.config import DEFAULT_LIMITS
from semantic_terraform_agent.models import DiagnosisContext, FailureInfo
from semantic_terraform_agent.security import redact_secrets


VERIFIED_FAILURE_FINGERPRINT_VERSION = "verified_failure_v1"
CONTEXT_POLICY_VERSION = "deterministic_minimal_v1"
SCHEMA_SLICE_POLICY_VERSION = "deterministic_schema_slice_v1"
PROVIDER_SCHEMA_CACHE_VERSION = "provider_schema_cache_v1"
SCHEMA_SLICE_CACHE_VERSION = "schema_slice_cache_v1"


@dataclass(frozen=True)
class FailureFingerprint:
    version: str
    value: str
    repository_scope: str
    failure_signature: str
    provider_lock_fingerprint: str | None


def canonical_hash(value: object) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def derive_repository_scope(root: Path, explicit_id: str | None = None) -> str:
    if explicit_id:
        safe = redact_secrets(explicit_id.strip())[:500]
        return canonical_hash({"kind": "explicit", "identity": safe})
    remote = _git_value(root, ["config", "--get", "remote.origin.url"])
    if remote:
        sanitized = _sanitize_remote(remote)
        if sanitized:
            return canonical_hash({"kind": "git_remote", "identity": sanitized})
    first_commit = _git_value(root, ["rev-list", "--max-parents=0", "HEAD"])
    if first_commit and re.fullmatch(r"[0-9a-fA-F]{40,64}(?:\n[0-9a-fA-F]{40,64})*", first_commit):
        return canonical_hash(
            {"kind": "git_roots", "identity": sorted(first_commit.splitlines())}
        )
    # A non-Git checkout has no portable identity. Hash the resolved location rather
    # than risking cross-repository reuse; the path itself is never persisted. Moving
    # such a checkout intentionally causes a conservative cache miss.
    return canonical_hash(
        {"kind": "local_path_fallback", "identity": str(root.resolve())}
    )


def build_failure_fingerprint(
    *,
    failure: FailureInfo,
    context: DiagnosisContext,
    repository_scope: str,
    terraform_version: str | None,
    provider_lock_fingerprint: str | None,
    terraform_source_fingerprint: str,
    fingerprint_version: str = VERIFIED_FAILURE_FINGERPRINT_VERSION,
    context_policy_version: str = CONTEXT_POLICY_VERSION,
    schema_policy_version: str = SCHEMA_SLICE_POLICY_VERSION,
) -> FailureFingerprint:
    signature = _failure_signature(failure)
    relevant_context = {
        "changed_lines": [item.model_dump(mode="json") for item in context.changed_lines],
        "resource_blocks": [item.model_dump(mode="json") for item in context.resource_blocks],
        "supporting_blocks": [item.model_dump(mode="json") for item in context.supporting_blocks],
        "referenced_symbols": context.referenced_symbols,
        "resolved_symbols": context.resolved_symbols,
        "unresolved_symbols": context.unresolved_symbols,
        "manifest": context.manifest.model_dump(mode="json"),
    }
    document = {
        "fingerprint_version": fingerprint_version,
        "repository_scope": repository_scope,
        "failure_signature": signature,
        "failed_stage": failure.stage,
        "resource_address": failure.resource_address,
        "referenced_file": failure.referenced_file,
        "referenced_line": failure.referenced_line,
        "relevant_context": relevant_context,
        "terraform_version": terraform_version,
        "provider_lock_fingerprint": provider_lock_fingerprint,
        "terraform_source_fingerprint": terraform_source_fingerprint,
        "context_policy_version": context_policy_version,
        "schema_policy_version": schema_policy_version,
        "limits": asdict(DEFAULT_LIMITS),
    }
    return FailureFingerprint(
        version=fingerprint_version,
        value=canonical_hash(document),
        repository_scope=repository_scope,
        failure_signature=signature,
        provider_lock_fingerprint=provider_lock_fingerprint,
    )


def provider_lock_fingerprint(terraform_root: Path) -> str | None:
    lock_file = terraform_root / ".terraform.lock.hcl"
    if not lock_file.is_file() or lock_file.is_symlink():
        return None
    try:
        data = lock_file.read_bytes()
    except OSError:
        return None
    if len(data) > 2 * 1024 * 1024:
        return None
    return hashlib.sha256(data).hexdigest()


def schema_cache_key(
    *,
    terraform_version: str | None,
    provider_lock_hash: str | None,
    source_fingerprint: str,
    resource_types: list[str],
) -> str:
    return canonical_hash(
        {
            "version": PROVIDER_SCHEMA_CACHE_VERSION,
            "terraform_version": terraform_version,
            "provider_lock_fingerprint": provider_lock_hash,
            "source_fingerprint": source_fingerprint,
            "resource_types": sorted(set(resource_types)),
        }
    )


def schema_slice_cache_key(
    *,
    schemas: list[dict],
    failure: FailureInfo,
    context: DiagnosisContext,
    strategy: str,
) -> str:
    return canonical_hash(
        {
            "version": SCHEMA_SLICE_CACHE_VERSION,
            "schemas": schemas,
            "failure": failure.model_dump(mode="json", exclude={"original_log"}),
            "context": context.model_dump(mode="json"),
            "strategy": strategy,
            "limits": asdict(DEFAULT_LIMITS),
        }
    )


def _failure_signature(failure: FailureInfo) -> str:
    value = "\n".join((failure.summary, failure.detail))
    return redact_secrets(" ".join(value.split()))[:2_000]


def _git_value(root: Path, arguments: list[str]) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), *arguments],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    value = completed.stdout.strip()
    return value if completed.returncode == 0 and value else None


def _sanitize_remote(value: str) -> str | None:
    value = value.strip()
    if re.match(r"^[^/@\s]+@[^:\s]+:", value):
        host_path = value.split("@", 1)[1].replace(":", "/", 1)
        return host_path.removesuffix(".git").lower()
    parsed = urlsplit(value)
    if parsed.hostname:
        path = parsed.path.strip("/").removesuffix(".git")
        return f"{parsed.hostname.lower()}/{path}".rstrip("/")
    if value and not any(marker in value for marker in ("@", "://")):
        return value.removesuffix(".git")[:500]
    return None
