"""Compose MiniSWE inference through the local service bridge."""

from __future__ import annotations

from collections.abc import Callable

from powdrr_contracts.models import ChatCompletionRequest, ChatCompletionResponse
from powdrr_service.local_service import LocalPowdrrService
from powdrr_service.providers.inference import (
    DeepInfraInferenceClient,
    InferenceClient,
)

from powdrr_local.bridge import JsonServiceBridge


def create_miniswe_inference_handler(
    worker_timeout_seconds: float,
    *,
    inference_client: InferenceClient | None = None,
) -> Callable[[ChatCompletionRequest], ChatCompletionResponse]:
    """Build the parent-side handler for MiniSWE worker requests."""
    request_timeout = max(0.1, min(120.0, worker_timeout_seconds * 0.8))
    service = LocalPowdrrService(
        inference_client=inference_client
        or DeepInfraInferenceClient(timeout=request_timeout)
    )
    return JsonServiceBridge(service).infer


__all__ = ["create_miniswe_inference_handler"]
