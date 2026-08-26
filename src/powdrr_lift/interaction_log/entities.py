"""Domain entities for the interaction file log."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class InteractionEntry:
    """A single recorded human or LLM interaction."""

    input: Any
    output: Any

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable representation of this entry."""
        return {
            "input": self.input,
            "output": self.output,
        }


@dataclass
class InteractionLog:
    """An in-memory collection of interaction entries."""

    entries: list[InteractionEntry] = field(default_factory=list)

    def add(self, entry: InteractionEntry) -> None:
        """Append an entry to the log."""
        self.entries.append(entry)

    def to_dict(self) -> list[dict[str, Any]]:
        """Return a JSON-serializable representation of the log."""
        return [entry.to_dict() for entry in self.entries]
