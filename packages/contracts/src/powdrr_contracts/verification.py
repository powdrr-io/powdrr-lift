"""Public schema identifiers shared by local verification and service policy."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

VALIDATION_PROVIDER_INVENTORY_SCHEMA_VERSION = "verification-provider-inventory-v2"


@dataclass(frozen=True, slots=True)
class PostRunProbeResult:
    """Transport-independent outcome of running one frozen behavior probe."""

    probe_id: str
    scenario_id: str
    status: str
    command: tuple[str, ...] = ()
    returncode: int | None = None
    output: str = ""
    error: str | None = None

    def to_data(self) -> dict[str, Any]:
        return {
            "probe_id": self.probe_id,
            "scenario_id": self.scenario_id,
            "status": self.status,
            "command": list(self.command),
            "returncode": self.returncode,
            "output": self.output,
            "error": self.error,
        }
