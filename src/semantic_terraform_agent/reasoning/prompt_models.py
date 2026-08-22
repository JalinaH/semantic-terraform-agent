"""Prompt payload with deterministic pre-call section measurements."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class PromptParts:
    system: str
    user: str
    section_characters: dict[str, int] = field(default_factory=dict)
    selected_context_characters: int | None = None

    @property
    def combined(self) -> str:
        return f"{self.system}\n\n{self.user}"

    @property
    def prompt_characters(self) -> int:
        return len(self.system) + len(self.user)
