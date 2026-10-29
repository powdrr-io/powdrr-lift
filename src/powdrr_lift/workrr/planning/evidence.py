"""Pinned repository evidence and requirement-level coverage binding."""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Protocol

from powdrr_agent.repository import (
    LocalActionError,
    execute_command,
    find_workspace_symbols,
    inspect_pinned_checkout,
    read_pinned_file,
)
from powdrr_service.planning.contracts import (
    CoverageAnswer,
)
from powdrr_service.planning.coverage import (
    RequirementCoverage,
    RequirementEvidenceSearch,
    ResidualRequirement,
    bind_coverage_answer,
    create_requirement_coverage,
)
from powdrr_service.planning.coverage import (
    build_requirement_coverage_request as _service_build_requirement_coverage_request,
)
from powdrr_service.planning.evidence import Evidence, EvidenceError
from powdrr_service.planning.questions import (
    QuestionRequest,
)
from powdrr_service.planning.source import (
    PlanningSourceError,
    Requirement,
    SegmentedSource,
    pinned_source_document,
)

from powdrr_lift.errors import PowdrrExecutionError

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


class SymbolFinder(Protocol):
    def __call__(
        self,
        queries: list[str],
        *,
        worktree_root: Path,
        limit: int = 50,
    ) -> dict[str, list[dict[str, Any]]]: ...


@dataclass(frozen=True, slots=True)
class SymbolLookupResult:
    evidence: tuple[Evidence, ...]
    unresolved_queries: tuple[str, ...]


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

    all_terms = tuple(
        dict.fromkeys(
            term for values in terms_by_requirement.values() for term in values
        )
    )
    rg_rows: list[tuple[str, int, str]] = []
    for offset in range(0, len(all_terms), MAX_RG_COVERAGE_TERMS_PER_PASS):
        command = [
            "rg",
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
            "--glob",
            "!docs/**",
        ]
        for pattern in all_terms[offset : offset + MAX_RG_COVERAGE_TERMS_PER_PASS]:
            command.extend(("--regexp", pattern))
        for glob in _COVERAGE_SEARCH_PATHS:
            command.extend(("--glob", glob))
        command.append(".")
        try:
            receipt, stdout, stderr = execute_command(
                root, tuple(command), timeout_seconds=300
            )
        except OSError as error:
            raise EvidenceError(
                f"could not run rg in pinned repository: {error}"
            ) from error
        if receipt.status == "timed_out":
            raise EvidenceError("pinned repository rg search timed out")
        if receipt.exit_code not in {0, 1}:
            details = stderr.data.decode("utf-8", errors="replace").strip() or (
                f"rg exited with status {receipt.exit_code}"
            )
            raise EvidenceError(f"pinned repository rg search failed: {details}")
        for raw_line in stdout.data.decode("utf-8", errors="replace").splitlines():
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
    root = str(Path(repository_root).resolve())
    return _read_pinned_code_file_cached(root, base_commit, path)


@lru_cache(maxsize=256)
def _read_pinned_code_file_cached(
    repository_root: str, base_commit: str, path: str
) -> tuple[str, str]:
    try:
        artifact = read_pinned_file(repository_root, base_commit, path)
    except LocalActionError as error:
        raise EvidenceError(str(error)) from error
    blob_sha = artifact.attributes.get("git_blob_sha")
    if not isinstance(blob_sha, str):
        raise EvidenceError("agent did not return the pinned Git blob ID")
    try:
        document = pinned_source_document(
            str(Path(repository_root).resolve()),
            base_commit,
            artifact.artifact_id,
            blob_sha,
            artifact.data,
        )
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


def build_requirement_coverage_request(
    requirement: Requirement,
    *,
    source: SegmentedSource,
    evidence: Sequence[Evidence],
    repository_root: str | Path,
    repository_search: Mapping[str, Any] | None = None,
) -> QuestionRequest[CoverageAnswer]:
    """Capture cited pinned excerpts locally, then delegate policy to service."""
    excerpts = {
        item.id: _read_evidence_excerpt(repository_root, item) for item in evidence
    }
    return _service_build_requirement_coverage_request(
        requirement,
        source=source,
        evidence=evidence,
        evidence_excerpts=excerpts,
        repository_search=repository_search,
    )


def _require_clean_pinned_checkout(root: Path, base_commit: str) -> None:
    try:
        head, clean = inspect_pinned_checkout(root)
    except LocalActionError as error:
        raise EvidenceError("could not verify pinned analysis checkout") from error
    if head != base_commit:
        raise EvidenceError("symbol lookup checkout does not match pinned base commit")
    if not clean:
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


def read_evidence_excerpt(repository_root: str | Path, evidence: Evidence) -> str:
    """Read and verify the lines for an evidence record from a pinned commit."""
    return _read_evidence_excerpt(repository_root, evidence)


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
    if value not in {"implementation", "test", "configuration", "execution_receipt"}:
        raise EvidenceError(f"unknown evidence kind: {value}")
    return value  # type: ignore[return-value]


def _evidence_id(ordinal: int) -> str:
    if ordinal < 1:
        raise EvidenceError("evidence ordinal must be positive")
    return f"evidence-{ordinal:04d}"


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
