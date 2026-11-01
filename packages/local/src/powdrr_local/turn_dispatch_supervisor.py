"""Supervisor-side dispatch for durable workspace chat turns."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable
from typing import Protocol
from uuid import uuid4

from powdrr_contracts.workspace import TurnState, WorkspaceTurn
from starlette.concurrency import run_in_threadpool

from powdrr_local.workspace import WorkspaceStore


class WorkspaceTurnDispatcher(Protocol):
    def dispatch_turn(
        self, turn: WorkspaceTurn
    ) -> WorkspaceTurn | Awaitable[WorkspaceTurn]:
        """Run one accepted turn or deliver one answered continuation."""


class TurnDispatchEventSource(Protocol):
    def notify(self, repository_id: str) -> object | Awaitable[object]: ...


class WorkspaceTurnDispatchSupervisor:
    """Claim and dispatch turns under a durable supervisor generation.

    Turns parked for user input keep their claim until an answer requeues them.
    A turn left running after a dispatcher or supervisor failure stays claimed;
    recovery must first establish worker ownership and confirm the prior attempt
    has stopped.
    """

    def __init__(
        self,
        store: WorkspaceStore,
        dispatcher: WorkspaceTurnDispatcher,
        *,
        supervisor_id: str,
        supervisor_generation: int,
        event_source: TurnDispatchEventSource | None = None,
    ) -> None:
        self._store = store
        self._dispatcher = dispatcher
        self._supervisor_id = supervisor_id
        self._supervisor_generation = supervisor_generation
        self._event_source = event_source

    async def dispatch_next(self, repository_id: str) -> WorkspaceTurn | None:
        """Dispatch the oldest eligible turn, returning ``None`` if none exist."""
        claim_id = uuid4().hex
        turn = await run_in_threadpool(
            self._store.claim_next_turn_dispatch,
            repository_id,
            claim_id=claim_id,
            supervisor_id=self._supervisor_id,
            supervisor_generation=self._supervisor_generation,
        )
        if turn is None:
            return None
        await self._notify(repository_id)
        try:
            dispatched = await self._dispatch(turn)
            if dispatched.turn_id != turn.turn_id:
                raise ValueError("Turn dispatcher returned a different turn")
        except Exception:
            latest = await run_in_threadpool(self._store.get_turn, turn.turn_id)
            await run_in_threadpool(
                self._store.finish_turn_dispatch,
                turn.turn_id,
                claim_id=claim_id,
                supervisor_id=self._supervisor_id,
                supervisor_generation=self._supervisor_generation,
                requeue=latest.state is TurnState.ACCEPTED,
            )
            await self._notify(repository_id)
            raise

        finished = await run_in_threadpool(
            self._store.finish_turn_dispatch,
            turn.turn_id,
            claim_id=claim_id,
            supervisor_id=self._supervisor_id,
            supervisor_generation=self._supervisor_generation,
            requeue=dispatched.state is TurnState.ACCEPTED,
        )
        await self._notify(repository_id)
        return finished

    async def _dispatch(self, turn: WorkspaceTurn) -> WorkspaceTurn:
        method = self._dispatcher.dispatch_turn
        if inspect.iscoroutinefunction(method):
            result = method(turn)
        else:
            result = await run_in_threadpool(method, turn)
        if inspect.isawaitable(result):
            return await result
        return result

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
            # Durable events remain readable; notification only wakes consumers.
            return
