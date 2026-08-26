"""Feed interaction inputs and outputs into the log writer."""

from __future__ import annotations

from typing import Any

from .entities import InteractionEntry
from .log_writer import LogWriter


class InteractionSource:
    """Captures human and LLM interactions and writes them to the log."""

    def __init__(self, writer: LogWriter | None = None) -> None:
        """Initialize the source with a log writer."""
        self.writer = writer or LogWriter()

    def record(self, role: str, content: Any) -> None:
        """Record an interaction input or output through the writer."""
        self.writer.append(InteractionEntry(role=role, content=content))
