"""Deterministic acceptance policy for contract-bound CI corrections."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Literal

from powdrr_contracts.ci import ProtectedArtifact, RepairContract


@dataclass(frozen=True, slots=True)
class CIRepairValidation:
    """One validation command result bound to the candidate diff fingerprint."""

    command: tuple[str, ...]
    status: Literal["passed", "failed", "incomplete"]
    diff_fingerprint: str | None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.command, tuple)
            or not self.command
            or any(not isinstance(part, str) or not part for part in self.command)
        ):
            raise ValueError("repair validation command must be a non-empty argv")
        if self.status not in {"passed", "failed", "incomplete"}:
            raise ValueError("repair validation status is unsupported")
        if self.diff_fingerprint is not None and not _is_sha256(self.diff_fingerprint):
            raise ValueError("repair validation diff fingerprint must be SHA-256")


@dataclass(frozen=True, slots=True)
class CIRepairAcceptance:
    """Deterministic service decision for one candidate correction."""

    status: Literal["accepted", "rejected"]
    contract_id: str
    reasons: tuple[str, ...]
    changed_paths: tuple[str, ...]
    validated_commands: tuple[tuple[str, ...], ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": "ci-repair-acceptance-v1",
            "status": self.status,
            "contract_id": self.contract_id,
            "reasons": list(self.reasons),
            "changed_paths": list(self.changed_paths),
            "validated_commands": [
                list(command) for command in self.validated_commands
            ],
        }


def assess_ci_repair_candidate(
    contract: RepairContract,
    *,
    base_sha: str,
    worker_status: str,
    changed_paths: Sequence[str],
    protected_artifacts: Sequence[ProtectedArtifact],
    diff_fingerprint: str | None,
    validations: Sequence[CIRepairValidation],
    now: datetime | None = None,
) -> CIRepairAcceptance:
    """Accept only a fresh, in-scope candidate with intact tests and receipts.

    Validation receipts must refer to the exact worker diff. This function does
    not infer that GitHub checks passed; publication and current-head observation
    remain later response steps.
    """
    validated_contract = RepairContract.from_dict(contract.to_dict())
    current_time = now or datetime.now(UTC)
    if current_time.tzinfo is None or current_time.utcoffset() is None:
        raise ValueError("repair acceptance time must be timezone-aware")

    reasons: list[str] = []
    if current_time >= _contract_expiry(validated_contract):
        reasons.append("repair contract has expired")
    if base_sha != validated_contract.head_sha:
        reasons.append("candidate base does not match the repair contract head")
    if worker_status != "completed":
        reasons.append("coding worker did not complete successfully")

    paths = tuple(changed_paths)
    if not paths:
        reasons.append("candidate contains no changed paths")
    elif len(paths) != len(set(paths)):
        reasons.append("candidate changed paths contain duplicates")
    elif any(not _valid_repository_path(path) for path in paths):
        reasons.append("candidate changed paths contain an invalid repository path")
    else:
        out_of_scope = tuple(
            path
            for path in paths
            if not any(
                _path_is_within(path, scope)
                for scope in validated_contract.allowed_paths
            )
        )
        if out_of_scope:
            reasons.append("candidate changes paths outside the repair contract")
        protected = {item.path for item in validated_contract.protected_artifacts}
        if protected.intersection(paths):
            reasons.append("candidate changes a protected artifact")

    expected_artifacts = {
        artifact.path: artifact.sha256
        for artifact in validated_contract.protected_artifacts
    }
    observed_artifacts = {
        artifact.path: artifact.sha256 for artifact in protected_artifacts
    }
    if len(observed_artifacts) != len(protected_artifacts):
        reasons.append("protected artifact evidence contains duplicate paths")
    elif observed_artifacts != expected_artifacts:
        reasons.append("protected artifact hashes do not match the contract")

    if not _is_sha256(diff_fingerprint):
        reasons.append("candidate diff fingerprint is missing")

    expected_commands = set(validated_contract.verification_commands)
    validation_by_command: dict[tuple[str, ...], CIRepairValidation] = {}
    for validation in validations:
        if validation.command in validation_by_command:
            reasons.append("validation evidence contains duplicate commands")
            continue
        validation_by_command[validation.command] = validation
        if validation.command not in expected_commands:
            reasons.append("validation evidence contains an uncontracted command")
        if validation.status != "passed":
            reasons.append("a required repair validation did not pass")
        if validation.diff_fingerprint != diff_fingerprint:
            reasons.append("validation evidence does not match the candidate diff")

    missing_commands = expected_commands - validation_by_command.keys()
    if missing_commands:
        reasons.append("required repair validations are missing")

    unique_reasons = tuple(dict.fromkeys(reasons))
    return CIRepairAcceptance(
        status="rejected" if unique_reasons else "accepted",
        contract_id=validated_contract.contract_id,
        reasons=unique_reasons,
        changed_paths=tuple(sorted(paths)),
        validated_commands=tuple(sorted(validation_by_command)),
    )


def _contract_expiry(contract: RepairContract) -> datetime:
    expiry = datetime.fromisoformat(contract.expires_at.replace("Z", "+00:00"))
    if expiry.tzinfo is None or expiry.utcoffset() is None:
        raise ValueError("repair contract expiry must be timezone-aware")
    return expiry.astimezone(UTC)


def _valid_repository_path(path: str) -> bool:
    if (
        not isinstance(path, str)
        or not path
        or "\\" in path
        or ":" in path
        or any(ord(char) < 32 for char in path)
        or any(char in path for char in "*?[]")
    ):
        return False
    candidate = PurePosixPath(path)
    return (
        not candidate.is_absolute()
        and path not in {".", ".."}
        and all(part not in {"", ".", ".."} for part in path.split("/"))
    )


def _path_is_within(path: str, scope: str) -> bool:
    return scope == "." or path == scope or path.startswith(scope.rstrip("/") + "/")


def _is_sha256(value: str | None) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


__all__ = [
    "CIRepairAcceptance",
    "CIRepairValidation",
    "assess_ci_repair_candidate",
]
