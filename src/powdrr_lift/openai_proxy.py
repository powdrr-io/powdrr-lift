from __future__ import annotations

import base64
import json
import select
import socket
import ssl
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import SplitResult, urlsplit

import httpx

_UPSTREAM_REQUEST_HEADERS = {
    "Host": "chatgpt.com",
    "oai-product-sku": "codex",
    "originator": "codex-tui",
    "user-agent": (
        "codex-tui/0.142.3 (Mac OS 26.5.1; arm64) "
        "Apple_Terminal/470.2 (codex-tui; 0.142.3)"
    ),
}

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

    class _Server(ThreadingHTTPServer):
        def handle_error(self, request: Any, client_address: Any) -> None:
            exc_type, exc_value, _ = sys.exc_info()
            if exc_value is not None and _is_disconnect_error(exc_value):
                return
            super().handle_error(request, client_address)

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
            started_at = datetime.now(UTC)
            parsed_request = urlsplit(self.path)
            received_headers = list(self.headers.items())
            request_body = self._read_request_body()
            print(f"REQUEST: {_preview_text(request_body)}", flush=True)
            print(f"REQUEST HEADERS: {received_headers}", flush=True)
            is_websocket_upgrade = _is_websocket_upgrade_request(self.headers)
            if is_websocket_upgrade:
                print("UPGRADE: websocket", flush=True)
            request_headers = self._forward_request_headers(
                upstream=upstream, preserve_upgrade=is_websocket_upgrade
            )
            auth_source = self._auth_source()
            print(f"AUTH: {auth_source}", flush=True)
            forwarded_path = _rewrite_path(
                parsed_request,
                client_path_prefix=client_prefix,
                upstream_path_prefix=upstream_prefix,
            )
            response_headers: dict[str, str] = {}
            response_body = b""
            response_status = int(HTTPStatus.BAD_GATEWAY)
            response_reason = HTTPStatus.BAD_GATEWAY.phrase
            error_text: str | None = None
            sent_response_headers: dict[str, str] = {}

            try:
                if is_websocket_upgrade:
                    (
                        response_status,
                        response_reason,
                        response_headers,
                        response_body,
                        _upstream_debug,
                        sent_response_headers,
                    ) = self._proxy_websocket_upgrade(
                        upstream=upstream,
                        path=forwarded_path,
                        body=request_body,
                        headers=request_headers,
                    )
                else:
                    (
                        response_status,
                        response_reason,
                        raw_response_headers,
                        response_body,
                        _upstream_debug,
                        sent_response_headers,
                    ) = self._proxy_http_request(
                        upstream=upstream,
                        path=forwarded_path,
                        body=request_body,
                        headers=request_headers,
                    )

                    response_headers = _sanitize_response_headers(
                        list(raw_response_headers.items())
                    )
                    self._send_client_response(
                        status=response_status,
                        reason=response_reason,
                        headers=response_headers,
                        body=response_body,
                    )
                    sent_response_headers = response_headers
            except Exception as exc:  # noqa: BLE001
                error_text = str(exc)
                response_body = self._send_proxy_error(error_text)
                sent_response_headers = {
                    "Content-Type": "application/json; charset=utf-8",
                    "Content-Length": str(len(response_body)),
                }

            print(
                f"RESPONSE {response_status} {response_reason}: "
                f"{_preview_text(response_body)}",
                flush=True,
            )
            self._write_exchange_dumps(
                log_dir=log_dir,
                started_at=started_at,
                request_line=self.requestline,
                method=self.command,
                client_path=parsed_request.path,
                forwarded_path=forwarded_path,
                query=parsed_request.query,
                received_headers=received_headers,
                request_headers=request_headers,
                request_body=request_body,
                response_status=int(response_status),
                response_reason=response_reason,
                response_headers=response_headers,
                response_body=response_body,
                sent_response_headers=sent_response_headers,
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

        def _forward_request_headers(
            self,
            *,
            upstream: SplitResult,
            preserve_upgrade: bool = False,
        ) -> dict[str, str]:
            headers: dict[str, str] = {}
            for key, value in self.headers.items():
                normalized_key = key.lower()
                if not preserve_upgrade and normalized_key in _HOP_BY_HOP_HEADERS:
                    continue
                if normalized_key == "accept-encoding":
                    continue
                headers[key] = value
            headers.update(_UPSTREAM_REQUEST_HEADERS)
            return headers

        def _auth_source(self) -> str:
            client_authorization = self.headers.get("Authorization")
            client_has_auth = bool(
                client_authorization and client_authorization.strip()
            )
            if client_has_auth:
                return "client"
            return "missing"

        def _proxy_websocket_upgrade(
            self,
            *,
            upstream: SplitResult,
            path: str,
            body: bytes,
            headers: dict[str, str],
        ) -> tuple[int, str, dict[str, str], bytes, dict[str, Any], dict[str, str]]:
            upstream_socket = _open_upstream_socket(upstream)
            upstream_stream = upstream_socket.makefile("rb")
            try:
                raw_request = _format_raw_http_request(
                    method=self.command,
                    path=path,
                    headers=headers,
                    body=body,
                    upstream=upstream,
                )
                _send_raw_http_request(
                    upstream_socket=upstream_socket,
                    method=self.command,
                    path=path,
                    headers=headers,
                    body=body,
                    upstream=upstream,
                )
                (
                    response_status,
                    response_reason,
                    response_headers,
                    response_body,
                ) = _read_raw_http_response(upstream_stream)
                sent_response_headers = dict(response_headers)
                raw_response = _format_raw_http_response(
                    status=response_status,
                    reason=response_reason,
                    headers=response_headers,
                    body=response_body,
                )

                self.send_response(response_status, response_reason)
                for header_name, header_value in response_headers.items():
                    self.send_header(header_name, header_value)
                self.end_headers()
                if response_body:
                    self.wfile.write(response_body)
                    self.wfile.flush()

                if response_status == HTTPStatus.SWITCHING_PROTOCOLS:
                    _relay_socket_streams(
                        client_socket=self.connection,
                        upstream_socket=upstream_socket,
                    )

                return (
                    response_status,
                    response_reason,
                    response_headers,
                    response_body,
                    {
                        "transport": "websocket",
                        "request": {"raw": raw_request},
                        "response": {"raw": raw_response},
                    },
                    sent_response_headers,
                )
            finally:
                upstream_stream.close()
                upstream_socket.close()

        def _proxy_http_request(
            self,
            *,
            upstream: SplitResult,
            path: str,
            body: bytes,
            headers: dict[str, str],
        ) -> tuple[int, str, dict[str, str], bytes, dict[str, Any], dict[str, str]]:
            raw_request = _format_raw_http_request(
                method=self.command,
                path=path,
                headers=headers,
                body=body,
                upstream=upstream,
            )
            url = f"{upstream.scheme}://{_upstream_host_header(upstream)}{path}"
            transport = httpx.HTTPTransport(http2=False, retries=0)
            with httpx.Client(transport=transport, timeout=120.0) as client:
                request = client.build_request(
                    method=self.command,
                    url=url,
                    headers=list(headers.items()),
                    content=body if body else None,
                )
                response = client.send(request, stream=True)
                try:
                    response_body = response.read()
                    response_headers = dict(response.headers.items())
                    response_status = int(response.status_code)
                    response_reason = response.reason_phrase
                    return (
                        response_status,
                        response_reason,
                        response_headers,
                        response_body,
                        {
                            "transport": "http",
                            "http_version": response.http_version,
                            "request": {
                                "raw": raw_request,
                                "http_version": request.extensions.get(
                                    "http_version",
                                    "",
                                ),
                            },
                            "response": {
                                "raw": _format_raw_http_response(
                                    status=response_status,
                                    reason=response_reason,
                                    headers=response_headers,
                                    body=response_body,
                                ),
                                "http_version": response.http_version,
                            },
                        },
                        dict(response_headers),
                    )
                finally:
                    response.close()

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

        def _send_client_response(
            self,
            *,
            status: int,
            reason: str,
            headers: dict[str, str],
            body: bytes,
        ) -> None:
            is_bodyless = self.command == "HEAD" or status in _BODYLESS_STATUS_CODES
            content_length = headers.get("Content-Length")

            self.send_response(status, reason)
            for header_name, header_value in headers.items():
                self.send_header(header_name, header_value)

            if is_bodyless:
                if content_length is not None:
                    self.send_header("Content-Length", content_length)
                self.end_headers()
                return

            if content_length is not None:
                self.send_header("Content-Length", content_length)
                self.end_headers()
                if body:
                    self.wfile.write(body)
                    self.wfile.flush()
                return

            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            if body:
                self._write_chunk(body)
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()

        def _write_exchange_dumps(
            self,
            *,
            log_dir: Path,
            started_at: datetime,
            request_line: str,
            method: str,
            client_path: str,
            forwarded_path: str,
            query: str,
            received_headers: list[tuple[str, str]],
            request_headers: dict[str, str],
            request_body: bytes,
            response_status: int,
            response_reason: str,
            response_headers: dict[str, str],
            response_body: bytes,
            sent_response_headers: dict[str, str],
        ) -> None:
            dump_prefix = _exchange_dump_prefix(started_at)
            request_in = {
                "raw": _format_raw_http_message(
                    start_line=request_line,
                    headers=received_headers,
                    body=request_body,
                ),
            }
            request_out = {
                "raw": _format_raw_http_message(
                    start_line=f"{method} {forwarded_path} HTTP/1.1",
                    headers=list(request_headers.items()),
                    body=request_body,
                ),
            }
            response_in = {
                "raw": _format_raw_http_message(
                    start_line=f"HTTP/1.1 {response_status} {response_reason}".rstrip(),
                    headers=list(response_headers.items()),
                    body=response_body,
                ),
            }
            response_out = {
                "raw": _format_raw_http_message(
                    start_line=f"HTTP/1.1 {response_status} {response_reason}".rstrip(),
                    headers=list(sent_response_headers.items()),
                    body=response_body,
                ),
            }
            _write_json_record(log_dir / f"{dump_prefix}-request-in.json", request_in)
            _write_json_record(log_dir / f"{dump_prefix}-request-out.json", request_out)
            _write_json_record(log_dir / f"{dump_prefix}-response-in.json", response_in)
            _write_json_record(
                log_dir / f"{dump_prefix}-response-out.json",
                response_out,
            )

    server = _Server((config.host, config.port), _Handler)
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
        "Set OPENAI_BASE_URL to the proxy base URL for your client, "
        f"for example http://{config.host}:{config.port}{config.client_path_prefix}"
    )
    server.serve_forever()


def default_openai_proxy_log_dir(log_root: str | Path = ".") -> Path:
    return Path(log_root).expanduser().resolve() / ".powdrr" / "openai-proxy"


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


def _exchange_dump_prefix(started_at: datetime) -> str:
    return started_at.strftime("%Y%m%dT%H%M%S.%fZ")


def _preview_text(body: bytes) -> str:
    return body.decode("utf-8", errors="replace")[:50]


def _write_json_record(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _format_raw_http_message(
    *,
    start_line: str,
    headers: list[tuple[str, str]],
    body: bytes,
) -> str:
    message_lines = [start_line]
    for header_name, header_value in headers:
        message_lines.append(f"{header_name}: {header_value}")
    message_text = "\r\n".join(message_lines) + "\r\n\r\n"
    if body:
        message_text += body.decode("utf-8", errors="replace")
    return message_text


def _format_raw_http_request(
    *,
    method: str,
    path: str,
    headers: dict[str, str],
    body: bytes,
    upstream: SplitResult,
) -> str:
    request_lines = [f"{method} {path} HTTP/1.1"]
    request_headers = list(headers.items())
    if body and not any(
        header_name.lower() == "content-length" for header_name, _ in request_headers
    ):
        request_headers.append(("Content-Length", str(len(body))))
    for header_name, header_value in request_headers:
        request_lines.append(f"{header_name}: {header_value}")
    request_text = "\r\n".join(request_lines) + "\r\n\r\n"
    if body:
        request_text += body.decode("utf-8", errors="replace")
    return request_text


def _format_raw_http_response(
    *,
    status: int,
    reason: str,
    headers: dict[str, str],
    body: bytes = b"",
) -> str:
    response_lines = [f"HTTP/1.1 {status} {reason}".rstrip()]
    for header_name, header_value in headers.items():
        response_lines.append(f"{header_name}: {header_value}")
    response_text = "\r\n".join(response_lines) + "\r\n\r\n"
    if body:
        response_text += body.decode("utf-8", errors="replace")
    return response_text


def _is_websocket_upgrade_request(headers: Any) -> bool:
    upgrade_header = (headers.get("Upgrade") or "").strip().lower()
    connection_header = (headers.get("Connection") or "").strip().lower()
    return (
        upgrade_header == "websocket"
        or "upgrade" in connection_header
        or bool(headers.get("Sec-WebSocket-Key"))
    )


def _open_upstream_socket(upstream: SplitResult) -> socket.socket:
    if upstream.hostname is None:
        raise ValueError("upstream_base_url must include a host name.")

    port = upstream.port or (443 if upstream.scheme == "https" else 80)
    raw_socket = socket.create_connection((upstream.hostname, port), timeout=120)
    if upstream.scheme == "https":
        context = ssl.create_default_context()
        return context.wrap_socket(raw_socket, server_hostname=upstream.hostname)
    return raw_socket


def _send_raw_http_request(
    *,
    upstream_socket: socket.socket,
    method: str,
    path: str,
    headers: dict[str, str],
    body: bytes,
    upstream: SplitResult,
) -> None:
    header_lines = [f"{method} {path} HTTP/1.1"]
    for header_name, header_value in headers.items():
        header_lines.append(f"{header_name}: {header_value}")
    if body and not any(
        header_name.lower() == "content-length" for header_name in headers
    ):
        header_lines.append(f"Content-Length: {len(body)}")
    request_bytes = ("\r\n".join(header_lines) + "\r\n\r\n").encode("utf-8")
    if body:
        request_bytes += body
    upstream_socket.sendall(request_bytes)


def _read_raw_http_response(
    stream: Any,
) -> tuple[int, str, dict[str, str], bytes]:
    status_line = stream.readline(65537)
    if not status_line:
        raise ConnectionError("Upstream closed the websocket handshake early.")

    status_parts = status_line.decode("iso-8859-1").rstrip("\r\n").split(" ", 2)
    if len(status_parts) < 2:
        raise ValueError(f"Invalid upstream response status line: {status_line!r}")
    response_reason = status_parts[2] if len(status_parts) > 2 else ""
    response_status = int(status_parts[1])

    response_headers: dict[str, str] = {}
    while True:
        header_line = stream.readline(65537)
        if not header_line or header_line in {b"\r\n", b"\n"}:
            break
        decoded_line = header_line.decode("iso-8859-1").rstrip("\r\n")
        header_name, header_value = decoded_line.split(":", 1)
        response_headers[header_name.strip()] = header_value.strip()

    response_body = b""
    transfer_encoding = response_headers.get("Transfer-Encoding", "").lower()
    if response_status != HTTPStatus.SWITCHING_PROTOCOLS:
        if transfer_encoding == "chunked":
            response_body = _read_chunked_body(stream)
        else:
            content_length = response_headers.get("Content-Length")
            if content_length is not None:
                response_body = stream.read(int(content_length))
    return response_status, response_reason, response_headers, response_body


def _read_chunked_body(stream: Any) -> bytes:
    body = bytearray()
    while True:
        chunk_size_line = stream.readline(65537)
        if not chunk_size_line:
            break
        chunk_size = int(chunk_size_line.split(b";", 1)[0].strip(), 16)
        if chunk_size == 0:
            while True:
                trailer_line = stream.readline(65537)
                if not trailer_line or trailer_line in {b"\r\n", b"\n"}:
                    return bytes(body)
            break
        body.extend(stream.read(chunk_size))
        stream.read(2)
    return bytes(body)


def _relay_socket_streams(
    *,
    client_socket: socket.socket,
    upstream_socket: socket.socket,
) -> None:
    client_socket.setblocking(False)
    upstream_socket.setblocking(False)
    sockets = [client_socket, upstream_socket]
    try:
        while True:
            readable, _, exceptional = select.select(sockets, [], sockets, 0.5)
            if exceptional:
                return
            for readable_socket in readable:
                try:
                    chunk = readable_socket.recv(65536)
                except BlockingIOError:
                    continue
                if not chunk:
                    return
                if readable_socket is client_socket:
                    upstream_socket.sendall(chunk)
                else:
                    client_socket.sendall(chunk)
    finally:
        try:
            client_socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            upstream_socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass


def _upstream_host_header(upstream: SplitResult) -> str:
    if upstream.hostname is None:
        raise ValueError("upstream_base_url must include a host name.")
    default_port = 443 if upstream.scheme == "https" else 80
    if upstream.port is None or upstream.port == default_port:
        return upstream.hostname
    return f"{upstream.hostname}:{upstream.port}"


def _is_disconnect_error(exc: BaseException) -> bool:
    return isinstance(
        exc,
        (BrokenPipeError, ConnectionAbortedError, ConnectionResetError),
    )
