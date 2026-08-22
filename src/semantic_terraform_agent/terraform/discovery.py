"""User-selected and progressive context-mode initialization."""

from __future__ import annotations

from typing import Literal

from semantic_terraform_agent.models import ContextSelection, FailureInfo, ResourceCandidate


def select_context_mode(
    requested: Literal["lightweight", "schema-aware", "auto"],
    failure: FailureInfo,
    resources: list[ResourceCandidate],
) -> ContextSelection:
    if requested != "auto":
        return ContextSelection(
            requested_mode=requested,
            selected_mode=requested,
            selection_reason=f"The user explicitly requested {requested} context.",
        )
    return ContextSelection(
        requested_mode="auto",
        selected_mode="progressive",
        selection_reason=(
            "Auto mode starts with deterministic minimal context and may add sliced "
            "provider schema only after verification supplies insufficiency evidence."
        ),
    )
