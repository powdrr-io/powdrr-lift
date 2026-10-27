"""Pinned repository evidence and requirement-level coverage binding."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from collections import Counter
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
MAX_COVERAGE_SEARCH_TERMS = 8
MAX_BASEDPYRIGHT_COVERAGE_QUERIES = 2_048
MAX_RG_COVERAGE_TERMS_PER_PASS = 512
MAX_COVERAGE_EVIDENCE_PER_KIND = 4
MAX_COVERAGE_EVIDENCE_PER_REQUIREMENT = MAX_COVERAGE_EVIDENCE_PER_KIND * 2
MAX_COVERAGE_EVIDENCE_LINE_CHARS = 1_200
_COVERAGE_STOPWORDS = frozenset(
    {
        "about",
        "after",
        "also",
        "already",
        "among",
        "because",
        "before",
        "being",
        "both",
        "could",
        "each",
        "from",
        "have",
        "into",
        "must",
        "only",
        "other",
        "should",
        "that",
        "their",
        "there",
        "these",
        "this",
        "through",
        "under",
        "using",
        "when",
        "where",
        "which",
        "with",
        "within",
    }
)
_COVERAGE_SEARCH_PATHS = (
    "*.py",
    "*.pyi",
    "*.go",
    "*.rs",
    "*.swift",
    "*.js",
    "*.jsx",
    "*.ts",
    "*.tsx",
    "*.java",
    "*.kt",
    "*.rb",
    "*.php",
    "*.cs",
    "*.c",
    "*.h",
    "*.cc",
    "*.cpp",
    "*.hpp",
    "*.sh",
    "*.bash",
    "*.zsh",
    "*.sql",
    "*.toml",
    "*.yaml",
    "*.yml",
    "*.json",
    "Makefile",
    "makefile",
    "Dockerfile",
)
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


@dataclass(frozen=True, slots=True)
class RequirementEvidenceSearch:
    """Bounded symbol and text search results tied to one pinned commit."""

    evidence: tuple[Evidence, ...]
    terms: tuple[str, ...]
    matching_line_count: int
    matching_path_count: int
    base_commit: str
    basedpyright_query_count: int = 0
    basedpyright_match_count: int = 0
    rg_match_count: int = 0
    basedpyright_available: bool = True

    def to_data(self) -> dict[str, Any]:
        return {
            "method": "basedpyright-symbols-and-rg-v1",
            "base_commit": self.base_commit,
            "terms": list(self.terms),
            "matching_line_count": self.matching_line_count,
            "matching_path_count": self.matching_path_count,
            "evidence_count": len(self.evidence),
            "basedpyright_query_count": self.basedpyright_query_count,
            "basedpyright_match_count": self.basedpyright_match_count,
            "rg_match_count": self.rg_match_count,
            "basedpyright_available": self.basedpyright_available,
            "scope": "tracked source, test, and configuration files",
        }


def search_requirement_evidence(
    repository_root: str | Path,
    base_commit: str,
    requirement: Requirement,
    *,
    first_ordinal: int,
) -> RequirementEvidenceSearch:
    """Find evidence for one requirement using the batched search implementation."""
    return search_requirements_evidence(
        repository_root,
        base_commit,
        (requirement,),
        first_ordinal=first_ordinal,
    )[requirement.id]


def search_requirements_evidence(
    repository_root: str | Path,
    base_commit: str,
    requirements: Sequence[Requirement],
    *,
    first_ordinal: int = 1,
) -> dict[str, RequirementEvidenceSearch]:
    """Search all plan requirements in one basedpyright session and rg passes."""
    root = Path(repository_root).resolve()
    _require_clean_pinned_checkout(root, base_commit)
    requirements_by_id = {item.id: item for item in requirements}
    if len(requirements_by_id) != len(requirements):
        raise EvidenceError("requirement IDs must be unique for repository search")
    terms_by_requirement = {
        item.id: _coverage_search_terms(item.statement) for item in requirements
    }
    query_by_requirement = {
        item.id: terms_by_requirement[item.id][0]
        for item in requirements
        if terms_by_requirement[item.id]
    }
    symbol_queries = tuple(dict.fromkeys(query_by_requirement.values()))[
        :MAX_BASEDPYRIGHT_COVERAGE_QUERIES
    ]
    symbols_by_query: dict[str, list[dict[str, Any]]] = {}
    basedpyright_available = True
    if symbol_queries:
        try:
            symbols_by_query = find_workspace_symbols(
                list(symbol_queries), worktree_root=root, limit=20
            )
        except PowdrrExecutionError:
            # Ripgrep still provides local multi-language evidence when the
            # Python language server is unavailable or cannot index this repo.
            basedpyright_available = False

    rg_binary = shutil.which("rg")
    if rg_binary is None:
        raise EvidenceError("rg is required for local repository coverage search")
    all_terms = tuple(
        dict.fromkeys(
            term for values in terms_by_requirement.values() for term in values
        )
    )
    rg_rows: list[tuple[str, int, str]] = []
    for offset in range(0, len(all_terms), MAX_RG_COVERAGE_TERMS_PER_PASS):
        command = [
            rg_binary,
            "--line-number",
            "--no-heading",
            "--color",
            "never",
            "--hidden",
            "--ignore-case",
            "--fixed-strings",
            "--glob",
            "!.git/**",
            "--glob",
            "!.venv/**",
            "--glob",
            "!node_modules/**",
            "--glob",
            "!.worktrees/**",
        ]
        for pattern in all_terms[offset : offset + MAX_RG_COVERAGE_TERMS_PER_PASS]:
            command.extend(("--regexp", pattern))
        for glob in _COVERAGE_SEARCH_PATHS:
            command.extend(("--glob", glob))
        command.append(".")
        try:
            result = subprocess.run(
                command,
                cwd=root,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
        except OSError as error:
            raise EvidenceError(
                f"could not run rg in pinned repository: {error}"
            ) from error
        if result.returncode not in {0, 1}:
            details = result.stderr.strip() or f"rg exited {result.returncode}"
            raise EvidenceError(f"pinned repository rg search failed: {details}")
        for raw_line in result.stdout.splitlines():
            parts = raw_line.split(":", 2)
            if len(parts) != 3 or not parts[1].isdigit():
                continue
            path = parts[0].removeprefix("./")
            text = parts[2]
            if len(text) <= MAX_COVERAGE_EVIDENCE_LINE_CHARS:
                rg_rows.append((path, int(parts[1]), text))
    rg_rows = list(dict.fromkeys(rg_rows))

    output: dict[str, RequirementEvidenceSearch] = {}
    pinned_files: dict[str, tuple[str, ...]] = {}
    for req_index, requirement in enumerate(requirements):
        terms = terms_by_requirement[requirement.id]
        term_patterns = tuple(re.compile(re.escape(term), re.I) for term in terms)
        candidates: list[tuple[int, int, str, int, int, str | None]] = []
        # Source priority 0 is basedpyright symbol evidence; rg is the
        # cross-language fallback and is ranked after those symbol matches.
        symbol_query = query_by_requirement.get(requirement.id)
        symbol_match_count = 0
        matching_paths: set[str] = set()
        if (
            basedpyright_available
            and symbol_query in symbol_queries
            and symbol_query is not None
        ):
            for symbol in symbols_by_query.get(symbol_query, []):
                symbol_path = symbol.get("path")
                name = symbol.get("name")
                location = symbol.get("range")
                if (
                    not isinstance(symbol_path, str)
                    or not symbol_path.casefold().endswith((".py", ".pyi"))
                    or not isinstance(name, str)
                    or not isinstance(location, Mapping)
                ):
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
                ):
                    continue
                symbol_match_count += 1
                matching_paths.add(symbol_path)
                candidates.append((0, 1, symbol_path, start, end, name))

        rg_match_count = 0
        for path, line_number, text in rg_rows:
            matched_terms = sum(bool(pattern.search(text)) for pattern in term_patterns)
            if not matched_terms:
                continue
            rg_match_count += 1
            matching_paths.add(path)
            candidates.append(
                (
                    1,
                    matched_terms,
                    path,
                    line_number,
                    line_number,
                    None,
                )
            )

        selected: list[tuple[int, int, str, int, int, str | None]] = []
        for kind in ("implementation", "test"):
            ranked = sorted(
                (
                    item
                    for item in candidates
                    if ("test" if _is_test_path(item[2]) else "implementation") == kind
                ),
                key=lambda item: (item[0], -item[1], item[2], item[3], item[4]),
            )
            distinct: list[tuple[int, int, str, int, int, str | None]] = []
            for candidate in ranked:
                _, _, path, start, end, _ = candidate
                if any(
                    existing[2] == path and start <= existing[4] and end >= existing[3]
                    for existing in distinct
                ):
                    continue
                distinct.append(candidate)
            selected.extend(distinct[:MAX_COVERAGE_EVIDENCE_PER_KIND])

        evidence: list[Evidence] = []
        seen_spans: set[tuple[str, int, int, str]] = set()
        for source_rank, _, path, start, end, symbol_name in selected:
            del source_rank
            evidence_kind = "test" if _is_test_path(path) else "implementation"
            key = (path, start, end, evidence_kind)
            if key in seen_spans:
                continue
            seen_spans.add(key)
            if path not in pinned_files:
                _, content = read_pinned_code_file(root, base_commit, path)
                pinned_files[path] = tuple(content.splitlines(keepends=True))
            lines = pinned_files[path]
            if start > len(lines) or end > len(lines):
                continue
            excerpt = "".join(lines[start - 1 : end])
            if len(excerpt) > MAX_SYMBOL_EVIDENCE_CHARS:
                excerpt = lines[start - 1]
                end = start
            if len(excerpt) > MAX_COVERAGE_EVIDENCE_LINE_CHARS:
                excerpt = lines[start - 1]
                end = start
            if len(excerpt) > MAX_COVERAGE_EVIDENCE_LINE_CHARS:
                continue
            evidence.append(
                Evidence(
                    id=_evidence_id(
                        first_ordinal
                        + req_index * MAX_COVERAGE_EVIDENCE_PER_REQUIREMENT
                        + len(evidence)
                    ),
                    base_commit=base_commit,
                    path=path,
                    start_line=start,
                    end_line=end,
                    content_sha256=hashlib.sha256(excerpt.encode("utf-8")).hexdigest(),
                    kind=evidence_kind,  # type: ignore[arg-type]
                    symbol=symbol_name,
                )
            )
        output[requirement.id] = RequirementEvidenceSearch(
            tuple(evidence),
            terms,
            rg_match_count + symbol_match_count,
            len(matching_paths),
            base_commit,
            len(symbol_queries),
            symbol_match_count,
            rg_match_count,
            basedpyright_available,
        )
    return output


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
    repository_search: Mapping[str, Any] | None = None,
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
    search_record = (
        None
        if repository_search is None
        else ContextRecord("repository-search", dict(repository_search))
    )
    if search_record is not None:
        accepted += (search_record,)
    digest_material = {
        "requirement": {
            "id": requirement.id,
            "statement": requirement.statement,
            "kind": requirement.kind,
            "source_refs": list(requirement.source_refs),
            "source_group_ids": list(requirement.source_group_ids),
        },
        "evidence": [item.to_data() for item in evidence_records],
        "repository_search": (
            None if repository_search is None else dict(repository_search)
        ),
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
            context_ref_ids=tuple(
                list(supplied_ids)
                + [requirement.id]
                + ([] if search_record is None else [search_record.ref_id])
            ),
            expected_answer_schema="coverage-answer-v1",
            attempt=1,
        ),
        exact_question=(
            "This source-validated requirement is the unit of assessment. Decide "
            "whether it is already implemented in the pinned repository. Use only "
            "the supplied code evidence and repository-search record. Search hits "
            "are candidates; ignore irrelevant matches. If the supplied matches "
            "do not implement this behavior, mark it missing. Missing means this "
            "requirement is work to plan, not a reason to stop. A search with no "
            "matches is a valid missing result; do not invent evidence."
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


def _coverage_search_terms(statement: str) -> tuple[str, ...]:
    candidates = re.findall(r"[A-Za-z][A-Za-z0-9_]{2,}", statement)
    frequency = Counter(candidate.casefold() for candidate in candidates)
    ranked = sorted(
        {
            candidate.casefold()
            for candidate in candidates
            if candidate.casefold() not in _COVERAGE_STOPWORDS
        },
        key=lambda term: (-frequency[term], -len(term), term),
    )
    return tuple(ranked[:MAX_COVERAGE_SEARCH_TERMS])


def _is_test_path(path: str) -> bool:
    return any(
        "test" in part.casefold() or "fixture" in part.casefold()
        for part in PurePosixPath(path).parts
    )


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
    "RequirementEvidenceSearch",
    "RequirementCoverage",
    "ResidualRequirement",
    "SymbolLookupResult",
    "bind_coverage_answer",
    "build_requirement_coverage_request",
    "create_requirement_coverage",
    "lookup_symbols_in_pinned_file",
    "read_pinned_code_file",
    "record_file_evidence",
    "search_requirement_evidence",
    "search_requirements_evidence",
]
