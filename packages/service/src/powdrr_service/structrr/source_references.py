"""Pure Python comment indexing and lexical ownership for Structrr references.

An index records source observations, not accepted intent or proof of behavior.
Candidate code is never imported or executed. Spans are navigation within the
hashed byte snapshot, not durable owner identities across revisions.
"""

from __future__ import annotations

import ast
import hashlib
import io
import re
import tokenize
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import PurePosixPath
from typing import Any

SOURCE_INDEX_SCHEMA = "structrr-source-index-v1"
_MARKER = re.compile(
    r"# structrr(?:\[(module|block)\])?:\s*([A-Za-z0-9_./:-]{1,256})\s*\Z"
)
_RESERVED = re.compile(r"# structrr")


@dataclass(frozen=True, slots=True)
class SourceOwner:
    kind: str
    qualified_name: str
    start_line: int
    end_line: int
    column: int

    def __post_init__(self) -> None:
        if self.kind not in {"module", "class", "function", "async_function", "block"}:
            raise ValueError("invalid source owner kind")
        if not self.qualified_name:
            raise ValueError("source owner requires a name")
        _span(self.start_line, self.end_line)
        _integer(self.column, 0)


@dataclass(frozen=True, slots=True)
class SourceAnnotation:
    ref: str
    scope: str
    line: int
    column: int
    owner: SourceOwner | None = None

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_./:-]{1,256}", self.ref, flags=re.ASCII):
            raise ValueError("invalid source reference ID")
        if self.scope not in {"declaration", "module", "block"}:
            raise ValueError("invalid annotation scope")
        _integer(self.line, 1)
        _integer(self.column, 0)


@dataclass(frozen=True, slots=True)
class SourceReferenceDiagnostic:
    code: str
    message: str
    line: int | None = None
    column: int | None = None

    def __post_init__(self) -> None:
        if (
            self.code
            not in {
                "malformed_annotation",
                "dangling_annotation",
                "duplicate_annotation",
                "invalid_scope",
                "scope_resolution_failed",
            }
            or not self.message
        ):
            raise ValueError("invalid source reference diagnostic")
        if self.line is not None:
            _integer(self.line, 1)
        if self.column is not None:
            _integer(self.column, 0)


@dataclass(frozen=True, slots=True)
class SourceReferenceIndex:
    path: str
    source_sha256: str
    parse_status: str
    line_count: int
    annotations: tuple[SourceAnnotation, ...]
    owners: tuple[SourceOwner, ...]
    diagnostics: tuple[SourceReferenceDiagnostic, ...]

    def __post_init__(self) -> None:
        _path(self.path)
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", self.source_sha256):
            raise ValueError("invalid source digest")
        if self.parse_status not in {"complete", "scope_resolution_failed"}:
            raise ValueError("invalid source parse status")
        _integer(self.line_count, 0)
        if len(set(self.owners)) != len(self.owners):
            raise ValueError("duplicate source owners")
        for owner in self.owners:
            if owner.end_line > max(1, self.line_count):
                raise ValueError("owner extends beyond source")
        for annotation in self.annotations:
            if annotation.line > self.line_count:
                raise ValueError("annotation extends beyond source")
            if annotation.owner is not None and annotation.owner not in self.owners:
                raise ValueError("annotation refers to an unknown owner")
        failed = any(
            item.code == "scope_resolution_failed" for item in self.diagnostics
        )
        if failed != (self.parse_status == "scope_resolution_failed"):
            raise ValueError("source parse status conflicts with diagnostics")
        if failed and (self.owners or any(a.owner for a in self.annotations)):
            raise ValueError("failed source index cannot claim resolved owners")

    def to_data(self) -> dict[str, Any]:
        data = asdict(self)
        data["annotations"] = [asdict(item) for item in self.annotations]
        data["owners"] = [asdict(item) for item in self.owners]
        data["diagnostics"] = [asdict(item) for item in self.diagnostics]
        return {"schema_version": SOURCE_INDEX_SCHEMA, **data}

    @classmethod
    def from_data(cls, raw: Mapping[str, Any]) -> SourceReferenceIndex:
        expected = {
            "schema_version",
            "path",
            "source_sha256",
            "parse_status",
            "line_count",
            "annotations",
            "owners",
            "diagnostics",
        }
        if set(raw) != expected or raw.get("schema_version") != SOURCE_INDEX_SCHEMA:
            raise ValueError("unsupported or invalid source index schema")
        for key in ("annotations", "owners", "diagnostics"):
            if not isinstance(raw[key], list) or any(
                not isinstance(item, Mapping) for item in raw[key]
            ):
                raise ValueError(f"source index {key} must be a list of objects")
        try:
            owners = tuple(SourceOwner(**item) for item in raw["owners"])
            annotations = tuple(
                SourceAnnotation(
                    **{
                        **item,
                        "owner": SourceOwner(**item["owner"])
                        if item.get("owner") is not None
                        else None,
                    }
                )
                for item in raw["annotations"]
            )
            return cls(
                path=raw["path"],
                source_sha256=raw["source_sha256"],
                parse_status=raw["parse_status"],
                line_count=raw["line_count"],
                annotations=annotations,
                owners=owners,
                diagnostics=tuple(
                    SourceReferenceDiagnostic(**item) for item in raw["diagnostics"]
                ),
            )
        except (TypeError, KeyError, AttributeError) as error:
            raise ValueError("invalid source index record") from error


def _integer(value: int, minimum: int) -> None:
    if type(value) is not int or value < minimum:
        raise ValueError("invalid source position")


def _span(start: int, end: int) -> None:
    _integer(start, 1)
    _integer(end, start)


def _path(path: str) -> None:
    if not isinstance(path, str) or not path or "\\" in path:
        raise ValueError("source path must be repository relative")
    parsed = PurePosixPath(path)
    if parsed.is_absolute() or ".." in parsed.parts or parsed.suffix != ".py":
        raise ValueError("source path must be a relative Python file")


def index_python_source(path: str, source: bytes) -> SourceReferenceIndex:
    """Index one exact byte snapshot; source errors become structured findings."""
    _path(path)
    digest = "sha256:" + hashlib.sha256(source).hexdigest()
    annotations: list[SourceAnnotation] = []
    diagnostics: list[SourceReferenceDiagnostic] = []
    comments: dict[int, tokenize.TokenInfo] = {}
    try:
        encoding, _ = tokenize.detect_encoding(io.BytesIO(source).readline)
        text = source.decode(encoding)
    except (SyntaxError, UnicodeError, LookupError) as error:
        return SourceReferenceIndex(
            path,
            digest,
            "scope_resolution_failed",
            0,
            (),
            (),
            (SourceReferenceDiagnostic("scope_resolution_failed", str(error)),),
        )
    # Python physical lines do not split on Unicode separators inside literals.
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    line_count = text.count("\n") + int(bool(text) and not text.endswith("\n"))
    token_failure = False
    try:
        for token in tokenize.generate_tokens(io.StringIO(text).readline):
            if token.type != tokenize.COMMENT:
                continue
            standalone = not token.line[: token.start[1]].strip()
            if standalone:
                comments[token.start[0]] = token
            if not _RESERVED.match(token.string):
                continue
            match = _MARKER.fullmatch(token.string)
            if not standalone or match is None:
                diagnostics.append(
                    SourceReferenceDiagnostic(
                        "malformed_annotation",
                        "Expected one standalone Structrr reference comment.",
                        *token.start,
                    )
                )
                continue
            annotations.append(
                SourceAnnotation(match[2], match[1] or "declaration", *token.start)
            )
    except (tokenize.TokenError, IndentationError, SyntaxError) as error:
        token_failure = True
        diagnostics.append(
            SourceReferenceDiagnostic("scope_resolution_failed", str(error))
        )
    try:
        tree = ast.parse(text, filename=path)
    except (SyntaxError, ValueError, RecursionError) as error:
        tree = None
        diagnostics.append(
            SourceReferenceDiagnostic("scope_resolution_failed", str(error))
        )
    if tree is None or token_failure:
        return SourceReferenceIndex(
            path,
            digest,
            "scope_resolution_failed",
            line_count,
            tuple(annotations),
            (),
            tuple(diagnostics),
        )

    owners: list[SourceOwner] = [SourceOwner("module", path, 1, max(1, line_count), 0)]
    starts: dict[tuple[int, int], list[SourceOwner]] = {}

    def walk(node: ast.AST, prefix: str = "") -> None:
        for child in ast.iter_child_nodes(node):
            nested = prefix
            if isinstance(child, ast.stmt):
                declaration = isinstance(
                    child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
                )
                if declaration:
                    assert isinstance(
                        child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
                    )
                    nested = prefix + child.name
                    kind = (
                        "class"
                        if isinstance(child, ast.ClassDef)
                        else "async_function"
                        if isinstance(child, ast.AsyncFunctionDef)
                        else "function"
                    )
                    start = min(
                        [child.lineno, *(item.lineno for item in child.decorator_list)]
                    )
                    name = nested
                else:
                    kind, start, name = (
                        "block",
                        child.lineno,
                        prefix.rstrip(".") + "::<" + type(child).__name__ + ">",
                    )
                owner = SourceOwner(
                    kind,
                    name,
                    start,
                    child.end_lineno or child.lineno,
                    child.col_offset,
                )
                owners.append(owner)
                starts.setdefault((start, child.col_offset), []).append(owner)
                if declaration:
                    nested += "."
            walk(child, nested)

    try:
        walk(tree)
    except RecursionError:
        diagnostics.append(
            SourceReferenceDiagnostic(
                "scope_resolution_failed", "Source exceeds AST scope traversal depth."
            )
        )
        return SourceReferenceIndex(
            path,
            digest,
            "scope_resolution_failed",
            line_count,
            tuple(annotations),
            (),
            tuple(diagnostics),
        )
    executable = tree.body
    if (
        executable
        and isinstance(executable[0], ast.Expr)
        and isinstance(executable[0].value, ast.Constant)
        and isinstance(executable[0].value.value, str)
    ):
        executable = executable[1:]
    if executable:
        first = executable[0]
        first_executable = min(
            [first.lineno, *(d.lineno for d in getattr(first, "decorator_list", []))]
        )
    else:
        first_executable = line_count + 1
    bound: list[SourceAnnotation] = []
    seen: set[tuple[SourceOwner, str]] = set()
    for annotation in annotations:
        owner = None
        if annotation.scope == "module":
            if annotation.column == 0 and annotation.line < first_executable:
                owner = owners[0]
            else:
                diagnostics.append(
                    SourceReferenceDiagnostic(
                        "invalid_scope",
                        "Module references must precede executable module statements.",
                        annotation.line,
                        annotation.column,
                    )
                )
        else:
            next_line = annotation.line + 1
            while next_line in comments:
                next_line += 1
            candidates = starts.get((next_line, annotation.column), [])
            if len(candidates) == 1:
                candidate = candidates[0]
                if annotation.scope == "declaration" and candidate.kind == "block":
                    diagnostics.append(
                        SourceReferenceDiagnostic(
                            "invalid_scope",
                            "Plain references require a class or function declaration.",
                            annotation.line,
                            annotation.column,
                        )
                    )
                else:
                    owner = candidate
            else:
                diagnostics.append(
                    SourceReferenceDiagnostic(
                        "dangling_annotation",
                        "No adjacent owner at the annotation's indentation.",
                        annotation.line,
                        annotation.column,
                    )
                )
        if owner is not None:
            key = (owner, annotation.ref)
            if key in seen:
                diagnostics.append(
                    SourceReferenceDiagnostic(
                        "duplicate_annotation",
                        "Reference is repeated on the same owner.",
                        annotation.line,
                        annotation.column,
                    )
                )
            seen.add(key)
        bound.append(
            SourceAnnotation(
                annotation.ref,
                annotation.scope,
                annotation.line,
                annotation.column,
                owner,
            )
        )
    return SourceReferenceIndex(
        path,
        digest,
        "complete",
        line_count,
        tuple(bound),
        tuple(owners),
        tuple(sorted(diagnostics, key=lambda d: (d.line or 0, d.column or 0, d.code))),
    )


def applicable_references(
    index: SourceReferenceIndex, start: int, end: int
) -> tuple[SourceAnnotation, ...]:
    """Return every contributing location, grouped by exact ID then location.

    Consumers resolve each distinct ref once, retaining these contributing records.
    Unbound markers are returned only when their own lines are requested. A failed
    index never claims inherited context: callers must also inspect parse_status.
    """
    _span(start, end)
    if end > index.line_count:
        raise ValueError("requested range extends beyond source")
    return tuple(
        sorted(
            (
                annotation
                for annotation in index.annotations
                if (
                    start <= annotation.line <= end
                    or annotation.owner is not None
                    and annotation.owner.start_line <= end
                    and annotation.owner.end_line >= start
                )
            ),
            key=lambda a: (a.ref, a.line, a.column),
        )
    )


def find_owner(
    index: SourceReferenceIndex, kind: str, qualified_name: str
) -> SourceOwner | None:
    """Resolve a selector only when unique within this source snapshot."""
    matches = [
        owner
        for owner in index.owners
        if owner.kind == kind and owner.qualified_name == qualified_name
    ]
    if len(matches) > 1:
        raise ValueError("ambiguous source owner; select an explicit snapshot span")
    return matches[0] if matches else None
