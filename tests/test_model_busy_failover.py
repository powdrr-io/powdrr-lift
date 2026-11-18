from __future__ import annotations

import pytest
from powdrr_service.providers.config import (
    DEEPINFRA_CHEAP_BACKUP_MODEL,
    DEEPINFRA_CHEAP_MODEL,
)
from powdrr_service.providers.errors import ProviderHTTPError
from powdrr_service.providers.model_failover import ModelBusyFailover, is_model_busy


def test_cheap_model_uses_requested_primary_and_backup() -> None:
    assert DEEPINFRA_CHEAP_MODEL == "deepseek-ai/DeepSeek-V4.1-Flash-1017"
    assert DEEPINFRA_CHEAP_BACKUP_MODEL == "deepseek-ai/DeepSeek-V4-Flash-0731"


def test_only_model_busy_429_is_failover_eligible() -> None:
    assert is_model_busy(ProviderHTTPError("DeepInfra", 429, "model busy"))
    assert not is_model_busy(ProviderHTTPError("DeepInfra", 429, "rate limited"))
    assert not is_model_busy(ProviderHTTPError("DeepInfra", 503, "model busy"))


def test_model_busy_switches_to_backup_after_primary_retry_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from powdrr_service.providers import model_failover

    clock = [0.0]
    monkeypatch.setattr(model_failover.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        model_failover.time,
        "sleep",
        lambda delay: clock.__setitem__(0, clock[0] + delay),
    )
    calls: list[str] = []

    def call(model: str) -> str:
        calls.append(model)
        if model == DEEPINFRA_CHEAP_MODEL:
            raise ProviderHTTPError("DeepInfra", 429, "model busy")
        return model

    failover = ModelBusyFailover()
    assert (
        failover.run(
            call,
            primary_model=DEEPINFRA_CHEAP_MODEL,
            backup_model=DEEPINFRA_CHEAP_BACKUP_MODEL,
        )
        == DEEPINFRA_CHEAP_BACKUP_MODEL
    )
    assert calls.count(DEEPINFRA_CHEAP_MODEL) > 1
    assert calls[-1] == DEEPINFRA_CHEAP_BACKUP_MODEL
    assert (
        failover.run(
            call,
            primary_model=DEEPINFRA_CHEAP_MODEL,
            backup_model=DEEPINFRA_CHEAP_BACKUP_MODEL,
        )
        == DEEPINFRA_CHEAP_BACKUP_MODEL
    )
    assert calls[-1] == DEEPINFRA_CHEAP_BACKUP_MODEL


def test_non_busy_error_is_not_retried() -> None:
    calls = 0

    def call(_model: str) -> str:
        nonlocal calls
        calls += 1
        raise ProviderHTTPError("DeepInfra", 429, "quota exceeded")

    with pytest.raises(ProviderHTTPError, match="quota exceeded"):
        ModelBusyFailover().run(
            call,
            primary_model=DEEPINFRA_CHEAP_MODEL,
            backup_model=DEEPINFRA_CHEAP_BACKUP_MODEL,
        )
    assert calls == 1


def test_primary_is_retried_after_five_minute_backup_cooldown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from powdrr_service.providers import model_failover

    clock = [0.0]
    monkeypatch.setattr(model_failover.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        model_failover.time,
        "sleep",
        lambda delay: clock.__setitem__(0, clock[0] + delay),
    )
    primary_busy = [True]
    calls: list[str] = []

    def call(model: str) -> str:
        calls.append(model)
        if model == DEEPINFRA_CHEAP_MODEL and primary_busy[0]:
            raise ProviderHTTPError("DeepInfra", 429, "model busy")
        return model

    failover = ModelBusyFailover()
    assert (
        failover.run(
            call,
            primary_model=DEEPINFRA_CHEAP_MODEL,
            backup_model=DEEPINFRA_CHEAP_BACKUP_MODEL,
        )
        == DEEPINFRA_CHEAP_BACKUP_MODEL
    )
    clock[0] += model_failover.BACKUP_COOLDOWN_SECONDS + 1
    primary_busy[0] = False
    assert (
        failover.run(
            call,
            primary_model=DEEPINFRA_CHEAP_MODEL,
            backup_model=DEEPINFRA_CHEAP_BACKUP_MODEL,
        )
        == DEEPINFRA_CHEAP_MODEL
    )
    assert calls[-1] == DEEPINFRA_CHEAP_MODEL
