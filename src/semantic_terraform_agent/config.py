"""Runtime configuration and bounded-input defaults."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from semantic_terraform_agent.models import (
    LLMProviderName,
    ProviderFailureCategory,
)


class AgentError(Exception):
    """Base class for expected, user-facing failures."""


class InputError(AgentError):
    """An input path or input document is invalid."""


class ProviderError(AgentError):
    """The configured LLM provider failed or returned invalid data."""

    def __init__(
        self,
        message: str,
        *,
        category: ProviderFailureCategory = ProviderFailureCategory.PROVIDER_UNAVAILABLE,
    ) -> None:
        super().__init__(message)
        self.category = category


@dataclass(frozen=True)
class Limits:
    max_log_bytes: int = 2 * 1024 * 1024
    max_diff_bytes: int = 2 * 1024 * 1024
    max_source_bytes: int = 512 * 1024
    max_prompt_log_chars: int = 20_000
    max_patch_bytes: int = 1024 * 1024
    max_verification_output_chars: int = 8_000
    max_diagnostic_context_chars: int = 4_000
    max_relevant_diff_chars: int = 6_000
    max_resource_block_chars: int = 12_000
    max_supporting_context_chars: int = 8_000
    max_total_context_chars: int = 26_000
    max_context_candidate_blocks: int = 3
    diff_context_lines: int = 3
    max_reference_depth: int = 1
    max_schema_slice_chars: int = 8_000
    max_schema_description_chars_per_field: int = 400
    max_schema_paths: int = 32
    max_nested_schema_depth: int = 4
    max_command_output_bytes: int = 50 * 1024 * 1024
    command_timeout_seconds: int = 180


DEFAULT_LIMITS = Limits()

DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"
MAX_MODEL_ID_LENGTH = 200
_OPENROUTER_MODEL_ID = re.compile(
    r"^~?[A-Za-z0-9][A-Za-z0-9._~-]*/[A-Za-z0-9][A-Za-z0-9._:+~-]*$"
)


def provider_names() -> tuple[str, ...]:
    return tuple(provider.value for provider in LLMProviderName)


def parse_provider_name(value: str | LLMProviderName) -> LLMProviderName:
    if isinstance(value, LLMProviderName):
        return value
    try:
        return LLMProviderName(value)
    except ValueError as exc:
        supported = ", ".join(provider_names())
        raise InputError(f"unsupported provider {value!r}; choose one of: {supported}") from exc


def validate_model_id(provider: str | LLMProviderName, model: str) -> str:
    provider_name = parse_provider_name(provider)
    if not model or len(model) > MAX_MODEL_ID_LENGTH:
        raise InputError(
            f"model ID must contain between 1 and {MAX_MODEL_ID_LENGTH} characters"
        )
    if any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in model):
        raise InputError("model ID must not contain whitespace or control characters")
    if provider_name is LLMProviderName.OPENROUTER and not _OPENROUTER_MODEL_ID.fullmatch(model):
        raise InputError(
            "OpenRouter model ID must use the provider/model form, optionally with a variant such as :free"
        )
    return model


def resolve_existing_file(path: Path, *, label: str, max_bytes: int) -> Path:
    resolved = path.expanduser().resolve(strict=True)
    if not resolved.is_file():
        raise InputError(f"{label} is not a regular file: {path}")
    size = resolved.stat().st_size
    if size > max_bytes:
        raise InputError(f"{label} exceeds the {max_bytes}-byte input limit: {path}")
    return resolved
