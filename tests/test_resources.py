from __future__ import annotations

from semantic_terraform_agent.collectors.failure_log import parse_failure_log
from semantic_terraform_agent.terraform.resources import detect_resources, extract_resource_blocks


def test_extracts_unknown_resource_types_and_nested_blocks() -> None:
    source = '''resource "acme_spaceship" "voyager" {
  settings = {
    engine = "ion"
  }
}
'''
    blocks = extract_resource_blocks({"infra/ship.tf": source})
    assert len(blocks) == 1
    assert blocks[0].address == "acme_spaceship.voyager"
    assert blocks[0].end_line == 5


def test_error_address_drives_high_confidence_detection() -> None:
    source = 'resource "acme_spaceship" "voyager" {\n  engine = "ion"\n}\n'
    failure = parse_failure_log(
        "Error: Unsupported engine\n\nwith acme_spaceship.voyager,\non ship.tf line 2"
    )
    candidates = detect_resources(
        failure, {"infra/ship.tf": source}, (), {}
    )
    assert [item.address for item in candidates] == ["acme_spaceship.voyager"]
    assert candidates[0].confidence == "high"


def test_multiple_changed_resources_are_returned() -> None:
    source = '''resource "alpha_thing" "one" {
  value = 1
}
resource "beta_thing" "two" {
  value = 2
}
'''
    failure = parse_failure_log("Error: provider validation failed")
    candidates = detect_resources(
        failure,
        {"infra/main.tf": source},
        ("infra/main.tf",),
        {"infra/main.tf": (2, 5)},
    )
    assert {item.address for item in candidates} == {"alpha_thing.one", "beta_thing.two"}
    assert all(item.confidence == "medium" for item in candidates)


def test_error_address_is_preserved_when_source_block_is_not_local() -> None:
    failure = parse_failure_log(
        "Error: remote module failed\nwith module.network.acme_gateway.edge[0],"
    )
    candidates = detect_resources(failure, {}, (), {})
    assert candidates[0].address == "module.network.acme_gateway.edge[0]"
    assert candidates[0].resource_type == "acme_gateway"
    assert candidates[0].confidence == "high"
