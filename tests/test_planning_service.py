from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from powdrr_lift.workrr.github_issue_queue import QueueIssue
from powdrr_lift.workrr.planning.checkpoints import InMemoryCheckpointStore
from powdrr_lift.workrr.planning.contracts import EnumerationAnswer, Question
from powdrr_lift.workrr.planning.questions import (
    QuestionRequest,
    SourceExcerpt,
)
from powdrr_lift.workrr.planning.service import (
    PlanningOptions,
    PlanningSession,
    advance_planning_session,
    create_planning_session,
    load_planning_session,
    planning_operation_executor,
)
from procedrr.catalog import ProcessCatalog
from procedrr_evaluator.runtime import ProcedrrExecutionService


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


class _Machine:
    def __init__(self) -> None:
        self.request = _request()

    def next_question(
        self, accepted_answers: Mapping[str, Mapping[str, Any]]
    ) -> QuestionRequest[EnumerationAnswer] | None:
        if self.request.question.question_id in accepted_answers:
            return None
        return self.request


class _NoProcessLLM:
    def complete_json(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("planning questions use the configured provider directly")


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


def _request() -> QuestionRequest[EnumerationAnswer]:
    question = Question(
        question_id="extract-requirements-block-01",
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
    assert [event.event_type for event in store.load().events] == [
        "question_started",
        "answer_accepted",
    ]


def test_session_issue_is_created_as_a_non_queue_tracker_and_resumes_by_number() -> (
    None
):
    client = _IssueClient()
    session = create_planning_session(
        client,  # type: ignore[arg-type]
        plan_id="bounded-plan",
        base_commit="a" * 40,
        source_path="docs/plan.md",
        source_blob_sha="b" * 40,
        verification_command="make test",
    )

    resumed = load_planning_session(client, 42)  # type: ignore[arg-type]

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
    process_directory = repository_root / "docs/procedrr/processes"
    catalog = ProcessCatalog.load(process_directory)
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
    executor = planning_operation_executor(
        session=session,
        state_machine=_Machine(),
        checkpoint_store=store,
        client=client,
        options=PlanningOptions(model="test-model"),
        stderr=None,
    )

    result = ProcedrrExecutionService(catalog, _NoProcessLLM(), executor).execute(
        "plan-execution",
        {"session_id": session_id, "issue_number": 42},
    )

    assert result.terminal_status.value == "succeeded"
    assert client.calls == 1
    assert [event.event_type for event in store.load().events] == [
        "question_started",
        "answer_accepted",
    ]
