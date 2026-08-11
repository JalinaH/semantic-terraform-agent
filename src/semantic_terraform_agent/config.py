"""Runtime configuration and bounded-input defaults."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


class AgentError(Exception):
    """Base class for expected, user-facing failures."""


class InputError(AgentError):
    """An input path or input document is invalid."""


class ProviderError(AgentError):
    """The configured LLM provider failed or returned invalid data."""


@dataclass(frozen=True)
class Limits:
    max_log_bytes: int = 2 * 1024 * 1024
    max_diff_bytes: int = 2 * 1024 * 1024
    max_source_bytes: int = 512 * 1024
    max_prompt_log_chars: int = 20_000
    max_patch_bytes: int = 1024 * 1024
    max_verification_output_chars: int = 8_000
    max_command_output_bytes: int = 50 * 1024 * 1024
    command_timeout_seconds: int = 180


DEFAULT_LIMITS = Limits()


def resolve_existing_file(path: Path, *, label: str, max_bytes: int) -> Path:
    resolved = path.expanduser().resolve(strict=True)
    if not resolved.is_file():
        raise InputError(f"{label} is not a regular file: {path}")
    size = resolved.stat().st_size
    if size > max_bytes:
        raise InputError(f"{label} exceeds the {max_bytes}-byte input limit: {path}")
    return resolved
