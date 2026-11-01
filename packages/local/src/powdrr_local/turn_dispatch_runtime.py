"""FastAPI-lifespan runtime for the durable workspace turn supervisor."""

from __future__ import annotations

import asyncio
import inspect
import os
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import uuid4

from powdrr_contracts.workspace import WorkspaceRepository
from starlette.concurrency import run_in_threadpool

from powdrr_local.turn_dispatch_supervisor import (
    TurnDispatchEventSource,
    WorkspaceTurnDispatcher,
    WorkspaceTurnDispatchSupervisor,
)
from powdrr_local.workspace import WorkspaceConflict, WorkspaceStore


class WorkspaceRepositorySource(Protocol):
    def list_repositories(self) -> tuple[WorkspaceRepository, ...]: ...


class WorkspaceTurnDispatchRuntime:
    """Run the turn supervisor while the configured API process is alive.

    The database lease elects one active dispatcher across service processes.
    Only accepted turns and answered questions are eligible for dispatch; turns
    left running are not restarted until a worker ownership recovery path exists.
    """

    def __init__(
        self,
        store: WorkspaceStore,
        repositories: WorkspaceRepositorySource,
        dispatcher: WorkspaceTurnDispatcher,
        *,
        event_source: TurnDispatchEventSource | None = None,
        lease_seconds: int = 30,
        poll_interval_seconds: float = 0.25,
        on_error: Callable[[Exception], Any] | None = None,
    ) -> None:
        if lease_seconds < 6:
            raise ValueError("lease_seconds must be at least six seconds")
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be positive")
        self._store = store
        self._repositories = repositories
        self._dispatcher = dispatcher
        self._event_source = event_source
        self._lease_seconds = lease_seconds
        self._poll_interval_seconds = poll_interval_seconds
        self._on_error = on_error
        self._supervisor_id = f"workspace-{uuid4().hex}"
        self._process_started_at = datetime.now(UTC).isoformat()
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._active_dispatch: asyncio.Task[Any] | None = None
        self._status = "stopped"
        self._generation: int | None = None
        self._last_error_type: str | None = None
        self._last_dispatch_at: str | None = None

    def start(self) -> None:
        """Start the supervisor task once during application startup."""
        if self._task is not None:
            return
        self._stop.clear()
        self._status = "starting"
        self._task = asyncio.create_task(self._run(), name="workspace-turn-dispatch")

    async def stop(self) -> None:
        """Stop polling and release the lease after any active dispatch returns."""
        task = self._task
        if task is None:
            return
        self._stop.set()
        await task
        self._task = None
        self._status = "stopped"

    def health(self) -> dict[str, Any]:
        """Return a bounded scheduler status suitable for the health response."""
        return {
            "status": self._status,
            "supervisor_generation": self._generation,
            "last_dispatch_at": self._last_dispatch_at,
            "last_error_type": self._last_error_type,
        }

    async def _run(self) -> None:
        lease = None
        supervisor: WorkspaceTurnDispatchSupervisor | None = None
        renew_at = 0.0
        loop = asyncio.get_running_loop()
        try:
            while not self._stop.is_set():
                now = loop.time()
                if lease is None:
                    try:
                        lease = await run_in_threadpool(
                            self._store.acquire_supervisor_lease,
                            self._supervisor_id,
                            process_id=os.getpid(),
                            process_started_at=self._process_started_at,
                            lease_seconds=self._lease_seconds,
                        )
                    except WorkspaceConflict as error:
                        self._status = "standby"
                        self._report_error(error)
                        await self._sleep()
                        continue
                    self._generation = lease.generation
                    self._status = "running"
                    renew_at = now + self._lease_seconds / 3
                    supervisor = WorkspaceTurnDispatchSupervisor(
                        self._store,
                        self._dispatcher,
                        supervisor_id=lease.supervisor_id,
                        supervisor_generation=lease.generation,
                        event_source=self._event_source,
                    )

                if now >= renew_at:
                    try:
                        lease = await run_in_threadpool(
                            self._store.renew_supervisor_lease,
                            self._supervisor_id,
                            lease.generation,
                            lease_seconds=self._lease_seconds,
                        )
                    except WorkspaceConflict as error:
                        self._status = "standby"
                        self._report_error(error)
                        lease = None
                        supervisor = None
                        await self._sleep()
                        continue
                    renew_at = now + self._lease_seconds / 3

                assert supervisor is not None
                try:
                    dispatched = await self._dispatch_with_lease_renewal(
                        supervisor, renew_at, lease.generation
                    )
                except Exception as error:  # noqa: BLE001 - keep the local API alive
                    self._report_error(error)
                    dispatched = None
                if dispatched is not None:
                    self._last_dispatch_at = datetime.now(UTC).isoformat()
                    renew_at = loop.time() + self._lease_seconds / 3
                else:
                    await self._sleep()
        finally:
            if lease is not None and (
                self._active_dispatch is None or self._active_dispatch.done()
            ):
                try:
                    await run_in_threadpool(
                        self._store.release_supervisor_lease,
                        self._supervisor_id,
                        lease.generation,
                    )
                except WorkspaceConflict:
                    pass

    async def _dispatch_with_lease_renewal(
        self,
        supervisor: WorkspaceTurnDispatchSupervisor,
        renew_at: float,
        generation: int,
    ) -> Any:
        loop = asyncio.get_running_loop()
        repositories = await run_in_threadpool(self._repositories.list_repositories)
        for repository in repositories:
            task = asyncio.create_task(
                supervisor.dispatch_next(repository.repository_id)
            )
            self._active_dispatch = task
            while not task.done():
                delay = max(0.05, renew_at - loop.time())
                await asyncio.wait({task}, timeout=delay)
                if task.done():
                    break
                try:
                    await run_in_threadpool(
                        self._store.renew_supervisor_lease,
                        self._supervisor_id,
                        generation,
                        lease_seconds=self._lease_seconds,
                    )
                except Exception:
                    # Do not start another dispatch in this process while the
                    # current adapter call is still running after lease loss.
                    try:
                        await task
                    finally:
                        if self._active_dispatch is task:
                            self._active_dispatch = None
                    raise
                renew_at = loop.time() + self._lease_seconds / 3
            try:
                result = await task
            finally:
                if self._active_dispatch is task:
                    self._active_dispatch = None
            if result is not None:
                return result
        return None

    async def _sleep(self) -> None:
        try:
            await asyncio.wait_for(
                self._stop.wait(), timeout=self._poll_interval_seconds
            )
        except TimeoutError:
            return

    def _report_error(self, error: Exception) -> None:
        self._last_error_type = type(error).__name__
        if self._on_error is not None:
            result = self._on_error(error)
            if inspect.isawaitable(result):
                raise TypeError("Workspace dispatch on_error callback must be sync")
