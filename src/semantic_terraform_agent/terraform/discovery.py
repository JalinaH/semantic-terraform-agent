"""Deterministic context selection for the first product version."""

from __future__ import annotations

import re
from typing import Literal

from semantic_terraform_agent.models import ContextSelection, FailureInfo, ResourceCandidate


_ARGUMENT_SIGNAL = re.compile(
    r"(?:argument|attribute|field|parameter)\s+[\"'`]?([A-Za-z_][A-Za-z0-9_-]*)|"
    r"[\"'`]([A-Za-z_][A-Za-z0-9_-]*)[\"'`]\s+(?:is|required|cannot|must|conflicts)",
    re.IGNORECASE,
)
_AMBIGUOUS_SIGNAL = re.compile(
    r"provider produced|provider validation|invalid configuration|invalid value|"
    r"failed validation|unexpected state|inconsistent result|unsupported combination",
    re.IGNORECASE,
)


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
    if len(resources) != 1:
        reason = (
            "No affected resource was identified confidently."
            if not resources
            else f"{len(resources)} resources are plausible, so provider constraints may disambiguate them."
        )
        return ContextSelection(
            requested_mode="auto", selected_mode="schema-aware", selection_reason=reason
        )
    combined = f"{failure.summary}\n{failure.detail}"
    candidate = resources[0]
    if _AMBIGUOUS_SIGNAL.search(combined) and not _ARGUMENT_SIGNAL.search(combined):
        return ContextSelection(
            requested_mode="auto",
            selected_mode="schema-aware",
            selection_reason="The provider/validation diagnostic is ambiguous and does not name a relevant argument.",
        )
    if _ARGUMENT_SIGNAL.search(combined) and candidate.confidence == "high":
        return ContextSelection(
            requested_mode="auto",
            selected_mode="lightweight",
            selection_reason="The diagnostic names an argument or constraint and exactly one resource was identified with high confidence.",
        )
    return ContextSelection(
        requested_mode="auto",
        selected_mode="schema-aware",
        selection_reason="The diagnostic or resource evidence is not specific enough for lightweight context.",
    )

