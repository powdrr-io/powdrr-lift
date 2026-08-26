"""Domain entities for the interaction file log."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class InteractionEntry:
    """A single recorded human or LLM interaction."""

    role: str
    content: Any
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable representation of this entry."""
        return {
            "role": self.role,
            "content": self.content,
            "timestamp": self.timestamp,
        }


@dataclass
class InteractionLog:
    """An in-memory collection of interaction entries."""

    entries: list[InteractionEntry] = field(default_factory=list)

    def add(self, entry: InteractionEntry) -> None:
        """Append an entry to the log."""
        self.entries.append(entry)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable representation of the log."""
        return {"entries": [entry.to_dict() for entry in self.entries]}
