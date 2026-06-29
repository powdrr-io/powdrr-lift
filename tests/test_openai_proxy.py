from __future__ import annotations

import http.client
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast

import pytest

from powdrr_lift.cli import main
from powdrr_lift.openai_proxy import OpenAIProxyConfig, build_server


def test_openai_proxy_forwards_and_records_exchange(tmp_path: Path) -> None:
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
            assert upstream_headers["Authorization"] == "Bearer test-token"

            record_path = _wait_for_single_record_path(tmp_path / "records")
            record = json.loads(record_path.read_text(encoding="utf-8"))
            assert record["request"]["method"] == "POST"
            assert record["request"]["client_path"] == "/v1/responses"
            assert record["request"]["forwarded_path"] == "/v1/responses?debug=true"
            assert record["request"]["headers"]["Authorization"] == "[redacted]"
            assert record["request"]["body"]["text"] == request_body.decode("utf-8")
            assert record["response"]["status"] == 200
            assert record["response"]["body"]["text"] == response_body.decode("utf-8")
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

            record_path = _wait_for_single_record_path(tmp_path / "records")
            record = json.loads(record_path.read_text(encoding="utf-8"))
            assert record["response"]["body"]["text"] == (
                "data: first\n\ndata: second\n\n"
            )
        finally:
            proxy_server.shutdown()
            proxy_server.server_close()
            proxy_thread.join(timeout=2)
    finally:
        upstream_server.shutdown()
        upstream_server.server_close()
        upstream_thread.join(timeout=2)


def test_openai_proxy_cli_invokes_proxy_server(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def _fake_serve(config: OpenAIProxyConfig) -> None:
        captured["config"] = config

    monkeypatch.setattr("powdrr_lift.cli.serve_openai_proxy", _fake_serve)

    exit_code = main(
        [
            "openai-proxy",
            "--upstream-base-url",
            "https://api.openai.com",
            "--repo-root",
            str(tmp_path),
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


def _start_server(server: ThreadingHTTPServer) -> threading.Thread:
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


def _wait_for_single_record_path(log_dir: Path) -> Path:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        records = sorted(log_dir.rglob("*.json"))
        if len(records) == 1:
            return records[0]
        time.sleep(0.01)
    records = sorted(log_dir.rglob("*.json"))
    assert len(records) == 1
    return records[0]
