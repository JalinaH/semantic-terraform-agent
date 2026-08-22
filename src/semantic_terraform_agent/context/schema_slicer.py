"""Deterministic provider resource-schema indexing, selection, and pruning."""

from __future__ import annotations

import copy
import json
import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from semantic_terraform_agent.config import DEFAULT_LIMITS, Limits
from semantic_terraform_agent.models import (
    DiagnosisContext,
    FailureInfo,
    SchemaOptimization,
    SchemaRecord,
    SchemaSlice,
    SchemaSliceManifest,
    SchemaSliceTelemetry,
)


_QUOTED_IDENTIFIER = re.compile(
    r"(?P<quote>['\"`])(?P<value>[A-Za-z_][A-Za-z0-9_-]*)(?P=quote)"
)
_RELATION_IDENTIFIER = re.compile(
    r"(?:conflicts?\s+with|required\s+with|argument|attribute|field|parameter)"
    r"\s+['\"`]?([A-Za-z_][A-Za-z0-9_-]*)",
    re.IGNORECASE,
)
_SNAKE_IDENTIFIER = re.compile(r"\b[A-Za-z_][A-Za-z0-9]*_[A-Za-z0-9_-]+\b")
_VALUE_AFTER_RELATION = re.compile(
    r"\b(?:is|equals?|value(?:\s+is)?|set\s+to)\s+"
    r"(?P<quote>['\"`])(?P<value>[A-Za-z_][A-Za-z0-9_-]*)(?P=quote)",
    re.IGNORECASE,
)
_VALUE_LIST = re.compile(
    r"(?:unused\s+attributes?|unmatched\s+indexes?)\s*:\s*\[(.*?)\]",
    re.IGNORECASE | re.DOTALL,
)
_RESOURCE_HEADER = re.compile(
    r'\b(?:resource|data)\s+"(?P<type>[^"]+)"\s+"(?P<name>[^"]+)"'
)
_ASSIGNMENT = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_-]*)\s*=\s*(.*)$")
_BLOCK_START = re.compile(
    r"^\s*([A-Za-z_][A-Za-z0-9_-]*)(?:\s+\"[^\"]*\")*\s*\{"
)
_EXPRESSION_SCOPE = "<expression>"

_PRIORITY = {
    "diagnostic_term": 0,
    "changed_attribute": 1,
    "affected_expression": 2,
    "diagnostic_case_insensitive": 3,
    "referenced_nested_block": 3,
    "required_sibling": 4,
    "related_sibling": 5,
    "resource_attribute_fallback": 6,
}


@dataclass(frozen=True)
class _SchemaEntry:
    path: str
    key: str
    kind: str
    logical_path: tuple[str, ...]
    definition: dict[str, Any]
    depth: int


@dataclass(frozen=True)
class _HCLTerm:
    kind: str
    key: str
    logical_path: tuple[str, ...]
    expression: str
    rendered: str


@dataclass
class _Candidate:
    entry: _SchemaEntry
    reasons: list[str]
    priority: int


class _SchemaIndex:
    def __init__(self, resource_schema: dict[str, Any]) -> None:
        block = resource_schema.get("block")
        if not isinstance(block, dict):
            raise ValueError("resource schema does not contain a block object")
        self.entries: dict[str, _SchemaEntry] = {}
        self.by_key: dict[str, list[_SchemaEntry]] = defaultdict(list)
        self.by_lower_key: dict[str, list[_SchemaEntry]] = defaultdict(list)
        self.by_logical_path: dict[tuple[str, ...], list[_SchemaEntry]] = defaultdict(
            list
        )
        self._walk_block(block, "block", (), 0)

    def _walk_block(
        self,
        block: dict[str, Any],
        block_path: str,
        logical_prefix: tuple[str, ...],
        depth: int,
    ) -> None:
        attributes = block.get("attributes", {})
        block_types = block.get("block_types", {})
        if not isinstance(attributes, dict) or not isinstance(block_types, dict):
            raise ValueError("resource schema block collections must be objects")
        for key, definition in sorted(attributes.items()):
            if not isinstance(key, str) or not isinstance(definition, dict):
                raise ValueError("resource schema attributes must be named objects")
            entry = _SchemaEntry(
                path=f"{block_path}.attributes.{key}",
                key=key,
                kind="attribute",
                logical_path=(*logical_prefix, key),
                definition=definition,
                depth=depth,
            )
            self._add(entry)
        for key, definition in sorted(block_types.items()):
            if not isinstance(key, str) or not isinstance(definition, dict):
                raise ValueError("resource schema block types must be named objects")
            nested = definition.get("block")
            if not isinstance(nested, dict):
                raise ValueError("nested schema block does not contain a block object")
            path = f"{block_path}.block_types.{key}"
            entry = _SchemaEntry(
                path=path,
                key=key,
                kind="block",
                logical_path=(*logical_prefix, key),
                definition=definition,
                depth=depth + 1,
            )
            self._add(entry)
            self._walk_block(
                nested,
                f"{path}.block",
                entry.logical_path,
                depth + 1,
            )

    def _add(self, entry: _SchemaEntry) -> None:
        self.entries[entry.path] = entry
        self.by_key[entry.key].append(entry)
        self.by_lower_key[entry.key.lower()].append(entry)
        self.by_logical_path[entry.logical_path].append(entry)


class SchemaSlicer:
    """Select exact provider-schema paths using only local Terraform evidence."""

    def __init__(self, limits: Limits = DEFAULT_LIMITS) -> None:
        self.limits = limits

    def slice(
        self,
        *,
        resource_type: str,
        resource_schema: dict[str, Any],
        failure: FailureInfo,
        diagnosis_context: DiagnosisContext,
        provider_source: str | None = None,
        provider_version: str | None = None,
    ) -> SchemaSlice:
        full_characters = _json_characters(resource_schema)
        try:
            index = _SchemaIndex(resource_schema)
        except (AttributeError, TypeError, ValueError):
            return self._full_fallback(
                resource_type=resource_type,
                resource_schema=resource_schema,
                provider_source=provider_source,
                provider_version=provider_version,
                reason="unsupported_schema_shape",
                full_characters=full_characters,
            )

        hcl_terms = _extract_hcl_terms(diagnosis_context)
        diagnostic_terms, value_terms = _extract_diagnostic_terms(failure)
        candidates: dict[str, _Candidate] = {}
        unmatched_terms: list[str] = []

        for term in diagnostic_terms:
            entries, matched_case_insensitively, ambiguous = _resolve_schema_key(
                index,
                term,
                hcl_terms,
                maximum_depth=self.limits.max_nested_schema_depth,
            )
            if not entries:
                if term not in value_terms:
                    unmatched_terms.append(term)
                continue
            reason = (
                "diagnostic_case_insensitive"
                if matched_case_insensitively
                else "diagnostic_term"
            )
            if ambiguous:
                unmatched_terms.append(term)
                continue
            for entry in entries:
                _add_candidate(candidates, entry, reason)

        changed_terms = _extract_changed_terms(diagnosis_context, hcl_terms)
        for term in changed_terms:
            for entry in _resolve_hcl_term(index, term, self.limits):
                _add_candidate(candidates, entry, "changed_attribute")

        for term in hcl_terms:
            if term.kind != "attribute" or not _expression_mentions(
                term.expression, value_terms
            ):
                continue
            for entry in _resolve_hcl_term(index, term, self.limits):
                _add_candidate(candidates, entry, "affected_expression")

        for term in hcl_terms:
            if term.kind != "block" or term.key not in diagnostic_terms:
                continue
            for entry in _resolve_hcl_term(index, term, self.limits):
                _add_candidate(candidates, entry, "referenced_nested_block")

        fallback_used = False
        fallback_reason: str | None = None
        if not candidates:
            fallback_used = True
            fallback_reason = "resource_attribute_fallback"
            for term in hcl_terms:
                if term.kind != "attribute":
                    continue
                entries = _resolve_hcl_term(index, term, self.limits)
                for entry in entries:
                    _add_candidate(candidates, entry, "resource_attribute_fallback")
                if len(candidates) >= self.limits.max_schema_paths:
                    break

        if not candidates:
            return self._full_fallback(
                resource_type=resource_type,
                resource_schema=resource_schema,
                provider_source=provider_source,
                provider_version=provider_version,
                reason="no_relevant_schema_paths",
                full_characters=full_characters,
                unmatched_terms=unmatched_terms,
            )

        self._add_required_siblings(candidates, index)
        ordered = sorted(
            candidates.values(),
            key=lambda item: (item.priority, item.entry.path),
        )
        retained: dict[str, _Candidate] = {}
        dropped_paths: list[str] = []
        budget_exceeded = False
        protected_count = sum(item.priority == 0 for item in ordered)
        path_limit = max(self.limits.max_schema_paths, protected_count)

        for candidate in ordered:
            if len(retained) >= path_limit and candidate.priority > 0:
                dropped_paths.append(candidate.entry.path)
                continue
            trial = {**retained, candidate.entry.path: candidate}
            trial_schema, _ = _build_selected_schema(
                resource_schema,
                index,
                set(trial),
                self.limits.max_schema_description_chars_per_field,
            )
            trial_characters = _json_characters(trial_schema)
            if (
                trial_characters > self.limits.max_schema_slice_chars
                and candidate.priority > 0
                and retained
            ):
                dropped_paths.append(candidate.entry.path)
                continue
            if trial_characters > self.limits.max_schema_slice_chars:
                budget_exceeded = True
            retained[candidate.entry.path] = candidate

        if not retained:
            return self._full_fallback(
                resource_type=resource_type,
                resource_schema=resource_schema,
                provider_source=provider_source,
                provider_version=provider_version,
                reason="slice_budget_failure",
                full_characters=full_characters,
                unmatched_terms=unmatched_terms,
            )

        selected_schema, truncated_descriptions = _build_selected_schema(
            resource_schema,
            index,
            set(retained),
            self.limits.max_schema_description_chars_per_field,
        )
        selected_characters = _json_characters(selected_schema)
        characters_avoided = max(full_characters - selected_characters, 0)
        ratio = (
            round(characters_avoided / full_characters, 6)
            if full_characters
            else None
        )
        selected_paths = sorted(
            retained,
            key=lambda path: (retained[path].priority, path),
        )
        reasons = {
            path: sorted(
                retained[path].reasons,
                key=lambda reason: (_PRIORITY.get(reason, 99), reason),
            )
            for path in selected_paths
        }
        manifest = SchemaSliceManifest(
            resource_type=resource_type,
            provider_source=provider_source,
            provider_version=provider_version,
            selected_paths=selected_paths,
            selection_reasons=reasons,
            unmatched_terms=list(dict.fromkeys(unmatched_terms)),
            description_truncated_paths=truncated_descriptions,
            dropped_paths=dropped_paths,
        )
        telemetry = SchemaSliceTelemetry(
            strategy="deterministic_schema_slice_v1",
            full_schema_characters=full_characters,
            selected_schema_characters=selected_characters,
            characters_avoided=characters_avoided,
            reduction_ratio=ratio,
            character_reduction_ratio=ratio,
            input_token_reduction_ratio=None,
            selected_path_count=len(selected_paths),
            fallback_used=fallback_used,
            fallback_reason=fallback_reason,
            description_truncated_count=len(truncated_descriptions),
            dropped_path_count=len(dropped_paths),
            budget_exceeded=budget_exceeded,
        )
        return SchemaSlice(
            resource_type=resource_type,
            provider_source=provider_source,
            provider_version=provider_version,
            schema=selected_schema,
            manifest=manifest,
            telemetry=telemetry,
        )

    def full_schema(
        self,
        record: SchemaRecord,
        *,
        evaluation: bool = True,
    ) -> SchemaSlice | None:
        if record.extraction_status != "ok" or record.resource_schema is None:
            return None
        full_characters = _json_characters(record.resource_schema)
        strategy = "full_schema_evaluation" if evaluation else "full_schema_fallback"
        reason = "evaluation_full_schema" if evaluation else "explicit_full_schema"
        return self._full_fallback(
            resource_type=record.resource_type,
            resource_schema=record.resource_schema,
            provider_source=record.provider_source,
            provider_version=record.provider_version,
            reason=reason,
            full_characters=full_characters,
            strategy=strategy,
            fallback_used=not evaluation,
        )

    def _add_required_siblings(
        self,
        candidates: dict[str, _Candidate],
        index: _SchemaIndex,
    ) -> None:
        parent_logical_paths: set[tuple[str, ...]] = set()
        for candidate in list(candidates.values()):
            entry = candidate.entry
            if entry.kind == "block":
                parent_logical_paths.add(entry.logical_path)
            elif len(entry.logical_path) > 1:
                parent_logical_paths.add(entry.logical_path[:-1])
        for parent in sorted(parent_logical_paths):
            for entries in index.by_logical_path.values():
                for entry in entries:
                    if (
                        entry.kind == "attribute"
                        and entry.logical_path[:-1] == parent
                        and entry.definition.get("required") is True
                        and entry.depth <= self.limits.max_nested_schema_depth
                    ):
                        _add_candidate(candidates, entry, "required_sibling")

    def _full_fallback(
        self,
        *,
        resource_type: str,
        resource_schema: dict[str, Any],
        provider_source: str | None,
        provider_version: str | None,
        reason: str,
        full_characters: int,
        unmatched_terms: list[str] | None = None,
        strategy: str = "full_schema_fallback",
        fallback_used: bool = True,
    ) -> SchemaSlice:
        schema_copy = copy.deepcopy(resource_schema)
        manifest = SchemaSliceManifest(
            resource_type=resource_type,
            provider_source=provider_source,
            provider_version=provider_version,
            selected_paths=["block"],
            selection_reasons={"block": [reason]},
            unmatched_terms=list(dict.fromkeys(unmatched_terms or [])),
        )
        telemetry = SchemaSliceTelemetry(
            strategy=strategy,
            full_schema_characters=full_characters,
            selected_schema_characters=full_characters,
            characters_avoided=0,
            reduction_ratio=0.0 if full_characters else None,
            character_reduction_ratio=0.0 if full_characters else None,
            input_token_reduction_ratio=None,
            selected_path_count=1,
            fallback_used=fallback_used,
            fallback_reason=reason if fallback_used else None,
        )
        return SchemaSlice(
            resource_type=resource_type,
            provider_source=provider_source,
            provider_version=provider_version,
            schema=schema_copy,
            manifest=manifest,
            telemetry=telemetry,
        )


def slice_schema_records(
    records: list[SchemaRecord],
    *,
    failure: FailureInfo,
    diagnosis_context: DiagnosisContext | None,
    strategy: str,
    limits: Limits = DEFAULT_LIMITS,
) -> tuple[list[SchemaSlice], SchemaOptimization | None]:
    slicer = SchemaSlicer(limits)
    slices: list[SchemaSlice] = []
    for record in records:
        if record.extraction_status != "ok" or record.resource_schema is None:
            continue
        if strategy == "full":
            sliced = slicer.full_schema(record)
        elif diagnosis_context is None:
            sliced = slicer._full_fallback(
                resource_type=record.resource_type,
                resource_schema=record.resource_schema,
                provider_source=record.provider_source,
                provider_version=record.provider_version,
                reason="missing_diagnosis_context",
                full_characters=_json_characters(record.resource_schema),
            )
        else:
            sliced = slicer.slice(
                resource_type=record.resource_type,
                resource_schema=record.resource_schema,
                failure=failure,
                diagnosis_context=diagnosis_context,
                provider_source=record.provider_source,
                provider_version=record.provider_version,
            )
        if sliced is not None:
            slices.append(sliced)
    return slices, aggregate_schema_optimization(slices)


def aggregate_schema_optimization(
    slices: list[SchemaSlice],
) -> SchemaOptimization | None:
    if not slices:
        return None
    full = sum(item.telemetry.full_schema_characters for item in slices)
    selected = sum(item.telemetry.selected_schema_characters for item in slices)
    avoided = max(full - selected, 0)
    ratio = round(avoided / full, 6) if full else None
    strategies = list(dict.fromkeys(item.telemetry.strategy for item in slices))
    reasons = list(
        dict.fromkeys(
            item.telemetry.fallback_reason
            for item in slices
            if item.telemetry.fallback_reason
        )
    )
    return SchemaOptimization(
        strategy=strategies[0] if len(strategies) == 1 else "mixed",
        full_schema_characters=full,
        selected_schema_characters=selected,
        characters_avoided=avoided,
        reduction_ratio=ratio,
        character_reduction_ratio=ratio,
        input_token_reduction_ratio=None,
        selected_path_count=sum(
            item.telemetry.selected_path_count for item in slices
        ),
        schema_count=len(slices),
        fallback_used=any(item.telemetry.fallback_used for item in slices),
        fallback_reason=", ".join(reasons) if reasons else None,
        repair_expanded=False,
    )


def _add_candidate(
    candidates: dict[str, _Candidate],
    entry: _SchemaEntry,
    reason: str,
) -> None:
    priority = _PRIORITY[reason]
    existing = candidates.get(entry.path)
    if existing is None:
        candidates[entry.path] = _Candidate(
            entry=entry,
            reasons=[reason],
            priority=priority,
        )
        return
    if reason not in existing.reasons:
        existing.reasons.append(reason)
    existing.priority = min(existing.priority, priority)


def _resolve_schema_key(
    index: _SchemaIndex,
    term: str,
    hcl_terms: list[_HCLTerm],
    *,
    maximum_depth: int,
) -> tuple[list[_SchemaEntry], bool, bool]:
    entries = [
        entry for entry in index.by_key.get(term, []) if entry.depth <= maximum_depth
    ]
    case_insensitive = False
    if not entries:
        lowered = [
            entry
            for entry in index.by_lower_key.get(term.lower(), [])
            if entry.depth <= maximum_depth
        ]
        if len(lowered) == 1:
            entries = lowered
            case_insensitive = True
    if len(entries) <= 1:
        return entries, case_insensitive, False
    source_paths = {term.logical_path for term in hcl_terms if term.key == entries[0].key}
    source_matches = [entry for entry in entries if entry.logical_path in source_paths]
    if len(source_matches) == 1:
        return source_matches, case_insensitive, False
    if source_paths:
        return [], case_insensitive, True
    top_level = [
        entry
        for entry in entries
        if entry.kind == "attribute" and len(entry.logical_path) == 1
    ]
    if len(top_level) == 1:
        return top_level, case_insensitive, False
    return [], case_insensitive, True


def _resolve_hcl_term(
    index: _SchemaIndex,
    term: _HCLTerm,
    limits: Limits,
) -> list[_SchemaEntry]:
    exact = [
        entry
        for entry in index.by_logical_path.get(term.logical_path, [])
        if entry.kind == term.kind
        and entry.depth <= limits.max_nested_schema_depth
    ]
    if exact:
        return exact
    entries = [
        entry
        for entry in index.by_key.get(term.key, [])
        if entry.kind == term.kind
        and entry.depth <= limits.max_nested_schema_depth
    ]
    if len(entries) == 1:
        return entries
    top_level = [entry for entry in entries if len(entry.logical_path) == 1]
    return top_level if len(top_level) == 1 else []


def _extract_diagnostic_terms(
    failure: FailureInfo,
) -> tuple[list[str], set[str]]:
    text = f"{failure.summary}\n{failure.detail}"
    resource_identifiers: set[str] = set()
    for match in _RESOURCE_HEADER.finditer(text):
        resource_identifiers.update((match.group("type"), match.group("name")))
    if failure.resource_address:
        address = re.sub(r"\[[^\]]*\]", "", failure.resource_address)
        parts = address.split(".")
        if "module" in parts:
            parts = parts[parts.index("module") + 2 :]
        if parts and parts[0] == "data":
            parts = parts[1:]
        resource_identifiers.update(parts[-2:])
    value_terms = {
        match.group("value") for match in _VALUE_AFTER_RELATION.finditer(text)
    }
    for match in _VALUE_LIST.finditer(text):
        value_terms.update(
            item.group("value") for item in _QUOTED_IDENTIFIER.finditer(match.group(1))
        )
    terms: list[str] = []
    for match in _QUOTED_IDENTIFIER.finditer(text):
        value = match.group("value")
        if value not in value_terms and value not in resource_identifiers:
            terms.append(value)
    terms.extend(match.group(1) for match in _RELATION_IDENTIFIER.finditer(text))
    terms.extend(match.group(0) for match in _SNAKE_IDENTIFIER.finditer(text))
    return [
        term
        for term in dict.fromkeys(terms)
        if term not in resource_identifiers and term not in value_terms
    ], value_terms


def _extract_hcl_terms(context: DiagnosisContext) -> list[_HCLTerm]:
    result: list[_HCLTerm] = []
    for block in context.resource_blocks:
        result.extend(_parse_hcl_block(block.source))
    unique: dict[tuple[str, tuple[str, ...], str], _HCLTerm] = {}
    for term in result:
        unique.setdefault((term.kind, term.logical_path, term.rendered), term)
    return list(unique.values())


def _parse_hcl_block(source: str) -> list[_HCLTerm]:
    result: list[_HCLTerm] = []
    scopes: list[str | None] = []
    for original in source.splitlines():
        line = _strip_hcl_comment(original)
        leading = re.match(r"^\s*(}+)\s*", line)
        leading_closes = len(leading.group(1)) if leading else 0
        for _ in range(min(leading_closes, len(scopes))):
            scopes.pop()
        if leading:
            line = line[leading.end() :]
        if not line.strip():
            continue
        logical_prefix = tuple(
            item for item in scopes if item not in (None, _EXPRESSION_SCOPE)
        )
        inside_expression = _EXPRESSION_SCOPE in scopes
        assignment = _ASSIGNMENT.match(line)
        block = _BLOCK_START.match(line) if assignment is None else None
        opens, closes = _brace_counts(line)
        pushed = 0
        if assignment is not None and not inside_expression:
            key = assignment.group(1)
            expression = assignment.group(2).strip()
            result.append(
                _HCLTerm(
                    kind="attribute",
                    key=key,
                    logical_path=(*logical_prefix, key),
                    expression=expression,
                    rendered=original.strip(),
                )
            )
            if opens > closes:
                scopes.append(_EXPRESSION_SCOPE)
                pushed = 1
        elif block is not None and not inside_expression:
            key = block.group(1)
            marker = None if key in {"resource", "data"} else key
            result.append(
                _HCLTerm(
                    kind="block",
                    key=key,
                    logical_path=(*logical_prefix, key),
                    expression="",
                    rendered=original.strip(),
                )
            )
            if opens > closes:
                scopes.append(marker)
                pushed = 1
        extra_opens = max(opens - closes - pushed, 0)
        scopes.extend([_EXPRESSION_SCOPE] * extra_opens)
        trailing_closes = max(closes - opens, 0)
        for _ in range(min(trailing_closes, len(scopes))):
            scopes.pop()
    return [term for term in result if term.key not in {"resource", "data"}]


def _extract_changed_terms(
    context: DiagnosisContext,
    hcl_terms: list[_HCLTerm],
) -> list[_HCLTerm]:
    result: list[_HCLTerm] = []
    by_key: dict[str, list[_HCLTerm]] = defaultdict(list)
    by_rendered: dict[str, list[_HCLTerm]] = defaultdict(list)
    for term in hcl_terms:
        by_key[term.key].append(term)
        by_rendered[_normalize_hcl_line(term.rendered)].append(term)
    for change in context.changed_lines:
        added_assignments = _changed_assignment_values(change.added_lines)
        removed_assignments = _changed_assignment_values(change.removed_lines)
        for lines, opposite in (
            (change.added_lines, removed_assignments),
            (change.removed_lines, added_assignments),
        ):
            for line in lines:
                assignment = _ASSIGNMENT.match(line)
                if assignment is not None:
                    key = assignment.group(1)
                    expression = _normalize_hcl_expression(assignment.group(2))
                    if expression in opposite.get(key, set()):
                        continue
                normalized = _normalize_hcl_line(line)
                exact = by_rendered.get(normalized, [])
                if exact:
                    result.extend(exact)
                    continue
                block = _BLOCK_START.match(line) if assignment is None else None
                if assignment is not None:
                    key = assignment.group(1)
                    matches = [
                        item
                        for item in by_key.get(key, [])
                        if item.kind == "attribute"
                    ]
                    if len(matches) == 1:
                        result.extend(matches)
                    elif not matches:
                        result.append(
                            _HCLTerm(
                                kind="attribute",
                                key=key,
                                logical_path=(key,),
                                expression=assignment.group(2).strip(),
                                rendered=line.strip(),
                            )
                        )
                elif block is not None:
                    key = block.group(1)
                    matches = [
                        item for item in by_key.get(key, []) if item.kind == "block"
                    ]
                    if len(matches) == 1:
                        result.extend(matches)
    unique: dict[tuple[str, tuple[str, ...]], _HCLTerm] = {}
    for term in result:
        unique.setdefault((term.kind, term.logical_path), term)
    return list(unique.values())


def _changed_assignment_values(lines: Any) -> dict[str, set[str]]:
    values: dict[str, set[str]] = defaultdict(set)
    for line in lines:
        match = _ASSIGNMENT.match(line)
        if match is not None:
            values[match.group(1)].add(_normalize_hcl_expression(match.group(2)))
    return values


def _build_selected_schema(
    resource_schema: dict[str, Any],
    index: _SchemaIndex,
    selected_paths: set[str],
    description_limit: int,
) -> tuple[dict[str, Any], list[str]]:
    truncated: list[str] = []

    def prune_block(
        block: dict[str, Any],
        block_path: str,
    ) -> dict[str, Any]:
        output: dict[str, Any] = {}
        for key, value in block.items():
            if key not in {"attributes", "block_types", "description"}:
                output[key] = copy.deepcopy(value)
        attributes = block.get("attributes", {})
        selected_attributes: dict[str, Any] = {}
        if isinstance(attributes, dict):
            for key, definition in sorted(attributes.items()):
                path = f"{block_path}.attributes.{key}"
                if path not in selected_paths:
                    continue
                selected_attributes[key] = _copy_definition(
                    definition,
                    path,
                    description_limit,
                    truncated,
                )
        if selected_attributes:
            output["attributes"] = selected_attributes
        block_types = block.get("block_types", {})
        selected_blocks: dict[str, Any] = {}
        if isinstance(block_types, dict):
            for key, definition in sorted(block_types.items()):
                path = f"{block_path}.block_types.{key}"
                relevant = path in selected_paths or any(
                    selected.startswith(f"{path}.block.")
                    for selected in selected_paths
                )
                if not relevant or not isinstance(definition, dict):
                    continue
                parent = {
                    name: copy.deepcopy(value)
                    for name, value in definition.items()
                    if name not in {"block", "description"}
                }
                if isinstance(definition.get("description"), str):
                    parent["description"] = _bounded_description(
                        definition["description"],
                        description_limit,
                        path,
                        truncated,
                    )
                nested = definition.get("block", {})
                parent["block"] = prune_block(nested, f"{path}.block")
                selected_blocks[key] = parent
        if selected_blocks:
            output["block_types"] = selected_blocks
        return output

    result: dict[str, Any] = {
        key: copy.deepcopy(value)
        for key, value in resource_schema.items()
        if key != "block"
    }
    result["block"] = prune_block(resource_schema["block"], "block")
    return result, list(dict.fromkeys(truncated))


def _copy_definition(
    definition: Any,
    path: str,
    description_limit: int,
    truncated: list[str],
) -> Any:
    if not isinstance(definition, dict):
        return copy.deepcopy(definition)
    result = copy.deepcopy(definition)
    description = result.get("description")
    if isinstance(description, str):
        result["description"] = _bounded_description(
            description,
            description_limit,
            path,
            truncated,
        )
    return result


def _bounded_description(
    description: str,
    maximum: int,
    path: str,
    truncated: list[str],
) -> str:
    if len(description) <= maximum:
        return description
    truncated.append(path)
    if maximum <= 0:
        return ""
    marker = "...[truncated]"
    if maximum <= len(marker):
        return marker[:maximum]
    available = maximum - len(marker)
    prefix = description[:available]
    sentence = max(prefix.rfind(". "), prefix.rfind("\n"))
    if sentence >= max(available // 3, 1):
        prefix = prefix[: sentence + 1]
    else:
        boundary = prefix.rfind(" ")
        if boundary > 0:
            prefix = prefix[:boundary]
    return prefix.rstrip() + marker


def _strip_hcl_comment(line: str) -> str:
    quote = False
    escaped = False
    for index, character in enumerate(line):
        if quote:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                quote = False
            continue
        if character == '"':
            quote = True
        elif character == "#":
            return line[:index]
        elif character == "/" and index + 1 < len(line) and line[index + 1] == "/":
            return line[:index]
    return line


def _brace_counts(line: str) -> tuple[int, int]:
    opens = 0
    closes = 0
    quote = False
    escaped = False
    for character in line:
        if quote:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                quote = False
            continue
        if character == '"':
            quote = True
        elif character == "{":
            opens += 1
        elif character == "}":
            closes += 1
    return opens, closes


def _normalize_hcl_line(line: str) -> str:
    return re.sub(r"\s+", " ", line.strip())


def _normalize_hcl_expression(expression: str) -> str:
    result: list[str] = []
    quoted = False
    escaped = False
    for character in _strip_hcl_comment(expression):
        if quoted:
            result.append(character)
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                quoted = False
        elif character == '"':
            quoted = True
            result.append(character)
        elif not character.isspace():
            result.append(character)
    return "".join(result)


def _expression_mentions(expression: str, values: set[str]) -> bool:
    for value in values:
        if re.search(rf"(?<![A-Za-z0-9_-]){re.escape(value)}(?![A-Za-z0-9_-])", expression):
            return True
    return False


def _json_characters(value: Any) -> int:
    return len(json.dumps(value, separators=(",", ":"), sort_keys=True))
