from __future__ import annotations

import copy
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml
from powdrr_service.structrr.actual_diff import compile_actual_structrr_diff
from powdrr_service.structrr.bootstrap import (
    bootstrap_structrr,
    validate_bootstrap_sections,
)
from powdrr_service.structrr.design import DesignHandoff, DesiredState, project_sections
from powdrr_service.structrr.proposal import compile_proposal_revision
from powdrr_service.structrr.rebase import rebase_structrr_snapshot
from powdrr_service.structrr.snapshot import snapshot_digest
from powdrr_service.structrr.user_stories import (
    STORY_SECTIONS,
    AllocatedStoryEdit,
    StoryContractError,
    StoryEditRequest,
    allocate_story_edit,
    apply_story_edits,
    case_digest,
    parse_story_catalog,
    story_contract_digest,
    validate_story_catalog,
)

STORY = "story:session-resume"


@pytest.fixture
def graph() -> dict[str, Any]:
    return yaml.safe_load(
        (
            Path(__file__).parent / "fixtures/user_stories/session-resume.yaml"
        ).read_text()
    )


def test_graph_round_trip_and_external_references(graph: dict[str, Any]) -> None:
    catalog = parse_story_catalog(graph)
    assert validate_story_catalog(catalog).passed
    assert parse_story_catalog(json.loads(json.dumps(catalog.to_data()))) == catalog
    assert set(catalog.observations_by_id) == {
        f"{STORY}:observation:1",
        f"{STORY}:observation:2",
    }
    assert validate_story_catalog(
        catalog,
        {"source_refs": ["interview:resume:answer:1", "interview:resume:answer:2"]},
    ).passed
    report = validate_story_catalog(catalog, {})
    assert {i.code for i in report.issues} == {"story_source_unresolved"}
    assert len(report.unmapped_step_ids) == 4


def test_canonical_hashes_preserve_walk_order(graph: dict[str, Any]) -> None:
    before = parse_story_catalog(graph)
    reordered = copy.deepcopy(graph)
    for section in STORY_SECTIONS:
        reordered[section].reverse()
    after = parse_story_catalog(reordered)
    assert story_contract_digest(before, STORY) == story_contract_digest(after, STORY)
    assert snapshot_digest(graph) == snapshot_digest(reordered)
    assert case_digest(before, f"{STORY}:case:1") == case_digest(
        after, f"{STORY}:case:1"
    )
    reordered["user_story_cases"][0]["transition_ids"].reverse()
    changed = parse_story_catalog(reordered)
    assert story_contract_digest(before, STORY) == story_contract_digest(changed, STORY)
    assert case_digest(before, f"{STORY}:case:2") != case_digest(
        changed, f"{STORY}:case:2"
    )
    assert snapshot_digest(graph) != snapshot_digest(reordered)


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (
            lambda g: g["user_story_steps"].append(g["user_story_steps"][0]),
            "story_duplicate_id",
        ),
        (
            lambda g: g["user_stories"][0].update(entry_step_id="missing"),
            "story_entry_missing",
        ),
        (
            lambda g: g["user_story_steps"][0].update(story_id="story:absent"),
            "story_owner_missing",
        ),
        (
            lambda g: g["user_story_transitions"][0].update(to_step_id="missing"),
            "story_endpoint_invalid",
        ),
        (
            lambda g: g["user_story_transitions"][0].update(
                from_step_id=f"{STORY}:step:3"
            ),
            "story_outcome_invalid",
        ),
        (
            lambda g: g["user_story_steps"][0].update(kind="decision"),
            "story_step_fields_invalid",
        ),
        (
            lambda g: g["user_story_cases"][0]["transition_ids"].reverse(),
            "story_case_disconnected",
        ),
        (
            lambda g: g["user_story_cases"][0].update(expected_observations=[]),
            "story_case_observation_missing",
        ),
        (
            lambda g: g["user_stories"][0]["identity_state"].update(next_step=4),
            "story_counter_invalid",
        ),
        (
            lambda g: g["user_stories"][0]["identity_state"]["retired_ids"].append(
                f"{STORY}:step:1"
            ),
            "story_id_retired",
        ),
    ],
)
def test_graph_errors(graph: dict[str, Any], mutate: Any, code: str) -> None:
    mutate(graph)
    assert code in {
        i.code for i in validate_story_catalog(parse_story_catalog(graph)).issues
    }


@pytest.mark.parametrize(
    "patch",
    [
        {"unknown": "field"},
        {"title": "TBD"},
        {"title": ""},
        {"title": 42},
        {"identity_state": {"next_step": True}},
    ],
)
def test_strict_records(graph: dict[str, Any], patch: dict[str, Any]) -> None:
    graph["user_stories"][0].update(patch)
    with pytest.raises(StoryContractError):
        parse_story_catalog(graph)


def test_confirmation_digest_is_exact(graph: dict[str, Any]) -> None:
    digest = story_contract_digest(parse_story_catalog(graph), STORY)
    graph["user_stories"][0]["confirmation"] = {
        "contract_digest": digest,
        "decision_ref": "answer:3",
        "authority": "user_confirmation",
    }
    assert validate_story_catalog(parse_story_catalog(graph)).passed
    graph["user_stories"][0]["title"] = "Resume the same session"
    assert "story_confirmation_stale" in {
        i.code for i in validate_story_catalog(parse_story_catalog(graph)).issues
    }


def test_atomic_replace_rewire_and_retirement(graph: dict[str, Any]) -> None:
    replacement = copy.deepcopy(graph["user_story_steps"][0])
    replacement["id"] = f"{STORY}:step:5"
    state = copy.deepcopy(graph["user_stories"][0]["identity_state"])
    state.update(next_step=6, retired_ids=[f"{STORY}:step:1"])
    candidate = project_sections(
        graph,
        {
            "user_stories": [
                {
                    "id": STORY,
                    "action": "change",
                    "entry_step_id": replacement["id"],
                    "identity_state": state,
                }
            ],
            "user_story_steps": [
                {"id": f"{STORY}:step:1", "action": "remove"},
                {"id": replacement["id"], "action": "add", "content": replacement},
            ],
            "user_story_transitions": [
                {
                    "id": f"{STORY}:transition:1",
                    "action": "change",
                    "from_step_id": replacement["id"],
                }
            ],
        },
    )
    assert validate_story_catalog(parse_story_catalog(candidate)).passed
    assert graph["user_stories"][0]["entry_step_id"] == f"{STORY}:step:1"
    with pytest.raises(StoryContractError, match="story_retirement_missing"):
        project_sections(
            graph, {"user_story_steps": [{"id": f"{STORY}:step:1", "action": "remove"}]}
        )
    with pytest.raises(StoryContractError, match="story_id_reused"):
        project_sections(
            candidate,
            {
                "user_story_steps": [
                    {
                        "id": graph["user_story_steps"][0]["id"],
                        "action": "add",
                        "content": graph["user_story_steps"][0],
                    }
                ]
            },
        )


def test_checkpoint_edit_replay_and_stale_input(graph: dict[str, Any]) -> None:
    catalog = parse_story_catalog(graph)
    edit = allocate_story_edit(
        catalog,
        StoryEditRequest(
            "user_story_steps",
            "change",
            STORY,
            {"description": "The maintainer requests resume for a persisted session."},
            f"{STORY}:step:1",
        ),
        "edit:1",
    )
    replay = AllocatedStoryEdit.from_data(json.loads(json.dumps(edit.to_data())))
    candidate = apply_story_edits(catalog, [replay], catalog.fingerprint)
    assert candidate.steps_by_id[f"{STORY}:step:1"].description.endswith(
        "persisted session."
    )
    assert apply_story_edits(catalog, [edit], catalog.fingerprint) == candidate
    with pytest.raises(StoryContractError, match="story_snapshot_stale"):
        apply_story_edits(candidate, [edit], catalog.fingerprint)


def test_old_documents_and_versions() -> None:
    assert not parse_story_catalog({}).user_stories
    assert validate_story_catalog(parse_story_catalog({})).passed
    with pytest.raises(StoryContractError, match="story_version_invalid"):
        parse_story_catalog({"section_versions": {"user_stories": 2}})
    assert not any(
        "user_stor" in (i.path or "")
        for i in validate_bootstrap_sections({"section_versions": {}})
    )


def test_story_proposal_portable_handoff(graph: dict[str, Any]) -> None:
    baseline: dict[str, Any] = {"entities": [], "entity_relationships": []}
    plan: dict[str, Any] = {
        section: [
            {"id": record["id"], "action": "add", "content": record}
            for record in graph[section]
        ]
        for section in STORY_SECTIONS
    }
    proposal = compile_proposal_revision(
        "story-design",
        baseline,
        plan,
        acceptance_criteria=[],
        must_preserve=[],
        non_goals=[],
        allowed_paths=["docs"],
        source_refs=["review:1"],
    )
    handoff = DesignHandoff.compile(baseline, proposal, base_commit="abc123")
    loaded = DesignHandoff.from_data(json.loads(json.dumps(handoff.to_data())))
    desired = DesiredState.from_data(loaded.to_data()["desired"])
    assert parse_story_catalog(desired.document) == parse_story_catalog(graph)
    assert story_contract_digest(
        parse_story_catalog(desired.document), STORY
    ) == story_contract_digest(parse_story_catalog(graph), STORY)


def test_rebase_story_subject_conflict(graph: dict[str, Any]) -> None:
    changed = copy.deepcopy(graph)
    changed["user_story_steps"][0]["description"] = "Request session recovery."
    report = rebase_structrr_snapshot(
        graph, changed, referenced_story_subject_ids=[f"{STORY}:step:1"]
    )
    assert report.classification == "conflicted"
    assert report.conflicts[0].area == "user_story_steps"
    assert rebase_structrr_snapshot(graph, changed).clean


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def story_repo(tmp_path: Path, graph: dict[str, Any]) -> Path:
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "tests@example.com")
    _git(tmp_path, "config", "user.name", "Tests")
    (tmp_path / "app.py").write_text("def resume():\n    return 'pending'\n")
    taxonomy = Path(__file__).parents[1] / "software_development_entity_taxonomy.md"
    (tmp_path / taxonomy.name).write_text(taxonomy.read_text())
    docs = tmp_path / "docs/current/product"
    docs.mkdir(parents=True)
    spec = docs / "architecture-specification.yaml"
    spec.write_text(
        yaml.safe_dump(
            {
                "schema": "https://powdrr.io/schemas/specification-v1",
                "id": "product",
                **graph,
            }
        )
    )
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "Record desired behavior")
    return tmp_path


def test_real_bootstrap_projection_reload_and_declaration_diff(
    story_repo: Path, graph: dict[str, Any]
) -> None:
    tmp_path = story_repo
    baseline = bootstrap_structrr(tmp_path, output_path=tmp_path / "baseline.yaml")
    assert baseline.validation.successful
    assert parse_story_catalog(baseline.document) == parse_story_catalog(graph)
    candidate = project_sections(
        baseline.document,
        {
            "user_story_steps": [
                {
                    "id": f"{STORY}:step:1",
                    "action": "change",
                    "description": "The maintainer requests the existing session.",
                }
            ]
        },
    )
    assert parse_story_catalog(
        yaml.safe_load(yaml.safe_dump(candidate))
    ) == parse_story_catalog(candidate)
    assert baseline.source_manifest is not None
    diff = compile_actual_structrr_diff(
        baseline.document, candidate, baseline.source_manifest, baseline.source_manifest
    )
    assert not diff.structural_operations
    assert not diff.behavioral_review_candidates
    assert any(
        item.get("area") == "user_story_steps" for item in diff.declaration_changes
    )
    rebuilt = bootstrap_structrr(tmp_path, output_path=tmp_path / "rebuilt.yaml")
    assert parse_story_catalog(rebuilt.document) == parse_story_catalog(graph)


def test_allocator_batches_new_graph_and_replays_checkpoint(
    graph: dict[str, Any],
) -> None:
    from powdrr_service.structrr.user_stories import StoryCatalog

    empty = StoryCatalog()
    content = {
        k: v
        for k, v in graph["user_stories"][0].items()
        if k not in {"id", "identity_state", "entry_step_id"}
    }
    created = allocate_story_edit(
        empty, StoryEditRequest("user_stories", "add", "", content), "draft:story:1"
    )
    story_id = created.subject_id
    edits = [created]
    action = allocate_story_edit(
        empty,
        StoryEditRequest(
            "user_story_steps",
            "add",
            story_id,
            {
                "kind": "user_action",
                "description": "The maintainer requests resume.",
                "source_refs": ["answer:1"],
                "action": {
                    "actor": "Maintainer",
                    "operation": "Resume",
                    "input": "Session ID",
                },
            },
        ),
        "draft:step:1",
        pending_edits=edits,
    )
    edits.append(action)
    outcome_request = StoryEditRequest(
        "user_story_steps",
        "add",
        story_id,
        {
            "kind": "outcome",
            "description": "Publish the resumed plan.",
            "outcome": "success",
            "source_refs": ["answer:1"],
            "expected_observations": [
                {
                    "description": "The published plan retains the decisions.",
                    "boundary": "Published plan",
                }
            ],
        },
    )
    outcome = allocate_story_edit(
        empty, outcome_request, "draft:step:2", pending_edits=edits
    )
    edits.append(outcome)
    assert outcome.subject_id == story_id + ":step:2"
    assert (
        allocate_story_edit(empty, outcome_request, "draft:step:2", pending_edits=edits)
        == outcome
    )
    with pytest.raises(StoryContractError, match="story_checkpoint_conflict"):
        allocate_story_edit(
            empty,
            StoryEditRequest(
                "user_story_steps", "remove", story_id, {}, outcome.subject_id
            ),
            "draft:step:2",
            pending_edits=edits,
        )
    transition = allocate_story_edit(
        empty,
        StoryEditRequest(
            "user_story_transitions",
            "add",
            story_id,
            {
                "from_step_id": action.subject_id,
                "to_step_id": outcome.subject_id,
                "condition": {"kind": "always", "description": "Resume requested."},
                "source_refs": ["answer:1"],
            },
        ),
        "draft:edge:1",
        pending_edits=edits,
    )
    edits.append(transition)
    candidate = apply_story_edits(
        empty,
        [
            AllocatedStoryEdit.from_data(json.loads(json.dumps(e.to_data())))
            for e in edits
        ],
        empty.fingerprint,
    )
    assert validate_story_catalog(candidate).passed
    assert candidate.stories_by_id[story_id].identity_state.next_step == 3
    assert candidate.stories_by_id[story_id].identity_state.next_observation == 2
    assert set(candidate.observations_by_id) == {story_id + ":observation:1"}
    with pytest.raises(StoryContractError, match="story_id_reused"):
        allocate_story_edit(
            empty,
            StoryEditRequest("user_stories", "add", "", content),
            "draft:story:1",
            accepted_history_ids=frozenset({story_id}),
        )


def test_disjoint_merge_and_removed_endpoint_conflict(graph: dict[str, Any]) -> None:
    from powdrr_service.structrr.user_stories import merge_story_catalogs

    current, desired = copy.deepcopy(graph), copy.deepcopy(graph)
    current["user_story_steps"][0]["description"] = (
        "The maintainer submits a resume request."
    )
    desired["user_story_steps"][2]["description"] = (
        "Publish the remaining plan with accepted answers."
    )
    merged = merge_story_catalogs(
        parse_story_catalog(graph),
        parse_story_catalog(current),
        parse_story_catalog(desired),
    )
    assert (
        merged.steps_by_id[f"{STORY}:step:1"].description
        == current["user_story_steps"][0]["description"]
    )
    assert (
        merged.steps_by_id[f"{STORY}:step:3"].description
        == desired["user_story_steps"][2]["description"]
    )
    desired["user_story_steps"][0]["description"] = "A different request."
    with pytest.raises(StoryContractError, match="story_merge_conflict"):
        merge_story_catalogs(
            parse_story_catalog(graph),
            parse_story_catalog(current),
            parse_story_catalog(desired),
        )


def test_changelog_retains_nested_step_action(graph: dict[str, Any]) -> None:
    from powdrr_service.changelog import parse_change_log
    from powdrr_service.structrr.user_stories import parse_story_changes

    document: dict[str, Any] = {
        "schema": "https://powdrr.io/schema/changelog-v2",
        **{
            section: [
                {"id": record["id"], "action": "added", "content": record}
                for record in graph[section]
            ]
            for section in STORY_SECTIONS
        },
    }
    parsed = parse_change_log(yaml.safe_dump(document))
    assert len(parsed.story_changes) == 10
    projected = project_sections(
        {},
        {
            section: [c.to_data() for c in parsed.story_changes if c.section == section]
            for section in STORY_SECTIONS
        },
    )
    assert parse_story_catalog(projected) == parse_story_catalog(graph)
    with pytest.raises(StoryContractError, match="story_unknown_field"):
        parse_story_changes(
            {
                "user_story_steps": [
                    {
                        "id": "step:1",
                        "action": "change",
                        "content": {"unknown": "field"},
                    }
                ]
            }
        )


def test_reviewed_history_survives_bootstrap_and_index_cache(story_repo: Path) -> None:
    from powdrr_lift.core.code_index import CodeIndexStore
    from powdrr_lift.core.codebase_state import build_current_state_specification_report
    from powdrr_lift.core.spec_context import gather_specification_context

    _git(story_repo, "checkout", "-qb", "feature/story-amendment")
    history = story_repo / "docs/changelogs/PR-1-changelog.yaml"
    history.parent.mkdir(parents=True)
    history.write_text(
        yaml.safe_dump(
            {
                "schema": "https://powdrr.io/schema/changelog-v2",
                "change_id": "1",
                "title": "Clarify resume action",
                "structured_files": [],
                "files": [],
                "user_story_steps": [
                    {
                        "id": f"{STORY}:step:1",
                        "action": "changed",
                        "content": {
                            "description": "Request resume for the persisted session."
                        },
                    }
                ],
            }
        )
    )
    _git(story_repo, "add", "docs/changelogs")
    _git(story_repo, "commit", "-qm", "Clarify resume (#1)")
    bootstrap = bootstrap_structrr(story_repo, output_path=story_repo / "baseline.yaml")
    assert bootstrap.validation.successful
    assert (
        parse_story_catalog(bootstrap.document)
        .steps_by_id[f"{STORY}:step:1"]
        .description
        == "Request resume for the persisted session."
    )
    store = CodeIndexStore(story_repo)
    first = store.refresh("feature/story-amendment", "main")
    reloaded = CodeIndexStore(story_repo).refresh("feature/story-amendment", "main")
    assert (
        first.documents[0].changelog.story_changes
        == reloaded.documents[0].changelog.story_changes
    )
    assert reloaded.documents[0].changelog.story_changes[0].id == f"{STORY}:step:1"
    current = build_current_state_specification_report(
        "feature/story-amendment", parent_branch="main", repo_root=story_repo
    )
    assert (
        parse_story_catalog(current).steps_by_id[f"{STORY}:step:1"].description
        == "Request resume for the persisted session."
    )
    matches = gather_specification_context(
        story_repo, types=["user_stories"], keywords=["planning"]
    )
    assert any(match.item["id"] == STORY for match in matches.matches)


def test_loops_keep_repeated_transition_order(graph: dict[str, Any]) -> None:
    graph["user_story_steps"].pop()
    graph["user_story_transitions"][2]["to_step_id"] = f"{STORY}:step:1"
    graph["user_story_transitions"][2]["condition"]["description"] = (
        "Retry reading the unavailable checkpoint."
    )
    graph["user_stories"][0]["identity_state"]["retired_ids"] = [
        f"{STORY}:step:4",
        f"{STORY}:observation:2",
    ]
    case = graph["user_story_cases"][1]
    case["transition_ids"] = [f"{STORY}:transition:{i}" for i in [1, 3, 1, 2]]
    case["expected_observations"] = [f"{STORY}:observation:1"]
    catalog = parse_story_catalog(graph)
    assert validate_story_catalog(catalog).passed
    assert (
        catalog.user_story_cases[1].transition_ids.count(f"{STORY}:transition:1") == 2
    )


def test_normalization_and_specification_validation_keep_story_contract(
    graph: dict[str, Any],
) -> None:
    from powdrr_service.specifications.common import normalize_specification_v1_yaml
    from powdrr_service.structrr.user_story_validation import validate_story_document

    normalized = yaml.safe_load(normalize_specification_v1_yaml(yaml.safe_dump(graph)))
    assert parse_story_catalog(normalized) == parse_story_catalog(graph)
    assert validate_story_document(graph).passed
    assert not validate_story_document({"user_stories": None}).passed
    assert not validate_story_document(
        {"user_stories": [{"id": STORY, "action": "invented"}]}
    ).passed
    assert validate_story_document(
        {
            "user_stories": [
                {
                    "id": STORY,
                    "action": "changed",
                    "content": {"title": "Resume planning"},
                }
            ]
        }
    ).passed


def test_multi_pr_projection_retains_story_sections(
    tmp_path: Path, graph: dict[str, Any]
) -> None:
    from powdrr_service.planning.multipr_structrr import (
        compile_multi_pr_proposal,
        load_multi_pr_proposal,
    )

    operations = [
        {
            "operation_id": "add:" + record["id"],
            "section": section,
            "subject_id": record["id"],
            "item_id": "story-contract",
            "action": "added",
            "content": record,
        }
        for section in STORY_SECTIONS
        for record in graph[section]
    ]
    proposal = compile_multi_pr_proposal(
        plan_id="stories-v1",
        baseline_snapshot={},
        items=[
            {
                "id": "story-contract",
                "acceptance_criteria": ["Persist the desired behavior."],
                "allowed_paths": ["docs"],
            }
        ],
        proposed_operations=operations,
        source_ref="review:1",
    )
    path = tmp_path / "proposal.json"
    path.write_text(json.dumps(proposal.to_data()))
    loaded = load_multi_pr_proposal(path)
    assert parse_story_catalog(loaded.expected_final_snapshot) == parse_story_catalog(
        graph
    )
    assert loaded.full_proposal.fingerprint == proposal.full_proposal.fingerprint


def test_amendment_pins_expected_before(graph: dict[str, Any]) -> None:
    from powdrr_service.decision_obligation import content_fingerprint

    subject = f"{STORY}:step:1"
    plan = {
        "user_story_steps": [
            {
                "id": subject,
                "action": "changed",
                "content": {
                    "id": subject,
                    "description": "Resume the persisted session.",
                },
            }
        ]
    }
    proposal = compile_proposal_revision(
        "amendment",
        graph,
        plan,
        acceptance_criteria=[],
        must_preserve=[],
        non_goals=[],
        allowed_paths=["docs"],
        source_refs=["review:1"],
    )
    assert proposal.operations[0].expected_before == content_fingerprint(
        graph["user_story_steps"][0]
    )
    assert DesignHandoff.compile(graph, proposal, base_commit="abc123")
    with pytest.raises(ValueError, match="stale story expected-before"):
        project_sections(
            graph,
            {
                "user_story_steps": [
                    {
                        **plan["user_story_steps"][0],
                        "expected_before": "sha256:" + "0" * 64,
                    }
                ]
            },
        )


def test_exact_declarations_and_changelist_additions_are_idempotent(
    graph: dict[str, Any],
) -> None:
    from powdrr_service.structrr.user_stories import (
        parse_story_changes,
        project_story_changes,
    )

    changes = parse_story_changes(
        {
            section: [
                {"id": record["id"], "action": "added", "content": record}
                for record in records
            ]
            for section, records in graph.items()
        }
    )
    assert parse_story_catalog(
        project_story_changes(graph, changes)
    ) == parse_story_catalog(graph)


def test_fixture_value_order_and_compiler_confirmation_invalidation(
    graph: dict[str, Any],
) -> None:
    before = parse_story_catalog(graph)
    graph["user_story_cases"][0]["input_examples"] = [{"scope": ["second", "first"]}]
    with_fixture = parse_story_catalog(graph)
    graph["user_story_cases"][0]["input_examples"][0]["scope"].reverse()
    assert case_digest(with_fixture, f"{STORY}:case:1") != case_digest(
        parse_story_catalog(graph), f"{STORY}:case:1"
    )
    graph["user_stories"][0]["confirmation"] = {
        "contract_digest": story_contract_digest(before, STORY),
        "decision_ref": "answer:confirmation",
        "authority": "user_confirmation",
    }
    catalog = parse_story_catalog(graph)
    edit = allocate_story_edit(
        catalog,
        StoryEditRequest(
            "user_story_steps",
            "change",
            STORY,
            {"description": "Resume planning for a persisted session."},
            f"{STORY}:step:1",
        ),
        "amendment:1",
    )
    changed = apply_story_edits(catalog, [edit], catalog.fingerprint)
    assert changed.stories_by_id[STORY].confirmation is None
    assert story_contract_digest(changed, STORY) != story_contract_digest(before, STORY)
