from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from threading import Barrier, Lock
from typing import Any

from powdrr_service.planning.assembly import PlannedWrite, WorkUnit
from powdrr_service.planning.checkpoints import (
    InMemoryCheckpointStore,
    PlanningCheckpoint,
)
from powdrr_service.planning.contracts import (
    EnumerationAnswer,
    PlanningContractError,
    Question,
)
from powdrr_service.planning.driver import (
    SourceLedgerQuestionMachine,
    _deterministic_block_disposition,
    _is_completed_zero_hit_search,
    _planned_existing_structural_subjects,
)
from powdrr_service.planning.questions import (
    QuestionRequest,
    SourceExcerpt,
    batch_question_requests,
)
from powdrr_service.planning.service import (
    PlanningOptions,
    PlanningSession,
    advance_planning_session,
    create_planning_session,
    load_planning_session,
    run_planning_procedrr,
)
from powdrr_service.planning.source import (
    PinnedSourceDocument,
    Requirement,
    segment_markdown_source,
)

from powdrr_lift.workrr.github_issue_queue import QueueIssue
from powdrr_lift.workrr.planning.evidence import RequirementEvidenceSearch
from procedrr.catalog import ProcessCatalog


class _Client:
    def __init__(self) -> None:
        self.calls = 0

    def complete_json(
        self,
        messages: list[dict[str, str]],
        *,
        response_schema: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.calls += 1
        return {
            "records": [{"statement": "The issue state is durable."}],
            "has_more": False,
            "next_anchor": None,
        }


class _PlanningScriptClient:
    """Provider fixture that emits structured answers for the full plan flow."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def complete_json(
        self,
        messages: list[dict[str, str]],
        *,
        response_schema: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        del response_schema
        packet = json.loads(messages[-1]["content"])
        question = packet["exact_question"]
        self.calls.append(question["kind"])
        if question["kind"] == "planning_question_batch":
            return {
                "answers": {
                    child["exact_question"]["question_id"]: self._answer(child)
                    for item in packet["accepted_facts"]
                    for child in (item["value"],)
                }
            }
        return self._answer(packet)

    def _answer(self, packet: Mapping[str, Any]) -> dict[str, Any]:
        question = packet["exact_question"]
        kind = question["kind"]
        excerpts = packet["source_excerpts"]
        source_ref = excerpts[0]["ref_id"] if excerpts else ""
        anchor_ref = next(
            (
                fact["ref_id"]
                for fact in packet["accepted_facts"]
                if fact["ref_id"].startswith("anchor-")
            ),
            source_ref,
        )
        source_text = excerpts[0]["text"] if excerpts else ""
        if kind == "extract_requirements":
            records = (
                [
                    {
                        "statement": "Implement the requested behavior.",
                        "kind": "behavior",
                        "source_refs": [anchor_ref],
                        "related_source_refs": [],
                    }
                ]
                if "Implement" in source_text
                else []
            )
            return {"records": records, "has_more": False, "next_anchor": None}
        if kind == "classify_source_block":
            if "Implement" in source_text:
                return {"disposition": "normative", "requirement_ids": ["req-0001"]}
            return {"disposition": "context", "reason": "Background only."}
        if kind == "assess_requirement_fidelity":
            return {"decision": "accept", "reason": "Faithful to the source."}
        if kind == "check_requirement_fidelity":
            return {"decision": "accept", "reason": "Faithful to the source."}
        if kind == "assess_requirement_coverage":
            return {
                "status": "missing",
                "evidence_ids": [],
                "reason": "No implementation evidence was supplied.",
            }
        if kind == "propose_work_units":
            return {
                "records": [
                    {
                        "requirement_ids": ["req-0001"],
                        "deliverable": "Implement the requested behavior.",
                        "planned_writes": [
                            {
                                "path": "src/behavior.txt",
                                "role": "implementation",
                                "source_refs": [anchor_ref],
                                "evidence_ids": [],
                            }
                        ],
                        "implementation_step": "Implement and verify the behavior.",
                        "acceptance_criteria": ["The behavior is implemented."],
                        "required_outputs": [],
                    }
                ],
                "has_more": False,
                "next_anchor": None,
            }
        if kind == "describe_item":
            return {
                "title": f"Implement behavior in {question['subject_ids'][0]}",
                "objective": "Implement the behavior described by the plan.",
            }
        if kind == "enumerate_structural_subjects":
            return {"records": [], "has_more": False, "next_anchor": None}
        if kind == "review_dependency_graph":
            return {"edges": []}
        raise AssertionError(f"unexpected planning question: {kind}")


class _Machine:
    def __init__(self) -> None:
        self.request = _request()
        self.accepted_input_digests: dict[str, str] = {}

    def set_accepted_question_input_digests(
        self, input_digests: Mapping[str, str]
    ) -> None:
        self.accepted_input_digests = dict(input_digests)

    def next_question(
        self, accepted_answers: Mapping[str, Mapping[str, Any]]
    ) -> QuestionRequest[EnumerationAnswer] | None:
        if self.request.question.question_id in accepted_answers:
            return None
        return self.request


class _IssueClient:
    repository = "powdrr-io/example"

    def __init__(self) -> None:
        self.issue: QueueIssue | None = None
        self.created_labels: tuple[str, ...] = ()
        self.ensured_labels: list[str] = []

    def ensure_planning_label(self) -> None:
        self.ensured_labels.append("powdrr:planning")

    def create_issue(
        self, *, body: str, labels: tuple[str, ...] | list[str], title: str
    ) -> QueueIssue:
        self.created_labels = tuple(labels)
        self.issue = QueueIssue(42, title, body, "open", tuple(labels), "issue-url")
        return self.issue

    def get_issue(self, issue_number: int) -> QueueIssue:
        assert issue_number == 42
        assert self.issue is not None
        return self.issue


def _request(
    question_id: str = "extract-requirements-block-01",
) -> QuestionRequest[EnumerationAnswer]:
    question = Question(
        question_id=question_id,
        kind="extract_requirements",
        contract_version=1,
        subject_ids=("block-01",),
        input_digest="sha256:" + "a" * 64,
        context_ref_ids=("block-01",),
        expected_answer_schema="requirement-page-v1",
        attempt=1,
    )
    return QuestionRequest(
        question=question,
        exact_question="Which obligations does this source block impose?",
        answer_field_definitions={"records": "Bounded records."},
        source_excerpts=(SourceExcerpt("block-01", "The issue state is durable."),),
        repository_evidence=(),
        accepted_facts=(),
        valid_example={
            "records": [{"statement": "An obligation."}],
            "has_more": False,
            "next_anchor": None,
        },
        empty_example={"records": [], "has_more": False, "next_anchor": None},
        answer_schema={
            "type": "object",
            "required": ["records", "has_more", "next_anchor"],
            "properties": {
                "records": {"type": "array", "items": {"type": "object"}},
                "has_more": {"type": "boolean"},
                "next_anchor": {"type": ["string", "null"]},
            },
        },
        parse_answer=EnumerationAnswer.from_mapping,
    )


def test_accepted_question_is_durable_and_not_called_again() -> None:
    session_id = "session-123"
    store = InMemoryCheckpointStore(session_id=session_id)
    session = PlanningSession(
        session_id=session_id,
        plan_id="bounded-plan",
        repository="powdrr-io/example",
        base_commit="a" * 40,
        source_path="docs/plan.md",
        source_blob_sha="b" * 40,
        verification_command="make test",
        issue=QueueIssue(
            42, "Planning: bounded-plan", "", "open", ("powdrr:planning",), ""
        ),
    )
    client = _Client()
    machine = _Machine()

    accepted = advance_planning_session(
        session=session,
        state_machine=machine,
        checkpoint_store=store,
        client=client,
        options=PlanningOptions(model="test-model"),
        stderr=None,
    )
    resumed = advance_planning_session(
        session=session,
        state_machine=machine,
        checkpoint_store=store,
        client=client,
        options=PlanningOptions(model="test-model"),
        stderr=None,
    )

    assert accepted.status == "question_accepted"
    assert resumed.status == "complete"
    assert client.calls == 1
    assert machine.accepted_input_digests == {
        machine.request.question.question_id: machine.request.question.input_digest
    }


def test_invalid_answer_is_checkpointed_with_telemetry_after_bounded_corrections() -> (
    None
):
    session_id = "session-invalid-answer"
    store = InMemoryCheckpointStore(session_id=session_id)
    session = PlanningSession(
        session_id=session_id,
        plan_id="bounded-plan",
        repository="powdrr-io/example",
        base_commit="a" * 40,
        source_path="docs/plan.md",
        source_blob_sha="b" * 40,
        verification_command="make test",
        issue=QueueIssue(
            43, "Planning: bounded-plan", "", "open", ("powdrr:planning",), ""
        ),
    )

    class InvalidClient:
        calls = 0

        def complete_json(
            self,
            messages: list[dict[str, str]],
            *,
            response_schema: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            del messages, response_schema
            self.calls += 1
            return {
                "records": [],
                "has_more": True,
                "next_anchor": None,
            }

    metrics: list[Mapping[str, Any]] = []
    client = InvalidClient()
    transition = advance_planning_session(
        session=session,
        state_machine=_Machine(),
        checkpoint_store=store,
        client=client,
        options=PlanningOptions(
            model="test-model", telemetry_sink=lambda metric: metrics.append(metric)
        ),
        stderr=None,
    )

    assert transition.status == "needs_answer"
    assert client.calls == 3
    assert metrics[0]["outcome"] == "needs_answer"
    assert metrics[0]["validation_failures"] == 2
    assert store.load().events[-1].event_type == "needs_answer"
    assert [event.event_type for event in store.load().events] == [
        "needs_answer",
    ]


def test_batched_question_answer_is_checkpointed_as_each_original_question() -> None:
    session_id = "session-question-batch"
    store = InMemoryCheckpointStore(session_id=session_id)
    session = PlanningSession(
        session_id=session_id,
        plan_id="bounded-plan",
        repository="powdrr-io/example",
        base_commit="a" * 40,
        source_path="docs/plan.md",
        source_blob_sha="b" * 40,
        verification_command="make test",
        issue=QueueIssue(
            44, "Planning: bounded-plan", "", "open", ("powdrr:planning",), ""
        ),
    )
    members = (_request("extract-block-01"), _request("extract-block-02"))
    (batch,) = batch_question_requests(members)

    class BatchMachine:
        def next_question(
            self, accepted_answers: Mapping[str, Mapping[str, Any]]
        ) -> QuestionRequest[Any] | None:
            if all(
                member.question.question_id in accepted_answers for member in members
            ):
                return None
            return batch

        def ready_questions(
            self, accepted_answers: Mapping[str, Mapping[str, Any]]
        ) -> tuple[QuestionRequest[Any], ...]:
            if all(
                member.question.question_id in accepted_answers for member in members
            ):
                return ()
            return (batch,)

    class BatchClient:
        calls = 0

        def complete_json(
            self,
            messages: list[dict[str, str]],
            *,
            response_schema: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            del messages, response_schema
            self.calls += 1
            return {
                "answers": {
                    member.question.question_id: dict(member.valid_example)
                    for member in members
                }
            }

    client = BatchClient()
    transition = advance_planning_session(
        session=session,
        state_machine=BatchMachine(),
        checkpoint_store=store,
        client=client,
        options=PlanningOptions(model="test-model"),
        stderr=None,
    )

    accepted_events = [
        event for event in store.load().events if event.event_type == "answer_accepted"
    ]
    assert transition.status == "question_accepted"
    assert set(transition.question_ids) == {
        member.question.question_id for member in members
    }
    assert {event.payload["question"]["question_id"] for event in accepted_events} == {
        member.question.question_id for member in members
    }
    assert client.calls == 1


def test_invalid_accepted_answer_is_reasked_under_the_current_contract() -> None:
    session_id = "session-invalid-replay"
    store = InMemoryCheckpointStore(session_id=session_id)
    session = PlanningSession(
        session_id=session_id,
        plan_id="bounded-plan",
        repository="powdrr-io/example",
        base_commit="a" * 40,
        source_path="docs/plan.md",
        source_blob_sha="b" * 40,
        verification_command="make test",
        issue=QueueIssue(
            45, "Planning: bounded-plan", "", "open", ("powdrr:planning",), ""
        ),
    )
    machine = _Machine()
    stale_answer = {"records": [{"statement": "Old accepted result."}]}
    checkpoint = PlanningCheckpoint.answer_accepted(
        session_id=session_id,
        event_id="answer-accepted-000001",
        sequence=1,
        parent_digest=None,
        question=machine.request.question,
        accepted_answer=stale_answer,
        validation_result={"accepted": True, "attempts": 1},
        id_counters={},
    )
    store.append(checkpoint, expected_parent_digest=None)

    class _ReplayRepairMachine:
        invalid_accepted_question_ids: frozenset[str] = frozenset()

        def ready_questions(
            self, accepted: Mapping[str, Mapping[str, Any]]
        ) -> tuple[QuestionRequest[EnumerationAnswer], ...]:
            question_id = machine.request.question.question_id
            if question_id in accepted:
                self.invalid_accepted_question_ids = frozenset({question_id})
            return (machine.request,)

        def next_question(
            self, accepted: Mapping[str, Mapping[str, Any]]
        ) -> QuestionRequest[EnumerationAnswer] | None:
            return self.ready_questions(accepted)[0]

    client = _Client()
    transition = advance_planning_session(
        session=session,
        state_machine=_ReplayRepairMachine(),
        checkpoint_store=store,
        client=client,
        options=PlanningOptions(model="test-model"),
        stderr=None,
    )

    assert transition.status == "question_accepted"
    assert client.calls == 1
    assert len(store.load().events) == 2
    assert (
        store.load().events[-1].payload["accepted_answer"]["records"][0]["statement"]
        == "The issue state is durable."
    )


def test_needs_answer_event_supersedes_an_older_accepted_answer() -> None:
    session_id = "session-reopened-answer"
    store = InMemoryCheckpointStore(session_id=session_id)
    session = PlanningSession(
        session_id=session_id,
        plan_id="bounded-plan",
        repository="powdrr-io/example",
        base_commit="a" * 40,
        source_path="docs/plan.md",
        source_blob_sha="b" * 40,
        verification_command="make test",
        issue=QueueIssue(
            46, "Planning: bounded-plan", "", "open", ("powdrr:planning",), ""
        ),
    )
    machine = _Machine()
    accepted = PlanningCheckpoint.answer_accepted(
        session_id=session_id,
        event_id="answer-accepted-000001",
        sequence=1,
        parent_digest=None,
        question=machine.request.question,
        accepted_answer={"records": [{"statement": "Old answer."}]},
        validation_result={"accepted": True, "attempts": 1},
        id_counters={},
    )
    store.append(accepted, expected_parent_digest=None)
    reopened = PlanningCheckpoint.needs_answer(
        session_id=session_id,
        event_id="needs-answer-000002",
        sequence=2,
        parent_digest=accepted.digest,
        question=machine.request.question,
        error=PlanningContractError("/records", "needs correction"),
        previous_answer={"records": [{"statement": "Old answer."}]},
        id_counters={},
    )
    store.append(reopened, expected_parent_digest=accepted.digest)

    client = _Client()
    transition = advance_planning_session(
        session=session,
        state_machine=machine,
        checkpoint_store=store,
        client=client,
        options=PlanningOptions(model="test-model"),
        stderr=None,
    )

    assert transition.status == "question_accepted"
    assert client.calls == 1
    assert len(store.load().events) == 3


def test_procedrr_initialize_reopens_answer_invalidated_by_later_checkpoint() -> None:
    session_id = "session-initialize-reopened-answer"
    store = InMemoryCheckpointStore(session_id=session_id)
    machine = _Machine()
    session = PlanningSession(
        session_id=session_id,
        plan_id="bounded-plan",
        repository="powdrr-io/example",
        base_commit="a" * 40,
        source_path="docs/plan.md",
        source_blob_sha="b" * 40,
        verification_command="make test",
        issue=QueueIssue(
            47, "Planning: bounded-plan", "", "open", ("powdrr:planning",), ""
        ),
    )
    accepted = PlanningCheckpoint.answer_accepted(
        session_id=session_id,
        event_id="answer-accepted-000001",
        sequence=1,
        parent_digest=None,
        question=machine.request.question,
        accepted_answer={"records": [{"statement": "Old answer."}]},
        validation_result={"accepted": True, "attempts": 1},
        id_counters={},
    )
    store.append(accepted, expected_parent_digest=None)
    reopened = PlanningCheckpoint.needs_answer(
        session_id=session_id,
        event_id="needs-answer-000002",
        sequence=2,
        parent_digest=accepted.digest,
        question=machine.request.question,
        error=PlanningContractError("/records", "needs correction"),
        previous_answer={"records": [{"statement": "Old answer."}]},
        id_counters={},
    )
    store.append(reopened, expected_parent_digest=accepted.digest)

    client = _Client()
    run_planning_procedrr(
        process_directory=(
            Path(__file__).resolve().parents[1] / "docs" / "procedrr" / "processes"
        ),
        session=session,
        state_machine=machine,
        checkpoint_store=store,
        client=client,
        options=PlanningOptions(model="test-model"),
        stderr=None,
    )

    assert client.calls == 1
    assert [event.event_type for event in store.load().events] == [
        "answer_accepted",
        "needs_answer",
        "answer_accepted",
    ]
    assert (
        store.load().events[-1].payload["accepted_answer"]["records"][0]["statement"]
        == "The issue state is durable."
    )


def test_oversized_work_unit_answer_replays_as_a_smaller_question_group() -> None:
    session_id = "session-oversized-work-units"
    store = InMemoryCheckpointStore(session_id=session_id)
    session = PlanningSession(
        session_id=session_id,
        plan_id="bounded-plan",
        repository="powdrr-io/example",
        base_commit="a" * 40,
        source_path="docs/plan.md",
        source_blob_sha="b" * 40,
        verification_command="make test",
        issue=QueueIssue(
            47, "Planning: bounded-plan", "", "open", ("powdrr:planning",), ""
        ),
    )
    question = Question(
        question_id="propose-units-req-0057-example",
        kind="propose_work_units",
        contract_version=1,
        subject_ids=("req-0057", "req-0058", "req-0059", "req-0060"),
        input_digest="sha256:" + "0" * 64,
        context_ref_ids=(),
        expected_answer_schema="work-unit-proposals-v2",
        attempt=1,
    )
    event = PlanningCheckpoint.needs_answer(
        session_id=session_id,
        event_id="needs-answer-000001",
        sequence=1,
        parent_digest=None,
        question=question,
        error=PlanningContractError(
            "/", "serialized answer is 6304 characters; limit is 4000"
        ),
        previous_answer={"records": []},
        id_counters={},
    )
    store.append(event, expected_parent_digest=None)
    single_question = Question(
        question_id="propose-units-req-0058-single",
        kind="propose_work_units",
        contract_version=1,
        subject_ids=("req-0058",),
        input_digest="sha256:" + "1" * 64,
        context_ref_ids=(),
        expected_answer_schema="work-unit-proposals-v2",
        attempt=1,
    )
    single_event = PlanningCheckpoint.needs_answer(
        session_id=session_id,
        event_id="needs-answer-000002",
        sequence=2,
        parent_digest=event.digest,
        question=single_question,
        error=PlanningContractError(
            "/", "serialized answer is 4249 characters; limit is 4000"
        ),
        previous_answer={"records": []},
        id_counters={},
    )
    store.append(single_event, expected_parent_digest=event.digest)

    class _SplitCaptureMachine(_Machine):
        oversized_groups: tuple[tuple[str, ...], ...] = ()

        def set_oversized_work_unit_groups(
            self, groups: tuple[tuple[str, ...], ...]
        ) -> None:
            self.oversized_groups = tuple(groups)

    machine = _SplitCaptureMachine()
    transition = advance_planning_session(
        session=session,
        state_machine=machine,
        checkpoint_store=store,
        client=_Client(),
        options=PlanningOptions(model="test-model"),
        stderr=None,
    )

    assert transition.status == "question_accepted"
    assert machine.oversized_groups == (
        ("req-0057", "req-0058", "req-0059", "req-0060"),
        ("req-0058",),
    )


def test_question_uses_json_object_mode_and_validates_schema_locally() -> None:
    session_id = "session-json-object"
    store = InMemoryCheckpointStore(session_id=session_id)
    session = PlanningSession(
        session_id=session_id,
        plan_id="bounded-plan",
        repository="powdrr-io/example",
        base_commit="a" * 40,
        source_path="docs/plan.md",
        source_blob_sha="b" * 40,
        verification_command="make test",
        issue=QueueIssue(
            44, "Planning: bounded-plan", "", "open", ("powdrr:planning",), ""
        ),
    )

    class _JsonObjectClient:
        def __init__(self) -> None:
            self.response_schemas: list[Mapping[str, Any] | None] = []

        def complete_json(
            self,
            _messages: list[dict[str, str]],
            *,
            response_schema: Mapping[str, Any] | None = None,
        ) -> dict[str, Any]:
            self.response_schemas.append(response_schema)
            return {
                "records": [{"statement": "The issue state is durable."}],
                "has_more": False,
                "next_anchor": None,
            }

    client = _JsonObjectClient()
    transition = advance_planning_session(
        session=session,
        state_machine=_Machine(),
        checkpoint_store=store,
        client=client,
        options=PlanningOptions(model="test-model", use_provider_schema=False),
        stderr=None,
    )

    assert transition.status == "question_accepted"
    assert client.response_schemas == [None]
    assert store.load().events[0].event_type == "answer_accepted"


def test_ready_question_frontier_runs_concurrently_and_checkpoints_serially() -> None:
    session_id = "session-batch"
    store = InMemoryCheckpointStore(session_id=session_id)
    session = PlanningSession(
        session_id=session_id,
        plan_id="bounded-plan",
        repository="powdrr-io/example",
        base_commit="a" * 40,
        source_path="docs/plan.md",
        source_blob_sha="b" * 40,
        verification_command="make test",
        issue=QueueIssue(
            43, "Planning: bounded-plan", "", "open", ("powdrr:planning",), ""
        ),
    )
    requests = tuple(_request(f"extract-block-{index:02d}") for index in range(1, 6))

    class BatchMachine:
        def ready_questions(
            self, accepted: Mapping[str, Mapping[str, Any]]
        ) -> tuple[QuestionRequest[EnumerationAnswer], ...]:
            return tuple(
                request
                for request in requests
                if request.question.question_id not in accepted
            )

        def next_question(
            self, accepted: Mapping[str, Mapping[str, Any]]
        ) -> QuestionRequest[EnumerationAnswer] | None:
            ready = self.ready_questions(accepted)
            return ready[0] if ready else None

    class ConcurrentClient:
        def __init__(self) -> None:
            self.barrier = Barrier(2, timeout=3)
            self.lock = Lock()
            self.calls = 0

        def complete_json(
            self,
            _messages: list[dict[str, str]],
            *,
            response_schema: Mapping[str, Any] | None = None,
        ) -> dict[str, Any]:
            del response_schema
            self.barrier.wait()
            with self.lock:
                self.calls += 1
            return {
                "records": [{"statement": "An obligation."}],
                "has_more": False,
                "next_anchor": None,
            }

    client = ConcurrentClient()
    telemetry: list[Mapping[str, Any]] = []
    transition = advance_planning_session(
        session=session,
        state_machine=BatchMachine(),
        checkpoint_store=store,
        client=client,
        options=PlanningOptions(
            model="test-model",
            max_concurrent_questions=2,
            provider="test-provider",
            telemetry_sink=telemetry.append,
        ),
        stderr=None,
    )
    next_transition = advance_planning_session(
        session=session,
        state_machine=BatchMachine(),
        checkpoint_store=store,
        client=client,
        options=PlanningOptions(
            model="test-model",
            max_concurrent_questions=2,
            provider="test-provider",
            telemetry_sink=telemetry.append,
        ),
        stderr=None,
    )

    assert transition.status == "question_accepted"
    assert transition.question_ids == tuple(
        request.question.question_id for request in requests[:2]
    )
    assert len(transition.accepted_answers) == 2
    assert next_transition.question_ids == tuple(
        request.question.question_id for request in requests[2:4]
    )
    assert client.calls == 4
    assert [event.sequence for event in store.load().events] == [1, 2, 3, 4]
    assert len(telemetry) == 4
    assert all(metric["provider"] == "test-provider" for metric in telemetry)
    assert all(metric["api_calls"] == 1 for metric in telemetry)
    assert all(metric["prompt_tokens"] is None for metric in telemetry)
    assert all(metric["completion_tokens"] is None for metric in telemetry)
    assert all(metric["request_chars"] > 0 for metric in telemetry)


def test_session_issue_is_created_as_a_non_queue_tracker_and_resumes_by_number() -> (
    None
):
    client = _IssueClient()
    session = create_planning_session(
        client,
        plan_id="bounded-plan",
        base_commit="a" * 40,
        source_path="docs/plan.md",
        source_blob_sha="b" * 40,
        verification_command="make test",
    )

    resumed = load_planning_session(client, 42)

    assert client.ensured_labels == ["powdrr:planning"]
    assert client.created_labels == ("powdrr:planning",)
    assert session == resumed
    assert "not an executable work item" in session.issue.body
    assert "planning-session-v1" in session.issue.body


def test_bundled_plan_process_is_validated_procedrr() -> None:
    repository_root = Path(__file__).parents[1]
    process_path = repository_root / "docs/procedrr/processes/plan-execution.yaml"

    process = ProcessCatalog.load(process_path.parent).get("plan-execution")

    assert process.validate_inputs(
        {"session_id": "session-123", "issue_number": 42}
    ) == {
        "session_id": "session-123",
        "issue_number": 42,
    }


def test_procedrr_process_runs_and_checkpoints_bounded_questions() -> None:
    repository_root = Path(__file__).parents[1]
    session_id = "session-123"
    session = PlanningSession(
        session_id=session_id,
        plan_id="bounded-plan",
        repository="powdrr-io/example",
        base_commit="a" * 40,
        source_path="docs/plan.md",
        source_blob_sha="b" * 40,
        verification_command="make test",
        issue=QueueIssue(
            42, "Planning: bounded-plan", "", "open", ("powdrr:planning",), ""
        ),
    )
    client = _Client()
    store = InMemoryCheckpointStore(session_id=session_id)
    result = run_planning_procedrr(
        process_directory=str(repository_root / "docs/procedrr/processes"),
        session=session,
        state_machine=_Machine(),
        checkpoint_store=store,
        client=client,
        options=PlanningOptions(model="test-model"),
        stderr=None,
    )

    assert result.terminal_status.value == "succeeded"
    assert client.calls == 1
    assert [event.event_type for event in store.load().events] == [
        "answer_accepted",
    ]


def test_source_question_machine_accounts_for_normative_and_context_blocks() -> None:
    text = (
        "# Plan\n\nImplement the requested behavior in src/behavior.txt and "
        "verify it in src/test.txt.\n\n"
        "Background only.\n"
    )
    source_document = PinnedSourceDocument(
        repository="owner/repo",
        commit_sha="a" * 40,
        path="docs/plan.md",
        blob_sha="b" * 40,
        content_sha256=hashlib.sha256(text.encode()).hexdigest(),
        text=text,
    )
    source = segment_markdown_source(source_document)
    searched_requirements: list[str] = []

    def search_evidence(requirements: Any) -> dict[str, RequirementEvidenceSearch]:
        searched_requirements.extend(item.id for item in requirements)
        return {
            item.id: RequirementEvidenceSearch(
                (), ("behavior",), 0, 0, source_document.commit_sha
            )
            for item in requirements
        }

    machine = SourceLedgerQuestionMachine(
        source,
        evidence_search=search_evidence,
        plan_id="bounded-plan",
        structrr_baseline={
            "entities": [],
            "entity_relationships": [],
            "files": [],
        },
    )
    answers: dict[str, dict[str, Any]] = {}
    block_by_id = {block.id: block for block in source.blocks}
    requirement_anchor = next(
        block.anchor_ids[0] for block in source.blocks if "Implement" in block.text
    )

    while True:
        for deterministic in machine.deterministic_answers(answers):
            answers[deterministic["question"].question_id] = dict(
                deterministic["answer"]
            )
        request = machine.next_question(answers)
        if request is None:
            break
        question = request.question
        if question.kind == "extract_requirements":
            block = block_by_id[question.subject_ids[0]]
            records = (
                [
                    {
                        "statement": "Implement the requested behavior.",
                        "kind": "behavior",
                        "source_refs": [block.anchor_ids[0]],
                        "related_source_refs": [],
                    }
                ]
                if "Implement" in block.text
                else []
            )
            answer = {"records": records, "has_more": False, "next_anchor": None}
        elif question.kind == "propose_work_units":
            answer = {
                "records": [
                    {
                        "requirement_ids": ["req-0001"],
                        "deliverable": "Implement the requested behavior.",
                        "planned_writes": [
                            {
                                "path": "src/behavior.txt",
                                "role": "implementation",
                                "source_refs": [requirement_anchor],
                                "evidence_ids": [],
                            }
                        ],
                        "implementation_step": "Implement and verify the behavior.",
                        "acceptance_criteria": ["The behavior is implemented."],
                        "required_outputs": [
                            {
                                "description": "The behavior contract.",
                                "requirement_ids": ["req-0001"],
                            }
                        ],
                    },
                    {
                        "requirement_ids": ["req-0001"],
                        "deliverable": (
                            "Test the requested behavior in src/behavior.txt."
                        ),
                        "planned_writes": [
                            {
                                "path": "src/test.txt",
                                "role": "test",
                                "source_refs": [requirement_anchor],
                                "evidence_ids": [],
                            }
                        ],
                        "implementation_step": "Add a focused behavior test.",
                        "acceptance_criteria": ["The behavior is tested."],
                        "required_outputs": [],
                    },
                ],
                "has_more": False,
                "next_anchor": None,
            }
        elif question.kind == "describe_item":
            answer = dict(request.valid_example)
            answer["title"] = f"Implement behavior in {question.subject_ids[0]}"
        elif question.kind == "confirm_dependency":
            answer = {
                "result": "required",
                "output": "The behavior contract.",
                "requirement_ids": ["req-0001"],
                "reason": "The focused test consumes the implementation contract.",
            }
        elif question.kind == "enumerate_structural_subjects":
            structural_requests = machine._structrr_requests(answers)
            assert len(structural_requests) == 1
            assert structural_requests[0].question.kind == (
                "enumerate_structural_subjects"
            )
            assert not structural_requests[0].batch_members
            answer = {"records": [], "has_more": False, "next_anchor": None}
        else:
            answer = {"decision": "accept", "reason": "Faithful to the source."}
        answers[question.question_id] = answer

    ledger = machine.accepted_ledger(answers)

    assert [requirement.id for requirement in ledger.requirements] == ["req-0001"]
    assert len(ledger.dispositions) == len(source.blocks)
    assert {item.disposition for item in ledger.dispositions} == {
        "normative",
        "context",
    }
    coverage = machine.accepted_coverage(answers)
    assert len(coverage) == 1
    assert coverage[0].answer.status == "missing"
    assert [unit.id for unit in machine.accepted_work_units(answers)] == [
        "unit-0001",
        "unit-0002",
    ]
    assert len(machine.accepted_item_descriptions(answers)) == 1
    dependency_edges = machine.accepted_dependency_edges(answers)
    assert len(dependency_edges) == 1
    assert dependency_edges[0].before_unit_id == "unit-0001"
    assert dependency_edges[0].after_unit_id == "unit-0002"
    assembled = machine.accepted_assembly(answers, plan_id="bounded-plan")
    assert assembled.dependency_graph.unit_edges == dependency_edges
    assert len(assembled.plan.items) == 1
    structrr_proposal = machine.accepted_structrr_proposal(answers)
    assert set(structrr_proposal.item_proposals) == {"item-0001"}
    compiled = machine.compile_accepted_plan(
        answers,
        plan_id="bounded-plan",
        plan_path="docs/plans/bounded-plan.yaml",
        proposal_path="docs/plans/bounded-plan-structrr-proposal.json",
    )
    assert set(compiled.destinations) == set(compiled.files)
    assert compiled.ready_item_ids == ("item-0001",)
    assert machine.next_question(answers) is None
    assert searched_requirements == ["req-0001"]


def test_source_dispositions_are_derived_only_for_unambiguous_cases() -> None:
    normative = _deterministic_block_disposition(
        block_id="block-1",
        direct_ids=("req-0001", "req-0002"),
        related_ids=(),
        prior_direct_ids=(),
        has_exact_duplicate=False,
    )
    assert normative is not None
    assert normative[0]["disposition"] == "normative"
    assert normative[0]["requirement_ids"] == ["req-0001", "req-0002"]

    context = _deterministic_block_disposition(
        block_id="block-2",
        direct_ids=(),
        related_ids=(),
        prior_direct_ids=(),
        has_exact_duplicate=False,
    )
    assert context is not None and context[0]["disposition"] == "context"

    duplicate = _deterministic_block_disposition(
        block_id="block-3",
        direct_ids=(),
        related_ids=(),
        prior_direct_ids=(("req-0001",),),
        has_exact_duplicate=True,
    )
    assert duplicate is not None and duplicate[0]["disposition"] == "duplicate"

    related_only = _deterministic_block_disposition(
        block_id="block-4",
        direct_ids=(),
        related_ids=("req-0001",),
        prior_direct_ids=(),
        has_exact_duplicate=False,
    )
    ambiguous_duplicate = _deterministic_block_disposition(
        block_id="block-5",
        direct_ids=(),
        related_ids=(),
        prior_direct_ids=(("req-0001",), ("req-0002",)),
        has_exact_duplicate=True,
    )
    assert related_only is None
    assert ambiguous_duplicate is None


def test_zero_hit_coverage_requires_complete_pinned_search_metadata() -> None:
    complete = RequirementEvidenceSearch((), (), 0, 0, "a" * 40)
    incomplete = RequirementEvidenceSearch(
        (), (), 0, 0, "a" * 40, basedpyright_available=False
    )
    positive_count = RequirementEvidenceSearch(
        (), (), 1, 0, "a" * 40, basedpyright_available=True
    )
    assert _is_completed_zero_hit_search(complete, pinned_commit="a" * 40)
    assert not _is_completed_zero_hit_search(incomplete, pinned_commit="a" * 40)
    assert not _is_completed_zero_hit_search(positive_count, pinned_commit="a" * 40)
    assert not _is_completed_zero_hit_search(complete, pinned_commit="b" * 40)


def test_structural_candidates_follow_baseline_symbol_bindings_on_planned_paths() -> (
    None
):
    text = "# Plan\n\nChange the parser in src/parser.py.\n"
    document = PinnedSourceDocument(
        repository="owner/repo",
        commit_sha="a" * 40,
        path="docs/plan.md",
        blob_sha="b" * 40,
        content_sha256=hashlib.sha256(text.encode()).hexdigest(),
        text=text,
    )
    source = segment_markdown_source(document)
    anchor = source.anchors[-1].id
    requirement = Requirement(
        "req-0001", "Change the parser.", "behavior", (anchor,), ("group-1",), ()
    )
    unit = WorkUnit(
        "unit-0001",
        (requirement.id,),
        "Update the parser",
        "repository",
        "python",
        (PlannedWrite("src/parser.py", "implementation", "python", (), ()),),
        "Change parser behavior.",
        ("The parser handles the requested case.",),
        ("validation-0001",),
    )
    baseline = {
        "entities": [
            {"id": "entity:parse", "type": "Function", "name": "parse"},
            {"id": "entity:unrelated", "type": "Function", "name": "unrelated"},
        ],
        "source_subjects": [
            {
                "id": "python:src/parser.py::src.parser.parse",
                "stable_key": "python::src.parser.parse",
                "path": "src/parser.py",
                "file_entity_id": "file:src/parser.py",
            }
        ],
        "source_bindings": [
            {"entity_id": "entity:parse", "path": "src/parser.py"},
            {"entity_id": "entity:unrelated", "path": "src/other.py"},
        ],
        "entity_relationships": [
            {
                "id": "relationship:parse-calls-tokenize",
                "source": "entity:parse",
                "target": "entity:tokenize",
            },
            {
                "id": "relationship:unrelated",
                "source": "entity:unrelated",
                "target": "entity:other",
            },
        ],
    }

    candidates = _planned_existing_structural_subjects(
        "item-0001",
        (unit,),
        requirement_by_id={requirement.id: requirement},
        projected_baseline=baseline,
        fallback_source_refs=(anchor,),
    )

    assert {item.subject_id for item in candidates} == {
        "entity:parse",
        "relationship:parse-calls-tokenize",
    }
    assert all(item.unit_ids == (unit.id,) for item in candidates)


def test_procedrr_runs_real_planning_machine_through_compiled_artifacts() -> None:
    text = (
        "# Plan\n\nImplement the requested behavior in src/behavior.txt.\n\n"
        "Background only.\n"
    )
    source_document = PinnedSourceDocument(
        repository="owner/repo",
        commit_sha="a" * 40,
        path="docs/plan.md",
        blob_sha="b" * 40,
        content_sha256=hashlib.sha256(text.encode()).hexdigest(),
        text=text,
    )
    source = segment_markdown_source(source_document)
    machine = SourceLedgerQuestionMachine(
        source,
        evidence_search=lambda requirements: {
            requirement.id: RequirementEvidenceSearch(
                (), (), 0, 0, source_document.commit_sha
            )
            for requirement in requirements
        },
        verification_command="make test",
        plan_id="bounded-plan",
        structrr_baseline={
            "entities": [],
            "entity_relationships": [],
            "files": [],
        },
    )
    session_id = "planning-e2e"
    session = PlanningSession(
        session_id=session_id,
        plan_id="bounded-plan",
        repository="owner/repo",
        base_commit="a" * 40,
        source_path="docs/plan.md",
        source_blob_sha="b" * 40,
        verification_command="make test",
        issue=QueueIssue(
            42,
            "Planning: bounded-plan",
            "",
            "open",
            ("powdrr:planning",),
            "https://github.com/owner/repo/issues/42",
        ),
    )
    provider = _PlanningScriptClient()
    store = InMemoryCheckpointStore(session_id=session_id)
    repository_root = Path(__file__).parents[1]

    result = run_planning_procedrr(
        process_directory=repository_root / "docs/procedrr/processes",
        session=session,
        state_machine=machine,
        checkpoint_store=store,
        client=provider,
        options=PlanningOptions(model="scripted-model"),
        stderr=None,
    )

    assert result.terminal_status.value == "succeeded", (
        store.load().events[-1].payload.get("validation_result")
    )
    assert provider.calls == [
        "planning_question_batch",
        "check_requirement_fidelity",
        "propose_work_units",
        "describe_item",
        "enumerate_structural_subjects",
    ]
    journal = store.load()
    deterministic_events = [
        event
        for event in journal.events
        if event.event_type == "answer_accepted"
        and event.payload.get("validation_result", {}).get("origin") == "deterministic"
    ]
    assert {event.payload["question"]["kind"] for event in deterministic_events} == {
        "classify_source_block",
        "assess_requirement_coverage",
    }
    assert any(
        event.payload["validation_result"]["rule_id"] == "completed-zero-hit-search-v1"
        for event in deterministic_events
    )
    accepted = {
        str(event.payload["question"]["question_id"]): dict(
            event.payload["accepted_answer"]
        )
        for event in journal.events
        if event.event_type == "answer_accepted"
        and isinstance(event.payload.get("question"), Mapping)
        and isinstance(event.payload.get("accepted_answer"), Mapping)
    }
    compiled = machine.compile_accepted_plan(
        accepted,
        plan_id="bounded-plan",
        plan_path="docs/plans/bounded-plan.yaml",
        proposal_path="docs/plans/bounded-plan-structrr-proposal.json",
    )
    assert compiled.ready_item_ids == ("item-0001",)
    assert len(compiled.files) == 3
    accepted_questions = [
        event.payload["question"]
        for event in journal.events
        if event.event_type == "answer_accepted"
    ]
    assert (
        sum(
            question["kind"] == "extract_requirements"
            for question in accepted_questions
        )
        == 3
    )
    assert (
        sum(
            question["kind"] == "classify_source_block"
            for question in accepted_questions
        )
        == 3
    )
