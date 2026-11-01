"""Secure local FastAPI application factory for the browser workspace."""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from powdrr_contracts.workspace import (
    WorkspaceApiError,
    WorkspaceApiErrorResponse,
)
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import JSONResponse

from powdrr_local.turn_dispatch_runtime import (
    WorkspaceTurnDispatchRuntime,
)
from powdrr_local.turn_dispatch_supervisor import WorkspaceTurnDispatcher
from powdrr_local.workspace import WorkspaceConflict, WorkspaceStore
from powdrr_local.workspace_api import (
    WorkspaceApiException,
    WorkspaceCommandDispatcher,
    WorkspaceEventSource,
    WorkspaceRepositoryRegistry,
    install_workspace_routes,
)

_SESSION_COOKIE = "powdrr_session"
_SESSION_MAX_AGE_SECONDS = 8 * 60 * 60


class BootstrapRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    token: str = Field(min_length=32, max_length=256)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(UTC)


def create_app(
    *,
    store: WorkspaceStore,
    bootstrap_secret: str,
    expected_origin: str,
    repository_registry: WorkspaceRepositoryRegistry | None = None,
    dispatcher: WorkspaceCommandDispatcher | None = None,
    event_source: WorkspaceEventSource | None = None,
    turn_dispatcher: WorkspaceTurnDispatcher | None = None,
    session_max_age: int = _SESSION_MAX_AGE_SECONDS,
) -> FastAPI:
    """Build an app without starting global services or touching credentials."""
    if len(bootstrap_secret) < 32:
        raise ValueError("bootstrap_secret must contain at least 32 characters")
    parsed = urlsplit(expected_origin)
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname is None
        or parsed.path
        or parsed.query
        or parsed.fragment
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("expected_origin must be an absolute loopback origin")
    try:
        loopback = (
            parsed.hostname == "localhost"
            or ipaddress.ip_address(parsed.hostname).is_loopback
        )
    except ValueError:
        loopback = False
    if not loopback:
        raise ValueError("expected_origin must use a loopback hostname")
    if not 60 <= session_max_age <= 24 * 60 * 60:
        raise ValueError("session_max_age must be between one minute and one day")

    repository_source = repository_registry or store
    turn_runtime = (
        WorkspaceTurnDispatchRuntime(
            store,
            repository_source,
            turn_dispatcher,
            event_source=event_source,
        )
        if turn_dispatcher is not None
        else None
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        if turn_runtime is not None:
            turn_runtime.start()
        try:
            yield
        finally:
            if turn_runtime is not None:
                await turn_runtime.stop()

    app = FastAPI(
        title="Powdrr Local Workspace API",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=[parsed.hostname])
    app.state.store = store
    app.state.bootstrap_digest = _digest(bootstrap_secret)
    app.state.expected_origin = expected_origin.rstrip("/")
    app.state.cookie_secure = parsed.scheme == "https"
    app.state.turn_dispatch_runtime = turn_runtime

    async def authorize_request(request: Request, *, mutating: bool) -> None:
        if mutating:
            _require_origin(request, app.state.expected_origin)
        session_cookie = request.cookies.get(_SESSION_COOKIE)
        if not session_cookie:
            raise WorkspaceApiException(
                401, "unauthorized", "Sign in to the local app."
            )
        session_digest = _digest(session_cookie)
        csrf_digest = await run_in_threadpool(
            store.get_api_session_csrf_digest,
            session_digest,
            now=_now().isoformat(),
        )
        if csrf_digest is None:
            raise WorkspaceApiException(401, "session_expired", "Sign in again.")
        if mutating:
            csrf_token = request.headers.get("X-CSRF-Token", "")
            if not csrf_token or not hmac.compare_digest(
                csrf_digest, _digest(csrf_token)
            ):
                raise WorkspaceApiException(
                    403, "csrf_rejected", "Request was rejected."
                )

    app.state.authorize_request = authorize_request

    @app.exception_handler(WorkspaceApiException)
    async def workspace_api_error_handler(
        request: Request, error: WorkspaceApiException
    ) -> JSONResponse:
        del request
        body = WorkspaceApiErrorResponse(
            WorkspaceApiError(
                error.code,
                error.message,
                error.current_revision,
                error.current_event_cursor,
            )
        )
        return JSONResponse(body.to_dict(), status_code=error.status_code)

    @app.exception_handler(WorkspaceConflict)
    async def workspace_conflict_handler(
        request: Request, error: WorkspaceConflict
    ) -> JSONResponse:
        del request, error
        body = WorkspaceApiErrorResponse(
            WorkspaceApiError("conflict", "The request conflicts with saved data.")
        )
        return JSONResponse(body.to_dict(), status_code=409)

    @app.exception_handler(RequestValidationError)
    async def request_validation_handler(
        request: Request, error: RequestValidationError
    ) -> JSONResponse:
        del request, error
        body = WorkspaceApiErrorResponse(
            WorkspaceApiError("invalid_request", "Request data is invalid.")
        )
        return JSONResponse(body.to_dict(), status_code=422)

    @app.exception_handler(KeyError)
    async def missing_resource_handler(
        request: Request, error: KeyError
    ) -> JSONResponse:
        del request, error
        body = WorkspaceApiErrorResponse(
            WorkspaceApiError("not_found", "Requested resource was not found.")
        )
        return JSONResponse(body.to_dict(), status_code=404)

    @app.exception_handler(Exception)
    async def unexpected_error_handler(
        request: Request, error: Exception
    ) -> JSONResponse:
        del request, error
        body = WorkspaceApiErrorResponse(
            WorkspaceApiError(
                "internal_error", "The local service could not complete the request."
            )
        )
        return JSONResponse(body.to_dict(), status_code=500)

    @app.post("/api/v1/session", status_code=201)
    async def bootstrap_session(
        payload: BootstrapRequest, request: Request, response: Response
    ) -> dict[str, Any]:
        _require_origin(request, app.state.expected_origin)
        if not hmac.compare_digest(_digest(payload.token), app.state.bootstrap_digest):
            raise WorkspaceApiException(
                401, "invalid_bootstrap", "Sign in to the local app."
            )

        now = _now()
        session_token = secrets.token_urlsafe(32)
        csrf_token = secrets.token_urlsafe(32)
        consumed = await run_in_threadpool(
            store.consume_api_bootstrap_token,
            app.state.bootstrap_digest,
            session_digest=_digest(session_token),
            csrf_digest=_digest(csrf_token),
            created_at=now.isoformat(),
            expires_at=(now + timedelta(seconds=session_max_age)).isoformat(),
        )
        if not consumed:
            raise WorkspaceApiException(
                401, "bootstrap_used", "Sign in to the local app."
            )
        response.set_cookie(
            _SESSION_COOKIE,
            session_token,
            max_age=session_max_age,
            httponly=True,
            secure=app.state.cookie_secure,
            samesite="strict",
            path="/api/v1",
        )
        return {"csrf_token": csrf_token}

    @app.post("/api/v1/session/refresh")
    async def refresh_session(request: Request) -> dict[str, str]:
        """Issue a fresh in-memory CSRF token for an existing browser session."""
        _require_origin(request, app.state.expected_origin)
        session_token = request.cookies.get(_SESSION_COOKIE)
        if session_token is None:
            raise WorkspaceApiException(
                401, "unauthorized", "Sign in to the local app."
            )
        csrf_token = secrets.token_urlsafe(32)
        rotated = await run_in_threadpool(
            store.rotate_api_session_csrf_digest,
            _digest(session_token),
            csrf_digest=_digest(csrf_token),
            now=_now().isoformat(),
        )
        if not rotated:
            raise WorkspaceApiException(401, "session_expired", "Sign in again.")
        return {"csrf_token": csrf_token}

    @app.post("/api/v1/session/revoke", status_code=204)
    async def revoke_session(request: Request) -> Response:
        await authorize_request(request, mutating=True)
        session_token = request.cookies.get(_SESSION_COOKIE)
        if session_token is not None:
            await run_in_threadpool(store.revoke_api_session, _digest(session_token))
        response = Response(status_code=204)
        response.delete_cookie(_SESSION_COOKIE, path="/api/v1", samesite="strict")
        return response

    install_workspace_routes(
        app,
        store=store,
        repository_registry=repository_source,
        dispatcher=dispatcher,
        event_source=event_source,
    )
    return app


def _require_origin(request: Request, expected_origin: str) -> None:
    origin = request.headers.get("Origin")
    if origin is None or not hmac.compare_digest(origin.rstrip("/"), expected_origin):
        raise WorkspaceApiException(403, "origin_rejected", "Request was rejected.")
