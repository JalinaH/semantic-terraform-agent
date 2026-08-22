"""Storage protocols independent of orchestration and Terraform execution."""

from __future__ import annotations

from typing import Protocol

from semantic_terraform_agent.cache.models import VerifiedFailureEntry


class FailureMemoryStore(Protocol):
    def get_failure(self, fingerprint: str) -> VerifiedFailureEntry | None: ...

    def put_failure(self, entry: VerifiedFailureEntry) -> bool: ...

    def record_rejection(self, fingerprint: str, reason: str) -> None: ...


class DeterministicArtifactStore(Protocol):
    def get_artifact(self, kind: str, key: str) -> dict | None: ...

    def put_artifact(self, kind: str, key: str, payload: dict) -> None: ...
