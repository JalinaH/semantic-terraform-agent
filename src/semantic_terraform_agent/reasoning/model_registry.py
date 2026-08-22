"""Offline deterministic model registry with strict local configuration validation."""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from pathlib import Path

from pydantic import ValidationError

from semantic_terraform_agent.config import (
    InputError,
    ModelRoutingError,
    validate_model_id,
)
from semantic_terraform_agent.models import (
    LLMProviderName,
    ModelDefinition,
    StrictModel,
)


MODEL_REGISTRY_ENV = "SEMANTIC_TERRAFORM_MODEL_REGISTRY_PATH"
OPENROUTER_MODEL_REGISTRY_ENV = "OPENROUTER_MODEL_REGISTRY_PATH"
MAX_REGISTRY_BYTES = 1024 * 1024


class ModelRegistryDocument(StrictModel):
    models: list[ModelDefinition]


class ModelRegistry:
    """Validated immutable collection used by routing without catalog access."""

    def __init__(self, models: Iterable[ModelDefinition]) -> None:
        definitions = tuple(models)
        seen: set[tuple[LLMProviderName, str]] = set()
        for definition in definitions:
            key = (definition.provider, definition.model_id)
            if key in seen:
                raise ModelRoutingError(
                    "model registry contains a duplicate provider/model entry",
                    code="invalid_model_registry",
                )
            seen.add(key)
            try:
                validate_model_id(definition.provider, definition.model_id)
            except InputError as exc:
                raise ModelRoutingError(
                    "model registry contains an invalid model ID",
                    code="invalid_model_registry",
                ) from exc
        self._models = definitions

    @property
    def models(self) -> tuple[ModelDefinition, ...]:
        return self._models

    def find(
        self, provider: LLMProviderName, model_id: str
    ) -> ModelDefinition | None:
        return next(
            (
                item
                for item in self._models
                if item.provider is provider and item.model_id == model_id
            ),
            None,
        )

    def for_provider(self, provider: LLMProviderName) -> list[ModelDefinition]:
        return [item for item in self._models if item.provider is provider]

    @classmethod
    def from_path(cls, path: Path) -> ModelRegistry:
        try:
            resolved = path.expanduser().resolve(strict=True)
            if not resolved.is_file() or resolved.stat().st_size > MAX_REGISTRY_BYTES:
                raise OSError
            raw = json.loads(resolved.read_text(encoding="utf-8"))
            document = ModelRegistryDocument.model_validate(raw)
        except (OSError, json.JSONDecodeError, ValidationError) as exc:
            raise ModelRoutingError(
                "model registry is missing, malformed, oversized, or contains invalid fields",
                code="invalid_model_registry",
            ) from exc
        return cls(document.models)

    @classmethod
    def configured(cls, path: Path | None = None) -> ModelRegistry:
        configured_path = path
        if configured_path is None:
            value = os.environ.get(MODEL_REGISTRY_ENV) or os.environ.get(
                OPENROUTER_MODEL_REGISTRY_ENV
            )
            configured_path = Path(value) if value else None
        return cls.from_path(configured_path) if configured_path else default_registry()


def default_registry() -> ModelRegistry:
    """Minimal stable built-in registry; external availability is not promised."""
    return ModelRegistry(
        [
            ModelDefinition(
                provider=LLMProviderName.GEMINI,
                model_id="gemini-2.5-flash",
                tier="balanced",
                priority=100,
                enabled=True,
                supports_structured_output=True,
                supports_json_fallback=False,
                notes="Built-in compatibility default; tier is local product metadata.",
            )
        ]
    )
