"""Deterministic validation execution for bounded coding-agent attempts."""

from __future__ import annotations

import os
import shlex
import signal
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from powdrr_lift.workrr.coding_agent import (
    CodingAgentAttempt,
    CodingAgentRunner,
    CodingAgentStatus,
    ImplementationRequest,
)
from powdrr_lift.workrr.source_reference_validation import (
    SourceReferenceValidationContext,
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
    test_id: str | None = None
    expected: str | None = None
    actual: str | None = None
    source_location: str | None = None
    evidence_path: str | None = None

    def to_data(self) -> dict[str, Any]:
        return {
            "profile": self.profile,
            "command": list(self.command),
            "status": self.status.value,
            "returncode": self.returncode,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "error": self.error,
            "test_id": self.test_id,
            "expected": self.expected,
            "actual": self.actual,
            "source_location": self.source_location,
            "evidence_path": self.evidence_path,
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
        failures = [
            normalize_failure(
                failure_id=f"local-validation:{result.profile}",
                stage="local_validation",
                test_id=result.test_id or result.profile,
                command=result.command,
                expected=result.expected or "validation command exits successfully",
                actual=result.actual or result.error or result.status.value,
                source_location=result.source_location,
                evidence_path=result.evidence_path,
                classification=(
                    "environment_failure"
                    if result.status is ValidationResultStatus.BLOCKED
                    else "test_failure"
                ),
                repairability=(
                    "blocked"
                    if result.status
                    in (
                        ValidationResultStatus.BLOCKED,
                        ValidationResultStatus.TIMED_OUT,
                    )
                    else "repairable"
                ),
            )
            for result in self.results
            if result.status is not ValidationResultStatus.PASSED
        ]
        return {
            "schema_version": self.schema_version,
            "attempt_id": self.attempt_id,
            "request_id": self.request_id,
            "status": self.status.value,
            "results": [result.to_data() for result in self.results],
            "error": self.error,
            "failures": failures,
        }


@dataclass(frozen=True, slots=True)
class CodingAgentExecutionResult:
    """One shared coding, local validation, and whitespace-check execution."""

    attempt: CodingAgentAttempt
    validation: ValidationReport
    diff_check: subprocess.CompletedProcess[str]


def execute_coding_agent_attempt(
    runner: CodingAgentRunner,
    request: ImplementationRequest,
    *,
    worktree_root: Path,
    attempt_id: str,
    profiles: Mapping[str, ValidationProfile],
    validation_timeout_seconds: float,
    reference_context: SourceReferenceValidationContext | None = None,
) -> CodingAgentExecutionResult:
    """Run one agent attempt and its common local validation gates."""
    attempt = runner.run(request, worktree_root=worktree_root, attempt_id=attempt_id)
    execution = (
        getattr(runner.provider, "source_reference_execution", None)
        if request.source_reference_prompt is not None
        else None
    )
    if execution is not None:
        execution.finalize_worker(attempt)
        reconcile_after_worker = getattr(execution, "reconcile_after_worker", None)
        if callable(reconcile_after_worker):
            reconcile_after_worker()
        if (
            reference_context is not None
            and reference_context is not execution.validation_context
        ):
            raise ValueError("Validation context differs from the prepared source run")
        reference_context = execution.validation_context
    validation = ValidationRunner(
        profiles,
        timeout_seconds=validation_timeout_seconds,
        reference_context=reference_context,
        command_wrapper=execution.protect_command if execution else None,
        environment_overrides=execution.validation_environment() if execution else None,
        reference_claims=execution.claims if execution else (),
    ).run(request, attempt, worktree_root=worktree_root)
    runner.store.save_validation_report(validation)
    diff_check = subprocess.run(
        ["git", "diff", "--check"],
        cwd=worktree_root,
        capture_output=True,
        text=True,
        check=False,
    )
    return CodingAgentExecutionResult(attempt, validation, diff_check)


def normalize_failure(
    *,
    failure_id: str,
    stage: str,
    test_id: str,
    command: Sequence[str] = (),
    contract_refs: Sequence[str] = (),
    expected: Any = None,
    actual: Any = None,
    source_location: str | None = None,
    evidence_path: str | None = None,
    classification: str = "product_failure",
    repairability: str = "repairable",
) -> dict[str, Any]:
    """Normalize local, verifier, scope, and policy failures for repair handoff."""
    if stage not in {"local_validation", "verifier", "scope", "policy"}:
        raise ValueError(f"unsupported validation failure stage: {stage}")
    if classification not in {
        "product_failure",
        "test_failure",
        "environment_failure",
        "policy_failure",
    }:
        raise ValueError(f"unsupported failure classification: {classification}")
    if repairability not in {"repairable", "blocked", "human_required"}:
        raise ValueError(f"unsupported failure repairability: {repairability}")
    return {
        "failure_id": failure_id,
        "stage": stage,
        "test_id": test_id,
        "command": list(command),
        "contract_refs": list(contract_refs),
        "expected": expected,
        "actual": actual,
        "source_location": source_location,
        "evidence_path": evidence_path,
        "classification": classification,
        "repairability": repairability,
    }


class ValidationRunner:
    """Run the declared validation profiles as argv commands in a worktree."""

    def __init__(
        self,
        profiles: Mapping[str, ValidationProfile],
        *,
        timeout_seconds: float = 600.0,
        reference_context: SourceReferenceValidationContext | None = None,
        command_wrapper: Callable[[tuple[str, ...]], Sequence[str]] | None = None,
        environment_overrides: Mapping[str, str] | None = None,
        reference_claims: tuple[dict[str, Any], ...] = (),
    ) -> None:
        self.profiles = dict(profiles)
        self.timeout_seconds = timeout_seconds
        self.reference_context = reference_context
        self.command_wrapper = command_wrapper
        self.environment_overrides = dict(environment_overrides or {})
        self.reference_claims = reference_claims

    def run(
        self,
        request: ImplementationRequest,
        attempt: CodingAgentAttempt,
        *,
        worktree_root: Path,
    ) -> ValidationReport:
        reference_required = (
            request.source_reference_prompt is not None
            or "structrr-references" in request.validation_profiles
        )
        names = (
            request.validation_profiles
            if not reference_required
            else tuple(
                dict.fromkeys(
                    (
                        *request.validation_profiles,
                        *(("structrr-references",) if reference_required else ()),
                    )
                )
            )
        )
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
                    for name in names
                ),
                error="validation is blocked until the coding-agent attempt completes",
            )

        profiles: list[ValidationProfile] = []
        missing: list[str] = []
        for name in request.validation_profiles:
            if name == "structrr-references":
                continue
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
                    for name in names
                ),
                error="one or more validation profiles are not registered",
            )

        # Candidate test programs can mutate files. Source evidence must describe
        # the final bytes after every ordinary verification command has finished.
        ordinary_results = tuple(
            self._run_profile(profile, request, worktree_root) for profile in profiles
        )
        reference_results: list[ValidationResult] = []
        if reference_required:
            reference_results.append(self._run_references(request, worktree_root))
            if not profiles:
                reference_results.append(
                    ValidationResult(
                        profile="behavioral-evidence",
                        command=(),
                        status=ValidationResultStatus.BLOCKED,
                        returncode=None,
                        error="Reference evidence cannot replace behavioral profiles.",
                    )
                )
        results = (
            *reference_results,
            *ordinary_results,
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

    def _run_references(
        self, request: ImplementationRequest, worktree_root: Path
    ) -> ValidationResult:
        import json

        try:
            if (
                self.reference_context is None
                or request.source_reference_prompt is None
            ):
                raise ValueError(
                    "Frozen reference context and public prompt are required."
                )
            self.reference_context.handoff.require_matches(
                request.source_reference_prompt
            )
            report = self.reference_context.check(
                worktree_root, claims=self.reference_claims
            )
            status = {
                "passed": ValidationResultStatus.PASSED,
                "failed": ValidationResultStatus.FAILED,
                "needs_review": ValidationResultStatus.BLOCKED,
            }[report.status]
            return ValidationResult(
                profile="structrr-references",
                command=(),
                status=status,
                returncode=0 if report.status == "passed" else 1,
                stdout=json.dumps(report.to_data()),
                expected=(
                    "Reference placements and preservation pass; "
                    "behavior is checked separately."
                ),
                actual=report.status,
            )
        except (ValueError, OSError, RuntimeError) as error:
            return ValidationResult(
                profile="structrr-references",
                command=(),
                status=ValidationResultStatus.BLOCKED,
                returncode=None,
                error=str(error),
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
        process: subprocess.Popen[str] | None = None
        try:
            environment = os.environ.copy()
            # Validation runs in the worker worktree. Ignore any activated
            # environment belonging to the caller's checkout so uv resolves
            # the project from this worktree.
            environment.pop("VIRTUAL_ENV", None)
            if request.source_reference_prompt is not None:
                from powdrr_lift.workrr.coding_agent import _remove_provider_credentials

                _remove_provider_credentials(environment)
                for key in tuple(environment):
                    if key.startswith("POWDRR_STRUCTRR_"):
                        environment.pop(key)
            environment.update(self.environment_overrides)
            process = subprocess.Popen(
                list(
                    self.command_wrapper(profile.command)
                    if self.command_wrapper
                    else profile.command
                ),
                cwd=worktree_root,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
            stdout, stderr = process.communicate(timeout=self.timeout_seconds)
        except OSError as error:
            if process is not None:
                _stop_process_group(process)
            return ValidationResult(
                profile=profile.name,
                command=profile.command,
                status=ValidationResultStatus.FAILED,
                returncode=None,
                error=f"could not start validation command: {error}",
            )
        except subprocess.TimeoutExpired as error:
            assert process is not None
            _stop_process_group(process)
            stdout, stderr = process.communicate()
            return ValidationResult(
                profile=profile.name,
                command=profile.command,
                status=ValidationResultStatus.TIMED_OUT,
                returncode=124,
                stdout=_text(stdout) or _text(error.stdout),
                stderr=_text(stderr) or _text(error.stderr),
                error="validation command timed out",
            )
        except BaseException:
            if process is not None:
                _stop_process_group(process)
            raise
        assert process is not None
        completed = subprocess.CompletedProcess(
            profile.command, process.returncode, stdout, stderr
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
            actual=(
                None
                if completed.returncode == 0
                else "\n".join(
                    part for part in (completed.stdout, completed.stderr) if part
                )
                or "command exited with status " + str(completed.returncode)
            ),
        )


def _stop_process_group(process: subprocess.Popen[str]) -> None:
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        elif process.poll() is None:
            process.kill()
    except ProcessLookupError:
        pass
    process.wait()


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
            test_id=item.get("test_id"),
            expected=item.get("expected"),
            actual=item.get("actual"),
            source_location=item.get("source_location"),
            evidence_path=item.get("evidence_path"),
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
