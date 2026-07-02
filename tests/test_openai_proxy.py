from __future__ import annotations

import http.client
import json
import socket
import socketserver
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast

import pytest

from powdrr_lift.cli import main
from powdrr_lift.openai_proxy import (
    OpenAIProxyConfig,
    _is_disconnect_error,
    build_server,
)


def test_openai_proxy_forwards_and_records_exchange(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    upstream_captured: dict[str, object] = {}

    class _UpstreamHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)
            upstream_captured["method"] = self.command
            upstream_captured["path"] = self.path
            upstream_captured["headers"] = dict(self.headers.items())
            upstream_captured["body"] = body

            response_body = json.dumps(
                {
                    "id": "resp-1",
                    "object": "response",
                    "output_text": "hello from upstream",
                }
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(response_body)))
            self.end_headers()
            self.wfile.write(response_body)

        def log_message(self, format: str, *args: object) -> None:  # noqa: A003
            return

    upstream_server = ThreadingHTTPServer(("127.0.0.1", 0), _UpstreamHandler)
    upstream_thread = _start_server(upstream_server)
    try:
        proxy_server = build_server(
            OpenAIProxyConfig(
                upstream_base_url=f"http://127.0.0.1:{upstream_server.server_address[1]}",
                log_dir=tmp_path / "records",
                host="127.0.0.1",
                port=0,
            )
        )
        proxy_thread = _start_server(proxy_server)
        try:
            proxy_port = proxy_server.server_address[1]
            connection = http.client.HTTPConnection("127.0.0.1", proxy_port, timeout=10)
            request_body = json.dumps(
                {
                    "model": "gpt-4.1",
                    "input": [{"role": "user", "content": "hello"}],
                }
            ).encode("utf-8")
            connection.request(
                "POST",
                "/v1/responses?debug=true",
                body=request_body,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": "Bearer test-token",
                },
            )
            response = connection.getresponse()
            response_body = response.read()
            connection.close()

            assert response.status == 200
            assert json.loads(response_body) == {
                "id": "resp-1",
                "object": "response",
                "output_text": "hello from upstream",
            }

            assert upstream_captured["method"] == "POST"
            assert upstream_captured["path"] == "/v1/responses?debug=true"
            assert upstream_captured["body"] == request_body
            upstream_headers = cast(dict[str, str], upstream_captured["headers"])
            upstream_headers_lower = {
                key.lower(): value for key, value in upstream_headers.items()
            }
            assert upstream_headers_lower["authorization"] == "Bearer test-token"
            assert upstream_headers_lower["host"] == "chatgpt.com"
            assert upstream_headers_lower["oai-product-sku"] == "codex"
            assert upstream_headers_lower["originator"] == "codex-tui"
            assert (
                upstream_headers_lower["user-agent"]
                == "codex-tui/0.142.3 (Mac OS 26.5.1; arm64) "
                "Apple_Terminal/470.2 (codex-tui; 0.142.3)"
            )

            records = _wait_for_exchange_dump_paths(tmp_path / "records")
            request_in = json.loads(records["request-in"].read_text(encoding="utf-8"))
            request_out = json.loads(records["request-out"].read_text(encoding="utf-8"))
            response_in = json.loads(records["response-in"].read_text(encoding="utf-8"))
            response_out = json.loads(
                records["response-out"].read_text(encoding="utf-8")
            )

            assert set(request_in) == {"raw"}
            assert set(request_out) == {"raw"}
            assert set(response_in) == {"raw"}
            assert set(response_out) == {"raw"}
            assert request_in["raw"].startswith(
                "POST /v1/responses?debug=true HTTP/1.1"
            )
            assert "Authorization: Bearer test-token" in request_in["raw"]
            assert request_body.decode("utf-8") in request_in["raw"]
            assert request_out["raw"].startswith(
                "POST /v1/responses?debug=true HTTP/1.1"
            )
            assert "Authorization: Bearer test-token" in request_out["raw"]
            assert request_body.decode("utf-8") in request_out["raw"]
            assert response_in["raw"].startswith("HTTP/1.1 200 OK")
            assert response_body.decode("utf-8") in response_in["raw"]
            assert response_out["raw"].startswith("HTTP/1.1 200 OK")
            assert response_body.decode("utf-8") in response_out["raw"]

            captured_output = capsys.readouterr().out
            assert 'REQUEST: {"model": "gpt-4.1"' in captured_output
            assert "AUTH: client" in captured_output
            assert 'RESPONSE 200 OK: {"id": "resp-1"' in captured_output
        finally:
            proxy_server.shutdown()
            proxy_server.server_close()
            proxy_thread.join(timeout=2)
    finally:
        upstream_server.shutdown()
        upstream_server.server_close()
        upstream_thread.join(timeout=2)


def test_openai_proxy_preserves_chunked_streaming(tmp_path: Path) -> None:
    class _UpstreamHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            self.rfile.read(length)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for chunk in (b"data: first\n\n", b"data: second\n\n"):
                self.wfile.write(f"{len(chunk):X}\r\n".encode("ascii"))
                self.wfile.write(chunk)
                self.wfile.write(b"\r\n")
                self.wfile.flush()
                time.sleep(0.02)
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()

        def log_message(self, format: str, *args: object) -> None:  # noqa: A003
            return

    upstream_server = ThreadingHTTPServer(("127.0.0.1", 0), _UpstreamHandler)
    upstream_thread = _start_server(upstream_server)
    try:
        proxy_server = build_server(
            OpenAIProxyConfig(
                upstream_base_url=f"http://127.0.0.1:{upstream_server.server_address[1]}",
                log_dir=tmp_path / "records",
                host="127.0.0.1",
                port=0,
            )
        )
        proxy_thread = _start_server(proxy_server)
        try:
            proxy_port = proxy_server.server_address[1]
            connection = http.client.HTTPConnection("127.0.0.1", proxy_port, timeout=10)
            connection.request(
                "POST",
                "/v1/responses",
                body=b"{}",
                headers={"Content-Type": "application/json"},
            )
            response = connection.getresponse()
            response_body = response.read()
            connection.close()

            assert response.status == 200
            assert response.getheader("Transfer-Encoding") == "chunked"
            assert response_body == b"data: first\n\ndata: second\n\n"

            records = _wait_for_exchange_dump_paths(tmp_path / "records")
            response_in = json.loads(records["response-in"].read_text(encoding="utf-8"))
            response_out = json.loads(
                records["response-out"].read_text(encoding="utf-8")
            )
            assert set(response_in) == {"raw"}
            assert set(response_out) == {"raw"}
            assert response_in["raw"].startswith("HTTP/1.1 200 OK")
            assert "data: first\n\ndata: second\n\n" in response_in["raw"]
            assert response_out["raw"].startswith("HTTP/1.1 200 OK")
            assert "data: first\n\ndata: second\n\n" in response_out["raw"]
        finally:
            proxy_server.shutdown()
            proxy_server.server_close()
            proxy_thread.join(timeout=2)
    finally:
        upstream_server.shutdown()
        upstream_server.server_close()
        upstream_thread.join(timeout=2)


def test_openai_proxy_tunnels_websocket_upgrade(tmp_path: Path) -> None:
    upstream_captured: dict[str, object] = {}

    class _UpstreamHandler(socketserver.BaseRequestHandler):
        def handle(self) -> None:
            request_bytes = bytearray()
            while b"\r\n\r\n" not in request_bytes:
                chunk = self.request.recv(1)
                if not chunk:
                    break
                request_bytes.extend(chunk)
            upstream_captured["request"] = request_bytes.decode("iso-8859-1")
            self.request.sendall(
                b"HTTP/1.1 101 Switching Protocols\r\n"
                b"Connection: Upgrade\r\n"
                b"Upgrade: websocket\r\n"
                b"Sec-WebSocket-Accept: test\r\n"
                b"\r\n"
            )
            upstream_captured["tunneled"] = self.request.recv(4)
            self.request.sendall(b"pong")

    upstream_server = socketserver.ThreadingTCPServer(
        ("127.0.0.1", 0), _UpstreamHandler
    )
    upstream_thread = _start_server(upstream_server)
    try:
        proxy_server = build_server(
            OpenAIProxyConfig(
                upstream_base_url=f"http://127.0.0.1:{upstream_server.server_address[1]}",
                log_dir=tmp_path / "records",
                host="127.0.0.1",
                port=0,
            )
        )
        proxy_thread = _start_server(proxy_server)
        try:
            proxy_port = proxy_server.server_address[1]
            connection = socket.create_connection(("127.0.0.1", proxy_port), timeout=10)
            connection.sendall(
                (
                    "GET /v1/responses HTTP/1.1\r\n"
                    "Host: 127.0.0.1\r\n"
                    "Connection: Upgrade\r\n"
                    "Upgrade: websocket\r\n"
                    "Sec-WebSocket-Key: test-key\r\n"
                    "Sec-WebSocket-Version: 13\r\n"
                    "Authorization: Bearer websocket-token\r\n"
                    "\r\n"
                ).encode("ascii")
            )

            response_head = bytearray()
            while b"\r\n\r\n" not in response_head:
                chunk = connection.recv(1)
                if not chunk:
                    break
                response_head.extend(chunk)

            assert b"101 Switching Protocols" in response_head
            assert b"Upgrade: websocket" in response_head

            connection.sendall(b"ping")
            tunneled_response = connection.recv(4)
            connection.close()

            assert tunneled_response == b"pong"
            upstream_request = cast(str, upstream_captured["request"])
            assert "Host: chatgpt.com" in upstream_request
            assert "oai-product-sku: codex" in upstream_request
            assert "originator: codex-tui" in upstream_request
            assert (
                "user-agent: codex-tui/0.142.3 (Mac OS 26.5.1; arm64) "
                "Apple_Terminal/470.2 (codex-tui; 0.142.3)" in upstream_request
            )
            assert "Connection: Upgrade" in upstream_request
            assert "Upgrade: websocket" in upstream_request
            assert "Authorization: Bearer websocket-token" in upstream_request
            assert cast(bytes, upstream_captured["tunneled"]) == b"ping"

            records = _wait_for_exchange_dump_paths(tmp_path / "records")
            request_out = json.loads(records["request-out"].read_text(encoding="utf-8"))
            response_in = json.loads(records["response-in"].read_text(encoding="utf-8"))
            response_out = json.loads(
                records["response-out"].read_text(encoding="utf-8")
            )
            assert set(request_out) == {"raw"}
            assert set(response_in) == {"raw"}
            assert set(response_out) == {"raw"}
            assert request_out["raw"].startswith("GET /v1/responses HTTP/1.1")
            assert "Sec-WebSocket-Key: test-key" in request_out["raw"]
            assert response_in["raw"].startswith("HTTP/1.1 101 Switching Protocols")
            assert response_out["raw"].startswith("HTTP/1.1 101 Switching Protocols")
        finally:
            proxy_server.shutdown()
            proxy_server.server_close()
            proxy_thread.join(timeout=2)
    finally:
        upstream_server.shutdown()
        upstream_server.server_close()
        upstream_thread.join(timeout=2)


def test_openai_proxy_preserves_client_authorization(tmp_path: Path) -> None:
    upstream_captured: dict[str, object] = {}

    class _UpstreamHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            _ = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            upstream_captured["headers"] = dict(self.headers.items())

            response_body = json.dumps(
                {
                    "id": "resp-1",
                    "object": "response",
                    "output_text": "hello from upstream",
                }
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(response_body)))
            self.end_headers()
            self.wfile.write(response_body)

        def log_message(self, format: str, *args: object) -> None:  # noqa: A003
            return

    upstream_server = ThreadingHTTPServer(("127.0.0.1", 0), _UpstreamHandler)
    upstream_thread = _start_server(upstream_server)
    try:
        proxy_server = build_server(
            OpenAIProxyConfig(
                upstream_base_url=f"http://127.0.0.1:{upstream_server.server_address[1]}",
                log_dir=tmp_path / "records",
                host="127.0.0.1",
                port=0,
            )
        )
        proxy_thread = _start_server(proxy_server)
        try:
            proxy_port = proxy_server.server_address[1]
            connection = http.client.HTTPConnection("127.0.0.1", proxy_port, timeout=10)
            connection.request(
                "POST",
                "/v1/responses",
                body=b"{}",
                headers={
                    "Content-Type": "application/json",
                    "Authorization": "Bearer client-session-token",
                },
            )
            response = connection.getresponse()
            response.read()
            connection.close()

            assert response.status == 200
            upstream_headers = cast(dict[str, str], upstream_captured["headers"])
            assert upstream_headers["Authorization"] == "Bearer client-session-token"

            records = _wait_for_exchange_dump_paths(tmp_path / "records")
            request_in = json.loads(records["request-in"].read_text(encoding="utf-8"))
            assert set(request_in) == {"raw"}
            assert request_in["raw"].startswith("POST /v1/responses HTTP/1.1")
        finally:
            proxy_server.shutdown()
            proxy_server.server_close()
            proxy_thread.join(timeout=2)
    finally:
        upstream_server.shutdown()
        upstream_server.server_close()
        upstream_thread.join(timeout=2)


def test_openai_proxy_relays_upstream_permission_errors(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class _UpstreamHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            _ = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            response_body = json.dumps(
                {
                    "error": {
                        "message": "You have insufficient permissions for this model.",
                        "type": "invalid_request_error",
                    }
                }
            ).encode("utf-8")
            self.send_response(403, "Forbidden")
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(response_body)))
            self.end_headers()
            self.wfile.write(response_body)

        def log_message(self, format: str, *args: object) -> None:  # noqa: A003
            return

    upstream_server = ThreadingHTTPServer(("127.0.0.1", 0), _UpstreamHandler)
    upstream_thread = _start_server(upstream_server)
    try:
        proxy_server = build_server(
            OpenAIProxyConfig(
                upstream_base_url=f"http://127.0.0.1:{upstream_server.server_address[1]}",
                log_dir=tmp_path / "records",
                host="127.0.0.1",
                port=0,
            )
        )
        proxy_thread = _start_server(proxy_server)
        try:
            proxy_port = proxy_server.server_address[1]
            connection = http.client.HTTPConnection("127.0.0.1", proxy_port, timeout=10)
            connection.request(
                "POST",
                "/v1/responses",
                body=b"{}",
                headers={"Content-Type": "application/json"},
            )
            response = connection.getresponse()
            response_body = response.read()
            connection.close()

            assert response.status == 403
            assert json.loads(response_body) == {
                "error": {
                    "message": "You have insufficient permissions for this model.",
                    "type": "invalid_request_error",
                }
            }

            records = _wait_for_exchange_dump_paths(tmp_path / "records")
            response_in = json.loads(records["response-in"].read_text(encoding="utf-8"))
            response_out = json.loads(
                records["response-out"].read_text(encoding="utf-8")
            )
            assert set(response_in) == {"raw"}
            assert set(response_out) == {"raw"}
            assert response_in["raw"].startswith("HTTP/1.1 403 Forbidden")
            assert response_out["raw"].startswith("HTTP/1.1 403 Forbidden")
            assert response_body.decode("utf-8") in response_in["raw"]
            assert response_body.decode("utf-8") in response_out["raw"]

            captured_output = capsys.readouterr().out
            assert "RESPONSE 403 Forbidden:" in captured_output
        finally:
            proxy_server.shutdown()
            proxy_server.server_close()
            proxy_thread.join(timeout=2)
    finally:
        upstream_server.shutdown()
        upstream_server.server_close()
        upstream_thread.join(timeout=2)


def test_openai_proxy_ignores_disconnect_errors() -> None:
    assert _is_disconnect_error(ConnectionResetError())
    assert _is_disconnect_error(BrokenPipeError())
    assert _is_disconnect_error(ConnectionAbortedError())
    assert not _is_disconnect_error(RuntimeError("no"))


def test_openai_proxy_cli_invokes_proxy_server(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def _fake_serve(config: OpenAIProxyConfig) -> None:
        captured["config"] = config

    monkeypatch.setattr("powdrr_lift.cli.serve_openai_proxy", _fake_serve)
    monkeypatch.chdir(tmp_path)

    exit_code = main(
        [
            "openai-proxy",
            "--upstream-base-url",
            "https://api.openai.com",
            "--host",
            "0.0.0.0",
            "--port",
            "8123",
            "--client-path-prefix",
            "/v1",
            "--upstream-path-prefix",
            "/v1",
        ]
    )

    assert exit_code == 0
    config = captured["config"]
    assert isinstance(config, OpenAIProxyConfig)
    assert config.upstream_base_url == "https://api.openai.com"
    assert config.host == "0.0.0.0"
    assert config.port == 8123
    assert config.client_path_prefix == "/v1"
    assert config.upstream_path_prefix == "/v1"
    assert config.log_dir == tmp_path / ".powdrr" / "openai-proxy"


def _start_server(server: Any) -> threading.Thread:
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    _wait_for_port(server.server_address[1])
    return thread


def _wait_for_port(port: int) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                return
        except OSError:
            time.sleep(0.01)
    raise RuntimeError(f"Timed out waiting for port {port}.")


def _wait_for_exchange_dump_paths(log_dir: Path) -> dict[str, Path]:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        records = {
            "request-in": next(log_dir.rglob("*-request-in.json"), None),
            "request-out": next(log_dir.rglob("*-request-out.json"), None),
            "response-in": next(log_dir.rglob("*-response-in.json"), None),
            "response-out": next(log_dir.rglob("*-response-out.json"), None),
        }
        if all(records.values()):
            try:
                for record_path in records.values():
                    assert record_path is not None
                    json.loads(record_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                time.sleep(0.01)
                continue
            return {key: cast(Path, value) for key, value in records.items()}
        time.sleep(0.01)
    records = {
        "request-in": next(log_dir.rglob("*-request-in.json"), None),
        "request-out": next(log_dir.rglob("*-request-out.json"), None),
        "response-in": next(log_dir.rglob("*-response-in.json"), None),
        "response-out": next(log_dir.rglob("*-response-out.json"), None),
    }
    assert all(records.values())
    for record_path in records.values():
        assert record_path is not None
        json.loads(record_path.read_text(encoding="utf-8"))
    return {key: cast(Path, value) for key, value in records.items()}
