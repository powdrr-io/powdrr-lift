"""MiniSWE model adapter that sends chat requests over local IPC."""

from __future__ import annotations

import os
import socket
import uuid
from collections.abc import Mapping
from typing import Any

from litellm import ModelResponse
from minisweagent.models.litellm_model import LitellmModel
from minisweagent.models.utils.actions_toolcall import BASH_TOOL
from powdrr_contracts.models import ChatCompletionRequest, ChatCompletionResponse

from powdrr_lift.inference_ipc import receive_frame, send_frame

_INFERENCE_FD_ENV = "POWDRR_MINISWE_INFERENCE_FD"


class IPCModel(LitellmModel):
    """Keep MiniSWE's normal parsing and accounting, replacing network calls."""

    def __init__(self, *, inference_fd: int | str | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        configured_fd = inference_fd or os.environ.get(_INFERENCE_FD_ENV)
        if configured_fd is None:
            raise RuntimeError("MiniSWE inference IPC descriptor is not configured")
        self._inference_socket = socket.socket(fileno=int(configured_fd))

    def _query(self, messages: list[dict[str, Any]], **kwargs: Any) -> ModelResponse:
        raw_tools = kwargs.pop("tools", [BASH_TOOL])
        if not isinstance(raw_tools, list) or not all(
            isinstance(tool, Mapping) for tool in raw_tools
        ):
            raise RuntimeError("MiniSWE supplied malformed tool definitions")
        request_id = uuid.uuid4().hex
        request = ChatCompletionRequest(
            model=self.config.model_name,
            messages=tuple(messages),
            tools=tuple(dict(tool) for tool in raw_tools),
            options=self.config.model_kwargs | kwargs,
            request_id=request_id,
        )
        send_frame(self._inference_socket, {"request": request.to_dict()})
        envelope = receive_frame(self._inference_socket)
        if envelope.get("request_id") != request_id:
            raise RuntimeError("MiniSWE received an uncorrelated inference response")
        if envelope.get("ok") is not True:
            error = envelope.get("error")
            detail = error.get("message") if isinstance(error, Mapping) else None
            raise RuntimeError(str(detail or "Powdrr inference request failed"))
        response = ChatCompletionResponse.from_dict(envelope.get("response"))
        provider_response = response.provider_data or _provider_response(
            request, response
        )
        try:
            return ModelResponse(**provider_response)
        except Exception as error:  # noqa: BLE001
            raise RuntimeError("Powdrr returned an invalid chat completion") from error


def _provider_response(
    request: ChatCompletionRequest, response: ChatCompletionResponse
) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": response.content}
    if response.tool_calls:
        message["tool_calls"] = list(response.tool_calls)
    return {
        "id": request.request_id,
        "object": "chat.completion",
        "model": request.model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": response.finish_reason,
            }
        ],
        "usage": response.usage,
    }
