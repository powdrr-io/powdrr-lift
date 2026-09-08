"""Durable JSON logging for human and model interactions."""

from __future__ import annotations

import json
import os
import tempfile
import threading
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

INTERACTION_LOG_SCHEMA_VERSION = 1
_WRITE_LOCK = threading.Lock()
_CURRENT_LOG: ContextVar[InteractionLog | None] = ContextVar(
    "powdrr_interaction_log", default=None
)


class InteractionLog:
    """Append structured interactions to one repository-local JSON document."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()

    def record(
        self,
        *,
        actor: str,
        input_value: Any,
        output_value: Any,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if actor not in {"human", "llm"}:
            raise ValueError("interaction actor must be human or llm")
        interaction: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "actor": actor,
            "input": input_value,
            "output": output_value,
        }
        if metadata:
            interaction["metadata"] = dict(metadata)
        with _WRITE_LOCK:
            document = self._read()
            document["interactions"].append(interaction)
            self._write(document)

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {
                "schema_version": INTERACTION_LOG_SCHEMA_VERSION,
                "interactions": [],
            }
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"Could not read interaction log {self.path}: {exc}"
            ) from exc
        if (
            not isinstance(document, dict)
            or document.get("schema_version") != INTERACTION_LOG_SCHEMA_VERSION
        ):
            raise RuntimeError(f"Invalid interaction log schema: {self.path}")
        interactions = document.get("interactions")
        if not isinstance(interactions, list):
            raise RuntimeError(f"Invalid interaction list: {self.path}")
        return document

    def _write(self, document: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.", dir=self.path.parent
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(document, stream, indent=2, ensure_ascii=False)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_name, self.path)
        finally:
            if os.path.exists(temporary_name):
                os.unlink(temporary_name)


def set_current_interaction_log(path: str | Path) -> None:
    """Route human prompt records for the active execution to ``path``."""
    _CURRENT_LOG.set(InteractionLog(path))


def record_current_human_interaction(prompt: str, answer: str) -> None:
    log = _CURRENT_LOG.get()
    if log is not None:
        log.record(actor="human", input_value=prompt, output_value=answer)
