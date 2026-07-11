from __future__ import annotations

import base64
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import SplitResult, urlsplit

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

_BODYLESS_STATUS_CODES = {HTTPStatus.NO_CONTENT, HTTPStatus.NOT_MODIFIED}


@dataclass(frozen=True, slots=True)
class OpenAIProxyConfig:
    upstream_base_url: str
    log_dir: Path
    host: str = "127.0.0.1"
    port: int = 8787
    client_path_prefix: str = "/v1"
    upstream_path_prefix: str = "/v1"


def build_server(config: OpenAIProxyConfig) -> ThreadingHTTPServer:
    upstream = _parse_upstream_base_url(config.upstream_base_url)
    log_dir = config.log_dir.expanduser().resolve()
    log_dir.mkdir(parents=True, exist_ok=True)
    client_prefix = _normalize_path_prefix(config.client_path_prefix)
    upstream_prefix = _normalize_path_prefix(config.upstream_path_prefix)

    class _Handler(BaseHTTPRequestHandler):
        server_version = "powdrr-lift-openai-proxy/1.0"
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:  # noqa: N802
            self._proxy_request()

        def do_POST(self) -> None:  # noqa: N802
            self._proxy_request()

        def do_PUT(self) -> None:  # noqa: N802
            self._proxy_request()

        def do_PATCH(self) -> None:  # noqa: N802
            self._proxy_request()

        def do_DELETE(self) -> None:  # noqa: N802
            self._proxy_request()

        def do_HEAD(self) -> None:  # noqa: N802
            self._proxy_request()

        def do_OPTIONS(self) -> None:  # noqa: N802
            self._proxy_request()

        def log_message(self, format: str, *args: object) -> None:  # noqa: A003
            return

        def _proxy_request(self) -> None:
            request_id = uuid.uuid4().hex
            started_at = datetime.now(UTC)
            parsed_request = urlsplit(self.path)
            request_body = self._read_request_body()
            request_headers = self._forward_request_headers()
            recorded_request_headers = _redact_request_headers(request_headers)
            forwarded_path = _rewrite_path(
                parsed_request,
                client_path_prefix=client_prefix,
                upstream_path_prefix=upstream_prefix,
            )
            response_headers: dict[str, str] = {}
            response_body = b""
            response_status = HTTPStatus.BAD_GATEWAY
            response_reason = HTTPStatus.BAD_GATEWAY.phrase
            error_text: str | None = None
            upstream_connection: Any | None = None
            upstream_response: Any | None = None

            try:
                upstream_connection, upstream_response = _send_upstream_request(
                    upstream=upstream,
                    method=self.command,
                    path=forwarded_path,
                    body=request_body,
                    headers=request_headers,
                )
                response_status = upstream_response.status
                response_reason = upstream_response.reason or ""
                response_headers = _sanitize_response_headers(
                    upstream_response.getheaders()
                )
                response_body = self._relay_upstream_response(upstream_response)
            except Exception as exc:  # noqa: BLE001
                error_text = str(exc)
                response_body = self._send_proxy_error(error_text)
            finally:
                if upstream_response is not None:
                    upstream_response.close()
                if upstream_connection is not None:
                    upstream_connection.close()

            self._write_exchange_record(
                log_dir=log_dir,
                request_id=request_id,
                started_at=started_at,
                method=self.command,
                client_path=parsed_request.path,
                forwarded_path=forwarded_path,
                query=parsed_request.query,
                request_headers=recorded_request_headers,
                request_body=request_body,
                response_status=int(response_status),
                response_reason=response_reason,
                response_headers=response_headers,
                response_body=response_body,
                error_text=error_text,
            )

        def _read_request_body(self) -> bytes:
            content_length = self.headers.get("Content-Length")
            if content_length is None:
                return b""

            try:
                expected_length = int(content_length)
            except ValueError:
                return b""

            if expected_length <= 0:
                return b""

            return self.rfile.read(expected_length)

        def _forward_request_headers(self) -> dict[str, str]:
            headers: dict[str, str] = {}
            for key, value in self.headers.items():
                normalized_key = key.lower()
                if normalized_key in _HOP_BY_HOP_HEADERS:
                    continue
                if normalized_key in {"content-length", "host", "accept-encoding"}:
                    continue
                headers[key] = value
            return headers

        def _relay_upstream_response(self, upstream_response: Any) -> bytes:
            is_bodyless = (
                self.command == "HEAD"
                or upstream_response.status in _BODYLESS_STATUS_CODES
            )
            body = bytearray()
            content_length = upstream_response.getheader("Content-Length")

            self.send_response(upstream_response.status, upstream_response.reason)
            for header_name, header_value in _sanitize_response_headers(
                upstream_response.getheaders()
            ).items():
                self.send_header(header_name, header_value)

            if is_bodyless:
                if content_length is not None:
                    self.send_header("Content-Length", content_length)
                self.end_headers()
                return b""

            if content_length is not None:
                self.send_header("Content-Length", content_length)
                self.end_headers()
                while True:
                    chunk = upstream_response.read(65536)
                    if not chunk:
                        break
                    body.extend(chunk)
                    self.wfile.write(chunk)
                self.wfile.flush()
                return bytes(body)

            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            while True:
                chunk = upstream_response.read(65536)
                if not chunk:
                    break
                body.extend(chunk)
                self._write_chunk(chunk)
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
            return bytes(body)

        def _write_chunk(self, chunk: bytes) -> None:
            self.wfile.write(f"{len(chunk):X}\r\n".encode("ascii"))
            self.wfile.write(chunk)
            self.wfile.write(b"\r\n")

        def _send_proxy_error(self, error_text: str) -> bytes:
            payload = {
                "error": {
                    "message": error_text,
                    "type": "proxy_error",
                }
            }
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(HTTPStatus.BAD_GATEWAY)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()
            return body

        def _write_exchange_record(
            self,
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

    server = ThreadingHTTPServer((config.host, config.port), _Handler)
    server.upstream_base_url = config.upstream_base_url  # type: ignore[attr-defined]
    server.log_dir = log_dir  # type: ignore[attr-defined]
    server.client_path_prefix = client_prefix  # type: ignore[attr-defined]
    server.upstream_path_prefix = upstream_prefix  # type: ignore[attr-defined]
    return server


def serve(config: OpenAIProxyConfig) -> None:
    server = build_server(config)
    print(f"Serving OpenAI proxy at http://{config.host}:{config.port}")
    print(f"Forwarding to {config.upstream_base_url}")
    print(f"Recording exchanges in {config.log_dir}")
    print(
        "Set Codex openai_base_url to the proxy base URL, for example "
        f'codex -c openai_base_url="http://{config.host}:{config.port}'
        f'{config.client_path_prefix}"'
    )
    server.serve_forever()


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


def _send_upstream_request(
    *,
    upstream: SplitResult,
    method: str,
    path: str,
    body: bytes,
    headers: dict[str, str],
) -> tuple[Any, Any]:
    import http.client

    if upstream.hostname is None:
        raise ValueError("upstream_base_url must include a host name.")

    connection_cls = (
        http.client.HTTPSConnection
        if upstream.scheme == "https"
        else http.client.HTTPConnection
    )
    connection = connection_cls(
        upstream.hostname,
        upstream.port,
        timeout=120,
    )
    try:
        connection.request(
            method,
            path,
            body=body if body else None,
            headers=headers,
        )
        return connection, connection.getresponse()
    except Exception:
        connection.close()
        raise


def _sanitize_response_headers(headers: list[tuple[str, str]]) -> dict[str, str]:
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


def _redact_request_headers(headers: dict[str, str]) -> dict[str, str]:
    redacted_headers = dict(headers)
    for header_name in ("Authorization", "Proxy-Authorization"):
        if header_name in redacted_headers:
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