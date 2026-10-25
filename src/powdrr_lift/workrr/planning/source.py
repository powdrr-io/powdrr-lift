"""Pinned Markdown sources, obligation anchors, and source accounting."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import PurePosixPath
from typing import Any, Literal

from powdrr_lift.core.instruction_ledger import (
    InstructionLedger,
    compile_instruction_ledger,
)
from powdrr_lift.workrr.planning.contracts import PlanningContractError, Question
from powdrr_lift.workrr.planning.questions import (
    ContextRecord,
    QuestionRequest,
    SourceExcerpt,
)

MAX_SOURCE_BLOCK_CHARS = 8_000
MAX_CLAUSES_PER_SOURCE_BLOCK = 64
_SHA_RE = re.compile(r"^[0-9a-f]{40,64}$")
_REQUIREMENT_KINDS = frozenset({"behavior", "constraint", "acceptance", "human_setup"})
_ENUMERATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["records", "has_more", "next_anchor"],
    "properties": {
        "records": {
            "type": "array",
            "maxItems": 8,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "statement",
                    "kind",
                    "source_refs",
                    "related_source_refs",
                ],
                "properties": {
                    "statement": {"type": "string", "minLength": 1},
                    "kind": {"enum": sorted(_REQUIREMENT_KINDS)},
                    "source_refs": {
                        "type": "array",
                        "minItems": 1,
                        "items": {"type": "string"},
                    },
                    "related_source_refs": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
            },
        },
        "has_more": {"type": "boolean"},
        "next_anchor": {"type": ["string", "null"]},
    },
    "allOf": [
        {
            "if": {"properties": {"has_more": {"const": True}}},
            "then": {"properties": {"next_anchor": {"type": "string", "minLength": 1}}},
            "else": {"properties": {"next_anchor": {"type": "null"}}},
        }
    ],
}
_DISPOSITION_SCHEMA: dict[str, Any] = {
    "oneOf": [
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["disposition", "requirement_ids"],
            "properties": {
                "disposition": {"const": "normative"},
                "requirement_ids": {
                    "type": "array",
                    "minItems": 1,
                    "items": {"type": "string"},
                },
            },
        },
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["disposition", "requirement_ids", "reason"],
            "properties": {
                "disposition": {"const": "duplicate"},
                "requirement_ids": {
                    "type": "array",
                    "minItems": 1,
                    "items": {"type": "string"},
                },
                "reason": {"type": "string", "minLength": 1},
            },
        },
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["disposition", "reason"],
            "properties": {
                "disposition": {"const": "context"},
                "reason": {"type": "string", "minLength": 1},
            },
        },
    ]
}
_FIDELITY_SCHEMA: dict[str, Any] = {
    "oneOf": [
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["decision", "reason"],
            "properties": {
                "decision": {"const": "accept"},
                "reason": {"type": "string", "minLength": 1},
            },
        },
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["decision", "statement", "reason"],
            "properties": {
                "decision": {"const": "revise"},
                "statement": {"type": "string", "minLength": 1},
                "reason": {"type": "string", "minLength": 1},
            },
        },
    ]
}
_EQUIVALENCE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["equivalent", "reason"],
    "properties": {
        "equivalent": {"type": "boolean"},
        "reason": {"type": "string", "minLength": 1},
    },
}


class PlanningSourceError(ValueError):
    """Invalid or unavailable pinned planning source."""


@dataclass(frozen=True, slots=True)
class PinnedSourceDocument:
    repository: str
    commit_sha: str
    path: str
    blob_sha: str
    content_sha256: str
    text: str


@dataclass(frozen=True, slots=True)
class SourceAnchor:
    id: str
    source_blob_sha: str
    block_id: str
    clause_id: str | None
    start_line: int
    end_line: int
    start_offset: int
    end_offset: int
    quote_sha256: str

    def to_data(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source_blob_sha": self.source_blob_sha,
            "block_id": self.block_id,
            "clause_id": self.clause_id,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "start_offset": self.start_offset,
            "end_offset": self.end_offset,
            "quote_sha256": self.quote_sha256,
        }


@dataclass(frozen=True, slots=True)
class SourceBlock:
    id: str
    source_blob_sha: str
    kind: str
    text: str
    start_line: int
    end_line: int
    start_offset: int
    end_offset: int
    heading_context: tuple[str, ...]
    clause_ids: tuple[str, ...]
    anchor_ids: tuple[str, ...]
    parent_block_id: str | None = None
    previous_block_id: str | None = None
    next_block_id: str | None = None
    continuation_of: str | None = None
    continuation_index: int | None = None
    continuation_count: int | None = None
    source_path: str = ""


@dataclass(frozen=True, slots=True)
class SegmentedSource:
    document: PinnedSourceDocument
    instruction_ledger: InstructionLedger
    blocks: tuple[SourceBlock, ...]
    anchors: tuple[SourceAnchor, ...]

    def validate(self) -> None:
        if (
            hashlib.sha256(self.document.text.encode("utf-8")).hexdigest()
            != self.document.content_sha256
        ):
            raise PlanningSourceError("pinned source content digest is stale")
        anchor_by_id = {anchor.id: anchor for anchor in self.anchors}
        if len(anchor_by_id) != len(self.anchors):
            raise PlanningSourceError("source anchor IDs are not unique")
        for block in self.blocks:
            if not block.text or len(block.text) > MAX_SOURCE_BLOCK_CHARS:
                raise PlanningSourceError("source block is empty or exceeds its bound")
            if self.document.text[block.start_offset : block.end_offset] != block.text:
                raise PlanningSourceError("source block text does not match its range")
            if any(ref not in anchor_by_id for ref in block.anchor_ids):
                raise PlanningSourceError("source block references an unknown anchor")
        for anchor in self.anchors:
            if (
                anchor.source_blob_sha != self.document.blob_sha
                or anchor.block_id not in {block.id for block in self.blocks}
                or anchor.start_offset < 0
                or anchor.end_offset <= anchor.start_offset
                or anchor.end_offset > len(self.document.text)
            ):
                raise PlanningSourceError("source anchor metadata is invalid")
            quote = self.document.text[anchor.start_offset : anchor.end_offset]
            if hashlib.sha256(quote.encode("utf-8")).hexdigest() != anchor.quote_sha256:
                raise PlanningSourceError("source anchor quote digest is stale")


@dataclass(frozen=True, slots=True)
class Requirement:
    id: str
    statement: str
    kind: str
    source_refs: tuple[str, ...]
    source_group_ids: tuple[str, ...]
    related_requirement_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RequirementDraft:
    statement: str
    kind: str
    source_refs: tuple[str, ...]
    related_source_refs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RequirementExtractionPage:
    records: tuple[dict[str, Any], ...]
    has_more: bool
    next_anchor: str | None


@dataclass(frozen=True, slots=True)
class BlockDisposition:
    block_id: str
    disposition: Literal["normative", "context", "duplicate"]
    requirement_ids: tuple[str, ...] = ()
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class FidelityDecision:
    decision: Literal["accept", "revise"]
    reason: str
    statement: str | None = None


@dataclass(frozen=True, slots=True)
class EquivalenceDecision:
    equivalent: bool
    reason: str


def read_pinned_source(
    repository_root: str,
    commit_sha: str,
    path: str,
) -> PinnedSourceDocument:
    """Read a tracked regular file from an immutable Git commit."""
    if not _SHA_RE.fullmatch(commit_sha):
        raise PlanningSourceError("commit_sha must be a full Git object ID")
    normalized = PurePosixPath(path)
    if (
        normalized.is_absolute()
        or not path
        or any(part in {"", ".", ".."} for part in path.split("/"))
    ):
        raise PlanningSourceError("source path must be a normalized repository path")
    path_text = normalized.as_posix()
    try:
        resolved_commit = subprocess.run(
            [
                "git",
                "-C",
                repository_root,
                "rev-parse",
                "--verify",
                f"{commit_sha}^{{commit}}",
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        if resolved_commit != commit_sha:
            raise PlanningSourceError("commit_sha must identify the full commit object")
        listing = subprocess.run(
            [
                "git",
                "-C",
                repository_root,
                "ls-tree",
                "-z",
                commit_sha,
                "--",
                f":(literal){path_text}",
            ],
            check=True,
            capture_output=True,
        ).stdout
    except subprocess.CalledProcessError as exc:
        raise PlanningSourceError("could not inspect pinned Git tree") from exc
    entries = [entry for entry in listing.split(b"\0") if entry]
    if len(entries) != 1:
        raise PlanningSourceError(
            "source path is not a tracked file at the pinned commit"
        )
    metadata, separator, entry_path = entries[0].partition(b"\t")
    fields = metadata.split()
    if not separator or entry_path.decode("utf-8", errors="strict") != path_text:
        raise PlanningSourceError("Git tree did not resolve the exact source path")
    if (
        len(fields) != 3
        or fields[0] not in {b"100644", b"100755"}
        or fields[1] != b"blob"
    ):
        raise PlanningSourceError("pinned source must be a regular tracked blob")
    blob_sha = fields[2].decode("ascii")
    try:
        content = subprocess.run(
            ["git", "-C", repository_root, "cat-file", "blob", blob_sha],
            check=True,
            capture_output=True,
        ).stdout
        text = content.decode("utf-8")
    except (subprocess.CalledProcessError, UnicodeDecodeError) as exc:
        raise PlanningSourceError("pinned source is not readable UTF-8") from exc
    return PinnedSourceDocument(
        repository=repository_root,
        commit_sha=commit_sha,
        path=path_text,
        blob_sha=blob_sha,
        content_sha256=hashlib.sha256(content).hexdigest(),
        text=text,
    )


def segment_markdown_source(
    document: PinnedSourceDocument,
    *,
    max_block_chars: int = MAX_SOURCE_BLOCK_CHARS,
) -> SegmentedSource:
    """Group instruction-ledger clauses into anchored Markdown blocks."""
    if not 1 <= max_block_chars <= MAX_SOURCE_BLOCK_CHARS:
        raise ValueError(
            f"max_block_chars must be between 1 and {MAX_SOURCE_BLOCK_CHARS}"
        )
    text = document.text
    if not text.strip():
        raise PlanningSourceError("pinned source document must not be empty")
    ledger = compile_instruction_ledger(document.path, text)
    lines = text.splitlines(keepends=True)
    ranges: list[tuple[str, int, int, tuple[str, ...]]] = []
    headings: list[tuple[int, str]] = []
    offset = 0
    line_index = 0
    fence_char: str | None = None
    fence_start = 0
    fence_heading: tuple[str, ...] = ()
    current_heading: tuple[str, ...] = ()
    while line_index < len(lines):
        line = lines[line_index]
        stripped = line.rstrip("\r\n")
        line_start = offset
        line_end = offset + len(line)
        fence = re.match(r"^\s{0,3}(```+|~~~+)", stripped)
        if fence_char is not None:
            if fence and fence.group(1)[0] == fence_char:
                ranges.append(("fence", fence_start, line_end, fence_heading))
                fence_char = None
            offset = line_end
            line_index += 1
            continue
        if fence:
            fence_char = fence.group(1)[0]
            fence_start = line_start
            fence_heading = current_heading
            offset = line_end
            line_index += 1
            continue
        heading = re.match(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$", stripped)
        if heading:
            level = len(heading.group(1))
            title = heading.group(2).strip()
            ranges.append(("heading", line_start, line_end, current_heading))
            headings = [(depth, value) for depth, value in headings if depth < level]
            headings.append((level, title))
            current_heading = tuple(value for _, value in headings)
            offset = line_end
            line_index += 1
            continue
        if line_index + 1 < len(lines):
            underline = lines[line_index + 1].rstrip("\r\n")
            setext = re.fullmatch(r"\s{0,3}(=+|-+)\s*", underline)
            if setext and stripped.strip():
                level = 1 if setext.group(1).startswith("=") else 2
                ranges.append(
                    (
                        "heading",
                        line_start,
                        line_end + len(lines[line_index + 1]),
                        current_heading,
                    )
                )
                headings = [
                    (depth, value) for depth, value in headings if depth < level
                ]
                headings.append((level, stripped.strip()))
                current_heading = tuple(value for _, value in headings)
                offset = line_end + len(lines[line_index + 1])
                line_index += 2
                continue
        if not stripped.strip():
            offset = line_end
            line_index += 1
            continue
        kind = (
            "table"
            if "|" in stripped
            and line_index + 1 < len(lines)
            and _is_table_delimiter(lines[line_index + 1])
            else None
        )
        if kind == "table":
            start = line_start
            heading_context = current_heading
            offset = line_end
            line_index += 1
            while (
                line_index < len(lines)
                and lines[line_index].strip()
                and "|" in lines[line_index]
            ):
                offset += len(lines[line_index])
                line_index += 1
            ranges.append((kind, start, offset, heading_context))
            continue
        if _is_list_item(stripped):
            start = line_start
            heading_context = current_heading
            offset = line_end
            line_index += 1
            while line_index < len(lines):
                candidate = lines[line_index]
                candidate_stripped = candidate.rstrip("\r\n")
                if not candidate_stripped.strip():
                    break
                if (
                    _is_list_item(candidate_stripped)
                    or candidate_stripped[:1].isspace()
                ):
                    offset += len(candidate)
                    line_index += 1
                    continue
                break
            ranges.append(("list", start, offset, heading_context))
            continue
        start = line_start
        heading_context = current_heading
        offset = line_end
        line_index += 1
        while line_index < len(lines):
            candidate = lines[line_index]
            candidate_stripped = candidate.rstrip("\r\n")
            if not candidate_stripped.strip() or _is_structural_line(
                candidate_stripped
            ):
                break
            offset += len(candidate)
            line_index += 1
        ranges.append(("paragraph", start, offset, heading_context))
    if fence_char is not None:
        ranges.append(("fence", fence_start, len(text), fence_heading))

    clause_by_range = [
        (clause.clause_id, *clause.source_span) for clause in ledger.clauses
    ]
    block_specs: list[
        tuple[
            str,
            int,
            int,
            tuple[str, ...],
            tuple[str, ...],
            str | None,
            int | None,
            int | None,
        ]
    ] = []
    for range_index, (kind, start, end, heading_context) in enumerate(ranges, start=1):
        if start >= end:
            continue
        clause_ids = tuple(
            clause_id
            for clause_id, left, right in clause_by_range
            if left < end and right > start
        )
        chunks = _split_range(
            text,
            start,
            end,
            max_block_chars,
            clause_by_range if kind != "fence" else (),
        )
        continuation_id = None
        if len(chunks) > 1:
            continuation_id = f"continuation-{range_index:04d}"
        for chunk_number, (chunk_start, chunk_end) in enumerate(chunks, start=1):
            overlapping = tuple(
                clause_id
                for clause_id, left, right in clause_by_range
                if left < chunk_end and right > chunk_start
            )
            block_specs.append(
                (
                    kind,
                    chunk_start,
                    chunk_end,
                    heading_context,
                    overlapping or clause_ids,
                    continuation_id,
                    chunk_number if continuation_id else None,
                    len(chunks) if continuation_id else None,
                )
            )

    blocks: list[SourceBlock] = []
    anchors: list[SourceAnchor] = []
    for index, (
        kind,
        start,
        end,
        heading_context,
        clause_ids,
        continuation_id,
        part,
        count,
    ) in enumerate(block_specs, start=1):
        block_id = f"block-{index:04d}"
        quote = text[start:end]
        start_line = _line_number(text, start)
        end_line = _line_number(text, max(start, end - 1))
        anchor_ids: list[str] = []
        anchor_id = f"anchor-{len(anchors) + 1:04d}"
        anchors.append(
            SourceAnchor(
                anchor_id,
                document.blob_sha,
                block_id,
                None,
                start_line,
                end_line,
                start,
                end,
                hashlib.sha256(quote.encode("utf-8")).hexdigest(),
            )
        )
        anchor_ids.append(anchor_id)
        for clause_id, clause_start, clause_end in clause_by_range:
            if (
                clause_id not in clause_ids
                or clause_start >= end
                or clause_end <= start
            ):
                continue
            clause_start = max(start, clause_start)
            clause_end = min(end, clause_end)
            clause_quote = text[clause_start:clause_end]
            clause_anchor_id = f"anchor-{len(anchors) + 1:04d}"
            anchors.append(
                SourceAnchor(
                    clause_anchor_id,
                    document.blob_sha,
                    block_id,
                    clause_id,
                    _line_number(text, clause_start),
                    _line_number(text, max(clause_start, clause_end - 1)),
                    clause_start,
                    clause_end,
                    hashlib.sha256(clause_quote.encode("utf-8")).hexdigest(),
                )
            )
            anchor_ids.append(clause_anchor_id)
        blocks.append(
            SourceBlock(
                block_id,
                document.blob_sha,
                kind,
                quote,
                start_line,
                end_line,
                start,
                end,
                heading_context,
                clause_ids,
                tuple(anchor_ids),
                None,
                f"block-{index - 1:04d}" if index > 1 else None,
                None,
                continuation_id,
                part,
                count,
                document.path,
            )
        )
    blocks = [
        replace(
            block,
            next_block_id=blocks[index + 1].id if index + 1 < len(blocks) else None,
        )
        for index, block in enumerate(blocks)
    ]
    result = SegmentedSource(document, ledger, tuple(blocks), tuple(anchors))
    result.validate()
    return result


def parse_requirement_page(
    value: Mapping[str, Any],
    *,
    allowed_source_refs: set[str],
    pagination_anchor_ids: Sequence[str],
    after_anchor: str | None = None,
) -> RequirementExtractionPage:
    """Validate one extraction page and constrain references to supplied source."""
    _exact_fields(value, {"records", "has_more", "next_anchor"}, "/")
    records = value["records"]
    if (
        not isinstance(records, Sequence)
        or isinstance(records, (str, bytes))
        or len(records) > 8
    ):
        raise PlanningContractError(
            "/records", "must be an array of at most eight records"
        )
    parsed: list[dict[str, Any]] = []
    for index, record in enumerate(records):
        pointer = f"/records/{index}"
        if not isinstance(record, Mapping):
            raise PlanningContractError(pointer, "must be an object")
        _exact_fields(
            record, {"statement", "kind", "source_refs", "related_source_refs"}, pointer
        )
        statement = _text(record["statement"], f"{pointer}/statement")
        kind = _text(record["kind"], f"{pointer}/kind")
        if kind not in _REQUIREMENT_KINDS:
            raise PlanningContractError(f"{pointer}/kind", "unknown requirement kind")
        refs = _string_array(record["source_refs"], f"{pointer}/source_refs")
        related = _string_array(
            record["related_source_refs"], f"{pointer}/related_source_refs"
        )
        if not refs:
            raise PlanningContractError(
                f"{pointer}/source_refs", "must cite at least one anchor"
            )
        if len(set(refs)) != len(refs) or len(set(related)) != len(related):
            raise PlanningContractError(pointer, "source references must be unique")
        if not set(refs + related) <= allowed_source_refs:
            raise PlanningContractError(
                pointer, "references an anchor outside supplied context"
            )
        parsed.append(
            {
                "statement": statement,
                "kind": kind,
                "source_refs": refs,
                "related_source_refs": related,
            }
        )
    has_more = value["has_more"]
    anchor = value["next_anchor"]
    if not isinstance(has_more, bool):
        raise PlanningContractError("/has_more", "must be a boolean")
    if has_more and (not isinstance(anchor, str) or not anchor.strip()):
        raise PlanningContractError("/next_anchor", "is required when has_more is true")
    if not has_more and anchor is not None:
        raise PlanningContractError("/next_anchor", "must be null on the final page")
    if has_more:
        if anchor not in pagination_anchor_ids:
            raise PlanningContractError(
                "/next_anchor", "must reference an anchor in the current source block"
            )
        cursor = -1
        if after_anchor is not None:
            try:
                cursor = pagination_anchor_ids.index(after_anchor)
            except ValueError as exc:
                raise PlanningContractError(
                    "/next_anchor", "the supplied page cursor is not in this block"
                ) from exc
        if pagination_anchor_ids.index(anchor) <= cursor:
            raise PlanningContractError(
                "/next_anchor", "must advance beyond the page cursor"
            )
    return RequirementExtractionPage(tuple(parsed), has_more, anchor)


def parse_block_disposition(
    value: Mapping[str, Any], *, block_id: str, known_requirement_ids: set[str]
) -> BlockDisposition:
    disposition = value.get("disposition")
    if disposition == "normative":
        _exact_fields(value, {"disposition", "requirement_ids"}, "/")
        refs = _known_ids(
            value["requirement_ids"], "/requirement_ids", known_requirement_ids
        )
        if not refs:
            raise PlanningContractError(
                "/requirement_ids", "normative blocks need a requirement"
            )
        return BlockDisposition(block_id, "normative", refs)
    if disposition == "duplicate":
        _exact_fields(value, {"disposition", "requirement_ids", "reason"}, "/")
        refs = _known_ids(
            value["requirement_ids"], "/requirement_ids", known_requirement_ids
        )
        if not refs:
            raise PlanningContractError(
                "/requirement_ids", "duplicate blocks need existing requirements"
            )
        return BlockDisposition(
            block_id, "duplicate", refs, _text(value["reason"], "/reason")
        )
    if disposition == "context":
        _exact_fields(value, {"disposition", "reason"}, "/")
        return BlockDisposition(
            block_id, "context", reason=_text(value["reason"], "/reason")
        )
    raise PlanningContractError(
        "/disposition", "must be normative, context, or duplicate"
    )


def parse_fidelity_decision(value: Mapping[str, Any]) -> FidelityDecision:
    if value.get("decision") == "accept":
        _exact_fields(value, {"decision", "reason"}, "/")
        return FidelityDecision("accept", _text(value["reason"], "/reason"))
    if value.get("decision") == "revise":
        _exact_fields(value, {"decision", "statement", "reason"}, "/")
        return FidelityDecision(
            "revise",
            _text(value["reason"], "/reason"),
            _text(value["statement"], "/statement"),
        )
    raise PlanningContractError("/decision", "must be accept or revise")


def parse_equivalence_decision(value: Mapping[str, Any]) -> EquivalenceDecision:
    _exact_fields(value, {"equivalent", "reason"}, "/")
    if not isinstance(value["equivalent"], bool):
        raise PlanningContractError("/equivalent", "must be a boolean")
    return EquivalenceDecision(value["equivalent"], _text(value["reason"], "/reason"))


def build_extract_requirements_request(
    block: SourceBlock,
    *,
    anchors: Mapping[str, SourceAnchor],
    related_blocks: Sequence[SourceBlock] = (),
    after_anchor: str | None = None,
    accepted_records: Sequence[Mapping[str, Any]] = (),
) -> QuestionRequest[RequirementExtractionPage]:
    """Build one bounded obligation enumeration request for a source block."""
    anchor_id = block.anchor_ids[0]
    valid = {
        "records": [
            {
                "statement": "The block's independently checkable obligation.",
                "kind": "behavior",
                "source_refs": [anchor_id],
                "related_source_refs": [],
            }
        ],
        "has_more": False,
        "next_anchor": None,
    }
    empty = {"records": [], "has_more": False, "next_anchor": None}
    supplied_blocks = (block, *related_blocks)
    if after_anchor is not None and after_anchor not in block.anchor_ids:
        raise ValueError("after_anchor must identify an anchor in the current block")
    supplied_anchor_ids = tuple(
        dict.fromkeys(ref for item in supplied_blocks for ref in item.anchor_ids)
    )
    if any(ref not in anchors for ref in supplied_anchor_ids):
        raise PlanningSourceError("source block references a missing source anchor")
    excerpts = tuple(
        SourceExcerpt(item.id, item.text, item.start_offset) for item in supplied_blocks
    )
    anchor_facts = tuple(
        ContextRecord(
            ref,
            {
                "block_id": anchors[ref].block_id,
                "clause_id": anchors[ref].clause_id,
                "start_line": anchors[ref].start_line,
                "end_line": anchors[ref].end_line,
                "start_offset": anchors[ref].start_offset,
                "end_offset": anchors[ref].end_offset,
                "quote_sha256": anchors[ref].quote_sha256,
            },
        )
        for ref in supplied_anchor_ids
    )
    accepted = tuple(accepted_records)
    if accepted:
        anchor_facts += (
            ContextRecord(
                f"accepted-requirements-{block.id}",
                {"records": [dict(item) for item in accepted]},
            ),
        )
    allowed_refs = set(supplied_anchor_ids)
    input_digest = _digest_json(
        {
            "block": source_block_input_digest(block),
            "related_blocks": [
                source_block_input_digest(item) for item in related_blocks
            ],
            "anchor_registry": [
                {"id": item.ref_id, "value": dict(item.value)}
                for item in anchor_facts
                if item.ref_id != f"accepted-requirements-{block.id}"
            ],
            "after_anchor": after_anchor,
            "accepted_records": [dict(item) for item in accepted],
        }
    )
    return _source_question_request(
        question_kind="extract_requirements",
        subject_id=block.id,
        input_digest=input_digest,
        schema_name="extract-requirements-answer-v1",
        prompt=(
            "Which independently checkable obligations does this block impose? "
            "Cite direct anchors and related anchors from supplied adjacent blocks "
            "when needed. The anchor registry gives IDs and exact source ranges. "
            "Do not repeat accepted records. Return at most eight; if more remain, "
            "set next_anchor to a later anchor in the current block."
        ),
        field_definitions={
            "records": "Obligations with statement, kind, and source references.",
            "kind": "One of behavior, constraint, acceptance, or human_setup.",
            "source_refs": "One or more anchors directly imposing this obligation.",
            "related_source_refs": "Anchors for cross-references or dependencies.",
            "has_more": "Whether another page of obligations remains in this block.",
            "next_anchor": "A later anchor, or null on the final page.",
        },
        excerpts=excerpts,
        accepted_facts=anchor_facts,
        valid_example=valid,
        empty_example=empty,
        schema=_ENUMERATION_SCHEMA,
        parse=lambda answer: parse_requirement_page(
            answer,
            allowed_source_refs=allowed_refs,
            pagination_anchor_ids=block.anchor_ids,
            after_anchor=after_anchor,
        ),
    )


def build_block_disposition_request(
    block: SourceBlock, *, requirement_ids: Sequence[str]
) -> QuestionRequest[BlockDisposition]:
    """Build the per-block accounting question; no global completeness claim."""
    known_ids = set(requirement_ids)
    if not known_ids:
        raise ValueError("block disposition needs at least one known requirement ID")
    input_digest = _digest_json(
        {
            "block": source_block_input_digest(block),
            "requirement_ids": sorted(known_ids),
        }
    )
    return _source_question_request(
        question_kind="classify_source_block",
        subject_id=block.id,
        input_digest=input_digest,
        schema_name="source-block-disposition-v1",
        prompt=(
            "Are all normative obligations represented by the supplied requirement "
            "IDs, or is this block context or a duplicate? Choose one disposition."
        ),
        field_definitions={
            "disposition": "normative, context, or duplicate.",
            "requirement_ids": "Existing IDs covering a normative or duplicate block.",
            "reason": "Why it is context or duplicates listed requirements.",
        },
        excerpt=SourceExcerpt(block.id, block.text, block.start_offset),
        valid_example={
            "disposition": "normative",
            "requirement_ids": [sorted(known_ids)[0]],
        },
        empty_example={
            "disposition": "context",
            "reason": "This block supplies background only.",
        },
        schema=_DISPOSITION_SCHEMA,
        parse=lambda answer: parse_block_disposition(
            answer, block_id=block.id, known_requirement_ids=known_ids
        ),
    )


def build_fidelity_request(
    requirement: Requirement, *, segmented: SegmentedSource
) -> QuestionRequest[FidelityDecision]:
    """Build one source-faithfulness check for a single extracted obligation."""
    anchors = {item.id: item for item in segmented.anchors}
    excerpts = tuple(
        SourceExcerpt(
            ref,
            segmented.document.text[
                anchors[ref].start_offset : anchors[ref].end_offset
            ],
            anchors[ref].start_offset,
        )
        for ref in requirement.source_refs
        if ref in anchors
    )
    if len(excerpts) != len(requirement.source_refs):
        raise PlanningSourceError("requirement references an unknown source anchor")
    fact = ContextRecord(
        requirement.id,
        {"statement": requirement.statement, "kind": requirement.kind},
    )
    input_digest = _digest_json(
        {
            "requirement": requirement.id,
            "statement": requirement.statement,
            "anchors": [
                source_block_input_digest(_block_for_anchor(segmented, ref))
                for ref in requirement.source_refs
            ],
        }
    )
    accept = {
        "decision": "accept",
        "reason": "The statement preserves the cited obligation.",
    }
    revise = {
        "decision": "revise",
        "statement": requirement.statement,
        "reason": "The statement needs a narrower wording to preserve the source.",
    }
    return _source_question_request(
        question_kind="check_requirement_fidelity",
        subject_id=requirement.id,
        input_digest=input_digest,
        schema_name="requirement-fidelity-v1",
        prompt=(
            "Does this statement preserve the cited obligation without adding or "
            "removing behavior? Accept or provide one corrected statement. "
            "The obligation cannot be deleted."
        ),
        field_definitions={
            "decision": "accept or revise.",
            "statement": "For revise, the corrected obligation statement.",
            "reason": "Why the statement is faithful or what needed correction.",
        },
        excerpts=excerpts,
        accepted_facts=(fact,),
        valid_example=accept,
        empty_example=revise,
        schema=_FIDELITY_SCHEMA,
        parse=parse_fidelity_decision,
    )


def build_equivalence_request(
    first: Requirement,
    second: Requirement,
    *,
    segmented: SegmentedSource,
) -> QuestionRequest[EquivalenceDecision]:
    """Build a pairwise duplicate check; only an explicit yes permits merging."""
    if first.id == second.id:
        raise ValueError("equivalence comparison needs two distinct requirements")
    anchors = {item.id: item for item in segmented.anchors}
    refs = tuple(dict.fromkeys(first.source_refs + second.source_refs))
    excerpts = tuple(
        SourceExcerpt(
            ref,
            segmented.document.text[
                anchors[ref].start_offset : anchors[ref].end_offset
            ],
            anchors[ref].start_offset,
        )
        for ref in refs
        if ref in anchors
    )
    if len(excerpts) != len(refs):
        raise PlanningSourceError("requirement references an unknown source anchor")
    facts = (
        ContextRecord(first.id, {"statement": first.statement, "kind": first.kind}),
        ContextRecord(second.id, {"statement": second.statement, "kind": second.kind}),
    )
    digest = _digest_json(
        {
            "requirements": [first, second],
            "anchors": refs,
            "source_digests": [
                source_block_input_digest(_block_for_anchor(segmented, ref))
                for ref in refs
            ],
        }
    )
    return _source_question_request(
        question_kind="compare_requirement_equivalence",
        subject_id=f"{first.id}-{second.id}",
        input_digest=digest,
        schema_name="requirement-equivalence-v1",
        prompt=(
            "Do these requirements impose the same complete obligation? "
            "Partial overlap is not equivalence."
        ),
        field_definitions={
            "equivalent": "True only when both preserve the same full obligation.",
            "reason": "Explain the equivalence or material difference.",
        },
        excerpts=excerpts,
        accepted_facts=facts,
        valid_example={
            "equivalent": True,
            "reason": "Both impose the same obligation.",
        },
        empty_example={"equivalent": False, "reason": "They overlap only partially."},
        schema=_EQUIVALENCE_SCHEMA,
        parse=parse_equivalence_decision,
    )


def allocate_requirement(
    record: Mapping[str, Any],
    *,
    ordinal: int,
    anchors: Mapping[str, SourceAnchor],
    ledger: InstructionLedger,
) -> Requirement:
    if ordinal < 1:
        raise PlanningSourceError("requirement ordinal must be positive")
    _exact_fields(
        record,
        {"statement", "kind", "source_refs", "related_source_refs"},
        "/",
    )
    statement = _text(record["statement"], "/statement")
    kind = _text(record["kind"], "/kind")
    if kind not in _REQUIREMENT_KINDS:
        raise PlanningContractError("/kind", "unknown requirement kind")
    refs = _string_array(record["source_refs"], "/source_refs")
    if not refs:
        raise PlanningContractError("/source_refs", "must cite at least one anchor")
    if len(refs) != len(set(refs)):
        raise PlanningContractError(
            "/source_refs", "must not contain duplicate anchors"
        )
    if any(ref not in anchors for ref in refs):
        raise PlanningSourceError("requirement references an unknown source anchor")
    clause_ids = {
        clause_id for ref in refs if (clause_id := anchors[ref].clause_id) is not None
    }
    clauses = {clause.clause_id: clause for clause in ledger.clauses}
    groups_set: set[str] = set()
    for clause_id in clause_ids:
        clause = clauses.get(clause_id)
        if clause is not None and clause.validation_group_id is not None:
            groups_set.add(clause.validation_group_id)
    groups = tuple(sorted(groups_set))
    return Requirement(
        id=f"req-{ordinal:04d}",
        statement=statement,
        kind=kind,
        source_refs=refs,
        source_group_ids=groups,
        related_requirement_ids=tuple(record.get("related_requirement_ids", ())),
    )


def allocate_requirements(
    records: Sequence[Mapping[str, Any]],
    *,
    first_ordinal: int,
    anchors: Mapping[str, SourceAnchor],
    ledger: InstructionLedger,
) -> tuple[tuple[Requirement, ...], tuple[RequirementDraft, ...]]:
    """Assign stable IDs and retain cross-block source links for later resolution."""
    requirements: list[Requirement] = []
    drafts: list[RequirementDraft] = []
    for index, record in enumerate(records):
        source_refs = tuple(record["source_refs"])
        related_refs = tuple(record["related_source_refs"])
        if any(ref not in anchors for ref in source_refs + related_refs):
            raise PlanningSourceError("requirement references an unknown source anchor")
        allocated = allocate_requirement(
            record, ordinal=first_ordinal + index, anchors=anchors, ledger=ledger
        )
        requirements.append(allocated)
        drafts.append(
            RequirementDraft(
                allocated.statement, allocated.kind, allocated.source_refs, related_refs
            )
        )
    return tuple(requirements), tuple(drafts)


def merge_equivalent_requirements(
    retained: Requirement,
    duplicate: Requirement,
    decision: EquivalenceDecision,
) -> Requirement:
    """Merge source anchors only after an explicit positive equivalence answer."""
    if not decision.equivalent:
        raise PlanningSourceError("non-equivalent requirements cannot be merged")
    return replace(
        retained,
        source_refs=tuple(dict.fromkeys(retained.source_refs + duplicate.source_refs)),
        source_group_ids=tuple(
            dict.fromkeys(retained.source_group_ids + duplicate.source_group_ids)
        ),
        related_requirement_ids=tuple(
            dict.fromkeys(
                retained.related_requirement_ids + duplicate.related_requirement_ids
            )
        ),
    )


def source_block_input_digest(block: SourceBlock) -> str:
    """Digest the exact source and anchors bound to one block question."""
    material = "\0".join(
        [
            block.source_path,
            block.kind,
            str(block.start_offset),
            str(block.end_offset),
            block.text,
            *block.heading_context,
        ]
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(material).hexdigest()}"


def _source_question_request[AnswerT](
    *,
    question_kind: str,
    subject_id: str,
    input_digest: str,
    schema_name: str,
    prompt: str,
    field_definitions: Mapping[str, str],
    valid_example: Mapping[str, Any],
    empty_example: Mapping[str, Any],
    schema: Mapping[str, Any],
    parse: Any,
    excerpt: SourceExcerpt | None = None,
    excerpts: Sequence[SourceExcerpt] = (),
    accepted_facts: Sequence[ContextRecord] = (),
) -> QuestionRequest[AnswerT]:
    source_excerpts = (excerpt,) if excerpt is not None else tuple(excerpts)
    facts = tuple(accepted_facts)
    refs = tuple(item.ref_id for item in source_excerpts) + tuple(
        item.ref_id for item in facts
    )
    question_id = (
        f"q-{question_kind.replace('_', '-')}"
        f"-{input_digest.removeprefix('sha256:')[:16]}"
    )
    request = QuestionRequest(
        question=Question(
            question_id=question_id,
            kind=question_kind,
            contract_version=1,
            subject_ids=(subject_id,),
            input_digest=input_digest,
            context_ref_ids=refs,
            expected_answer_schema=schema_name,
            attempt=1,
        ),
        exact_question=prompt,
        answer_field_definitions=field_definitions,
        source_excerpts=source_excerpts,
        repository_evidence=(),
        accepted_facts=facts,
        valid_example=valid_example,
        empty_example=empty_example,
        answer_schema=schema,
        parse_answer=parse,
    )
    request.validate()
    return request


def _digest_json(value: Mapping[str, Any]) -> str:
    material = json.dumps(
        value, sort_keys=True, separators=(",", ":"), default=_json_default
    )
    return f"sha256:{hashlib.sha256(material.encode('utf-8')).hexdigest()}"


def _json_default(value: Any) -> Any:
    if hasattr(value, "__dataclass_fields__"):
        return {name: getattr(value, name) for name in value.__dataclass_fields__}
    if isinstance(value, tuple):
        return list(value)
    raise TypeError(f"not JSON serializable: {type(value).__name__}")


def _block_for_anchor(segmented: SegmentedSource, ref_id: str) -> SourceBlock:
    anchor = next((item for item in segmented.anchors if item.id == ref_id), None)
    if anchor is None:
        raise PlanningSourceError("requirement references an unknown source anchor")
    block = next(
        (item for item in segmented.blocks if item.id == anchor.block_id), None
    )
    if block is None:
        raise PlanningSourceError("source anchor points to a missing block")
    return block


def validate_source_coverage(
    blocks: Sequence[SourceBlock],
    dispositions: Sequence[BlockDisposition],
    requirements: Sequence[Requirement],
) -> None:
    by_id = {item.block_id: item for item in dispositions}
    if len(by_id) != len(dispositions):
        raise PlanningSourceError("source block has multiple dispositions")
    missing = [block.id for block in blocks if block.id not in by_id]
    unknown = set(by_id) - {block.id for block in blocks}
    known_requirements = {item.id for item in requirements}
    dangling = {
        requirement_id
        for item in dispositions
        for requirement_id in item.requirement_ids
        if requirement_id not in known_requirements
    }
    if missing or unknown or dangling:
        raise PlanningSourceError(
            "source coverage is unresolved "
            f"(missing={missing}, unknown={sorted(unknown)}, "
            f"dangling_requirements={sorted(dangling)})"
        )


def _split_range(
    text: str, start: int, end: int, limit: int, clauses: Sequence[tuple[str, int, int]]
) -> list[tuple[int, int]]:
    result: list[tuple[int, int]] = []
    cursor = start
    while True:
        remaining_clauses = [
            (left, right)
            for _, left, right in clauses
            if left >= cursor and right <= end
        ]
        exceeds_char_limit = end - cursor > limit
        exceeds_clause_limit = len(remaining_clauses) > MAX_CLAUSES_PER_SOURCE_BLOCK
        if not exceeds_char_limit and not exceeds_clause_limit:
            break
        target = min(cursor + limit, end)
        candidates = [
            right
            for _, left, right in clauses
            if cursor < right <= target and left >= cursor
        ]
        boundary = max(candidates, default=target)
        if exceeds_clause_limit:
            clause_boundary = remaining_clauses[MAX_CLAUSES_PER_SOURCE_BLOCK - 1][1]
            boundary = min(boundary, clause_boundary)
        if boundary < target // 2 + cursor:
            newline = text.rfind("\n", cursor + 1, target + 1)
            if newline >= cursor + limit // 2:
                boundary = newline + 1
        if boundary <= cursor:
            boundary = target
        result.append((cursor, boundary))
        cursor = boundary
    if cursor < end:
        result.append((cursor, end))
    return result


def _line_number(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _is_list_item(line: str) -> bool:
    return bool(re.match(r"^\s{0,3}(?:[-*+]\s+|\d+[.)]\s+)", line))


def _is_table_delimiter(line: str) -> bool:
    return "|" in line and bool(
        re.fullmatch(r"\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*", line)
    )


def _is_structural_line(line: str) -> bool:
    return bool(re.match(r"^\s{0,3}(?:#{1,6}\s+|```|~~~|[-*+]\s+|\d+[.)]\s+)", line))


def _exact_fields(value: Mapping[str, Any], expected: set[str], pointer: str) -> None:
    missing = expected - value.keys()
    unknown = value.keys() - expected
    if missing:
        raise PlanningContractError(
            f"{pointer}/{sorted(missing)[0]}", "required field is missing"
        )
    if unknown:
        raise PlanningContractError(f"{pointer}/{sorted(unknown)[0]}", "unknown field")


def _text(value: Any, pointer: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PlanningContractError(pointer, "must be a non-empty string")
    return value.strip()


def _string_array(value: Any, pointer: str) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise PlanningContractError(pointer, "must be an array of strings")
    return tuple(_text(item, f"{pointer}/{index}") for index, item in enumerate(value))


def _known_ids(value: Any, pointer: str, known: set[str]) -> tuple[str, ...]:
    items = _string_array(value, pointer)
    if len(items) != len(set(items)):
        raise PlanningContractError(pointer, "must not contain duplicate IDs")
    unknown = set(items) - known
    if unknown:
        raise PlanningContractError(
            pointer, f"unknown requirement ID: {sorted(unknown)[0]}"
        )
    return items


__all__ = [
    "BlockDisposition",
    "EquivalenceDecision",
    "FidelityDecision",
    "MAX_SOURCE_BLOCK_CHARS",
    "MAX_CLAUSES_PER_SOURCE_BLOCK",
    "PinnedSourceDocument",
    "PlanningSourceError",
    "Requirement",
    "RequirementDraft",
    "RequirementExtractionPage",
    "SegmentedSource",
    "SourceAnchor",
    "SourceBlock",
    "allocate_requirement",
    "parse_block_disposition",
    "build_block_disposition_request",
    "build_equivalence_request",
    "build_extract_requirements_request",
    "build_fidelity_request",
    "merge_equivalent_requirements",
    "allocate_requirements",
    "parse_equivalence_decision",
    "parse_fidelity_decision",
    "parse_requirement_page",
    "read_pinned_source",
    "segment_markdown_source",
    "source_block_input_digest",
    "validate_source_coverage",
]
