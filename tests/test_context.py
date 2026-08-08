from semantic_terraform_agent.collectors.failure_log import parse_failure_log
from semantic_terraform_agent.models import ResourceCandidate
from semantic_terraform_agent.terraform.discovery import select_context_mode


def candidate(confidence: str = "high") -> ResourceCandidate:
    return ResourceCandidate(
        address="example_widget.primary",
        resource_type="example_widget",
        name="primary",
        file="infra/main.tf",
        start_line=1,
        end_line=3,
        evidence=["test"],
        confidence=confidence,
        source='resource "example_widget" "primary" {}',
    )


def test_explicit_lightweight_context() -> None:
    selected = select_context_mode(
        "lightweight", parse_failure_log("Error: vague"), []
    )
    assert selected.selected_mode == "lightweight"


def test_explicit_schema_aware_context() -> None:
    selected = select_context_mode(
        "schema-aware", parse_failure_log("Error: clear"), [candidate()]
    )
    assert selected.selected_mode == "schema-aware"


def test_auto_uses_lightweight_for_named_argument_and_one_confident_resource() -> None:
    failure = parse_failure_log(
        'Error: Invalid argument\nArgument "mode" must be "safe"\nwith example_widget.primary,'
    )
    selected = select_context_mode("auto", failure, [candidate()])
    assert selected.selected_mode == "lightweight"


def test_auto_uses_schema_for_ambiguous_or_multiple_resources() -> None:
    ambiguous = parse_failure_log("Error: Provider validation failed")
    assert select_context_mode("auto", ambiguous, [candidate()]).selected_mode == "schema-aware"
    assert (
        select_context_mode("auto", ambiguous, [candidate(), candidate("medium")]).selected_mode
        == "schema-aware"
    )

