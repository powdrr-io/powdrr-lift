from __future__ import annotations

import base64
import json
import uuid
from collections.abc import AsyncGenerator, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import SplitResult, urlsplit

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse
from starlette.routing import Route

from powdrr_lift.change_log_template import _resolve_repo_root

_HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}

_BODYLESS_STATUS_CODES = {204, 304}
_PROXY_METHODS = ["DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT"]


@dataclass(frozen=True, slots=True)
class OpenAIProxyConfig:
    upstream_base_url: str
    log_dir: Path
    host: str = "127.0.0.1"
    port: int = 8787
    client_path_prefix: str = "/v1"
    upstream_path_prefix: str = "/v1"


def build_server(config: OpenAIProxyConfig) -> Starlette:
    upstream = _parse_upstream_base_url(config.upstream_base_url)
    log_dir = config.log_dir.expanduser().resolve()
    log_dir.mkdir(parents=True, exist_ok=True)
    client_prefix = _normalize_path_prefix(config.client_path_prefix)
    upstream_prefix = _normalize_path_prefix(config.upstream_path_prefix)

    async def _proxy_request(request: Request) -> Response:
        request_id = uuid.uuid4().hex
        started_at = datetime.now(UTC)
        parsed_request = urlsplit(str(request.url))
        request_body = await request.body()
        request_headers = _forward_request_headers(request.headers.items())
        recorded_request_headers = _redact_request_headers(request_headers)
        forwarded_path = _rewrite_path(
            parsed_request,
            client_path_prefix=client_prefix,
            upstream_path_prefix=upstream_prefix,
        )

        response_headers: dict[str, str] = {}
        response_body = b""
        response_status = 502
        response_reason = "Bad Gateway"
        error_text: str | None = None
        upstream_response: httpx.Response | None = None
        client: httpx.AsyncClient | None = None

        async def _close_upstream() -> None:
            if upstream_response is not None:
                await upstream_response.aclose()
            if client is not None:
                await client.aclose()

        def _write_record() -> None:
            _write_exchange_record(
                log_dir=log_dir,
                request_id=request_id,
                started_at=started_at,
                method=request.method,
                client_path=parsed_request.path,
                forwarded_path=forwarded_path,
                query=parsed_request.query,
                request_headers=recorded_request_headers,
                request_body=request_body,
                response_status=response_status,
                response_reason=response_reason,
                response_headers=response_headers,
                response_body=response_body,
                error_text=error_text,
            )

        try:
            client = httpx.AsyncClient(timeout=120.0)
            upstream_request = client.build_request(
                method=request.method,
                url=_build_upstream_url(upstream, forwarded_path),
                headers=request_headers,
                content=request_body if request_body else None,
            )
            upstream_response = await client.send(upstream_request, stream=True)
            response_status = upstream_response.status_code
            response_reason = upstream_response.reason_phrase
            response_headers = _sanitize_response_headers(
                list(upstream_response.headers.items())
            )

            if request.method == "HEAD" or response_status in _BODYLESS_STATUS_CODES:
                response_body = await upstream_response.aread()
                await _close_upstream()
                _write_record()
                return Response(
                    content=b"",
                    headers=response_headers,
                    status_code=response_status,
                )

            if upstream_response.headers.get("Content-Length") is not None:
                response_body = await upstream_response.aread()
                await _close_upstream()
                _write_record()
                return Response(
                    content=response_body,
                    headers=response_headers,
                    status_code=response_status,
                )

            collected = bytearray()

            async def _stream_body() -> AsyncGenerator[bytes, None]:
                nonlocal response_body
                try:
                    assert upstream_response is not None
                    async for chunk in upstream_response.aiter_bytes():
                        collected.extend(chunk)
                        yield chunk
                finally:
                    response_body = bytes(collected)
                    await _close_upstream()
                    _write_record()

            return StreamingResponse(
                _stream_body(),
                headers=response_headers,
                status_code=response_status,
            )
        except Exception as exc:  # noqa: BLE001
            error_text = str(exc)
            response_body = _proxy_error_body(error_text)
            await _close_upstream()
            _write_record()
            return Response(
                content=response_body,
                headers={"Content-Type": "application/json; charset=utf-8"},
                status_code=502,
            )

    app = Starlette(
        routes=[
            Route("/{path:path}", _proxy_request, methods=_PROXY_METHODS),
        ]
    )
    app.state.upstream_base_url = config.upstream_base_url
    app.state.log_dir = log_dir
    app.state.client_path_prefix = client_prefix
    app.state.upstream_path_prefix = upstream_prefix
    return app


def serve(config: OpenAIProxyConfig) -> None:
    app = build_server(config)
    print(f"Serving OpenAI proxy at http://{config.host}:{config.port}")
    print(f"Forwarding to {config.upstream_base_url}")
    print(f"Recording exchanges in {config.log_dir}")
    print(
        "Set Codex openai_base_url to the proxy base URL, for example "
        f'codex -c openai_base_url="http://{config.host}:{config.port}'
        f'{config.client_path_prefix}"'
    )
    uvicorn.run(
        app,
        host=config.host,
        port=config.port,
        log_level="warning",
        access_log=False,
        lifespan="off",
    )


def default_openai_proxy_log_dir(repo_root: str | Path | None = None) -> Path:
    resolved_repo_root = _resolve_repo_root(repo_root)
    return resolved_repo_root / ".powdrr" / "openai-proxy"


def _parse_upstream_base_url(raw_base_url: str) -> SplitResult:
    parsed = urlsplit(raw_base_url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("upstream_base_url must start with http:// or https://")
    if parsed.netloc == "":
        raise ValueError("upstream_base_url must include a host name.")
    return parsed


def _build_upstream_url(upstream: SplitResult, path: str) -> str:
    return f"{upstream.scheme}://{upstream.netloc}{path}"


def _normalize_path_prefix(path_prefix: str) -> str:
    stripped = path_prefix.strip()
    if not stripped or stripped == "/":
        return ""
    if not stripped.startswith("/"):
        stripped = f"/{stripped}"
    return stripped.rstrip("/")


def _rewrite_path(
    parsed_request: SplitResult,
    *,
    client_path_prefix: str,
    upstream_path_prefix: str,
) -> str:
    request_path = parsed_request.path or "/"
    stripped_path = _strip_prefix(request_path, client_path_prefix)
    rewritten_path = _join_paths(upstream_path_prefix, stripped_path)
    if parsed_request.query:
        return f"{rewritten_path}?{parsed_request.query}"
    return rewritten_path


def _strip_prefix(path: str, prefix: str) -> str:
    if not prefix:
        return path
    if path == prefix:
        return "/"
    if path.startswith(f"{prefix}/"):
        return path[len(prefix) :]
    return path


def _join_paths(prefix: str, suffix: str) -> str:
    if not prefix:
        return suffix or "/"
    if not suffix or suffix == "/":
        return prefix
    return f"{prefix.rstrip('/')}/{suffix.lstrip('/')}"


def _forward_request_headers(
    headers: Iterable[tuple[str, str]],
) -> dict[str, str]:
    forwarded_headers: dict[str, str] = {}
    for header_name, header_value in headers:
        normalized_name = header_name.lower()
        if normalized_name in _HOP_BY_HOP_HEADERS:
            continue
        if normalized_name in {"content-length", "host"}:
            continue
        forwarded_headers[header_name] = header_value
    return forwarded_headers


def _sanitize_response_headers(
    headers: Iterable[tuple[str, str]],
) -> dict[str, str]:
    response_headers: dict[str, str] = {}
    for header_name, header_value in headers:
        normalized_name = header_name.lower()
        if normalized_name in _HOP_BY_HOP_HEADERS:
            continue
        if normalized_name == "content-length":
            continue
        response_headers[header_name] = header_value
    return response_headers


def _body_to_record(body: bytes) -> dict[str, str]:
    if not body:
        return {"encoding": "utf-8", "text": ""}

    try:
        return {"encoding": "utf-8", "text": body.decode("utf-8")}
    except UnicodeDecodeError:
        return {
            "encoding": "base64",
            "data": base64.b64encode(body).decode("ascii"),
        }


def _proxy_error_body(error_text: str) -> bytes:
    payload = {
        "error": {
            "message": error_text,
            "type": "proxy_error",
        }
    }
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def _write_exchange_record(
    *,
    log_dir: Path,
    request_id: str,
    started_at: datetime,
    method: str,
    client_path: str,
    forwarded_path: str,
    query: str,
    request_headers: dict[str, str],
    request_body: bytes,
    response_status: int,
    response_reason: str,
    response_headers: dict[str, str],
    response_body: bytes,
    error_text: str | None,
) -> None:
    record_path = _exchange_record_path(log_dir, started_at, request_id)
    record_path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "id": request_id,
        "timestamp": started_at.isoformat(),
        "request": {
            "method": method,
            "client_path": client_path,
            "forwarded_path": forwarded_path,
            "query": query,
            "headers": request_headers,
            "body": _body_to_record(request_body),
        },
        "response": {
            "status": response_status,
            "reason": response_reason,
            "headers": response_headers,
            "body": _body_to_record(response_body),
        },
    }
    if error_text is not None:
        record["error"] = error_text
    record_path.write_text(
        json.dumps(record, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _redact_request_headers(headers: dict[str, str]) -> dict[str, str]:
    redacted_headers = dict(headers)
    for header_name in tuple(redacted_headers):
        if header_name.lower() in {"authorization", "proxy-authorization"}:
            redacted_headers[header_name] = "[redacted]"
    return redacted_headers


def _exchange_record_path(log_dir: Path, started_at: datetime, request_id: str) -> Path:
    return (
        log_dir
        / f"{started_at:%Y}"
        / f"{started_at:%m}"
        / f"{started_at:%d}"
        / f"{started_at:%H%M%S}-{request_id}.json"
    )
