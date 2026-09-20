"""Artifact-backed, per-contract verification evidence."""

from __future__ import annotations

import fnmatch
import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from powdrr_lift.structrr.verification_obligations import VerificationObligation
from powdrr_lift.workrr.verification_provider import (
    ProviderExecutionResult,
    VerificationProviderRegistry,
    VerificationProviderRequest,
)

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
class VerificationEvidence:
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


ProgressCallback = Callable[[Mapping[str, Any]], None]


class VerificationEvidenceRunner:
    """Execute selected obligations and persist bounded structured evidence."""

    def __init__(
        self,
        registry: VerificationProviderRegistry,
        *,
        timeout_seconds: float = 600.0,
        progress: ProgressCallback | None = None,
    ) -> None:
        self.registry = registry
        self.timeout_seconds = timeout_seconds
        self.progress = progress

    def run(
        self,
        obligations: Sequence[VerificationObligation],
        *,
        root: Path,
        artifact_root: Path,
        candidate_tree: str,
        inventory: Sequence[Mapping[str, Any]],
    ) -> tuple[VerificationEvidence, ...]:
        artifact_root.mkdir(parents=True, exist_ok=True)
        inventory_by_key = {
            (
                str(item.get("provider", "")),
                str(item.get("profile", "")),
                str(item.get("selector", "")),
            ): item
            for item in inventory
        }
        results: list[VerificationEvidence] = []
        for obligation in obligations:
            self._emit("verification_started", obligation)
            item = inventory_by_key.get(
                (obligation.provider, obligation.profile, obligation.selector)
            )
            command = _command(item)
            command_fingerprint = _fingerprint(command)
            protected_fingerprint = _protected_inputs_fingerprint(
                root, obligation.protected_inputs
            )
            provider = self.registry.provider(obligation.provider)
            if item is None:
                result = ProviderExecutionResult(
                    obligation.provider,
                    obligation.selector,
                    obligation.profile,
                    EvidenceStatus.NOT_COLLECTED.value,
                    error="exact selector is absent from provider inventory",
                )
            elif provider is None:
                result = ProviderExecutionResult(
                    obligation.provider,
                    obligation.selector,
                    obligation.profile,
                    EvidenceStatus.ERRORED.value,
                    error="verification provider is not registered",
                )
            else:
                result = provider.execute(
                    VerificationProviderRequest(
                        obligation.provider,
                        obligation.selector,
                        obligation.profile,
                        root,
                        command=command,
                        timeout_seconds=self.timeout_seconds,
                    )
                )
            evidence = self._persist(
                obligation,
                result,
                artifact_root=artifact_root,
                candidate_tree=candidate_tree,
                command_fingerprint=command_fingerprint,
                protected_fingerprint=protected_fingerprint,
            )
            results.append(evidence)
            self._emit("verification_finished", obligation, evidence=evidence)
        return tuple(results)

    def _persist(
        self,
        obligation: VerificationObligation,
        result: ProviderExecutionResult,
        *,
        artifact_root: Path,
        candidate_tree: str,
        command_fingerprint: str,
        protected_fingerprint: str,
    ) -> VerificationEvidence:
        directory = artifact_root / obligation.obligation_id.removeprefix("sha256:")
        directory.mkdir(parents=True, exist_ok=True)
        refs: list[str] = []
        for name, content in (
            ("stdout.txt", result.stdout),
            ("stderr.txt", result.stderr),
        ):
            if content:
                path = directory / name
                path.write_text(content, encoding="utf-8")
                refs.append(str(path))
        evidence = VerificationEvidence(
            obligation_id=obligation.obligation_id,
            contract_id=obligation.contract_id,
            provider=obligation.provider,
            selector=obligation.selector,
            profile=obligation.profile,
            status=_status(result.status),
            candidate_tree=candidate_tree,
            contract_fingerprint=obligation.contract_fingerprint,
            verifier_fingerprint=obligation.verifier_fingerprint,
            protected_inputs_fingerprint=protected_fingerprint,
            command_fingerprint=command_fingerprint,
            provider_version=result.provider_version,
            returncode=result.returncode,
            duration_ms=result.duration_ms,
            artifact_refs=tuple(refs),
            error=result.error,
        )
        (directory / "evidence.json").write_text(
            json.dumps(evidence.to_data(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return evidence

    def _emit(
        self,
        event: str,
        obligation: VerificationObligation,
        *,
        evidence: VerificationEvidence | None = None,
    ) -> None:
        if self.progress is None:
            return
        payload: dict[str, Any] = {
            "event": event,
            "obligation_id": obligation.obligation_id,
            "contract_id": obligation.contract_id,
            "provider": obligation.provider,
            "selector": obligation.selector,
        }
        if evidence is not None:
            payload["status"] = evidence.status.value
        self.progress(payload)


def _status(value: str) -> EvidenceStatus:
    try:
        return EvidenceStatus(value)
    except ValueError:
        return EvidenceStatus.ERRORED


def _command(item: Mapping[str, Any] | None) -> tuple[str, ...]:
    if item is None:
        return ()
    value = item.get("command")
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    return tuple(str(part) for part in value)


def _protected_inputs_fingerprint(root: Path, patterns: Sequence[str]) -> str:
    files: list[tuple[str, str]] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if any(_matches(pattern, relative) for pattern in patterns):
            files.append((relative, hashlib.sha256(path.read_bytes()).hexdigest()))
    return _fingerprint(tuple(sorted(files)))


def _matches(pattern: str, path: str) -> bool:
    normalized = pattern.rstrip("/") or pattern
    return fnmatch.fnmatch(path, normalized) or fnmatch.fnmatch(
        path, normalized + "/**"
    )


def _fingerprint(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


__all__ = [
    "EvidenceStatus",
    "VERIFICATION_EVIDENCE_SCHEMA_VERSION",
    "VerificationEvidence",
    "VerificationEvidenceRunner",
]
