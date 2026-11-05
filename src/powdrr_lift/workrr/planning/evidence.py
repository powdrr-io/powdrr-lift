"""Pinned repository evidence and requirement-level coverage binding."""

from __future__ import annotations

import atexit
import hashlib
import json
import os
import re
import subprocess
import tarfile
import tempfile
from collections import Counter
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from functools import lru_cache
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Protocol

import yaml
from powdrr_agent.repository import (
    LocalActionError,
    execute_command,
    find_workspace_document_analysis,
    find_workspace_document_symbols,
    find_workspace_reference_paths,
    find_workspace_symbols,
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
from powdrr_service.structrr.code_symbols import (
    CodeSymbolArtifact,
    CodeSymbolQuery,
    build_python_symbol_artifact,
    load_code_symbol_artifact,
    query_python_references,
    query_python_symbols,
)

from powdrr_lift.errors import PowdrrExecutionError

MAX_SYMBOL_QUERIES = 8
MAX_SYMBOL_QUERY_CHARS = 120
MAX_SYMBOL_EVIDENCE_CHARS = 8_000
MAX_COVERAGE_SEARCH_TERMS = 8
MAX_BASEDPYRIGHT_COVERAGE_QUERIES = 2_048
MAX_CODE_SYMBOL_FILES_PER_PROVIDER_REQUEST = 512
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
_PINNED_ANALYSIS_TEMPORARIES: dict[
    tuple[str, str], tempfile.TemporaryDirectory[str]
] = {}


def _cleanup_pinned_analysis_temporaries() -> None:
    for temporary in _PINNED_ANALYSIS_TEMPORARIES.values():
        with suppress(OSError):
            temporary.cleanup()
    _PINNED_ANALYSIS_TEMPORARIES.clear()


atexit.register(_cleanup_pinned_analysis_temporaries)


def _pinned_analysis_root(repository_root: Path, base_commit: str) -> Path:
    """Materialize one immutable Git tree for code analysis tools."""
    root = repository_root.resolve()
    key = (str(root), base_commit)
    cached = _PINNED_ANALYSIS_TEMPORARIES.get(key)
    if cached is not None:
        return (Path(cached.name) / "repository").resolve()

    temporary = tempfile.TemporaryDirectory(prefix="powdrr-pinned-analysis-")
    snapshot = Path(temporary.name) / "repository"
    snapshot.mkdir()
    process: subprocess.Popen[bytes] | None = None
    try:
        process = subprocess.Popen(
            ["git", "-C", str(root), "archive", "--format=tar", base_commit],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert process.stdout is not None
        with tarfile.open(fileobj=process.stdout, mode="r|") as archive:
            archive.extractall(snapshot, filter="data")
        stderr = b"" if process.stderr is None else process.stderr.read()
        return_code = process.wait()
        if return_code != 0:
            raise EvidenceError(
                "could not materialize the pinned code-analysis snapshot: "
                + (
                    stderr.decode("utf-8", errors="replace").strip()
                    or "git archive failed"
                )
            )
    except (OSError, tarfile.TarError, EvidenceError) as error:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait()
        temporary.cleanup()
        if isinstance(error, EvidenceError):
            raise
        raise EvidenceError(
            f"could not materialize pinned code-analysis snapshot: {error}"
        ) from error
    _PINNED_ANALYSIS_TEMPORARIES[key] = temporary
    return snapshot.resolve()


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
    analysis_root = _pinned_analysis_root(root, base_commit)
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
            symbols_by_query = search_pinned_workspace_symbols(
                root,
                base_commit,
                list(symbol_queries),
                limit=20,
            )
        except (EvidenceError, PowdrrExecutionError):
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
                analysis_root, tuple(command), timeout_seconds=300
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


def search_dependency_reference_paths(
    repository_root: str | Path,
    base_commit: str,
    paths_by_unit: Mapping[str, Sequence[str]],
) -> dict[tuple[str, str], tuple[str, ...]]:
    """Map planned unit pairs connected by BasedPyright symbol references."""
    root = Path(repository_root).resolve()
    analysis_root = _pinned_analysis_root(root, base_commit)
    owners_by_path: dict[str, set[str]] = {}
    for unit_id, paths in paths_by_unit.items():
        for path in paths:
            if Path(path).suffix.casefold() in {".py", ".pyi"}:
                owners_by_path.setdefault(path, set()).add(unit_id)
    reference_paths = find_workspace_reference_paths(
        sorted(owners_by_path), worktree_root=analysis_root
    )
    candidate_paths: dict[tuple[str, str], set[str]] = {}
    for producer_path, consumer_paths in reference_paths.items():
        for consumer_path in consumer_paths:
            for producer_id in owners_by_path.get(producer_path, ()):
                for consumer_id in owners_by_path.get(consumer_path, ()):
                    if producer_id != consumer_id:
                        candidate_paths.setdefault(
                            (producer_id, consumer_id), set()
                        ).add(f"{producer_path} -> {consumer_path}")
    return {
        pair: tuple(sorted(paths)) for pair, paths in sorted(candidate_paths.items())
    }


def search_structural_document_symbols(
    repository_root: str | Path,
    base_commit: str,
    paths: Sequence[str],
) -> dict[str, list[dict[str, Any]]]:
    """Read BasedPyright symbols from a materialized pinned Git tree."""
    root = Path(repository_root).resolve()
    normalized_paths = tuple(sorted(set(paths)))
    _, reference, document = _load_or_build_pinned_symbol_artifact(root, base_commit)
    rows = document.get("symbols", [])
    selected = [
        row
        for row in rows
        if isinstance(row, Mapping) and row.get("path") in normalized_paths
    ]
    return _group_index_symbols(selected, reference)


def _load_or_build_pinned_symbol_artifact(
    repository_root: Path, base_commit: str
) -> tuple[bytes, CodeSymbolArtifact, dict[str, Any]]:
    """Load or materialize one reusable Python symbol artifact for a Git tree."""
    analysis_root = _pinned_analysis_root(repository_root, base_commit)
    return _load_or_build_symbol_artifact(
        repository_root, analysis_root, source_revision=base_commit
    )


def _load_or_build_symbol_artifact(
    repository_root: Path,
    source_root: Path,
    *,
    source_revision: str,
) -> tuple[bytes, CodeSymbolArtifact, dict[str, Any]]:
    """Index an exact file snapshot; commit plus tree digest identifies dirty trees."""
    try:
        provider_version = version("basedpyright")
    except PackageNotFoundError as error:
        raise EvidenceError("BasedPyright provider is not installed") from error
    config_fingerprint = _python_configuration_fingerprint(source_root)
    component_id = f"component:python:{repository_root.name}"
    python_paths = tuple(
        sorted(
            path.relative_to(source_root).as_posix()
            for path in source_root.rglob("*")
            if path.is_file()
            and not path.is_symlink()
            and path.suffix.casefold() in {".py", ".pyi"}
            and not _excluded_python_index_path(path.relative_to(source_root))
        )
    )
    source_by_path = {
        path: (source_root / path).read_text(encoding="utf-8") for path in python_paths
    }
    source_tree_digest = _source_tree_digest(source_by_path)
    cache_key = hashlib.sha256(
        json.dumps(
            {
                "source_revision": source_revision,
                "source_tree_digest": source_tree_digest,
                "component_id": component_id,
                "provider_id": "basedpyright",
                "provider_version": provider_version,
                "configuration_fingerprint": config_fingerprint,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    cache_root = repository_root / ".powdrr" / "cache" / "structrr-code-symbols"
    manifest_path = cache_root / f"{cache_key}.json"
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            reference_data = manifest["reference"]
            content_ref = reference_data["content_ref"]
            content_path = (repository_root / content_ref).resolve()
            if repository_root.resolve() not in content_path.parents:
                raise EvidenceError("cached symbol artifact path escapes repository")
            content = content_path.read_bytes()
            reference = _symbol_artifact_from_data(reference_data)
            document = load_code_symbol_artifact(content, reference.to_data())
            return content, reference, document
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            # A missing or corrupt cache entry is rebuilt from the pinned source.
            pass

    checked_in = _find_checked_in_symbol_artifact(
        repository_root,
        source_tree_digest=source_tree_digest,
        component_id=component_id,
        provider_version=provider_version,
        configuration_fingerprint=config_fingerprint,
    )
    if checked_in is not None:
        content, reference, document = checked_in
        cache_root.mkdir(parents=True, exist_ok=True)
        _write_atomically(
            manifest_path,
            json.dumps(
                {"cache_key": cache_key, "reference": reference.to_data()},
                sort_keys=True,
                indent=2,
            ).encode("utf-8")
            + b"\n",
        )
        return content, reference, document

    symbols_by_path: dict[str, list[dict[str, Any]]] = {}
    for offset in range(
        0, len(python_paths), MAX_CODE_SYMBOL_FILES_PER_PROVIDER_REQUEST
    ):
        batch = python_paths[
            offset : offset + MAX_CODE_SYMBOL_FILES_PER_PROVIDER_REQUEST
        ]
        try:
            batch_result = find_workspace_document_symbols(
                list(batch), worktree_root=source_root
            )
        except PowdrrExecutionError as error:
            raise EvidenceError(f"Python symbol indexing failed: {error}") from error
        symbols_by_path.update(batch_result)
    content, reference, document = build_python_symbol_artifact(
        source_revision=source_revision,
        source_tree_digest=source_tree_digest,
        component_id=component_id,
        provider_version=provider_version,
        configuration_fingerprint=config_fingerprint,
        symbols_by_path=symbols_by_path,
        source_by_path=source_by_path,
    )
    content_path = repository_root / reference.content_ref
    content_path.parent.mkdir(parents=True, exist_ok=True)
    _write_atomically(content_path, content)
    cache_root.mkdir(parents=True, exist_ok=True)
    _write_atomically(
        manifest_path,
        json.dumps(
            {"cache_key": cache_key, "reference": reference.to_data()},
            sort_keys=True,
            indent=2,
        ).encode("utf-8")
        + b"\n",
    )
    return content, reference, document


def _find_checked_in_symbol_artifact(
    repository_root: Path,
    *,
    source_tree_digest: str,
    component_id: str,
    provider_version: str,
    configuration_fingerprint: str,
) -> tuple[bytes, CodeSymbolArtifact, dict[str, Any]] | None:
    """Reuse a versioned artifact when its indexed Python tree still matches."""
    artifact_root = repository_root / "artifacts" / "structrr" / "code-symbols"
    if not artifact_root.is_dir():
        return None
    for content_path in sorted(artifact_root.glob("sha256-*.yaml")):
        try:
            content = content_path.read_bytes()
            document = yaml.safe_load(content)
            components = (
                document.get("components") if isinstance(document, dict) else None
            )
            if (
                not isinstance(document, dict)
                or document.get("source_tree_digest") != f"sha256:{source_tree_digest}"
                or not isinstance(components, list)
            ):
                continue
            component = next(
                (
                    row
                    for row in components
                    if isinstance(row, Mapping)
                    and row.get("component_id") == component_id
                    and row.get("provider_id") == "basedpyright"
                    and row.get("provider_version") == provider_version
                    and row.get("configuration_fingerprint")
                    == configuration_fingerprint
                ),
                None,
            )
            if component is None:
                continue
            digest = hashlib.sha256(content).hexdigest()
            symbols = document.get("symbols")
            relationships = document.get("relationships")
            if not isinstance(symbols, list) or not isinstance(relationships, list):
                continue
            reference = CodeSymbolArtifact(
                artifact_id=f"structrr-code-symbols:sha256:{digest}",
                content_ref=content_path.relative_to(repository_root).as_posix(),
                content_digest=digest,
                source_revision=document["source_revision"],
                source_tree_digest=source_tree_digest,
                component_id=component_id,
                provider_id="basedpyright",
                provider_version=provider_version,
                configuration_fingerprint=configuration_fingerprint,
                symbol_count=len(symbols),
                relationship_count=len(relationships),
                coverage=component.get("coverage", "unavailable"),
            )
            validated = load_code_symbol_artifact(content, reference.to_data())
            return content, reference, validated
        except (KeyError, OSError, TypeError, ValueError, yaml.YAMLError):
            # Invalid checked-in artifacts are ignored and rebuilt from source.
            continue
    return None


def _symbol_artifact_from_data(value: Mapping[str, Any]) -> CodeSymbolArtifact:
    digest = value.get("content_digest")
    if not isinstance(digest, str) or not digest.startswith("sha256:"):
        raise EvidenceError("cached symbol artifact reference has an invalid digest")
    coverage = value.get("coverage")
    if coverage not in {"complete", "partial", "unavailable", "failed"}:
        raise EvidenceError("cached symbol artifact coverage is invalid")
    fields = (
        "artifact_id",
        "content_ref",
        "source_revision",
        "source_tree_digest",
        "component_id",
        "provider_id",
        "provider_version",
        "configuration_fingerprint",
    )
    if not all(isinstance(value.get(field), str) for field in fields):
        raise EvidenceError("cached symbol artifact reference is incomplete")
    tree_digest = value.get("source_tree_digest")
    if not isinstance(tree_digest, str) or not tree_digest.startswith("sha256:"):
        raise EvidenceError("cached symbol artifact tree digest is invalid")
    symbol_count = value.get("symbol_count")
    relationship_count = value.get("relationship_count")
    if (
        isinstance(symbol_count, bool)
        or not isinstance(symbol_count, int)
        or isinstance(relationship_count, bool)
        or not isinstance(relationship_count, int)
    ):
        raise EvidenceError("cached symbol artifact counts are invalid")
    return CodeSymbolArtifact(
        artifact_id=str(value["artifact_id"]),
        content_ref=str(value["content_ref"]),
        content_digest=digest.removeprefix("sha256:"),
        source_revision=str(value["source_revision"]),
        source_tree_digest=tree_digest.removeprefix("sha256:"),
        component_id=str(value["component_id"]),
        provider_id=str(value["provider_id"]),
        provider_version=str(value["provider_version"]),
        configuration_fingerprint=str(value["configuration_fingerprint"]),
        symbol_count=symbol_count,
        relationship_count=relationship_count,
        coverage=coverage,
    )


def _python_configuration_fingerprint(root: Path) -> str:
    names = {
        "pyproject.toml",
        "pyrightconfig.json",
        "pyrightconfig.jsonc",
        "setup.cfg",
        "tox.ini",
        "uv.lock",
        "poetry.lock",
        "requirements.txt",
    }
    digest = hashlib.sha256()
    for path in sorted(
        item
        for item in root.rglob("*")
        if item.is_file()
        and item.name in names
        and not _excluded_python_index_path(item.relative_to(root))
    ):
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _excluded_python_index_path(path: Path) -> bool:
    return any(
        part.casefold()
        in {
            ".git",
            ".powdrr",
            ".venv",
            "venv",
            "vendor",
            "node_modules",
            "build",
            "dist",
        }
        for part in path.parts
    )


def _write_atomically(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _planning_structrr_snapshot_id(
    repository_root: Path, base_commit: str, reference: CodeSymbolArtifact
) -> str:
    fingerprint = hashlib.sha256(
        f"{repository_root.name}\0{base_commit}\0{reference.source_tree_digest}\0{reference.artifact_id}".encode()
    ).hexdigest()
    return f"structrr:sha256:{fingerprint}"


def _index_symbol_to_provider_record(
    record: Mapping[str, Any], reference: CodeSymbolArtifact
) -> dict[str, Any]:
    span = record.get("span")
    if not isinstance(span, Mapping):
        return {}
    return {
        "name": record.get("name"),
        "kind": record.get("kind"),
        "path": record.get("path"),
        "range": {
            "start_line": span.get("start_line"),
            "start_character": span.get("start_column"),
            "end_line": span.get("end_line"),
            "end_character": span.get("end_column"),
        },
        "symbol_id": record.get("symbol_id"),
        "qualified_name": record.get("qualified_name"),
        "signature": record.get("signature"),
        "artifact_id": reference.artifact_id,
        "artifact_digest": reference.content_digest,
    }


def _group_index_symbols(
    records: Sequence[Mapping[str, Any]],
    reference: CodeSymbolArtifact,
) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for record in records:
        path = record.get("path")
        if isinstance(path, str):
            grouped.setdefault(path, []).append(record)
    output: dict[str, list[dict[str, Any]]] = {}
    for path, path_records in sorted(grouped.items()):
        by_name: dict[str, dict[str, Any]] = {}
        roots: list[dict[str, Any]] = []
        for record in sorted(
            path_records,
            key=lambda item: (
                len(str(item.get("qualified_name", "")).split(".")),
                str(item.get("qualified_name", "")),
            ),
        ):
            provider_record = _index_symbol_to_provider_record(record, reference)
            qualified = record.get("qualified_name")
            if not isinstance(qualified, str):
                continue
            provider_record["children"] = []
            by_name[qualified] = provider_record
            parent = qualified.rpartition(".")[0]
            if parent in by_name:
                by_name[parent]["children"].append(provider_record)
            else:
                roots.append(provider_record)
        output[path] = roots
    return output


def search_pinned_workspace_symbols(
    repository_root: str | Path,
    base_commit: str,
    queries: Sequence[str],
    *,
    limit: int = 50,
) -> dict[str, list[dict[str, Any]]]:
    """Search the revision-pinned Structrr symbol artifact by deterministic name."""
    root = Path(repository_root).resolve()
    content, reference, document = _load_or_build_pinned_symbol_artifact(
        root, base_commit
    )
    del content
    snapshot_id = _planning_structrr_snapshot_id(root, base_commit, reference)
    return _query_symbol_artifact(
        document,
        reference,
        snapshot_id=snapshot_id,
        queries=queries,
        limit=limit,
    )


def search_workspace_symbols_from_worktree(
    repository_root: str | Path,
    queries: Sequence[str],
    *,
    limit: int = 50,
) -> dict[str, list[dict[str, Any]]]:
    """Search the current worktree snapshot, including dirty and untracked files."""
    root = Path(repository_root).resolve()
    revision_result = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--verify", "HEAD^{commit}"],
        capture_output=True,
        text=True,
        check=False,
    )
    revision = revision_result.stdout.strip()
    if revision_result.returncode != 0 or not _is_git_revision(revision):
        raise EvidenceError("workspace symbol indexing needs a full Git revision")
    _, reference, document = _load_or_build_symbol_artifact(
        root, root, source_revision=revision
    )
    snapshot_id = _planning_structrr_snapshot_id(root, revision, reference)
    return _query_symbol_artifact(
        document,
        reference,
        snapshot_id=snapshot_id,
        queries=queries,
        limit=limit,
    )


def _query_symbol_artifact(
    document: Mapping[str, Any],
    reference: CodeSymbolArtifact,
    *,
    snapshot_id: str,
    queries: Sequence[str],
    limit: int,
) -> dict[str, list[dict[str, Any]]]:
    output: dict[str, list[dict[str, Any]]] = {}
    for query_text in queries:
        query = CodeSymbolQuery(
            structrr_snapshot_id=snapshot_id,
            query_kind="symbols",
            symbol_names=(query_text,),
            limit=limit,
            purpose="planning workspace symbol lookup",
        )
        result = query_python_symbols(
            document, query, artifact_id=reference.artifact_id
        )
        output[query_text] = [
            _index_symbol_to_provider_record(row, reference)
            for row in result["records"]
        ]
    return output


def _source_tree_digest(source_by_path: Mapping[str, str]) -> str:
    digest = hashlib.sha256()
    for path, source in sorted(source_by_path.items()):
        digest.update(path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(source.encode("utf-8")).digest())
        digest.update(b"\0")
    return digest.hexdigest()


def _is_git_revision(value: str) -> bool:
    return bool(re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", value))


def pinned_code_symbol_artifact_reference(
    repository_root: str | Path, base_commit: str
) -> tuple[dict[str, Any], str]:
    """Return the durable Structrr reference and planning snapshot identity."""
    root = Path(repository_root).resolve()
    _, reference, _ = _load_or_build_pinned_symbol_artifact(root, base_commit)
    return (
        reference.to_data(),
        _planning_structrr_snapshot_id(root, base_commit, reference),
    )


def search_structural_document_analysis(
    repository_root: str | Path,
    base_commit: str,
    paths: Sequence[str],
) -> tuple[
    dict[str, list[dict[str, Any]]],
    dict[str, tuple[str, ...]],
    Literal["complete", "partial", "unavailable"],
]:
    """Read artifact-backed symbols and bounded references for pinned files."""
    root = Path(repository_root).resolve()
    analysis_root = _pinned_analysis_root(root, base_commit)
    normalized_paths = tuple(sorted(set(paths)))
    _, reference, document = _load_or_build_pinned_symbol_artifact(root, base_commit)
    selected_symbols = [
        row
        for row in document.get("symbols", [])
        if isinstance(row, Mapping) and row.get("path") in normalized_paths
    ]
    symbols = _group_index_symbols(selected_symbols, reference)
    try:
        _, references = find_workspace_document_analysis(
            list(normalized_paths), worktree_root=analysis_root
        )
    except PowdrrExecutionError:
        return symbols, {}, "unavailable"
    query = CodeSymbolQuery(
        structrr_snapshot_id=_planning_structrr_snapshot_id(
            root, base_commit, reference
        ),
        query_kind="references",
        paths=normalized_paths,
        relationship_kinds=("references",),
        limit=200,
        max_encoded_bytes=32_768,
        purpose="identify cross-file dependencies among planned Python paths",
    )
    query_result = query_python_references(
        document,
        query,
        artifact_id=reference.artifact_id,
        references_by_path=references,
    )
    result_references: dict[str, set[str]] = {}
    for record in query_result["records"]:
        source_path = record.get("source_path")
        target_path = record.get("target_path")
        if isinstance(source_path, str) and isinstance(target_path, str):
            result_references.setdefault(source_path, set()).add(target_path)
    normalized_references = {
        path: tuple(sorted(targets))
        for path, targets in sorted(result_references.items())
    }
    return (
        symbols,
        normalized_references,
        "partial" if query_result["truncated"] else "complete",
    )


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

    File paths are never sent as workspace-symbol queries. BasedPyright runs on
    a temporary materialization of `base_commit`, so local edits cannot affect
    the evidence result.
    """
    root = Path(repository_root).resolve()
    normalized_path, text = read_pinned_code_file(root, base_commit, path)
    analysis_root = _pinned_analysis_root(root, base_commit)
    evidence_kind = _kind(kind)
    if evidence_kind in {"configuration", "execution_receipt"}:
        raise EvidenceError("symbol evidence must be implementation or test code")
    _validate_test_kind(normalized_path, evidence_kind)
    normalized_queries = _validate_symbol_queries(queries)
    finder = symbol_finder or find_workspace_symbols
    try:
        results = finder(
            normalized_queries,
            worktree_root=analysis_root,
            limit=MAX_SYMBOL_QUERIES * 4,
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
    parts = PurePosixPath(path).parts
    if any(
        part.casefold()
        in {"test", "tests", "testing", "__tests__", "fixture", "fixtures"}
        for part in parts[:-1]
    ):
        return True
    name = parts[-1].casefold() if parts else ""
    return name.startswith(("test_", "test.", "test-")) or any(
        marker in name
        for marker in ("_test.", ".test.", ".spec.", "_spec.", ".fixture.")
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
    "pinned_code_symbol_artifact_reference",
    "read_pinned_code_file",
    "record_file_evidence",
    "search_requirement_evidence",
    "search_requirements_evidence",
    "search_pinned_workspace_symbols",
    "search_structural_document_analysis",
    "search_structural_document_symbols",
    "search_workspace_symbols_from_worktree",
]
