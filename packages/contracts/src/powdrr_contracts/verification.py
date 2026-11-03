"""Public schema identifiers shared by local verification and service policy."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

VALIDATION_PROVIDER_INVENTORY_SCHEMA_VERSION = "verification-provider-inventory-v2"


@dataclass(frozen=True, slots=True)
class PostRunProbe:
    """Executable probe data exchanged between service policy and the agent."""

    probe_id: str
    scenario_id: str
    oracle_basis: str
    test_source: str
    function_name: str
    obligation_id: str = ""
    obligation_category: str = "other"
    obligation_rule: str = "not_applicable"
    interaction_ids: tuple[str, ...] = ()
    execution_path: str = "sync"

    def to_data(self) -> dict[str, Any]:
        return {
            "probe_id": self.probe_id,
            "scenario_id": self.scenario_id,
            "oracle_basis": self.oracle_basis,
            "function_name": self.function_name,
            "test_source": self.test_source,
            "obligation_id": self.obligation_id,
            "obligation_category": self.obligation_category,
            "obligation_rule": self.obligation_rule,
            "interaction_ids": list(self.interaction_ids),
            "execution_path": self.execution_path,
        }


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
