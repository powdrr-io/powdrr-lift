import sqlite3
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from math import nan
from pathlib import Path

import pytest
from powdrr_contracts.workspace import (
    Conversation,
    DispatchIntent,
    EventType,
    Job,
    JobAction,
    JobAttemptSummary,
    JobBlocker,
    JobKind,
    JobOperation,
    JobState,
    Message,
    MessageRole,
    QuestionState,
    SupervisorLease,
    TurnState,
    WorkerOwnership,
    WorkerState,
    WorkspaceApiError,
    WorkspaceApiErrorResponse,
    WorkspaceContractError,
    WorkspaceEvent,
    WorkspaceJobDetail,
    WorkspaceQuestion,
    WorkspaceRepository,
    WorkspaceSnapshot,
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


def test_bounded_history_pages_and_snapshot_cursor_replay(tmp_path: Path) -> None:
    store = WorkspaceStore(tmp_path / "workspace.db")
    conversations = [
        store.create_conversation("repo", f"Feature {index}") for index in range(3)
    ]
    chat = conversations[0]
    messages = [
        store.append_message(
            chat.conversation_id,
            message_id=f"message-{index}",
            role="user",
            content=f"Message {index}",
        )
        for index in range(3)
    ]
    jobs = [
        store.create_job("repo", kind="planning", title=f"Plan {index}")
        for index in range(3)
    ]

    newest_messages = store.list_messages(chat.conversation_id, limit=2)
    assert newest_messages == tuple(messages[1:])
    assert store.list_messages(
        chat.conversation_id, limit=2, before_sequence=messages[1].sequence
    ) == (messages[0],)
    first_conversations = store.list_conversations("repo", limit=2)
    next_conversations = store.list_conversations(
        "repo",
        limit=2,
        before=(
            first_conversations[-1].updated_at,
            first_conversations[-1].conversation_id,
        ),
    )
    assert len(first_conversations) == 2
    assert len(next_conversations) == 1
    first_jobs = store.list_jobs("repo", limit=2)
    next_jobs = store.list_jobs(
        "repo",
        limit=2,
        before=(first_jobs[-1].updated_at, first_jobs[-1].job_id),
    )
    assert len(first_jobs) == 2
    assert len(next_jobs) == 1
    assert {item.job_id for item in first_jobs + next_jobs} == {
        item.job_id for item in jobs
    }

    snapshot = store.read_snapshot("repo", conversation_limit=2, job_limit=2)
    assert len(snapshot.conversations) == 2
    assert len(snapshot.jobs) == 2
    assert WorkspaceSnapshot.from_dict(snapshot.to_dict()) == snapshot
    later = store.create_conversation("repo", "Created after snapshot")
    replay = store.read_events("repo", after=snapshot.event_cursor)
    assert len(replay) == 1
    assert replay[0].entity_id == later.conversation_id
    assert replay[0].schema_version == 2
    assert replay[0].revision == 1


def test_history_limits_are_validated(tmp_path: Path) -> None:
    store = WorkspaceStore(tmp_path / "workspace.db")
    with pytest.raises(ValueError, match="limit"):
        store.list_conversations("repo", limit=0)
    with pytest.raises(ValueError, match="limit"):
        store.list_messages("chat", limit=1001)
    with pytest.raises(ValueError, match="limit"):
        store.list_jobs("repo", limit=0)
    with pytest.raises(ValueError, match="limit"):
        store.list_job_operations("job", limit=1001)


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
    assert version == 11
    assert sequence == 7
    assert conversation.revision == 1
    assert conversation.specification_refs == ("docs/old.yaml",)
    assert store.read_events("repo-old")[0].schema_version == 1


def test_workspace_contracts_round_trip_and_reject_unknown_fields() -> None:
    message = Message(
        "msg-1", "chat-1", MessageRole.USER, "Build search", "2026-01-01T00:00:00Z", 1
    )
    assert Message.from_dict(message.to_dict()) == message
    job = Job(
        "job-1",
        "repo-a",
        JobKind.PLANNING,
        "Plan search",
        JobState.PENDING,
        None,
        ("job-2",),
        ("artifact:plan",),
        "Waiting for dependency",
        3,
        "2026-01-01T00:00:00Z",
        "2026-01-01T00:00:00Z",
    )
    attempt = JobAttemptSummary("attempt-1", "run-1", "revision-1", "created")
    detail = WorkspaceJobDetail(
        job=job,
        attempts=(attempt,),
        stage="waiting",
        blockers=(JobBlocker("job-2", "Prerequisite", JobState.READY),),
        question=None,
        outputs=("artifact:plan",),
        allowed_actions=(JobAction.CANCEL,),
    )
    assert WorkspaceJobDetail.from_dict(detail.to_dict()) == detail
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
    event = WorkspaceEvent(
        1,
        "repo-a",
        "job",
        "job-1",
        EventType.JOB_UPDATED,
        {"revision": 3, "attempt_id": "attempt-1"},
        "2026-01-01T00:00:00Z",
        revision=3,
        attempt_id="attempt-1",
    )
    assert WorkspaceEvent.from_dict(event.to_dict()) == event
    api_error = WorkspaceApiErrorResponse(
        WorkspaceApiError(
            "resync_required",
            "Fetch a fresh snapshot.",
            current_event_cursor=12,
        )
    )
    assert WorkspaceApiErrorResponse.from_dict(api_error.to_dict()) == api_error
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
    assert version == 11
    assert "repositories" in tables
    assert "turns" in tables
    assert "questions" in tables
    assert "job_submissions" in tables


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


def test_start_job_operation_persists_inputs_before_marking_job_running(
    tmp_path: Path,
) -> None:
    store = WorkspaceStore(tmp_path / "workspace.db")
    job = store.create_job("repo", kind="planning", title="Plan search")
    workdir = tmp_path / "feature worktree"
    operation = store.start_job_operation(
        job.job_id,
        expected_revision=job.revision,
        run_id="run-plan-1",
        attempt_id="attempt-plan-1",
        workflow_directory=workdir,
        checkpoint_ref="checkpoint:planning/run-plan-1",
        job_inputs={"pinned_spec_ref": "spec:search-v2", "include_tests": True},
        source_refs=["spec:search-v2"],
        source_spec_revision="revision-7",
        provider_config_refs=["provider:deepinfra-flash"],
        request_id="start-plan",
    )
    assert store.get_job(job.job_id).state == JobState.RUNNING
    assert operation.workflow_directory == str(workdir.resolve())
    assert operation.source_spec_revision == "revision-7"
    assert operation.source_refs == ("spec:search-v2",)
    assert JobOperation.from_dict(operation.to_dict()) == operation
    assert store.get_job_operation(job.job_id, "attempt-plan-1") == operation
    assert store.list_job_operations(job.job_id) == (operation,)
    assert (
        store.start_job_operation(
            job.job_id,
            expected_revision=job.revision,
            run_id="run-plan-1",
            attempt_id="attempt-plan-1",
            workflow_directory=workdir,
            checkpoint_ref="checkpoint:planning/run-plan-1",
            job_inputs={"pinned_spec_ref": "spec:search-v2", "include_tests": True},
            source_refs=["spec:search-v2"],
            source_spec_revision="revision-7",
            provider_config_refs=["provider:deepinfra-flash"],
            request_id="start-plan",
        )
        == operation
    )
    with pytest.raises(WorkspaceConflict, match="different content"):
        store.start_job_operation(
            job.job_id,
            expected_revision=job.revision,
            run_id="run-plan-1",
            attempt_id="attempt-plan-1",
            workflow_directory=workdir,
            checkpoint_ref="checkpoint:planning/run-plan-1",
            job_inputs={"pinned_spec_ref": "spec:search-v3"},
            source_refs=["spec:search-v3"],
            source_spec_revision="revision-8",
            provider_config_refs=["provider:deepinfra-flash"],
            request_id="start-plan",
        )


def test_job_submission_inputs_are_pinned_before_start_and_cannot_drift(
    tmp_path: Path,
) -> None:
    store = WorkspaceStore(tmp_path / "workspace.db")
    inputs = {"pinned_spec_ref": "spec:search-v2", "include_tests": True}
    job = store.create_job(
        "repo",
        kind="planning",
        title="Plan search",
        job_inputs=inputs,
        source_refs=["spec:search-v2"],
        source_spec_revision="revision-7",
        provider_config_refs=["provider:deepinfra-flash"],
        request_id="create-pinned-plan",
    )
    submission = store.get_job_submission(job.job_id)
    assert submission["source_spec_revision"] == "revision-7"
    assert submission["source_refs"] == ("spec:search-v2",)
    assert submission["job_inputs"] == inputs
    assert submission["provider_config_refs"] == ("provider:deepinfra-flash",)
    assert store.get_job(job.job_id).state == JobState.READY

    with pytest.raises(ValueError, match="differ from the saved job submission"):
        store.start_job_operation(
            job.job_id,
            expected_revision=job.revision,
            run_id="run-pinned-plan",
            attempt_id="attempt-pinned-plan",
            workflow_directory=tmp_path / "worktree",
            checkpoint_ref="checkpoint:pinned-plan",
            job_inputs={"pinned_spec_ref": "spec:search-v2", "include_tests": False},
            source_refs=["spec:search-v2"],
            source_spec_revision="revision-7",
            provider_config_refs=["provider:deepinfra-flash"],
        )
    assert store.get_job(job.job_id).state == JobState.READY

    operation = store.start_job_operation(
        job.job_id,
        expected_revision=job.revision,
        run_id="run-pinned-plan",
        attempt_id="attempt-pinned-plan",
        workflow_directory=tmp_path / "worktree",
        checkpoint_ref="checkpoint:pinned-plan",
        job_inputs=inputs,
        source_refs=["spec:search-v2"],
        source_spec_revision="revision-7",
        provider_config_refs=["provider:deepinfra-flash"],
    )
    assert operation.job_inputs == inputs
    assert store.get_job(job.job_id).state == JobState.RUNNING


def test_operation_binding_rejects_secret_fields_and_unpinned_execution(
    tmp_path: Path,
) -> None:
    store = WorkspaceStore(tmp_path / "workspace.db")
    planning = store.create_job("repo", kind="planning", title="Plan")
    with pytest.raises(ValueError, match="secret field"):
        store.start_job_operation(
            planning.job_id,
            expected_revision=planning.revision,
            run_id="run-1",
            attempt_id="attempt-1",
            workflow_directory=tmp_path / "worktree",
            checkpoint_ref="checkpoint:1",
            job_inputs={
                "pinned_spec_ref": "spec:1",
                "openai_api_key": "never-persist-this",
            },
            source_refs=["spec:1"],
            source_spec_revision="revision-1",
        )
    assert store.get_job(planning.job_id).state == JobState.READY
    assert store.list_job_operations(planning.job_id) == ()

    execution = store.create_job("repo", kind="pr_execution", title="Implement")
    with pytest.raises(ValueError, match="approved_change_ref"):
        store.start_job_operation(
            execution.job_id,
            expected_revision=execution.revision,
            run_id="run-2",
            attempt_id="attempt-2",
            workflow_directory=tmp_path / "worktree",
            checkpoint_ref="checkpoint:2",
            job_inputs={"issue_id": 41},
            source_refs=["issue:41"],
            provider_config_refs=["provider:deepinfra-flash"],
        )
    assert store.get_job(execution.job_id).state == JobState.READY
    approved_operation = store.start_job_operation(
        execution.job_id,
        expected_revision=execution.revision,
        run_id="run-2",
        attempt_id="attempt-3",
        workflow_directory=tmp_path / "worktree",
        checkpoint_ref="checkpoint:2",
        job_inputs={"approved_change_ref": "proposal:search-part-1"},
        source_refs=["proposal:search-part-1"],
        provider_config_refs=["provider:deepinfra-flash"],
    )
    assert approved_operation.source_refs == ("proposal:search-part-1",)
    assert store.get_job(execution.job_id).state == JobState.RUNNING


def test_schema_v5_migrates_to_job_operation_bindings(tmp_path: Path) -> None:
    path = tmp_path / "workspace-v5.db"
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version = 5")
    WorkspaceStore(path)
    with sqlite3.connect(path) as db:
        version = db.execute("PRAGMA user_version").fetchone()[0]
        tables = {
            row[0]
            for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert version == 11
    assert "job_operations" in tables


def test_schema_v6_migrates_to_supervisor_and_worker_ownership(tmp_path: Path) -> None:
    path = tmp_path / "workspace-v6.db"
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version = 6")
    WorkspaceStore(path)
    with sqlite3.connect(path) as db:
        version = db.execute("PRAGMA user_version").fetchone()[0]
        tables = {
            row[0]
            for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert version == 11
    assert "supervisor_lease" in tables
    assert "worker_ownership" in tables


def test_schema_v8_migrates_to_api_sessions(tmp_path: Path) -> None:
    path = tmp_path / "workspace-v8.db"
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version = 8")
    store = WorkspaceStore(path)
    assert store.schema_version == 11
    with sqlite3.connect(path) as db:
        table_names = {
            row[0]
            for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert {"api_bootstrap_uses", "api_sessions"} <= table_names


def test_supervisor_lease_excludes_second_launcher_and_fences_old_generation(
    tmp_path: Path,
) -> None:
    store = WorkspaceStore(tmp_path / "workspace.db")
    instant = datetime(2026, 10, 31, tzinfo=UTC)
    first = store.acquire_supervisor_lease(
        "supervisor-a",
        process_id=100,
        process_started_at="process-start-a",
        lease_seconds=30,
        now=instant,
    )
    assert first.generation == 1
    with pytest.raises(WorkspaceConflict, match="Another supervisor"):
        store.acquire_supervisor_lease(
            "supervisor-b",
            process_id=200,
            process_started_at="process-start-b",
            now=instant + timedelta(seconds=1),
        )
    renewed = store.renew_supervisor_lease(
        first.supervisor_id,
        first.generation,
        lease_seconds=30,
        now=instant + timedelta(seconds=10),
    )
    assert renewed.generation == first.generation
    released = store.release_supervisor_lease(
        first.supervisor_id,
        first.generation,
        now=instant + timedelta(seconds=11),
    )
    assert released.generation == first.generation
    second = store.acquire_supervisor_lease(
        "supervisor-b",
        process_id=200,
        process_started_at="process-start-b",
        now=instant + timedelta(seconds=12),
    )
    assert second.generation == 2
    assert SupervisorLease.from_dict(second.to_dict()) == second
    with pytest.raises(WorkspaceConflict, match="stale or expired"):
        store.renew_supervisor_lease(
            first.supervisor_id,
            first.generation,
            now=instant + timedelta(seconds=13),
        )


def test_worker_lease_heartbeat_cancellation_and_exit_are_durable(
    tmp_path: Path,
) -> None:
    store = WorkspaceStore(tmp_path / "workspace.db")
    instant = datetime(2026, 10, 31, tzinfo=UTC)
    job = store.create_job("repo", kind="planning", title="Plan")
    store.start_job_operation(
        job.job_id,
        expected_revision=job.revision,
        run_id="run-1",
        attempt_id="attempt-1",
        workflow_directory=tmp_path / "worktree",
        checkpoint_ref="checkpoint:run-1",
        job_inputs={"pinned_spec_ref": "spec:1"},
        source_refs=["spec:1"],
        source_spec_revision="revision-1",
        provider_config_refs=["provider:deepinfra"],
    )
    supervisor = store.acquire_supervisor_lease(
        "supervisor-a",
        process_id=100,
        process_started_at="supervisor-start",
        lease_seconds=20,
        now=instant,
    )
    worker = store.claim_job_worker(
        job.job_id,
        "attempt-1",
        worker_id="worker-1",
        supervisor_id=supervisor.supervisor_id,
        supervisor_generation=supervisor.generation,
        process_id=101,
        process_started_at="worker-start",
        dispatch_intent=DispatchIntent.START,
        lease_seconds=8,
        now=instant + timedelta(seconds=1),
    )
    assert worker.state == WorkerState.CLAIMED
    assert WorkerOwnership.from_dict(worker.to_dict()) == worker
    assert (
        store.claim_job_worker(
            job.job_id,
            "attempt-1",
            worker_id="worker-1",
            supervisor_id=supervisor.supervisor_id,
            supervisor_generation=supervisor.generation,
            process_id=101,
            process_started_at="worker-start",
            dispatch_intent=DispatchIntent.START,
            lease_seconds=8,
            now=instant + timedelta(seconds=1),
        )
        == worker
    )

    heartbeat = store.heartbeat_job_worker(
        job.job_id,
        "attempt-1",
        worker_id="worker-1",
        supervisor_id=supervisor.supervisor_id,
        supervisor_generation=supervisor.generation,
        process_id=101,
        process_started_at="worker-start",
        lease_seconds=8,
        now=instant + timedelta(seconds=2),
    )
    assert heartbeat.state == WorkerState.RUNNING
    cancellation = store.request_job_worker_cancellation(
        job.job_id,
        "attempt-1",
        expected_revision=heartbeat.revision,
        request_id="cancel-worker",
    )
    assert cancellation.cancellation_requested
    assert (
        store.request_job_worker_cancellation(
            job.job_id,
            "attempt-1",
            expected_revision=heartbeat.revision,
            request_id="cancel-worker",
        )
        == cancellation
    )
    refreshed = store.heartbeat_job_worker(
        job.job_id,
        "attempt-1",
        worker_id="worker-1",
        supervisor_id=supervisor.supervisor_id,
        supervisor_generation=supervisor.generation,
        process_id=101,
        process_started_at="worker-start",
        lease_seconds=8,
        now=instant + timedelta(seconds=3),
    )
    assert refreshed.cancellation_requested
    exited = store.mark_job_worker_exited(
        job.job_id,
        "attempt-1",
        expected_revision=refreshed.revision,
        worker_id="worker-1",
        supervisor_id=supervisor.supervisor_id,
        supervisor_generation=supervisor.generation,
        process_id=101,
        process_started_at="worker-start",
        now=instant + timedelta(seconds=4),
    )
    assert exited.state == WorkerState.EXITED
    assert (
        store.mark_job_worker_exited(
            job.job_id,
            "attempt-1",
            expected_revision=refreshed.revision,
            worker_id="worker-1",
            supervisor_id=supervisor.supervisor_id,
            supervisor_generation=supervisor.generation,
            process_id=101,
            process_started_at="worker-start",
            now=instant + timedelta(seconds=5),
        )
        == exited
    )
    assert (
        store.list_expired_job_workers("repo", now=instant + timedelta(seconds=30))
        == ()
    )


def test_expired_worker_can_be_reclaimed_only_by_current_supervisor_generation(
    tmp_path: Path,
) -> None:
    store = WorkspaceStore(tmp_path / "workspace.db")
    instant = datetime(2026, 10, 31, tzinfo=UTC)
    job = store.create_job("repo", kind="planning", title="Plan")
    store.start_job_operation(
        job.job_id,
        expected_revision=job.revision,
        run_id="run-1",
        attempt_id="attempt-1",
        workflow_directory=tmp_path / "worktree",
        checkpoint_ref="checkpoint:run-1",
        job_inputs={"pinned_spec_ref": "spec:1"},
        source_refs=["spec:1"],
        source_spec_revision="revision-1",
        provider_config_refs=["provider:deepinfra"],
    )
    first_supervisor = store.acquire_supervisor_lease(
        "supervisor-a",
        process_id=100,
        process_started_at="supervisor-start-a",
        lease_seconds=5,
        now=instant,
    )
    first_worker = store.claim_job_worker(
        job.job_id,
        "attempt-1",
        worker_id="worker-1",
        supervisor_id=first_supervisor.supervisor_id,
        supervisor_generation=first_supervisor.generation,
        process_id=101,
        process_started_at="worker-start-a",
        dispatch_intent=DispatchIntent.START,
        lease_seconds=5,
        now=instant,
    )
    assert store.list_expired_job_workers(
        "repo", now=instant + timedelta(seconds=6)
    ) == (first_worker,)
    second_supervisor = store.acquire_supervisor_lease(
        "supervisor-b",
        process_id=200,
        process_started_at="supervisor-start-b",
        lease_seconds=10,
        now=instant + timedelta(seconds=6),
    )
    assert second_supervisor.generation == first_supervisor.generation + 1
    replacement = store.claim_job_worker(
        job.job_id,
        "attempt-1",
        worker_id="worker-2",
        supervisor_id=second_supervisor.supervisor_id,
        supervisor_generation=second_supervisor.generation,
        process_id=201,
        process_started_at="worker-start-b",
        dispatch_intent=DispatchIntent.RESUME,
        lease_seconds=5,
        now=instant + timedelta(seconds=6),
    )
    assert replacement.state == WorkerState.CLAIMED
    assert replacement.supervisor_generation == 2
    with pytest.raises(WorkspaceConflict, match="Supervisor lease is stale"):
        store.heartbeat_job_worker(
            job.job_id,
            "attempt-1",
            worker_id="worker-1",
            supervisor_id=first_supervisor.supervisor_id,
            supervisor_generation=first_supervisor.generation,
            process_id=101,
            process_started_at="worker-start-a",
            now=instant + timedelta(seconds=7),
        )


def test_api_bootstrap_token_is_single_use_across_store_instances(
    tmp_path: Path,
) -> None:
    path = tmp_path / "workspace.db"
    stores = (WorkspaceStore(path), WorkspaceStore(path))

    def consume(index: int) -> bool:
        return stores[index].consume_api_bootstrap_token(
            "token-digest",
            session_digest=f"session-{index}",
            csrf_digest=f"csrf-{index}",
            created_at="2026-01-01T00:00:00+00:00",
            expires_at="2026-01-01T08:00:00+00:00",
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = tuple(pool.map(consume, (0, 1)))

    assert sorted(outcomes) == [False, True]
    assert stores[0].schema_version == 11
