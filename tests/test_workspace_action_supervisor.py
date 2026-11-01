import asyncio
from pathlib import Path
from typing import Any

import pytest
from powdrr_contracts.workspace import (
    JobAction,
    JobActionIntentState,
    JobState,
    WorkspaceJobActionIntent,
)
from powdrr_local.job_action_supervisor import (
    RetryableJobActionError,
    WorkspaceJobActionSupervisor,
)
from powdrr_local.workspace import WorkspaceStore


class RecordingDispatcher:
    def __init__(self) -> None:
        self.actions: list[WorkspaceJobActionIntent] = []

    def dispatch_job_action(self, action: WorkspaceJobActionIntent) -> None:
        self.actions.append(action)


class RecordingEventSource:
    def __init__(self, *, fail: bool = False) -> None:
        self.repositories: list[str] = []
        self.fail = fail

    def notify(self, repository_id: str) -> None:
        if self.fail:
            raise RuntimeError("notification unavailable")
        self.repositories.append(repository_id)


def _setup(tmp_path: Path) -> tuple[WorkspaceStore, str, str, int]:
    store = WorkspaceStore(tmp_path / "workspace.db")
    job = store.create_job(
        "repo", kind="planning", title="Plan", request_id="create-plan"
    )
    store.accept_job_action(
        "repo",
        job.job_id,
        JobAction.START,
        expected_revision=job.revision,
        request_id="start-plan",
    )
    lease = store.acquire_supervisor_lease(
        "supervisor-1",
        process_id=100,
        process_started_at="supervisor-start",
    )
    return store, job.job_id, lease.supervisor_id, lease.generation


def _supervisor(
    store: WorkspaceStore,
    supervisor_id: str,
    generation: int,
    dispatcher: Any,
    event_source: Any = None,
) -> WorkspaceJobActionSupervisor:
    return WorkspaceJobActionSupervisor(
        store,
        dispatcher,
        supervisor_id=supervisor_id,
        supervisor_generation=generation,
        event_source=event_source,
    )


def test_dispatch_next_marks_intent_completed_after_dispatch(tmp_path: Path) -> None:
    store, job_id, supervisor_id, generation = _setup(tmp_path)
    dispatcher = RecordingDispatcher()
    events = RecordingEventSource()
    supervisor = _supervisor(store, supervisor_id, generation, dispatcher, events)

    completed = asyncio.run(supervisor.dispatch_next("repo"))

    assert completed is not None
    assert completed.job_id == job_id
    assert completed.state == JobActionIntentState.COMPLETED
    assert store.get_job(job_id).state == JobState.READY
    assert len(dispatcher.actions) == 1
    assert dispatcher.actions[0].state == JobActionIntentState.CLAIMED
    assert events.repositories == ["repo", "repo"]
    assert asyncio.run(supervisor.dispatch_next("repo")) is None


def test_retryable_dispatch_releases_intent_for_later_retry(tmp_path: Path) -> None:
    store, _, supervisor_id, generation = _setup(tmp_path)

    class DeferredDispatcher:
        async def dispatch_job_action(self, action: WorkspaceJobActionIntent) -> None:
            assert action.state == JobActionIntentState.CLAIMED
            raise RetryableJobActionError("adapter is temporarily unavailable")

    supervisor = _supervisor(store, supervisor_id, generation, DeferredDispatcher())
    with pytest.raises(RetryableJobActionError):
        asyncio.run(supervisor.dispatch_next("repo"))

    pending = store.list_pending_job_actions("repo")
    assert len(pending) == 1
    assert pending[0].state == JobActionIntentState.PENDING


def test_permanent_dispatch_error_marks_intent_failed(tmp_path: Path) -> None:
    store, _, supervisor_id, generation = _setup(tmp_path)

    class FailingDispatcher:
        def dispatch_job_action(self, action: WorkspaceJobActionIntent) -> None:
            raise ValueError("unsupported action")

    supervisor = _supervisor(store, supervisor_id, generation, FailingDispatcher())
    with pytest.raises(ValueError, match="unsupported action"):
        asyncio.run(supervisor.dispatch_next("repo"))

    with store._connection() as db:
        failed = db.execute(
            "SELECT state FROM job_action_intents ORDER BY created_at LIMIT 1"
        ).fetchone()
    assert failed["state"] == JobActionIntentState.FAILED.value


def test_event_notification_failure_does_not_prevent_dispatch(tmp_path: Path) -> None:
    store, _, supervisor_id, generation = _setup(tmp_path)
    dispatcher = RecordingDispatcher()
    supervisor = _supervisor(
        store,
        supervisor_id,
        generation,
        dispatcher,
        RecordingEventSource(fail=True),
    )

    result = asyncio.run(supervisor.dispatch_next("repo"))

    assert result is not None
    assert result.state == JobActionIntentState.COMPLETED
    assert len(dispatcher.actions) == 1
