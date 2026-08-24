"""Conservative deterministic Verified Failure Memory eligibility policy."""

from __future__ import annotations

from semantic_terraform_agent.models import DiagnosisContext
from semantic_terraform_agent.security import redact_secrets


class FailureMemoryPolicy:
    def eligible_for_lookup(
        self,
        context: DiagnosisContext | None,
        *,
        verification_enabled: bool,
    ) -> bool:
        return bool(
            verification_enabled
            and context is not None
            and context.failure.stage in {"validate", "plan"}
            and not context.manifest.ambiguous
            and len(context.resource_blocks) == 1
            and bool(context.resource_blocks[0].source.strip())
        )

    def eligible_for_store(
        self,
        context: DiagnosisContext | None,
        *,
        verification_status: str,
        verification_outcome: str,
        patch: str,
    ) -> bool:
        return bool(
            self.eligible_for_lookup(
                context,
                verification_enabled=True,
            )
            and verification_status
            in {"verified_first_attempt", "verified_after_retry"}
            and verification_outcome == "fully_verified"
            and patch.strip()
            and redact_secrets(patch) == patch
        )
