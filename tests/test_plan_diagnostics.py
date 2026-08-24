from __future__ import annotations

import json

import pytest

from semantic_terraform_agent.models import VerificationCommand
from semantic_terraform_agent.terraform.plan_diagnostics import classify_plan_failure


@pytest.mark.parametrize(
    ("message", "classification", "reason_code"),
    [
        (
            "NoCredentialProviders: no valid providers in chain",
            "credentials",
            "aws_no_credentials",
        ),
        ("ExpiredToken: The security token has expired", "credentials", "aws_expired_credentials"),
        (
            "InvalidClientTokenId: The security token included in the request is invalid",
            "credentials",
            "aws_invalid_security_token",
        ),
        (
            "AccessDenied: is not authorized to perform ec2:DescribeInstances",
            "permissions",
            "aws_access_denied",
        ),
        ("UnauthorizedOperation while calling EC2", "permissions", "aws_unauthorized_operation"),
        ("dial tcp: lookup api.example: no such host", "network", "dns_resolution_failed"),
        ("dial tcp 10.0.0.1:443: i/o timeout", "network", "connection_timeout"),
        ("connection refused by remote host", "network", "connection_refused"),
        ("api error ServiceUnavailable: status code: 503", "provider_unavailable", "provider_service_unavailable"),
        ("provider plugin did not respond", "provider_unavailable", "provider_plugin_unavailable"),
        ("Too Many Requests: status code: 429", "external_service", "external_rate_limited"),
        ("required runtime dependency executable file not found", "runtime_environment", "runtime_dependency_unavailable"),
        ('Invalid value for variable "environment"', "terraform_semantic", "invalid_variable_value"),
        ("Error: Unsupported argument", "terraform_semantic", "unsupported_argument"),
        ("argument mode conflicts with region", "terraform_semantic", "conflicting_arguments"),
        ("Error: Invalid index", "terraform_semantic", "invalid_index_or_key"),
        (
            "Error: Resource precondition failed",
            "terraform_semantic",
            "resource_precondition_failed",
        ),
        (
            "Error: precondition failed",
            "terraform_semantic",
            "resource_precondition_failed",
        ),
        (
            "Error: Module output value precondition failed",
            "terraform_semantic",
            "resource_precondition_failed",
        ),
        (
            "Error: postcondition failed",
            "terraform_semantic",
            "resource_postcondition_failed",
        ),
        (
            "Error: Check block assertion failed",
            "terraform_semantic",
            "check_assertion_failed",
        ),
        ("something happened while planning", "unknown", "unclassified_plan_failure"),
    ],
)
def test_plan_failure_classification_is_deterministic(
    message: str, classification: str, reason_code: str
) -> None:
    failure = classify_plan_failure(
        VerificationCommand(
            command=["terraform", "plan"],
            status="failed",
            exit_code=1,
            stderr=message,
        )
    )
    assert failure.classification == classification
    assert failure.reason_code == reason_code
    assert failure.diagnostic_format == "bounded_text"


def test_terraform_json_diagnostic_preserves_safe_location_fields() -> None:
    diagnostic = json.dumps(
        {
            "type": "diagnostic",
            "diagnostic": {
                "severity": "error",
                "summary": "Invalid value for variable",
                "detail": 'Invalid value for variable "environment".',
                "range": {
                    "filename": "variables.tf",
                    "start": {"line": 8, "column": 1, "byte": 100},
                },
                "address": "var.environment",
            },
        }
    )
    failure = classify_plan_failure(
        VerificationCommand(
            command=["terraform", "plan", "-json"],
            status="failed",
            exit_code=1,
            stdout=diagnostic,
        )
    )
    assert failure.classification == "terraform_semantic"
    assert failure.reason_code == "invalid_variable_value"
    assert failure.diagnostic_format == "terraform_json"
    assert failure.source_file == "variables.tf"
    assert failure.source_line == 8
    assert failure.resource_address == "var.environment"


def test_plan_failure_detail_is_bounded_and_redacted() -> None:
    secret = "AKIA1234567890ABCDEF"
    failure = classify_plan_failure(
        VerificationCommand(
            command=["terraform", "plan"],
            status="failed",
            exit_code=1,
            stderr=f"AccessDenied token={secret} " + "x" * 20_000,
        )
    )
    assert failure.classification == "permissions"
    assert secret not in failure.detail
    assert "[REDACTED]" in failure.detail
    assert len(failure.summary) <= 500
    assert len(failure.detail) <= 2_000
