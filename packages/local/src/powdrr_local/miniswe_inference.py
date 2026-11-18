"""Compose MiniSWE inference through the local service bridge."""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from threading import Lock
from typing import Any

from powdrr_contracts.models import ChatCompletionRequest, ChatCompletionResponse
from powdrr_service.local_service import LocalPowdrrService
from powdrr_service.providers.inference import (
    DeepInfraInferenceClient,
    InferenceClient,
    RoutedInferenceClient,
)

from powdrr_local.bridge import JsonServiceBridge

_MINISWE_PROMPT_BUILDER_VERSION = "minisweagent-session-v1"


def create_miniswe_inference_handler(
    worker_timeout_seconds: float,
    *,
    inference_client: InferenceClient | None = None,
) -> Callable[[ChatCompletionRequest], ChatCompletionResponse]:
    """Build the parent-side handler for MiniSWE worker requests."""
    request_timeout = max(0.1, min(120.0, worker_timeout_seconds * 0.8))
    service = LocalPowdrrService(
        inference_client=inference_client
        or RoutedInferenceClient(
            timeout=request_timeout,
            default_client=DeepInfraInferenceClient(timeout=request_timeout),
        )
    )
    return JsonServiceBridge(service).infer


def trace_miniswe_inference_handler(
    handler: Callable[[ChatCompletionRequest], ChatCompletionResponse],
    *,
    trace_path: Path,
    task_id: str,
    attempt_id: str,
) -> Callable[[ChatCompletionRequest], ChatCompletionResponse]:
    """Record redacted interaction metadata around each MiniSWE inference call."""
    write_lock = Lock()

    def traced(request: ChatCompletionRequest) -> ChatCompletionResponse:
        started = time.monotonic()
        prompt_fingerprint = _fingerprint(
            {"messages": request.messages, "tools": request.tools}
        )
        try:
            response = handler(request)
        except Exception as error:
            _append_trace(
                trace_path,
                write_lock,
                {
                    **_trace_identity(request, task_id, attempt_id),
                    "prompt_builder_version": _MINISWE_PROMPT_BUILDER_VERSION,
                    "prompt_fingerprint": prompt_fingerprint,
                    "provider": _provider_for_model(request.model),
                    "model": request.model,
                    "duration_ms": round((time.monotonic() - started) * 1000),
                    "outcome": "error",
                    "error_type": type(error).__name__,
                    "transport_attempt": 1,
                    "retry_cause": None,
                    "replay_status": "miss",
                    "usage": {},
                    "prompt_tokens": None,
                    "completion_tokens": None,
                    "cost_amount": None,
                    "cost_currency": None,
                },
            )
            raise
        usage = response.usage
        _append_trace(
            trace_path,
            write_lock,
            {
                **_trace_identity(request, task_id, attempt_id),
                "prompt_builder_version": _MINISWE_PROMPT_BUILDER_VERSION,
                "prompt_fingerprint": prompt_fingerprint,
                "provider": _provider_for_model(request.model),
                "model": request.model,
                "duration_ms": round((time.monotonic() - started) * 1000),
                "outcome": "success",
                "transport_attempt": 1,
                "retry_cause": None,
                "replay_status": "miss",
                "usage": _safe_usage(usage),
                "prompt_tokens": _numeric_usage(usage, "prompt_tokens"),
                "completion_tokens": _numeric_usage(usage, "completion_tokens"),
                "cost_amount": _numeric_usage(usage, "cost"),
                "cost_currency": usage.get("cost_currency"),
            },
        )
        return response

    return traced


def _trace_identity(
    request: ChatCompletionRequest, task_id: str, attempt_id: str
) -> dict[str, Any]:
    return {
        "record_type": "powdrr_service.miniswe.provider_attempt",
        "interaction_id": request.request_id,
        "request_id": request.request_id,
        "caller": "minisweagent",
        "gate_consumer": None,
        "task_id": task_id,
        "attempt_id": attempt_id,
    }


def _provider_for_model(model: str) -> str:
    if model.startswith("isoquant/") or model == "deepseek-v4.1-flash-1017-fast":
        return "isoquant"
    return "deepinfra"


def _safe_usage(usage: Mapping[str, Any]) -> dict[str, int | float | str]:
    return {
        key: value
        for key in ("prompt_tokens", "completion_tokens", "total_tokens", "cost")
        if isinstance((value := usage.get(key)), (int, float, str))
        and not isinstance(value, bool)
    }


def _numeric_usage(usage: Mapping[str, Any], key: str) -> int | float | None:
    value = usage.get(key)
    return (
        value
        if isinstance(value, (int, float)) and not isinstance(value, bool)
        else None
    )


def _fingerprint(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _append_trace(path: Path, lock: Lock, record: Mapping[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with lock, path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(dict(record), sort_keys=True) + "\n")
    except OSError:
        # Diagnostic persistence must not change the model request's outcome.
        return


__all__ = ["create_miniswe_inference_handler", "trace_miniswe_inference_handler"]
