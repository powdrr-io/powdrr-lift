"""Tool wrapper for the interaction log writer."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..interaction_log.entities import InteractionEntry
from ..interaction_log.log_writer import LogWriter


def log_writer_tool(
    input: Any,
    output: Any,
    log_path: str | None = None,
) -> dict[str, Any]:
    """Append an interaction entry to the log file."""
    writer = LogWriter(Path(log_path) if log_path else None)
    writer.append(InteractionEntry(input=input, output=output))
    return {"status": "ok", "log_path": str(writer.log_path)}
