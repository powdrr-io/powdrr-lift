"""Structrr's versioned Python code-symbol artifact and bounded query contract."""

from __future__ import annotations

import ast
import hashlib
import json
import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Literal

import yaml

CODE_SYMBOL_SCHEMA = "https://powdrr.io/schema/structrr-code-symbols-v1"
CODE_SYMBOL_REF_SCHEMA = "https://powdrr.io/schema/structrr-code-symbol-artifact-ref-v1"
CODE_QUERY_SCHEMA = "structrr-code-query-v1"
CODE_SYMBOL_MEDIA_TYPE = "application/vnd.powdrr.structrr-code-symbols+yaml"
_SHA = re.compile(r"^[0-9a-f]{64}$")
_REVISION = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


class CodeSymbolError(ValueError):
    """Invalid code-symbol artifact or query."""


@dataclass(frozen=True, slots=True)
class CodeSymbolArtifact:
    artifact_id: str
    content_ref: str
    content_digest: str
    source_revision: str
    source_tree_digest: str
    component_id: str
    provider_id: str
    provider_version: str
    configuration_fingerprint: str
    symbol_count: int
    relationship_count: int
    coverage: Literal["complete", "partial", "unavailable", "failed"]

    def to_data(self) -> dict[str, Any]:
        return {
            "schema": CODE_SYMBOL_REF_SCHEMA,
            "artifact_id": self.artifact_id,
            "content_ref": self.content_ref,
            "media_type": CODE_SYMBOL_MEDIA_TYPE,
            "content_digest": f"sha256:{self.content_digest}",
            "source_revision": self.source_revision,
            "source_tree_digest": f"sha256:{self.source_tree_digest}",
            "component_id": self.component_id,
            "provider_id": self.provider_id,
            "provider_version": self.provider_version,
            "configuration_fingerprint": self.configuration_fingerprint,
            "symbol_count": self.symbol_count,
            "relationship_count": self.relationship_count,
            "coverage": self.coverage,
        }


@dataclass(frozen=True, slots=True)
class CodeSymbolSearchResult(Mapping[str, list[dict[str, Any]]]):
    """Named symbol results plus artifact coverage for interpreting empty results."""

    symbols_by_path: Mapping[str, list[dict[str, Any]]]
    coverage: Mapping[str, Any]

    def __getitem__(self, path: str) -> list[dict[str, Any]]:
        return self.symbols_by_path[path]

    def __iter__(self) -> Iterator[str]:
        return iter(self.symbols_by_path)

    def __len__(self) -> int:
        return len(self.symbols_by_path)


@dataclass(frozen=True, slots=True)
class CodeSymbolQuery:
    structrr_snapshot_id: str
    query_kind: Literal["symbols", "references"]
    component_ids: tuple[str, ...] = ()
    paths: tuple[str, ...] = ()
    symbol_ids: tuple[str, ...] = ()
    symbol_names: tuple[str, ...] = ()
    relationship_kinds: tuple[str, ...] = ()
    limit: int = 50
    max_encoded_bytes: int = 32_768
    purpose: str = ""


def build_python_symbol_artifact(
    *,
    source_revision: str,
    source_tree_digest: str | None = None,
    component_id: str,
    provider_version: str,
    configuration_fingerprint: str,
    symbols_by_path: Mapping[str, Sequence[Mapping[str, Any]]],
    source_by_path: Mapping[str, str],
) -> tuple[bytes, CodeSymbolArtifact, dict[str, Any]]:
    """Normalize BasedPyright document symbols into deterministic Structrr YAML."""
    if not _REVISION.fullmatch(source_revision):
        raise CodeSymbolError("source_revision must be a full Git object ID")
    if not component_id or not provider_version:
        raise CodeSymbolError("component_id and provider_version are required")
    if not _SHA.fullmatch(configuration_fingerprint):
        raise CodeSymbolError("configuration_fingerprint must be a SHA-256 digest")
    tree_digest = source_tree_digest or _source_tree_digest(source_by_path)
    if not _SHA.fullmatch(tree_digest):
        raise CodeSymbolError("source_tree_digest must be a SHA-256 digest")

    symbols: list[dict[str, Any]] = []
    indexed_paths = sorted(set(symbols_by_path) & set(source_by_path))
    unindexed_paths = sorted(set(source_by_path) - set(symbols_by_path))
    coverage_status: Literal["complete", "partial"] = (
        "partial" if unindexed_paths else "complete"
    )
    for path in sorted(symbols_by_path):
        _validate_path(path)
        source = source_by_path.get(path)
        if not isinstance(source, str):
            raise CodeSymbolError(f"source text is missing for indexed path {path!r}")
        signatures = _python_signatures(source)
        _flatten_symbols(
            symbols_by_path[path],
            path=path,
            component_id=component_id,
            source=source,
            signatures=signatures,
            parent_name="",
            output=symbols,
        )
    symbols.sort(key=lambda item: item["symbol_id"])
    document: dict[str, Any] = {
        "schema": CODE_SYMBOL_SCHEMA,
        "source_revision": source_revision,
        "source_tree_digest": f"sha256:{tree_digest}",
        "components": [
            {
                "component_id": component_id,
                "language": "python",
                "provider_id": "basedpyright",
                "provider_version": provider_version,
                "configuration_fingerprint": configuration_fingerprint,
                "coverage": coverage_status,
            }
        ],
        "symbols": symbols,
        "relationships": [],
        "coverage": [
            {
                "component_id": component_id,
                "indexed_paths": indexed_paths,
                "excluded_paths": [],
                "unsupported_paths": [],
                "unindexed_paths": unindexed_paths,
                "status": coverage_status,
            }
        ],
    }
    content = (
        yaml.safe_dump(
            document,
            allow_unicode=True,
            default_flow_style=False,
            sort_keys=False,
            width=1_000_000,
        )
        .replace("\r\n", "\n")
        .encode("utf-8")
    )
    digest = hashlib.sha256(content).hexdigest()
    content_ref = f"artifacts/structrr/code-symbols/sha256-{digest}.yaml"
    reference = CodeSymbolArtifact(
        artifact_id=f"structrr-code-symbols:sha256:{digest}",
        content_ref=content_ref,
        content_digest=digest,
        source_revision=source_revision,
        source_tree_digest=tree_digest,
        component_id=component_id,
        provider_id="basedpyright",
        provider_version=provider_version,
        configuration_fingerprint=configuration_fingerprint,
        symbol_count=len(symbols),
        relationship_count=0,
        coverage=coverage_status,
    )
    return content, reference, document


def query_python_symbols(
    document: Mapping[str, Any],
    query: CodeSymbolQuery,
    *,
    artifact_id: str,
) -> dict[str, Any]:
    """Run a deterministic bounded symbol query against a validated index."""
    if not query.structrr_snapshot_id:
        raise CodeSymbolError("structrr_snapshot_id is required")
    if query.query_kind != "symbols":
        raise CodeSymbolError("only symbol queries are available in this index")
    if not 1 <= query.limit <= 200:
        raise CodeSymbolError("limit must be between 1 and 200")
    if not 1 <= query.max_encoded_bytes <= 1_000_000:
        raise CodeSymbolError("max_encoded_bytes must be between 1 and 1000000")
    _validate_filters(query.paths)
    selected_components = set(query.component_ids)
    selected_paths = set(query.paths)
    selected_ids = set(query.symbol_ids)
    selected_names = {name.casefold() for name in query.symbol_names}
    rows = document.get("symbols")
    if not isinstance(rows, list):
        raise CodeSymbolError("symbol artifact has no symbols array")
    matches = [
        item
        for item in rows
        if isinstance(item, Mapping)
        and (not selected_components or item.get("component_id") in selected_components)
        and (not selected_paths or item.get("path") in selected_paths)
        and (not selected_ids or item.get("symbol_id") in selected_ids)
        and (
            not selected_names
            or any(
                name in str(item.get("name", "")).casefold()
                or name in str(item.get("qualified_name", "")).casefold()
                for name in selected_names
            )
        )
    ]
    matches.sort(key=lambda item: str(item.get("symbol_id", "")))
    omitted = max(0, len(matches) - query.limit)
    selected = matches[: query.limit]
    coverage = document.get("coverage", [])
    result = {
        "schema_version": CODE_QUERY_SCHEMA,
        "structrr_snapshot_id": query.structrr_snapshot_id,
        "artifact_id": artifact_id,
        "query_kind": query.query_kind,
        "filters": {
            "component_ids": list(query.component_ids),
            "paths": list(query.paths),
            "symbol_ids": list(query.symbol_ids),
            "symbol_names": list(query.symbol_names),
        },
        "purpose": query.purpose,
        "records": selected,
        "omitted_count": omitted,
        "truncated": omitted > 0,
        "coverage": coverage,
    }
    return _bound_query_response(result, max_encoded_bytes=query.max_encoded_bytes)


def query_python_references(
    document: Mapping[str, Any],
    query: CodeSymbolQuery,
    *,
    artifact_id: str,
    references_by_path: Mapping[str, Sequence[str]],
) -> dict[str, Any]:
    """Wrap bounded on-demand BasedPyright path references in the Structrr query."""
    if not query.structrr_snapshot_id:
        raise CodeSymbolError("structrr_snapshot_id is required")
    if query.query_kind != "references":
        raise CodeSymbolError("query_kind must be references")
    if not 1 <= query.limit <= 200:
        raise CodeSymbolError("limit must be between 1 and 200")
    if not 1 <= query.max_encoded_bytes <= 1_000_000:
        raise CodeSymbolError("max_encoded_bytes must be between 1 and 1000000")
    _validate_filters(query.paths)
    selected_paths = set(query.paths)
    records = [
        {
            "source_path": source_path,
            "target_path": target_path,
            "kind": "references",
            "provider_id": "basedpyright",
        }
        for source_path, target_paths in sorted(references_by_path.items())
        if not selected_paths or source_path in selected_paths
        for target_path in sorted(set(target_paths))
        if target_path != source_path
    ]
    omitted = max(0, len(records) - query.limit)
    selected = records[: query.limit]
    result = {
        "schema_version": CODE_QUERY_SCHEMA,
        "structrr_snapshot_id": query.structrr_snapshot_id,
        "artifact_id": artifact_id,
        "query_kind": query.query_kind,
        "filters": {
            "component_ids": list(query.component_ids),
            "paths": list(query.paths),
            "symbol_ids": list(query.symbol_ids),
            "symbol_names": list(query.symbol_names),
            "relationship_kinds": list(query.relationship_kinds),
        },
        "purpose": query.purpose,
        "records": selected,
        "omitted_count": omitted,
        "truncated": omitted > 0,
        "coverage": document.get("coverage", []),
    }
    return _bound_query_response(result, max_encoded_bytes=query.max_encoded_bytes)


def load_code_symbol_artifact(
    content: bytes, reference: Mapping[str, Any]
) -> dict[str, Any]:
    """Validate artifact bytes against their Structrr reference before loading."""
    expected = reference.get("content_digest")
    actual = hashlib.sha256(content).hexdigest()
    if expected != f"sha256:{actual}":
        raise CodeSymbolError("symbol artifact digest does not match its reference")
    if reference.get("schema") != CODE_SYMBOL_REF_SCHEMA:
        raise CodeSymbolError("symbol artifact reference schema is invalid")
    if reference.get("media_type") != CODE_SYMBOL_MEDIA_TYPE:
        raise CodeSymbolError("symbol artifact reference media type is invalid")
    if reference.get("artifact_id") != f"structrr-code-symbols:sha256:{actual}":
        raise CodeSymbolError("symbol artifact ID does not match its content digest")
    try:
        value = yaml.safe_load(content)
    except yaml.YAMLError as error:
        raise CodeSymbolError("symbol artifact is not valid YAML") from error
    if not isinstance(value, dict) or value.get("schema") != CODE_SYMBOL_SCHEMA:
        raise CodeSymbolError("symbol artifact schema is invalid")
    if value.get("source_revision") != reference.get("source_revision"):
        raise CodeSymbolError("symbol artifact revision does not match its reference")
    if value.get("source_tree_digest") != reference.get("source_tree_digest"):
        raise CodeSymbolError(
            "symbol artifact source tree does not match its reference"
        )
    components = value.get("components")
    component = (
        next(
            (
                item
                for item in components
                if isinstance(item, Mapping)
                and item.get("component_id") == reference.get("component_id")
                and item.get("provider_id") == reference.get("provider_id")
                and item.get("provider_version") == reference.get("provider_version")
                and item.get("configuration_fingerprint")
                == reference.get("configuration_fingerprint")
            ),
            None,
        )
        if isinstance(components, list)
        else None
    )
    if not isinstance(component, Mapping):
        raise CodeSymbolError("symbol artifact component provenance is invalid")
    coverage_status = component.get("coverage")
    if coverage_status not in {"complete", "partial", "unavailable", "failed"}:
        raise CodeSymbolError("symbol artifact component coverage is invalid")
    if reference.get("coverage") != coverage_status:
        raise CodeSymbolError("symbol artifact coverage does not match its reference")
    symbols = value.get("symbols")
    if not isinstance(symbols, list):
        raise CodeSymbolError("symbol artifact symbols must be an array")
    ids: set[str] = set()
    for symbol in symbols:
        if not isinstance(symbol, Mapping):
            raise CodeSymbolError("symbol records must be mappings")
        symbol_id = symbol.get("symbol_id")
        if not isinstance(symbol_id, str) or not symbol_id or symbol_id in ids:
            raise CodeSymbolError("symbol IDs must be unique non-empty strings")
        ids.add(symbol_id)
        path = symbol.get("path")
        if not isinstance(path, str):
            raise CodeSymbolError("symbol path must be a repository-relative string")
        _validate_path(path)
    relationships = value.get("relationships")
    if not isinstance(relationships, list):
        raise CodeSymbolError("symbol artifact relationships must be an array")
    for relationship in relationships:
        if not isinstance(relationship, Mapping):
            raise CodeSymbolError("relationship records must be mappings")
        if (
            relationship.get("source_symbol_id") not in ids
            or relationship.get("target_symbol_id") not in ids
        ):
            raise CodeSymbolError(
                "relationship endpoints must identify indexed symbols"
            )
    coverage = value.get("coverage")
    if not isinstance(coverage, list):
        raise CodeSymbolError("symbol artifact coverage must be an array")
    coverage_record = next(
        (
            item
            for item in coverage
            if isinstance(item, Mapping)
            and item.get("component_id") == reference.get("component_id")
        ),
        None,
    )
    if not isinstance(coverage_record, Mapping):
        raise CodeSymbolError("symbol artifact component coverage is missing")
    indexed_paths = coverage_record.get("indexed_paths")
    unindexed_paths = coverage_record.get("unindexed_paths", [])
    if not isinstance(indexed_paths, list) or not isinstance(unindexed_paths, list):
        raise CodeSymbolError("symbol artifact coverage paths must be arrays")
    if not all(isinstance(path, str) for path in (*indexed_paths, *unindexed_paths)):
        raise CodeSymbolError("symbol artifact coverage paths must be strings")
    for path in (*indexed_paths, *unindexed_paths):
        _validate_path(path)
    if len(indexed_paths) != len(set(indexed_paths)) or len(unindexed_paths) != len(
        set(unindexed_paths)
    ):
        raise CodeSymbolError("symbol artifact coverage paths must be unique")
    if set(indexed_paths) & set(unindexed_paths):
        raise CodeSymbolError("indexed and unindexed paths must not overlap")
    if coverage_record.get("status") != coverage_status:
        raise CodeSymbolError("symbol artifact coverage status is inconsistent")
    if coverage_status == "complete" and unindexed_paths:
        raise CodeSymbolError("complete symbol coverage cannot have unindexed paths")
    if coverage_status == "partial" and not unindexed_paths:
        raise CodeSymbolError("partial symbol coverage must identify unindexed paths")
    if any(symbol.get("path") not in indexed_paths for symbol in symbols):
        raise CodeSymbolError("symbol records must belong to indexed paths")
    if reference.get("symbol_count") != len(symbols):
        raise CodeSymbolError("symbol artifact count does not match its reference")
    if reference.get("relationship_count") != len(relationships):
        raise CodeSymbolError("relationship count does not match its reference")
    return value


def _flatten_symbols(
    records: Sequence[Mapping[str, Any]],
    *,
    path: str,
    component_id: str,
    source: str,
    signatures: Mapping[tuple[str, str], str],
    parent_name: str,
    output: list[dict[str, Any]],
) -> None:
    for raw in records:
        name = raw.get("name")
        kind = raw.get("kind")
        location = raw.get("range")
        if not isinstance(name, str) or not name or not isinstance(kind, str):
            continue
        if not isinstance(location, Mapping):
            continue
        start_line = location.get("start_line")
        end_line = location.get("end_line")
        if (
            isinstance(start_line, bool)
            or not isinstance(start_line, int)
            or isinstance(end_line, bool)
            or not isinstance(end_line, int)
            or start_line < 1
            or end_line < start_line
        ):
            continue
        qualified_name = f"{parent_name}.{name}" if parent_name else name
        span = {
            "start_line": start_line,
            "start_column": location.get("start_character") or 0,
            "end_line": end_line,
            "end_column": location.get("end_character") or 0,
        }
        excerpt = "\n".join(source.splitlines()[start_line - 1 : end_line])
        fingerprint = hashlib.sha256(excerpt.encode("utf-8")).hexdigest()
        stable_identity = f"python\0{component_id}\0{path}\0{qualified_name}\0{kind}"
        symbol_id = (
            "symbol:python:"
            + hashlib.sha256(stable_identity.encode("utf-8")).hexdigest()
        )
        record = {
            "symbol_id": symbol_id,
            "component_id": component_id,
            "language": "python",
            "kind": kind,
            "name": name,
            "qualified_name": qualified_name,
            "path": path,
            "span": span,
            "resolution": "syntactic",
            "provider_id": "basedpyright",
            "source_fingerprint": f"sha256:{fingerprint}",
        }
        signature = signatures.get((qualified_name, kind))
        if signature is not None:
            record["signature"] = signature
        output.append(record)
        children = raw.get("children")
        if isinstance(children, list):
            valid_children = [item for item in children if isinstance(item, Mapping)]
            _flatten_symbols(
                valid_children,
                path=path,
                component_id=component_id,
                source=source,
                signatures=signatures,
                parent_name=qualified_name,
                output=output,
            )


def _validate_path(path: str) -> None:
    parsed = PurePosixPath(path)
    if (
        not path
        or parsed.is_absolute()
        or parsed.as_posix() != path
        or any(part in {"", ".", ".."} for part in path.split("/"))
    ):
        raise CodeSymbolError("symbol paths must be normalized repository paths")


def _validate_filters(paths: Sequence[str]) -> None:
    for path in paths:
        _validate_path(path)


def _encoded_size(value: object) -> int:
    return len(
        json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )


def _bound_query_response(
    result: dict[str, Any], *, max_encoded_bytes: int
) -> dict[str, Any]:
    """Fit the complete encoded response, including its envelope and digest."""
    result["result_fingerprint"] = "0" * 64
    records = result["records"]
    while records and _encoded_size(result) > max_encoded_bytes:
        records.pop()
        result["omitted_count"] += 1
        result["truncated"] = True
    if _encoded_size(result) > max_encoded_bytes:
        raise CodeSymbolError(
            "max_encoded_bytes is too small for the query response envelope"
        )
    fingerprint_payload = {
        key: value for key, value in result.items() if key != "result_fingerprint"
    }
    result["result_fingerprint"] = hashlib.sha256(
        json.dumps(
            fingerprint_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return result


def _source_tree_digest(source_by_path: Mapping[str, str]) -> str:
    digest = hashlib.sha256()
    for path, source in sorted(source_by_path.items()):
        _validate_path(path)
        digest.update(path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(source.encode("utf-8")).digest())
        digest.update(b"\0")
    return digest.hexdigest()


def _python_signatures(source: str) -> dict[tuple[str, str], str]:
    try:
        module = ast.parse(source)
    except SyntaxError:
        return {}
    signatures: dict[tuple[str, str], str] = {}

    def visit(items: Sequence[ast.stmt], prefix: str) -> None:
        for node in items:
            if isinstance(node, ast.ClassDef):
                qualified = f"{prefix}.{node.name}" if prefix else node.name
                bases = [ast.unparse(base) for base in node.bases]
                signature = (
                    f"class {node.name}({', '.join(bases)})"
                    if bases
                    else f"class {node.name}"
                )
                signatures[(qualified, "class")] = signature
                visit(node.body, qualified)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qualified = f"{prefix}.{node.name}" if prefix else node.name
                async_prefix = (
                    "async " if isinstance(node, ast.AsyncFunctionDef) else ""
                )
                signature = f"{async_prefix}def {node.name}({ast.unparse(node.args)})"
                if node.returns is not None:
                    signature += f" -> {ast.unparse(node.returns)}"
                signatures[(qualified, "function")] = signature
                signatures[(qualified, "method")] = signature
                visit(node.body, qualified)

    visit(module.body, "")
    return signatures


__all__ = [
    "CODE_QUERY_SCHEMA",
    "CODE_SYMBOL_MEDIA_TYPE",
    "CODE_SYMBOL_REF_SCHEMA",
    "CODE_SYMBOL_SCHEMA",
    "CodeSymbolArtifact",
    "CodeSymbolError",
    "CodeSymbolQuery",
    "CodeSymbolSearchResult",
    "build_python_symbol_artifact",
    "load_code_symbol_artifact",
    "query_python_references",
    "query_python_symbols",
]
