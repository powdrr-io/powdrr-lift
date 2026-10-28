"""Transport-independent API provided by the Powdrr service."""

from __future__ import annotations

from typing import Protocol

from powdrr_contracts.models import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    OperationInput,
    OperationReference,
    OperationRequest,
    OperationUpdate,
)


class PowdrrService(Protocol):
    def start(self, request: OperationRequest) -> OperationUpdate: ...

    def advance(self, operation_input: OperationInput) -> OperationUpdate: ...

    def inspect(self, reference: OperationReference) -> OperationUpdate: ...

    def cancel(self, reference: OperationReference) -> OperationUpdate: ...

    def infer(self, request: ChatCompletionRequest) -> ChatCompletionResponse: ...
