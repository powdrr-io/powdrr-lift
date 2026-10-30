import sqlite3
import subprocess
from concurrent.futures import ThreadPoolExecutor
from math import nan
from pathlib import Path

import pytest
from powdrr_contracts.workspace import (
    Conversation,
    EventType,
    Job,
    JobState,
    Message,
    MessageRole,
    QuestionState,
    TurnState,
    WorkspaceContractError,
    WorkspaceEvent,
    WorkspaceQuestion,
    WorkspaceRepository,
    WorkspaceTurn,
)
from powdrr_local.workspace import WorkspaceConflict, WorkspaceStore


def test_conversations_messages_and_spec_links_survive_reopening(
    tmp_path: Path,
) -> None:
    path = tmp_path / "workspace.sqlite3"
    store = WorkspaceStore(path)
    chat = store.create_conversation("repo-a", "New feature")
    store.create_conversation("repo-b", "Other repository")
    store.update_conversation(
        chat.conversation_id,
        title="Design search",
        expected_revision=chat.revision,
        specification_refs=["docs/proposals/search/spec.yaml"],
    )
    updated = store.get_conversation(chat.conversation_id)
    store.update_conversation(
        chat.conversation_id, title="Design search", expected_revision=updated.revision
    )
    first = store.append_message(
        chat.conversation_id, message_id="user-1", role="user", content="Add search."
    )
    second = store.append_message(
        chat.conversation_id,
        message_id="agent-1",
        role="assistant",
        content="What quality level?",
    )
    reopened = WorkspaceStore(path)
    chats = reopened.list_conversations("repo-a")
    assert len(chats) == 1
    assert chats[0].title == "Design search"
    assert chats[0].specification_refs == ("docs/proposals/search/spec.yaml",)
    assert chats[0].revision == 3
    assert reopened.list_messages(chat.conversation_id) == (first, second)
    assert first.sequence < second.sequence


def test_retried_message_submission_is_idempotent_and_conflicts_roll_back(
    tmp_path: Path,
) -> None:
    store = WorkspaceStore(tmp_path / "workspace.db")
    chat = store.create_conversation("repo", "Feature")
    message = store.append_message(
        chat.conversation_id, message_id="request-1", role="user", content="Build it."
    )
    assert (
        store.append_message(
            chat.conversation_id,
            message_id="request-1",
            role="user",
            content="Build it.",
        )
        == message
    )
    before = store.read_events("repo")
    with pytest.raises(WorkspaceConflict):
        store.append_message(
            chat.conversation_id,
            message_id="request-1",
            role="user",
            content="Different request",
        )
    assert store.read_events("repo") == before
    assert store.list_messages(chat.conversation_id) == (message,)


def test_concurrent_workers_cannot_claim_the_same_job(tmp_path: Path) -> None:
    path = tmp_path / "workspace.db"
    store = WorkspaceStore(path)
    job = store.create_job("repo", kind="planning", title="Plan search")

    def claim(_: int) -> bool:
        try:
            WorkspaceStore(path).transition_job(
                job.job_id,
                expected_revision=job.revision,
                state=JobState.RUNNING,
            )
            return True
        except WorkspaceConflict:
            return False

    with ThreadPoolExecutor(max_workers=2) as workers:
        assert sorted(workers.map(claim, range(2))) == [False, True]
    assert store.get_job(job.job_id).state == JobState.RUNNING
    assert len(store.read_events("repo")) == 2


def test_dependencies_release_only_after_all_jobs_succeed(tmp_path: Path) -> None:
    store = WorkspaceStore(tmp_path / "workspace.db")
    first = store.create_job("repo", kind="planning", title="First plan")
    second = store.create_job("repo", kind="planning", title="Second plan")
    dependent = store.create_job(
        "repo",
        kind="pr_execution",
        title="Implement",
        dependency_ids=[first.job_id, second.job_id],
    )
    assert dependent.state == JobState.PENDING
    with pytest.raises(ValueError):
        store.transition_job(
            dependent.job_id, expected_revision=1, state=JobState.RUNNING
        )
    for job in (first, second):
        running = store.transition_job(
            job.job_id, expected_revision=1, state=JobState.RUNNING
        )
        store.transition_job(
            job.job_id,
            expected_revision=running.revision,
            state=JobState.SUCCEEDED,
            artifact_refs=["plan.yaml"],
        )
        if job == first:
            assert store.get_job(dependent.job_id).state == JobState.PENDING
    assert store.get_job(dependent.job_id).state == JobState.READY
    assert (
        store.list_jobs("repo", states=[JobState.READY])[0].job_id == dependent.job_id
    )
    assert store.get_job(first.job_id).artifact_refs == ("plan.yaml",)


def test_replay_cursor_and_interrupted_job_survive_restart(tmp_path: Path) -> None:
    path = tmp_path / "workspace.db"
    store = WorkspaceStore(path)
    job = store.create_job("repo", kind="planning", title="Plan")
    running = store.transition_job(
        job.job_id, expected_revision=1, state=JobState.RUNNING
    )
    cursor = store.read_events("repo")[-1].sequence
    # Opening a second UI connection must not interrupt the owning worker.
    reopened = WorkspaceStore(path)
    assert reopened.get_job(job.job_id).state == JobState.RUNNING
    interrupted = reopened.transition_job(
        job.job_id,
        expected_revision=running.revision,
        state=JobState.INTERRUPTED,
        detail="Worker exited",
    )
    replay = store.read_events("repo", after=cursor)
    assert len(replay) == 1
    assert replay[0].payload["state"] == "interrupted"
    assert replay[0].sequence > cursor
    assert store.read_events("another-repo", after=cursor) == ()
    assert store.get_job(job.job_id) == interrupted
    resumed = store.transition_job(
        job.job_id, expected_revision=interrupted.revision, state=JobState.READY
    )
    assert resumed.state == JobState.READY


def test_invalid_links_do_not_leave_partial_jobs_or_events(tmp_path: Path) -> None:
    store = WorkspaceStore(tmp_path / "workspace.db")
    chat = store.create_conversation("other-repo", "Other feature")
    prerequisite = store.create_job("other-repo", kind="planning", title="Other plan")
    with pytest.raises(ValueError):
        store.create_job(
            "repo", kind="planning", title="Plan", conversation_id=chat.conversation_id
        )
    with pytest.raises(ValueError):
        store.create_job(
            "repo",
            kind="pr_execution",
            title="Implement",
            dependency_ids=[prerequisite.job_id],
        )
    with pytest.raises(KeyError):
        store.create_job(
            "repo", kind="planning", title="Plan", dependency_ids=["missing"]
        )
    assert store.list_jobs("repo") == ()
    assert store.read_events("repo") == ()


def test_conversation_updates_require_the_current_revision(tmp_path: Path) -> None:
    store = WorkspaceStore(tmp_path / "workspace.db")
    chat = store.create_conversation("repo", "Feature")
    store.update_conversation(
        chat.conversation_id, title="Renamed", expected_revision=chat.revision
    )
    before = store.read_events("repo")
    with pytest.raises(WorkspaceConflict):
        store.update_conversation(
            chat.conversation_id, title="Stale rename", expected_revision=chat.revision
        )
    assert store.get_conversation(chat.conversation_id).title == "Renamed"
    assert store.read_events("repo") == before


def test_version_one_database_migrates_without_changing_existing_records(
    tmp_path: Path,
) -> None:
    path = tmp_path / "workspace-v1.db"
    with sqlite3.connect(path) as db:
        db.executescript(
            "CREATE TABLE conversations (conversation_id TEXT PRIMARY KEY, "
            "repository_id TEXT NOT NULL, title TEXT NOT NULL, "
            "specification_refs TEXT NOT NULL, created_at TEXT NOT NULL, "
            "updated_at TEXT NOT NULL); "
            "CREATE TABLE events (sequence INTEGER PRIMARY KEY AUTOINCREMENT, "
            "repository_id TEXT NOT NULL, entity_type TEXT NOT NULL, "
            "entity_id TEXT NOT NULL, event_type TEXT NOT NULL, "
            "payload TEXT NOT NULL, created_at TEXT NOT NULL); "
            "PRAGMA user_version = 1;"
        )
        db.execute(
            "INSERT INTO conversations VALUES (?,?,?,?,?,?)",
            (
                "chat-old",
                "repo-old",
                "Old chat",
                '["docs/old.yaml"]',
                "created",
                "updated",
            ),
        )
        db.execute(
            "INSERT INTO events VALUES (7,?,?,?,?,?,?)",
            (
                "repo-old",
                "conversation",
                "chat-old",
                "conversation.created",
                "{}",
                "created",
            ),
        )
    store = WorkspaceStore(path)
    conversation = store.get_conversation("chat-old")
    with sqlite3.connect(path) as db:
        version = db.execute("PRAGMA user_version").fetchone()[0]
        sequence = db.execute("SELECT sequence FROM events").fetchone()[0]
    assert version == 5
    assert sequence == 7
    assert conversation.revision == 1
    assert conversation.specification_refs == ("docs/old.yaml",)


def test_workspace_contracts_round_trip_and_reject_unknown_fields() -> None:
    message = Message(
        "msg-1", "chat-1", MessageRole.USER, "Build search", "2026-01-01T00:00:00Z", 1
    )
    assert Message.from_dict(message.to_dict()) == message
    conversation = Conversation(
        "chat-1",
        "repo-a",
        "Search",
        ("docs/search.yaml",),
        "2026-01-01T00:00:00Z",
        "2026-01-01T00:00:00Z",
        2,
    )
    assert Conversation.from_dict(conversation.to_dict()) == conversation
    malformed = message.to_dict() | {"debug": "unexpected"}
    with pytest.raises(WorkspaceContractError, match="unexpected fields"):
        Message.from_dict(malformed)


def test_workspace_contracts_reject_unknown_versions_invalid_ids_and_nan() -> None:
    message = Message(
        "msg-1", "chat-1", MessageRole.USER, "Build search", "2026-01-01T00:00:00Z", 1
    )
    with pytest.raises(WorkspaceContractError, match="schema version"):
        Message.from_dict(message.to_dict() | {"schema_version": 2})
    with pytest.raises(WorkspaceContractError, match="opaque ID"):
        Message.from_dict(message.to_dict() | {"message_id": "../secret"})
    event = WorkspaceEvent(
        1,
        "repo-a",
        "job",
        "job-1",
        EventType.JOB_UPDATED,
        {"progress": nan},
        "2026-01-01T00:00:00Z",
    )
    with pytest.raises(WorkspaceContractError, match="finite JSON"):
        WorkspaceEvent.from_dict(event.to_dict())


def test_mutation_requests_replay_the_original_result_and_reject_reuse(
    tmp_path: Path,
) -> None:
    store = WorkspaceStore(tmp_path / "workspace.db")
    chat = store.create_conversation("repo", "Feature", request_id="create-chat")
    assert (
        store.create_conversation("repo", "Feature", request_id="create-chat") == chat
    )
    with pytest.raises(WorkspaceConflict, match="different content"):
        store.create_conversation("repo", "Other", request_id="create-chat")

    message = store.append_message(
        chat.conversation_id,
        message_id="message-1",
        role="user",
        content="Build it",
        request_id="send-message",
    )
    assert (
        store.append_message(
            chat.conversation_id,
            message_id="message-1",
            role="user",
            content="Build it",
            request_id="send-message",
        )
        == message
    )
    with pytest.raises(WorkspaceConflict, match="different content"):
        store.append_message(
            chat.conversation_id,
            message_id="message-1",
            role="user",
            content="Different text",
            request_id="send-message",
        )
    assert len(store.read_events("repo")) == 2


def test_update_and_transition_retries_return_the_first_result(
    tmp_path: Path,
) -> None:
    store = WorkspaceStore(tmp_path / "workspace.db")
    chat = store.create_conversation("repo", "Feature")
    updated = store.update_conversation(
        chat.conversation_id,
        title="Renamed",
        expected_revision=chat.revision,
        request_id="rename-chat",
    )
    assert (
        store.update_conversation(
            chat.conversation_id,
            title="Renamed",
            expected_revision=chat.revision,
            request_id="rename-chat",
        )
        == updated
    )

    job = store.create_job("repo", kind="planning", title="Plan")
    running = store.transition_job(
        job.job_id,
        expected_revision=job.revision,
        state=JobState.RUNNING,
        request_id="start-job",
    )
    succeeded = store.transition_job(
        job.job_id,
        expected_revision=running.revision,
        state=JobState.SUCCEEDED,
    )
    replayed = store.transition_job(
        job.job_id,
        expected_revision=job.revision,
        state=JobState.RUNNING,
        request_id="start-job",
    )
    assert replayed == running
    assert store.get_job(job.job_id) == succeeded


def test_concurrent_identical_create_requests_produce_one_job(
    tmp_path: Path,
) -> None:
    path = tmp_path / "workspace.db"
    WorkspaceStore(path)

    def create(_: int) -> Job:
        return WorkspaceStore(path).create_job(
            "repo", kind="planning", title="Plan", request_id="create-plan"
        )

    with ThreadPoolExecutor(max_workers=2) as workers:
        jobs = tuple(workers.map(create, range(2)))
    assert jobs[0] == jobs[1]
    store = WorkspaceStore(path)
    assert store.list_jobs("repo") == (jobs[0],)
    assert len(store.read_events("repo")) == 1


def test_repository_registration_returns_opaque_id_and_canonical_root(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo with spaces"
    root.mkdir()
    subprocess.run(
        ["git", "init", str(root)], check=True, capture_output=True, text=True
    )
    nested = root / "src" / "feature"
    nested.mkdir(parents=True)
    store = WorkspaceStore(tmp_path / "workspace.db")

    registered = store.register_repository(root, request_id="register-repo")
    retried = store.register_repository(root, request_id="register-repo")
    from_nested_path = store.register_repository(nested)

    assert registered == retried == from_nested_path
    assert registered.canonical_root == str(root.resolve())
    assert registered.repository_id != registered.canonical_root
    assert WorkspaceRepository.from_dict(registered.to_dict()) == registered
    assert "canonical_root" not in registered.to_public_dict()
    assert store.list_repositories() == (registered,)
    assert store.resolve_repository_root(registered.repository_id) == root.resolve()
    events = store.read_events(registered.repository_id)
    assert len(events) == 1
    assert events[0].event_type == EventType.REPOSITORY_REGISTERED


def test_repository_registry_rejects_non_git_paths_and_unknown_ids(
    tmp_path: Path,
) -> None:
    store = WorkspaceStore(tmp_path / "workspace.db")
    with pytest.raises(ValueError, match="Git working tree"):
        store.register_repository(tmp_path)
    assert store.list_repositories() == ()
    with pytest.raises(KeyError, match="not registered"):
        store.resolve_repository_root("unknown-repository")


def test_schema_v3_migrates_to_durable_turns(tmp_path: Path) -> None:
    path = tmp_path / "workspace-v3.db"
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version = 3")
    WorkspaceStore(path)
    with sqlite3.connect(path) as db:
        version = db.execute("PRAGMA user_version").fetchone()[0]
        tables = {
            row[0]
            for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert version == 5
    assert "repositories" in tables
    assert "turns" in tables
    assert "questions" in tables


def test_turn_creation_is_atomic_and_only_one_active_turn_is_allowed(
    tmp_path: Path,
) -> None:
    store = WorkspaceStore(tmp_path / "workspace.db")
    first_chat = store.create_conversation("repo", "First")
    second_chat = store.create_conversation("repo", "Second")
    turn = store.create_turn(
        first_chat.conversation_id,
        turn_id="turn-1",
        message_id="turn-message-1",
        content="Add search",
        request_id="start-turn-1",
    )
    assert (
        store.create_turn(
            first_chat.conversation_id,
            turn_id="turn-1",
            message_id="turn-message-1",
            content="Add search",
            request_id="start-turn-1",
        )
        == turn
    )
    assert WorkspaceTurn.from_dict(turn.to_dict()) == turn
    assert store.list_messages(first_chat.conversation_id)[0].content == "Add search"
    with pytest.raises(WorkspaceConflict, match="active turn"):
        store.create_turn(
            first_chat.conversation_id,
            turn_id="turn-2",
            message_id="turn-message-2",
            content="Another request",
        )
    second_turn = store.create_turn(
        second_chat.conversation_id,
        turn_id="turn-3",
        message_id="turn-message-3",
        content="Independent chat",
    )
    assert second_turn.state == TurnState.ACCEPTED
    assert len(store.read_events("repo")) == 6


def test_question_answers_validate_and_recover_until_delivered(
    tmp_path: Path,
) -> None:
    path = tmp_path / "workspace.db"
    store = WorkspaceStore(path)
    chat = store.create_conversation("repo", "Design")
    turn = store.create_turn(
        chat.conversation_id,
        turn_id="turn-1",
        message_id="user-message-1",
        content="Design search",
    )
    running = store.transition_turn(
        turn.turn_id,
        expected_revision=turn.revision,
        state=TurnState.RUNNING,
        run_id="run-1",
        attempt_id="attempt-1",
    )
    response_schema = {
        "type": "object",
        "properties": {"scope": {"type": "string"}},
        "required": ["scope"],
        "additionalProperties": False,
    }
    question = store.create_question(
        turn.turn_id,
        question_id="question-1",
        prompt="What should search include?",
        response_schema=response_schema,
        run_id="run-1",
        attempt_id="attempt-1",
        expected_turn_revision=running.revision,
        request_id="ask-scope",
    )
    assert WorkspaceQuestion.from_dict(question.to_dict()) == question
    assert store.get_turn(turn.turn_id).state == TurnState.WAITING_FOR_INPUT
    with pytest.raises(ValueError, match="does not match"):
        store.answer_question(
            question.question_id,
            answer={"scope": 42},
            expected_revision=question.revision,
        )

    answered = store.answer_question(
        question.question_id,
        answer={"scope": "product names"},
        expected_revision=question.revision,
        request_id="answer-scope",
    )
    reopened = WorkspaceStore(path)
    assert reopened.list_undelivered_question_answers("repo") == (answered,)
    with pytest.raises(WorkspaceConflict, match="different run or attempt"):
        reopened.mark_question_answer_delivered(
            question.question_id,
            expected_revision=answered.revision,
            run_id="run-1",
            attempt_id="attempt-other",
            delivery_receipt_id="receipt-wrong",
        )
    delivered = reopened.mark_question_answer_delivered(
        question.question_id,
        expected_revision=answered.revision,
        run_id="run-1",
        attempt_id="attempt-1",
        delivery_receipt_id="receipt-1",
        request_id="deliver-scope",
    )
    assert delivered.state == QuestionState.DELIVERED
    assert delivered.delivery_receipt_id == "receipt-1"
    assert reopened.list_undelivered_question_answers("repo") == ()
    assert reopened.get_turn(turn.turn_id).state == TurnState.RUNNING
    assert (
        reopened.answer_question(
            question.question_id,
            answer={"scope": "product names"},
            expected_revision=question.revision,
            request_id="answer-scope",
        )
        == answered
    )
    assert (
        reopened.mark_question_answer_delivered(
            question.question_id,
            expected_revision=answered.revision,
            run_id="run-1",
            attempt_id="attempt-1",
            delivery_receipt_id="receipt-1",
            request_id="deliver-scope",
        )
        == delivered
    )

    assistant_message = reopened.append_message(
        chat.conversation_id,
        message_id="assistant-message-1",
        role="assistant",
        content="I will search product names.",
    )
    finished = reopened.transition_turn(
        turn.turn_id,
        expected_revision=reopened.get_turn(turn.turn_id).revision,
        state=TurnState.SUCCEEDED,
        final_message_id=assistant_message.message_id,
    )
    assert finished.state == TurnState.SUCCEEDED
    assert finished.final_message_id == assistant_message.message_id
