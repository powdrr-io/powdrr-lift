from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Mapping
from io import StringIO
from pathlib import Path
from typing import Any

from powdrr_contracts.workspace import EventType, QuestionState, TurnState
from powdrr_local.workspace import WorkspaceStore
from powdrr_service.resources import bundled_process_directory

from powdrr_lift.workrr import procedrr_chat
from powdrr_lift.workrr.chat_config import WorkflowChatConfig
from powdrr_lift.workrr.procedrr import WorkrrProcedrrClient
from powdrr_lift.workrr.workspace_chat_turn import (
    WorkspaceChatTurnDispatcher,
    WorkspaceChatTurnRunner,
)


class SelectingProvider:
    def complete_json(
        self,
        messages: list[dict[str, str]],
        *,
        response_schema: Mapping[str, Any],
    ) -> dict[str, Any]:
        _ = (messages, response_schema)
        return {
            "process_name": "echo-process",
            "reason": "The scripted process handles this chat.",
        }


def test_workspace_turn_runs_and_resumes_procedrr_chat(
    tmp_path: Path, monkeypatch: Any
) -> None:
    process_dir = tmp_path / "processes"
    process_dir.mkdir()
    shutil.copy(bundled_process_directory() / "select-process.yaml", process_dir)
    process_source = "\n".join(
        [
            "version: 1",
            "name: echo-process",
            "description: Ask a durable question and return the answer.",
            "inputs:",
            "  - {name: user_request, type: string, required: true}",
            "  - {name: workflow_context, type: object, required: true}",
            "  - {name: language_labels, type: array, required: true}",
            "steps:",
            "  - operation:",
            "      tool: respond",
            '      parameters: {text: "Started: ${user_request}"}',
            "      bind: started",
            "  - operation:",
            "      tool: prompt_user",
            '      parameters: {text: "Which region should I use?"}',
            "      bind: reply",
            "  - operation:",
            "      tool: respond",
            '      parameters: {text: "Selected: ${reply.answer}"}',
            "      bind: response",
            "  - terminal: succeeded",
            "",
        ]
    )
    (process_dir / "echo-process.yaml").write_text(process_source, encoding="utf-8")
    repo_root = tmp_path / ".worktrees" / "chat"
    repo_root.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=repo_root, check=True)
    monkeypatch.setattr(
        procedrr_chat, "_persist_workflow_context", lambda *_args, **_kwargs: None
    )

    store = WorkspaceStore(tmp_path / "workspace.sqlite3")
    conversation = store.create_conversation("repo-id", "Design interview")
    turn = store.create_turn(
        conversation.conversation_id,
        turn_id="turn-one",
        message_id="user-one",
        content="Configure the deployment.",
    )
    client = WorkrrProcedrrClient(
        SelectingProvider(),  # type: ignore[arg-type]
        process_directory=process_dir,
        provider_retry_attempts=0,
    )
    chat_stderr = StringIO()
    runner = WorkspaceChatTurnRunner(
        store,
        WorkflowChatConfig(
            processes_dir=process_dir,
            repo_root=repo_root,
            provider="local",
        ),
        chat_runner=lambda config, **kwargs: procedrr_chat.run_procedrr_workflow_chat(
            config,
            procedrr_client_factory=lambda _role: client,
            input_func=lambda: (_ for _ in ()).throw(
                AssertionError("workspace chat must not block for terminal input")
            ),
            stderr=chat_stderr,
            **kwargs,
        ),
    )

    waiting = runner.run_turn(turn.turn_id)

    assert waiting.state is TurnState.WAITING_FOR_INPUT, chat_stderr.getvalue()
    assert waiting.run_id is not None
    assert waiting.attempt_id is not None
    snapshot = store.read_snapshot("repo-id")
    question = next(q for q in snapshot.active_questions if q.turn_id == turn.turn_id)
    assert question.prompt == "Which region should I use?"
    assert question.response_schema == {"type": "string"}
    before_resume = store.list_messages(conversation.conversation_id)
    assert [message.content for message in before_resume] == [
        "Configure the deployment.",
        "Started: Configure the deployment.",
    ]

    store.answer_question(
        question.question_id,
        answer="us-west-2",
        expected_revision=question.revision,
        request_id="answer-one",
    )
    succeeded = runner.run_turn(turn.turn_id)

    assert succeeded.state is TurnState.SUCCEEDED
    assert succeeded.final_message_id is not None
    delivered = store.get_question(question.question_id)
    assert delivered.state is QuestionState.DELIVERED
    messages = store.list_messages(conversation.conversation_id)
    assert messages[-1].content == "Selected: us-west-2"
    activities = [
        event
        for event in store.read_events("repo-id")
        if event.event_type is EventType.TURN_ACTIVITY
    ]
    assert any(event.payload["kind"] == "stage" for event in activities)
    assert any(event.payload["kind"] == "outcome" for event in activities)
    manifest_path = repo_root / ".powdrr" / "procedrr-runs" / f"{succeeded.run_id}.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["status"] == "succeeded"


def test_chat_dispatcher_uses_the_turns_registered_repository(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / ".worktrees" / "registered-repository"
    repo_root.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=repo_root, check=True)
    store = WorkspaceStore(tmp_path / "workspace.sqlite3")
    repository = store.register_repository(repo_root, request_id="register-repo")
    conversation = store.create_conversation(repository.repository_id, "Interview")
    turn = store.create_turn(
        conversation.conversation_id,
        turn_id="registered-turn",
        message_id="registered-user-message",
        content="Design a deployment flow.",
    )
    observed: dict[str, Any] = {}

    def fake_chat(config: WorkflowChatConfig, **_kwargs: Any) -> int:
        observed["repo_root"] = config.repo_root
        observed["initial_message"] = config.initial_message
        observed["defer_user_input"] = config.defer_user_input
        return 0

    dispatcher = WorkspaceChatTurnDispatcher(
        store,
        store,
        WorkflowChatConfig(
            processes_dir=tmp_path / "processes",
            repo_root=tmp_path / "wrong-repository",
        ),
        chat_runner=fake_chat,
    )

    completed = dispatcher.dispatch_turn(turn)

    assert completed.state is TurnState.SUCCEEDED
    assert observed == {
        "repo_root": repo_root.resolve(),
        "initial_message": "Design a deployment flow.",
        "defer_user_input": True,
    }
