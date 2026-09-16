"""Deterministic validation execution for bounded coding-agent attempts."""

from __future__ import annotations

import shlex
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from powdrr_lift.workrr.coding_agent import (
    CodingAgentAttempt,
    CodingAgentStatus,
    ImplementationRequest,
)

CODING_AGENT_VALIDATION_REPORT_SCHEMA_VERSION = "coding-agent-validation-report-v1"


class ValidationResultStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    BLOCKED = "blocked"


class ValidationReportStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class ValidationProfile:
    name: str
    command: tuple[str, ...]

    def to_data(self) -> dict[str, Any]:
        return {"name": self.name, "command": list(self.command)}


@dataclass(frozen=True, slots=True)
class ValidationResult:
    profile: str
    command: tuple[str, ...]
    status: ValidationResultStatus
    returncode: int | None
    stdout: str = ""
    stderr: str = ""
    error: str | None = None

    def to_data(self) -> dict[str, Any]:
        return {
            "profile": self.profile,
            "command": list(self.command),
            "status": self.status.value,
            "returncode": self.returncode,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "error": self.error,
        }


@dataclass(frozen=True, slots=True)
class ValidationReport:
    attempt_id: str
    request_id: str
    status: ValidationReportStatus
    results: tuple[ValidationResult, ...]
    error: str | None = None
    schema_version: str = CODING_AGENT_VALIDATION_REPORT_SCHEMA_VERSION

    def to_data(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "attempt_id": self.attempt_id,
            "request_id": self.request_id,
            "status": self.status.value,
            "results": [result.to_data() for result in self.results],
            "error": self.error,
        }


class ValidationRunner:
    """Run the declared validation profiles as argv commands in a worktree."""

    def __init__(
        self,
        profiles: Mapping[str, ValidationProfile],
        *,
        timeout_seconds: float = 600.0,
    ) -> None:
        self.profiles = dict(profiles)
        self.timeout_seconds = timeout_seconds

    def run(
        self,
        request: ImplementationRequest,
        attempt: CodingAgentAttempt,
        *,
        worktree_root: Path,
    ) -> ValidationReport:
        if attempt.status is not CodingAgentStatus.COMPLETED:
            return ValidationReport(
                attempt_id=attempt.attempt_id,
                request_id=request.request_id,
                status=ValidationReportStatus.BLOCKED,
                results=tuple(
                    ValidationResult(
                        profile=name,
                        command=(),
                        status=ValidationResultStatus.BLOCKED,
                        returncode=None,
                        error=f"coding-agent attempt was {attempt.status.value}",
                    )
                    for name in request.validation_profiles
                ),
                error="validation is blocked until the coding-agent attempt completes",
            )

        profiles: list[ValidationProfile] = []
        missing: list[str] = []
        for name in request.validation_profiles:
            profile = self.profiles.get(name)
            if profile is None:
                missing.append(name)
            else:
                profiles.append(profile)
        if missing:
            return ValidationReport(
                attempt_id=attempt.attempt_id,
                request_id=request.request_id,
                status=ValidationReportStatus.BLOCKED,
                results=tuple(
                    ValidationResult(
                        profile=name,
                        command=(),
                        status=ValidationResultStatus.BLOCKED,
                        returncode=None,
                        error=f"validation profile is not registered: {name}",
                    )
                    for name in request.validation_profiles
                ),
                error="one or more validation profiles are not registered",
            )

        results = tuple(
            self._run_profile(profile, request, worktree_root) for profile in profiles
        )
        blocked = any(
            result.status is ValidationResultStatus.BLOCKED for result in results
        )
        failed = any(
            result.status
            in (ValidationResultStatus.FAILED, ValidationResultStatus.TIMED_OUT)
            for result in results
        )
        return ValidationReport(
            attempt_id=attempt.attempt_id,
            request_id=request.request_id,
            status=(
                ValidationReportStatus.BLOCKED
                if blocked
                else ValidationReportStatus.FAILED
                if failed
                else ValidationReportStatus.PASSED
            ),
            results=results,
        )

    def _run_profile(
        self,
        profile: ValidationProfile,
        request: ImplementationRequest,
        worktree_root: Path,
    ) -> ValidationResult:
        if not profile.command:
            return ValidationResult(
                profile=profile.name,
                command=(),
                status=ValidationResultStatus.BLOCKED,
                returncode=None,
                error="validation profile command must not be empty",
            )
        if not any(
            _command_matches(profile.command, allowed)
            for allowed in request.allowed_commands
        ):
            return ValidationResult(
                profile=profile.name,
                command=profile.command,
                status=ValidationResultStatus.BLOCKED,
                returncode=None,
                error="validation command is not allowed by the implementation request",
            )
        try:
            completed = subprocess.run(
                list(profile.command),
                cwd=worktree_root,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
            )
        except OSError as error:
            return ValidationResult(
                profile=profile.name,
                command=profile.command,
                status=ValidationResultStatus.FAILED,
                returncode=None,
                error=f"could not start validation command: {error}",
            )
        except subprocess.TimeoutExpired as error:
            return ValidationResult(
                profile=profile.name,
                command=profile.command,
                status=ValidationResultStatus.TIMED_OUT,
                returncode=124,
                stdout=_text(error.stdout),
                stderr=_text(error.stderr),
                error="validation command timed out",
            )
        return ValidationResult(
            profile=profile.name,
            command=profile.command,
            status=(
                ValidationResultStatus.PASSED
                if completed.returncode == 0
                else ValidationResultStatus.FAILED
            ),
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            error=None if completed.returncode == 0 else "validation command failed",
        )


def parse_validation_profile(value: str) -> ValidationProfile:
    """Parse the CLI form ``name=argv words`` without invoking a shell."""
    name, separator, command_text = value.partition("=")
    if not separator or not name.strip():
        raise ValueError("validation profile must use NAME=COMMAND syntax")
    command = tuple(shlex.split(command_text))
    if not command:
        raise ValueError("validation profile command must not be empty")
    return ValidationProfile(name.strip(), command)


def _command_matches(command: Sequence[str], allowed: str) -> bool:
    allowed_parts = tuple(shlex.split(allowed.removesuffix("*")))
    return bool(allowed_parts) and tuple(command[: len(allowed_parts)]) == allowed_parts


def _text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    return value.decode(errors="replace") if isinstance(value, bytes) else value


def validation_report_from_data(data: Mapping[str, Any]) -> ValidationReport:
    results = tuple(
        ValidationResult(
            profile=str(item["profile"]),
            command=tuple(item.get("command", [])),
            status=ValidationResultStatus(str(item["status"])),
            returncode=item.get("returncode"),
            stdout=str(item.get("stdout", "")),
            stderr=str(item.get("stderr", "")),
            error=item.get("error"),
        )
        for item in data.get("results", [])
    )
    return ValidationReport(
        attempt_id=str(data["attempt_id"]),
        request_id=str(data["request_id"]),
        status=ValidationReportStatus(str(data["status"])),
        results=results,
        error=data.get("error"),
        schema_version=str(data["schema_version"]),
    )


__all__ = [
    "CODING_AGENT_VALIDATION_REPORT_SCHEMA_VERSION",
    "ValidationProfile",
    "ValidationReport",
    "ValidationReportStatus",
    "ValidationResult",
    "ValidationResultStatus",
    "ValidationRunner",
    "parse_validation_profile",
    "validation_report_from_data",
]
