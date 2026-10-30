from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from powdrr_contracts.workspace import JobState
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
        specification_refs=["docs/proposals/search/spec.yaml"],
    )
    store.update_conversation(chat.conversation_id, title="Design search")
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
