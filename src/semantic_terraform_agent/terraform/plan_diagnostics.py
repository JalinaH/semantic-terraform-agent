"""Bounded deterministic classification of Terraform plan diagnostics."""

from __future__ import annotations

import json

from semantic_terraform_agent.collectors.failure_log import parse_failure_log
from semantic_terraform_agent.config import DEFAULT_LIMITS
from semantic_terraform_agent.models import (
    PlanFailure,
    PlanFailureClassification,
    PlanFailureReasonCode,
    VerificationCommand,
)
from semantic_terraform_agent.security import redact_secrets


_ENVIRONMENT_CLASSES = {
    "credentials",
    "permissions",
    "network",
    "provider_unavailable",
    "external_service",
    "runtime_environment",
}


def classify_plan_failure(command: VerificationCommand) -> PlanFailure:
    """Parse one already-bounded command result and fail closed on uncertain causes."""
    output = _bounded_redacted(
        f"{command.stdout}\n{command.stderr}".strip(),
        DEFAULT_LIMITS.max_verification_output_chars,
    )
    parsed = parse_failure_log(output)
    diagnostic_format = (
        "terraform_json" if _contains_json_diagnostic(output) else "bounded_text"
    )
    classification, reason_code = _classify(
        f"{parsed.summary}\n{parsed.detail}\n{output}".lower()
    )
    summary = _bounded_redacted(
        parsed.summary or "Terraform plan failed",
        DEFAULT_LIMITS.max_plan_failure_summary_chars,
    )
    detail = _bounded_redacted(
        parsed.detail or output or "Terraform plan failed without diagnostic detail.",
        DEFAULT_LIMITS.max_plan_failure_detail_chars,
    )
    return PlanFailure(
        classification=classification,
        reason_code=reason_code,
        summary=summary or "Terraform plan failed",
        detail=detail or "Terraform plan failed without diagnostic detail.",
        source_file=_optional_bounded(
            parsed.referenced_file,
            DEFAULT_LIMITS.max_plan_failure_source_chars,
        ),
        source_line=parsed.referenced_line,
        resource_address=_optional_bounded(
            parsed.resource_address,
            DEFAULT_LIMITS.max_plan_failure_resource_chars,
        ),
        diagnostic_format=diagnostic_format,
    )


def is_environmental_plan_failure(value: PlanFailure | None) -> bool:
    return bool(value and value.classification in _ENVIRONMENT_CLASSES)


def _classify(
    text: str,
) -> tuple[PlanFailureClassification, PlanFailureReasonCode]:
    credentials = (
        (("nocredentialproviders", "no credential providers", "no valid credential", "unable to locate credentials", "credentials not found", "could not find credential", "failed to retrieve credential"), "aws_no_credentials"),
        (("expiredtoken", "expired token", "request has expired", "security token included in the request is expired"), "aws_expired_credentials"),
        (("invalidclienttokenid", "invalid security token", "security token included in the request is invalid"), "aws_invalid_security_token"),
        (("authentication failure", "authentication failed", "authentication required"), "authentication_failed"),
    )
    permissions = (
        (("unauthorizedoperation",), "aws_unauthorized_operation"),
        (("accessdenied", "access denied", "not authorized to perform"), "aws_access_denied"),
        (("explicit deny", "explicitly denied"), "explicit_deny"),
        (("authorizationerror", "permission denied by service"), "permission_denied"),
    )
    network = (
        (("no such host", "name or service not known", "temporary failure in name resolution", "dns resolution"), "dns_resolution_failed"),
        (("i/o timeout", "connection timeout", "connect timeout", "context deadline exceeded", "tls handshake timeout"), "connection_timeout"),
        (("connection refused",), "connection_refused"),
        (("connection reset",), "connection_reset"),
        (("tls certificate", "tls connectivity", "x509:"), "tls_connectivity_failed"),
        (("network is unreachable", "dial tcp"), "network_unavailable"),
    )
    external = (
        (("too many requests", "rate exceeded", "rate limit", "throttling", "status code: 429", "statuscode: 429"), "external_rate_limited"),
        (("third-party service", "external service unavailable", "remote api unavailable"), "external_service_unavailable"),
    )
    runtime = (
        (("executable file not found", "required runtime dependency", "exec format error"), "runtime_dependency_unavailable"),
        (("no space left on device", "cannot allocate memory", "required environmental prerequisite"), "runtime_prerequisite_unavailable"),
    )
    semantic = (
        (("invalid value for variable", "invalid value for input variable", "invalid variable value"), "invalid_variable_value"),
        (("invalid expression", "invalid character", "syntax error", "unclosed configuration block"), "invalid_expression"),
        (("unsupported argument", "unsupported block type"), "unsupported_argument"),
        (("conflicts with", "conflicting arguments", "invalid combination", "exactly one of", "at least one of"), "conflicting_arguments"),
        (("invalid index", "invalid key", "key does not identify an element"), "invalid_index_or_key"),
        (("missing required argument", "required argument", "required attribute"), "missing_required_argument"),
        (("reference to undeclared", "undeclared resource", "unknown variable", "invalid reference"), "invalid_terraform_reference"),
        (("invalid provider configuration", "provider configuration not present", "provider configuration is invalid"), "invalid_provider_configuration"),
        (("expected one of", "must be one of", "provider-schema", "provider schema", "failed validation", "must not be set"), "provider_schema_constraint"),
        (("invalid resource configuration", "invalid configuration", "incorrect attribute value type"), "invalid_resource_configuration"),
    )
    provider = (
        (("failed to load plugin schemas", "plugin did not respond", "provider plugin", "failed to instantiate provider", "failed to obtain provider schema"), "provider_plugin_unavailable"),
        (("serviceunavailable", "service unavailable", "temporarily unavailable", "status code: 503", "statuscode: 503"), "provider_service_unavailable"),
    )
    for patterns, reason in credentials:
        if any(pattern in text for pattern in patterns):
            return "credentials", reason  # type: ignore[return-value]
    for patterns, reason in permissions:
        if any(pattern in text for pattern in patterns):
            return "permissions", reason  # type: ignore[return-value]
    for patterns, reason in network:
        if any(pattern in text for pattern in patterns):
            return "network", reason  # type: ignore[return-value]
    for patterns, reason in external:
        if any(pattern in text for pattern in patterns):
            return "external_service", reason  # type: ignore[return-value]
    for patterns, reason in runtime:
        if any(pattern in text for pattern in patterns):
            return "runtime_environment", reason  # type: ignore[return-value]
    for patterns, reason in semantic:
        if any(pattern in text for pattern in patterns):
            return "terraform_semantic", reason  # type: ignore[return-value]
    for patterns, reason in provider:
        if any(pattern in text for pattern in patterns):
            return "provider_unavailable", reason  # type: ignore[return-value]
    return "unknown", "unclassified_plan_failure"


def _contains_json_diagnostic(text: str) -> bool:
    for line in text.splitlines():
        try:
            value = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(value, dict):
            continue
        diagnostic = value.get("diagnostic", value)
        if isinstance(diagnostic, dict) and isinstance(diagnostic.get("summary"), str):
            return True
    return False


def _bounded_redacted(value: str, limit: int) -> str:
    redacted = redact_secrets(value).strip()
    if len(redacted) <= limit:
        return redacted
    suffix = "...[truncated]"
    return f"{redacted[: limit - len(suffix)]}{suffix}"


def _optional_bounded(value: str | None, limit: int) -> str | None:
    if not value:
        return None
    result = _bounded_redacted(value, limit)
    return result or None
