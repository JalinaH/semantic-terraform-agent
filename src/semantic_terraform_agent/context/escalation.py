"""Cheap deterministic policy for one progressive context decision."""

from __future__ import annotations

import re

from semantic_terraform_agent.collectors.failure_log import parse_failure_log
from semantic_terraform_agent.models import (
    ContextLevel,
    DiagnosisContext,
    EscalationDecision,
    FailureInfo,
    ModelDiagnosis,
    PatchFailureCategory,
    VerificationAttempt,
    VerificationCommand,
    VerificationErrorRelation,
)


SEMANTIC_ERROR_PHRASES = (
    "conflicts with",
    "must not be set",
    "required argument",
    "unsupported argument",
    "invalid combination",
    "expected one of",
    "at least one of",
    "exactly one of",
    "provider produced",
    "invalid value for",
    "all attributes must",
    "failed validation",
    "inconsistent result",
    "invalid configuration",
)

SYNTACTIC_ERROR_PHRASES = (
    "invalid expression",
    "invalid character",
    "missing newline",
    "unclosed configuration block",
    "argument or block definition required",
    "unexpected token",
    "syntax error",
    "invalid hcl",
)

CREDENTIAL_ERROR_PHRASES = (
    "no valid credential",
    "could not find credential",
    "failed to retrieve credential",
    "authentication required",
    "access denied",
    "unauthorized",
    "expired token",
)

NETWORK_ERROR_PHRASES = (
    "failed to query available provider packages",
    "no such host",
    "network is unreachable",
    "connection refused",
    "context deadline exceeded",
    "tls handshake timeout",
)

TERRAFORM_LANGUAGE_PLAN_REASON_CODES = {
    "resource_precondition_failed",
    "resource_postcondition_failed",
    "check_assertion_failed",
}

_TERM = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*")
_ASSIGNMENT = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_-]*)\s*=\s*(.*)$")
_STOP_TERMS = {
    "and",
    "argument",
    "attribute",
    "configuration",
    "error",
    "failed",
    "failure",
    "for",
    "from",
    "invalid",
    "must",
    "provider",
    "resource",
    "terraform",
    "that",
    "the",
    "this",
    "value",
    "with",
}


class ContextEscalationPolicy:
    """Choose stop, repair, or minimal-to-schema escalation without I/O."""

    def decide(
        self,
        *,
        requested_mode: str,
        failure: FailureInfo,
        diagnosis_context: DiagnosisContext | None,
        initial_diagnosis: ModelDiagnosis,
        verification: VerificationAttempt,
        schema_eligible: bool,
        second_attempt_enabled: bool,
    ) -> EscalationDecision:
        del initial_diagnosis  # Reserved structured signal for later policy versions.
        relation = classify_verification_error(failure, verification)
        level = (
            ContextLevel.SCHEMA
            if requested_mode == "schema-aware"
            else ContextLevel.MINIMAL
        )
        signals = _base_signals(verification, relation)

        if verification.status == "verified":
            return _stop(
                level,
                "verification_passed",
                "The first candidate passed isolated verification.",
                signals,
                relation,
            )
        if verification.status == "skipped":
            return _stop(
                level,
                "verification_skipped",
                "Verification was skipped, so insufficiency cannot be established.",
                signals,
                relation,
            )
        if (
            verification.status == "rejected"
            and verification.failure_category
            is PatchFailureCategory.MALFORMED_REPAIRABLE
        ):
            if second_attempt_enabled:
                return _repair(
                    level,
                    "malformed_patch",
                    "The intended Terraform change is in scope, but its candidate representation or deterministic edit construction is invalid.",
                    signals,
                    relation,
                )
            return _stop(
                level,
                "second_attempt_disabled",
                "The invalid candidate representation is repairable, but the bounded second attempt is disabled.",
                signals,
                relation,
            )
        if verification.status == "rejected":
            return _stop(
                level,
                "unsafe_patch",
                "The candidate failed patch safety validation.",
                signals,
                relation,
            )
        if verification.status == "unavailable" or relation is VerificationErrorRelation.ENVIRONMENT_FAILURE:
            code = _environment_reason_code(verification)
            return _stop(
                level,
                code,
                "The verification environment is unavailable; more model context cannot help.",
                signals,
                relation,
            )
        if (
            verification.failed_stage == "plan"
            and verification.plan_failure is not None
            and verification.plan_failure.classification == "unknown"
        ):
            return _stop(
                level,
                "no_actionable_failure",
                "Terraform plan failed for an unclassified reason; fail closed without another model call.",
                signals,
                relation,
            )
        if not second_attempt_enabled:
            return _stop(
                level,
                "second_attempt_disabled",
                "The bounded second model attempt is disabled.",
                signals,
                relation,
            )

        stage = verification.failed_stage
        actionable = {"patch_check", "fmt", "validate", "plan"}
        if requested_mode != "auto":
            if verification.status == "failed" and stage in actionable:
                code = "formatting_failure" if stage == "fmt" else "explicit_mode_repair"
                return _repair(
                    level,
                    code,
                    "Explicit context mode keeps its original one-repair behavior.",
                    signals,
                    relation,
                )
            code = "patch_apply_failure" if stage == "patch_apply" else "no_actionable_failure"
            return _stop(
                level,
                code,
                "The failure is not eligible for the existing repair policy.",
                signals,
                relation,
            )

        if stage == "patch_check":
            return _repair(
                level,
                "patch_check_failure",
                "Patch construction failed before Terraform semantic verification.",
                signals,
                relation,
            )
        if stage == "patch_apply":
            return _stop(
                level,
                "patch_apply_failure",
                "Patch application failure is not evidence that provider schema is missing.",
                signals,
                relation,
            )
        if stage == "fmt":
            return _repair(
                level,
                "formatting_failure",
                "Formatting failure is a local patch correction, not a schema insufficiency.",
                signals,
                relation,
            )
        if stage == "init":
            return _stop(
                level,
                "environment_unavailable",
                "Initialization failure does not justify another model call.",
                signals,
                relation,
            )
        if relation is VerificationErrorRelation.NEW_SYNTACTIC_FAILURE:
            return _repair(
                level,
                "syntactic_patch_failure",
                "The candidate introduced a new syntax error that schema cannot resolve.",
                signals,
                relation,
            )

        if (
            stage == "plan"
            and verification.plan_failure is not None
            and verification.plan_failure.reason_code
            in TERRAFORM_LANGUAGE_PLAN_REASON_CODES
        ):
            return _repair(
                level,
                "terraform_language_semantic_failure",
                "Terraform reported a language-level assertion failure; use source-backed semantic repair without provider schema.",
                [*signals, "Terraform language assertion does not require provider schema"],
                relation,
            )

        ambiguous = bool(
            diagnosis_context
            and (
                diagnosis_context.manifest.ambiguous
                or len(diagnosis_context.manifest.included_resources) > 1
            )
        )
        relevant_unresolved = _relevant_unresolved_symbol(failure, diagnosis_context)
        semantic = relation in {
            VerificationErrorRelation.SAME_FAILURE,
            VerificationErrorRelation.NEW_SEMANTIC_FAILURE,
        }
        if stage in {"validate", "plan"} and schema_eligible:
            if relation is VerificationErrorRelation.SAME_FAILURE:
                return _escalate(
                    "provider_constraint_unresolved",
                    "The post-patch diagnostic matches the original semantic failure.",
                    [*signals, "original failure remained after the minimal patch"],
                    relation,
                )
            if semantic:
                return _escalate(
                    "verification_semantic_failure",
                    "Terraform reported a provider-semantic failure after the minimal patch.",
                    [*signals, "provider schema was not included in the first call"],
                    relation,
                )
            if ambiguous:
                return _escalate(
                    "ambiguous_resource",
                    "Bounded resource ambiguity remains after semantic verification.",
                    [*signals, "minimal context contains multiple candidate resources"],
                    relation,
                )
            if relevant_unresolved:
                return _escalate(
                    "unresolved_supporting_symbol",
                    "A failure-relevant attribute depends on an unresolved symbol.",
                    [*signals, "failure-relevant expression has an unresolved symbol"],
                    relation,
                )

        if verification.status == "failed" and stage in actionable:
            return _repair(
                level,
                "insufficient_evidence",
                "No deterministic signal justifies schema escalation; use the bounded repair.",
                signals,
                relation,
            )
        return _stop(
            level,
            "no_actionable_failure",
            "The verification result does not justify another model call.",
            signals,
            relation,
        )


def classify_verification_error(
    failure: FailureInfo,
    verification: VerificationAttempt,
) -> VerificationErrorRelation:
    if (
        verification.status == "unavailable"
        or verification.failure_category is PatchFailureCategory.ENVIRONMENT_FAILURE
    ):
        return VerificationErrorRelation.ENVIRONMENT_FAILURE
    output = _failed_output(verification)
    lowered = output.lower()
    if verification.plan_failure is not None:
        if verification.plan_failure.classification == "terraform_semantic":
            if _same_failure(failure, output, verification.failed_stage):
                return VerificationErrorRelation.SAME_FAILURE
            return VerificationErrorRelation.NEW_SEMANTIC_FAILURE
        if verification.plan_failure.classification == "unknown":
            return VerificationErrorRelation.UNKNOWN
    if any(phrase in lowered for phrase in (*CREDENTIAL_ERROR_PHRASES, *NETWORK_ERROR_PHRASES)):
        return VerificationErrorRelation.ENVIRONMENT_FAILURE
    if any(phrase in lowered for phrase in SYNTACTIC_ERROR_PHRASES):
        return VerificationErrorRelation.NEW_SYNTACTIC_FAILURE
    if _same_failure(failure, output, verification.failed_stage):
        return VerificationErrorRelation.SAME_FAILURE
    if any(phrase in lowered for phrase in SEMANTIC_ERROR_PHRASES):
        return VerificationErrorRelation.NEW_SEMANTIC_FAILURE
    return VerificationErrorRelation.UNKNOWN


def _same_failure(
    failure: FailureInfo,
    output: str,
    failed_stage: str | None,
) -> bool:
    if not output.strip() or failed_stage != failure.stage:
        return False
    parsed = parse_failure_log(output)
    original_terms = _terms(f"{failure.summary}\n{failure.detail}")
    verification_terms = _terms(f"{parsed.summary}\n{parsed.detail}")
    overlap = original_terms & verification_terms
    denominator = min(len(original_terms), len(verification_terms))
    term_match = len(overlap) >= 2 and denominator > 0 and len(overlap) / denominator >= 0.4
    if not term_match:
        return False
    if failure.resource_address and parsed.resource_address:
        return failure.resource_address == parsed.resource_address
    return True


def _terms(value: str) -> set[str]:
    return {
        item.lower()
        for item in _TERM.findall(value)
        if len(item) > 2 and item.lower() not in _STOP_TERMS
    }


def _failed_command(verification: VerificationAttempt) -> VerificationCommand | None:
    stage = verification.failed_stage
    if stage is None:
        return None
    attribute = "terraform_validate" if stage == "validate" else stage
    return getattr(verification.commands, attribute)


def _failed_output(verification: VerificationAttempt) -> str:
    command = _failed_command(verification)
    if command is None:
        return "\n".join(verification.warnings)
    return f"{command.stdout}\n{command.stderr}\n" + "\n".join(
        verification.warnings
    )


def _environment_reason_code(verification: VerificationAttempt) -> str:
    lowered = _failed_output(verification).lower()
    if any(phrase in lowered for phrase in CREDENTIAL_ERROR_PHRASES):
        return "credentials_unavailable"
    if any(phrase in lowered for phrase in NETWORK_ERROR_PHRASES):
        return "provider_network_failure"
    return "environment_unavailable"


def _relevant_unresolved_symbol(
    failure: FailureInfo,
    context: DiagnosisContext | None,
) -> bool:
    if context is None or not context.unresolved_symbols:
        return False
    diagnostic_terms = _terms(f"{failure.summary}\n{failure.detail}")
    for block in context.resource_blocks:
        for line in block.source.splitlines():
            assignment = _ASSIGNMENT.match(line)
            if assignment is None or assignment.group(1).lower() not in diagnostic_terms:
                continue
            expression = assignment.group(2)
            if any(symbol in expression for symbol in context.unresolved_symbols):
                return True
            if any(marker in expression for marker in ("module.", "file(", "templatefile(")):
                return True
    return False


def _base_signals(
    verification: VerificationAttempt,
    relation: VerificationErrorRelation,
) -> list[str]:
    signals = [f"verification status is {verification.status}"]
    if verification.failed_stage:
        signals.append(f"verification failed at {verification.failed_stage}")
    if verification.failure_category is not None:
        signals.append(f"patch failure category is {verification.failure_category.value}")
    if verification.failure_reason_code is not None:
        signals.append(f"patch failure reason is {verification.failure_reason_code}")
    signals.append(f"verification error relation is {relation.value}")
    return signals


def _stop(
    level: ContextLevel,
    code: str,
    reason: str,
    signals: list[str],
    relation: VerificationErrorRelation,
) -> EscalationDecision:
    return EscalationDecision(
        action="stop",
        should_escalate=False,
        should_repair=False,
        from_level=level,
        reason_code=code,
        reason=reason,
        signals=signals[:8],
        verification_error_relation=relation,
    )


def _repair(
    level: ContextLevel,
    code: str,
    reason: str,
    signals: list[str],
    relation: VerificationErrorRelation,
) -> EscalationDecision:
    return EscalationDecision(
        action="repair",
        should_escalate=False,
        should_repair=True,
        from_level=level,
        to_level=level,
        reason_code=code,
        reason=reason,
        signals=signals[:8],
        verification_error_relation=relation,
    )


def _escalate(
    code: str,
    reason: str,
    signals: list[str],
    relation: VerificationErrorRelation,
) -> EscalationDecision:
    return EscalationDecision(
        action="escalate",
        should_escalate=True,
        should_repair=False,
        from_level=ContextLevel.MINIMAL,
        to_level=ContextLevel.SCHEMA,
        reason_code=code,
        reason=reason,
        signals=signals[:8],
        verification_error_relation=relation,
    )
