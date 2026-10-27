"""Pinned repository evidence and requirement-level coverage binding."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Protocol

from powdrr_lift.basedpyright_tools import find_workspace_symbols
from powdrr_lift.errors import PowdrrExecutionError
from powdrr_lift.workrr.planning.contracts import (
    COVERAGE_ANSWER_JSON_SCHEMA,
    CompleteCoverage,
    CoverageAnswer,
    MissingCoverage,
    PartialCoverage,
    PlanningContractError,
    Question,
    parse_coverage_answer,
)
from powdrr_lift.workrr.planning.questions import (
    ContextRecord,
    QuestionRequest,
)
from powdrr_lift.workrr.planning.source import (
    PlanningSourceError,
    Requirement,
    SegmentedSource,
    read_pinned_source,
)

MAX_SYMBOL_QUERIES = 8
MAX_SYMBOL_QUERY_CHARS = 120
MAX_SYMBOL_EVIDENCE_CHARS = 8_000
_EVIDENCE_KINDS = frozenset(
    {"implementation", "test", "configuration", "execution_receipt"}
)


class EvidenceError(ValueError):
    """Invalid pinned evidence, symbol lookup, or coverage binding."""


class SymbolFinder(Protocol):
    def __call__(
        self,
        queries: list[str],
        *,
        worktree_root: Path,
        limit: int = 50,
    ) -> dict[str, list[dict[str, Any]]]: ...


@dataclass(frozen=True, slots=True)
class Evidence:
    id: str
    base_commit: str
    path: str
    start_line: int
    end_line: int
    content_sha256: str
    kind: Literal["implementation", "test", "configuration", "execution_receipt"]
    symbol: str | None = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> Evidence:
        expected = {
            "id",
            "base_commit",
            "path",
            "start_line",
            "end_line",
            "content_sha256",
            "kind",
            "symbol",
        }
        if set(value) != expected:
            raise EvidenceError("evidence record fields are invalid")
        evidence_id = value["id"]
        commit = value["base_commit"]
        path = value["path"]
        start = value["start_line"]
        end = value["end_line"]
        digest = value["content_sha256"]
        kind = value["kind"]
        symbol = value["symbol"]
        if not isinstance(evidence_id, str) or not re.fullmatch(
            r"evidence-[0-9]{4,}", evidence_id
        ):
            raise EvidenceError("evidence ID is invalid")
        if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40,64}", commit):
            raise EvidenceError("evidence base commit is invalid")
        if not isinstance(path, str):
            raise EvidenceError("evidence path is invalid")
        _validate_repository_path(path)
        if (
            isinstance(start, bool)
            or not isinstance(start, int)
            or isinstance(end, bool)
            or not isinstance(end, int)
            or start < 1
            or end < start
        ):
            raise EvidenceError("evidence line range is invalid")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise EvidenceError("evidence content digest is invalid")
        evidence_kind = _kind(kind) if isinstance(kind, str) else None
        if evidence_kind is None:
            raise EvidenceError("evidence kind is invalid")
        if symbol is not None and (not isinstance(symbol, str) or not symbol.strip()):
            raise EvidenceError("evidence symbol must be a name or null")
        if (
            evidence_kind in {"configuration", "execution_receipt"}
            and symbol is not None
        ):
            raise EvidenceError("file evidence cannot carry a symbol name")
        _validate_test_kind(path, evidence_kind)
        return cls(
            evidence_id,
            commit,
            path,
            start,
            end,
            digest,
            evidence_kind,
            symbol,
        )

    def to_data(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "base_commit": self.base_commit,
            "path": self.path,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "content_sha256": self.content_sha256,
            "kind": self.kind,
            "symbol": self.symbol,
        }


@dataclass(frozen=True, slots=True)
class SymbolLookupResult:
    evidence: tuple[Evidence, ...]
    unresolved_queries: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ResidualRequirement:
    id: str
    original_requirement_id: str
    revision: int
    statement: str
    kind: str
    source_refs: tuple[str, ...]
    source_group_ids: tuple[str, ...]
    related_requirement_ids: tuple[str, ...]

    def to_data(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "original_requirement_id": self.original_requirement_id,
            "revision": self.revision,
            "statement": self.statement,
            "kind": self.kind,
            "source_refs": list(self.source_refs),
            "source_group_ids": list(self.source_group_ids),
            "related_requirement_ids": list(self.related_requirement_ids),
        }


@dataclass(frozen=True, slots=True)
class RequirementCoverage:
    requirement_id: str
    answer: CoverageAnswer
    residual: ResidualRequirement | None = None


def read_pinned_code_file(
    repository_root: str | Path, base_commit: str, path: str
) -> tuple[str, str]:
    """Read the exact UTF-8 blob and return normalized path plus text."""
    try:
        document = read_pinned_source(str(repository_root), base_commit, path)
    except PlanningSourceError as error:
        raise EvidenceError(str(error)) from error
    return document.path, document.text


def record_file_evidence(
    repository_root: str | Path,
    base_commit: str,
    path: str,
    *,
    kind: str,
    ordinal: int,
) -> Evidence:
    """Register a whole pinned file as evidence; IDs remain code-owned."""
    normalized_path, text = read_pinned_code_file(repository_root, base_commit, path)
    evidence_kind = _kind(kind)
    if evidence_kind == "execution_receipt":
        raise EvidenceError(
            "execution receipts must be registered from an observed run"
        )
    _validate_test_kind(normalized_path, evidence_kind)
    lines = text.splitlines(keepends=True)
    if not lines:
        raise EvidenceError("empty files cannot establish code coverage")
    return Evidence(
        id=_evidence_id(ordinal),
        base_commit=base_commit,
        path=normalized_path,
        start_line=1,
        end_line=len(lines),
        content_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        kind=evidence_kind,
    )


def lookup_symbols_in_pinned_file(
    repository_root: str | Path,
    base_commit: str,
    path: str,
    queries: Sequence[str],
    *,
    kind: str,
    first_ordinal: int,
    symbol_finder: SymbolFinder | None = None,
) -> SymbolLookupResult:
    """Read a file first, then search symbol names and filter to that file.

    File paths are never sent as workspace-symbol queries. The checkout must be
    a clean realization of `base_commit` so basedpyright cannot inspect edits
    that are absent from the pinned source.
    """
    root = Path(repository_root).resolve()
    normalized_path, text = read_pinned_code_file(root, base_commit, path)
    _require_clean_pinned_checkout(root, base_commit)
    evidence_kind = _kind(kind)
    if evidence_kind in {"configuration", "execution_receipt"}:
        raise EvidenceError("symbol evidence must be implementation or test code")
    _validate_test_kind(normalized_path, evidence_kind)
    normalized_queries = _validate_symbol_queries(queries)
    finder = symbol_finder or find_workspace_symbols
    try:
        results = finder(
            normalized_queries, worktree_root=root, limit=MAX_SYMBOL_QUERIES * 4
        )
    except PowdrrExecutionError as error:
        raise EvidenceError(f"symbol lookup failed: {error}") from error
    lines = text.splitlines(keepends=True)
    output: list[Evidence] = []
    unresolved: list[str] = []
    seen: set[tuple[str, int, int]] = set()
    next_ordinal = first_ordinal
    for query in normalized_queries:
        matches = results.get(query, [])
        found_for_query = False
        for symbol in matches:
            if not isinstance(symbol, Mapping):
                continue
            symbol_path = symbol.get("path")
            name = symbol.get("name")
            location = symbol.get("range")
            if symbol_path != normalized_path or not isinstance(name, str) or not name:
                continue
            if not isinstance(location, Mapping):
                continue
            start = location.get("start_line")
            end = location.get("end_line")
            if (
                isinstance(start, bool)
                or not isinstance(start, int)
                or isinstance(end, bool)
                or not isinstance(end, int)
                or start < 1
                or end < start
                or end > len(lines)
            ):
                continue
            key = (name, start, end)
            if key in seen:
                continue
            excerpt = "".join(lines[start - 1 : end])
            if len(excerpt) > MAX_SYMBOL_EVIDENCE_CHARS:
                continue
            if not re.search(rf"(?<!\w){re.escape(name)}(?!\w)", excerpt):
                continue
            seen.add(key)
            output.append(
                Evidence(
                    id=_evidence_id(next_ordinal),
                    base_commit=base_commit,
                    path=normalized_path,
                    start_line=start,
                    end_line=end,
                    content_sha256=hashlib.sha256(excerpt.encode("utf-8")).hexdigest(),
                    kind=evidence_kind,
                    symbol=name,
                )
            )
            next_ordinal += 1
            found_for_query = True
        if not found_for_query:
            unresolved.append(query)
    return SymbolLookupResult(tuple(output), tuple(unresolved))


def bind_coverage_answer(
    value: Mapping[str, Any],
    *,
    evidence_by_id: Mapping[str, Evidence],
    supplied_evidence_ids: Sequence[str],
) -> CoverageAnswer:
    """Validate answer references and enforce evidence required by its status."""
    answer = parse_coverage_answer(value)
    supplied = set(supplied_evidence_ids)
    if len(supplied) != len(supplied_evidence_ids):
        raise PlanningContractError(
            "/evidence_ids", "supplied evidence IDs must be unique"
        )
    answer_ids = set(answer.evidence_ids)
    if not answer_ids <= supplied:
        raise PlanningContractError(
            "/evidence_ids",
            "coverage answer cites evidence outside the question context",
        )
    evidence: list[Evidence] = []
    for evidence_id in answer.evidence_ids:
        record = evidence_by_id.get(evidence_id)
        if record is None:
            raise PlanningContractError(
                "/evidence_ids", f"coverage answer cites unknown evidence {evidence_id}"
            )
        evidence.append(record)
    if isinstance(answer, PartialCoverage) and not any(
        record.kind == "implementation" for record in evidence
    ):
        raise PlanningContractError(
            "/evidence_ids", "partial coverage requires implementation evidence"
        )
    if isinstance(answer, CompleteCoverage):
        kinds = {record.kind for record in evidence}
        has_execution_receipt = "execution_receipt" in kinds
        has_code_and_test = (
            bool(kinds & {"implementation", "configuration"}) and "test" in kinds
        )
        if not has_execution_receipt and not has_code_and_test:
            raise PlanningContractError(
                "/evidence_ids",
                "complete coverage needs implementation/configuration plus test "
                "evidence, or an execution receipt",
            )
    return answer


def build_requirement_coverage_request(
    requirement: Requirement,
    *,
    source: SegmentedSource,
    evidence: Sequence[Evidence],
    repository_root: str | Path,
) -> QuestionRequest[CoverageAnswer]:
    """Build one coverage question bound to one requirement and supplied evidence."""
    known_anchor_ids = {item.id for item in source.anchors}
    if not set(requirement.source_refs) <= known_anchor_ids:
        raise EvidenceError("requirement cites unknown source anchor")
    evidence_records = tuple(evidence)
    evidence_by_id = {item.id: item for item in evidence_records}
    if len(evidence_by_id) != len(evidence_records):
        raise EvidenceError("evidence IDs must be unique")
    evidence_context: list[ContextRecord] = []
    for item in evidence_records:
        if item.base_commit != source.document.commit_sha:
            raise EvidenceError(
                "evidence and requirement source use different base commits"
            )
        excerpt = _read_evidence_excerpt(repository_root, item)
        evidence_context.append(
            ContextRecord(item.id, {**item.to_data(), "source_excerpt": excerpt})
        )
    accepted = tuple(evidence_context)
    accepted += (
        ContextRecord(
            requirement.id,
            {
                "statement": requirement.statement,
                "kind": requirement.kind,
                "source_group_ids": list(requirement.source_group_ids),
            },
        ),
    )
    digest_material = {
        "requirement": {
            "id": requirement.id,
            "statement": requirement.statement,
            "kind": requirement.kind,
            "source_refs": list(requirement.source_refs),
            "source_group_ids": list(requirement.source_group_ids),
        },
        "evidence": [item.to_data() for item in evidence_records],
    }
    input_digest = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(digest_material, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()
    )
    supplied_ids = tuple(item.id for item in evidence_records)
    receipt = next(
        (item for item in evidence_records if item.kind == "execution_receipt"), None
    )
    implementation = next(
        (item for item in evidence_records if item.kind == "implementation"), None
    )
    configuration = next(
        (item for item in evidence_records if item.kind == "configuration"), None
    )
    test = next((item for item in evidence_records if item.kind == "test"), None)
    if receipt is not None:
        positive_example: Mapping[str, Any] = {
            "status": "complete",
            "evidence_ids": [receipt.id],
            "reason": "An observed execution receipt covers this requirement.",
        }
    elif (implementation is not None or configuration is not None) and test is not None:
        code_evidence = implementation or configuration
        assert code_evidence is not None
        positive_example = {
            "status": "complete",
            "evidence_ids": [code_evidence.id, test.id],
            "reason": "Pinned implementation and relevant tests cover it.",
        }
    elif implementation is not None:
        positive_example = {
            "status": "partial",
            "evidence_ids": [implementation.id],
            "reason": "Implementation exists, but part of the obligation remains.",
            "remaining_statement": "Add the specific missing behavior or test.",
        }
    else:
        positive_example = {
            "status": "missing",
            "evidence_ids": [],
            "reason": "The inspected evidence has no matching implementation.",
        }
    negative_example = {
        "status": "missing",
        "evidence_ids": [],
        "reason": "No matching implementation was found in the inspected evidence.",
    }
    return QuestionRequest(
        question=Question(
            question_id=f"q-coverage-{input_digest[-16:]}",
            kind="assess_requirement_coverage",
            contract_version=1,
            subject_ids=(requirement.id,),
            input_digest=input_digest,
            context_ref_ids=tuple(list(supplied_ids) + [requirement.id]),
            expected_answer_schema="coverage-answer-v1",
            attempt=1,
        ),
        exact_question=(
            "This source-validated requirement is the unit of assessment. How much "
            "of it is already implemented? Use only supplied pinned evidence. "
            "Mark missing only when the bounded "
            "code investigation found no matching implementation; an empty lookup "
            "alone is not proof that it is absent."
        ),
        answer_field_definitions={
            "status": "missing, partial, or complete.",
            "evidence_ids": "Supplied evidence IDs that support this assessment.",
            "reason": "What the evidence establishes or fails to establish.",
            "remaining_statement": "For partial, the concrete unmet obligation.",
        },
        # The requirement statement has already passed source-fidelity review.
        # Repeating every cited source paragraph can exceed the bounded question
        # size without adding information needed for coverage judgment.
        source_excerpts=(),
        repository_evidence=tuple(evidence_context),
        accepted_facts=(accepted[-1],),
        valid_example=positive_example,
        empty_example=negative_example,
        answer_schema=COVERAGE_ANSWER_JSON_SCHEMA,
        parse_answer=lambda answer: bind_coverage_answer(
            answer,
            evidence_by_id=evidence_by_id,
            supplied_evidence_ids=supplied_ids,
        ),
    )


def create_requirement_coverage(
    requirement: Requirement | ResidualRequirement,
    answer: CoverageAnswer,
    *,
    evidence_by_id: Mapping[str, Evidence],
    supplied_evidence_ids: Sequence[str],
    revision: int,
) -> RequirementCoverage:
    """Keep the accepted result and compile a provenance-preserving residual."""
    raw: dict[str, Any]
    if isinstance(answer, MissingCoverage):
        raw = {"status": answer.status, "evidence_ids": [], "reason": answer.reason}
    elif isinstance(answer, CompleteCoverage):
        raw = {
            "status": answer.status,
            "evidence_ids": list(answer.evidence_ids),
            "reason": answer.reason,
        }
    else:
        raw = {
            "status": answer.status,
            "evidence_ids": list(answer.evidence_ids),
            "reason": answer.reason,
            "remaining_statement": answer.remaining_statement,
        }
    bound = bind_coverage_answer(
        raw,
        evidence_by_id=evidence_by_id,
        supplied_evidence_ids=supplied_evidence_ids,
    )
    if not isinstance(bound, PartialCoverage):
        return RequirementCoverage(requirement.id, bound)
    previous_revision = (
        requirement.revision if isinstance(requirement, ResidualRequirement) else 0
    )
    if revision <= previous_revision:
        raise EvidenceError(
            "residual requirement revision must advance its prior revision"
        )
    original_requirement_id = (
        requirement.original_requirement_id
        if isinstance(requirement, ResidualRequirement)
        else requirement.id
    )
    residual = ResidualRequirement(
        id=f"{original_requirement_id}-residual-{revision:04d}",
        original_requirement_id=original_requirement_id,
        revision=revision,
        statement=bound.remaining_statement,
        kind=requirement.kind,
        source_refs=requirement.source_refs,
        source_group_ids=requirement.source_group_ids,
        related_requirement_ids=requirement.related_requirement_ids,
    )
    return RequirementCoverage(requirement.id, bound, residual)


def _require_clean_pinned_checkout(root: Path, base_commit: str) -> None:
    try:
        head = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--verify", "HEAD^{commit}"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        status = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=all",
            ],
            check=True,
            capture_output=True,
        ).stdout
    except subprocess.CalledProcessError as error:
        raise EvidenceError("could not verify pinned analysis checkout") from error
    if head != base_commit:
        raise EvidenceError("symbol lookup checkout does not match pinned base commit")
    if status:
        raise EvidenceError("symbol lookup requires a clean pinned checkout")


def _read_evidence_excerpt(repository_root: str | Path, evidence: Evidence) -> str:
    path, content = read_pinned_code_file(
        repository_root, evidence.base_commit, evidence.path
    )
    if path != evidence.path:
        raise EvidenceError("evidence path is not normalized")
    lines = content.splitlines(keepends=True)
    if evidence.start_line < 1 or evidence.end_line < evidence.start_line:
        raise EvidenceError("evidence line range is invalid")
    if evidence.end_line > len(lines):
        raise EvidenceError("evidence line range exceeds its pinned file")
    excerpt = "".join(lines[evidence.start_line - 1 : evidence.end_line])
    if hashlib.sha256(excerpt.encode("utf-8")).hexdigest() != evidence.content_sha256:
        raise EvidenceError("evidence content digest does not match its pinned range")
    return excerpt


def _validate_symbol_queries(queries: Sequence[str]) -> list[str]:
    if not 1 <= len(queries) <= MAX_SYMBOL_QUERIES:
        raise EvidenceError(f"symbol queries must contain 1-{MAX_SYMBOL_QUERIES} names")
    result: list[str] = []
    seen: set[str] = set()
    for query in queries:
        if (
            not isinstance(query, str)
            or not query.strip()
            or len(query) > MAX_SYMBOL_QUERY_CHARS
            or "/" in query
            or "\\" in query
            or PurePosixPath(query).suffix.casefold() in {".py", ".pyi"}
        ):
            raise EvidenceError(
                "symbol query must be a bounded symbol name, not a path"
            )
        normalized = query.strip()
        if normalized.casefold() not in seen:
            result.append(normalized)
            seen.add(normalized.casefold())
    return result


def _validate_test_kind(path: str, kind: str) -> None:
    if kind == "test" and not any(
        "test" in part.casefold() or "fixture" in part.casefold()
        for part in PurePosixPath(path).parts
    ):
        raise EvidenceError("test evidence must come from a test or fixture path")


def _kind(
    value: str,
) -> Literal["implementation", "test", "configuration", "execution_receipt"]:
    if value not in _EVIDENCE_KINDS:
        raise EvidenceError(f"unknown evidence kind: {value}")
    return value  # type: ignore[return-value]


def _evidence_id(ordinal: int) -> str:
    if ordinal < 1:
        raise EvidenceError("evidence ordinal must be positive")
    return f"evidence-{ordinal:04d}"


def _validate_repository_path(path: str) -> None:
    normalized = PurePosixPath(path)
    if (
        not path
        or normalized.is_absolute()
        or normalized.as_posix() != path
        or any(part in {"", ".", ".."} for part in path.split("/"))
    ):
        raise EvidenceError("evidence path must be a normalized repository path")


__all__ = [
    "Evidence",
    "EvidenceError",
    "MAX_SYMBOL_EVIDENCE_CHARS",
    "MAX_SYMBOL_QUERIES",
    "MAX_SYMBOL_QUERY_CHARS",
    "RequirementCoverage",
    "ResidualRequirement",
    "SymbolLookupResult",
    "bind_coverage_answer",
    "build_requirement_coverage_request",
    "create_requirement_coverage",
    "lookup_symbols_in_pinned_file",
    "read_pinned_code_file",
    "record_file_evidence",
]
