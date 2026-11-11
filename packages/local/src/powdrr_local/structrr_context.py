"""Local IPC composition for the private Structrr observation service."""

from __future__ import annotations

import json
import socket
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from powdrr_agent.execution.inference_ipc import receive_frame, send_frame
from powdrr_service.structrr.reference_validation import ReferenceValidationReport
from powdrr_service.structrr.source_observation import SourceObservationSession


class StructrrContextBridge:
    """Serve bounded read and enrichment requests; never send the catalog file."""

    def __init__(
        self,
        session: SourceObservationSession,
        event_path: Path,
        mode: str,
        *,
        reference_check: Callable[[], ReferenceValidationReport] | None = None,
    ) -> None:
        if mode not in {"observe", "enforce"}:
            raise ValueError("source reference mode must be observe or enforce")
        self.session = session
        self.event_path = event_path
        self.mode = mode
        self.reference_check = reference_check
        self._temporary: tempfile.TemporaryDirectory[str] | None = None
        self._socket: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def __enter__(self) -> StructrrContextBridge:
        self.event_path.parent.mkdir(parents=True, exist_ok=True)
        metadata = {
            "schema_version": "structrr-source-attempt-inputs-v1",
            "source_reference_mode": self.mode,
            "catalog_digest": self.session.reader.catalog.digest,
            "base_commit": self.session.reader.catalog.base_commit,
            "limits_digest": self.session.reader.limits.digest,
        }
        metadata_path = self.event_path.with_suffix(".inputs.json")
        if metadata_path.exists():
            if json.loads(metadata_path.read_text(encoding="utf-8")) != metadata:
                raise ValueError("source attempt inputs cannot change")
        else:
            with metadata_path.open("x", encoding="utf-8") as stream:
                stream.write(json.dumps(metadata, sort_keys=True) + "\n")
        # Keep AF_UNIX paths within the macOS limit; directory is mode 0700.
        self._temporary = tempfile.TemporaryDirectory(prefix="pctx-", dir="/tmp")
        self.endpoint = str(Path(self._temporary.name) / "s")
        self._socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._socket.bind(self.endpoint)
        self._socket.listen(8)
        self._socket.settimeout(0.2)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *args: Any) -> None:
        self._stop.set()
        if self._socket is not None:
            self._socket.close()
        if self._thread is not None:
            self._thread.join(timeout=2)
        if self._temporary is not None:
            self._temporary.cleanup()

    def _serve(self) -> None:
        assert self._socket is not None
        while not self._stop.is_set():
            try:
                connection, _ = self._socket.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            with connection:
                connection.settimeout(5)
                try:
                    request = receive_frame(connection)
                    response = self._dispatch(request)
                    send_frame(connection, {"ok": True, "response": response})
                except (
                    ValueError,
                    TypeError,
                    KeyError,
                    OSError,
                    RuntimeError,
                    EOFError,
                ) as error:
                    try:
                        send_frame(connection, {"ok": False, "error": str(error)})
                    except OSError:
                        pass

    def _dispatch(self, request: dict[str, Any]) -> dict[str, Any]:
        operation = request.get("operation")
        if operation == "hello" and set(request) == {"operation"}:
            return {
                "catalog_digest": self.session.reader.catalog.digest,
                "limits_digest": self.session.reader.limits.digest,
                "mode": self.mode,
            }
        if (
            operation == "reader"
            and set(request) == {"operation", "request"}
            and isinstance(request["request"], dict)
        ):
            if request["request"] == {"operation": "check"}:
                if self.reference_check is None:
                    raise ValueError(
                        "Trusted reference validation inputs are not configured."
                    )
                return self.reference_check().to_data()
            return self.session.read(request["request"])
        if (
            operation in {"observe", "submission"}
            and set(request) == {"operation", "command", "output"}
            and all(isinstance(request[k], str) for k in ("command", "output"))
        ):
            result = self.session.observe(
                request["command"],
                request["output"],
                submitted=operation == "submission",
            )
            event = {**result["event"], "source_reference_mode": self.mode}
            result["event"] = event
            with self.event_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(event, sort_keys=True) + "\n")
            return result
        raise ValueError("unsupported Structrr IPC request")
