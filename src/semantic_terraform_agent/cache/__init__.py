"""Versioned deterministic caches and Verified Failure Memory."""

from semantic_terraform_agent.cache.fingerprint import (
    VERIFIED_FAILURE_FINGERPRINT_VERSION,
    FailureFingerprint,
    build_failure_fingerprint,
    derive_repository_scope,
)
from semantic_terraform_agent.cache.policy import FailureMemoryPolicy
from semantic_terraform_agent.cache.store import LocalCacheStore

__all__ = [
    "VERIFIED_FAILURE_FINGERPRINT_VERSION",
    "FailureFingerprint",
    "FailureMemoryPolicy",
    "LocalCacheStore",
    "build_failure_fingerprint",
    "derive_repository_scope",
]
