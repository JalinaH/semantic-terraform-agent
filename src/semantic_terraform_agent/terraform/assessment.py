"""Deterministic verification assessment independent of model reasoning."""

from __future__ import annotations

from semantic_terraform_agent.models import VerificationAssessment, VerificationAttempt
from semantic_terraform_agent.terraform.plan_diagnostics import (
    is_environmental_plan_failure,
)


def assess_verification(attempt: VerificationAttempt) -> VerificationAssessment:
    commands = attempt.commands
    patch_check = _passed(commands.patch_check)
    patch_apply = _passed(commands.patch_apply)
    fmt = _passed(commands.fmt)
    init = _passed(commands.init)
    validate = _passed(commands.terraform_validate)
    plan_attempted = bool(
        commands.plan is not None and commands.plan.status != "skipped"
    )
    plan = _passed(commands.plan) if attempt.plan_requested else None
    local = bool(
        attempt.verification_mode == "local"
        and not attempt.plan_requested
        and not plan_attempted
        and attempt.plan_skip_reason == "cloud_verification_not_configured"
        and attempt.status == "locally_validated"
        and patch_check
        and patch_apply
        and fmt
        and init
        and validate
    )
    full = bool(
        attempt.verification_mode == "full"
        and attempt.plan_requested
        and attempt.status == "verified"
        and patch_check
        and patch_apply
        and fmt
        and init
        and validate
        and plan
    )

    if full:
        outcome = "fully_verified"
    elif local:
        outcome = "locally_validated"
    elif attempt.plan_failure is not None:
        if is_environmental_plan_failure(attempt.plan_failure):
            outcome = "environment_blocked"
        elif attempt.plan_failure.classification == "terraform_semantic":
            outcome = "semantic_failure"
        else:
            outcome = "unknown_failure"
    elif attempt.status == "rejected" or attempt.failed_stage in {
        "patch_check",
        "patch_apply",
        "fmt",
    }:
        outcome = "patch_invalid"
    elif attempt.status == "unavailable":
        outcome = "environment_blocked"
    elif attempt.failed_stage == "validate":
        outcome = "semantic_failure"
    else:
        outcome = "unknown_failure"

    conditionally_eligible = bool(
        local
        or (
            outcome == "environment_blocked"
            and is_environmental_plan_failure(attempt.plan_failure)
            and patch_check
            and patch_apply
            and fmt
            and init
            and validate
            and plan_attempted
            and not plan
        )
    )
    apply_safety = (
        "verified"
        if full
        else ("conditionally_eligible" if conditionally_eligible else "ineligible")
    )
    return VerificationAssessment(
        outcome=outcome,
        verification_mode=attempt.verification_mode,
        plan_requested=attempt.plan_requested,
        patch_check_passed=patch_check,
        patch_apply_passed=patch_apply,
        fmt_passed=fmt,
        init_passed=init,
        validate_passed=validate,
        plan_attempted=plan_attempted,
        plan_passed=plan,
        plan_skip_reason=attempt.plan_skip_reason,
        full_verification_passed=full,
        apply_safety=apply_safety,
        plan_failure=attempt.plan_failure,
    )


def _passed(command) -> bool:
    return command is not None and command.status == "passed"
