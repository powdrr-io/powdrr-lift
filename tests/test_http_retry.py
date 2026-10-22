from __future__ import annotations

from email.message import Message
from urllib.error import HTTPError

import pytest

from powdrr_lift.workrr import http_retry


@pytest.mark.parametrize("status_code", [429, 503])
def test_retry_transient_http_uses_exponential_backoff(
    monkeypatch: pytest.MonkeyPatch, status_code: int
) -> None:
    delays: list[float] = []
    calls = 0

    def operation() -> str:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise HTTPError(
                "https://example.test", status_code, "temporary", Message(), None
            )
        return "ok"

    monkeypatch.setattr(http_retry.time, "sleep", delays.append)

    assert (
        http_retry.retry_transient_http(
            operation, max_retries=3, base_delay_seconds=0.5
        )
        == "ok"
    )
    assert calls == 3
    assert delays == [0.5, 1.0]


def test_retry_transient_http_does_not_retry_client_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delays: list[float] = []

    def fail() -> None:
        raise HTTPError("https://example.test", 400, "invalid request", Message(), None)

    monkeypatch.setattr(http_retry.time, "sleep", delays.append)
    with pytest.raises(HTTPError):
        http_retry.retry_transient_http(fail)
    assert delays == []
