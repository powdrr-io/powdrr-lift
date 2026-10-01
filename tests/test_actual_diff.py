from __future__ import annotations

from pathlib import Path
from typing import Any

from powdrr_lift.structrr.actual_diff import compile_actual_structrr_diff
from powdrr_lift.structrr.candidate_comparison import compare_candidate_snapshot
from powdrr_lift.structrr.proposal import compile_proposal_revision
from powdrr_lift.structrr.source_manifest import (
    SourceManifest,
    compile_source_manifest,
)


def _manifest(
    root: Path, *, revision: str, evidence_files: tuple[str, ...]
) -> SourceManifest:
    return compile_source_manifest(
        root,
        evidence_files,
        submission_base="submission-base",
        source_revision=revision,
        taxonomy_fingerprint="sha256:taxonomy",
    )


def test_actual_diff_separates_structural_operations_from_body_observations(
    tmp_path: Path,
) -> None:
    before_root = tmp_path / "before"
    after_root = tmp_path / "after"
    before_root.mkdir()
    after_root.mkdir()
    (before_root / "module.py").write_text("def run(): return 1\n", encoding="utf-8")
    (after_root / "module.py").write_text("def run(): return 2\n", encoding="utf-8")
    (after_root / "helper.py").write_text("def helper(): pass\n", encoding="utf-8")

    baseline: dict[str, Any] = {
        "entities": [],
        "entity_relationships": [],
        "source_subjects": [],
        "source_bindings": [],
    }
    candidate = {
        **baseline,
        "entities": [{"id": "worker", "type": "Component"}],
    }
    actual = compile_actual_structrr_diff(
        baseline,
        candidate,
        _manifest(before_root, revision="base", evidence_files=("module.py",)),
        _manifest(
            after_root,
            revision="candidate",
            evidence_files=("module.py", "helper.py"),
        ),
    )

    data = actual.to_data()
    assert actual.extraction_complete is True
    assert actual.structural_operations[0]["action"] == "add"
    assert actual.structural_operations[0]["subject_id"] == "worker"
    assert {item["path"] for item in actual.source_observations} == {
        "module.py",
        "helper.py",
    }
    assert actual.behavioral_review_candidates == ("helper.py", "module.py")
    assert actual.fingerprint == data["fingerprint"]


def test_actual_diff_keeps_specification_edits_out_of_code_observations(
    tmp_path: Path,
) -> None:
    before_root = tmp_path / "before"
    after_root = tmp_path / "after"
    for root in (before_root, after_root):
        (root / "docs/current/product").mkdir(parents=True)
    (before_root / "docs/current/product/architecture-specification.yaml").write_text(
        "version: 1\n", encoding="utf-8"
    )
    (after_root / "docs/current/product/architecture-specification.yaml").write_text(
        "version: 2\n", encoding="utf-8"
    )
    snapshot: dict[str, Any] = {
        "entities": [],
        "entity_relationships": [],
        "source_subjects": [],
        "source_bindings": [],
    }

    actual = compile_actual_structrr_diff(
        snapshot,
        snapshot,
        _manifest(
            before_root,
            revision="base",
            evidence_files=("docs/current/product/architecture-specification.yaml",),
        ),
        _manifest(
            after_root,
            revision="candidate",
            evidence_files=("docs/current/product/architecture-specification.yaml",),
        ),
    )

    assert actual.source_observations == ()
    assert actual.behavioral_review_candidates == ()
    assert actual.declaration_changes[0]["evidence_kind"] == "declaration"


def test_actual_diff_marks_manifest_read_failures_incomplete(tmp_path: Path) -> None:
    before_root = tmp_path / "before"
    after_root = tmp_path / "after"
    before_root.mkdir()
    after_root.mkdir()
    snapshot: dict[str, Any] = {
        "entities": [],
        "entity_relationships": [],
        "source_subjects": [],
        "source_bindings": [],
    }

    actual = compile_actual_structrr_diff(
        snapshot,
        snapshot,
        _manifest(before_root, revision="base", evidence_files=("missing.py",)),
        _manifest(after_root, revision="candidate", evidence_files=("missing.py",)),
    )

    assert actual.extraction_complete is False
    assert len(actual.unknowns) == 2


def test_candidate_comparison_binds_actual_diff_and_reports_body_review_candidates(
    tmp_path: Path,
) -> None:
    before_root = tmp_path / "before"
    after_root = tmp_path / "after"
    before_root.mkdir()
    after_root.mkdir()
    (before_root / "module.py").write_text("def run(): return 1\n", encoding="utf-8")
    (after_root / "module.py").write_text("def run(): return 2\n", encoding="utf-8")
    snapshot: dict[str, Any] = {
        "entities": [],
        "entity_relationships": [],
        "source_subjects": [],
        "source_bindings": [],
    }
    proposal = compile_proposal_revision(
        "body-change",
        snapshot,
        {},
        acceptance_criteria=("run returns the new value",),
        must_preserve=(),
        non_goals=(),
        allowed_paths=("src",),
        source_refs=("instruction:1",),
    )
    actual = compile_actual_structrr_diff(
        snapshot,
        snapshot,
        _manifest(before_root, revision="base", evidence_files=("module.py",)),
        _manifest(after_root, revision="candidate", evidence_files=("module.py",)),
    )

    report = compare_candidate_snapshot(
        proposal,
        snapshot,
        snapshot,
        extraction_complete=True,
        actual_diff=actual,
    )

    assert report["passed"] is True
    assert report["actual_diff_fingerprint"] == actual.fingerprint
    assert report["candidate_product_digest"] == actual.candidate_product_digest
    assert report["behavioral_review_candidates"] == ["module.py"]
