"""Token-preserving edits for Python Structrr source annotations."""

from __future__ import annotations

import ast
import hashlib
import io
import json
import os
import tempfile
import tokenize
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml

from powdrr_service.structrr.reference_definitions import ReferenceDefinition
from powdrr_service.structrr.reference_reconciliation import (
    LogicalSourceEdge,
    ReconciliationOperation,
    SourceOwnerIdentity,
    reconcile_source_references,
)
from powdrr_service.structrr.source_references import (
    SourceReferenceIndex,
    index_python_source,
)


def _text_and_encoding(source: bytes) -> tuple[str, str]:
    encoding, _ = tokenize.detect_encoding(io.BytesIO(source).readline)
    return source.decode(encoding), encoding


def _ast_fingerprint(source: bytes, path: str) -> str:
    text, _ = _text_and_encoding(source)
    tree = ast.parse(text, filename=path)
    return ast.dump(tree, annotate_fields=True, include_attributes=False)


def edit_source_annotations(
    *,
    path: str,
    source: bytes,
    ensure: Mapping[SourceOwnerIdentity, Sequence[str]] = {},
    remove: Mapping[SourceOwnerIdentity, Sequence[str]] = {},
    retained_edges: Mapping[SourceOwnerIdentity, Sequence[str]] = {},
) -> bytes:
    """Insert/remove exact IDs while preserving non-target bytes and Python AST."""
    if any(identity.path != path for identity in (*ensure.keys(), *remove.keys())):
        raise ValueError("annotation edit owner path does not match source path")
    index = index_python_source(path, source)
    if index.parse_status != "complete":
        raise ValueError("cannot edit annotations in a source file that does not parse")
    annotations = list(index.annotations)
    requested_remove = {(owner, ref) for owner, refs in remove.items() for ref in refs}
    keep = {(owner, ref) for owner, refs in retained_edges.items() for ref in refs}
    lines = source.splitlines(keepends=True)
    newline = b"\r\n" if b"\r\n" in source else b"\r" if b"\r" in source else b"\n"
    encoding, _ = tokenize.detect_encoding(io.BytesIO(source).readline)

    remove_lines: set[int] = set()
    for annotation in annotations:
        owner = annotation.owner
        if owner is None:
            continue
        identity = SourceOwnerIdentity(path, "python", owner.kind, owner.qualified_name)
        if (identity, annotation.ref) in requested_remove and (
            identity,
            annotation.ref,
        ) not in keep:
            remove_lines.add(annotation.line - 1)

    for line_no in sorted(remove_lines, reverse=True):
        del lines[line_no]

    # Re-index after removals so decorator and scope positions are current.
    updated = b"".join(lines)
    updated_index = index_python_source(path, updated)
    if updated_index.parse_status != "complete":
        raise ValueError("annotation removal produced source that does not parse")
    to_add: list[tuple[int, int, str, str]] = []
    for identity, refs in ensure.items():
        if identity.path != path:
            continue
        owners = [
            o
            for o in updated_index.owners
            if (o.kind, o.qualified_name) == (identity.kind, identity.qualified_name)
        ]
        if len(owners) != 1:
            raise ValueError(
                f"source owner {identity.qualified_name!r} does not resolve uniquely"
            )
        owner = owners[0]
        existing = {
            a.ref
            for a in updated_index.annotations
            if a.owner is not None
            and (a.owner.kind, a.owner.qualified_name)
            == (identity.kind, identity.qualified_name)
        }
        for ref in sorted(set(refs) - existing):
            # Scope annotations directly precede the declaration/decorators.
            scope = (
                "[module]"
                if identity.kind == "module"
                else "[block]"
                if identity.kind == "block"
                else ""
            )
            to_add.append((owner.start_line - 1, owner.column, scope, ref))
    for line_no, column, scope, ref in sorted(to_add, reverse=True):
        prefix = b" " * column
        marker = f"# structrr{scope}: {ref}".encode(encoding)
        lines = updated.splitlines(keepends=True)
        lines.insert(line_no, prefix + marker + newline)
        updated = b"".join(lines)
    if _ast_fingerprint(source, path) != _ast_fingerprint(updated, path):
        raise ValueError("source annotation edit changed the Python AST")
    return updated


def publish_staged_files(
    *,
    staged: Mapping[Path, bytes],
    expected_digests: Mapping[Path, str],
    receipt_path: Path,
) -> dict[str, object]:
    """Publish digest-checked files with per-file atomic replace and preimages."""
    if set(staged) != set(expected_digests):
        raise ValueError("staged files and expected digests must have identical paths")
    preimages: dict[str, bytes] = {}
    before: dict[str, str] = {}
    after: dict[str, str] = {}
    for path in sorted(staged):
        original = path.read_bytes()
        digest = "sha256:" + hashlib.sha256(original).hexdigest()
        if digest != expected_digests[path]:
            raise ValueError(f"stale reconciliation input: {path}")
        preimages[str(path)] = original
        before[str(path)] = digest
        after[str(path)] = "sha256:" + hashlib.sha256(staged[path]).hexdigest()
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    preimage_dir = Path(
        tempfile.mkdtemp(
            prefix=f"{receipt_path.stem}.preimages-", dir=receipt_path.parent
        )
    )
    receipt: dict[str, object] = {
        "schema_version": "structrr-link-reconciliation-receipt-v1",
        "status": "prepared",
        "before_digests": before,
        "after_digests": after,
        "preimage_directory": str(preimage_dir),
    }
    for number, (name, data) in enumerate(sorted(preimages.items())):
        (preimage_dir / f"{number:04d}.before").write_bytes(data)
        receipt.setdefault("preimages", {})
        assert isinstance(receipt["preimages"], dict)
        receipt["preimages"][name] = f"{number:04d}.before"
    _write_receipt(receipt_path, receipt)
    replaced: list[Path] = []
    try:
        for path in sorted(staged):
            fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(staged[path])
                    stream.flush()
                    os.fsync(stream.fileno())
                os.chmod(temporary, path.stat().st_mode)
                os.replace(temporary, path)
                replaced.append(path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
    except BaseException:
        receipt["status"] = "interrupted"
        receipt["replaced_paths"] = [str(path) for path in replaced]
        _write_receipt(receipt_path, receipt)
        raise
    receipt["status"] = "complete"
    receipt["replaced_paths"] = [str(path) for path in replaced]
    _write_receipt(receipt_path, receipt)
    return receipt


def _write_receipt(path: Path, receipt: Mapping[str, object]) -> None:
    encoded = json.dumps(receipt, sort_keys=True, ensure_ascii=False, indent=2).encode()
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def apply_reconciliation_operations(
    *,
    repo_root: Path,
    document_path: Path,
    operations: Sequence[ReconciliationOperation],
    source_indexes: Mapping[str, SourceReferenceIndex],
    definitions: Mapping[str, ReferenceDefinition],
    receipt_path: Path,
) -> dict[str, object]:
    """Stage reciprocal metadata corrections, validate digests, then publish."""
    root = repo_root.resolve()
    doc_path = document_path if document_path.is_absolute() else root / document_path
    doc_path = doc_path.resolve()
    if not doc_path.is_relative_to(root) or not doc_path.is_file():
        raise ValueError("Structrr document must be inside the repository")
    original_document = doc_path.read_bytes()
    document_digest = "sha256:" + hashlib.sha256(original_document).hexdigest()
    if any(op.expected_document_digest != document_digest for op in operations):
        raise ValueError(
            "Structrr document changed; regenerate the reconciliation plan"
        )
    document = yaml.safe_load(original_document)
    if not isinstance(document, Mapping):
        raise ValueError("Structrr document must contain a YAML mapping")

    operations_by_path: dict[str, list[ReconciliationOperation]] = {}
    for operation in operations:
        if operation.operation not in {
            "ensure_binding",
            "remove_binding",
            "ensure_annotation",
            "remove_annotation",
        }:
            raise ValueError(
                f"unsupported reconciliation operation {operation.operation!r}"
            )
        operations_by_path.setdefault(operation.edge.owner.path, []).append(operation)

    staged: dict[Path, bytes] = {}
    expected: dict[Path, str] = {doc_path: document_digest}
    current_indexes: dict[str, SourceReferenceIndex] = {}
    for relative, path_operations in sorted(operations_by_path.items()):
        source_path = (root / relative).resolve()
        if not source_path.is_relative_to(root) or source_path.is_symlink():
            raise ValueError("reconciliation source path must be a repository file")
        original = source_path.read_bytes()
        source_digest = "sha256:" + hashlib.sha256(original).hexdigest()
        if any(op.expected_source_digest != source_digest for op in path_operations):
            raise ValueError(
                f"source {relative!r} changed; regenerate the reconciliation plan"
            )
        current_index = index_python_source(relative, original)
        if current_index.parse_status != "complete":
            raise ValueError(f"source {relative!r} no longer parses")
        current_indexes[relative] = current_index
        ensure: dict[SourceOwnerIdentity, list[str]] = {}
        remove: dict[SourceOwnerIdentity, list[str]] = {}
        for op in path_operations:
            if op.operation == "ensure_annotation":
                ensure.setdefault(op.edge.owner, []).append(op.edge.reference_id)
            elif op.operation == "remove_annotation":
                remove.setdefault(op.edge.owner, []).append(op.edge.reference_id)
        if ensure or remove:
            retained: dict[SourceOwnerIdentity, list[str]] = {}
            for annotation in current_index.annotations:
                if annotation.owner is None:
                    continue
                identity = SourceOwnerIdentity(
                    relative,
                    "python",
                    annotation.owner.kind,
                    annotation.owner.qualified_name,
                )
                if annotation.ref not in remove.get(identity, ()):
                    retained.setdefault(identity, []).append(annotation.ref)
            for identity, refs in ensure.items():
                retained.setdefault(identity, []).extend(refs)
            changed = edit_source_annotations(
                path=relative,
                source=original,
                ensure=ensure,
                remove=remove,
                retained_edges=retained,
            )
            staged[source_path] = changed
            expected[source_path] = source_digest
            current_indexes[relative] = index_python_source(relative, changed)
        elif any(op.operation.endswith("binding") for op in path_operations):
            # Keep the source fingerprint in the transaction's final preflight
            # even when only the corresponding Structrr span is refreshed.
            staged[source_path] = original
            expected[source_path] = source_digest

    binding_operations = [op for op in operations if op.operation.endswith("binding")]
    if binding_operations:
        changed_document = _edit_binding_section(
            original_document, document, binding_operations, current_indexes
        )
        staged[doc_path] = changed_document
        try:
            updated_document = yaml.safe_load(changed_document)
        except yaml.YAMLError as error:
            raise ValueError("staged Structrr document is invalid") from error
        if not isinstance(updated_document, Mapping):
            raise ValueError("staged Structrr document is invalid")
        updated_bindings = updated_document.get("source_bindings", [])
        updated_subjects = updated_document.get("source_subjects", [])
        for operation in binding_operations:
            edge = operation.edge
            subject = next(
                item
                for item in updated_subjects
                if item.get("path") == edge.owner.path
                and item.get("kind") == edge.owner.kind
                and (
                    edge.owner.kind == "module"
                    or item.get("qualified_name") == edge.owner.qualified_name
                )
            )
            count = sum(
                1
                for item in updated_bindings
                if _binding_matches(item, edge, subject["id"])
            )
            expected_count = 1 if operation.operation == "ensure_binding" else 0
            if count != expected_count:
                raise ValueError(
                    "staged Structrr bindings failed reciprocal validation"
                )
    for operation in operations:
        if not operation.operation.endswith("annotation"):
            continue
        source_path = root / operation.edge.owner.path
        staged_source = staged.get(source_path, source_path.read_bytes())
        index = index_python_source(operation.edge.owner.path, staged_source)
        count = sum(
            1
            for annotation in index.annotations
            if annotation.ref == operation.edge.reference_id
            and annotation.owner is not None
            and annotation.owner.kind == operation.edge.owner.kind
            and annotation.owner.qualified_name == operation.edge.owner.qualified_name
        )
        expected_count = 1 if operation.operation == "ensure_annotation" else 0
        if count != expected_count:
            raise ValueError("staged source annotations failed reciprocal validation")
    staged_document = yaml.safe_load(staged.get(doc_path, original_document))
    staged_indexes = dict(source_indexes)
    for relative in operations_by_path:
        candidate = staged.get(root / relative)
        if candidate is not None:
            staged_indexes[relative] = index_python_source(relative, candidate)
    final_report = reconcile_source_references(
        document=staged_document,
        definitions=definitions,
        source_indexes=staged_indexes,
    )
    final_structrr = set(final_report.structrr_edges)
    final_source = set(final_report.source_edges)
    for edge in {operation.edge for operation in operations}:
        edge_operations = {op.operation for op in operations if op.edge == edge}
        if "ensure_binding" in edge_operations:
            if edge not in final_structrr or edge not in final_source:
                raise ValueError(
                    "staged reciprocal scan did not retain the accepted edge"
                )
        elif "remove_binding" in edge_operations:
            if edge in final_structrr or edge in final_source:
                raise ValueError("staged reciprocal scan retained a rejected edge")
    if not staged:
        return {"status": "complete", "before_digests": {}, "after_digests": {}}
    return publish_staged_files(
        staged=staged, expected_digests=expected, receipt_path=receipt_path
    )


def _edge_binding_relation(edge: LogicalSourceEdge) -> str:
    return "verified_by" if edge.relationship == "checks" else "implemented_by"


def _binding_matches(
    record: Mapping[str, Any], edge: LogicalSourceEdge, subject_id: str
) -> bool:
    expected_relationship = _edge_binding_relation(edge)
    return (
        record.get("entity_id") == edge.reference_id
        and record.get("subject_id") == subject_id
        and record.get("relationship") == expected_relationship
    )


def _edit_binding_section(
    raw_document: bytes,
    document: Mapping[str, Any],
    operations: Sequence[ReconciliationOperation],
    source_indexes: Mapping[str, SourceReferenceIndex],
) -> bytes:
    text, encoding = _text_and_encoding(raw_document)
    root_node = yaml.compose(text)
    if not isinstance(root_node, yaml.MappingNode):
        raise ValueError("Structrr document root must be a YAML mapping")
    key_nodes: dict[str, yaml.Node] = {}
    sequence_nodes: dict[str, yaml.Node] = {}
    for key, value in root_node.value:
        if isinstance(key, yaml.ScalarNode) and key.value in {
            "source_bindings",
            "source_subjects",
        }:
            key_nodes[key.value] = key
            sequence_nodes[key.value] = value
    binding_key_node = key_nodes.get("source_bindings")
    sequence_node = sequence_nodes.get("source_bindings")
    if sequence_node is not None and not isinstance(sequence_node, yaml.SequenceNode):
        raise ValueError("source_bindings must be a YAML sequence")
    subjects = document.get("source_subjects", [])
    bindings = document.get("source_bindings", [])
    if not isinstance(subjects, list) or not isinstance(bindings, list):
        raise ValueError("source_subjects and source_bindings must be lists")

    records_to_append: dict[LogicalSourceEdge, dict[str, Any]] = {}
    records_to_replace: dict[int, dict[str, Any]] = {}
    updated_subject_records: dict[int, dict[str, Any]] = {}
    remove_indexes: set[int] = set()
    subject_sequence = sequence_nodes.get("source_subjects")
    if subject_sequence is not None and (
        not isinstance(subject_sequence, yaml.SequenceNode)
        or len(subject_sequence.value) != len(subjects)
    ):
        raise ValueError("source_subjects YAML and data representations disagree")
    for subject_index, subject in enumerate(subjects):
        if (
            not isinstance(subject, Mapping)
            or subject.get("path") not in source_indexes
        ):
            continue
        path = subject["path"]
        source_index = source_indexes[path]
        kind, name = subject.get("kind"), subject.get("qualified_name")
        if not isinstance(kind, str) or not isinstance(name, str):
            continue
        owners = [
            owner
            for owner in source_index.owners
            if owner.kind == kind and (kind == "module" or owner.qualified_name == name)
        ]
        if len(owners) != 1:
            continue
        refreshed = dict(subject)
        refreshed["span"] = {
            "start_line": owners[0].start_line,
            "end_line": owners[0].end_line,
        }
        if refreshed != subject:
            updated_subject_records[subject_index] = refreshed
    for operation in operations:
        edge = operation.edge
        subject_matches = [
            item
            for item in subjects
            if isinstance(item, Mapping)
            and item.get("path") == edge.owner.path
            and item.get("kind") == edge.owner.kind
            and (
                edge.owner.kind == "module"
                or item.get("qualified_name") == edge.owner.qualified_name
            )
        ]
        if len(subject_matches) != 1 or not isinstance(
            subject_matches[0].get("id"), str
        ):
            raise ValueError("Structrr source subject does not resolve uniquely")
        subject_id = subject_matches[0]["id"]
        matching = [
            binding_index
            for binding_index, item in enumerate(bindings)
            if isinstance(item, Mapping) and _binding_matches(item, edge, subject_id)
        ]
        if operation.operation == "remove_binding":
            remove_indexes.update(matching)
            continue
        if operation.operation != "ensure_binding":
            continue
        current_source_index = source_indexes.get(edge.owner.path)
        if (
            current_source_index is None
            or current_source_index.parse_status != "complete"
        ):
            raise ValueError(
                "current source owner index is required for binding updates"
            )
        owners = [
            item
            for item in current_source_index.owners
            if item.kind == edge.owner.kind
            and (
                edge.owner.kind == "module"
                or item.qualified_name == edge.owner.qualified_name
            )
        ]
        if len(owners) != 1:
            raise ValueError("source owner no longer resolves uniquely")
        owner = owners[0]
        binding_id = (
            "binding:reconciled-"
            + hashlib.sha256(
                json.dumps(edge.to_data(), sort_keys=True).encode()
            ).hexdigest()[:20]
        )
        record = {
            "id": binding_id,
            "subject_id": subject_id,
            "entity_id": edge.reference_id,
            "relationship": _edge_binding_relation(edge),
            "path": edge.owner.path,
            "span": {"start_line": owner.start_line, "end_line": owner.end_line},
            "confidence": "high",
            "evidence": "accepted source-link existence decision",
        }
        if matching:
            records_to_replace[matching[0]] = record
            remove_indexes.update(matching[1:])
        else:
            records_to_append[edge] = record

    edits: list[tuple[int, int, str]] = []
    if subject_sequence is not None and isinstance(subject_sequence, yaml.SequenceNode):
        for index, record in updated_subject_records.items():
            node = subject_sequence.value[index]
            start = text.rfind("\n", 0, node.start_mark.index) + 1
            end = node.end_mark.index
            edits.append(
                (
                    start,
                    end,
                    yaml.safe_dump([record], sort_keys=False, allow_unicode=True),
                )
            )
    if sequence_node is not None:
        if not isinstance(sequence_node, yaml.SequenceNode) or len(
            sequence_node.value
        ) != len(bindings):
            raise ValueError("source_bindings YAML and data representations disagree")
        removes_all = (
            bool(bindings)
            and len(remove_indexes) == len(bindings)
            and not records_to_replace
            and not records_to_append
        )
        if removes_all:
            if binding_key_node is None:
                raise ValueError("source_bindings key is missing")
            start = text.rfind("\n", 0, binding_key_node.start_mark.index) + 1
            end = sequence_node.end_mark.index
            edits.append((start, end, "source_bindings: []"))
        else:
            for index in remove_indexes | set(records_to_replace):
                node = sequence_node.value[index]
                start = text.rfind("\n", 0, node.start_mark.index) + 1
                end = node.end_mark.index
                replacement = (
                    yaml.safe_dump(
                        [records_to_replace[index]], sort_keys=False, allow_unicode=True
                    )
                    if index in records_to_replace
                    else ""
                )
                edits.append((start, end, replacement))
        if records_to_append:
            rendered = "".join(
                yaml.safe_dump(
                    [records_to_append[edge]], sort_keys=False, allow_unicode=True
                )
                for edge in sorted(records_to_append)
            )
            if not sequence_node.value:
                if binding_key_node is None:
                    raise ValueError("source_bindings key is missing")
                start = text.rfind("\n", 0, binding_key_node.start_mark.index) + 1
                end = text.find("\n", binding_key_node.end_mark.index)
                end = len(text) if end < 0 else end + 1
                edits.append(
                    (
                        start,
                        end,
                        "source_bindings:\n" + rendered,
                    )
                )
            else:
                index = sequence_node.end_mark.index
                edits.append((index, index, "\n" + rendered))
    elif records_to_append:
        rendered = "".join(
            yaml.safe_dump(
                [records_to_append[edge]], sort_keys=False, allow_unicode=True
            )
            for edge in sorted(records_to_append)
        )
        edits.append((len(text), len(text), "\nsource_bindings:\n" + rendered))
    for start, end, replacement in sorted(
        edits, key=lambda edit: edit[0], reverse=True
    ):
        text = text[:start] + replacement + text[end:]
    return text.encode(encoding)


__all__ = [
    "apply_reconciliation_operations",
    "edit_source_annotations",
    "publish_staged_files",
]
