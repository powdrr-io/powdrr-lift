import sqlite3
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
    WorkspaceContractError,
    WorkspaceEvent,
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
    assert version == 3
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
