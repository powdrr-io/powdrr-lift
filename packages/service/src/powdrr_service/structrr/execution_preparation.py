"""Side-effect-free preparation for reviewed source-reference work."""

from __future__ import annotations

import json
import shlex
from collections.abc import Mapping, Sequence
from pathlib import PurePosixPath
from typing import Any

from powdrr_service.decision_obligation import content_fingerprint
from powdrr_service.structrr.reference_catalog import compile_reference_catalog
from powdrr_service.structrr.source_reference_handoff import (
    compile_source_reference_handoff,
)

PREPARATION_SCHEMA = "source-reference-preparation-v1"
REFERENCE_CHECK_COMMAND = (
    "python",
    "-m",
    "powdrr_agent.execution.source_reader",
    "check",
)
_SHELL_OPERATORS = frozenset({";", "&&", "||", "|", ">", ">>", "<", "2>", "&"})


def parse_command_spec(value: str) -> tuple[str, ...]:
    """Parse one argv command without permitting shell composition."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("verification command must be non-empty")
    try:
        command = tuple(shlex.split(value))
    except ValueError as error:
        raise ValueError(f"verification command is malformed: {error}") from error
    if not command or any(
        token in _SHELL_OPERATORS or any(char in token for char in "\n\r\x00")
        for token in command
    ):
        raise ValueError("verification command must be one direct argv command")
    return command


def normalize_allowed_paths(paths: Sequence[str]) -> tuple[str, ...]:
    """Validate and canonicalize the reviewed repository scope."""
    if isinstance(paths, (str, bytes)) or not paths:
        raise ValueError("allowed_paths must contain at least one repository path")
    normalized: set[str] = set()
    for path in paths:
        if not isinstance(path, str) or not path.strip() or path != path.strip():
            raise ValueError("allowed_paths contains an invalid path")
        parsed = PurePosixPath(path)
        if (
            parsed.is_absolute()
            or ".." in parsed.parts
            or parsed.as_posix() != path
            or "\\" in path
            or any(character in path for character in "*?[]\n\r\x00")
        ):
            raise ValueError(f"allowed path is not a safe repository path: {path!r}")
        normalized.add(path)
    return tuple(sorted(normalized))


def compile_execution_preparation(
    accepted_design: Mapping[str, Any],
    *,
    planning_base_commit: str,
    objective: str,
    allowed_paths: Sequence[str],
    verification_commands: Sequence[Sequence[str]],
    dependency_preconditions: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Compile the accepted design into a deterministic preparation contract.

    This function performs no repository I/O and creates no run artifacts. Both
    plan compilation and worker preparation use the same implementation.
    """
    if not isinstance(planning_base_commit, str) or len(planning_base_commit) not in {
        40,
        64,
    }:
        raise ValueError("planning base must be a full Git commit ID")
    if any(character not in "0123456789abcdef" for character in planning_base_commit):
        raise ValueError("planning base must be a lowercase hexadecimal Git commit ID")
    if not isinstance(objective, str) or not objective.strip():
        raise ValueError("execution objective must be non-empty")
    paths = normalize_allowed_paths(allowed_paths)
    commands: list[list[str]] = []
    for command in verification_commands:
        if (
            isinstance(command, (str, bytes))
            or not command
            or not all(isinstance(part, str) and part.strip() for part in command)
        ):
            raise ValueError("verification command specifications must be argv arrays")
        if any(
            part in _SHELL_OPERATORS or any(char in part for char in "\n\r\x00")
            for part in command
        ):
            raise ValueError("verification commands cannot contain shell operators")
        commands.append(list(command))
    if not commands:
        raise ValueError("at least one verification command is required")
    if len({json.dumps(command) for command in commands}) != len(commands):
        raise ValueError("verification command specifications must be unique")

    catalog = compile_reference_catalog(
        accepted_design,
        base_commit=planning_base_commit,
    )
    handoff = compile_source_reference_handoff(
        accepted_design,
        catalog,
        objective=objective,
        allowed_paths=paths,
        verification_commands=tuple(tuple(command) for command in commands),
        reference_check_command=REFERENCE_CHECK_COMMAND,
    )
    handoff_data = handoff.to_data()
    worker_contract = handoff_data["worker_prompt"]["contract"]
    private = handoff_data["validation_manifest"]["private_validation_data"]
    routes = {
        "behavior_scenarios": [
            item["scenario_id"] for item in worker_contract["behavior_scenarios"]
        ],
        "obligation_evidence_contracts": list(private["obligation_evidence_contracts"]),
    }
    accepted_contract = {
        key: value
        for key, value in worker_contract.items()
        if key not in {"base_commit", "catalog_digest"}
    }
    preparation = {
        "schema_version": PREPARATION_SCHEMA,
        "planning_base_commit": planning_base_commit,
        "accepted_design_fingerprint": content_fingerprint(accepted_design),
        "catalog": catalog.to_data(),
        "catalog_fingerprint": catalog.digest,
        "accepted_contract": accepted_contract,
        "command_specs": {
            "verification": commands,
            "reference_check": list(REFERENCE_CHECK_COMMAND),
        },
        "scope_policy": {"mode": "enforce", "allowed_paths": list(paths)},
        "required_evidence_routes": routes,
        "dependency_preconditions": [dict(item) for item in dependency_preconditions],
    }
    preparation["plan_revision_fingerprint"] = content_fingerprint(
        {
            "accepted_design_fingerprint": preparation["accepted_design_fingerprint"],
            "accepted_contract": accepted_contract,
            "command_specs": preparation["command_specs"],
            "scope_policy": preparation["scope_policy"],
            "dependency_preconditions": preparation["dependency_preconditions"],
        }
    )
    preparation["fingerprint"] = content_fingerprint(preparation)
    return preparation


def validate_execution_preparation(
    recorded: Mapping[str, Any],
    accepted_design: Mapping[str, Any],
    *,
    planning_base_commit: str,
    objective: str,
    allowed_paths: Sequence[str],
    verification_commands: Sequence[Sequence[str]],
    dependency_preconditions: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Recompile and compare the accepted part of a recorded plan contract."""
    compiled = compile_execution_preparation(
        accepted_design,
        planning_base_commit=planning_base_commit,
        objective=objective,
        allowed_paths=allowed_paths,
        verification_commands=verification_commands,
        dependency_preconditions=dependency_preconditions,
    )
    if dict(recorded) != compiled:
        raise ValueError(
            "execution preparation differs from the reviewed plan; a plan "
            "amendment is required"
        )
    return compiled


__all__ = [
    "PREPARATION_SCHEMA",
    "REFERENCE_CHECK_COMMAND",
    "compile_execution_preparation",
    "normalize_allowed_paths",
    "parse_command_spec",
    "validate_execution_preparation",
]
