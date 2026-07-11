from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import httpx
import pytest
from starlette.testclient import TestClient

from powdrr_lift.cli import main
from powdrr_lift.openai_proxy import OpenAIProxyConfig, build_server


def test_openai_proxy_forwards_and_records_exchange(tmp_path: Path) -> None:
    upstream_captured: dict[str, object] = {}

    real_async_client = httpx.AsyncClient

    def _handler(request: httpx.Request) -> httpx.Response:
        upstream_captured["method"] = request.method
        upstream_captured["path"] = request.url.raw_path.decode("ascii")
        upstream_captured["headers"] = dict(request.headers.items())
        upstream_captured["body"] = request.read()

        response_body = json.dumps(
            {
                "id": "resp-1",
                "object": "response",
                "output_text": "hello from upstream",
            }
        ).encode("utf-8")
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json; charset=utf-8"},
            content=response_body,
        )

    def _client_factory(
        *,
        timeout: httpx.Timeout
        | float
        | tuple[
            float | None,
            float | None,
            float | None,
            float | None,
        ]
        | None = None,
    ) -> httpx.AsyncClient:
        return real_async_client(
            timeout=timeout,
            transport=httpx.MockTransport(_handler),
        )

    proxy_app = build_server(
        OpenAIProxyConfig(
            upstream_base_url="http://upstream.invalid",
            log_dir=tmp_path / "records",
            host="127.0.0.1",
            port=0,
        )
    )
    request_body = json.dumps(
        {
            "model": "gpt-4.1",
            "input": [{"role": "user", "content": "hello"}],
        }
    ).encode("utf-8")
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr("powdrr_lift.openai_proxy.httpx.AsyncClient", _client_factory)
    try:
        with TestClient(proxy_app, base_url="http://testserver") as client:
            response = client.post(
                "/v1/responses?debug=true",
                content=request_body,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": "Bearer test-token",
                },
            )
            response_body = response.content
    finally:
        monkeypatch.undo()

    assert response.status_code == 200
    assert json.loads(response_body) == {
        "id": "resp-1",
        "object": "response",
        "output_text": "hello from upstream",
    }

    assert upstream_captured["method"] == "POST"
    assert upstream_captured["path"] == "/v1/responses?debug=true"
    assert upstream_captured["body"] == request_body
    upstream_headers = {
        header_name.lower(): header_value
        for header_name, header_value in cast(
            dict[str, str], upstream_captured["headers"]
        ).items()
    }
    assert upstream_headers["authorization"] == "Bearer test-token"

    record_path = _wait_for_single_record_path(tmp_path / "records")
    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert record["request"]["method"] == "POST"
    assert record["request"]["client_path"] == "/v1/responses"
    assert record["request"]["forwarded_path"] == "/v1/responses?debug=true"
    recorded_headers = {
        header_name.lower(): header_value
        for header_name, header_value in record["request"]["headers"].items()
    }
    assert recorded_headers["authorization"] == "[redacted]"
    assert record["request"]["body"]["text"] == request_body.decode("utf-8")
    assert record["response"]["status"] == 200
    assert record["response"]["body"]["text"] == response_body.decode("utf-8")


def test_openai_proxy_preserves_chunked_streaming(tmp_path: Path) -> None:
    real_async_client = httpx.AsyncClient

    def _handler(request: httpx.Request) -> httpx.Response:
        request.read()
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            stream=httpx.ByteStream(b"data: first\n\ndata: second\n\n"),
        )

    def _client_factory(
        *,
        timeout: httpx.Timeout
        | float
        | tuple[
            float | None,
            float | None,
            float | None,
            float | None,
        ]
        | None = None,
    ) -> httpx.AsyncClient:
        return real_async_client(
            timeout=timeout,
            transport=httpx.MockTransport(_handler),
        )

    proxy_app = build_server(
        OpenAIProxyConfig(
            upstream_base_url="http://upstream.invalid",
            log_dir=tmp_path / "records",
            host="127.0.0.1",
            port=0,
        )
    )
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr("powdrr_lift.openai_proxy.httpx.AsyncClient", _client_factory)
    try:
        with TestClient(proxy_app, base_url="http://testserver") as client:
            response = client.post(
                "/v1/responses",
                content=b"{}",
                headers={"Content-Type": "application/json"},
            )
    finally:
        monkeypatch.undo()

    assert response.status_code == 200
    assert response.content == b"data: first\n\ndata: second\n\n"

    record_path = _wait_for_single_record_path(tmp_path / "records")
    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert record["response"]["body"]["text"] == "data: first\n\ndata: second\n\n"


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


def _wait_for_single_record_path(log_dir: Path) -> Path:
    records = sorted(log_dir.rglob("*.json"))
    assert len(records) == 1
    return records[0]
