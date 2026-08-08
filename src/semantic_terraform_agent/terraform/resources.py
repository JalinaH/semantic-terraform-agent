"""Provider-neutral Terraform resource block extraction and candidate ranking."""

from __future__ import annotations

import re
from dataclasses import dataclass

from semantic_terraform_agent.models import FailureInfo, ResourceCandidate


RESOURCE_START = re.compile(
    r'(?m)^\s*resource\s+"(?P<type>[A-Za-z0-9_-]+)"\s+"(?P<name>[A-Za-z0-9_-]+)"\s*\{'
)


@dataclass(frozen=True)
class ResourceBlock:
    address: str
    resource_type: str
    name: str
    file: str
    start_line: int
    end_line: int
    source: str


def _matching_brace(text: str, opening: int) -> int:
    depth = 0
    quote: str | None = None
    escaped = False
    line_comment = False
    block_comment = False
    index = opening
    while index < len(text):
        char = text[index]
        nxt = text[index + 1] if index + 1 < len(text) else ""
        if line_comment:
            if char == "\n":
                line_comment = False
            index += 1
            continue
        if block_comment:
            if char == "*" and nxt == "/":
                block_comment = False
                index += 2
            else:
                index += 1
            continue
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            index += 1
            continue
        if char in ('"', "'"):
            quote = char
        elif char == "#" or (char == "/" and nxt == "/"):
            line_comment = True
            if char == "/":
                index += 1
        elif char == "/" and nxt == "*":
            block_comment = True
            index += 1
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return len(text) - 1


def extract_resource_blocks(sources: dict[str, str]) -> list[ResourceBlock]:
    blocks: list[ResourceBlock] = []
    for path, text in sources.items():
        for match in RESOURCE_START.finditer(text):
            opening = text.find("{", match.start())
            closing = _matching_brace(text, opening)
            start_line = text.count("\n", 0, match.start()) + 1
            end_line = text.count("\n", 0, closing) + 1
            resource_type = match.group("type")
            name = match.group("name")
            blocks.append(
                ResourceBlock(
                    address=f"{resource_type}.{name}",
                    resource_type=resource_type,
                    name=name,
                    file=path,
                    start_line=start_line,
                    end_line=end_line,
                    source=text[match.start() : closing + 1],
                )
            )
    return blocks


def _address_base(address: str) -> str:
    return re.sub(r"\[[^\]]+\]$", "", address)


def _address_type_and_name(address: str) -> tuple[str, str] | None:
    parts = _address_base(address).split(".")
    while len(parts) >= 2 and parts[0] == "module":
        parts = parts[2:]
    if parts and parts[0] == "data":
        parts = parts[1:]
    if len(parts) < 2:
        return None
    return parts[-2], parts[-1]


def detect_resources(
    failure: FailureInfo,
    sources: dict[str, str],
    changed_files: tuple[str, ...],
    changed_lines: dict[str, tuple[int, ...]],
) -> list[ResourceCandidate]:
    blocks = extract_resource_blocks(sources)
    ranked: list[tuple[int, ResourceCandidate]] = []
    matched_explicit_address = False
    failure_file = failure.referenced_file.replace("\\", "/") if failure.referenced_file else None
    for block in blocks:
        evidence: list[str] = []
        score = 0
        if failure.resource_address and _address_base(failure.resource_address).endswith(block.address):
            evidence.append("Terraform diagnostic names this resource address")
            score += 100
            matched_explicit_address = True
        if failure_file and (block.file == failure_file or block.file.endswith(f"/{failure_file}")):
            if failure.referenced_line and block.start_line <= failure.referenced_line <= block.end_line:
                evidence.append("Terraform diagnostic line falls inside this resource block")
                score += 80
            else:
                evidence.append("Terraform diagnostic references this resource file")
                score += 25
        line_hits = [
            line
            for line in changed_lines.get(block.file, ())
            if block.start_line <= line <= block.end_line
        ]
        if line_hits:
            evidence.append("Git diff changes lines inside this resource block")
            score += 50
        elif block.file in changed_files:
            evidence.append("Git diff changes the Terraform file containing this resource")
            score += 15
        if not evidence:
            continue
        confidence = "high" if score >= 75 else "medium" if score >= 40 else "low"
        ranked.append(
            (
                score,
                ResourceCandidate(
                    address=failure.resource_address if score >= 100 else block.address,
                    resource_type=block.resource_type,
                    name=block.name,
                    file=block.file,
                    start_line=block.start_line,
                    end_line=block.end_line,
                    evidence=evidence,
                    confidence=confidence,
                    source=block.source,
                ),
            )
        )
    if failure.resource_address and not matched_explicit_address:
        identity = _address_type_and_name(failure.resource_address)
        if identity:
            resource_type, name = identity
            ranked.append(
                (
                    100,
                    ResourceCandidate(
                        address=failure.resource_address,
                        resource_type=resource_type,
                        name=name,
                        file=failure.referenced_file,
                        start_line=failure.referenced_line,
                        end_line=failure.referenced_line,
                        evidence=["Terraform diagnostic names this resource address"],
                        confidence="high",
                        source="",
                    ),
                )
            )
    ranked.sort(key=lambda item: (-item[0], item[1].address))
    return [candidate for _, candidate in ranked]
