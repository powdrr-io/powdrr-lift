"""Repository evidence and scenario coverage for implementation contracts.

The closure is a pre-implementation artifact. It reads Python files listed in
the captured base commit's Git tree and links accepted behavior scenarios to
source declarations and statically visible calls. It never scans untracked or
post-base worktree files, and Workrr refuses capture if tracked files differ
from the base commit. The links are evidence candidates: Python's dynamic
dispatch means this inventory cannot claim a complete runtime call graph.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

CONTRACT_CLOSURE_SCHEMA_VERSION = "contract-closure-v1"
CONTRACT_CLOSURE_COMPILER_REVISION = "contract-closure-compiler-v1"
PYTHON_EVIDENCE_REVISION = "python-contract-evidence-v1"


class ContractClosureError(ValueError):
    """Raised when repository evidence or closure references are invalid."""


def _fingerprint(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class RepositoryEvidenceRecord:
    evidence_id: str
    kind: str
    name: str
    qualified_name: str
    path: str
    start_line: int
    end_line: int
    signature: str = ""
    bases: tuple[str, ...] = ()
    calls: tuple[str, ...] = ()
    docstring: str = ""
    fingerprint: str = ""

    def __post_init__(self) -> None:
        if not self.evidence_id or not self.name or not self.path:
            raise ContractClosureError("repository evidence needs identity and path")
        if self.start_line < 1 or self.end_line < self.start_line:
            raise ContractClosureError("repository evidence has an invalid source span")
        expected_fingerprint = _fingerprint(self.to_data(False))
        if not self.fingerprint:
            object.__setattr__(self, "fingerprint", expected_fingerprint)
        elif self.fingerprint != expected_fingerprint:
            raise ContractClosureError(
                "repository evidence record fingerprint is stale"
            )

    def to_data(self, include_fingerprint: bool = True) -> dict[str, Any]:
        result: dict[str, Any] = {
            "evidence_id": self.evidence_id,
            "kind": self.kind,
            "name": self.name,
            "qualified_name": self.qualified_name,
            "path": self.path,
            "span": {"start_line": self.start_line, "end_line": self.end_line},
            "signature": self.signature,
            "bases": list(self.bases),
            "calls": list(self.calls),
            "docstring": self.docstring,
        }
        if include_fingerprint:
            result["fingerprint"] = self.fingerprint
        return result

    @classmethod
    def from_data(cls, raw: Mapping[str, Any]) -> RepositoryEvidenceRecord:
        span = raw.get("span")
        if not isinstance(span, Mapping):
            raise ContractClosureError("repository evidence source span is malformed")
        return cls(
            evidence_id=_required_text(raw.get("evidence_id"), "evidence_id"),
            kind=_required_text(raw.get("kind"), "kind"),
            name=_required_text(raw.get("name"), "name"),
            qualified_name=_required_text(raw.get("qualified_name"), "qualified_name"),
            path=_required_text(raw.get("path"), "path"),
            start_line=int(span.get("start_line", 0)),
            end_line=int(span.get("end_line", 0)),
            signature=str(raw.get("signature", "")),
            bases=tuple(_text_values(raw.get("bases"))),
            calls=tuple(_text_values(raw.get("calls"))),
            docstring=str(raw.get("docstring", "")),
            fingerprint=str(raw.get("fingerprint", "")),
        )


@dataclass(frozen=True, slots=True)
class RepositoryEvidence:
    base_commit: str
    revision: str
    records: tuple[RepositoryEvidenceRecord, ...]

    @property
    def fingerprint(self) -> str:
        return _fingerprint(self.to_data(False))

    def to_data(self, include_fingerprint: bool = True) -> dict[str, Any]:
        result: dict[str, Any] = {
            "schema_version": "repository-contract-evidence-v1",
            "base_commit": self.base_commit,
            "revision": self.revision,
            "records": [record.to_data() for record in self.records],
        }
        if include_fingerprint:
            result["fingerprint"] = self.fingerprint
        return result

    @classmethod
    def from_data(cls, raw: Mapping[str, Any]) -> RepositoryEvidence:
        records = raw.get("records")
        if (
            raw.get("schema_version") != "repository-contract-evidence-v1"
            or not isinstance(records, list)
            or not all(isinstance(item, Mapping) for item in records)
        ):
            raise ContractClosureError("repository evidence artifact is malformed")
        evidence = cls(
            base_commit=_required_text(raw.get("base_commit"), "base_commit"),
            revision=_required_text(raw.get("revision"), "revision"),
            records=tuple(RepositoryEvidenceRecord.from_data(item) for item in records),
        )
        if raw.get("fingerprint") != evidence.fingerprint:
            raise ContractClosureError(
                "repository evidence artifact fingerprint is stale"
            )
        return evidence


def collect_python_evidence(
    repo_root: Path, *, base_commit: str, tracked_paths: Sequence[str]
) -> RepositoryEvidence:
    """Collect Python declarations only from paths tracked at the base commit."""
    records: list[RepositoryEvidenceRecord] = []
    for relative in sorted(set(tracked_paths)):
        candidate = Path(relative)
        if (
            candidate.is_absolute()
            or ".." in candidate.parts
            or candidate.suffix != ".py"
        ):
            continue
        path = repo_root / candidate
        if path.is_symlink() or not path.is_file():
            continue
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(path))
        except (OSError, SyntaxError, UnicodeError):
            continue
        module = relative.removesuffix(".py").replace("/", ".")
        if module.endswith(".__init__"):
            module = module.removesuffix(".__init__")
        records.extend(_records_for_module(tree, module, relative))
    records.sort(key=lambda item: (item.path, item.start_line, item.qualified_name))
    return RepositoryEvidence(
        base_commit=base_commit,
        revision=PYTHON_EVIDENCE_REVISION,
        records=tuple(records),
    )


def _records_for_module(
    tree: ast.Module, module: str, relative: str
) -> list[RepositoryEvidenceRecord]:
    result: list[RepositoryEvidenceRecord] = []

    def visit(node: ast.AST, parents: tuple[str, ...]) -> None:
        if isinstance(node, ast.ClassDef):
            qualified = ".".join((module, *parents, node.name))
            result.append(
                _record(
                    node,
                    relative,
                    "class",
                    node.name,
                    qualified,
                    signature="class " + node.name,
                    bases=tuple(_expr_name(base) for base in node.bases),
                    docstring=ast.get_docstring(node) or "",
                )
            )
            for child in node.body:
                visit(child, (*parents, node.name))
            return
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            qualified = ".".join((module, *parents, node.name))
            if parents:
                kind = "method"
            else:
                kind = "function"
            result.append(
                _record(
                    node,
                    relative,
                    kind,
                    node.name,
                    qualified,
                    signature=_signature(node),
                    calls=tuple(sorted(_called_names(node))),
                    docstring=ast.get_docstring(node) or "",
                )
            )
            # Nested functions are independently discoverable and are scoped by
            # their enclosing qualified declaration.
            for child in node.body:
                if isinstance(
                    child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
                ):
                    visit(child, (*parents, node.name))

    for item in tree.body:
        visit(item, ())
    return result


def _record(
    node: ast.AST,
    relative: str,
    kind: str,
    name: str,
    qualified_name: str,
    *,
    signature: str = "",
    bases: tuple[str, ...] = (),
    calls: tuple[str, ...] = (),
    docstring: str = "",
) -> RepositoryEvidenceRecord:
    end_line = getattr(node, "end_lineno", getattr(node, "lineno", 1))
    start_line = getattr(node, "lineno", 1)
    evidence_id = f"python:{relative}:{start_line}:{qualified_name}"
    return RepositoryEvidenceRecord(
        evidence_id=evidence_id,
        kind=kind,
        name=name,
        qualified_name=qualified_name,
        path=relative,
        start_line=start_line,
        end_line=end_line,
        signature=signature,
        bases=bases,
        calls=calls,
        docstring=docstring,
    )


def _signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    prefix = "async " if isinstance(node, ast.AsyncFunctionDef) else ""
    args = ast.unparse(node.args)
    returns = f" -> {ast.unparse(node.returns)}" if node.returns is not None else ""
    return f"{prefix}def {node.name}({args}){returns}"


def _expr_name(node: ast.expr) -> str:
    try:
        return ast.unparse(node)
    except Exception:  # pragma: no cover - defensive for exotic AST nodes
        return type(node).__name__


def _called_names(node: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    names: set[str] = set()
    for child in ast.walk(node):
        if (
            isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
            and child is not node
        ):
            continue
        if isinstance(child, ast.Call):
            names.add(_expr_name(child.func))
    return names


_DOTTED_REFERENCE = re.compile(r"(?<![\w])(?:[A-Za-z_]\w*\.)+[A-Za-z_]\w*")
_CALL_REFERENCE = re.compile(r"(?<![\w])(?:[A-Za-z_]\w*\.)*[A-Za-z_]\w*(?=\s*\()")


def compile_contract_closure(
    scenarios: Sequence[Mapping[str, Any]],
    evidence: RepositoryEvidence,
    *,
    design_revision: str,
) -> dict[str, Any]:
    """Bind each behavior scenario to relevant, source-cited repo evidence.

    Unmatched operation names are retained as new-surface candidates. That is
    expected when instructions introduce a new API and is not treated as a
    failed lookup.
    """
    if not design_revision.strip():
        raise ContractClosureError("design revision is required")
    evidence_by_id = {item.evidence_id: item for item in evidence.records}
    if len(evidence_by_id) != len(evidence.records):
        raise ContractClosureError("repository evidence IDs must be unique")
    operations: list[dict[str, Any]] = []
    scenario_ids: set[str] = set()
    for scenario in scenarios:
        scenario_id = _required_text(scenario.get("scenario_id"), "scenario_id")
        if scenario_id in scenario_ids:
            raise ContractClosureError(f"duplicate scenario ID: {scenario_id}")
        scenario_ids.add(scenario_id)
        subject = _required_text(scenario.get("subject"), f"{scenario_id}.subject")
        text = " ".join(
            str(scenario.get(key, ""))
            for key in ("subject", "given", "when", "then", "evidence")
        )
        references = set(_DOTTED_REFERENCE.findall(text)) | set(
            _CALL_REFERENCE.findall(text)
        )
        surfaces, surface_count = _candidate_surfaces(references, evidence.records)
        operations.append(
            {
                "operation_ref": scenario_id,
                "subject": subject,
                "scenario_refs": [scenario_id],
                "surfaces": [
                    {
                        "evidence_ref": record.evidence_id,
                        "kind": record.kind,
                        "qualified_name": record.qualified_name,
                        "path": record.path,
                        "span": {
                            "start_line": record.start_line,
                            "end_line": record.end_line,
                        },
                        "signature": record.signature,
                        "bases": list(record.bases),
                        "calls": list(record.calls),
                        "docstring": record.docstring,
                        "fingerprint": record.fingerprint,
                    }
                    for record in surfaces
                ],
                "surface_search": {
                    "explicit_references": sorted(references),
                    "result": "matched" if surfaces else "new_or_unmatched",
                    "candidate_count": surface_count,
                    "truncated": surface_count > len(surfaces),
                    "inventory_fingerprint": evidence.fingerprint,
                },
                "behavior": {
                    "given": scenario.get("given"),
                    "when": scenario.get("when"),
                    "then": scenario.get("then"),
                    "dimensions": dict(scenario.get("dimensions", {})),
                    "assumptions": [
                        dict(item)
                        for item in scenario.get("assumptions", [])
                        if isinstance(item, Mapping)
                    ],
                },
            }
        )
    record = {
        "schema_version": CONTRACT_CLOSURE_SCHEMA_VERSION,
        "design_revision": design_revision,
        "base_commit": evidence.base_commit,
        "repository_evidence_fingerprint": evidence.fingerprint,
        "compiler_revision": CONTRACT_CLOSURE_COMPILER_REVISION,
        "evidence_revision": evidence.revision,
        "analysis_limitations": [
            "Static call relationships are candidates and may miss dynamic dispatch, "
            "reflection, generated code, and runtime plugin registration.",
            "Unmatched source references may name new APIs or aliases; they require "
            "worker inspection rather than an unsupported-capability conclusion.",
        ],
        "operations": operations,
    }
    return {**record, "fingerprint": _fingerprint(record)}


def validate_contract_closure(
    raw: Mapping[str, Any], evidence: RepositoryEvidence
) -> dict[str, Any]:
    """Validate closure integrity and prove every scenario has a coverage row."""
    if raw.get("schema_version") != CONTRACT_CLOSURE_SCHEMA_VERSION:
        raise ContractClosureError("contract closure schema is unsupported")
    if raw.get("compiler_revision") != CONTRACT_CLOSURE_COMPILER_REVISION:
        raise ContractClosureError("contract closure compiler revision is unsupported")
    if raw.get("base_commit") != evidence.base_commit:
        raise ContractClosureError("contract closure base commit is stale")
    if raw.get("repository_evidence_fingerprint") != evidence.fingerprint:
        raise ContractClosureError("contract closure repository evidence is stale")
    operations = raw.get("operations")
    if not isinstance(operations, list) or not operations:
        raise ContractClosureError("contract closure requires at least one operation")
    seen: set[str] = set()
    known_evidence = {item.evidence_id: item for item in evidence.records}
    for operation in operations:
        if not isinstance(operation, Mapping):
            raise ContractClosureError("contract closure operation is malformed")
        operation_ref = _required_text(operation.get("operation_ref"), "operation_ref")
        if operation_ref in seen:
            raise ContractClosureError(f"duplicate closure operation: {operation_ref}")
        seen.add(operation_ref)
        if not _text_values(operation.get("scenario_refs")):
            raise ContractClosureError(f"{operation_ref} has no behavior scenario")
        if not isinstance(operation.get("behavior"), Mapping):
            raise ContractClosureError(f"{operation_ref} has no behavior contract")
        search = operation.get("surface_search")
        if (
            not isinstance(search, Mapping)
            or search.get("inventory_fingerprint") != evidence.fingerprint
        ):
            raise ContractClosureError(f"{operation_ref} has stale surface search")
        surfaces = operation.get("surfaces")
        if not isinstance(surfaces, list):
            raise ContractClosureError(f"{operation_ref} surfaces are malformed")
        for surface in surfaces:
            if not isinstance(surface, Mapping):
                raise ContractClosureError(
                    f"{operation_ref} cites unknown repository evidence"
                )
            record = known_evidence.get(str(surface.get("evidence_ref", "")))
            if record is None:
                raise ContractClosureError(
                    f"{operation_ref} cites unknown repository evidence"
                )
            expected_span = {
                "start_line": record.start_line,
                "end_line": record.end_line,
            }
            if (
                surface.get("kind") != record.kind
                or surface.get("qualified_name") != record.qualified_name
                or surface.get("path") != record.path
                or surface.get("span") != expected_span
                or surface.get("signature") != record.signature
                or surface.get("bases") != list(record.bases)
                or surface.get("calls") != list(record.calls)
                or surface.get("docstring") != record.docstring
                or surface.get("fingerprint") != record.fingerprint
            ):
                raise ContractClosureError(
                    f"{operation_ref} repository evidence does not match its source"
                )
    expected_fingerprint = _fingerprint(
        {key: value for key, value in raw.items() if key != "fingerprint"}
    )
    if raw.get("fingerprint") != expected_fingerprint:
        raise ContractClosureError("contract closure fingerprint is stale")
    return dict(raw)


def render_contract_closure(raw: Mapping[str, Any]) -> str:
    """Render a compact, evidence-attributed context section for the worker."""
    operations = raw.get("operations")
    if not isinstance(operations, list):
        raise ContractClosureError("contract closure operations are malformed")
    lines = ["Repository evidence relevant to the requested behavior:"]
    unmatched: list[str] = []
    for operation in operations:
        if not isinstance(operation, Mapping):
            continue
        surfaces = operation.get("surfaces", [])
        if not surfaces:
            subject = operation.get("subject")
            if isinstance(subject, str) and subject.strip():
                unmatched.append(subject.strip())
            continue
        lines.append(f"- {operation.get('subject', 'Behavior')}")
        for surface in surfaces:
            if not isinstance(surface, Mapping):
                continue
            span = surface.get("span", {})
            start_line = span.get("start_line") if isinstance(span, Mapping) else None
            location = f"{surface.get('path')}:{start_line}"
            detail = str(surface.get("signature") or surface.get("qualified_name"))
            lines.append(f"  - Existing {surface.get('kind')} {detail} at {location}.")
            if surface.get("docstring"):
                lines.append(f"    Existing contract: {surface['docstring']}")
            if surface.get("bases"):
                lines.append("    Declared bases: " + ", ".join(surface["bases"]) + ".")
            if surface.get("calls"):
                lines.append(
                    "    Statically visible calls: "
                    + ", ".join(surface["calls"][:12])
                    + "."
                )
        search = operation.get("surface_search")
        if isinstance(search, Mapping) and search.get("truncated") is True:
            lines.append(
                "  - The evidence list was bounded; inspect additional matching "
                "implementations in the repository."
            )
    if unmatched:
        lines.append(
            "- No matching declaration was found for: "
            + ", ".join(unmatched)
            + ". These may be new operations or aliases; inspect related code "
            "and tests."
        )
    lines.extend(
        (
            "Use this evidence to inspect the relevant implementation and tests. "
            "Static call lists can omit dynamic dispatch and are not proof that "
            "other paths do not exist. Preserve documented and tested behavior "
            "unless an accepted requirement changes it.",
        )
    )
    return "\n".join(lines)


def _candidate_surfaces(
    references: Iterable[str], records: Sequence[RepositoryEvidenceRecord]
) -> tuple[tuple[RepositoryEvidenceRecord, ...], int]:
    normalized = {item.removesuffix("()") for item in references}
    terminal_names = {item.rsplit(".", 1)[-1] for item in normalized}
    direct: list[RepositoryEvidenceRecord] = []
    callers: list[RepositoryEvidenceRecord] = []
    for record in records:
        if record.qualified_name in normalized or record.name in terminal_names:
            direct.append(record)
            continue
        if any(
            call in normalized or call.rsplit(".", 1)[-1] in terminal_names
            for call in record.calls
        ):
            callers.append(record)
    ordered = tuple((*direct, *callers))
    limit = 24
    return ordered[:limit], len(ordered)


def _required_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractClosureError(f"{label} must be non-empty text")
    return value.strip()


def _text_values(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,) if value.strip() else ()
    if isinstance(value, Sequence):
        return tuple(item for item in value if isinstance(item, str) and item.strip())
    return ()


__all__ = [
    "CONTRACT_CLOSURE_COMPILER_REVISION",
    "CONTRACT_CLOSURE_SCHEMA_VERSION",
    "ContractClosureError",
    "RepositoryEvidence",
    "RepositoryEvidenceRecord",
    "collect_python_evidence",
    "compile_contract_closure",
    "render_contract_closure",
    "validate_contract_closure",
]
