"""Write interaction entries to a JSON file log."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .entities import InteractionEntry, InteractionLog


class LogWriter:
    """Appends interaction entries to a JSON file under a hidden directory."""

    def __init__(self, log_path: Path | None = None) -> None:
        """Initialize the writer with the default or provided log path."""
        self.log_path = log_path or Path(".powdrr") / "interaction-log.json"

    def append(self, entry: InteractionEntry) -> None:
        """Append a single entry to the log file, creating the directory if needed."""
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        log = self._read()
        log.add(entry)
        self._write(log)

    def _read(self) -> InteractionLog:
        """Read the existing log file, or return an empty log if it does not exist."""
        if not self.log_path.exists():
            return InteractionLog()
        with self.log_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return InteractionLog(
            entries=[
                InteractionEntry(
                    role=item["role"],
                    content=item["content"],
                    timestamp=item.get("timestamp", ""),
                )
                for item in data.get("entries", [])
            ]
        )

    def _write(self, log: InteractionLog) -> None:
        """Write the log to disk as JSON."""
        with self.log_path.open("w", encoding="utf-8") as f:
            json.dump(log.to_dict(), f, indent=2)
            f.write("\n")
