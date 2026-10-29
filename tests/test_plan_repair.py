from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from powdrr_service.planning.repair import (
    PlanDiscovery,
    PlanRepairError,
    PlanRepairPatch,
    RepairDraft,
    RepairSubject,
    allocated_repair_item_id,
    apply_plan_repair_patch,
    build_plan_repair_patch,
    dependency_edge_digest,
    expand_repair_impact,
    item_content_digest,
    item_input_contract_digest,
    plan_revision_digest,
)

from powdrr_lift.workrr.github_issue_queue import PlannedWorkItem, ReviewedExecutionPlan
from procedrr import ProcessCatalog


def _item(item_id: str, *, dependencies: tuple[str, ...] = ()) -> PlannedWorkItem:
    return PlannedWorkItem(
        item_id=item_id,
        title=f"Implement {item_id}",
        objective=f"Deliver {item_id}.",
        scope=(f"Scope for {item_id}.",),
        acceptance_criteria=(f"{item_id} works.",),
        dependencies=dependencies,
        workflow_directory=None,
        workflow_id=f"plan-{item_id}",
        workflow_file=f"docs/workflows/plan/{item_id}.yaml",
        implementation_steps=(f"Implement {item_id}.",),
        allowed_paths=("src/",),
    )


def _draft(*, split_first_item: bool = False) -> RepairDraft:
    items = (
        _item("item-01"),
        _item("item-02", dependencies=("item-01",)),
        _item("item-03"),
    )
    units = [
        {"id": "unit-01", "requirement_ids": ["req-01"], "validation_ids": ["val-01"]},
        {"id": "unit-02", "requirement_ids": ["req-02"], "validation_ids": ["val-01"]},
        {"id": "unit-03", "requirement_ids": ["req-03"], "validation_ids": ["val-01"]},
    ]
    requirements = [
        {"id": "req-01", "statement": "First behavior", "source_refs": ["anchor-01"]},
        {"id": "req-02", "statement": "Second behavior", "source_refs": ["anchor-02"]},
        {
            "id": "req-03",
            "statement": "Independent behavior",
            "source_refs": ["anchor-03"],
        },
    ]
    if split_first_item:
        requirements.append(
            {
                "id": "req-04",
                "statement": "Downstream behavior",
                "source_refs": ["anchor-04"],
            }
        )
        units.append(
            {
                "id": "unit-04",
                "requirement_ids": ["req-04"],
                "validation_ids": ["val-01"],
            }
        )
    manifest: dict[str, Any] = {
        "schema": "https://powdrr.io/schemas/planning-manifest-v1",
        "source": {"path": "docs/plan.md"},
        "base": {"commit_sha": "a" * 40},
        "requirements": requirements,
        "coverage": [
            {"requirement_id": requirement["id"], "status": "missing"}
            for requirement in requirements
        ],
        "units": units,
        "item_unit_ids": {
            "item-01": ["unit-01", *(["unit-02"] if split_first_item else [])],
            "item-02": (["unit-03"] if split_first_item else ["unit-02"]),
            "item-03": (["unit-04"] if split_first_item else ["unit-03"]),
        },
        "validations": [{"id": "val-01"}],
        "evidence": [{"id": "evidence-0001"}],
        "setups": [],
        "structrr": {"proposal_path": "docs/plans/plan-structrr-proposal.json"},
        "latest_repair": None,
    }
    plan = ReviewedExecutionPlan("plan", items, "docs/plan.md", manifest)
    artifacts = _compile(plan, manifest)
    return RepairDraft(plan, manifest, artifacts)


def _compile(
    plan: ReviewedExecutionPlan,
    manifest: Mapping[str, Any],
    *,
    corrupt_unrelated: bool = False,
) -> dict[str, str]:
    artifacts = {
        "docs/plans/plan-execution-plan.yaml": "\n".join(
            f"{item.item_id}:{item_content_digest(item)}" for item in plan.items
        ),
        "docs/plans/plan-structrr-proposal.json": json.dumps(
            manifest.get("structrr"), sort_keys=True
        ),
    }
    for item in plan.items:
        content = item_content_digest(item)
        if corrupt_unrelated and item.item_id == "item-03":
            content = "changed-without-impact"
        artifacts[item.workflow_file or ""] = content
    return artifacts


def _update_operation(item: PlannedWorkItem) -> dict[str, Any]:
    return {
        "operation": "update_item",
        "item_id": item.item_id,
        "expected_digest": item_content_digest(item),
        "replacement_unit_ids": ["unit-01-revised"],
        "reason": "Coverage evidence shows one required behavior remains.",
    }


def test_input_contract_digest_includes_human_setup_and_validation_context() -> None:
    item = _item("item-01")
    manifest = {
        "item_unit_ids": {"item-01": ["unit-01"]},
        "units": [{"id": "unit-01", "validation_ids": ["validation-01"]}],
        "setups": [
            {
                "id": "setup-01",
                "owner_item_id": "item-01",
                "unblocks_validation_ids": ["validation-01"],
                "instructions": "Install the credential.",
            }
        ],
        "validations": [{"id": "validation-01", "target": "CI credential"}],
        "validation_coverage": [
            {
                "item_id": "item-01",
                "criterion": "The configured credential is used.",
                "validation_ids": ["validation-01"],
            }
        ],
    }
    changed_setup = dict(manifest)
    changed_setup["setups"] = [
        {
            "id": "setup-01",
            "owner_item_id": "item-01",
            "unblocks_validation_ids": ["validation-01"],
            "instructions": "Install the rotated credential.",
        }
    ]

    assert item_input_contract_digest(item, manifest) != item_input_contract_digest(
        item, changed_setup
    )


def test_coverage_discovery_expands_dependencies_and_preserves_unrelated_item() -> None:
    draft = _draft()
    impact = expand_repair_impact(
        draft,
        [RepairSubject("requirement", "req-01", "Coverage is partial.")],
    )
    assert impact.item_ids == ("item-01", "item-02")
    assert "docs/workflows/plan/item-03.yaml" not in impact.artifact_paths

    first = draft.plan.items[0]
    replacement = replace(first, title="Complete the missing behavior")
    operation = _update_operation(first)
    patch = build_plan_repair_patch(
        draft,
        repair_id="repair-coverage-01",
        discovery_ids=["discovery-coverage-01"],
        affected_item_ids=impact.item_ids,
        operations=[operation],
        replacement_items={"item-01": replacement},
        replacement_units=[
            {
                "id": "unit-01-revised",
                "requirement_ids": ["req-01"],
                "validation_ids": ["val-01"],
            }
        ],
    )
    assert PlanRepairPatch.from_data(patch.to_data()) == patch
    repaired = apply_plan_repair_patch(
        draft,
        patch,
        replacement_items={"item-01": replacement},
        replacement_units=[
            {
                "id": "unit-01-revised",
                "requirement_ids": ["req-01"],
                "validation_ids": ["val-01"],
            }
        ],
        compile_candidate=_compile,
    )

    before = {item.item_id: item_content_digest(item) for item in draft.plan.items}
    after = {item.item_id: item_content_digest(item) for item in repaired.plan.items}
    assert before["item-03"] == after["item-03"]
    assert (
        repaired.artifacts["docs/workflows/plan/item-03.yaml"]
        == draft.artifacts["docs/workflows/plan/item-03.yaml"]
    )
    assert repaired.plan.items[0].item_id == "item-01"
    assert repaired.manifest["latest_repair"]["affected_item_ids"] == [
        "item-01",
        "item-02",
    ]


def test_split_allocates_stable_successors_and_preserves_requirements() -> None:
    draft = _draft(split_first_item=True)
    old_item = draft.plan.items[0]
    repair_id = "repair-split-01"
    first_id = allocated_repair_item_id("plan", repair_id, "first-scope")
    second_id = allocated_repair_item_id("plan", repair_id, "second-scope")
    templates = {
        "first-scope": _item("placeholder-first"),
        "second-scope": _item("placeholder-second"),
    }
    operations = [
        {
            "operation": "add_item",
            "new_item_ref": "first-scope",
            "unit_ids": ["unit-01"],
            "requirement_ids": ["req-01"],
            "reason": "Separate first behavior.",
        },
        {
            "operation": "add_item",
            "new_item_ref": "second-scope",
            "unit_ids": ["unit-02"],
            "requirement_ids": ["req-02"],
            "reason": "Separate second behavior.",
        },
        {
            "operation": "retire_item",
            "item_id": old_item.item_id,
            "expected_digest": item_content_digest(old_item),
            "disposition": "superseded",
            "reason": "The combined item exceeded the intended PR size.",
            "replacement_item_ids": [first_id, second_id],
            "evidence_ids": [],
        },
    ]
    patch = build_plan_repair_patch(
        draft,
        repair_id=repair_id,
        discovery_ids=["discovery-split-01"],
        affected_item_ids=["item-01", "item-02"],
        operations=operations,
        replacement_items=templates,
    )
    repaired = apply_plan_repair_patch(
        draft,
        patch,
        replacement_items=templates,
        compile_candidate=_compile,
    )
    allocations = repaired.manifest["item_unit_ids"]
    assert "item-01" not in allocations
    assert allocations[first_id] == ["unit-01"]
    assert allocations[second_id] == ["unit-02"]
    assert {item.item_id for item in repaired.plan.items} == {
        "item-02",
        "item-03",
        first_id,
        second_id,
    }
    consumer = next(item for item in repaired.plan.items if item.item_id == "item-02")
    assert set(consumer.dependencies) == {first_id, second_id}
    assert {"req-01", "req-02"} <= {
        requirement_id
        for unit in repaired.manifest["units"]
        for requirement_id in unit["requirement_ids"]
    }


def test_stale_patch_and_dependency_cycle_leave_draft_unchanged() -> None:
    draft = _draft()
    first = draft.plan.items[0]
    replacement = replace(first, title="Updated title")
    patch = build_plan_repair_patch(
        draft,
        repair_id="repair-stale-01",
        discovery_ids=["discovery-01"],
        affected_item_ids=["item-01", "item-02"],
        operations=[_update_operation(first)],
        replacement_items={"item-01": replacement},
        replacement_units=[
            {
                "id": "unit-01-revised",
                "requirement_ids": ["req-01"],
                "validation_ids": ["val-01"],
            }
        ],
    )
    changed_base = replace(
        draft,
        plan=replace(
            draft.plan,
            items=(
                replace(first, objective="Changed independently."),
                *draft.plan.items[1:],
            ),
        ),
    )
    original_digest = plan_revision_digest(changed_base)
    with pytest.raises(PlanRepairError, match="base digest is stale"):
        apply_plan_repair_patch(
            changed_base,
            patch,
            replacement_items={"item-01": replacement},
            replacement_units=[
                {
                    "id": "unit-01-revised",
                    "requirement_ids": ["req-01"],
                    "validation_ids": ["val-01"],
                }
            ],
            compile_candidate=_compile,
        )
    assert plan_revision_digest(changed_base) == original_digest

    tampered_patch = replace(patch, expected_result_digest="sha256:" + "0" * 64)
    with pytest.raises(PlanRepairError, match="result digest does not match"):
        apply_plan_repair_patch(
            draft,
            tampered_patch,
            replacement_items={"item-01": replacement},
            replacement_units=[
                {
                    "id": "unit-01-revised",
                    "requirement_ids": ["req-01"],
                    "validation_ids": ["val-01"],
                }
            ],
            compile_candidate=_compile,
        )

    second = draft.plan.items[1]
    cycle_operation = {
        "operation": "set_dependency",
        "before_item_id": second.item_id,
        "after_item_id": first.item_id,
        "present": True,
        "expected_edge_digest": dependency_edge_digest(second, first),
        "reason": "This deliberately creates a dependency cycle.",
    }
    cycle_patch = PlanRepairPatch(
        "repair-cycle-01",
        plan_revision_digest(draft),
        ("discovery-cycle-01",),
        ("item-01", "item-02"),
        (cycle_operation,),
        "sha256:" + "0" * 64,
    )
    before_artifacts = dict(draft.artifacts)
    with pytest.raises(PlanRepairError, match="contains a cycle"):
        apply_plan_repair_patch(draft, cycle_patch, compile_candidate=_compile)
    assert draft.artifacts == before_artifacts
    assert plan_revision_digest(draft) == cycle_patch.base_plan_digest


def test_repair_rejects_compiler_changes_to_an_unrelated_workflow() -> None:
    draft = _draft()
    first = draft.plan.items[0]
    replacement = replace(first, title="Updated title")
    impact = expand_repair_impact(
        draft, [RepairSubject("item", "item-01", "Fix one item.")]
    )
    patch = build_plan_repair_patch(
        draft,
        repair_id="repair-preserve-01",
        discovery_ids=["discovery-preserve-01"],
        affected_item_ids=impact.item_ids,
        operations=[_update_operation(first)],
        replacement_items={"item-01": replacement},
        replacement_units=[
            {
                "id": "unit-01-revised",
                "requirement_ids": ["req-01"],
                "validation_ids": ["val-01"],
            }
        ],
    )
    with pytest.raises(PlanRepairError, match="unrelated workflow artifact"):
        apply_plan_repair_patch(
            draft,
            patch,
            replacement_items={"item-01": replacement},
            replacement_units=[
                {
                    "id": "unit-01-revised",
                    "requirement_ids": ["req-01"],
                    "validation_ids": ["val-01"],
                }
            ],
            compile_candidate=lambda plan, manifest: _compile(
                plan, manifest, corrupt_unrelated=True
            ),
        )


def test_repair_procedrr_process_is_catalogued() -> None:
    process_dir = Path(__file__).parents[1] / "docs/procedrr/processes"
    process = ProcessCatalog.load(process_dir).get("plan-repair-execution")
    assert process.name == "plan-repair-execution"


def test_discovery_is_bound_to_its_plan_revision_and_repository_base() -> None:
    draft = _draft()
    data = {
        "id": "discovery-coverage-01",
        "kind": "dependency_defect",
        "summary": "This item does not actually depend on the previous output.",
        "requirement_ids": ["req-01"],
        "item_ids": ["item-01"],
        "evidence_ids": ["evidence-0001"],
        "requested_outcome": "Remove the unsupported dependency edge.",
        "base_plan_digest": plan_revision_digest(draft),
        "base_commit": "a" * 40,
    }
    discovery = PlanDiscovery.from_data(data)
    patch = build_plan_repair_patch(
        draft,
        repair_id="repair-discovery-01",
        discovery_ids=[discovery],
        affected_item_ids=["item-01", "item-02"],
        operations=[
            {
                "operation": "set_dependency",
                "before_item_id": "item-01",
                "after_item_id": "item-02",
                "present": False,
                "expected_edge_digest": dependency_edge_digest(
                    draft.plan.items[0], draft.plan.items[1]
                ),
                "reason": "The downstream validation consumes this output.",
            }
        ],
    )
    assert patch.discovery_ids == (discovery.id,)
    repaired = apply_plan_repair_patch(draft, patch, compile_candidate=_compile)
    assert (
        next(
            item for item in repaired.plan.items if item.item_id == "item-02"
        ).dependencies
        == ()
    )
    stale_data = {**data, "base_plan_digest": "sha256:" + "0" * 64}
    with pytest.raises(PlanRepairError, match="different plan revision"):
        build_plan_repair_patch(
            draft,
            repair_id="repair-stale-discovery",
            discovery_ids=[PlanDiscovery.from_data(stale_data)],
            affected_item_ids=["item-01", "item-02"],
            operations=[
                {
                    "operation": "set_dependency",
                    "before_item_id": "item-01",
                    "after_item_id": "item-02",
                    "present": False,
                    "expected_edge_digest": dependency_edge_digest(
                        draft.plan.items[0], draft.plan.items[1]
                    ),
                    "reason": "The downstream validation consumes this output.",
                }
            ],
        )
