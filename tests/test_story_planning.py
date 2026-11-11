from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import yaml
from powdrr_service.planning.checkpoints import InMemoryCheckpointStore
from powdrr_service.planning.compiler import (
    PlanningCompilationError,
    PlanningManifest,
    compile_plan,
    validate_compiled_plan,
)
from powdrr_service.planning.coverage import RequirementEvidenceSearch
from powdrr_service.planning.driver import SourceLedgerQuestionMachine
from powdrr_service.planning.multipr_structrr import (
    load_multi_pr_proposal,
    write_multi_pr_proposal,
)
from powdrr_service.planning.plan_document import parse_reviewed_plan_yaml
from powdrr_service.planning.service import (
    PlanningOptions,
    PlanningSession,
    run_planning_procedrr,
)
from powdrr_service.planning.source import PinnedSourceDocument, segment_markdown_source
from powdrr_service.planning.story_allocation import (
    StoryAllocationError,
    merge_story_refs,
    parse_story_refs,
    pin_story,
    source_story_refs,
    story_amendment_impact,
    story_planning_context,
    validate_story_refs,
)
from powdrr_service.resources import bundled_process_directory
from powdrr_service.structrr.design import project_sections
from powdrr_service.structrr.user_stories import (
    parse_story_catalog,
    story_contract_digest,
)
from test_planning_service import _PlanningScriptClient

from powdrr_lift.workrr import task_agent
from powdrr_lift.workrr.github_issue_queue import QueueIssue

STORY = "story:session-resume"


def confirm(snapshot: dict[str, Any]) -> dict[str, Any]:
    snapshot["user_stories"][0]["confirmation"] = {
        "contract_digest": story_contract_digest(parse_story_catalog(snapshot), STORY),
        "decision_ref": "review:fixture:1",
        "authority": "reviewed_source",
    }
    return snapshot


@pytest.fixture
def graph() -> dict[str, Any]:
    return confirm(
        yaml.safe_load(
            (
                Path(__file__).parent / "fixtures/user_stories/session-resume.yaml"
            ).read_text()
        )
    )


class StoryPlanningClient(_PlanningScriptClient):
    def _answer(self, packet: Mapping[str, Any]) -> dict[str, Any]:
        result = super()._answer(packet)
        if packet["exact_question"]["kind"] == "propose_work_units":
            context = next(
                fact["value"]
                for fact in packet["accepted_facts"]
                if fact["ref_id"] == "story-contract-context"
            )
            assert len(context["story_context"]["user_story_steps"]) >= 4
            assert len(context["story_context"]["user_story_transitions"]) >= 3
            first = result["records"][0]
            second = copy.deepcopy(first)
            first["planned_writes"][0]["path"] = "src/resume.py"
            second["planned_writes"][0]["path"] = "src/resume.ts"
            second["deliverable"] = "Expose the resume interaction to the user."
            result["records"].append(second)
        return result


def build_story_plan(graph: dict[str, Any], tmp_path: Path) -> tuple[Any, Any, Any]:
    ref = pin_story(graph, STORY)
    text = (
        "# Plan\n\nImplement the requested behavior.\n\n```yaml\n"
        + yaml.safe_dump({"story_refs": [ref.to_data()]})
        + "```\n"
    )
    document = PinnedSourceDocument(
        "owner/repo",
        "a" * 40,
        "docs/plan.md",
        "b" * 40,
        hashlib.sha256(text.encode()).hexdigest(),
        text,
    )
    machine = SourceLedgerQuestionMachine(
        segment_markdown_source(document),
        plan_id="story-plan",
        structrr_baseline=graph,
        evidence_search=lambda requirements: {
            r.id: RequirementEvidenceSearch((), (), 0, 0, document.commit_sha)
            for r in requirements
        },
    )
    session = PlanningSession(
        session_id="story-planning-e2e",
        plan_id="story-plan",
        repository="owner/repo",
        base_commit=document.commit_sha,
        source_path=document.path,
        source_blob_sha=document.blob_sha,
        verification_command="make test",
        issue=QueueIssue(
            42,
            "Planning",
            "",
            "open",
            ("powdrr:planning",),
            "https://github.com/owner/repo/issues/42",
        ),
    )
    store = InMemoryCheckpointStore(session_id=session.session_id)
    client = StoryPlanningClient()
    result = run_planning_procedrr(
        process_directory=bundled_process_directory(),
        session=session,
        state_machine=machine,
        checkpoint_store=store,
        client=client,
        options=PlanningOptions(model="scripted"),
        stderr=None,
    )
    assert result.terminal_status.value == "succeeded", store.load().events[-1].payload
    accepted = {
        event.payload["question"]["question_id"]: dict(event.payload["accepted_answer"])
        for event in store.load().events
        if event.event_type == "answer_accepted"
    }
    compiled = machine.compile_accepted_plan(
        accepted,
        plan_id="story-plan",
        plan_path="docs/plans/story-execution-plan.yaml",
        proposal_path="docs/plans/story-structrr-proposal.json",
    )
    proposal_path = tmp_path / "proposal.json"
    proposal_path.write_text(compiled.files[compiled.proposal_path])
    proposal = load_multi_pr_proposal(proposal_path)
    return compiled, proposal, machine


@pytest.fixture
def compiled_story_plan(graph: dict[str, Any], tmp_path: Path) -> tuple[Any, Any, Any]:
    return build_story_plan(graph, tmp_path)


def test_native_multi_pr_story_allocation_survives_every_artifact(
    compiled_story_plan: tuple[Any, Any, Any], graph: dict[str, Any]
) -> None:
    compiled, proposal, machine = compiled_story_plan
    expected = (pin_story(graph, STORY),)
    assert len(compiled.plan.items) == 3
    first, second, integration = compiled.plan.items
    assert integration.story_validation
    assert set(integration.dependencies) == {first.item_id, second.item_id}
    assert not first.story_validation and not second.story_validation
    assert all(item.story_refs == expected for item in compiled.plan.items)
    assert all(
        unit["story_refs"] == [expected[0].to_data()]
        for unit in compiled.plan.planning["units"]
    )
    assert all(
        requirement["story_refs"] == [expected[0].to_data()]
        for requirement in compiled.plan.planning["requirements"]
    )
    assert proposal.item_story_refs == {
        item.item_id: expected for item in compiled.plan.items
    }
    assert parse_reviewed_plan_yaml(compiled.files[compiled.plan_path]) == compiled.plan
    for item in compiled.plan.items:
        context = yaml.safe_load(compiled.files[item.workflow_file])["inputs"][
            "proposed_pr_context"
        ]
        assert context["story_refs"] == [expected[0].to_data()]
        assert context["story_validation"] == item.story_validation
    data = compiled.plan.planning
    assert validate_compiled_plan(
        compiled, {"source": data["source"], "base": data["base"]}
    ).valid
    assert machine.story_refs == expected


def test_compiler_rejects_lost_story_refs_and_missing_integration_dependency(
    compiled_story_plan: tuple[Any, Any, Any],
) -> None:
    compiled, proposal, _ = compiled_story_plan
    manifest = PlanningManifest.from_data(compiled.plan.planning)
    snapshot = {"source": manifest.data["source"], "base": manifest.data["base"]}
    for items, message in (
        (
            (replace(compiled.plan.items[0], story_refs=()), *compiled.plan.items[1:]),
            "allocations differ",
        ),
        (
            (
                *compiled.plan.items[:-1],
                replace(compiled.plan.items[-1], dependencies=()),
            ),
            "omits a work-unit dependency",
        ),
    ):
        with pytest.raises(PlanningCompilationError, match=message):
            compile_plan(
                plan=replace(compiled.plan, items=items),
                planning_manifest=manifest,
                repository_snapshot=snapshot,
                structrr_proposal=proposal,
                verification_command="make test",
                plan_path=compiled.plan_path,
            )
    files = dict(compiled.files)
    item = compiled.plan.items[0]
    workflow = yaml.safe_load(files[item.workflow_file])
    workflow["inputs"]["proposed_pr_context"]["story_refs"] = []
    files[item.workflow_file] = yaml.safe_dump(workflow)
    report = validate_compiled_plan(replace(compiled, files=files), snapshot)
    assert not report.valid and any(
        "story allocation" in error for error in report.errors
    )


def test_source_requires_current_approved_graph_and_retains_case_revision(
    graph: dict[str, Any],
) -> None:
    ref = pin_story(graph, STORY)
    assert source_story_refs(yaml.safe_dump(graph), graph) == (ref,)
    assert parse_story_refs([ref.to_data()]) == (ref,)
    draft = copy.deepcopy(graph)
    draft["user_stories"][0].pop("confirmation")
    with pytest.raises(ValueError, match="confirmation"):
        source_story_refs(yaml.safe_dump(draft), draft)
    changed = copy.deepcopy(graph)
    changed["user_story_cases"][0]["initial_conditions"] = [
        "The checkpoint is readable."
    ]
    with pytest.raises(StoryAllocationError, match="case revision"):
        validate_story_refs((ref,), changed)
    assert pin_story(changed, STORY).contract_digest == ref.contract_digest
    with pytest.raises(StoryAllocationError, match="conflicting case"):
        merge_story_refs([(ref,), (pin_story(changed, STORY),)])


def split_entry(graph: dict[str, Any]) -> dict[str, Any]:
    story = copy.deepcopy(graph["user_stories"][0])
    story["entry_step_id"] = STORY + ":step:5"
    story["identity_state"]["next_step"] = 7
    story["identity_state"]["next_transition"] = 5
    story["identity_state"]["retired_ids"] = [STORY + ":step:1"]
    story["confirmation"] = None
    original = graph["user_story_steps"][0]
    action1 = {
        **copy.deepcopy(original),
        "id": STORY + ":step:5",
        "description": "Select the session to resume.",
    }
    action2 = {
        **copy.deepcopy(original),
        "id": STORY + ":step:6",
        "description": "Request resumption of the selected session.",
    }
    edge = {
        **copy.deepcopy(graph["user_story_transitions"][0]),
        "id": STORY + ":transition:4",
        "from_step_id": action1["id"],
        "to_step_id": action2["id"],
    }
    candidate = project_sections(
        graph,
        {
            "user_stories": [{"id": STORY, "action": "change", "content": story}],
            "user_story_steps": [
                {"id": original["id"], "action": "remove"},
                *[
                    {"id": item["id"], "action": "add", "content": item}
                    for item in (action1, action2)
                ],
            ],
            "user_story_transitions": [
                {
                    "id": STORY + ":transition:1",
                    "action": "change",
                    "content": {
                        "id": STORY + ":transition:1",
                        "from_step_id": action2["id"],
                    },
                },
                {"id": edge["id"], "action": "add", "content": edge},
            ],
            "user_story_cases": [
                {
                    "id": case["id"],
                    "action": "change",
                    "content": {
                        "id": case["id"],
                        "transition_ids": [edge["id"], *case["transition_ids"]],
                    },
                }
                for case in graph["user_story_cases"]
            ],
        },
    )
    return confirm(candidate)


def test_step_split_reports_impact_and_keeps_old_receipts_historical(
    graph: dict[str, Any],
) -> None:
    ref = pin_story(graph, STORY)
    receipt = {"id": "receipt:old", "status": "passed", "story_refs": [ref.to_data()]}
    candidate = split_entry(graph)
    impact = story_amendment_impact(graph, candidate, {"pr-1": (ref,)}, (receipt,))
    assert impact["affected_plan_items"] == {"pr-1": [STORY]}
    assert set(impact["changed_subjects"]["user_story_steps"]) == {
        STORY + ":step:1",
        STORY + ":step:5",
        STORY + ":step:6",
    }
    assert impact["route"] == "story-design-interview"
    assert impact["historical_receipts"][0]["status"] == "stale"
    assert impact["historical_receipts"][0]["historical_receipt"] == receipt
    assert receipt["status"] == "passed"
    with pytest.raises(StoryAllocationError, match="conflict"):
        validate_story_refs((ref,), candidate)
    validate_story_refs((pin_story(candidate, STORY),), candidate)


def test_issue_worker_blocks_concurrent_story_edit_with_concrete_impact(
    compiled_story_plan: tuple[Any, Any, Any],
    graph: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compiled, proposal, _ = compiled_story_plan
    path = tmp_path / "proposal.json"
    write_multi_pr_proposal(path, proposal)
    current = split_entry(graph)
    monkeypatch.setattr(
        task_agent,
        "_load_committed_issue_worker_structrr_baseline",
        lambda *a, **kw: (graph, None),
    )
    monkeypatch.setattr(
        task_agent,
        "_bootstrap_issue_worker_structrr_baseline",
        lambda *a, **kw: (current, None),
    )
    monkeypatch.setattr(
        task_agent, "_preflight_proposed_file_additions", lambda *a, **kw: ()
    )
    item = compiled.plan.items[0]
    context = {
        "plan_id": compiled.plan.plan_id,
        "structrr_proposal_file": "proposal.json",
        "item_id": item.item_id,
        "story_refs": [ref.to_data() for ref in item.story_refs],
    }
    with pytest.raises(
        task_agent.PowdrrExecutionError, match="story-design-interview"
    ) as caught:
        task_agent._issue_worker_structrr_context(
            tmp_path, context, base_commit="a" * 40
        )
    assert STORY + ":step:1" in str(caught.value)
    assert "affected_plan_items" in str(caught.value)


def test_context_is_scoped_and_dependency_changes_are_not_silently_reused(
    graph: dict[str, Any],
) -> None:
    graph["invariants"] = [
        {"id": "invariant:preserve", "description": "Preserve accepted answers."}
    ]
    graph["user_stories"][0]["invariant_refs"] = ["invariant:preserve"]
    confirm(graph)
    ref = pin_story(graph, STORY)
    current = copy.deepcopy(graph)
    current["entities"] = [{"id": "unrelated", "name": "Unrelated addition"}]
    validate_story_refs((ref,), current)
    assert "unrelated" not in json.dumps(story_planning_context(current, (ref,)))
    current["invariants"][0]["description"] = "Discard previous answers."
    with pytest.raises(StoryAllocationError, match="context conflict"):
        validate_story_refs((ref,), current)
    impact = story_amendment_impact(graph, current, {"pr": (ref,)})
    assert impact["affected_plan_items"] == {"pr": [STORY]}


def test_single_feature_execution_uses_same_confirmation_gate(
    graph: dict[str, Any],
) -> None:
    from powdrr_service.planning.story_allocation import story_proposal_context
    from powdrr_service.structrr.proposal import compile_proposal_revision

    source = {
        section: [
            {"id": record["id"], "action": "added", "content": record}
            for record in records
        ]
        for section, records in graph.items()
    }
    proposal = compile_proposal_revision(
        "feature",
        {},
        source,
        acceptance_criteria=("Resume works.",),
        must_preserve=(),
        non_goals=(),
        allowed_paths=(".",),
        source_refs=("review:fixture:1",),
    )
    context = story_proposal_context({}, source, proposal, base_commit="a" * 40)
    assert context["story_refs"] == [pin_story(graph, STORY).to_data()]
    assert context["coverage_status"] == "planned"
    draft = copy.deepcopy(source)
    draft["user_stories"][0]["content"]["confirmation"] = None
    proposal = compile_proposal_revision(
        "feature",
        {},
        draft,
        acceptance_criteria=("Resume works.",),
        must_preserve=(),
        non_goals=(),
        allowed_paths=(".",),
        source_refs=("review:fixture:1",),
    )
    with pytest.raises(StoryAllocationError, match="confirmation"):
        story_proposal_context({}, draft, proposal, base_commit="a" * 40)


def test_repair_preserves_story_pins_and_cannot_approve_behavior(
    compiled_story_plan: tuple[Any, Any, Any],
) -> None:
    from powdrr_service.planning.repair import RepairDraft
    from powdrr_service.planning.repair_service import (
        PlanRepairServiceError,
        _compile_artifacts,
        _planned_item,
        apply_repair_request,
    )

    compiled, _, _ = compiled_story_plan
    original = compiled.plan.items[0]
    replacement = _planned_item(
        original.item_id,
        {
            "title": "Correct the resume deliverable title",
            "objective": original.objective,
            "scope": original.scope,
            "acceptance_criteria": original.acceptance_criteria,
        },
        original,
    )
    assert replacement.story_refs == original.story_refs
    plan = replace(compiled.plan, items=(replacement, *compiled.plan.items[1:]))
    draft = RepairDraft(compiled.plan, compiled.plan.planning, compiled.files)
    artifacts = _compile_artifacts(draft, plan, compiled.plan.planning)
    assert (
        parse_reviewed_plan_yaml(artifacts[compiled.plan_path]).items[0].story_refs
        == original.story_refs
    )
    manifest = copy.deepcopy(dict(compiled.plan.planning))
    manifest["units"][0].pop("story_refs")
    manifest = PlanningManifest.create(
        {
            key: value
            for key, value in manifest.items()
            if key not in {"schema", "semantic_revision_digest"}
        }
    ).to_data()
    with pytest.raises(ValueError, match="dropped or changed"):
        _compile_artifacts(draft, plan, manifest)
    request = {
        "discovery": {
            "kind": "design_amendment_needed",
            "summary": "Change the failure outcome.",
            "requirement_ids": [],
            "item_ids": [original.item_id],
            "evidence_ids": [],
            "requested_outcome": "Ask the user to approve the changed story.",
        },
        "subjects": [],
        "operations": [],
        "replacement_items": {},
        "replacement_units": [],
        "replacement_requirements": {},
    }
    with pytest.raises(PlanRepairServiceError, match="story-design-interview"):
        apply_repair_request(draft, request)


def test_related_story_amendment_and_removal_conflict_but_unrelated_story_does_not(
    graph: dict[str, Any],
) -> None:
    other = json.loads(json.dumps(graph).replace(STORY, "story:other"))
    other["user_stories"][0]["confirmation"]["contract_digest"] = story_contract_digest(
        parse_story_catalog(other), "story:other"
    )
    baseline = {section: records + other[section] for section, records in graph.items()}
    root_ref = pin_story(baseline, STORY)
    amended = copy.deepcopy(baseline)
    amended["user_stories"][1]["title"] = "Another unrelated story"
    amended["user_stories"][1]["confirmation"]["contract_digest"] = (
        story_contract_digest(parse_story_catalog(amended), "story:other")
    )
    validate_story_refs((root_ref,), amended)
    baseline["user_stories"][0]["related_story_refs"] = [
        {"story_id": "story:other", "relationship": "uses"}
    ]
    confirm(baseline)
    linked = pin_story(baseline, STORY)
    amended["user_stories"][0] = copy.deepcopy(baseline["user_stories"][0])
    with pytest.raises(StoryAllocationError, match="context conflict"):
        validate_story_refs((linked,), amended)
    with pytest.raises(StoryAllocationError, match="removed or is unknown"):
        validate_story_refs((root_ref,), {})


def test_approved_split_can_be_replanned_with_fresh_refs(
    graph: dict[str, Any], tmp_path: Path
) -> None:
    amended = split_entry(graph)
    compiled, proposal, _ = build_story_plan(amended, tmp_path)
    refs = compiled.plan.items[-1].story_refs
    assert STORY + ":step:1" not in refs[0].step_ids
    assert {STORY + ":step:5", STORY + ":step:6"} <= set(refs[0].step_ids)
    assert proposal.item_story_refs[compiled.plan.items[-1].item_id] == refs
    assert refs[0].contract_digest != pin_story(graph, STORY).contract_digest


def test_invalid_or_shadowed_source_story_declarations_are_not_silently_dropped(
    graph: dict[str, Any],
) -> None:
    with pytest.raises(StoryAllocationError, match="shadowing"):
        source_story_refs(yaml.safe_dump({**graph, "story_refs": []}), graph)
    with pytest.raises(StoryAllocationError, match="valid YAML"):
        source_story_refs("# Plan\n\nuser_stories: [\n", graph)
    case = graph["user_story_cases"][0]
    source = {
        "user_story_cases": [{"id": case["id"], "action": "changed", "content": case}]
    }
    assert source_story_refs(yaml.safe_dump(source), graph) == (
        pin_story(graph, STORY),
    )
