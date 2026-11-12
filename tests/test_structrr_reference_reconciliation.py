from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from powdrr_service.planning.semantic_decision import (
    DECISION_VALUES,
    SemanticDecisionProvider,
)
from powdrr_service.structrr.reference_definitions import ReferenceDefinition
from powdrr_service.structrr.reference_editing import (
    apply_reconciliation_operations,
    edit_source_annotations,
    publish_staged_files,
)
from powdrr_service.structrr.reference_reconciliation import (
    LogicalSourceEdge,
    ReconciliationOperation,
    SourceOwnerIdentity,
    compile_reconciliation_operations,
    prepare_source_link_decision,
    reconcile_source_references,
    run_source_link_decision,
)
from powdrr_service.structrr.source_references import index_python_source


def _definition() -> ReferenceDefinition:
    return ReferenceDefinition(
        id="feature:retry",
        kind="feature",
        statement="Retry safely.",
        source_ref="design.md#retry",
    )


def _document(*, binding: bool = True) -> dict[str, Any]:
    document: dict[str, Any] = {
        "source_subjects": [
            {
                "id": "subject:retry",
                "path": "src/retry.py",
                "kind": "function",
                "qualified_name": "retry",
                "span": {"start_line": 2, "end_line": 3},
            }
        ],
        "source_bindings": [],
    }
    if binding:
        document["source_bindings"] = [
            {
                "id": "binding:retry",
                "subject_id": "subject:retry",
                "entity_id": "feature:retry",
                "relationship": "implemented_by",
                "path": "src/retry.py",
                "span": {"start_line": 2, "end_line": 3},
            }
        ]
    return document


def test_reciprocal_edges_are_normalized_and_compared() -> None:
    source = b"# structrr: feature:retry\ndef retry():\n    return True\n"
    report = reconcile_source_references(
        document=_document(),
        definitions={"feature:retry": _definition()},
        source_indexes={"src/retry.py": index_python_source("src/retry.py", source)},
    )
    assert report.consistent
    assert report.structrr_edges == report.source_edges


def test_both_mismatch_directions_are_reported_without_inference() -> None:
    source = b"def retry():\n    return True\n"
    missing_comment = reconcile_source_references(
        document=_document(),
        definitions={"feature:retry": _definition()},
        source_indexes={"src/retry.py": index_python_source("src/retry.py", source)},
    )
    assert len(missing_comment.structrr_only) == 1
    source_only = reconcile_source_references(
        document=_document(binding=False),
        definitions={"feature:retry": _definition()},
        source_indexes={
            "src/retry.py": index_python_source(
                "src/retry.py",
                b"# structrr: feature:retry\ndef retry():\n    return True\n",
            )
        },
    )
    assert len(source_only.source_only) == 1


def test_reciprocal_presence_can_be_reassessed_as_semantically_absent() -> None:
    source = b"# structrr: feature:retry\ndef retry():\n    return True\n"
    report = reconcile_source_references(
        document=_document(),
        definitions={"feature:retry": _definition()},
        source_indexes={"src/retry.py": index_python_source("src/retry.py", source)},
    )
    assert report.consistent
    edge = report.structrr_edges[0]
    spec = prepare_source_link_decision(
        edge=edge,
        definition=_definition(),
        source_text=source.decode(),
        source_fingerprint=report.source_digests[edge.owner.path],
        observed_representations={"structrr": True, "source": True},
        context_text="independent semantic reassessment",
    )
    decision = spec.bind(
        provider=SemanticDecisionProvider("human"),
        provider_result={"status": "resolved", "value": "absent"},
        evidence_refs=["design.md#retry"],
        created_at="2026-11-11T00:00:00Z",
    )
    operations = compile_reconciliation_operations(
        report=report, specifications={edge: spec}, decisions={edge: decision}
    )
    assert {operation.operation for operation in operations} == {
        "remove_binding",
        "remove_annotation",
    }


def test_source_link_decision_kind_is_narrow_and_fingerprinted() -> None:
    definition = _definition()
    edge = LogicalSourceEdge(
        "feature:retry",
        SourceOwnerIdentity("src/retry.py", "python", "function", "retry"),
        "implemented_by",
    )
    spec = prepare_source_link_decision(
        edge=edge,
        definition=definition,
        source_text="def retry(): pass\n",
        source_fingerprint="sha256:" + "a" * 64,
        observed_representations={"structrr": True, "source": False},
        context_text="caller and test excerpts",
    )
    assert spec.decision_kind == "source_link_existence"
    assert DECISION_VALUES[spec.decision_kind] == {"exists", "absent"}
    assert spec.to_data()["input_fingerprint"].startswith("sha256:")
    assert SemanticDecisionProvider("human").kind == "human"


def test_annotation_edit_preserves_python_ast_and_unrelated_comments() -> None:
    source = b"# keep this comment\n@decorate\ndef retry():\n    return True\n"
    owner = SourceOwnerIdentity("src/retry.py", "python", "function", "retry")
    edited = edit_source_annotations(
        path="src/retry.py", source=source, ensure={owner: ["feature:retry"]}
    )
    assert edited == (
        b"# keep this comment\n"
        b"# structrr: feature:retry\n"
        b"@decorate\n"
        b"def retry():\n"
        b"    return True\n"
    )
    removed = edit_source_annotations(
        path="src/retry.py", source=edited, remove={owner: ["feature:retry"]}
    )
    assert removed == source


def test_module_and_block_annotations_use_their_scoped_grammar() -> None:
    module = SourceOwnerIdentity("src/retry.py", "python", "module", "src/retry.py")
    module_source = edit_source_annotations(
        path="src/retry.py",
        source=b'"""Module docs."""\n\nVALUE = 1\n',
        ensure={module: ["feature:retry"]},
    )
    assert module_source.startswith(b"# structrr[module]: feature:retry\n")
    block = SourceOwnerIdentity("src/retry.py", "python", "block", "retry::<Return>")
    block_source = edit_source_annotations(
        path="src/retry.py",
        source=b"def retry():\n    return True\n",
        ensure={block: ["feature:retry"]},
    )
    assert b"    # structrr[block]: feature:retry\n" in block_source


def test_decision_compiles_only_missing_reciprocal_operations() -> None:
    source = b"def retry():\n    return True\n"
    document = _document()
    document["source_bindings"][0]["span"] = {"start_line": 1, "end_line": 2}
    report = reconcile_source_references(
        document=document,
        definitions={"feature:retry": _definition()},
        source_indexes={"src/retry.py": index_python_source("src/retry.py", source)},
    )
    edge = report.structrr_only[0]
    spec = prepare_source_link_decision(
        edge=edge,
        definition=_definition(),
        source_text=source.decode(),
        source_fingerprint=report.source_digests[edge.owner.path],
        observed_representations={"structrr": True, "source": False},
        context_text="accepted feature and current source",
    )
    decision = spec.bind(
        provider=SemanticDecisionProvider("human"),
        provider_result={"status": "resolved", "value": "exists"},
        evidence_refs=["design.md#retry"],
        created_at="2026-11-11T00:00:00Z",
    )
    operations = compile_reconciliation_operations(
        report=report, specifications={edge: spec}, decisions={edge: decision}
    )
    assert [operation.operation for operation in operations] == [
        "ensure_annotation",
        "ensure_binding",
    ]


def test_absent_decision_does_not_remove_a_shared_retained_annotation() -> None:
    source = b"# structrr: feature:retry\ndef retry():\n    return True\n"
    owner = SourceOwnerIdentity("src/retry.py", "python", "function", "retry")
    result = edit_source_annotations(
        path="src/retry.py",
        source=source,
        remove={owner: ["feature:retry"]},
        retained_edges={owner: ["feature:retry"]},
    )
    assert result == source


def test_absent_source_only_edge_compiles_only_source_removal() -> None:
    source = b"# structrr: feature:retry\ndef retry():\n    return True\n"
    report = reconcile_source_references(
        document=_document(binding=False),
        definitions={"feature:retry": _definition()},
        source_indexes={"src/retry.py": index_python_source("src/retry.py", source)},
    )
    edge = report.source_only[0]
    spec = prepare_source_link_decision(
        edge=edge,
        definition=_definition(),
        source_text=source.decode(),
        source_fingerprint=report.source_digests[edge.owner.path],
        observed_representations={"structrr": False, "source": True},
        context_text="accepted feature and current source",
    )
    decision = spec.bind(
        provider=SemanticDecisionProvider("human"),
        provider_result={"status": "resolved", "value": "absent"},
        evidence_refs=["design.md#retry"],
        created_at="2026-11-11T00:00:00Z",
    )
    operations = compile_reconciliation_operations(
        report=report, specifications={edge: spec}, decisions={edge: decision}
    )
    assert [item.operation for item in operations] == ["remove_annotation"]


def test_primary_llm_gets_one_fallback_with_distinct_fingerprint() -> None:
    definition = _definition()
    edge = LogicalSourceEdge(
        "feature:retry",
        SourceOwnerIdentity("src/retry.py", "python", "function", "retry"),
        "implements",
    )
    spec = prepare_source_link_decision(
        edge=edge,
        definition=definition,
        source_text="def retry(): pass\n",
        source_fingerprint="sha256:" + "b" * 64,
        observed_representations={"structrr": True, "source": False},
        context_text="tests show retry behavior",
    )
    calls: list[str] = []

    def unresolved(current: Any) -> dict[str, str]:
        calls.append(current.decision_id)
        return {"status": "unresolved", "reason_code": "conflicting_evidence"}

    def exists(current: Any) -> dict[str, str]:
        calls.append(current.decision_id)
        return {"status": "resolved", "value": "exists"}

    result = run_source_link_decision(
        specification=spec,
        primary=(SemanticDecisionProvider("planning-llm", "primary", "m1"), unresolved),
        fallback=(SemanticDecisionProvider("planning-llm", "fallback", "m2"), exists),
        evidence_refs=["design.md#retry", "src/retry.py:1-2"],
        created_at="2026-11-11T00:00:00Z",
    )
    assert calls == [spec.decision_id, spec.decision_id + ":fallback"]
    assert result.final_decision.result.value == "exists"
    assert len(result.attempts) == 2
    assert type(result).from_data(result.to_data()) == result
    assert (
        result.attempts[0].decision.input_fingerprint
        != result.final_decision.input_fingerprint
    )
    calls_before_reuse = len(calls)
    reused = run_source_link_decision(
        specification=spec,
        primary=(SemanticDecisionProvider("planning-llm", "primary", "m1"), unresolved),
        fallback=(SemanticDecisionProvider("planning-llm", "fallback", "m2"), exists),
        evidence_refs=["design.md#retry", "src/retry.py:1-2"],
        created_at="2026-11-11T00:00:00Z",
        cached_run=result,
    )
    assert reused == result
    assert len(calls) == calls_before_reuse

    absent = run_source_link_decision(
        specification=spec,
        primary=(SemanticDecisionProvider("planning-llm", "primary", "m1"), unresolved),
        fallback=(
            SemanticDecisionProvider("planning-llm", "fallback", "m2"),
            lambda _current: {"status": "resolved", "value": "absent"},
        ),
        evidence_refs=["design.md#retry", "src/retry.py:1-2"],
        created_at="2026-11-11T00:00:00Z",
    )
    assert absent.final_decision.result.value == "absent"

    unresolved_twice = run_source_link_decision(
        specification=spec,
        primary=(SemanticDecisionProvider("planning-llm", "primary", "m1"), unresolved),
        fallback=(
            SemanticDecisionProvider("planning-llm", "fallback", "m2"),
            unresolved,
        ),
        evidence_refs=["design.md#retry", "src/retry.py:1-2"],
        created_at="2026-11-11T00:00:00Z",
    )
    assert unresolved_twice.final_decision.result.status == "unresolved"
    assert len(unresolved_twice.attempts) == 2

    def unavailable(_current: Any) -> dict[str, str]:
        raise RuntimeError("provider unavailable")

    failed_fallback = run_source_link_decision(
        specification=spec,
        primary=(SemanticDecisionProvider("planning-llm", "primary", "m1"), unresolved),
        fallback=(
            SemanticDecisionProvider("planning-llm", "fallback", "m2"),
            unavailable,
        ),
        evidence_refs=["design.md#retry", "src/retry.py:1-2"],
        created_at="2026-11-11T00:00:00Z",
    )
    assert failed_fallback.final_decision.result.status == "unresolved"
    assert failed_fallback.attempts[-1].failure == "RuntimeError: provider unavailable"


def test_apply_updates_yaml_and_python_as_one_checked_transaction(
    tmp_path: Path,
) -> None:
    root = tmp_path
    source_path = root / "src/retry.py"
    source_path.parent.mkdir()
    source = b"def retry():\n    return True\n"
    source_path.write_bytes(source)
    document_path = root / "structrr.yaml"
    document = {
        "source_subjects": [
            {
                "id": "subject:retry",
                "path": "src/retry.py",
                "kind": "function",
                "qualified_name": "retry",
                "span": {"start_line": 1, "end_line": 2},
            }
        ],
        "source_bindings": [],
    }
    raw_document = yaml.safe_dump(document, sort_keys=False).encode()
    document_path.write_bytes(raw_document)
    index = index_python_source("src/retry.py", source)
    edge = LogicalSourceEdge(
        "feature:retry",
        SourceOwnerIdentity("src/retry.py", "python", "function", "retry"),
        "implements",
    )
    operations = (
        ReconciliationOperation(
            "ensure_binding",
            edge,
            "sha256:" + hashlib.sha256(raw_document).hexdigest(),
            index.source_sha256,
        ),
        ReconciliationOperation(
            "ensure_annotation",
            edge,
            "sha256:" + hashlib.sha256(raw_document).hexdigest(),
            index.source_sha256,
        ),
    )
    receipt = apply_reconciliation_operations(
        repo_root=root,
        document_path=document_path,
        operations=operations,
        source_indexes={"src/retry.py": index},
        definitions={"feature:retry": _definition()},
        receipt_path=root / "plan.receipt.json",
    )
    updated = yaml.safe_load(document_path.read_bytes())
    assert len(updated["source_bindings"]) == 1
    assert updated["source_bindings"][0]["span"] == {
        "start_line": 2,
        "end_line": 3,
    }
    assert updated["source_subjects"][0]["span"] == {
        "start_line": 2,
        "end_line": 3,
    }
    assert source_path.read_bytes().startswith(b"# structrr: feature:retry\n")
    assert receipt["status"] == "complete"
    updated_document_bytes = document_path.read_bytes()
    updated_source = source_path.read_bytes()
    updated_index = index_python_source("src/retry.py", updated_source)
    document_digest = "sha256:" + hashlib.sha256(updated_document_bytes).hexdigest()
    removal_receipt = apply_reconciliation_operations(
        repo_root=root,
        document_path=document_path,
        operations=(
            ReconciliationOperation(
                "remove_binding", edge, document_digest, updated_index.source_sha256
            ),
            ReconciliationOperation(
                "remove_annotation", edge, document_digest, updated_index.source_sha256
            ),
        ),
        source_indexes={"src/retry.py": updated_index},
        definitions={"feature:retry": _definition()},
        receipt_path=root / "absent.receipt.json",
    )
    assert yaml.safe_load(document_path.read_bytes())["source_bindings"] == []
    assert source_path.read_bytes() == source
    assert removal_receipt["status"] == "complete"


def test_interrupted_publish_retains_preimages_and_never_reports_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from powdrr_service.structrr import reference_editing

    first = tmp_path / "a.py"
    second = tmp_path / "b.py"
    first.write_bytes(b"before-a")
    second.write_bytes(b"before-b")
    originals = {first: first.read_bytes(), second: second.read_bytes()}
    staged = {first: b"after-a", second: b"after-b"}
    expected = {
        path: "sha256:" + hashlib.sha256(content).hexdigest()
        for path, content in originals.items()
    }
    replace = reference_editing.os.replace

    def fail_on_second(source: str, destination: str | Path) -> None:
        if Path(destination) == second:
            raise OSError("simulated interruption")
        replace(source, destination)

    monkeypatch.setattr(reference_editing.os, "replace", fail_on_second)
    receipt_path = tmp_path / "transaction.receipt.json"
    with pytest.raises(OSError, match="simulated interruption"):
        publish_staged_files(
            staged=staged,
            expected_digests=expected,
            receipt_path=receipt_path,
        )
    receipt = json.loads(receipt_path.read_text())
    assert receipt["status"] == "interrupted"
    assert first.read_bytes() == b"after-a"
    assert second.read_bytes() == b"before-b"
    assert all(
        (Path(receipt["preimage_directory"]) / name).is_file()
        for name in receipt["preimages"].values()
    )
