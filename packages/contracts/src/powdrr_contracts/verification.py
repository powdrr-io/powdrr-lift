"""Public schema identifiers shared by local verification and service policy."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

VALIDATION_PROVIDER_INVENTORY_SCHEMA_VERSION = "verification-provider-inventory-v2"
VERIFICATION_EVIDENCE_SCHEMA_VERSION = "verification-evidence-v1"


class EvidenceStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"
    XFAILED = "xfailed"
    DESELECTED = "deselected"
    NOT_COLLECTED = "not_collected"
    ERRORED = "errored"
    TIMED_OUT = "timed_out"
    MISSING = "missing"


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


@dataclass(frozen=True, slots=True)
class VerificationEvidence:
    """Transport-independent result of one verification obligation run."""

    obligation_id: str
    contract_id: str
    provider: str
    selector: str
    profile: str
    status: EvidenceStatus
    candidate_tree: str
    contract_fingerprint: str
    verifier_fingerprint: str
    protected_inputs_fingerprint: str
    command_fingerprint: str
    provider_version: str | None = None
    returncode: int | None = None
    duration_ms: int | None = None
    artifact_refs: tuple[str, ...] = ()
    error: str | None = None

    def to_data(self) -> dict[str, Any]:
        return {
            "schema_version": VERIFICATION_EVIDENCE_SCHEMA_VERSION,
            "obligation_id": self.obligation_id,
            "contract_id": self.contract_id,
            "provider": self.provider,
            "selector": self.selector,
            "profile": self.profile,
            "status": self.status.value,
            "candidate_tree": self.candidate_tree,
            "contract_fingerprint": self.contract_fingerprint,
            "verifier_fingerprint": self.verifier_fingerprint,
            "protected_inputs_fingerprint": self.protected_inputs_fingerprint,
            "command_fingerprint": self.command_fingerprint,
            "provider_version": self.provider_version,
            "returncode": self.returncode,
            "duration_ms": self.duration_ms,
            "artifact_refs": list(self.artifact_refs),
            "error": self.error,
        }
