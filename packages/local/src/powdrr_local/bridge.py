"""JSON serialization bridge used by local composition and future transports."""

from __future__ import annotations

import json
from typing import Any

from powdrr_contracts.models import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    ContractError,
    OperationInput,
    OperationReference,
    OperationRequest,
    OperationUpdate,
    json_round_trip,
)
from powdrr_contracts.service import PowdrrService

from powdrr_local.registry import registered_operation


class JsonServiceBridge:
    """Round-trip every public request and response through JSON validation."""

    def __init__(
        self, service: PowdrrService, *, max_frame_bytes: int = 32 * 1024 * 1024
    ) -> None:
        self._service = service
        self._max_frame_bytes = max_frame_bytes

    def start(self, request: OperationRequest) -> OperationUpdate:
        if not registered_operation(request.operation_kind):
            raise ContractError(
                f"unregistered operation kind: {request.operation_kind}"
            )
        payload = self._encode(request.to_dict())
        parsed = OperationRequest.from_dict(json.loads(payload))
        return self._update(self._service.start(parsed))

    def advance(self, value: OperationInput) -> OperationUpdate:
        parsed = OperationInput.from_dict(json.loads(self._encode(value.to_dict())))
        return self._update(self._service.advance(parsed))

    def inspect(self, value: OperationReference) -> OperationUpdate:
        parsed = OperationReference.from_dict(json.loads(self._encode(value.to_dict())))
        return self._update(self._service.inspect(parsed))

    def cancel(self, value: OperationReference) -> OperationUpdate:
        parsed = OperationReference.from_dict(json.loads(self._encode(value.to_dict())))
        return self._update(self._service.cancel(parsed))

    def infer(self, value: ChatCompletionRequest) -> ChatCompletionResponse:
        parsed = ChatCompletionRequest.from_dict(
            json.loads(self._encode(value.to_dict()))
        )
        response = self._service.infer(parsed)
        return ChatCompletionResponse.from_dict(
            json.loads(self._encode(response.to_dict()))
        )

    def _update(self, value: OperationUpdate) -> OperationUpdate:
        return OperationUpdate.from_dict(json.loads(self._encode(value.to_dict())))

    def _encode(self, value: dict[str, Any]) -> str:
        detached = json_round_trip(value)
        encoded = json.dumps(detached, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > self._max_frame_bytes:
            raise ContractError(
                "serialized service frame exceeds configured size bound"
            )
        return encoded
