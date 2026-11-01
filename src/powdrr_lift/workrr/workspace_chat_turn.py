"""Bridge durable workspace turns to the existing Procedrr chat runner."""

from __future__ import annotations

import inspect
import json
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from powdrr_contracts.workspace import (
    MessageRole,
    QuestionState,
    TurnState,
    WorkspaceQuestion,
    WorkspaceRepository,
    WorkspaceTurn,
)
from powdrr_local.workspace import WorkspaceConflict, WorkspaceStore

from powdrr_lift.workrr.chat_config import WorkflowChatConfig
from powdrr_lift.workrr.chat_events import WorkflowChatEvent, WorkflowChatEventKind
from powdrr_lift.workrr.procedrr_chat import (
    WORKFLOW_CHAT_WAITING_FOR_INPUT,
    run_procedrr_workflow_chat,
)


class WorkspaceChatTurnRunner:
    """Execute one accepted turn or deliver one saved answer continuation.

    A supervisor may call this method after claiming the turn. It never claims
    work itself, so concurrent-process ownership and recovery remain the
    supervisor's responsibility.
    """

    def __init__(
        self,
        store: WorkspaceStore,
        workflow_config: WorkflowChatConfig,
        *,
        chat_runner: Callable[..., int] = run_procedrr_workflow_chat,
        event_notifier: Callable[[str], Any] | None = None,
    ) -> None:
        self._store = store
        self._workflow_config = workflow_config
        self._chat_runner = chat_runner
        self._event_notifier = event_notifier

    def run_turn(self, turn_id: str) -> WorkspaceTurn:
        """Advance a turn through one workflow invocation or question resume."""
        turn = self._store.get_turn(turn_id)
        conversation = self._store.get_conversation(turn.conversation_id)
        initial_message: str | None
        resume_value: str | None
        if turn.state is TurnState.ACCEPTED:
            turn, initial_message, resume_value = self._start_turn(turn)
        elif turn.state is TurnState.WAITING_FOR_INPUT:
            continuation = self._answered_question(turn, conversation.repository_id)
            if continuation is None:
                return turn
            turn, initial_message, resume_value = continuation
        else:
            raise WorkspaceConflict(
                f"Turn in state {turn.state.value} is not dispatchable by this adapter"
            )

        run_id = turn.run_id
        attempt_id = turn.attempt_id
        if run_id is None or attempt_id is None:
            raise WorkspaceConflict("Active workspace turn has no run binding")
        current_turn = turn
        assistant_message_ids: list[str] = []

        def persist_event(event: WorkflowChatEvent) -> None:
            nonlocal current_turn
            if (
                event.execution_id != run_id
                or event.conversation_id != turn.conversation_id
            ):
                raise WorkspaceConflict("Chat event does not match the active turn")
            request_id = f"workspace-chat:{turn_id}:event:{event.sequence}"
            if event.kind is WorkflowChatEventKind.ASSISTANT_MESSAGE:
                if not event.text or not event.text.strip():
                    return
                message_id = f"{run_id}:assistant:{event.sequence}"
                message = self._store.append_message(
                    turn.conversation_id,
                    message_id=message_id,
                    role=MessageRole.ASSISTANT.value,
                    content=event.text,
                    turn_id=turn_id,
                    run_id=run_id,
                    attempt_id=attempt_id,
                    request_id=request_id,
                )
                assistant_message_ids.append(message.message_id)
            elif event.kind is WorkflowChatEventKind.QUESTION:
                schema = event.data.get("response_schema")
                response_schema = (
                    dict(schema) if isinstance(schema, Mapping) else {"type": "string"}
                )
                self._store.create_question(
                    turn_id,
                    question_id=f"{run_id}:question:{event.sequence}",
                    prompt=event.text or "Please provide a response.",
                    response_schema=response_schema,
                    run_id=run_id,
                    attempt_id=attempt_id,
                    expected_turn_revision=current_turn.revision,
                    request_id=request_id,
                )
                current_turn = self._store.get_turn(turn_id)
            else:
                self._store.append_turn_activity(
                    turn_id,
                    run_id=run_id,
                    attempt_id=attempt_id,
                    activity_id=f"{run_id}:activity:{event.sequence}",
                    kind=event.kind.value,
                    data={
                        "path": event.path,
                        "text": event.text,
                        "details": dict(event.data),
                    },
                    request_id=request_id,
                )
            self._notify(conversation.repository_id)

        config = replace(
            self._workflow_config,
            conversation_id=turn.conversation_id,
            execution_id=run_id,
            initial_message=initial_message,
            defer_user_input=True,
            resume_value=resume_value,
        )
        try:
            exit_code = self._chat_runner(config, event_callback=persist_event)
        except Exception as error:  # noqa: BLE001 - persist a durable turn failure
            latest = self._store.get_turn(turn_id)
            return self._fail_turn(
                latest,
                f"Procedrr chat raised {type(error).__name__}: {error}",
            )

        latest = self._store.get_turn(turn_id)
        if exit_code == WORKFLOW_CHAT_WAITING_FOR_INPUT:
            if latest.state is not TurnState.WAITING_FOR_INPUT:
                return self._fail_turn(
                    latest,
                    "Procedrr chat requested input without a persisted question.",
                )
            return latest
        if exit_code == 0:
            if latest.state is not TurnState.RUNNING:
                return self._fail_turn(
                    latest,
                    "Procedrr chat completed while the turn was not running.",
                )
            if not assistant_message_ids:
                message = self._store.append_message(
                    turn.conversation_id,
                    message_id=f"{run_id}:assistant:completed",
                    role=MessageRole.ASSISTANT.value,
                    content="Workflow completed successfully.",
                    turn_id=turn_id,
                    run_id=run_id,
                    attempt_id=attempt_id,
                    request_id=f"workspace-chat:{turn_id}:completion-message",
                )
                assistant_message_ids.append(message.message_id)
            latest = self._store.transition_turn(
                turn_id,
                expected_revision=latest.revision,
                state=TurnState.SUCCEEDED,
                final_message_id=assistant_message_ids[-1],
                request_id=f"workspace-chat:{turn_id}:succeeded",
            )
            self._notify(conversation.repository_id)
            return latest
        return self._fail_turn(latest, f"Procedrr chat exited with code {exit_code}.")

    def _start_turn(self, turn: WorkspaceTurn) -> tuple[WorkspaceTurn, str, None]:
        user_message = self._store.get_message(turn.user_message_id)
        run_id = f"chat-{uuid4().hex}"
        attempt_id = f"attempt-{uuid4().hex}"
        running = self._store.transition_turn(
            turn.turn_id,
            expected_revision=turn.revision,
            state=TurnState.RUNNING,
            run_id=run_id,
            attempt_id=attempt_id,
            request_id=f"workspace-chat:{turn.turn_id}:start",
        )
        conversation = self._store.get_conversation(turn.conversation_id)
        self._notify(conversation.repository_id)
        return running, user_message.content, None

    def _answered_question(
        self, turn: WorkspaceTurn, repository_id: str
    ) -> tuple[WorkspaceTurn, None, str] | None:
        questions = [
            question
            for question in self._store.list_undelivered_question_answers(repository_id)
            if question.turn_id == turn.turn_id
            and question.run_id == turn.run_id
            and question.attempt_id == turn.attempt_id
            and question.state is QuestionState.ANSWERED
        ]
        if not questions:
            return None
        if len(questions) != 1:
            raise WorkspaceConflict("Turn has multiple answered questions")
        question: WorkspaceQuestion = questions[0]
        answer = question.answer
        if not isinstance(answer, str):
            answer = json.dumps(answer, ensure_ascii=False, separators=(",", ":"))
        receipt_id = (
            f"{turn.run_id}:delivery:{question.question_id}:{question.revision}"
        )
        self._store.mark_question_answer_delivered(
            question.question_id,
            expected_revision=question.revision,
            run_id=question.run_id,
            attempt_id=question.attempt_id,
            delivery_receipt_id=receipt_id,
            request_id=f"workspace-chat:{turn.turn_id}:deliver:{question.question_id}",
        )
        running = self._store.get_turn(turn.turn_id)
        self._notify(repository_id)
        return running, None, answer

    def _fail_turn(self, turn: WorkspaceTurn, reason: str) -> WorkspaceTurn:
        if turn.state not in {
            TurnState.ACCEPTED,
            TurnState.RUNNING,
            TurnState.WAITING_FOR_INPUT,
        }:
            return turn
        failed = self._store.transition_turn(
            turn.turn_id,
            expected_revision=turn.revision,
            state=TurnState.FAILED,
            error=reason,
            request_id=f"workspace-chat:{turn.turn_id}:failed:{turn.revision}",
        )
        conversation = self._store.get_conversation(turn.conversation_id)
        self._notify(conversation.repository_id)
        return failed

    def _notify(self, repository_id: str) -> None:
        if self._event_notifier is None:
            return
        result = self._event_notifier(repository_id)
        if inspect.isawaitable(result):
            raise TypeError("Workspace chat event notifier must be synchronous")


class WorkspaceChatRepositoryRegistry(Protocol):
    def get_repository(self, repository_id: str) -> WorkspaceRepository: ...


class WorkspaceChatTurnDispatcher:
    """Build a conversation-scoped chat runner for each registered repository."""

    def __init__(
        self,
        store: WorkspaceStore,
        repository_registry: WorkspaceChatRepositoryRegistry,
        workflow_config: WorkflowChatConfig,
        *,
        chat_runner: Callable[..., int] = run_procedrr_workflow_chat,
        event_notifier: Callable[[str], Any] | None = None,
    ) -> None:
        self._store = store
        self._repository_registry = repository_registry
        self._workflow_config = workflow_config
        self._chat_runner = chat_runner
        self._event_notifier = event_notifier

    def dispatch_turn(self, turn: WorkspaceTurn) -> WorkspaceTurn:
        """Dispatch a turn with its registered repository as the workflow root."""
        conversation = self._store.get_conversation(turn.conversation_id)
        repository = self._repository_registry.get_repository(
            conversation.repository_id
        )
        config = replace(
            self._workflow_config,
            repo_root=Path(repository.canonical_root),
        )
        runner = WorkspaceChatTurnRunner(
            self._store,
            config,
            chat_runner=self._chat_runner,
            event_notifier=self._event_notifier,
        )
        return runner.run_turn(turn.turn_id)
