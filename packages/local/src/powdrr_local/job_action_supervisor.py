"""Supervisor-side delivery for durable browser workspace job actions."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable
from typing import Any, Protocol
from uuid import uuid4

from powdrr_contracts.workspace import (
    JobActionIntentState,
    WorkspaceJobActionIntent,
)
from starlette.concurrency import run_in_threadpool

from powdrr_local.workspace import WorkspaceStore


class JobActionDispatcher(Protocol):
    def dispatch_job_action(
        self, action: WorkspaceJobActionIntent
    ) -> Any | Awaitable[Any]: ...


class JobActionEventSource(Protocol):
    def notify(self, repository_id: str) -> Any: ...


class RetryableJobActionError(Exception):
    """The action was not dispatched and should return to the durable queue."""


class WorkspaceJobActionSupervisor:
    """Deliver one claimed action using an existing supervisor lease.

    A completed intent means the dispatcher accepted the action. It does not
    mean that the job's workflow has completed.
    """

    def __init__(
        self,
        store: WorkspaceStore,
        dispatcher: JobActionDispatcher,
        *,
        supervisor_id: str,
        supervisor_generation: int,
        event_source: JobActionEventSource | None = None,
    ) -> None:
        self._store = store
        self._dispatcher = dispatcher
        self._supervisor_id = supervisor_id
        self._supervisor_generation = supervisor_generation
        self._event_source = event_source

    async def dispatch_next(
        self, repository_id: str
    ) -> WorkspaceJobActionIntent | None:
        """Dispatch one pending action, or return ``None`` when the queue is empty."""
        claim_id = uuid4().hex
        intent = await run_in_threadpool(
            self._store.claim_next_job_action_intent,
            repository_id,
            claim_id=claim_id,
            supervisor_id=self._supervisor_id,
            supervisor_generation=self._supervisor_generation,
        )
        if intent is None:
            return None
        await self._notify(repository_id)

        try:
            await self._dispatch(intent)
        except RetryableJobActionError:
            await run_in_threadpool(
                self._store.finish_job_action_intent,
                intent.action_id,
                claim_id=claim_id,
                supervisor_id=self._supervisor_id,
                supervisor_generation=self._supervisor_generation,
                state=JobActionIntentState.PENDING,
            )
            await self._notify(repository_id)
            raise
        except Exception:
            await run_in_threadpool(
                self._store.finish_job_action_intent,
                intent.action_id,
                claim_id=claim_id,
                supervisor_id=self._supervisor_id,
                supervisor_generation=self._supervisor_generation,
                state=JobActionIntentState.FAILED,
            )
            await self._notify(repository_id)
            raise

        completed = await run_in_threadpool(
            self._store.finish_job_action_intent,
            intent.action_id,
            claim_id=claim_id,
            supervisor_id=self._supervisor_id,
            supervisor_generation=self._supervisor_generation,
            state=JobActionIntentState.COMPLETED,
        )
        await self._notify(repository_id)
        return completed

    async def _dispatch(self, intent: WorkspaceJobActionIntent) -> None:
        method = self._dispatcher.dispatch_job_action
        if inspect.iscoroutinefunction(method):
            result = method(intent)
        else:
            result = await run_in_threadpool(method, intent)
        if inspect.isawaitable(result):
            await result

    async def _notify(self, repository_id: str) -> None:
        if self._event_source is None:
            return
        notify = self._event_source.notify
        try:
            if inspect.iscoroutinefunction(notify):
                result = notify(repository_id)
            else:
                result = await run_in_threadpool(notify, repository_id)
            if inspect.isawaitable(result):
                await result
        except Exception:
            # Durable events remain readable; a failed wake-up must not wedge work.
            return
