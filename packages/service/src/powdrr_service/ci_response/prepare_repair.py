"""Prepare only repairs with a reproducible, exact-path failure target."""

from __future__ import annotations

import hashlib
import shlex
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from powdrr_contracts.ci import (
    CIFailureUnit,
    CIObservation,
    LocalReproduction,
    ProtectedArtifact,
    RegisteredCIFix,
    RepairContract,
)

from powdrr_service.ci_response.repair_contract import compile_repair_contract


def prepare_ci_repair_contract(
    *,
    observation: CIObservation,
    failure: CIFailureUnit,
    reproduction: LocalReproduction,
    changed_paths: Sequence[str],
    failure_log_excerpt: str,
    reviewed_context: Mapping[str, Any],
    protected_artifacts: Sequence[ProtectedArtifact],
    now: datetime | None = None,
) -> RepairContract:
    """Compile a contract only for one reproduced test failure at one changed path.

    Repository paths are selected by matching the captured failing-job log to
    the immutable PR file list and intersecting that exact file with the
    reviewed plan allowlist. Ambiguous or non-local failures must be escalated
    for human review instead of receiving a broad edit scope.
    """
    if reproduction.status != "reproduced" or not failure.locally_executable:
        raise ValueError("CI repair requires a locally reproduced failure")
    registered_correction = _registered_ruff_fix(
        failure, reproduction, failure_log_excerpt
    )
    if failure.test_node_id is None and registered_correction is None:
        raise ValueError(
            "CI repair requires a pytest failure or an exact registered correction"
        )
    if observation.head_sha != failure.head_sha:
        raise ValueError("CI failure belongs to a different observed PR head")
    if observation.required_checks_status != "available":
        raise ValueError("CI repair requires complete required-check policy")

    raw_allowlist = reviewed_context.get("allowed_paths")
    if (
        not isinstance(raw_allowlist, list)
        or not raw_allowlist
        or not all(isinstance(path, str) and path.strip() for path in raw_allowlist)
    ):
        raise ValueError("reviewed plan must provide an explicit allowed_paths list")
    allowlist = tuple(path.strip().rstrip("/") or "." for path in raw_allowlist)
    candidates = tuple(
        path
        for path in dict.fromkeys(changed_paths)
        if _safe_repo_path(path)
        and not _is_test_path(path)
        and any(_within_reviewed_scope(path, scope) for scope in allowlist)
        and path in failure_log_excerpt
    )
    if len(candidates) != 1:
        raise ValueError(
            "failed-job evidence does not identify exactly one changed, reviewed "
            "source path"
        )

    if failure.test_node_id is not None:
        test_path = failure.test_node_id.split("::", 1)[0]
        if not _safe_repo_path(test_path) or not _is_test_path(test_path):
            raise ValueError(
                "CI failure does not identify a safe test artifact to protect"
            )
        if not any(item.path == test_path for item in protected_artifacts):
            raise ValueError(
                "the failing test file is not protected by the repair contract"
            )

    acceptance = reviewed_context.get("acceptance_criteria")
    if (
        not isinstance(acceptance, list)
        or not acceptance
        or not all(isinstance(item, str) and item.strip() for item in acceptance)
    ):
        raise ValueError("reviewed plan must provide acceptance criteria")
    verification_command = reviewed_context.get("verification_command")
    if not isinstance(verification_command, str) or not verification_command.strip():
        raise ValueError("reviewed plan must provide a verification command")
    try:
        declared_command = tuple(shlex.split(verification_command))
    except ValueError as error:
        raise ValueError("reviewed verification command is malformed") from error
    commands = tuple(
        dict.fromkeys(
            command
            for command in (
                declared_command,
                reproduction.command,
                registered_correction.check_command
                if registered_correction is not None
                else (),
            )
            if command
        )
    )
    if not commands:
        raise ValueError("repair contract requires verification commands")

    expiry = (now or datetime.now(UTC)) + timedelta(hours=1)
    return compile_repair_contract(
        observation,
        failure,
        reproduction,
        allowed_paths=candidates,
        protected_artifacts=protected_artifacts,
        required_behavior=tuple(item.strip() for item in acceptance),
        verification_commands=commands,
        required_checks=tuple(item.name for item in observation.required_checks),
        stop_conditions=(
            "Stop if the candidate requires a path outside the single authorized "
            "source file.",
            "Stop if changing the protected test or reducing its coverage appears "
            "necessary.",
            "Stop if the candidate head changes or required-check policy is "
            "unavailable.",
        ),
        expires_at=expiry.isoformat(),
        registered_correction=registered_correction,
    )


def _registered_ruff_fix(
    failure: CIFailureUnit,
    reproduction: LocalReproduction,
    failure_log_excerpt: str,
) -> RegisteredCIFix | None:
    if (
        failure.phase != "formatter_failure"
        or failure.test_node_id is not None
        or not failure.command
        or reproduction.status != "reproduced"
        or reproduction.matching_failure is not True
        or reproduction.exit_code != 1
        or reproduction.command != failure.command
    ):
        return None
    prefixes = (
        ("ruff", "format", "--check"),
        ("python", "-m", "ruff", "format", "--check"),
    )
    prefix = next(
        (item for item in prefixes if failure.command[: len(item)] == item), None
    )
    if prefix is None or len(failure.command) != len(prefix) + 1:
        return None
    target = failure.command[-1]
    if not _safe_repo_path(target) or not target.endswith(".py"):
        return None
    lines = [line.strip() for line in failure_log_excerpt.splitlines() if line.strip()]
    diagnostics = [line for line in lines if line.startswith("Would reformat: ")]
    summaries = [line for line in lines if line.endswith("file would be reformatted")]
    if diagnostics != [f"Would reformat: {target}"] or summaries != [
        "1 file would be reformatted"
    ]:
        return None
    return RegisteredCIFix(
        correction_id="ruff-format-v1",
        path=target,
        check_command=failure.command,
        diagnostic_sha256=hashlib.sha256(
            failure_log_excerpt.encode("utf-8")
        ).hexdigest(),
    )


def _safe_repo_path(value: str) -> bool:
    normalized = value.replace("\\", "/")
    return bool(
        normalized
        and not normalized.startswith("/")
        and "\\" not in value
        and ".." not in normalized.split("/")
        and all(ord(character) >= 32 for character in normalized)
    )


def _is_test_path(value: str) -> bool:
    parts = value.replace("\\", "/").split("/")
    return any(part == "tests" or part.startswith("test_") for part in parts) or (
        parts[-1].startswith("test_") and parts[-1].endswith(".py")
    )


def _within_reviewed_scope(path: str, scope: str) -> bool:
    return scope == "." or path == scope or path.startswith(scope + "/")


__all__ = ["prepare_ci_repair_contract"]
