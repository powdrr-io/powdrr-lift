from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from powdrr_service.structrr.design import DesignHandoff
from powdrr_service.structrr.story_interview import StoryInterview, StoryUserResponse
from powdrr_service.structrr.story_semantic_review import (
    STORY_REVIEW_DIMENSIONS,
    StorySemanticReviewer,
)
from powdrr_service.structrr.user_stories import StoryContractError, StoryEditRequest


class ReviewClient:
    def __init__(self, classification: str = "resolved") -> None:
        self.classification = classification
        self.calls = 0

    def complete_json(
        self, messages: list[dict[str, str]], *, response_schema: Any = None
    ) -> dict[str, Any]:
        self.calls += 1
        if "classification" in response_schema["properties"]:
            return {"classification": self.classification}
        return {
            "explanation": "The supplied source establishes this dimension.",
            "question": "Which outcome should the unavailable checkpoint produce?"
            if self.classification in {"ambiguous", "contradictory"}
            else None,
        }


@pytest.fixture
def graph() -> dict[str, Any]:
    return yaml.safe_load(
        (
            Path(__file__).parent / "fixtures/user_stories/session-resume.yaml"
        ).read_text()
    )


def new_interview() -> StoryInterview:
    return StoryInterview.start(
        "chat-test",
        "resume-planning",
        "Resume interrupted planning without losing accepted decisions.",
        {},
        source_revision="a" * 40,
    )


def add_graph(session: StoryInterview, graph: dict[str, Any]) -> None:
    source_ref = session.status()["source_catalog"][0]["id"]
    story = {
        key: value
        for key, value in graph["user_stories"][0].items()
        if key not in {"id", "identity_state", "entry_step_id"}
    }
    story["source_refs"] = [source_ref]
    session.edit(StoryEditRequest("user_stories", "add", "", story), "story")
    identity = session.story_id
    old = graph["user_stories"][0]["id"]
    for section in ("user_story_steps", "user_story_transitions", "user_story_cases"):
        for index, raw in enumerate(graph[section]):
            record = json.loads(json.dumps(raw).replace(old, identity))
            record.pop("id")
            record.pop("story_id")
            record["source_refs"] = [source_ref]
            if section == "user_story_steps":
                for observation in record.get("expected_observations", []):
                    observation.pop("id")
            session.edit(
                StoryEditRequest(section, "add", identity, record), f"{section}:{index}"
            )


def review_all(
    session: StoryInterview, client: ReviewClient | None = None
) -> ReviewClient:
    client = client or ReviewClient()
    reviewer = StorySemanticReviewer(client, client)
    for dimension in STORY_REVIEW_DIMENSIONS:
        session.review(dimension, reviewer)
    return client


def confirm(session: StoryInterview) -> None:
    question = session.present()
    session.accept_user_response(
        StoryUserResponse(question["question_id"], question["text"], "confirm")
    )


def test_prose_assumption_confirmation_proposal_and_checkpoint_resume(
    graph: dict[str, Any],
) -> None:
    session = new_interview()
    add_graph(session, graph)
    question = session.ask(
        "recovery",
        "Should unavailable checkpoints stop resume?",
        proposed_assumption=(
            "Report the unavailable checkpoint and preserve accepted decisions."
        ),
    )
    session = StoryInterview.from_data(json.loads(json.dumps(session.to_data())))
    response = session.accept_user_response(
        StoryUserResponse(question["question_id"], question["text"], "yes")
    )
    source_ref = response["source_ref"]
    session.edit(
        StoryEditRequest(
            "user_story_steps",
            "change",
            session.story_id,
            {"source_refs": [source_ref]},
            session.story_id + ":step:4",
        ),
        "bind-answer",
    )
    repeated = session.ask(
        "recovery",
        "Should unavailable checkpoints stop resume?",
        proposed_assumption=(
            "Report the unavailable checkpoint and preserve accepted decisions."
        ),
    )
    assert repeated["answered"]
    client = review_all(session)
    assert session.status()["status"] == "needs_confirmation"
    question = session.present()
    assert "otherwise" in question["text"] and "Outcome: failure" in question["text"]
    assert "user's response: yes" in question["text"]
    session = StoryInterview.from_data(json.loads(json.dumps(session.to_data())))
    assert not session.status()["ready"]
    session.accept_user_response(
        StoryUserResponse(question["question_id"], question["text"], "confirm")
    )
    assert session.status()["ready"]
    assert session.present()["answered"]
    before = client.calls
    review_all(session, client)
    assert client.calls == before
    proposal = session.compile_proposal()
    handoff = DesignHandoff.compile({}, proposal, base_commit="a" * 40)
    assert (
        handoff.to_data()["desired"]["document"]["user_stories"][0]["id"]
        == session.story_id
    )
    assert session.to_data()["instruction_ledger"]["source"]["text"].startswith(
        "Resume interrupted"
    )


def test_draft_allocations_resume_without_duplicate_ids(graph: dict[str, Any]) -> None:
    session = new_interview()
    add_graph(session, graph)
    state = session.to_data()
    allocated = state["edits"]["story"]["allocated"]
    request = state["edits"]["story"]["request_fingerprint"]
    restored = StoryInterview.from_data(json.loads(json.dumps(state)))
    assert restored.catalog == session.catalog
    assert restored.to_data()["edits"]["story"]["allocated"] == allocated
    assert restored.to_data()["edits"]["story"]["request_fingerprint"] == request
    with pytest.raises(StoryContractError, match="story_checkpoint_conflict"):
        restored.edit(
            StoryEditRequest("user_stories", "add", "", {"title": "Another story"}),
            "story",
        )


def test_changed_display_cannot_reuse_confirmation(graph: dict[str, Any]) -> None:
    session = new_interview()
    add_graph(session, graph)
    review_all(session)
    question = session.present()
    session.edit(
        StoryEditRequest(
            "user_story_steps",
            "change",
            session.story_id,
            {"description": "Request the persisted planning session."},
            session.story_id + ":step:1",
        ),
        "amend-action",
    )
    with pytest.raises(StoryContractError, match="story_confirmation_stale"):
        session.accept_user_response(
            StoryUserResponse(question["question_id"], question["text"], "confirm")
        )
    assert not session.status()["ready"]
    with pytest.raises(StoryContractError, match="story_not_ready"):
        session.compile_proposal()


@pytest.mark.parametrize("classification", ["ambiguous", "contradictory", "invented"])
def test_review_failures_cannot_be_empty_success(
    graph: dict[str, Any], classification: str
) -> None:
    session = new_interview()
    add_graph(session, graph)
    review_all(session, ReviewClient(classification))
    assert not session.status()["ready"]
    with pytest.raises(StoryContractError, match="story_review_unavailable"):
        session.present()


def test_provider_failure_is_persisted(graph: dict[str, Any]) -> None:
    class FailedClient(ReviewClient):
        def complete_json(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("provider unavailable")

    session = new_interview()
    add_graph(session, graph)
    review = session.review(
        "source_fidelity", StorySemanticReviewer(FailedClient(), FailedClient())
    )
    assert review["findings"][0]["classification"] == "unavailable"
    assert review["findings"][0]["failure_type"] == "RuntimeError"
    assert not session.status()["ready"]


@pytest.mark.parametrize(
    ("policy", "benchmark_mode"), [("normative_default", False), ("clarify", True)]
)
def test_defaults_and_benchmark_cannot_gain_user_authority(
    graph: dict[str, Any], policy: str, benchmark_mode: bool
) -> None:
    session = StoryInterview.start(
        "defaults",
        "resume-planning",
        "Resume planning.",
        {},
        source_revision="a" * 40,
        uncertainty_policy=policy,
        benchmark_mode=benchmark_mode,
    )
    add_graph(session, graph)
    review_all(session)
    with pytest.raises(
        StoryContractError, match="story_confirmation_authority_invalid"
    ):
        session.present()


def test_existing_functionality_story_can_be_amended_without_product_work(
    graph: dict[str, Any],
) -> None:
    session = StoryInterview.start(
        "amend",
        "resume-planning",
        "Document the existing resume behavior.",
        graph,
        source_revision="a" * 40,
        story_mode="amend",
        existing_story_ref="story:session-resume",
    )
    assert not session.status()["ready"]
    review_all(session)
    confirm(session)
    proposal = session.compile_proposal()
    assert all(
        operation.section in {"user_stories"} for operation in proposal.operations
    )
    assert all(operation.action == "change" for operation in proposal.operations)
    assert not any(
        operation.section in {"entities", "files"} for operation in proposal.operations
    )


def test_model_flags_and_checkpoint_tampering_cannot_grant_approval(
    graph: dict[str, Any],
) -> None:
    session = new_interview()
    add_graph(session, graph)
    with pytest.raises(StoryContractError, match="story_authority_invalid"):
        session.edit(
            StoryEditRequest(
                "user_stories",
                "change",
                session.story_id,
                {
                    "confirmation": {
                        "contract_digest": "made-up",
                        "decision_ref": "model",
                        "authority": "user_confirmation",
                    }
                },
                session.story_id,
            ),
            "forge",
        )
    bad = copy.deepcopy(session.to_data())
    bad["confirmation_evidence"] = {"accepted": True}
    with pytest.raises(ValueError, match="fingerprint"):
        StoryInterview.from_data(bad)


def test_current_dependency_changes_block_approval_and_publication(
    graph: dict[str, Any],
) -> None:
    graph["invariants"] = [
        {
            "id": "invariant:retained-decisions",
            "description": "Accepted decisions remain unchanged.",
        }
    ]
    graph["user_stories"][0]["invariant_refs"] = ["invariant:retained-decisions"]
    session = StoryInterview.start(
        "dependencies",
        "resume-planning",
        "Retain accepted decisions while resuming.",
        graph,
        source_revision="a" * 40,
        story_mode="amend",
        existing_story_ref="story:session-resume",
    )
    review_all(session)
    confirm(session)
    unrelated = copy.deepcopy(graph)
    unrelated["entities"] = [{"id": "unrelated", "type": "Function"}]
    session.require_current_dependencies(unrelated)
    assert session.status()["ready"]
    changed = copy.deepcopy(graph)
    changed["invariants"][0]["description"] = "A different preservation constraint."
    with pytest.raises(StoryContractError, match="story_dependency_stale"):
        session.require_current_dependencies(changed)
    assert not session.status()["ready"]
    with pytest.raises(StoryContractError, match="story_not_ready"):
        session.compile_proposal()


def test_scoped_retrieval_reports_incompleteness_and_omits_unrelated_graphs(
    graph: dict[str, Any],
) -> None:
    unrelated = json.loads(
        json.dumps(graph).replace("story:session-resume", "story:billing")
    )
    for section, records in unrelated.items():
        graph[section].extend(records)
    graph["entities"] = [
        {"id": f"function:{index}", "type": "Function"} for index in range(70)
    ]
    session = StoryInterview.start(
        "scoped",
        "resume-planning",
        "Resume a session.",
        graph,
        source_revision="a" * 40,
        story_mode="amend",
        existing_story_ref="story:session-resume",
    )
    request = session.review_request("related_context")
    assert "story:billing" not in json.dumps(request.context)
    partial = session.retrieve_context(("function",))
    assert not partial["complete"] and partial["total_matches"] == 70
    assert partial["corrective_action"]
    selected = session.retrieve_context(("function:69",))
    assert selected["complete"] and len(selected["records"]) == 1


def test_operational_review_retry_retains_failure_history(
    graph: dict[str, Any],
) -> None:
    class FailedClient(ReviewClient):
        def complete_json(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("temporarily unavailable")

    session = new_interview()
    add_graph(session, graph)
    first = session.review(
        "source_fidelity", StorySemanticReviewer(FailedClient(), FailedClient())
    )
    client = ReviewClient()
    second = session.review("source_fidelity", StorySemanticReviewer(client, client))
    assert first["findings"][0]["classification"] == "unavailable"
    assert second["findings"][0]["classification"] == "resolved"
    assert second["attempt"] == 2
    assert len(session.to_data()["review_history"]) == 2
    assert not session.status()["ready"]


def test_derived_case_review_does_not_require_new_user_confirmation(
    graph: dict[str, Any],
) -> None:
    session = new_interview()
    add_graph(session, graph)
    review_all(session)
    confirm(session)
    evidence = session.to_data()["confirmation_evidence"]
    source = session.status()["default_source_ref"]
    identity = session.story_id
    session.edit(
        StoryEditRequest(
            "user_story_cases",
            "add",
            identity,
            {
                "description": "Check the normal resumed plan.",
                "initial_conditions": [],
                "transition_ids": [
                    identity + ":transition:1",
                    identity + ":transition:2",
                ],
                "expected_observations": [identity + ":observation:1"],
                "source_refs": [source],
            },
        ),
        "additional-case",
    )
    assert not session.status()["ready"]
    review_all(session)
    assert session.status()["ready"]
    assert session.to_data()["confirmation_evidence"] == evidence
    assert session.present()["answered"]


def test_answer_redelivery_after_checkpoint_write_is_idempotent(
    graph: dict[str, Any],
) -> None:
    session = new_interview()
    add_graph(session, graph)
    question = session.ask(
        "recovery",
        "Should an unavailable checkpoint stop resume?",
        proposed_assumption="Stop without changing accepted decisions.",
    )
    response = StoryUserResponse(question["question_id"], question["text"], "yes")
    first = session.accept_user_response(response)
    restored = StoryInterview.from_data(json.loads(json.dumps(session.to_data())))
    second = restored.accept_user_response(response)
    assert second["source_ref"] == first["source_ref"]
    assert len(restored.to_data()["answers"]) == 1
    with pytest.raises(StoryContractError, match="story_answer_conflict"):
        restored.accept_user_response(
            StoryUserResponse(question["question_id"], question["text"], "no")
        )
