"""Deterministic minimal Terraform context selection and one-hop symbol resolution."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from semantic_terraform_agent.collectors.git_diff import DiffData
from semantic_terraform_agent.collectors.repository import RepositoryLayout
from semantic_terraform_agent.config import DEFAULT_LIMITS, Limits
from semantic_terraform_agent.models import (
    ChangedLineContext,
    ContextBlockKind,
    ContextFailure,
    ContextManifest,
    ContextOptimization,
    ContextSourceBlock,
    DiagnosisContext,
    FailureInfo,
    ResourceCandidate,
)
from semantic_terraform_agent.terraform.resources import (
    ResourceBlock,
    _matching_brace,
    extract_resource_blocks,
)


_HUNK_HEADER = re.compile(
    r"^@@\s+-(?P<old>\d+)(?:,(?P<old_count>\d+))?\s+"
    r"\+(?P<new>\d+)(?:,(?P<new_count>\d+))?\s+@@"
)
_VARIABLE_START = re.compile(
    r'(?m)^\s*variable\s+"(?P<name>[A-Za-z_][A-Za-z0-9_-]*)"\s*\{'
)
_DATA_START = re.compile(
    r'(?m)^\s*data\s+"(?P<type>[A-Za-z0-9_-]+)"\s+'
    r'"(?P<name>[A-Za-z0-9_-]+)"\s*\{'
)
_LOCALS_START = re.compile(r"(?m)^\s*locals\s*\{")
_VAR_REF = re.compile(r"\bvar\.([A-Za-z_][A-Za-z0-9_-]*)")
_LOCAL_REF = re.compile(r"\blocal\.([A-Za-z_][A-Za-z0-9_-]*)")
_MODULE_REF = re.compile(
    r"\bmodule\.([A-Za-z_][A-Za-z0-9_-]*)(?:\.[A-Za-z_][A-Za-z0-9_-]*)?"
)
_DATA_REF = re.compile(
    r"\bdata\.([A-Za-z_][A-Za-z0-9_-]*)\.([A-Za-z_][A-Za-z0-9_-]*)"
)
_RESOURCE_REF = re.compile(
    r"(?<!data\.)\b([A-Za-z_][A-Za-z0-9_-]*)\."
    r"([A-Za-z_][A-Za-z0-9_-]*)\.[A-Za-z_][A-Za-z0-9_-]*"
)
_FILE_REF = re.compile(r"\b(file|templatefile)\s*\(")
_GENERIC_REFERENCE_ROOTS = {
    "var",
    "local",
    "module",
    "data",
    "each",
    "count",
    "path",
    "terraform",
    "self",
}


@dataclass(frozen=True)
class _BlockRecord:
    kind: ContextBlockKind
    identifier: str
    file: str
    start_line: int
    end_line: int
    source: str
    focus_line: int | None = None


@dataclass(frozen=True)
class _ParsedHunk:
    file: str
    old_start: int
    new_start: int
    new_end: int
    added_lines: tuple[str, ...]
    removed_lines: tuple[str, ...]
    context_lines: tuple[str, ...]
    body_lines: tuple[str, ...]
    rendered: str

    @property
    def changed_line_count(self) -> int:
        return len(self.added_lines) + len(self.removed_lines)


def normalize_resource_address(address: str) -> str | None:
    """Reduce indexed/module-prefixed addresses to the source block identity."""
    without_indexes = re.sub(r"\[[^\]]*\]", "", address.strip())
    parts = [part for part in without_indexes.split(".") if part]
    while len(parts) >= 2 and parts[0] == "module":
        parts = parts[2:]
    if parts and parts[0] == "data":
        if len(parts) < 3:
            return None
        return f"data.{parts[1]}.{parts[2]}"
    if len(parts) < 2:
        return None
    return f"{parts[-2]}.{parts[-1]}"


class ContextBuilder:
    """Build the smallest deterministic source context justified by local evidence."""

    def __init__(self, limits: Limits = DEFAULT_LIMITS) -> None:
        self.limits = limits

    def build(
        self,
        *,
        repository: RepositoryLayout,
        failure: FailureInfo,
        diff: DiffData,
        all_sources: dict[str, str],
        detected_resources: list[ResourceCandidate],
        mode: str,
    ) -> DiagnosisContext:
        truncated_sections: list[str] = []
        context_failure = self._failure_context(failure, truncated_sections)
        all_resource_blocks = extract_resource_blocks(all_sources)
        selected_resources, ambiguous = self._select_resources(
            failure,
            detected_resources,
            all_resource_blocks,
            diff.changed_lines,
        )

        used_characters = _failure_characters(context_failure)
        resource_blocks: list[ContextSourceBlock] = []
        for block, identifier, focus_lines in selected_resources:
            remaining = max(self.limits.max_total_context_chars - used_characters, 1)
            bounded, truncated = _bounded_source_block(
                block,
                identifier=identifier,
                focus_lines=focus_lines,
                maximum=min(self.limits.max_resource_block_chars, remaining),
            )
            resource_blocks.append(bounded)
            used_characters += len(bounded.source)
            if truncated:
                truncated_sections.append(
                    f"resource:{identifier}:resource_block_exceeded_limit"
                )

        selected_ranges: dict[str, list[tuple[int, int]]] = {}
        for block, _, _ in selected_resources:
            selected_ranges.setdefault(block.file, []).append(
                (block.start_line, block.end_line)
            )
        selected_files = set(selected_ranges)
        if not selected_files:
            selected_files.update(diff.changed_files)
            if failure.referenced_file:
                selected_files.update(
                    path
                    for path in repository.terraform_files
                    if _same_file(path, failure.referenced_file)
                )
        diff_budget = min(
            self.limits.max_relevant_diff_chars,
            max(self.limits.max_total_context_chars - used_characters, 0),
        )
        changed_context, diff_truncated = _relevant_changed_lines(
            diff.text,
            allowed_files=set(repository.terraform_files),
            selected_files=selected_files,
            selected_ranges=selected_ranges,
            maximum=diff_budget,
            nearby_context_lines=self.limits.diff_context_lines,
        )
        used_characters += sum(len(item.rendered) for item in changed_context)
        if diff_truncated:
            truncated_sections.append("git_diff:relevant_diff_exceeded_limit")

        references = _collect_references(block.source for block in resource_blocks)
        support_budget = min(
            self.limits.max_supporting_context_chars,
            max(self.limits.max_total_context_chars - used_characters, 0),
        )
        supporting_blocks, resolved, unresolved, support_truncations = self._resolve_symbols(
            references,
            all_sources,
            all_resource_blocks,
            selected_resources,
            support_budget,
        )
        truncated_sections.extend(support_truncations)

        included_blocks = [*resource_blocks, *supporting_blocks]
        included_files = list(
            dict.fromkeys(
                [block.file for block in included_blocks]
                + [change.file for change in changed_context]
            )
        )
        included_resources = list(
            dict.fromkeys(
                block.identifier
                for block in included_blocks
                if block.kind in {"resource", "data"}
            )
        )
        changed_line_count = sum(
            len(item.added_lines) + len(item.removed_lines)
            for item in changed_context
        )
        selected_source_characters = sum(len(block.source) for block in included_blocks)
        used_characters += sum(len(block.source) for block in supporting_blocks)
        metadata: dict[str, str | int | bool | None] = {
            "mode": mode,
            "strategy": "deterministic_minimal_v1",
            "ambiguous": ambiguous,
            "reference_depth": self.limits.max_reference_depth,
        }
        metadata_characters = _metadata_characters(metadata, unresolved)
        if used_characters + metadata_characters > self.limits.max_total_context_chars:
            metadata = {}
            metadata_characters = 0
            truncated_sections.append("metadata:total_context_limit")
        available_source_characters = sum(len(source) for source in all_sources.values())
        characters_avoided = max(
            available_source_characters - selected_source_characters, 0
        )
        reduction_ratio = (
            round(characters_avoided / available_source_characters, 6)
            if available_source_characters
            else None
        )
        manifest = ContextManifest(
            included_files=included_files,
            included_resources=included_resources,
            included_symbols=resolved,
            referenced_symbols=references,
            resolved_symbols=resolved,
            unresolved_symbols=unresolved,
            changed_lines=changed_line_count,
            truncated_sections=list(dict.fromkeys(truncated_sections)),
            ambiguous=ambiguous,
        )
        optimization = ContextOptimization(
            available_source_characters=available_source_characters,
            selected_source_characters=selected_source_characters,
            characters_avoided=characters_avoided,
            reduction_ratio=reduction_ratio,
            character_reduction_ratio=reduction_ratio,
            input_token_reduction_ratio=None,
            available_source_file_count=len(all_sources),
            selected_source_file_count=len(
                {block.file for block in included_blocks}
            ),
            available_resource_count=len(all_resource_blocks),
            selected_resource_count=len(
                [block for block in included_blocks if block.kind == "resource"]
            ),
        )
        selected_context_characters = (
            _failure_characters(context_failure)
            + sum(len(item.rendered) for item in changed_context)
            + selected_source_characters
            + metadata_characters
        )
        return DiagnosisContext(
            failure=context_failure,
            changed_lines=changed_context,
            resource_blocks=resource_blocks,
            supporting_blocks=supporting_blocks,
            referenced_symbols=references,
            resolved_symbols=resolved,
            unresolved_symbols=unresolved,
            metadata=metadata,
            manifest=manifest,
            optimization=optimization,
            selected_context_characters=selected_context_characters,
        )

    def _failure_context(
        self, failure: FailureInfo, truncated_sections: list[str]
    ) -> ContextFailure:
        detail = failure.detail
        fixed_characters = len(failure.summary) + sum(
            len(value or "")
            for value in (
                failure.resource_address,
                failure.referenced_file,
                str(failure.referenced_line) if failure.referenced_line else None,
                failure.stage,
            )
        )
        available = max(self.limits.max_diagnostic_context_chars - fixed_characters, 0)
        if len(detail) > available:
            detail = _bounded_text(detail, available)
            truncated_sections.append("terraform_error:diagnostic_exceeded_limit")
        return ContextFailure(
            summary=failure.summary,
            detail=detail,
            stage=failure.stage,
            resource_address=failure.resource_address,
            referenced_file=failure.referenced_file,
            referenced_line=failure.referenced_line,
            diagnostic_excerpt=None,
        )

    def _select_resources(
        self,
        failure: FailureInfo,
        candidates: list[ResourceCandidate],
        blocks: list[ResourceBlock],
        changed_lines: dict[str, tuple[int, ...]],
    ) -> tuple[list[tuple[ResourceBlock, str, tuple[int, ...]]], bool]:
        explicit_identity = (
            normalize_resource_address(failure.resource_address)
            if failure.resource_address
            else None
        )
        if explicit_identity and not explicit_identity.startswith("data."):
            exact = [block for block in blocks if block.address == explicit_identity]
            if exact:
                block = _prefer_file_match(exact, failure.referenced_file)
                return [
                    (
                        block,
                        failure.resource_address or block.address,
                        _focus_lines(block, failure, changed_lines),
                    )
                ], False

        resolved: list[tuple[ResourceBlock, str, tuple[int, ...]]] = []
        seen: set[tuple[str, int, int]] = set()
        for candidate in candidates:
            matches = [
                block
                for block in blocks
                if block.resource_type == candidate.resource_type
                and block.name == candidate.name
                and (not candidate.file or _same_file(block.file, candidate.file))
            ]
            if not matches:
                continue
            block = matches[0]
            key = (block.file, block.start_line, block.end_line)
            if key in seen:
                continue
            seen.add(key)
            resolved.append(
                (
                    block,
                    candidate.address,
                    _focus_lines(block, failure, changed_lines),
                )
            )

        line_specific = bool(
            failure.referenced_line
            and any(
                block.start_line <= failure.referenced_line <= block.end_line
                for block, _, _ in resolved
            )
        )
        ambiguous = (
            not failure.resource_address and not line_specific and len(resolved) > 1
        )
        limit = self.limits.max_context_candidate_blocks if ambiguous else 1
        return resolved[:limit], ambiguous

    def _resolve_symbols(
        self,
        references: list[str],
        all_sources: dict[str, str],
        resource_blocks: list[ResourceBlock],
        selected_resources: list[tuple[ResourceBlock, str, tuple[int, ...]]],
        maximum: int,
    ) -> tuple[list[ContextSourceBlock], list[str], list[str], list[str]]:
        variables = _extract_named_blocks(all_sources, "variable")
        data_blocks = _extract_named_blocks(all_sources, "data")
        locals_by_name = _extract_local_definitions(all_sources)
        selected_keys = {
            (block.file, block.start_line, block.end_line)
            for block, _, _ in selected_resources
        }
        supporting: list[ContextSourceBlock] = []
        resolved: list[str] = []
        unresolved: list[str] = []
        truncations: list[str] = []
        used = 0
        seen_blocks: set[tuple[str, int, int]] = set()

        ordered = sorted(
            references,
            key=lambda item: (0 if item.startswith(("var.", "local.")) else 1),
        )
        for reference in ordered:
            record: _BlockRecord | None = None
            if reference.startswith("var."):
                record = variables.get(reference)
            elif reference.startswith("local."):
                record = locals_by_name.get(reference)
            elif reference.startswith("data."):
                record = data_blocks.get(reference)
            elif reference.startswith(("module.", "file()", "templatefile()")):
                record = None
            else:
                identity = normalize_resource_address(reference)
                matches = [
                    block for block in resource_blocks if block.address == identity
                ]
                if matches:
                    match = matches[0]
                    record = _BlockRecord(
                        kind="resource",
                        identifier=reference,
                        file=match.file,
                        start_line=match.start_line,
                        end_line=match.end_line,
                        source=match.source,
                    )

            if record is None:
                unresolved.append(reference)
                continue
            key = (record.file, record.start_line, record.end_line)
            if key in selected_keys:
                resolved.append(reference)
                continue
            if key in seen_blocks:
                resolved.append(reference)
                continue
            remaining = maximum - used
            if remaining <= 0:
                unresolved.append(reference)
                truncations.append(f"supporting:{reference}:total_context_limit")
                continue
            bounded, truncated = _bounded_record(record, remaining)
            supporting.append(bounded)
            seen_blocks.add(key)
            used += len(bounded.source)
            resolved.append(reference)
            if truncated:
                truncations.append(
                    f"supporting:{reference}:supporting_context_exceeded_limit"
                )
        return (
            supporting,
            list(dict.fromkeys(resolved)),
            list(dict.fromkeys(unresolved)),
            truncations,
        )


def minimal_sources(context: DiagnosisContext) -> dict[str, str]:
    """Adapt structured context to the v0.5 request field without broadening it."""
    grouped: dict[str, list[ContextSourceBlock]] = {}
    for block in [*context.resource_blocks, *context.supporting_blocks]:
        grouped.setdefault(block.file, []).append(block)
    result: dict[str, str] = {}
    for file, blocks in grouped.items():
        ordered = sorted(blocks, key=lambda block: (block.start_line, block.end_line))
        result[file] = "\n\n".join(
            block.source
            for block in ordered
            if block.source
        )
    return result


def minimal_diff(context: DiagnosisContext) -> str:
    return "\n\n".join(item.rendered for item in context.changed_lines)


def _failure_characters(failure: ContextFailure) -> int:
    return sum(
        len(str(value))
        for value in (
            failure.summary,
            failure.detail,
            failure.stage,
            failure.resource_address,
            failure.referenced_file,
            failure.referenced_line,
            failure.diagnostic_excerpt,
        )
        if value is not None
    )


def _metadata_characters(
    metadata: dict[str, str | int | bool | None], unresolved: list[str]
) -> int:
    return sum(len(key) + len(str(value)) for key, value in metadata.items()) + sum(
        len(symbol) for symbol in unresolved
    )


def _prefer_file_match(
    blocks: list[ResourceBlock], failure_file: str | None
) -> ResourceBlock:
    if failure_file:
        for block in blocks:
            if _same_file(block.file, failure_file):
                return block
    return blocks[0]


def _same_file(path: str, reference: str) -> bool:
    normalized = reference.replace("\\", "/")
    return path == normalized or path.endswith(f"/{normalized}")


def _focus_lines(
    block: ResourceBlock,
    failure: FailureInfo,
    changed_lines: dict[str, tuple[int, ...]],
) -> tuple[int, ...]:
    lines = [
        line
        for line in changed_lines.get(block.file, ())
        if block.start_line <= line <= block.end_line
    ]
    if (
        failure.referenced_line
        and failure.referenced_file
        and _same_file(block.file, failure.referenced_file)
        and block.start_line <= failure.referenced_line <= block.end_line
    ):
        lines.append(failure.referenced_line)
    return tuple(sorted(dict.fromkeys(lines)))


def _bounded_source_block(
    block: ResourceBlock,
    *,
    identifier: str,
    focus_lines: tuple[int, ...],
    maximum: int,
) -> tuple[ContextSourceBlock, bool]:
    source, start, end, truncated = _bounded_hcl_excerpt(
        block.source,
        block.start_line,
        block.end_line,
        focus_lines,
        maximum,
    )
    return ContextSourceBlock(
        kind="resource",
        identifier=identifier,
        file=block.file,
        start_line=start,
        end_line=end,
        source=source,
        truncated=truncated,
        truncation_reason="resource_block_exceeded_limit" if truncated else None,
    ), truncated


def _bounded_record(
    block: _BlockRecord, maximum: int
) -> tuple[ContextSourceBlock, bool]:
    source, start, end, truncated = _bounded_hcl_excerpt(
        block.source,
        block.start_line,
        block.end_line,
        (block.focus_line or block.start_line,),
        maximum,
    )
    return ContextSourceBlock(
        kind=block.kind,
        identifier=block.identifier,
        file=block.file,
        start_line=start,
        end_line=end,
        source=source,
        truncated=truncated,
        truncation_reason="supporting_context_exceeded_limit" if truncated else None,
    ), truncated


def _bounded_hcl_excerpt(
    source: str,
    start_line: int,
    end_line: int,
    focus_lines: tuple[int, ...],
    maximum: int,
) -> tuple[str, int, int, bool]:
    if len(source) <= maximum:
        return source, start_line, end_line, False
    lines = source.splitlines(keepends=True)
    if not lines or maximum <= 0:
        return "", start_line, start_line, True
    focus = focus_lines[0] if focus_lines else start_line
    center = min(max(focus - start_line, 0), len(lines) - 1)
    low = center
    high = center
    used = len(lines[center])
    while used < maximum:
        expanded = False
        if low > 0 and used + len(lines[low - 1]) <= maximum:
            low -= 1
            used += len(lines[low])
            expanded = True
        if high + 1 < len(lines) and used + len(lines[high + 1]) <= maximum:
            high += 1
            used += len(lines[high])
            expanded = True
        if not expanded:
            break
    excerpt = "".join(lines[low : high + 1])
    excerpt_start = start_line + low
    excerpt_end = start_line + high
    return excerpt, excerpt_start, excerpt_end, True


def _parse_diff(text: str) -> list[_ParsedHunk]:
    lines = text.splitlines()
    hunks: list[_ParsedHunk] = []
    current_file: str | None = None
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.startswith("+++ "):
            current_file = re.sub(r"^\+\+\+\s+(?:b/)?", "", line).strip()
            if current_file == "/dev/null":
                current_file = None
            index += 1
            continue
        header = _HUNK_HEADER.match(line)
        if header and current_file:
            old_start = int(header.group("old"))
            new_start = int(header.group("new"))
            hunk_lines = [line]
            added: list[str] = []
            removed: list[str] = []
            context: list[str] = []
            new_cursor = new_start
            new_touched: list[int] = []
            index += 1
            while index < len(lines):
                item = lines[index]
                if item.startswith(("@@ ", "diff --git ", "+++ ")):
                    break
                hunk_lines.append(item)
                if item.startswith("+") and not item.startswith("+++"):
                    added.append(item[1:])
                    new_touched.append(new_cursor)
                    new_cursor += 1
                elif item.startswith("-") and not item.startswith("---"):
                    removed.append(item[1:])
                    new_touched.append(new_cursor)
                elif item.startswith(" "):
                    context.append(item[1:])
                    new_cursor += 1
                elif not item.startswith("\\"):
                    new_cursor += 1
                index += 1
            new_end = max(new_touched) if new_touched else max(new_cursor - 1, new_start)
            rendered = (
                f"--- a/{current_file}\n+++ b/{current_file}\n"
                + "\n".join(hunk_lines)
            )
            hunks.append(
                _ParsedHunk(
                    file=current_file,
                    old_start=old_start,
                    new_start=new_start,
                    new_end=new_end,
                    added_lines=tuple(added),
                    removed_lines=tuple(removed),
                    context_lines=tuple(context),
                    body_lines=tuple(hunk_lines[1:]),
                    rendered=rendered,
                )
            )
            continue
        index += 1
    return hunks


def _relevant_changed_lines(
    text: str,
    *,
    allowed_files: set[str],
    selected_files: set[str],
    selected_ranges: dict[str, list[tuple[int, int]]],
    maximum: int,
    nearby_context_lines: int,
) -> tuple[list[ChangedLineContext], bool]:
    selected: list[ChangedLineContext] = []
    used = 0
    truncated = False
    for hunk in _parse_diff(text):
        if hunk.file not in allowed_files:
            continue
        relevant = hunk.file in selected_files
        if hunk.file in selected_ranges:
            relevant = any(
                hunk.new_end >= start and hunk.new_start <= end
                for start, end in selected_ranges[hunk.file]
            )
        if not relevant:
            continue
        remaining = maximum - used
        rendered = hunk.rendered
        included_added = hunk.added_lines
        included_removed = hunk.removed_lines
        included_context = hunk.context_lines
        hunk_truncated = False
        if len(rendered) > remaining:
            bounded = _bounded_diff_hunk(
                hunk,
                remaining,
                nearby_context_lines=nearby_context_lines,
            )
            if bounded is None:
                truncated = True
                continue
            (
                rendered,
                included_added,
                included_removed,
                included_context,
            ) = bounded
            hunk_truncated = True
            truncated = True
        selected.append(
            ChangedLineContext(
                file=hunk.file,
                old_start=hunk.old_start,
                new_start=hunk.new_start,
                added_lines=list(included_added),
                removed_lines=list(included_removed),
                context_lines=list(included_context),
                rendered=rendered,
                truncated=hunk_truncated,
            )
        )
        used += len(rendered)
    return selected, truncated


def _bounded_diff_hunk(
    hunk: _ParsedHunk,
    maximum: int,
    *,
    nearby_context_lines: int,
) -> tuple[str, tuple[str, ...], tuple[str, ...], tuple[str, ...]] | None:
    original_lines = hunk.rendered.splitlines()
    header = original_lines[2] if len(original_lines) > 2 else "@@"
    prefix = (
        f"--- a/{hunk.file}\n+++ b/{hunk.file}\n"
        f"{header}\n"
    )
    changed_indexes = [
        index
        for index, line in enumerate(hunk.body_lines)
        if line.startswith(("+", "-"))
    ]
    if not changed_indexes or len(prefix) >= maximum:
        return None
    selected: set[int] = set()
    used = len(prefix)
    for index in changed_indexes:
        line_size = len(hunk.body_lines[index]) + 1
        if used + line_size > maximum:
            continue
        selected.add(index)
        used += line_size
    if not selected:
        return None
    for distance in range(1, nearby_context_lines + 1):
        candidates: list[int] = []
        for index in changed_indexes:
            candidates.extend((index - distance, index + distance))
        for index in sorted(dict.fromkeys(candidates)):
            if index < 0 or index >= len(hunk.body_lines) or index in selected:
                continue
            line_size = len(hunk.body_lines[index]) + 1
            if used + line_size > maximum:
                continue
            selected.add(index)
            used += line_size
    body = [hunk.body_lines[index] for index in sorted(selected)]
    rendered = prefix + "\n".join(body)
    return (
        rendered,
        tuple(line[1:] for line in body if line.startswith("+")),
        tuple(line[1:] for line in body if line.startswith("-")),
        tuple(line[1:] for line in body if line.startswith(" ")),
    )


def _extract_named_blocks(
    sources: dict[str, str], kind: str
) -> dict[str, _BlockRecord]:
    pattern = _VARIABLE_START if kind == "variable" else _DATA_START
    result: dict[str, _BlockRecord] = {}
    for file, source in sources.items():
        for match in pattern.finditer(source):
            opening = source.find("{", match.start())
            closing = _matching_brace(source, opening)
            start_line = source.count("\n", 0, match.start()) + 1
            end_line = source.count("\n", 0, closing) + 1
            if kind == "variable":
                identifier = f"var.{match.group('name')}"
            else:
                identifier = f"data.{match.group('type')}.{match.group('name')}"
            result.setdefault(
                identifier,
                _BlockRecord(
                    kind=kind,
                    identifier=identifier,
                    file=file,
                    start_line=start_line,
                    end_line=end_line,
                    source=source[match.start() : closing + 1],
                ),
            )
    return result


def _extract_local_definitions(sources: dict[str, str]) -> dict[str, _BlockRecord]:
    result: dict[str, _BlockRecord] = {}
    for file, source in sources.items():
        for match in _LOCALS_START.finditer(source):
            opening = source.find("{", match.start())
            closing = _matching_brace(source, opening)
            block_source = source[match.start() : closing + 1]
            block_start = source.count("\n", 0, match.start()) + 1
            block_end = source.count("\n", 0, closing) + 1
            for offset, line in enumerate(block_source.splitlines()):
                assignment = re.match(
                    r"^\s*([A-Za-z_][A-Za-z0-9_-]*)\s*=\s*(.+)$", line
                )
                if not assignment:
                    continue
                name = assignment.group(1)
                identifier = f"local.{name}"
                value = assignment.group(2)
                simple = _balanced_expression(value)
                result.setdefault(
                    identifier,
                    _BlockRecord(
                        kind="local",
                        identifier=identifier,
                        file=file,
                        start_line=block_start + offset if simple else block_start,
                        end_line=block_start + offset if simple else block_end,
                        source=line if simple else block_source,
                        focus_line=block_start + offset,
                    ),
                )
    return result


def _balanced_expression(value: str) -> bool:
    pairs = {"(": ")", "[": "]", "{": "}"}
    stack: list[str] = []
    quote = False
    escaped = False
    for character in value:
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
        elif character in pairs:
            stack.append(pairs[character])
        elif character in pairs.values():
            if not stack or stack.pop() != character:
                return False
    return not quote and not stack


def _collect_references(sources: Iterable[str]) -> list[str]:
    found: list[tuple[int, int, str]] = []
    position_base = 0
    for source in sources:
        for match in _VAR_REF.finditer(source):
            found.append((position_base + match.start(), 0, f"var.{match.group(1)}"))
        for match in _LOCAL_REF.finditer(source):
            found.append((position_base + match.start(), 0, f"local.{match.group(1)}"))
        for match in _DATA_REF.finditer(source):
            found.append(
                (
                    position_base + match.start(),
                    1,
                    f"data.{match.group(1)}.{match.group(2)}",
                )
            )
        for match in _MODULE_REF.finditer(source):
            found.append((position_base + match.start(), 2, match.group(0)))
        for match in _FILE_REF.finditer(source):
            found.append((position_base + match.start(), 3, f"{match.group(1)}()"))
        for match in _RESOURCE_REF.finditer(source):
            root = match.group(1)
            if root in _GENERIC_REFERENCE_ROOTS:
                continue
            found.append(
                (
                    position_base + match.start(),
                    4,
                    f"{root}.{match.group(2)}",
                )
            )
        position_base += len(source) + 1
    found.sort(key=lambda item: (item[0], item[1]))
    return list(dict.fromkeys(item[2] for item in found))


def _bounded_text(value: str, maximum: int) -> str:
    if maximum <= 0:
        return ""
    if len(value) <= maximum:
        return value
    lines = value.splitlines(keepends=True)
    result: list[str] = []
    used = 0
    for line in lines:
        if used + len(line) > maximum:
            break
        result.append(line)
        used += len(line)
    if result:
        return "".join(result).rstrip()
    return value[:maximum]
