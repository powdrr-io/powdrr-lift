from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from threading import Barrier, Lock
from typing import Any

from powdrr_lift.workrr.github_issue_queue import QueueIssue
from powdrr_lift.workrr.planning.checkpoints import InMemoryCheckpointStore
from powdrr_lift.workrr.planning.contracts import EnumerationAnswer, Question
from powdrr_lift.workrr.planning.driver import SourceLedgerQuestionMachine
from powdrr_lift.workrr.planning.evidence import RequirementEvidenceSearch
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
    run_planning_procedrr,
)
from powdrr_lift.workrr.planning.source import (
    PinnedSourceDocument,
    segment_markdown_source,
)
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
        kind = question["kind"]
        self.calls.append(kind)
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
                        "component": "repository",
                        "implementation_runtime": "neutral",
                        "planned_writes": [
                            {
                                "path": "src/behavior.txt",
                                "role": "implementation",
                                "runtime": None,
                                "source_refs": [anchor_ref],
                                "evidence_ids": [],
                            }
                        ],
                        "implementation_step": "Implement and verify the behavior.",
                        "acceptance_criteria": ["The behavior is implemented."],
                        "validation_ids": ["validation-0001"],
                        "required_outputs": [],
                    }
                ],
                "has_more": False,
                "next_anchor": None,
            }
        if kind == "describe_item":
            return {
                "title": "Implement the behavior",
                "objective": "Implement the behavior described by the plan.",
                "scope": [
                    {
                        "description": "Implement and validate the behavior.",
                        "unit_ids": ["unit-0001"],
                    }
                ],
            }
        if kind == "enumerate_structural_subjects":
            return {"records": [], "has_more": False, "next_anchor": None}
        raise AssertionError(f"unexpected planning question: {kind}")


class _Machine:
    def __init__(self) -> None:
        self.request = _request()

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
    assert [event.event_type for event in store.load().events] == [
        "answer_accepted",
    ]


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
    requests = (_request("extract-block-01"), _request("extract-block-02"))

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
    transition = advance_planning_session(
        session=session,
        state_machine=BatchMachine(),
        checkpoint_store=store,
        client=client,
        options=PlanningOptions(model="test-model", max_concurrent_questions=2),
        stderr=None,
    )

    assert transition.status == "question_accepted"
    assert transition.question_ids == tuple(
        request.question.question_id for request in requests
    )
    assert len(transition.accepted_answers) == 2
    assert client.calls == 2
    assert [event.sequence for event in store.load().events] == [1, 2]


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

    while (request := machine.next_question(answers)) is not None:
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
        elif question.kind == "classify_source_block":
            block = block_by_id[question.subject_ids[0]]
            if "Implement" in block.text:
                answer = {"disposition": "normative", "requirement_ids": ["req-0001"]}
            else:
                answer = {"disposition": "context", "reason": "Background information."}
        elif question.kind == "assess_requirement_coverage":
            assert request.accepted_facts[-1].ref_id == "repository-search"
            assert request.accepted_facts[-1].value["terms"] == ["behavior"]
            answer = {
                "status": "missing",
                "evidence_ids": [],
                "reason": "No supplied repository evidence implements it.",
            }
        elif question.kind == "propose_work_units":
            answer = {
                "records": [
                    {
                        "requirement_ids": ["req-0001"],
                        "deliverable": "Implement the requested behavior.",
                        "component": "repository",
                        "implementation_runtime": "neutral",
                        "planned_writes": [
                            {
                                "path": "src/behavior.txt",
                                "role": "implementation",
                                "runtime": None,
                                "source_refs": [requirement_anchor],
                                "evidence_ids": [],
                            }
                        ],
                        "implementation_step": "Implement and verify the behavior.",
                        "acceptance_criteria": ["The behavior is implemented."],
                        "validation_ids": ["validation-0001"],
                        "required_outputs": [
                            {
                                "description": "The behavior contract.",
                                "requirement_ids": ["req-0001"],
                            }
                        ],
                    },
                    {
                        "requirement_ids": ["req-0001"],
                        "deliverable": "Test the requested behavior.",
                        "component": "repository",
                        "implementation_runtime": "neutral",
                        "planned_writes": [
                            {
                                "path": "src/test.txt",
                                "role": "test",
                                "runtime": None,
                                "source_refs": [requirement_anchor],
                                "evidence_ids": [],
                            }
                        ],
                        "implementation_step": "Add a focused behavior test.",
                        "acceptance_criteria": ["The behavior is tested."],
                        "validation_ids": ["validation-0001"],
                        "required_outputs": [],
                    },
                ],
                "has_more": False,
                "next_anchor": None,
            }
        elif question.kind == "describe_item":
            answer = dict(request.valid_example)
        elif question.kind == "confirm_dependency":
            answer = {
                "result": "required",
                "output": "The behavior contract.",
                "requirement_ids": ["req-0001"],
                "reason": "The focused test consumes the implementation contract.",
            }
        elif question.kind == "enumerate_structural_subjects":
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


def test_procedrr_runs_real_planning_machine_through_compiled_artifacts(
    tmp_path: Path,
) -> None:
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
        repository_root=tmp_path,
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
        "extract_requirements",
        "extract_requirements",
        "extract_requirements",
        "classify_source_block",
        "classify_source_block",
        "classify_source_block",
        "check_requirement_fidelity",
        "assess_requirement_coverage",
        "propose_work_units",
        "describe_item",
        "enumerate_structural_subjects",
    ]
    journal = store.load()
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
