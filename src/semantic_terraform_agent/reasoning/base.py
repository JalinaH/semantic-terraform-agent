"""Provider-neutral reasoning interface."""

from __future__ import annotations

from typing import Protocol

from semantic_terraform_agent.models import DiagnosisRequest, ProviderResponse


class LLMProvider(Protocol):
    def diagnose(self, request: DiagnosisRequest) -> ProviderResponse:
        """Return a strictly validated diagnosis for the supplied evidence."""

