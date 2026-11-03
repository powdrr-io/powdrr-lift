"""Length-prefixed JSON frames for local MiniSWE inference IPC."""

from __future__ import annotations

import json
import socket
import struct
from typing import Any

MAX_INFERENCE_FRAME_BYTES = 32 * 1024 * 1024
_FRAME_HEADER = struct.Struct("!I")


class InferenceIPCError(RuntimeError):
    """An inference IPC frame is malformed or cannot be exchanged."""


def send_frame(connection: socket.socket, value: dict[str, Any]) -> None:
    payload = json.dumps(value, separators=(",", ":")).encode("utf-8")
    if len(payload) > MAX_INFERENCE_FRAME_BYTES:
        raise InferenceIPCError("inference IPC frame exceeds its size limit")
    connection.sendall(_FRAME_HEADER.pack(len(payload)) + payload)


def receive_frame(connection: socket.socket) -> dict[str, Any]:
    header = _receive_exact(connection, _FRAME_HEADER.size)
    (size,) = _FRAME_HEADER.unpack(header)
    if size > MAX_INFERENCE_FRAME_BYTES:
        raise InferenceIPCError("inference IPC frame exceeds its size limit")
    try:
        value = json.loads(_receive_exact(connection, size))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise InferenceIPCError("inference IPC frame is not valid JSON") from error
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise InferenceIPCError("inference IPC frame must be a JSON object")
    return value


def _receive_exact(connection: socket.socket, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        chunk = connection.recv(size - len(chunks))
        if not chunk:
            raise EOFError("MiniSWE inference IPC peer closed the connection")
        chunks.extend(chunk)
    return bytes(chunks)
