from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from typing import Any

import pytest

from powdrr_lift.structrr.rebase import snapshot_digest
from powdrr_lift.workrr.github_issue_queue import (
    GitHubIssueQueueError,
    PlannedWorkItem,
    ReviewedExecutionPlan,
    parse_reviewed_plan_yaml,
)
from powdrr_lift.workrr.multipr_structrr import compile_multi_pr_proposal
from powdrr_lift.workrr.planning.assembly import PlannedWrite, WorkUnit
from powdrr_lift.workrr.planning.compiler import (
    PLANNING_MANIFEST_SCHEMA,
    PlanningCompilationError,
    PlanningManifest,
    compile_plan,
    serialize_reviewed_plan_yaml,
    validate_compiled_plan,
)


def _source_digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _fixture() -> tuple[ReviewedExecutionPlan, PlanningManifest, Any]:
    plan_id = "bounded-plan"
    baseline: dict[str, Any] = {"entities": []}
    proposal = compile_multi_pr_proposal(
        plan_id=plan_id,
        baseline_snapshot=baseline,
        items=[
            {
                "id": "item-01",
                "acceptance_criteria": ["The implementation is covered."],
                "allowed_paths": ["src/app.py"],
            }
        ],
        proposed_operations=[],
        source_ref="docs/plan.md#sha256:abc",
    )
    item = PlannedWorkItem(
        item_id="item-01",
        title="Implement the requirement",
        objective="Add the required behavior.",
        scope=("Implement the behavior.",),
        acceptance_criteria=("The behavior is covered.",),
        dependencies=(),
        workflow_directory=None,
        workflow_id="bounded-plan-item-01",
        workflow_file="docs/workflows/bounded-plan/item-01.yaml",
        implementation_steps=("Implement and verify the behavior.",),
        allowed_paths=("src/app.py",),
    )
    plan = ReviewedExecutionPlan(plan_id, (item,), "docs/plan.md", None)
    commit = "a" * 40
    blob = "b" * 40
    anchor_digest = "c" * 64
    evidence_digest = "d" * 64
    unit = WorkUnit(
        "unit-01",
        ("req-01",),
        "Implement behavior",
        "core",
        "python",
        (
            PlannedWrite(
                "src/app.py",
                "implementation",
                "python",
                ("anchor-01",),
                ("evidence-0001",),
            ),
        ),
        "Implement the behavior.",
        ("The behavior works.",),
        ("validation-01",),
    )
    records: dict[str, Any] = {
        "source": {
            "repository": "owner/repo",
            "commit_sha": commit,
            "path": "docs/plan.md",
            "blob_sha": blob,
            "content_sha256": _source_digest("source"),
        },
        "base": {
            "commit_sha": commit,
            "structrr_fingerprint": snapshot_digest(baseline),
        },
        "source_blocks": [
            {
                "id": "block-01",
                "disposition": "normative",
                "requirement_ids": ["req-01"],
                "reason": None,
            }
        ],
        "source_anchors": [
            {
                "id": "anchor-01",
                "source_blob_sha": blob,
                "block_id": "block-01",
                "clause_id": "clause-01",
                "start_line": 1,
                "end_line": 1,
                "start_offset": 0,
                "end_offset": 7,
                "quote_sha256": anchor_digest,
            }
        ],
        "requirements": [
            {
                "id": "req-01",
                "statement": "Implement the behavior.",
                "kind": "behavior",
                "source_refs": ["anchor-01"],
                "related_requirement_ids": [],
            }
        ],
        "coverage": [
            {
                "requirement_id": "req-01",
                "status": "complete",
                "evidence_ids": ["evidence-0001"],
                "reason": "The implementation is already evidenced.",
            }
        ],
        "validation_coverage": [
            {
                "item_id": "item-01",
                "criterion": "The behavior is covered.",
                "validation_ids": ["validation-01"],
                "reason": "The focused test exercises the criterion.",
            }
        ],
        "evidence": [
            {
                "id": "evidence-0001",
                "base_commit": commit,
                "path": "src/existing.py",
                "start_line": 1,
                "end_line": 5,
                "content_sha256": evidence_digest,
                "kind": "implementation",
                "symbol": "handle_request",
            }
        ],
        "units": [unit.to_data()],
        "item_unit_ids": {"item-01": ["unit-01"]},
        "runtime_facts": [
            {"unit_id": "unit-01", "path": "src/app.py", "runtime": "python"}
        ],
        "validations": [
            {
                "id": "validation-01",
                "description": "Run the focused tests.",
                "kind": "local_command",
                "target": "pytest tests/test_app.py",
                "requirement_ids": ["req-01"],
                "source_refs": ["anchor-01"],
                "evidence_ids": [],
            }
        ],
        "setups": [],
        "structrr": {
            "baseline_fingerprint": proposal.to_data()["baseline_fingerprint"],
            "proposal_path": "docs/plans/bounded-plan-structrr-proposal.json",
            "proposal_fingerprint": proposal.to_data()["fingerprint"],
        },
        "latest_repair": None,
    }
    manifest = PlanningManifest.create(records)
    return plan, manifest, proposal


def test_compiler_round_trips_plan_and_emits_linked_artifacts() -> None:
    plan, manifest, proposal = _fixture()
    assert manifest.data["schema"] == PLANNING_MANIFEST_SCHEMA

    compiled = compile_plan(
        plan=plan,
        planning_manifest=manifest,
        repository_snapshot={
            "source": manifest.data["source"],
            "base": manifest.data["base"],
        },
        structrr_proposal=proposal,
        verification_command="pytest -q",
        plan_path="docs/plans/bounded-plan.yaml",
    )

    reparsed = parse_reviewed_plan_yaml(compiled.files["docs/plans/bounded-plan.yaml"])
    assert reparsed.planning == manifest.to_data()
    assert set(compiled.destinations) == set(compiled.files)
    assert compiled.ready_item_ids == ("item-01",)
    assert not compiled.blocked_item_reasons
    repository_snapshot = {
        "source": manifest.data["source"],
        "base": manifest.data["base"],
    }
    assert validate_compiled_plan(compiled, repository_snapshot).valid
    workflow = compiled.files["docs/workflows/bounded-plan/item-01.yaml"]
    assert "dependencies: []" in workflow
    assert "docs/plans/bounded-plan.yaml" in workflow
    assert "docs/plans/bounded-plan-structrr-proposal.json" in workflow
    assert compiled.files["docs/plans/bounded-plan-structrr-proposal.json"].endswith(
        "\n"
    )
    changed_source = dict(manifest.data["source"])
    changed_source["commit_sha"] = "e" * 40
    with pytest.raises(PlanningCompilationError, match="pinned source changed"):
        compile_plan(
            plan=plan,
            planning_manifest=manifest,
            repository_snapshot={
                "source": changed_source,
                "base": manifest.data["base"],
            },
            structrr_proposal=proposal,
            verification_command="pytest -q",
            plan_path="docs/plans/bounded-plan.yaml",
        )


def test_compiled_artifact_validation_detects_tampering() -> None:
    plan, manifest, proposal = _fixture()
    compiled = compile_plan(
        plan=plan,
        planning_manifest=manifest,
        repository_snapshot={
            "source": manifest.data["source"],
            "base": manifest.data["base"],
        },
        structrr_proposal=proposal,
        verification_command="pytest -q",
        plan_path="docs/plans/bounded-plan.yaml",
    )
    proposal_data = json.loads(
        compiled.files["docs/plans/bounded-plan-structrr-proposal.json"]
    )
    proposal_data["plan_id"] = "tampered-plan"
    files = {
        **compiled.files,
        "docs/plans/bounded-plan-structrr-proposal.json": json.dumps(proposal_data),
    }
    tampered = replace(compiled, files=files)

    report = validate_compiled_plan(
        tampered,
        {"source": manifest.data["source"], "base": manifest.data["base"]},
    )

    assert not report.valid
    assert "Structrr proposal file differs" in report.errors[0]


def test_legacy_plan_without_manifest_remains_readable() -> None:
    plan, _, _ = _fixture()
    rendered = serialize_reviewed_plan_yaml(plan)

    parsed = parse_reviewed_plan_yaml(rendered)

    assert parsed.planning is None
    assert parsed.plan_id == plan.plan_id


def test_manifest_rejects_unresolved_source_refs_and_missing_work_allocation() -> None:
    plan, manifest, proposal = _fixture()
    data = manifest.to_data()
    data["requirements"][0]["source_refs"] = ["unknown-anchor"]
    with pytest.raises(PlanningCompilationError, match="unknown source anchor"):
        PlanningManifest.from_data(data)

    data = manifest.to_data()
    data["item_unit_ids"] = {"item-01": []}
    with pytest.raises(PlanningCompilationError, match="unit IDs"):
        compile_plan(
            plan=plan,
            planning_manifest=data,
            repository_snapshot={
                "source": manifest.data["source"],
                "base": manifest.data["base"],
            },
            structrr_proposal=proposal,
            verification_command="pytest -q",
            plan_path="docs/plans/bounded-plan.yaml",
        )


def test_plan_parser_rejects_a_forged_manifest_digest() -> None:
    plan, manifest, _ = _fixture()
    plan = ReviewedExecutionPlan(
        plan.plan_id, plan.items, plan.source_document, manifest.to_data()
    )
    rendered = serialize_reviewed_plan_yaml(plan).replace(
        manifest.data["semantic_revision_digest"], "sha256:" + "0" * 64
    )

    with pytest.raises(GitHubIssueQueueError, match="semantic revision digest"):
        parse_reviewed_plan_yaml(rendered)


def test_compiler_preserves_dependency_edges_and_manual_setup_context() -> None:
    plan, manifest, _ = _fixture()
    second_item = PlannedWorkItem(
        item_id="item-02",
        title="Complete integration validation",
        objective="Run the integration validation after its prerequisite.",
        scope=("Run the integration validation.",),
        acceptance_criteria=("The integration check succeeds.",),
        dependencies=("item-01",),
        workflow_directory=None,
        workflow_id="bounded-plan-item-02",
        workflow_file="docs/workflows/bounded-plan/item-02.yaml",
        implementation_steps=("Run the integration check.",),
        allowed_paths=("tests/integration.py",),
    )
    plan = replace(plan, items=(*plan.items, second_item))
    unit_two = WorkUnit(
        "unit-02",
        ("req-01",),
        "Run integration validation",
        "integration",
        "python",
        (
            PlannedWrite(
                "tests/integration.py",
                "test",
                "python",
                ("anchor-01",),
                ("evidence-0001",),
            ),
        ),
        "Run the integration check.",
        ("The integration check succeeds.",),
        ("validation-01",),
        ("unit-01",),
    )
    records = manifest.to_data()
    records.pop("schema")
    records.pop("semantic_revision_digest")
    records["units"].append(unit_two.to_data())
    records["item_unit_ids"] = {"item-01": ["unit-01"], "item-02": ["unit-02"]}
    records["runtime_facts"].append(
        {"unit_id": "unit-02", "path": "tests/integration.py", "runtime": "python"}
    )
    records["validation_coverage"].append(
        {
            "item_id": "item-02",
            "criterion": "The integration check succeeds.",
            "validation_ids": ["validation-01"],
            "reason": "The integration check is the acceptance test.",
        }
    )
    proposal = compile_multi_pr_proposal(
        plan_id=plan.plan_id,
        baseline_snapshot={"entities": []},
        items=[
            {"id": "item-01", "acceptance_criteria": ["The behavior is covered."]},
            {
                "id": "item-02",
                "acceptance_criteria": ["The integration check succeeds."],
            },
        ],
        proposed_operations=[],
        source_ref="docs/plan.md#sha256:abc",
    )
    records["structrr"]["baseline_fingerprint"] = proposal.to_data()[
        "baseline_fingerprint"
    ]
    records["structrr"]["proposal_fingerprint"] = proposal.to_data()["fingerprint"]
    manual: dict[str, Any] = {
        "title": "Register the GitHub App",
        "instructions": ["Install the app in the repository."],
        "verification": "Confirm the report check can access the app.",
        "trigger": "after_implementation_before_validation",
        "unblocks": "pytest tests/test_app.py",
    }
    records["setups"] = [
        {
            "id": "setup-01",
            "requirement_ids": ["req-01"],
            "owner": "human",
            "owner_item_id": "item-02",
            "title": manual["title"],
            "instructions": manual["instructions"],
            "verification": manual["verification"],
            "trigger": manual["trigger"],
            "unblocks_validation_ids": ["validation-01"],
            "source_refs": ["anchor-01"],
        }
    ]
    manifest_with_setup = PlanningManifest.create(records)
    plan = replace(
        plan, items=(plan.items[0], replace(second_item, manual_prerequisite=manual))
    )

    compiled = compile_plan(
        plan=plan,
        planning_manifest=manifest_with_setup,
        repository_snapshot={
            "source": manifest_with_setup.data["source"],
            "base": manifest_with_setup.data["base"],
        },
        structrr_proposal=proposal,
        verification_command="pytest -q",
        plan_path="docs/plans/bounded-plan.yaml",
    )

    assert second_item.workflow_file is not None
    second_workflow = compiled.files[second_item.workflow_file]
    assert "dependencies:\n- bounded-plan-item-01" in second_workflow
    assert manual["title"] in second_workflow
    assert manual["unblocks"] in second_workflow
    assert compiled.ready_item_ids == ("item-01",)
    assert compiled.blocked_item_reasons == {"item-02": "waiting for item-01"}

    incorrect_manual = {**manual, "unblocks": "different validation"}
    invalid_plan = replace(
        plan,
        items=(
            plan.items[0],
            replace(second_item, manual_prerequisite=incorrect_manual),
        ),
    )
    with pytest.raises(PlanningCompilationError, match="setup context differs"):
        compile_plan(
            plan=invalid_plan,
            planning_manifest=manifest_with_setup,
            repository_snapshot={
                "source": manifest_with_setup.data["source"],
                "base": manifest_with_setup.data["base"],
            },
            structrr_proposal=proposal,
            verification_command="pytest -q",
            plan_path="docs/plans/bounded-plan.yaml",
        )
