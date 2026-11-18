"""Bounded failover for DeepInfra model-busy responses."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import TypeVar

from powdrr_service.providers.errors import ProviderHTTPError

ResultT = TypeVar("ResultT")
PRIMARY_RETRY_SECONDS = 20.0
BACKUP_COOLDOWN_SECONDS = 300.0
_BACKOFF_SECONDS = (1.0, 2.0, 4.0, 5.0)


def is_model_busy(error: ProviderHTTPError) -> bool:
    if error.status_code != 429:
        return False
    detail = str(error).casefold()
    return "model busy" in detail or "model is busy" in detail


class ModelBusyFailover:
    """Use the backup during a five-minute cooldown after sustained busy errors."""

    def __init__(self) -> None:
        self._backup_until = 0.0
        self._lock = threading.Lock()

    def run(
        self,
        call: Callable[[str], ResultT],
        *,
        primary_model: str,
        backup_model: str,
    ) -> ResultT:
        now = time.monotonic()
        with self._lock:
            use_backup = now < self._backup_until
        if use_backup:
            return call(backup_model)

        deadline = now + PRIMARY_RETRY_SECONDS
        attempt = 0
        while True:
            try:
                return call(primary_model)
            except ProviderHTTPError as error:
                if not is_model_busy(error):
                    raise
                delay = _BACKOFF_SECONDS[min(attempt, len(_BACKOFF_SECONDS) - 1)]
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    with self._lock:
                        self._backup_until = time.monotonic() + BACKUP_COOLDOWN_SECONDS
                    return call(backup_model)
                time.sleep(min(delay, remaining))
                attempt += 1
