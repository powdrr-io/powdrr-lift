from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from powdrr_service.structrr.design import (
    DesignHandoff,
    DesiredState,
    project_sections,
)
from powdrr_service.structrr.design_agreement import check_design_agreement
from powdrr_service.structrr.proposal import compile_proposal_revision
from powdrr_service.structrr.source_manifest import (
    SourceManifest,
    compile_source_manifest,
)

from powdrr_lift.cli import main
from powdrr_lift.workrr.coding_agent import ImplementationRequest


def _handoff(
    baseline: dict[str, Any], plan: dict[str, Any], **kwargs: Any
) -> DesignHandoff:
    proposal = compile_proposal_revision(
        "retry-design",
        baseline,
        plan,
        acceptance_criteria=(),
        must_preserve=(),
        non_goals=(),
        allowed_paths=("retry.py",),
        source_refs=("incident:duplicate-processing",),
        **kwargs,
    )
    return DesignHandoff.compile(baseline, proposal, base_commit="base")


def _manifest(root: Path) -> SourceManifest:
    return compile_source_manifest(
        root,
        ("retry.py",),
        submission_base="base",
        source_revision="candidate",
        taxonomy_fingerprint="taxonomy",
    )


def _architecture_handoff() -> DesignHandoff:
    return _handoff(
        {},
        {
            "invariants": [
                {
                    "id": "retry-boundary",
                    "action": "added",
                    "description": (
                        "Retry dispatch exists and has no persistence dependency."
                    ),
                    "rationale": "Downstream deduplication owns transaction storage.",
                    "verification": [
                        {
                            "kind": "python-symbol",
                            "path": "retry.py",
                            "symbol": "dispatch",
                        },
                        {
                            "kind": "python-import",
                            "path": "retry.py",
                            "module": "persistence",
                            "present": False,
                        },
                    ],
                }
            ]
        },
    )


def test_complete_design_transition_preserves_why_and_leaves_inputs_unchanged() -> None:
    baseline = {
        "invariants": [
            {
                "id": "identity",
                "description": "One ID per attempt.",
                "rationale": "Historical design.",
            }
        ],
        "decisions": [{"id": "old", "summary": "Create IDs in transport."}],
    }
    handoff = _handoff(
        baseline,
        {
            "invariants": [
                {
                    "id": "identity",
                    "action": "altered",
                    "description": "One ID per logical transaction.",
                    "rationale": "Deduplication depends on stable identity.",
                }
            ],
            "decisions": [
                {"id": "old", "action": "removed"},
                {
                    "id": "new",
                    "action": "added",
                    "supersedes": "old",
                    "summary": "Create the ID before retries.",
                    "rationale": "Retries must preserve it.",
                },
            ],
        },
    )
    document = handoff.to_data()["desired"]["document"]
    assert (
        document["invariants"][0]["rationale"]
        == "Deduplication depends on stable identity."
    )
    assert document["decisions"][0]["supersedes"] == "old"
    assert baseline["invariants"][0]["description"] == "One ID per attempt."
    assert "action" not in document["invariants"][0]
    assert DesignHandoff.from_data(json.loads(json.dumps(handoff.to_data()))) == handoff


def test_rationale_changes_design_identity_but_observations_do_not() -> None:
    first = {
        "entities": [
            {"id": "retry", "type": "Component", "rationale": "Bound retries."}
        ]
    }
    state = DesiredState.from_snapshot(first)
    assert (
        DesiredState.from_snapshot({**first, "source_manifest": {"digest": "new"}})
        == state
    )
    changed = DesiredState.from_snapshot(
        {"entities": [{**first["entities"][0], "rationale": "Preserve IDs."}]}
    )
    assert changed.fingerprint != state.fingerprint
    copy = state.document
    copy["entities"][0]["rationale"] = "Mutated"
    assert state.document["entities"][0]["rationale"] == "Bound retries."


@pytest.mark.parametrize("mutate", ["desired", "proposal", "base_commit", "baseline"])
def test_handoff_rejects_tampering(mutate: str) -> None:
    data = _architecture_handoff().to_data()
    if mutate == "base_commit":
        data[mutate] = "other"
    elif mutate == "baseline":
        data[mutate] = {"entities": [{"id": "unexpected"}]}
    else:
        data[mutate]["fingerprint"] = "tampered"
    with pytest.raises(ValueError):
        DesignHandoff.from_data(data)


@pytest.mark.parametrize("action", ["changed", "removed"])
def test_missing_subject_cannot_be_silently_changed_or_removed(action: str) -> None:
    with pytest.raises(ValueError, match="absent"):
        project_sections({}, {"invariants": [{"id": "missing", "action": action}]})


def test_transition_rejects_duplicates_and_unknown_operations() -> None:
    with pytest.raises(ValueError, match="duplicate operation"):
        project_sections(
            {},
            {
                "invariants": [
                    {"id": "same", "action": "added"},
                    {"id": "same", "action": "added"},
                ]
            },
        )
    with pytest.raises(ValueError, match="unsupported Structrr action"):
        _handoff({}, {"features": [{"id": "lost", "action": "typo"}]})


def test_serialized_desired_state_is_the_next_handoff_baseline() -> None:
    first = _architecture_handoff().to_data()["desired"]
    second = _handoff(
        first,
        {
            "invariants": [
                {
                    "id": "retry-boundary",
                    "action": "changed",
                    "rationale": "Keep retry policy independent of storage adapters.",
                }
            ]
        },
    ).to_data()
    assert second["before"]["fingerprint"] == first["fingerprint"]
    assert second["desired"]["document"]["invariants"][0]["verification"]
    assert second["desired"]["fingerprint"] != first["fingerprint"]


def test_design_rejects_dangling_relationships_and_unrecognized_meaning() -> None:
    with pytest.raises(ValueError, match="endpoints"):
        DesiredState.from_snapshot(
            {
                "entity_relationships": [
                    {
                        "id": "broken",
                        "source": "missing",
                        "target": "other",
                    }
                ]
            }
        )
    with pytest.raises(ValueError, match="unsupported desired-state"):
        DesiredState.from_snapshot({"undocumented_constraints": ["Do not lose this."]})
    with pytest.raises(ValueError, match="removal delta"):
        DesiredState.from_snapshot(
            {"invariants": [{"id": "retired", "action": "removed"}]}
        )


def test_structural_metadata_is_not_required_in_observed_source() -> None:
    from powdrr_service.structrr.candidate_comparison import compare_candidate_snapshot

    baseline: dict[str, Any] = {"entities": []}
    handoff = _handoff(
        baseline,
        {
            "entities": [
                {
                    "id": "retry",
                    "type": "Component",
                    "action": "added",
                    "rationale": "Preserve retry ownership.",
                    "change_reason": "Fix duplicate IDs.",
                }
            ]
        },
    )
    from powdrr_service.structrr.proposal import ProposalRevision

    report = compare_candidate_snapshot(
        ProposalRevision.from_data(handoff.to_data()["proposal"]),
        baseline,
        {"entities": [{"id": "retry", "type": "Component"}]},
        extraction_complete=True,
    )
    assert report["passed"] is True


def test_change_intent_and_modifications_reach_worker_without_reinterpretation() -> (
    None
):
    handoff = _handoff(
        {"invariants": [{"id": "stable", "description": "Old contract."}]},
        {
            "intent": {
                "problem": "Duplicate processing.",
                "goal": "Preserve transaction identity.",
            },
            "invariants": [
                {
                    "id": "stable",
                    "action": "modified",
                    "description": "One ID per transaction.",
                }
            ],
        },
    )
    restored = DesignHandoff.from_data(handoff.to_data())
    request = ImplementationRequest.from_structrr_handoff(restored)
    assert "Duplicate processing." in request.prompt
    assert "One ID per transaction." in request.prompt


def test_explicit_source_checks_cannot_be_overridden_by_favorable_evidence(
    tmp_path: Path,
) -> None:
    (tmp_path / "retry.py").write_text("import persistence\ndef dispatch(): pass\n")
    handoff = _architecture_handoff()
    manifest = _manifest(tmp_path)
    report = check_design_agreement(
        handoff,
        root=tmp_path,
        manifest=manifest,
        evidence=[
            {
                "subject": "invariants:retry-boundary",
                "status": "satisfied",
                "handoff_fingerprint": handoff.to_data()["fingerprint"],
                "candidate_product_digest": manifest.product_digest,
                "reason": "A model says it is fine.",
                "evidence_refs": ["review:1"],
            }
        ],
    )
    assert report["passed"] is False
    assert report["findings"][0]["status"] == "contradicted"


def test_cli_handles_intended_source_deletions(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    source = repo / "retry.py"
    source.write_text("def dispatch(): pass\n")
    subprocess.run(["git", "-C", str(repo), "add", "retry.py"], check=True)
    source.unlink()
    handoff = _handoff(
        {},
        {
            "invariants": [
                {
                    "id": "retired",
                    "action": "added",
                    "verification": [
                        {"kind": "file-exists", "path": "retry.py", "present": False}
                    ],
                }
            ]
        },
    )
    path = tmp_path / "handoff.json"
    path.write_text(json.dumps(handoff.to_data()))
    assert (
        main(
            [
                "check-structrr-design",
                "--handoff",
                str(path),
                "--repo-root",
                str(repo),
                "--output",
                str(tmp_path / "agreement.json"),
            ]
        )
        == 0
    )


@pytest.mark.parametrize("action", ["change", "changed", "altered", "modified"])
def test_existing_design_modifications_survive_task_and_plan_projection(
    tmp_path: Path, action: str
) -> None:
    from powdrr_lift.workrr.feature_endpoint import (
        _load_implementation_plan,
        _task_structrr_changes,
    )

    plan = {
        "invariants": [
            {
                "id": "identity",
                "instruction_ref": "clause-1",
                "action": action,
                "description": "One transaction ID across retries.",
                "rationale": "Deduplicate downstream.",
            }
        ]
    }
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan))
    additions, deletions, *_ = _load_implementation_plan(path, "Fix retries")
    assert additions[0]["action"] == action
    assert not deletions
    additions, deletions, refs = _task_structrr_changes(
        plan, {"source_clause_refs": ["clause-1"]}, "structrr:plan"
    )
    assert additions[0]["rationale"] == "Deduplicate downstream."
    assert not deletions
    assert refs == ("structrr:plan#change:invariants:identity",)


def test_fresh_process_compiles_request_without_design_conversation(
    tmp_path: Path,
) -> None:
    path = tmp_path / "handoff.json"
    path.write_text(json.dumps(_architecture_handoff().to_data()))
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "\n".join(
                [
                    "import json, sys",
                    "from powdrr_service.structrr.design import DesignHandoff",
                    "from powdrr_lift.workrr.coding_agent import ImplementationRequest",
                    "from pathlib import Path",
                    "raw = json.loads(Path(sys.argv[1]).read_text())",
                    "handoff = DesignHandoff.from_data(raw)",
                    "print(ImplementationRequest.from_structrr_handoff(handoff).to_json())",
                ]
            ),
            str(path),
        ],
        check=True,
        text=True,
        capture_output=True,
    )
    request = ImplementationRequest.from_data(json.loads(completed.stdout))
    assert "Downstream deduplication owns transaction storage." in request.prompt
    assert "persistence" in request.prompt
    assert request.base_commit == "base"
    assert request.allowed_paths == ("retry.py",)


def test_architectural_discrepancy_requires_source_fix_or_design_amendment(
    tmp_path: Path,
) -> None:
    path = tmp_path / "retry.py"
    path.write_text("import persistence\ndef dispatch(): pass\n")
    handoff = _architecture_handoff()
    failed = check_design_agreement(
        handoff, root=tmp_path, manifest=_manifest(tmp_path)
    )
    assert failed["passed"] is False
    assert failed["findings"][0]["status"] == "contradicted"
    path.write_text("def dispatch(): pass\n")
    passed = check_design_agreement(
        handoff, root=tmp_path, manifest=_manifest(tmp_path)
    )
    assert passed["passed"] is True
    assert handoff.to_data() == _architecture_handoff().to_data()


def test_behavior_requires_current_evidence_and_cannot_pass_from_declaration(
    tmp_path: Path,
) -> None:
    (tmp_path / "retry.py").write_text("def dispatch(): return 'stable-id'\n")
    handoff = _handoff(
        {},
        {
            "invariants": [
                {
                    "id": "stable-id",
                    "action": "added",
                    "description": "Retries preserve the ID.",
                    "rationale": "Deduplicate downstream.",
                }
            ]
        },
    )
    manifest = _manifest(tmp_path)
    unknown = check_design_agreement(handoff, root=tmp_path, manifest=manifest)
    assert unknown["passed"] is False
    assert unknown["findings"][0]["status"] == "unknown"
    evidence = {
        "subject": "invariants:stable-id",
        "handoff_fingerprint": handoff.to_data()["fingerprint"],
        "candidate_product_digest": manifest.product_digest,
        "status": "satisfied",
        "reason": "Both attempts used the same transaction ID.",
        "evidence_refs": ["pytest:tests/test_retry.py::test_preserves_id"],
    }
    assert (
        check_design_agreement(
            handoff, root=tmp_path, manifest=manifest, evidence=[evidence]
        )["passed"]
        is True
    )
    assert (
        check_design_agreement(
            handoff, root=tmp_path, manifest=manifest, evidence=[evidence, evidence]
        )["passed"]
        is False
    )
    (tmp_path / "retry.py").write_text("def dispatch(): return 'new-id'\n")
    assert (
        check_design_agreement(
            handoff, root=tmp_path, manifest=manifest, evidence=[evidence]
        )["passed"]
        is False
    )
    assert (
        check_design_agreement(
            handoff, root=tmp_path, manifest=_manifest(tmp_path), evidence=[evidence]
        )["passed"]
        is False
    )


@pytest.mark.parametrize(
    "check",
    [
        {"kind": "unimplemented-analysis", "path": "retry.py"},
        {"kind": "file-exists", "path": "../outside.py", "present": False},
        {
            "kind": "python-symbol",
            "path": "retry.py",
            "symbol": "dispatch",
            "present": "yes",
        },
    ],
)
def test_unknown_or_unsafe_source_checks_block_agreement(
    tmp_path: Path, check: dict[str, Any]
) -> None:
    (tmp_path / "retry.py").write_text("def dispatch(): pass\n")
    handoff = _handoff(
        {},
        {
            "invariants": [
                {"id": "boundary", "action": "added", "verification": [check]}
            ]
        },
    )
    report = check_design_agreement(
        handoff, root=tmp_path, manifest=_manifest(tmp_path)
    )
    assert report["passed"] is False
    assert report["findings"][0]["status"] == "unknown"


def test_cli_handoff_and_agreement_are_independent_of_workflow_state(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "retry.py").write_text("def dispatch(): pass\n")
    baseline = tmp_path / "baseline.json"
    baseline.write_text("{}")
    proposal = tmp_path / "proposal.json"
    raw = _architecture_handoff().to_data()["proposal"]
    proposal.write_text(json.dumps(raw))
    handoff = tmp_path / "handoff.json"
    assert (
        main(
            [
                "compile-structrr-handoff",
                "--baseline",
                str(baseline),
                "--proposal",
                str(proposal),
                "--base-commit",
                "base",
                "--output",
                str(handoff),
            ]
        )
        == 0
    )
    assert handoff.with_suffix(".request.json").is_file()
    assert (
        main(
            [
                "check-structrr-design",
                "--handoff",
                str(handoff),
                "--repo-root",
                str(repo),
                "--output",
                str(tmp_path / "agreement.json"),
            ]
        )
        == 0
    )
    (repo / "retry.py").write_text(
        "from persistence import store\ndef dispatch(): pass\n"
    )
    assert (
        main(
            [
                "check-structrr-design",
                "--handoff",
                str(handoff),
                "--repo-root",
                str(repo),
                "--output",
                str(tmp_path / "agreement.json"),
            ]
        )
        == 1
    )
