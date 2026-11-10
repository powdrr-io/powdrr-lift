"""Bounded local source inspection with frozen, service-owned Structrr context.

This reader performs no writes, imports no candidate code, and executes no shell
commands. Its envelope is an observation, not authorization: the environment
integration must independently verify source and catalog inputs before use.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import stat
import tokenize
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from powdrr_service.decision_obligation import content_fingerprint
from powdrr_service.structrr.reference_catalog import (
    ReferenceCatalog,
    resolve_reference,
)
from powdrr_service.structrr.source_references import (
    SourceReferenceIndex,
    applicable_references,
    index_python_source,
)

SOURCE_READ_SCHEMA = "structrr-source-read-v1"
CONTEXT_RENDERER_REVISION = "structrr-source-context-json-v1"
_EXCLUDED_DIRECTORIES = frozenset(
    {
        ".git",
        ".venv",
        ".worktrees",
        "node_modules",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
    }
)


@dataclass(frozen=True, slots=True)
class ReaderLimits:
    max_file_bytes: int = 1024 * 1024
    max_source_lines: int = 200
    max_search_matches: int = 50
    max_context_chars: int = 16000

    def __post_init__(self) -> None:
        for value in asdict(self).values():
            if type(value) is not int or value < 1:
                raise ValueError("reader limits must be positive integers")

    def to_data(self) -> dict[str, Any]:
        return {"schema_version": "structrr-source-reader-limits-v1", **asdict(self)}

    @classmethod
    def from_data(cls, raw: Mapping[str, Any]) -> ReaderLimits:
        if (
            set(raw)
            != {
                "schema_version",
                "max_file_bytes",
                "max_source_lines",
                "max_search_matches",
                "max_context_chars",
            }
            or raw.get("schema_version") != "structrr-source-reader-limits-v1"
        ):
            raise ValueError("invalid reader limits schema")
        return cls(
            **{key: value for key, value in raw.items() if key != "schema_version"}
        )

    @property
    def digest(self) -> str:
        return content_fingerprint(
            {
                "limits": self.to_data(),
                "renderer": CONTEXT_RENDERER_REVISION,
                "excluded_directories": sorted(_EXCLUDED_DIRECTORIES),
            }
        )


@dataclass(frozen=True, slots=True)
class SourceReadResult:
    """Immutable serialized backing; consumers receive detached envelope data."""

    _data_json: str

    def to_data(self) -> dict[str, Any]:
        return json.loads(self._data_json)

    @property
    def status(self) -> str:
        return str(self.to_data()["status"])

    @classmethod
    def from_data(cls, raw: Mapping[str, Any]) -> SourceReadResult:
        fields = {
            "schema_version",
            "operation",
            "status",
            "base_commit",
            "catalog_digest",
            "limits_digest",
            "renderer_revision",
            "source_complete",
            "scope_complete",
            "context_complete",
            "excerpts",
            "references",
            "context_pages",
            "remaining_refs",
            "diagnostics",
            "continuations",
            "fingerprint",
        }
        if set(raw) != fields or raw.get("schema_version") != SOURCE_READ_SCHEMA:
            raise ValueError("invalid source read envelope schema")
        if (
            raw.get("renderer_revision") != CONTEXT_RENDERER_REVISION
            or raw.get("operation") not in {"read", "search", "context"}
            or raw.get("status") not in {"complete", "incomplete", "error"}
        ):
            raise ValueError("unsupported reader operation, status, or renderer")
        for key in ("source_complete", "scope_complete", "context_complete"):
            if type(raw[key]) is not bool:
                raise ValueError("reader completeness must be boolean")
        if raw["status"] == "complete" and not all(
            raw[key]
            for key in ("source_complete", "scope_complete", "context_complete")
        ):
            raise ValueError("complete envelope contains incomplete observations")
        for key in ("base_commit", "catalog_digest", "limits_digest"):
            if not isinstance(raw[key], str) or not raw[key]:
                raise ValueError("missing reader input identity")
        for key in (
            "excerpts",
            "references",
            "context_pages",
            "diagnostics",
            "continuations",
        ):
            if not isinstance(raw[key], list) or any(
                not isinstance(item, Mapping) for item in raw[key]
            ):
                raise ValueError(f"invalid reader {key}")
        if not isinstance(raw["remaining_refs"], list) or any(
            not isinstance(ref, str) for ref in raw["remaining_refs"]
        ):
            raise ValueError("invalid remaining reference IDs")
        for excerpt in raw["excerpts"]:
            if set(excerpt) != {
                "path",
                "source_sha256",
                "start_line",
                "end_line",
                "line_numbers",
                "lines",
            }:
                raise ValueError("invalid source excerpt schema")
            if not isinstance(excerpt["lines"], list) or any(
                not isinstance(line, str) for line in excerpt["lines"]
            ):
                raise ValueError("invalid source lines")
            start, end = excerpt["start_line"], excerpt["end_line"]
            if (
                type(start) is not int
                or type(end) is not int
                or start < 1
                or end < start - 1
            ):
                raise ValueError("invalid excerpt range")
            if (
                excerpt["line_numbers"] != list(range(start, end + 1))
                or len(excerpt["lines"]) != end - start + 1
            ):
                raise ValueError("excerpt lines disagree with their labels")
        for page in raw["context_pages"]:
            if set(page) != {
                "ref",
                "offset",
                "text",
                "total_characters",
                "next_offset",
            }:
                raise ValueError("invalid context page schema")
            offset, total = page["offset"], page["total_characters"]
            if (
                type(offset) is not int
                or type(total) is not int
                or offset < 0
                or total < offset
                or not isinstance(page["text"], str)
            ):
                raise ValueError("invalid context page bounds")
            end = offset + len(page["text"])
            if end > total or page["next_offset"] != (end if end < total else None):
                raise ValueError("invalid context continuation")
        data = dict(raw)
        fingerprint = data.pop("fingerprint")
        if fingerprint != content_fingerprint(data):
            raise ValueError("source read envelope fingerprint mismatch")
        return cls(
            json.dumps(dict(raw), sort_keys=True, ensure_ascii=False, allow_nan=False)
        )


class _ReadFailure(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _relative(path: str, *, directory: bool = False) -> str:
    if (
        not isinstance(path, str)
        or not path
        or "\\" in path
        or PureWindowsPath(path).drive
    ):
        raise _ReadFailure("unsafe_path", "Use a repository-relative path.")
    parsed = PurePosixPath(path)
    if parsed.is_absolute() or ".." in parsed.parts:
        raise _ReadFailure(
            "unsafe_path", "Absolute paths and traversal are not allowed."
        )
    if not directory and parsed.suffix != ".py":
        raise _ReadFailure(
            "unsupported_source", "Only Python source files are supported."
        )
    return parsed.as_posix()


class SourceReader:
    """A trusted workspace/catalog session with a bounded digest-keyed index cache."""

    def __init__(
        self,
        workspace: Path,
        catalog: ReferenceCatalog,
        *,
        limits: ReaderLimits | None = None,
    ) -> None:
        self.workspace = workspace.resolve(strict=True)
        if not self.workspace.is_dir():
            raise ValueError("reader workspace must be a directory")
        self.catalog = catalog
        self.limits = limits or ReaderLimits()
        self._indexes: OrderedDict[tuple[str, str], SourceReferenceIndex] = (
            OrderedDict()
        )

    def _target(self, relative: str) -> Path:
        target = (self.workspace / relative).resolve(strict=True)
        if not target.is_relative_to(self.workspace):
            raise _ReadFailure(
                "unsafe_path", "Source path escapes the trusted workspace."
            )
        return target

    def _bytes(self, relative: str) -> bytes:
        target = self._target(relative)
        # Resolve permitted internal links first, then anchor traversal to a root
        # descriptor and refuse link replacements at every component.
        if os.open not in os.supports_dir_fd or not hasattr(os, "O_NOFOLLOW"):
            raise _ReadFailure(
                "unsupported_filesystem",
                "Secure descriptor-relative source reads are unavailable.",
            )
        parts = target.relative_to(self.workspace).parts
        descriptor = os.open(
            self.workspace, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        )
        try:
            for part in parts[:-1]:
                child = os.open(
                    part,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=descriptor,
                )
                os.close(descriptor)
                descriptor = child
            file_descriptor = os.open(
                parts[-1],
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=descriptor,
            )
            with os.fdopen(file_descriptor, "rb") as source:
                if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                    raise _ReadFailure(
                        "unsupported_source", "Source must be a regular file."
                    )
                data = source.read(self.limits.max_file_bytes + 1)
        finally:
            os.close(descriptor)
        if len(data) > self.limits.max_file_bytes:
            raise _ReadFailure(
                "file_too_large", "Source exceeds the configured per-file byte limit."
            )
        if b"\x00" in data:
            raise _ReadFailure("binary_source", "Source contains binary NUL bytes.")
        return data

    def _snapshot(
        self, relative: str
    ) -> tuple[bytes, tuple[str, ...], SourceReferenceIndex]:
        data = self._bytes(relative)
        try:
            encoding, _ = tokenize.detect_encoding(io.BytesIO(data).readline)
            text = data.decode(encoding)
        except (SyntaxError, UnicodeError, LookupError) as error:
            raise _ReadFailure("encoding_failed", str(error)) from error
        lines = tuple(io.StringIO(text, newline=""))
        digest = "sha256:" + hashlib.sha256(data).hexdigest()
        key = (relative, digest)
        index = self._indexes.get(key)
        if index is None:
            index = index_python_source(relative, data)
            self._indexes[key] = index
            if len(self._indexes) > 64:
                self._indexes.popitem(last=False)
        self._indexes.move_to_end(key)
        return data, lines, index

    def _context_text(self, ref: str) -> str:
        return json.dumps(
            resolve_reference(self.catalog, ref).to_data(),
            sort_keys=True,
            ensure_ascii=False,
            indent=2,
        )

    def _finish(
        self,
        operation: str,
        excerpts: list[dict[str, Any]],
        references: list[dict[str, Any]],
        diagnostics: list[dict[str, Any]],
        *,
        source_complete: bool = True,
        scope_complete: bool = True,
        error: bool = False,
        offset: int = 0,
    ) -> SourceReadResult:
        pages: list[dict[str, Any]] = []
        remaining: list[str] = []
        continuations: list[dict[str, Any]] = []
        budget = self.limits.max_context_chars
        resolved = True
        for reference in references:
            ref = reference["ref"]
            resolution = resolve_reference(self.catalog, ref)
            if resolution.status != "resolved":
                resolved = False
                diagnostics.append(
                    {
                        "code": resolution.status,
                        "message": resolution.reason,
                        "ref": ref,
                    }
                )
            text = self._context_text(ref)
            if offset > len(text):
                diagnostics.append(
                    {
                        "code": "invalid_offset",
                        "message": "Offset exceeds context length.",
                        "ref": ref,
                    }
                )
                error = True
                resolved = False
                continue
            end = min(len(text), offset + budget)
            if budget:
                pages.append(
                    {
                        "ref": ref,
                        "offset": offset,
                        "text": text[offset:end],
                        "total_characters": len(text),
                        "next_offset": end if end < len(text) else None,
                    }
                )
                budget -= end - offset
            if end < len(text):
                remaining.append(ref)
                continuations.append(
                    {
                        "ref": ref,
                        "offset": end,
                        "command": [
                            "python",
                            "-m",
                            "powdrr_lift.structrr.source_reader",
                            "context",
                            f"--ref={ref}",
                            f"--offset={end}",
                        ],
                    }
                )
        context_complete = scope_complete and resolved and not remaining and not error
        data = {
            "schema_version": SOURCE_READ_SCHEMA,
            "operation": operation,
            "status": "error"
            if error
            else "complete"
            if source_complete and context_complete
            else "incomplete",
            "base_commit": self.catalog.base_commit,
            "catalog_digest": self.catalog.digest,
            "limits_digest": self.limits.digest,
            "renderer_revision": CONTEXT_RENDERER_REVISION,
            "source_complete": source_complete and not error,
            "scope_complete": scope_complete,
            "context_complete": context_complete,
            "excerpts": excerpts,
            "references": references,
            "context_pages": pages,
            "remaining_refs": remaining,
            "diagnostics": diagnostics,
            "continuations": continuations,
        }
        data["fingerprint"] = content_fingerprint(data)
        return SourceReadResult.from_data(data)

    def _collect(
        self,
        relative: str,
        lines: tuple[str, ...],
        index: SourceReferenceIndex,
        start: int,
        end: int,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        excerpt = {
            "path": relative,
            "source_sha256": index.source_sha256,
            "start_line": start,
            "end_line": end,
            "line_numbers": list(range(start, end + 1)),
            "lines": list(lines[start - 1 : end]),
        }
        references: dict[str, dict[str, Any]] = {}
        if end >= start:
            for annotation in applicable_references(index, start, end):
                reference = references.setdefault(
                    annotation.ref,
                    {
                        "ref": annotation.ref,
                        "status": resolve_reference(
                            self.catalog, annotation.ref
                        ).status,
                        "locations": [],
                    },
                )
                reference["locations"].append(
                    {
                        "path": relative,
                        "source_sha256": index.source_sha256,
                        "annotation": asdict(annotation),
                    }
                )
        return excerpt, [references[ref] for ref in sorted(references)]

    def read(
        self, path: str, start: int = 1, end: int | None = None
    ) -> SourceReadResult:
        try:
            relative = _relative(path)
            if (
                type(start) is not int
                or start < 1
                or end is not None
                and (type(end) is not int or end < start)
            ):
                raise _ReadFailure(
                    "invalid_range", "Use one-based inclusive line ranges."
                )
            if end is not None and end - start + 1 > self.limits.max_source_lines:
                raise _ReadFailure("read_too_large", "Request a smaller source range.")
            _, lines, index = self._snapshot(relative)
            if not lines and start == 1 and end in (None, 1):
                end = 0
            else:
                end = len(lines) if end is None else end
                if end - start + 1 > self.limits.max_source_lines:
                    raise _ReadFailure(
                        "read_too_large", "Request a smaller source range."
                    )
                if start > len(lines) or end > len(lines):
                    raise _ReadFailure(
                        "invalid_range", "Requested range exceeds the source snapshot."
                    )
            excerpt, references = self._collect(relative, lines, index, start, end)
            diagnostics = [
                {"path": relative, **asdict(item)} for item in index.diagnostics
            ]
            return self._finish(
                "read",
                [excerpt],
                references,
                diagnostics,
                scope_complete=index.parse_status == "complete" and not diagnostics,
            )
        except (_ReadFailure, OSError, RuntimeError) as error:
            return self._finish(
                "read",
                [],
                [],
                [_diagnostic(error, path)],
                error=True,
                scope_complete=False,
            )

    def context(self, ref: str, offset: int = 0) -> SourceReadResult:
        if type(offset) is not int or offset < 0:
            return self._finish(
                "context",
                [],
                [],
                [
                    {
                        "code": "invalid_offset",
                        "message": "Context offsets must be non-negative integers.",
                    }
                ],
                error=True,
            )
        reference = {
            "ref": ref,
            "status": resolve_reference(self.catalog, ref).status,
            "locations": [],
        }
        return self._finish("context", [], [reference], [], offset=offset)

    def search(self, query: str, path: str = ".") -> SourceReadResult:
        diagnostics: list[dict[str, Any]] = []
        excerpts: list[dict[str, Any]] = []
        references: dict[str, dict[str, Any]] = {}
        source_complete = True
        scope_complete = True
        try:
            if (
                not isinstance(query, str)
                or not query
                or "\n" in query
                or "\r" in query
            ):
                raise _ReadFailure(
                    "invalid_query", "Supply a non-empty single-line literal query."
                )
            relative = _relative(path, directory=True)
            target = self._target(relative)
            if target.is_file():
                paths = [relative]
            elif target.is_dir():
                paths = self._paths(relative, diagnostics)
            else:
                raise _ReadFailure(
                    "unsupported_source", "Search requires a source file or directory."
                )
            for filename in paths:
                try:
                    _, lines, index = self._snapshot(_relative(filename))
                except (_ReadFailure, OSError, RuntimeError) as error:
                    diagnostics.append(_diagnostic(error, filename))
                    source_complete = False
                    continue
                for number, line in enumerate(lines, 1):
                    if query not in line:
                        continue
                    if len(excerpts) >= min(
                        self.limits.max_search_matches, self.limits.max_source_lines
                    ):
                        diagnostics.append(
                            {
                                "code": "search_match_limit",
                                "message": "More matches exist; narrow the search.",
                            }
                        )
                        source_complete = False
                        break
                    excerpt, found = self._collect(
                        filename, lines, index, number, number
                    )
                    excerpts.append(excerpt)
                    for reference in found:
                        combined = references.setdefault(
                            reference["ref"], {**reference, "locations": []}
                        )
                        for location in reference["locations"]:
                            if location not in combined["locations"]:
                                combined["locations"].append(location)
                    if index.diagnostics:
                        scope_complete = False
                        for item in index.diagnostics:
                            diagnostic = {"path": filename, **asdict(item)}
                            if diagnostic not in diagnostics:
                                diagnostics.append(diagnostic)
                if diagnostics and diagnostics[-1]["code"] == "search_match_limit":
                    break
            if diagnostics:
                source_complete = False
            return self._finish(
                "search",
                excerpts,
                [references[ref] for ref in sorted(references)],
                diagnostics,
                source_complete=source_complete,
                scope_complete=scope_complete,
            )
        except (_ReadFailure, OSError, RuntimeError) as error:
            return self._finish(
                "search",
                [],
                [],
                [_diagnostic(error, path)],
                error=True,
                scope_complete=False,
            )

    def _paths(self, relative: str, diagnostics: list[dict[str, Any]]) -> list[str]:
        paths: list[str] = []

        def scan_error(error: OSError) -> None:
            diagnostics.append(_diagnostic(error, relative))

        for directory, directories, files in os.walk(
            self.workspace / relative, followlinks=False, onerror=scan_error
        ):
            for name in sorted(directories.copy()):
                candidate = Path(directory) / name
                if name in _EXCLUDED_DIRECTORIES:
                    directories.remove(name)
                elif candidate.is_symlink():
                    directories.remove(name)
                    diagnostics.append(
                        {
                            "code": "symlink_directory_skipped",
                            "path": candidate.relative_to(self.workspace).as_posix(),
                            "message": "Search skips directory links.",
                        }
                    )
            paths.extend(
                (Path(directory) / name).relative_to(self.workspace).as_posix()
                for name in files
                if name.endswith(".py")
            )
        return sorted(paths)


def _diagnostic(error: Exception, path: str) -> dict[str, Any]:
    return {
        "code": error.code if isinstance(error, _ReadFailure) else "source_io_failed",
        "path": path,
        "message": str(error),
    }


def read_source(
    workspace: Path, catalog: ReferenceCatalog, path: str, start: int, end: int
) -> SourceReadResult:
    return SourceReader(workspace, catalog).read(path, start, end)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read Python source with frozen Structrr context."
    )
    commands = parser.add_subparsers(dest="operation", required=True)
    read = commands.add_parser("read")
    read.add_argument("path")
    read.add_argument("--start", type=int, default=1)
    read.add_argument("--end", type=int)
    search = commands.add_parser("search")
    search.add_argument("query")
    search.add_argument("path", nargs="?", default=".")
    context = commands.add_parser("context")
    context.add_argument("--ref", required=True)
    context.add_argument("--offset", type=int, default=0)
    args = parser.parse_args(argv)
    try:
        workspace = Path(os.environ["POWDRR_STRUCTRR_WORKSPACE"])
        catalog_path = Path(os.environ["POWDRR_STRUCTRR_CATALOG"])
        catalog = ReferenceCatalog.from_data(
            json.loads(catalog_path.read_text(encoding="utf-8"))
        )
        limits = (
            ReaderLimits.from_data(
                json.loads(os.environ["POWDRR_STRUCTRR_READER_LIMITS"])
            )
            if "POWDRR_STRUCTRR_READER_LIMITS" in os.environ
            else ReaderLimits()
        )
        reader = SourceReader(workspace, catalog, limits=limits)
    except (KeyError, ValueError, TypeError, OSError) as error:
        print(
            json.dumps(
                {
                    "schema_version": "structrr-source-reader-initialization-error-v1",
                    "status": "error",
                    "code": "invalid_reader_configuration",
                    "message": str(error),
                }
            )
        )
        return 2
    if args.operation == "read":
        result = reader.read(args.path, args.start, args.end)
    elif args.operation == "search":
        result = reader.search(args.query, args.path)
    else:
        result = reader.context(args.ref, args.offset)
    print(json.dumps(result.to_data(), ensure_ascii=False))
    return 0 if result.status == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
