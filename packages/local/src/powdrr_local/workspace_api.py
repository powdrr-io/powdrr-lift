"""HTTP routes for the local browser workspace."""

from __future__ import annotations

import asyncio
import inspect
import json
import re
import time
from collections.abc import AsyncGenerator, Mapping
from pathlib import Path
from typing import Annotated, Any, Protocol

from fastapi import APIRouter, Depends, FastAPI, Header, Query, Request
from powdrr_contracts.workspace import (
    Conversation,
    Job,
    JobState,
    WorkspaceApiError,
    WorkspaceApiErrorResponse,
    WorkspaceRepository,
)
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse, StreamingResponse

from powdrr_local.workspace import WorkspaceConflict, WorkspaceStore


class WorkspaceRepositoryRegistry(Protocol):
    def list_repositories(self) -> tuple[WorkspaceRepository, ...]: ...

    def register_repository(
        self,
        path: str | Path,
        *,
        display_name: str | None = None,
        request_id: str | None = None,
    ) -> WorkspaceRepository: ...

    def get_repository(self, repository_id: str) -> WorkspaceRepository: ...


class WorkspaceEventSource(Protocol):
    def notify(self, repository_id: str) -> Any: ...


class WorkspaceCommandDispatcher(Protocol):
    def health(self) -> Mapping[str, Any]: ...


class WorkspaceApiRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class RepositoryCreateRequest(WorkspaceApiRequest):
    path: str = Field(min_length=1, max_length=4096)
    display_name: str | None = Field(default=None, max_length=256)
    request_id: str = Field(min_length=1, max_length=128)


class ConversationCreateRequest(WorkspaceApiRequest):
    title: str = Field(min_length=1, max_length=512)
    request_id: str = Field(min_length=1, max_length=128)


class ConversationUpdateRequest(WorkspaceApiRequest):
    title: str = Field(min_length=1, max_length=512)
    expected_revision: int = Field(ge=1)
    specification_refs: list[str] | None = None
    request_id: str = Field(min_length=1, max_length=128)


class WorkspaceApiException(Exception):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        current_revision: int | None = None,
        current_event_cursor: int | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.current_revision = current_revision
        self.current_event_cursor = current_event_cursor

    def response(self) -> JSONResponse:
        body = WorkspaceApiErrorResponse(
            WorkspaceApiError(
                self.code,
                self.message,
                self.current_revision,
                self.current_event_cursor,
            )
        )
        return JSONResponse(body.to_dict(), status_code=self.status_code)


_EVENT_BATCH_LIMIT = 100
_EVENT_POLL_INTERVAL_SECONDS = 0.25
_EVENT_HEARTBEAT_SECONDS = 15
_EVENT_CURSOR_PATTERN = re.compile(r"^(0|[1-9][0-9]*)$")


def require_session(*, mutating: bool = False):
    async def dependency(request: Request) -> None:
        await request.app.state.authorize_request(request, mutating=mutating)

    return dependency


def _conversation_in_repository(
    conversation: Conversation, repository_id: str
) -> Conversation:
    if conversation.repository_id != repository_id:
        raise WorkspaceApiException(404, "not_found", "Conversation was not found.")
    return conversation


async def _notify(
    event_source: WorkspaceEventSource | None, repository_id: str
) -> None:
    if event_source is None:
        return
    if inspect.iscoroutinefunction(event_source.notify):
        result = event_source.notify(repository_id)
    else:
        result = await run_in_threadpool(event_source.notify, repository_id)
    if inspect.isawaitable(result):
        await result


async def _event_stream(
    store: WorkspaceStore,
    repository_id: str,
    after: int,
    request: Request,
) -> AsyncGenerator[str, None]:
    cursor = after
    last_heartbeat = time.monotonic()
    while not await request.is_disconnected():
        now = time.monotonic()
        if now - last_heartbeat >= _EVENT_HEARTBEAT_SECONDS:
            try:
                await request.app.state.authorize_request(request, mutating=False)
            except WorkspaceApiException:
                return
            yield ": keep-alive\n\n"
            last_heartbeat = now

        events = await run_in_threadpool(
            store.read_events,
            repository_id,
            after=cursor,
            limit=_EVENT_BATCH_LIMIT,
        )
        if events:
            for event in events:
                if await request.is_disconnected():
                    return
                data = json.dumps(
                    event.to_dict(), ensure_ascii=False, separators=(",", ":")
                )
                yield (
                    f"id: {event.sequence}\n"
                    f"event: {event.event_type.value}\n"
                    f"data: {data}\n\n"
                )
                cursor = event.sequence
            continue

        await asyncio.sleep(_EVENT_POLL_INTERVAL_SECONDS)


def install_workspace_routes(
    app: FastAPI,
    *,
    store: WorkspaceStore,
    repository_registry: WorkspaceRepositoryRegistry,
    dispatcher: WorkspaceCommandDispatcher | None,
    event_source: WorkspaceEventSource | None,
) -> None:
    router = APIRouter(prefix="/api/v1")

    @router.get("/health")
    async def health() -> dict[str, Any]:
        if dispatcher is None:
            scheduler: Mapping[str, Any] = {"status": "not_configured"}
        elif inspect.iscoroutinefunction(dispatcher.health):
            scheduler = await dispatcher.health()
        else:
            scheduler = await run_in_threadpool(dispatcher.health)
        return {
            "status": "ok",
            "schema_version": await run_in_threadpool(lambda: store.schema_version),
            "scheduler": dict(scheduler),
        }

    @router.get("/repositories")
    async def list_repositories(
        _: None = Depends(require_session()),
    ) -> dict[str, Any]:
        repositories = await run_in_threadpool(repository_registry.list_repositories)
        return {"repositories": [item.to_public_dict() for item in repositories]}

    @router.post("/repositories", status_code=201)
    async def register_repository(
        payload: RepositoryCreateRequest,
        _: None = Depends(require_session(mutating=True)),
    ) -> dict[str, Any]:
        try:
            repository = await run_in_threadpool(
                repository_registry.register_repository,
                payload.path,
                display_name=payload.display_name,
                request_id=payload.request_id,
            )
        except WorkspaceConflict as error:
            raise WorkspaceApiException(
                409, "request_conflict", "The request conflicts with saved data."
            ) from error
        except (ValueError, RuntimeError) as error:
            raise WorkspaceApiException(
                422, "invalid_repository", "The repository could not be registered."
            ) from error
        await _notify(event_source, repository.repository_id)
        return repository.to_public_dict()

    @router.get("/repositories/{repository_id}/snapshot")
    async def repository_snapshot(
        repository_id: str,
        _: None = Depends(require_session()),
    ) -> dict[str, Any]:
        await _get_repository(repository_registry, repository_id)
        snapshot = await run_in_threadpool(store.read_snapshot, repository_id)
        return snapshot.to_dict()

    @router.get("/repositories/{repository_id}/events")
    async def stream_events(
        repository_id: str,
        request: Request,
        after: int = Query(default=0, ge=0),
        last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
        _: None = Depends(require_session()),
    ) -> StreamingResponse:
        await _get_repository(repository_registry, repository_id)
        if last_event_id is not None:
            if _EVENT_CURSOR_PATTERN.fullmatch(last_event_id) is None:
                raise WorkspaceApiException(
                    422,
                    "invalid_cursor",
                    "The event cursor must be a non-negative integer.",
                )
            cursor = int(last_event_id)
        else:
            cursor = after

        current_cursor = await run_in_threadpool(store.event_cursor, repository_id)
        if cursor > current_cursor:
            raise WorkspaceApiException(
                409,
                "resync_required",
                "The event cursor is ahead of this repository. Fetch a fresh "
                "snapshot and reconnect from its event cursor.",
                current_event_cursor=current_cursor,
            )

        return StreamingResponse(
            _event_stream(store, repository_id, cursor, request),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    @router.get("/repositories/{repository_id}/jobs")
    async def list_jobs(
        repository_id: str,
        states: Annotated[list[JobState] | None, Query()] = None,
        limit: int = Query(default=100, ge=1, le=1000),
        before_updated_at: str | None = None,
        before_job_id: str | None = None,
        _: None = Depends(require_session()),
    ) -> dict[str, Any]:
        await _get_repository(repository_registry, repository_id)
        if (before_updated_at is None) != (before_job_id is None):
            raise WorkspaceApiException(
                422,
                "invalid_cursor",
                "Both job cursor fields must be supplied together.",
            )
        before = (
            None
            if before_updated_at is None
            else (before_updated_at, before_job_id or "")
        )
        jobs = await run_in_threadpool(
            store.list_jobs,
            repository_id,
            states=states or (),
            limit=limit,
            before=before,
        )
        last = jobs[-1] if len(jobs) == limit else None
        return {
            "jobs": [item.to_dict() for item in jobs],
            "next_cursor": (
                None
                if last is None
                else {"updated_at": last.updated_at, "job_id": last.job_id}
            ),
        }

    @router.get("/repositories/{repository_id}/jobs/{job_id}")
    async def get_job(
        repository_id: str,
        job_id: str,
        _: None = Depends(require_session()),
    ) -> dict[str, Any]:
        await _get_repository(repository_registry, repository_id)
        job = await _get_job_in_repository(store, repository_id, job_id)
        operations = await run_in_threadpool(store.list_job_operations, job_id)
        return {
            "job": job.to_dict(),
            "attempts": [
                {
                    "attempt_id": item.attempt_id,
                    "run_id": item.run_id,
                    "source_spec_revision": item.source_spec_revision,
                    "created_at": item.created_at,
                }
                for item in operations
            ],
        }

    @router.get("/repositories/{repository_id}/conversations")
    async def list_conversations(
        repository_id: str,
        limit: int = Query(default=100, ge=1, le=1000),
        before_updated_at: str | None = None,
        before_conversation_id: str | None = None,
        _: None = Depends(require_session()),
    ) -> dict[str, Any]:
        await _get_repository(repository_registry, repository_id)
        if (before_updated_at is None) != (before_conversation_id is None):
            raise WorkspaceApiException(
                422,
                "invalid_cursor",
                "Both conversation cursor fields must be supplied together.",
            )
        before = (
            None
            if before_updated_at is None
            else (before_updated_at, before_conversation_id or "")
        )
        conversations = await run_in_threadpool(
            store.list_conversations,
            repository_id,
            limit=limit,
            before=before,
        )
        last = conversations[-1] if len(conversations) == limit else None
        return {
            "conversations": [item.to_dict() for item in conversations],
            "next_cursor": (
                None
                if last is None
                else {
                    "updated_at": last.updated_at,
                    "conversation_id": last.conversation_id,
                }
            ),
        }

    @router.post("/repositories/{repository_id}/conversations", status_code=201)
    async def create_conversation(
        repository_id: str,
        payload: ConversationCreateRequest,
        _: None = Depends(require_session(mutating=True)),
    ) -> dict[str, Any]:
        await _get_repository(repository_registry, repository_id)
        try:
            conversation = await run_in_threadpool(
                store.create_conversation,
                repository_id,
                payload.title,
                request_id=payload.request_id,
            )
        except WorkspaceConflict as error:
            raise WorkspaceApiException(
                409, "request_conflict", "The request conflicts with saved data."
            ) from error
        await _notify(event_source, repository_id)
        return conversation.to_dict()

    @router.get("/repositories/{repository_id}/conversations/{conversation_id}")
    async def get_conversation(
        repository_id: str,
        conversation_id: str,
        _: None = Depends(require_session()),
    ) -> dict[str, Any]:
        await _get_repository(repository_registry, repository_id)
        conversation = await run_in_threadpool(store.get_conversation, conversation_id)
        return _conversation_in_repository(conversation, repository_id).to_dict()

    @router.patch("/repositories/{repository_id}/conversations/{conversation_id}")
    async def update_conversation(
        repository_id: str,
        conversation_id: str,
        payload: ConversationUpdateRequest,
        _: None = Depends(require_session(mutating=True)),
    ) -> dict[str, Any]:
        await _get_repository(repository_registry, repository_id)
        await _get_conversation_in_repository(store, repository_id, conversation_id)
        try:
            updated = await run_in_threadpool(
                store.update_conversation,
                conversation_id,
                title=payload.title,
                expected_revision=payload.expected_revision,
                specification_refs=payload.specification_refs,
                request_id=payload.request_id,
            )
        except WorkspaceConflict as error:
            latest = await _get_conversation_in_repository(
                store, repository_id, conversation_id
            )
            raise WorkspaceApiException(
                409,
                "stale_revision",
                "This conversation changed in another window.",
                current_revision=latest.revision,
            ) from error
        await _notify(event_source, repository_id)
        return updated.to_dict()

    @router.get(
        "/repositories/{repository_id}/conversations/{conversation_id}/messages"
    )
    async def list_messages(
        repository_id: str,
        conversation_id: str,
        limit: int = Query(default=100, ge=1, le=1000),
        before_sequence: int | None = Query(default=None, ge=1),
        _: None = Depends(require_session()),
    ) -> dict[str, Any]:
        await _get_repository(repository_registry, repository_id)
        await _get_conversation_in_repository(store, repository_id, conversation_id)
        messages = await run_in_threadpool(
            store.list_messages,
            conversation_id,
            limit=limit,
            before_sequence=before_sequence,
        )
        return {
            "messages": [item.to_dict() for item in messages],
            "next_before_sequence": (
                messages[0].sequence if len(messages) == limit else None
            ),
        }

    app.include_router(router)


async def _get_repository(
    registry: WorkspaceRepositoryRegistry, repository_id: str
) -> WorkspaceRepository:
    try:
        return await run_in_threadpool(registry.get_repository, repository_id)
    except KeyError as error:
        raise WorkspaceApiException(
            404, "not_found", "Repository was not found."
        ) from error


async def _get_conversation_in_repository(
    store: WorkspaceStore, repository_id: str, conversation_id: str
) -> Conversation:
    try:
        conversation = await run_in_threadpool(store.get_conversation, conversation_id)
    except KeyError as error:
        raise WorkspaceApiException(
            404, "not_found", "Conversation was not found."
        ) from error
    return _conversation_in_repository(conversation, repository_id)


async def _get_job_in_repository(
    store: WorkspaceStore, repository_id: str, job_id: str
) -> Job:
    try:
        job = await run_in_threadpool(store.get_job, job_id)
    except KeyError as error:
        raise WorkspaceApiException(404, "not_found", "Job was not found.") from error
    if job.repository_id != repository_id:
        raise WorkspaceApiException(404, "not_found", "Job was not found.")
    return job
