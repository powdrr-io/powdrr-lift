from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from powdrr_contracts.workspace import (
    MessageRole,
    QuestionState,
    TurnState,
    WorkspaceTurn,
)
from powdrr_local.turn_dispatch_supervisor import WorkspaceTurnDispatchSupervisor
from powdrr_local.workspace import WorkspaceConflict, WorkspaceStore


class InterviewDispatcher:
    def __init__(self, store: WorkspaceStore) -> None:
        self.store = store
        self.calls: list[str] = []

    def dispatch_turn(self, turn: WorkspaceTurn) -> WorkspaceTurn:
        self.calls.append(turn.turn_id)
        if turn.state is TurnState.ACCEPTED:
            running = self.store.transition_turn(
                turn.turn_id,
                expected_revision=turn.revision,
                state=TurnState.RUNNING,
                run_id="run-1",
                attempt_id="attempt-1",
            )
            self.store.create_question(
                turn.turn_id,
                question_id="question-1",
                prompt="Which region?",
                response_schema={"type": "string"},
                run_id="run-1",
                attempt_id="attempt-1",
                expected_turn_revision=running.revision,
            )
            return self.store.get_turn(turn.turn_id)

        question = next(
            question
            for question in self.store.list_undelivered_question_answers("repo")
            if question.turn_id == turn.turn_id
        )
        self.store.mark_question_answer_delivered(
            question.question_id,
            expected_revision=question.revision,
            run_id="run-1",
            attempt_id="attempt-1",
            delivery_receipt_id="receipt-1",
        )
        current = self.store.get_turn(turn.turn_id)
        final = self.store.append_message(
            current.conversation_id,
            message_id="assistant-final",
            role=MessageRole.ASSISTANT.value,
            content="Using the selected region.",
            turn_id=turn.turn_id,
            run_id="run-1",
            attempt_id="attempt-1",
        )
        return self.store.transition_turn(
            turn.turn_id,
            expected_revision=current.revision,
            state=TurnState.SUCCEEDED,
            final_message_id=final.message_id,
        )


def _setup(tmp_path: Path) -> tuple[WorkspaceStore, WorkspaceTurn, str, int]:
    store = WorkspaceStore(tmp_path / "workspace.db")
    conversation = store.create_conversation("repo", "Interview")
    turn = store.create_turn(
        conversation.conversation_id,
        turn_id="turn-1",
        message_id="user-1",
        content="Configure the deployment.",
    )
    lease = store.acquire_supervisor_lease(
        "supervisor-1",
        process_id=100,
        process_started_at="supervisor-start",
    )
    return store, turn, lease.supervisor_id, lease.generation


def test_dispatch_parks_for_input_and_answer_requeues_continuation(
    tmp_path: Path,
) -> None:
    store, turn, supervisor_id, generation = _setup(tmp_path)
    dispatcher = InterviewDispatcher(store)
    supervisor = WorkspaceTurnDispatchSupervisor(
        store,
        dispatcher,
        supervisor_id=supervisor_id,
        supervisor_generation=generation,
    )

    waiting = asyncio.run(supervisor.dispatch_next("repo"))

    assert waiting is not None
    assert waiting.state is TurnState.WAITING_FOR_INPUT
    assert asyncio.run(supervisor.dispatch_next("repo")) is None
    question = store.get_question("question-1")
    assert question.state is QuestionState.OPEN
    store.answer_question(
        question.question_id,
        answer="us-west-2",
        expected_revision=question.revision,
        request_id="answer-1",
    )
    assert store.list_pending_turn_dispatches("repo") == (waiting,)

    succeeded = asyncio.run(supervisor.dispatch_next("repo"))

    assert succeeded is not None
    assert succeeded.state is TurnState.SUCCEEDED
    assert dispatcher.calls == [turn.turn_id, turn.turn_id]
    assert store.list_pending_turn_dispatches("repo") == ()
    assert asyncio.run(supervisor.dispatch_next("repo")) is None


def test_supervisor_generation_reclaims_only_safe_accepted_turns(
    tmp_path: Path,
) -> None:
    store, turn, supervisor_id, generation = _setup(tmp_path)
    claimed = store.claim_next_turn_dispatch(
        "repo",
        claim_id="old-claim",
        supervisor_id=supervisor_id,
        supervisor_generation=generation,
    )
    assert claimed == turn

    lease_expiry = datetime.fromisoformat(store.get_supervisor_lease().lease_expires_at)
    with pytest.raises(WorkspaceConflict, match="Another supervisor"):
        store.acquire_supervisor_lease(
            "supervisor-2",
            process_id=200,
            process_started_at="supervisor-start-2",
            now=lease_expiry - timedelta(seconds=1),
        )

    new_lease = store.acquire_supervisor_lease(
        "supervisor-2",
        process_id=200,
        process_started_at="supervisor-start-2",
        now=lease_expiry + timedelta(seconds=1),
    )
    reclaimed = store.claim_next_turn_dispatch(
        "repo",
        claim_id="new-claim",
        supervisor_id=new_lease.supervisor_id,
        supervisor_generation=new_lease.generation,
        now=lease_expiry + timedelta(seconds=2),
    )
    assert reclaimed == turn


def test_running_turn_is_not_reclaimed_after_supervisor_generation_changes(
    tmp_path: Path,
) -> None:
    store, turn, supervisor_id, generation = _setup(tmp_path)
    running = store.transition_turn(
        turn.turn_id,
        expected_revision=turn.revision,
        state=TurnState.RUNNING,
        run_id="run-1",
        attempt_id="attempt-1",
    )
    store.claim_next_turn_dispatch(
        "repo",
        claim_id="old-claim",
        supervisor_id=supervisor_id,
        supervisor_generation=generation,
    )
    lease_expiry = datetime.fromisoformat(store.get_supervisor_lease().lease_expires_at)
    newer_lease = store.acquire_supervisor_lease(
        "supervisor-2",
        process_id=200,
        process_started_at="supervisor-start-2",
        now=lease_expiry + timedelta(seconds=1),
    )

    reclaimed = store.claim_next_turn_dispatch(
        "repo",
        claim_id="new-claim",
        supervisor_id=newer_lease.supervisor_id,
        supervisor_generation=newer_lease.generation,
    )

    assert reclaimed is None
    assert store.get_turn(turn.turn_id) == running
