from __future__ import annotations

from semantic_terraform_agent.config import Limits
from semantic_terraform_agent.context.schema_slicer import SchemaSlicer
from semantic_terraform_agent.models import (
    ChangedLineContext,
    ContextFailure,
    ContextManifest,
    ContextOptimization,
    ContextSourceBlock,
    DiagnosisContext,
    FailureInfo,
    SchemaRecord,
)


def _failure(
    detail: str,
    *,
    summary: str = "Invalid provider configuration",
) -> FailureInfo:
    return FailureInfo(
        summary=summary,
        detail=detail,
        stage="plan",
        resource_address="example_widget.main",
        referenced_file="main.tf",
        referenced_line=1,
        original_log=detail,
    )


def _context(
    source: str,
    *,
    added: list[str] | None = None,
    removed: list[str] | None = None,
) -> DiagnosisContext:
    failure = ContextFailure(
        summary="Invalid provider configuration",
        detail="provider rejected the selected fields",
        stage="plan",
        resource_address="example_widget.main",
        referenced_file="main.tf",
        referenced_line=1,
    )
    changes = []
    if added or removed:
        rendered = "\n".join(
            [
                "--- a/main.tf",
                "+++ b/main.tf",
                "@@ -1 +1 @@",
                *(f"-{line}" for line in removed or []),
                *(f"+{line}" for line in added or []),
            ]
        )
        changes.append(
            ChangedLineContext(
                file="main.tf",
                old_start=1,
                new_start=1,
                added_lines=added or [],
                removed_lines=removed or [],
                rendered=rendered,
            )
        )
    return DiagnosisContext(
        failure=failure,
        changed_lines=changes,
        resource_blocks=[
            ContextSourceBlock(
                kind="resource",
                identifier="example_widget.main",
                file="main.tf",
                start_line=1,
                end_line=len(source.splitlines()),
                source=source,
            )
        ],
        manifest=ContextManifest(
            included_files=["main.tf"],
            included_resources=["example_widget.main"],
            changed_lines=sum(len(change.added_lines) for change in changes),
        ),
        optimization=ContextOptimization(
            available_source_characters=len(source),
            selected_source_characters=len(source),
            characters_avoided=0,
            reduction_ratio=0,
            character_reduction_ratio=0,
            available_source_file_count=1,
            selected_source_file_count=1,
            available_resource_count=1,
            selected_resource_count=1,
        ),
        selected_context_characters=len(source),
    )


def _schema() -> dict:
    return {
        "version": 2,
        "block": {
            "description_kind": "plain",
            "attributes": {
                "mode": {
                    "type": "string",
                    "required": True,
                    "description": "Selects the operating mode.",
                },
                "size": {"type": "number", "optional": True},
                "gp2": {"type": "string", "computed": True},
                "unrelated": {"type": "string", "computed": True},
            },
            "block_types": {
                "rule": {
                    "nesting_mode": "list",
                    "min_items": 1,
                    "max_items": 2,
                    "block": {
                        "description_kind": "plain",
                        "attributes": {
                            "name": {"type": "string", "required": True},
                            "kind": {"type": "string", "required": True},
                            "note": {"type": "string", "optional": True},
                        },
                    },
                },
                "other_rule": {
                    "nesting_mode": "set",
                    "block": {
                        "attributes": {
                            "name": {"type": "string", "required": True}
                        }
                    },
                },
            },
        },
    }


def _slice(
    schema: dict,
    failure: FailureInfo,
    context: DiagnosisContext,
    *,
    limits: Limits | None = None,
):
    return SchemaSlicer(limits or Limits()).slice(
        resource_type="example_widget",
        resource_schema=schema,
        failure=failure,
        diagnosis_context=context,
        provider_source="registry.terraform.io/example/example",
        provider_version="1.2.3",
    )


def test_selects_one_top_level_attribute_and_preserves_complete_definition() -> None:
    schema = _schema()
    context = _context(
        '''resource "example_widget" "main" {
  mode = "unsafe"
  unrelated = "ignored"
}'''
    )
    result = _slice(schema, _failure('Argument "mode" is invalid.'), context)

    assert result.manifest.selected_paths == ["block.attributes.mode"]
    assert result.selected_schema["block"]["attributes"] == {
        "mode": schema["block"]["attributes"]["mode"]
    }
    assert "unrelated" not in result.selected_schema["block"]["attributes"]
    assert result.manifest.selection_reasons["block.attributes.mode"] == [
        "diagnostic_term"
    ]


def test_multiple_diagnostic_fields_selected_but_dynamic_value_is_ignored() -> None:
    schema = _schema()
    schema["block"]["attributes"]["throughput"] = {
        "type": "number",
        "optional": True,
    }
    schema["block"]["attributes"]["type"] = {
        "type": "string",
        "optional": True,
    }
    context = _context(
        '''resource "example_widget" "main" {
  type       = "gp2"
  throughput = 125
}'''
    )
    result = _slice(
        schema,
        _failure("'throughput' must not be set when 'type' is 'gp2'."),
        context,
    )

    assert set(result.manifest.selected_paths) == {
        "block.attributes.throughput",
        "block.attributes.type",
    }
    assert "block.attributes.gp2" not in result.manifest.selected_paths
    assert "gp2" not in result.manifest.unmatched_terms


def test_changed_attribute_selected_and_format_only_change_ignored() -> None:
    context = _context(
        '''resource "example_widget" "main" {
  mode = "unsafe"
  size = 3
}''',
        added=['  mode = "unsafe"', "  size = 3"],
        removed=['mode     = "unsafe"', "  size = 2"],
    )
    result = _slice(_schema(), _failure("provider rejected the value"), context)

    assert result.manifest.selected_paths == ["block.attributes.size"]
    assert result.manifest.selection_reasons["block.attributes.size"] == [
        "changed_attribute"
    ]


def test_whitespace_inside_a_string_is_a_real_changed_attribute() -> None:
    context = _context(
        '''resource "example_widget" "main" {
  mode = "a b"
}''',
        added=['  mode = "a b"'],
        removed=['mode = "ab"'],
    )
    result = _slice(_schema(), _failure("provider rejected the value"), context)

    assert result.manifest.selected_paths == ["block.attributes.mode"]
    assert result.manifest.selection_reasons["block.attributes.mode"] == [
        "changed_attribute"
    ]


def test_nested_attribute_retains_parent_metadata_and_required_sibling() -> None:
    context = _context(
        '''resource "example_widget" "main" {
  rule {
    name = "primary"
    kind = "allow"
    note = "unrelated"
  }
}'''
    )
    result = _slice(_schema(), _failure('Attribute "name" is invalid.'), context)

    assert set(result.manifest.selected_paths) == {
        "block.block_types.rule.block.attributes.name",
        "block.block_types.rule.block.attributes.kind",
    }
    rule = result.selected_schema["block"]["block_types"]["rule"]
    assert rule["nesting_mode"] == "list"
    assert rule["min_items"] == 1
    assert rule["max_items"] == 2
    assert set(rule["block"]["attributes"]) == {"name", "kind"}
    assert "required_sibling" in result.manifest.selection_reasons[
        "block.block_types.rule.block.attributes.kind"
    ]
    assert "note" not in rule["block"]["attributes"]


def test_explicit_nested_block_selects_only_its_required_fields() -> None:
    context = _context(
        '''resource "example_widget" "main" {
  rule {
    name = "primary"
    kind = "allow"
    note = "optional"
  }
}'''
    )
    result = _slice(_schema(), _failure('Block "rule" is incomplete.'), context)

    assert "block.block_types.rule" in result.manifest.selected_paths
    assert set(
        result.selected_schema["block"]["block_types"]["rule"]["block"][
            "attributes"
        ]
    ) == {"name", "kind"}


def test_selected_metadata_and_deprecation_fields_remain_exact() -> None:
    schema = _schema()
    definition = schema["block"]["attributes"]["mode"]
    definition.update(
        {
            "optional": True,
            "computed": True,
            "deprecated": True,
            "deprecation_message": "Use mode_v2 instead.",
            "description_kind": "markdown",
        }
    )
    result = _slice(
        schema,
        _failure('Attribute "mode" is deprecated.'),
        _context('resource "example_widget" "main" { mode = "old" }'),
    )

    assert result.selected_schema["block"]["attributes"]["mode"] == definition


def test_long_description_is_bounded_without_losing_constraint_metadata() -> None:
    schema = _schema()
    definition = schema["block"]["attributes"]["mode"]
    definition["description"] = ("First useful sentence. " + "More details " * 100)
    result = _slice(
        schema,
        _failure('Attribute "mode" is invalid.'),
        _context('resource "example_widget" "main" { mode = "old" }'),
        limits=Limits(max_schema_description_chars_per_field=64),
    )

    selected = result.selected_schema["block"]["attributes"]["mode"]
    assert len(selected["description"]) <= 64
    assert selected["description"].endswith("...[truncated]")
    assert selected["required"] is True
    assert result.manifest.description_truncated_paths == ["block.attributes.mode"]
    assert result.telemetry.description_truncated_count == 1


def test_path_budget_drops_lower_priority_changed_fields_first() -> None:
    context = _context(
        '''resource "example_widget" "main" {
  mode = "bad"
  size = 3
}''',
        added=['  mode = "bad"', "  size = 3"],
        removed=['  mode = "safe"', "  size = 2"],
    )
    result = _slice(
        _schema(),
        _failure('Attribute "mode" is invalid.'),
        context,
        limits=Limits(max_schema_paths=1),
    )

    assert result.manifest.selected_paths == ["block.attributes.mode"]
    assert result.manifest.dropped_paths == ["block.attributes.size"]
    assert result.telemetry.dropped_path_count == 1


def test_path_budget_softly_preserves_all_exact_diagnostic_fields() -> None:
    result = _slice(
        _schema(),
        _failure('Attributes "mode" and "size" conflict.'),
        _context(
            '''resource "example_widget" "main" {
  mode = "bad"
  size = 2
}'''
        ),
        limits=Limits(max_schema_paths=1),
    )

    assert set(result.manifest.selected_paths) == {
        "block.attributes.mode",
        "block.attributes.size",
    }


def test_character_budget_keeps_exact_field_and_drops_lower_priority_field() -> None:
    schema = _schema()
    schema["block"]["attributes"]["mode"]["extra_metadata"] = "x" * 200
    context = _context(
        '''resource "example_widget" "main" {
  mode = "bad"
  size = 3
}''',
        added=["  size = 3"],
        removed=["  size = 2"],
    )
    result = _slice(
        schema,
        _failure('Attribute "mode" is invalid.'),
        context,
        limits=Limits(max_schema_slice_chars=300),
    )

    assert result.manifest.selected_paths == ["block.attributes.mode"]
    assert result.manifest.dropped_paths == ["block.attributes.size"]
    assert result.telemetry.budget_exceeded is True


def test_unknown_term_is_recorded_then_source_attribute_fallback_is_used() -> None:
    result = _slice(
        _schema(),
        _failure('Field "missing_field" is invalid.'),
        _context('''resource "example_widget" "main" {
  mode = "bad"
}'''),
    )

    assert result.manifest.unmatched_terms == ["missing_field"]
    assert result.manifest.selected_paths == ["block.attributes.mode"]
    assert result.telemetry.fallback_used is True
    assert result.telemetry.fallback_reason == "resource_attribute_fallback"


def test_unique_case_insensitive_match_is_low_priority_and_preserves_exact_key() -> None:
    result = _slice(
        _schema(),
        _failure('Attribute "MODE" is invalid.'),
        _context('resource "example_widget" "main" { mode = "bad" }'),
    )

    assert result.manifest.selected_paths == ["block.attributes.mode"]
    assert result.manifest.selection_reasons["block.attributes.mode"] == [
        "diagnostic_case_insensitive"
    ]


def test_nested_depth_budget_falls_back_instead_of_flattening_deep_schema() -> None:
    deep_schema = {
        "version": 1,
        "block": {
            "block_types": {
                "outer": {
                    "nesting_mode": "list",
                    "block": {
                        "block_types": {
                            "inner": {
                                "nesting_mode": "set",
                                "block": {
                                    "attributes": {
                                        "target": {
                                            "type": "string",
                                            "required": True,
                                        }
                                    }
                                },
                            }
                        }
                    },
                }
            }
        },
    }
    context = _context(
        '''resource "example_widget" "main" {
  outer {
    inner {
      target = "bad"
    }
  }
}'''
    )
    result = _slice(
        deep_schema,
        _failure('Attribute "target" is invalid.'),
        context,
        limits=Limits(max_nested_schema_depth=1),
    )

    assert result.telemetry.strategy == "full_schema_fallback"
    assert result.telemetry.fallback_reason == "no_relevant_schema_paths"
    assert result.selected_schema == deep_schema


def test_ambiguous_nested_key_falls_back_safely_instead_of_guessing() -> None:
    result = _slice(
        _schema(),
        _failure('Attribute "name" is invalid.'),
        _context('resource "example_widget" "main" {}'),
    )

    assert result.telemetry.strategy == "full_schema_fallback"
    assert result.telemetry.fallback_reason == "no_relevant_schema_paths"
    assert result.manifest.unmatched_terms == ["name"]
    assert result.selected_schema == _schema()


def test_exact_nested_hcl_path_disambiguates_repeated_schema_key() -> None:
    schema = _schema()
    schema["block"]["attributes"]["name"] = {
        "type": "string",
        "optional": True,
    }
    result = _slice(
        schema,
        _failure('Attribute "name" is invalid.'),
        _context(
            '''resource "example_widget" "main" {
  rule {
    name = "nested"
    kind = "allow"
  }
}'''
        ),
    )

    assert "block.attributes.name" not in result.manifest.selected_paths
    assert "block.block_types.rule.block.attributes.name" in (
        result.manifest.selected_paths
    )


def test_malformed_schema_falls_back_to_full_without_crashing() -> None:
    malformed = {"version": 1, "block": {"attributes": []}}
    result = _slice(
        malformed,
        _failure('Attribute "mode" is invalid.'),
        _context('resource "example_widget" "main" { mode = "bad" }'),
    )

    assert result.selected_schema == malformed
    assert result.telemetry.strategy == "full_schema_fallback"
    assert result.telemetry.fallback_reason == "unsupported_schema_shape"


def test_full_evaluation_mode_preserves_schema_with_explicit_telemetry() -> None:
    record = SchemaRecord(
        resource_type="example_widget",
        provider_source="registry.terraform.io/example/example",
        provider_version="1.2.3",
        extraction_status="ok",
        schema=_schema(),
    )
    result = SchemaSlicer().full_schema(record)

    assert result is not None
    assert result.selected_schema == _schema()
    assert result.telemetry.strategy == "full_schema_evaluation"
    assert result.telemetry.fallback_used is False
    assert result.telemetry.reduction_ratio == 0


def test_character_telemetry_is_exact_and_never_claims_token_reduction() -> None:
    result = _slice(
        _schema(),
        _failure('Attribute "mode" is invalid.'),
        _context('resource "example_widget" "main" { mode = "bad" }'),
    )

    assert result.telemetry.full_schema_characters > (
        result.telemetry.selected_schema_characters
    )
    assert result.telemetry.characters_avoided == (
        result.telemetry.full_schema_characters
        - result.telemetry.selected_schema_characters
    )
    assert result.telemetry.reduction_ratio is not None
    assert result.telemetry.selected_path_count == 1
    assert result.telemetry.input_token_reduction_ratio is None
