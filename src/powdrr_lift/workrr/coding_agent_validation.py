"""Deterministic validation execution for bounded coding-agent attempts."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import platform
import re
import shlex
import signal
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

from powdrr_lift.workrr.coding_agent import (
    CodingAgentAttempt,
    CodingAgentRunner,
    CodingAgentStatus,
    ImplementationRequest,
    _working_paths,
)
from powdrr_lift.workrr.source_reference_validation import (
    SourceReferenceValidationContext,
)

CODING_AGENT_VALIDATION_REPORT_SCHEMA_VERSION = "coding-agent-validation-report-v1"
VALIDATION_RECEIPT_SCHEMA_VERSION = "coding-agent-validation-receipts-v1"
_VALIDATION_RECEIPT_LIMIT = 128
_UNKNOWN_CONDITION_VALUE = object()
_CONDITION_TOKEN = re.compile(
    r"\s*(?:(?P<operator>&&|\|\||==|!=|!|\(|\))"
    r"|(?P<string>'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\")"
    r"|(?P<literal>true|false|null)\b"
    r"|(?P<identifier>[A-Za-z_][A-Za-z0-9_.]*))"
)


class ValidationResultStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    BLOCKED = "blocked"
    NOT_APPLICABLE = "not_applicable"
    PENDING = "pending"


class ValidationReportStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    BLOCKED = "blocked"
    PENDING = "pending"


class ValidationApplicabilityStatus(StrEnum):
    APPLICABLE = "applicable"
    NOT_APPLICABLE = "not_applicable"
    PENDING = "pending"


@dataclass(frozen=True, slots=True)
class ValidationProfile:
    name: str
    command: tuple[str, ...]
    source: str = ""
    provider: str | None = None
    cwd: str = "."
    environment_id: str | None = None
    component: str | None = None
    execution_kind: str = "argv"
    shell: str | None = None
    script: str | None = None
    applicability: Mapping[str, Any] | None = None
    selectors: tuple[str, ...] = ()
    config_files: tuple[str, ...] = ()
    settings: Mapping[str, Any] | None = None
    purpose: str = ""
    roles: tuple[str, ...] = ()
    requiredness: Mapping[str, Any] | None = None
    declaration: str = "unknown"
    evidence: tuple[str, ...] = ()
    confirmation: Mapping[str, Any] | None = None
    depends_on: tuple[str, ...] = ()
    local_reproducibility: str = "unknown"
    baseline: Mapping[str, Any] | None = None
    unresolved: tuple[str, ...] = ()

    def to_data(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "command": list(self.command),
            "source": self.source,
            "provider": self.provider,
            "cwd": self.cwd,
            "environment_id": self.environment_id,
            "component": self.component,
            "execution_kind": self.execution_kind,
            "shell": self.shell,
            "script": self.script,
            "applicability": (
                dict(self.applicability) if self.applicability is not None else None
            ),
            "selectors": list(self.selectors),
            "config_files": list(self.config_files),
            "settings": dict(self.settings) if self.settings is not None else None,
            "purpose": self.purpose,
            "roles": list(self.roles),
            "requiredness": (
                dict(self.requiredness) if self.requiredness is not None else None
            ),
            "declaration": self.declaration,
            "evidence": list(self.evidence),
            "confirmation": (
                dict(self.confirmation) if self.confirmation is not None else None
            ),
            "depends_on": list(self.depends_on),
            "local_reproducibility": self.local_reproducibility,
            "baseline": dict(self.baseline) if self.baseline is not None else None,
            "unresolved": list(self.unresolved),
        }

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> ValidationProfile:
        name = data.get("name")
        command = data.get("command")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("validation profile name must be non-empty")
        if not isinstance(command, list) or not all(
            isinstance(part, str) and part for part in command
        ):
            raise ValueError("validation profile command must be an argv list")

        def strings(key: str) -> tuple[str, ...]:
            value = data.get(key, [])
            if not isinstance(value, list) or not all(
                isinstance(item, str) for item in value
            ):
                raise ValueError(f"validation profile {key} must be a string list")
            return tuple(value)

        def mapping(key: str) -> Mapping[str, Any] | None:
            value = data.get(key)
            if value is None:
                return None
            if not isinstance(value, Mapping):
                raise ValueError(f"validation profile {key} must be a mapping")
            return dict(value)

        return cls(
            name=name,
            command=tuple(command),
            source=str(data.get("source", "")),
            provider=(
                data.get("provider") if isinstance(data.get("provider"), str) else None
            ),
            cwd=str(data.get("cwd", ".")),
            environment_id=(
                data.get("environment_id")
                if isinstance(data.get("environment_id"), str)
                else None
            ),
            component=(
                data.get("component")
                if isinstance(data.get("component"), str)
                else None
            ),
            execution_kind=str(data.get("execution_kind", "argv")),
            shell=data.get("shell") if isinstance(data.get("shell"), str) else None,
            script=data.get("script") if isinstance(data.get("script"), str) else None,
            applicability=mapping("applicability"),
            selectors=strings("selectors"),
            config_files=strings("config_files"),
            settings=mapping("settings"),
            purpose=str(data.get("purpose", "")),
            roles=strings("roles"),
            requiredness=mapping("requiredness"),
            declaration=str(data.get("declaration", "unknown")),
            evidence=strings("evidence"),
            confirmation=mapping("confirmation"),
            depends_on=strings("depends_on"),
            local_reproducibility=str(data.get("local_reproducibility", "unknown")),
            baseline=mapping("baseline"),
            unresolved=strings("unresolved"),
        )


@dataclass(frozen=True, slots=True)
class ValidationApplicabilityContext:
    """Actual target context used to evaluate a profile's event filters."""

    event: str | None = None
    base_branch: str | None = None
    changed_paths: tuple[str, ...] | None = None

    def to_data(self) -> dict[str, Any]:
        return {
            "event": self.event,
            "base_branch": self.base_branch,
            "changed_paths": (
                list(self.changed_paths) if self.changed_paths is not None else None
            ),
        }


def evaluate_validation_applicability(
    profile: ValidationProfile,
    context: ValidationApplicabilityContext,
) -> tuple[ValidationApplicabilityStatus, str]:
    """Evaluate supported event, base-branch, and changed-path constraints."""
    applicability = profile.applicability
    if applicability is None:
        return (
            ValidationApplicabilityStatus.APPLICABLE,
            "no applicability filters declared",
        )

    evaluation = applicability.get("evaluation", "unknown")
    if evaluation == "not_applicable":
        return ValidationApplicabilityStatus.NOT_APPLICABLE, (
            "profile is declared not applicable"
        )
    condition = applicability.get("condition")
    condition_matched = False
    if condition not in (None, ""):
        condition_result = _evaluate_github_condition(condition, context)
        if condition_result is False:
            return ValidationApplicabilityStatus.NOT_APPLICABLE, (
                "applicability condition evaluated false"
            )
        if condition_result is None:
            return ValidationApplicabilityStatus.PENDING, (
                "applicability condition needs unsupported syntax "
                "or unavailable context"
            )
        condition_matched = True

    local = applicability.get("local")
    if local is False:
        return ValidationApplicabilityStatus.PENDING, (
            "profile is not reproducible in the local validation runner"
        )
    if local is not None and not isinstance(local, bool):
        return ValidationApplicabilityStatus.PENDING, (
            "local applicability declaration is malformed"
        )
    if evaluation == "applicable" and not any(
        key in applicability
        for key in ("events", "base_branches", "paths", "condition")
    ):
        return ValidationApplicabilityStatus.APPLICABLE, (
            "profile is declared applicable"
        )

    constraints: list[str] = []
    if condition_matched:
        constraints.append("condition matched")
    for key, actual in (
        ("events", context.event),
        ("base_branches", context.base_branch),
    ):
        declared = applicability.get(key)
        if declared is None:
            continue
        if (
            not isinstance(declared, (list, tuple))
            or not declared
            or not all(isinstance(item, str) and item for item in declared)
        ):
            return ValidationApplicabilityStatus.PENDING, (
                f"{key} applicability filter is malformed"
            )
        if actual is None:
            return ValidationApplicabilityStatus.PENDING, (
                f"actual {key} is unavailable"
            )
        if actual not in declared:
            return ValidationApplicabilityStatus.NOT_APPLICABLE, (
                f"actual {key} {actual!r} is outside the declared filter"
            )
        constraints.append(f"{key} matched")

    declared_paths = applicability.get("paths")
    if declared_paths is not None:
        if (
            not isinstance(declared_paths, (list, tuple))
            or not declared_paths
            or not all(isinstance(item, str) and item for item in declared_paths)
        ):
            return ValidationApplicabilityStatus.PENDING, (
                "paths applicability filter is malformed"
            )
        if any(pattern == "!" for pattern in declared_paths) or not any(
            not pattern.startswith("!") for pattern in declared_paths
        ):
            return ValidationApplicabilityStatus.PENDING, (
                "paths applicability filter must contain a positive pattern"
            )
        if context.changed_paths is None:
            return ValidationApplicabilityStatus.PENDING, (
                "actual changed paths are unavailable"
            )

        def path_is_included(path: str) -> bool:
            included = False
            for pattern in declared_paths:
                excluded = pattern.startswith("!")
                glob = pattern[1:] if excluded else pattern
                if fnmatchcase(path, glob):
                    included = not excluded
            return included

        if not any(path_is_included(path) for path in context.changed_paths):
            return ValidationApplicabilityStatus.NOT_APPLICABLE, (
                "no changed path remains included by the declared path filter"
            )
        constraints.append("changed paths matched")

    if constraints:
        return ValidationApplicabilityStatus.APPLICABLE, "; ".join(constraints)
    if local is True or evaluation == "applicable":
        return ValidationApplicabilityStatus.APPLICABLE, (
            "profile applies to local validation"
        )
    return ValidationApplicabilityStatus.PENDING, ("profile applicability is unknown")


def _evaluate_github_condition(
    condition: object,
    context: ValidationApplicabilityContext,
) -> bool | None:
    """Evaluate the deterministic GitHub expression subset supported locally."""
    if not isinstance(condition, str) or len(condition) > 1024:
        return None
    expression = condition.strip()
    if expression.startswith("${{") and expression.endswith("}}"):
        expression = expression[3:-2].strip()
    if not expression:
        return None
    tokens: list[tuple[str, str]] = []
    offset = 0
    while offset < len(expression):
        match = _CONDITION_TOKEN.match(expression, offset)
        if match is None:
            return None
        kind = next(
            name for name, value in match.groupdict().items() if value is not None
        )
        tokens.append((kind, match.group(kind)))
        offset = match.end()

    class _Parser:
        def __init__(self) -> None:
            self.index = 0

        def peek(self, value: str | None = None) -> bool:
            if self.index >= len(tokens):
                return False
            return value is None or tokens[self.index][1] == value

        def take(self, value: str | None = None) -> tuple[str, str] | None:
            if not self.peek(value):
                return None
            token = tokens[self.index]
            self.index += 1
            return token

        def parse(self) -> object:
            result = self.parse_or()
            return result if self.index == len(tokens) else _UNKNOWN_CONDITION_VALUE

        def parse_or(self) -> object:
            result = self.parse_and()
            while self.take("||"):
                right = self.parse_and()
                result = _condition_or(result, right)
            return result

        def parse_and(self) -> object:
            result = self.parse_unary()
            while self.take("&&"):
                right = self.parse_unary()
                result = _condition_and(result, right)
            return result

        def parse_unary(self) -> object:
            if self.take("!"):
                value = self.parse_unary()
                return (
                    _UNKNOWN_CONDITION_VALUE
                    if value is _UNKNOWN_CONDITION_VALUE
                    else not value
                )
            return self.parse_comparison()

        def parse_comparison(self) -> object:
            left = self.parse_primary()
            operator = self.take("==") or self.take("!=")
            if operator is None:
                return left
            right = self.parse_primary()
            if left is _UNKNOWN_CONDITION_VALUE or right is _UNKNOWN_CONDITION_VALUE:
                return _UNKNOWN_CONDITION_VALUE
            equal = type(left) is type(right) and left == right
            return equal if operator[1] == "==" else not equal

        def parse_primary(self) -> object:
            if self.take("("):
                value = self.parse_or()
                if not self.take(")"):
                    return _UNKNOWN_CONDITION_VALUE
                return value
            token = self.take()
            if token is None:
                return _UNKNOWN_CONDITION_VALUE
            kind, value = token
            if kind == "string":
                try:
                    parsed = ast.literal_eval(value)
                except (SyntaxError, ValueError):
                    return _UNKNOWN_CONDITION_VALUE
                return parsed if isinstance(parsed, str) else _UNKNOWN_CONDITION_VALUE
            if kind == "literal":
                return {"true": True, "false": False, "null": None}[value]
            if kind == "identifier":
                known_values: dict[str, object] = {
                    "github.event_name": context.event
                    if context.event is not None
                    else _UNKNOWN_CONDITION_VALUE,
                    "github.base_ref": context.base_branch
                    if context.base_branch is not None
                    else _UNKNOWN_CONDITION_VALUE,
                    "github.event.pull_request.base.ref": context.base_branch
                    if context.base_branch is not None
                    else _UNKNOWN_CONDITION_VALUE,
                }
                return known_values.get(value, _UNKNOWN_CONDITION_VALUE)
            return _UNKNOWN_CONDITION_VALUE

    parsed = _Parser().parse()
    if isinstance(parsed, bool):
        return parsed
    return None


def _condition_or(left: object, right: object) -> object:
    if left is True or right is True:
        return True
    if left is False and right is False:
        return False
    return _UNKNOWN_CONDITION_VALUE


def _condition_and(left: object, right: object) -> object:
    if left is False or right is False:
        return False
    if left is True and right is True:
        return True
    return _UNKNOWN_CONDITION_VALUE


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
    applicability_status: str | None = None
    applicability_reason: str | None = None
    applicability_context: Mapping[str, Any] | None = None
    receipt_key: str | None = None
    receipt_reused: bool = False
    receipt_created_at: str | None = None
    receipt_error: str | None = None

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
            "applicability_status": self.applicability_status,
            "applicability_reason": self.applicability_reason,
            "applicability_context": (
                dict(self.applicability_context)
                if self.applicability_context is not None
                else None
            ),
            "receipt_key": self.receipt_key,
            "receipt_reused": self.receipt_reused,
            "receipt_created_at": self.receipt_created_at,
            "receipt_error": self.receipt_error,
        }


@dataclass(frozen=True, slots=True)
class ValidationReport:
    attempt_id: str
    request_id: str
    status: ValidationReportStatus
    results: tuple[ValidationResult, ...]
    error: str | None = None
    profile_contexts: tuple[Mapping[str, Any], ...] = ()
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
                    if result.status
                    in (ValidationResultStatus.BLOCKED, ValidationResultStatus.PENDING)
                    else "test_failure"
                ),
                repairability=(
                    "blocked"
                    if result.status
                    in (
                        ValidationResultStatus.BLOCKED,
                        ValidationResultStatus.TIMED_OUT,
                        ValidationResultStatus.PENDING,
                    )
                    else "repairable"
                ),
            )
            for result in self.results
            if result.status is not ValidationResultStatus.PASSED
            and result.status is not ValidationResultStatus.NOT_APPLICABLE
        ]
        return {
            "schema_version": self.schema_version,
            "attempt_id": self.attempt_id,
            "request_id": self.request_id,
            "status": self.status.value,
            "results": [result.to_data() for result in self.results],
            "profile_contexts": [dict(item) for item in self.profile_contexts],
            "error": self.error,
            "failures": failures,
        }


@dataclass(frozen=True, slots=True)
class CodingAgentExecutionResult:
    """One shared coding, local validation, and whitespace-check execution."""

    attempt: CodingAgentAttempt
    validation: ValidationReport
    diff_check: subprocess.CompletedProcess[str]
    registered_corrections: tuple[Mapping[str, Any], ...] = ()


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
        if callable(reconcile_after_worker) and (
            attempt.status is CodingAgentStatus.COMPLETED
        ):
            reconcile_after_worker()
        if (
            reference_context is not None
            and reference_context is not execution.validation_context
        ):
            raise ValueError("Validation context differs from the prepared source run")
        reference_context = execution.validation_context

    def run_validation() -> ValidationReport:
        paths_before = _working_paths(worktree_root) if execution is not None else set()
        report = ValidationRunner(
            profiles,
            timeout_seconds=validation_timeout_seconds,
            reference_context=reference_context,
            command_wrapper=execution.protect_command if execution else None,
            environment_overrides=(
                execution.validation_environment() if execution else None
            ),
            reference_claims=execution.claims if execution else (),
        ).run(request, attempt, worktree_root=worktree_root)
        if execution is not None:
            cleanup_fixtures = getattr(
                execution, "cleanup_generated_validation_fixtures", None
            )
            if callable(cleanup_fixtures):
                cleanup_fixtures(paths_before)
        return report

    validation = run_validation()
    registered_corrections: tuple[Mapping[str, Any], ...] = ()
    if execution is not None:
        correction = execution.apply_registered_correction(attempt, validation)
        if correction is not None:
            registered_corrections = (correction,)
            if correction.get("status") == "applied":
                validation = run_validation()
                execution.record_registered_correction_candidate()
    runner.store.save_validation_report(validation)
    diff_check = subprocess.run(
        ["git", "diff", "--check"],
        cwd=worktree_root,
        capture_output=True,
        text=True,
        check=False,
    )
    return CodingAgentExecutionResult(
        attempt, validation, diff_check, registered_corrections
    )


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
        applicability_context: ValidationApplicabilityContext | None = None,
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
                profile_contexts=self._profile_contexts(names),
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
                profile_contexts=self._profile_contexts(names),
            )

        if request.validation_profile_contexts:
            declared = {
                str(item["name"]): dict(item)
                for item in request.validation_profile_contexts
            }
            mismatched = [
                profile.name
                for profile in profiles
                if declared.get(profile.name) != profile.to_data()
            ]
            if mismatched:
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
                            error=(
                                "validation profile context does not match the "
                                "reviewed request: "
                                f"{name}"
                            ),
                        )
                        for name in mismatched
                    ),
                    error="validation profile context changed after request review",
                    profile_contexts=self._profile_contexts(names),
                )

        # Candidate test programs can mutate files. Source evidence must describe
        # the final bytes after every ordinary verification command has finished.
        context = applicability_context or ValidationApplicabilityContext()
        ordinary_results = tuple(
            self._run_profile_with_receipt(
                profile,
                request,
                attempt,
                worktree_root,
                context,
            )
            for profile in profiles
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
        pending = (
            not results
            or any(
                result.status is ValidationResultStatus.PENDING for result in results
            )
            or bool(results)
            and all(
                result.status is ValidationResultStatus.NOT_APPLICABLE
                for result in results
            )
        )
        return ValidationReport(
            attempt_id=attempt.attempt_id,
            request_id=request.request_id,
            status=(
                ValidationReportStatus.BLOCKED
                if blocked
                else ValidationReportStatus.FAILED
                if failed
                else ValidationReportStatus.PENDING
                if pending
                else ValidationReportStatus.PASSED
            ),
            results=results,
            error="no validation profiles produced results" if not results else None,
            profile_contexts=tuple(profile.to_data() for profile in profiles),
        )

    def _run_profile_with_receipt(
        self,
        profile: ValidationProfile,
        request: ImplementationRequest,
        attempt: CodingAgentAttempt,
        worktree_root: Path,
        context: ValidationApplicabilityContext,
    ) -> ValidationResult:
        applicable, _reason = evaluate_validation_applicability(profile, context)
        receipt_key, receipt_error = _validation_receipt_key(
            profile,
            request,
            attempt,
            worktree_root,
            context,
            environment_overrides=self.environment_overrides,
            has_command_wrapper=self.command_wrapper is not None,
        )
        if applicable is ValidationApplicabilityStatus.APPLICABLE and receipt_key:
            receipt = _load_validation_receipt(worktree_root, receipt_key)
            if receipt is not None:
                result_data = receipt.get("result")
                created_at = receipt.get("created_at")
                if isinstance(result_data, Mapping) and isinstance(created_at, str):
                    try:
                        result = _validation_result_from_data(result_data)
                    except (KeyError, TypeError, ValueError):
                        result = None
                    if (
                        result is not None
                        and result.status is ValidationResultStatus.PASSED
                        and result.command == profile.command
                    ):
                        return replace(
                            result,
                            receipt_key=receipt_key,
                            receipt_reused=True,
                            receipt_created_at=created_at,
                            receipt_error=None,
                        )

        result = self._run_profile_for_context(profile, request, worktree_root, context)
        if receipt_error is not None:
            return replace(result, receipt_error=receipt_error)
        if not receipt_key or result.status is not ValidationResultStatus.PASSED:
            return replace(result, receipt_key=receipt_key)
        current_key, current_error = _validation_receipt_key(
            profile,
            request,
            attempt,
            worktree_root,
            context,
            environment_overrides=self.environment_overrides,
            has_command_wrapper=self.command_wrapper is not None,
        )
        if current_error is not None or current_key != receipt_key:
            return replace(
                result,
                receipt_key=receipt_key,
                receipt_error=(
                    current_error
                    or "candidate inputs changed while validation was running"
                ),
            )
        created_at = datetime.now(UTC).isoformat()
        try:
            _store_validation_receipt(
                worktree_root,
                receipt_key,
                created_at=created_at,
                result=result,
            )
        except OSError as error:
            return replace(
                result,
                receipt_key=receipt_key,
                receipt_error=f"could not persist validation receipt: {error}",
            )
        return replace(
            result,
            receipt_key=receipt_key,
            receipt_reused=False,
            receipt_created_at=created_at,
        )

    def _profile_contexts(self, names: Sequence[str]) -> tuple[Mapping[str, Any], ...]:
        return tuple(
            self.profiles[name].to_data() for name in names if name in self.profiles
        )

    def _run_profile_for_context(
        self,
        profile: ValidationProfile,
        request: ImplementationRequest,
        worktree_root: Path,
        context: ValidationApplicabilityContext,
    ) -> ValidationResult:
        applicability_status, reason = evaluate_validation_applicability(
            profile, context
        )
        if applicability_status is ValidationApplicabilityStatus.NOT_APPLICABLE:
            result = ValidationResult(
                profile=profile.name,
                command=profile.command,
                status=ValidationResultStatus.NOT_APPLICABLE,
                returncode=None,
                error=reason,
            )
        elif applicability_status is ValidationApplicabilityStatus.PENDING:
            result = ValidationResult(
                profile=profile.name,
                command=profile.command,
                status=ValidationResultStatus.PENDING,
                returncode=None,
                error=reason,
            )
        else:
            result = self._run_profile(profile, request, worktree_root)
        return replace(
            result,
            applicability_status=applicability_status.value,
            applicability_reason=reason,
            applicability_context=context.to_data(),
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
        if profile.execution_kind != "argv" or profile.shell or profile.script:
            return ValidationResult(
                profile=profile.name,
                command=profile.command,
                status=ValidationResultStatus.BLOCKED,
                returncode=None,
                error=(
                    "shell or unresolved validation execution is not supported "
                    "by the argv runner"
                ),
            )
        if not profile.command:
            return ValidationResult(
                profile=profile.name,
                command=(),
                status=ValidationResultStatus.BLOCKED,
                returncode=None,
                error="validation profile command must not be empty",
            )
        relative_cwd = Path(profile.cwd)
        if not profile.cwd or relative_cwd.is_absolute() or ".." in relative_cwd.parts:
            return ValidationResult(
                profile=profile.name,
                command=profile.command,
                status=ValidationResultStatus.BLOCKED,
                returncode=None,
                error="validation profile cwd must be repository-relative",
            )
        execution_cwd = (worktree_root / relative_cwd).resolve()
        if not execution_cwd.is_relative_to(worktree_root.resolve()):
            return ValidationResult(
                profile=profile.name,
                command=profile.command,
                status=ValidationResultStatus.BLOCKED,
                returncode=None,
                error="validation profile cwd escapes the worktree",
            )
        if not execution_cwd.is_dir():
            return ValidationResult(
                profile=profile.name,
                command=profile.command,
                status=ValidationResultStatus.BLOCKED,
                returncode=None,
                error=f"validation profile cwd does not exist: {profile.cwd}",
            )
        argv_allowed = any(
            profile.command[: len(allowed)] == allowed
            for allowed in request.allowed_command_argv
        )
        legacy_allowed = not request.allowed_command_argv and any(
            _command_matches(profile.command, allowed)
            for allowed in request.allowed_commands
        )
        if not argv_allowed and not legacy_allowed:
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
                cwd=execution_cwd,
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
            applicability_status=item.get("applicability_status"),
            applicability_reason=item.get("applicability_reason"),
            applicability_context=(
                dict(item["applicability_context"])
                if isinstance(item.get("applicability_context"), Mapping)
                else None
            ),
            receipt_key=(
                item.get("receipt_key")
                if isinstance(item.get("receipt_key"), str)
                else None
            ),
            receipt_reused=item.get("receipt_reused") is True,
            receipt_created_at=(
                item.get("receipt_created_at")
                if isinstance(item.get("receipt_created_at"), str)
                else None
            ),
            receipt_error=(
                item.get("receipt_error")
                if isinstance(item.get("receipt_error"), str)
                else None
            ),
        )
        for item in data.get("results", [])
    )
    return ValidationReport(
        attempt_id=str(data["attempt_id"]),
        request_id=str(data["request_id"]),
        status=ValidationReportStatus(str(data["status"])),
        results=results,
        error=data.get("error"),
        profile_contexts=tuple(
            dict(item)
            for item in data.get("profile_contexts", [])
            if isinstance(item, Mapping)
        ),
        schema_version=str(data["schema_version"]),
    )


def _validation_receipt_key(
    profile: ValidationProfile,
    request: ImplementationRequest,
    attempt: CodingAgentAttempt,
    worktree_root: Path,
    context: ValidationApplicabilityContext,
    *,
    environment_overrides: Mapping[str, str],
    has_command_wrapper: bool,
) -> tuple[str | None, str | None]:
    environment_id = profile.environment_id
    if (
        not isinstance(environment_id, str)
        or not environment_id.strip()
        or (environment_id.strip().lower() in {"unknown", "unavailable", "pending"})
    ):
        return None, "validation environment identity is unknown"
    if environment_overrides or has_command_wrapper:
        return None, "runtime command environment cannot be fingerprinted safely"
    root = worktree_root.resolve()
    candidate_paths = sorted(
        set(attempt.changed_paths) | set(context.changed_paths or ())
    )
    if not candidate_paths:
        return None, "candidate file identity is unavailable"
    candidate_files, error = _validation_file_fingerprints(root, candidate_paths)
    if error is not None:
        return None, error
    verifier_paths = set(profile.config_files) | set(profile.evidence)
    verifier_paths.update(
        selector.split("::", 1)[0]
        for selector in profile.selectors
        if selector.split("::", 1)[0]
    )
    relative_cwd = Path(profile.cwd)
    for argument in profile.command:
        candidate = Path(argument.split("::", 1)[0])
        if candidate.is_absolute() or ".." in candidate.parts:
            continue
        relative = relative_cwd / candidate
        try:
            resolved = (root / relative).resolve()
        except OSError:
            continue
        if resolved.is_file() and resolved.is_relative_to(root):
            verifier_paths.add(relative.as_posix())
    verifier_files, error = _validation_file_fingerprints(root, sorted(verifier_paths))
    if error is not None:
        return None, error
    contract = {
        "objective": request.objective,
        "prompt": request.prompt,
        "base_commit": request.base_commit,
        "plan_fingerprint": request.plan_fingerprint,
        "allowed_paths": request.allowed_paths,
        "acceptance_criteria": request.acceptance_criteria,
        "validation_profiles": request.validation_profiles,
        "validation_profile_contexts": request.validation_profile_contexts,
        "source_reference_mode": request.source_reference_mode,
    }
    inputs = {
        "candidate_files": candidate_files,
        "verifier_files": verifier_files,
        "profile": profile.to_data(),
        "contract": contract,
        "applicability_context": context.to_data(),
        "environment": {
            "id": environment_id,
            "platform": platform.platform(),
            "python": sys.version,
        },
    }
    try:
        encoded = json.dumps(inputs, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as error:
        return None, f"validation inputs could not be fingerprinted: {error}"
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest(), None


def _validation_file_fingerprints(
    root: Path, paths: Sequence[str]
) -> tuple[list[dict[str, str]], str | None]:
    records: list[dict[str, str]] = []
    for raw_path in sorted(set(paths)):
        candidate = Path(raw_path)
        if candidate.is_absolute() or ".." in candidate.parts:
            return [], f"validation input path is not repository-relative: {raw_path}"
        target = (root / candidate).resolve()
        if not target.is_relative_to(root):
            return [], f"validation input path escapes candidate worktree: {raw_path}"
        if not target.is_file():
            records.append({"path": candidate.as_posix(), "sha256": "missing"})
            continue
        digest = hashlib.sha256()
        try:
            with target.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
        except OSError as error:
            return [], f"could not fingerprint validation input {raw_path}: {error}"
        records.append({"path": candidate.as_posix(), "sha256": digest.hexdigest()})
    return records, None


def _validation_receipt_path(worktree_root: Path) -> Path:
    return worktree_root / ".powdrr" / "validation-receipts.json"


def _load_validation_receipt(
    worktree_root: Path, receipt_key: str
) -> Mapping[str, Any] | None:
    try:
        document = json.loads(_validation_receipt_path(worktree_root).read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if (
        not isinstance(document, Mapping)
        or document.get("schema") != VALIDATION_RECEIPT_SCHEMA_VERSION
        or not isinstance(document.get("receipts"), Mapping)
    ):
        return None
    receipt = document["receipts"].get(receipt_key)
    return receipt if isinstance(receipt, Mapping) else None


def _store_validation_receipt(
    worktree_root: Path,
    receipt_key: str,
    *,
    created_at: str,
    result: ValidationResult,
) -> None:
    path = _validation_receipt_path(worktree_root)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    receipts: dict[str, Any] = {}
    try:
        current = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        current = None
    if (
        isinstance(current, Mapping)
        and current.get("schema") == VALIDATION_RECEIPT_SCHEMA_VERSION
        and isinstance(current.get("receipts"), Mapping)
    ):
        receipts.update(
            (str(key), value)
            for key, value in current["receipts"].items()
            if isinstance(value, Mapping)
        )
    receipts.pop(receipt_key, None)
    receipts[receipt_key] = {
        "created_at": created_at,
        "result": replace(
            result,
            receipt_key=receipt_key,
            receipt_reused=False,
            receipt_created_at=created_at,
            receipt_error=None,
        ).to_data(),
    }
    receipts = dict(list(receipts.items())[-_VALIDATION_RECEIPT_LIMIT:])
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".validation-receipts-", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(
                {"schema": VALIDATION_RECEIPT_SCHEMA_VERSION, "receipts": receipts},
                stream,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _validation_result_from_data(data: Mapping[str, Any]) -> ValidationResult:
    return ValidationResult(
        profile=str(data["profile"]),
        command=tuple(data.get("command", [])),
        status=ValidationResultStatus(str(data["status"])),
        returncode=data.get("returncode"),
        stdout=str(data.get("stdout", "")),
        stderr=str(data.get("stderr", "")),
        error=data.get("error"),
        test_id=data.get("test_id"),
        expected=data.get("expected"),
        actual=data.get("actual"),
        source_location=data.get("source_location"),
        evidence_path=data.get("evidence_path"),
        applicability_status=data.get("applicability_status"),
        applicability_reason=data.get("applicability_reason"),
        applicability_context=(
            dict(data["applicability_context"])
            if isinstance(data.get("applicability_context"), Mapping)
            else None
        ),
        receipt_key=(
            data.get("receipt_key")
            if isinstance(data.get("receipt_key"), str)
            else None
        ),
        receipt_reused=data.get("receipt_reused") is True,
        receipt_created_at=(
            data.get("receipt_created_at")
            if isinstance(data.get("receipt_created_at"), str)
            else None
        ),
        receipt_error=(
            data.get("receipt_error")
            if isinstance(data.get("receipt_error"), str)
            else None
        ),
    )


__all__ = [
    "CODING_AGENT_VALIDATION_REPORT_SCHEMA_VERSION",
    "ValidationProfile",
    "ValidationApplicabilityContext",
    "ValidationApplicabilityStatus",
    "ValidationReport",
    "ValidationReportStatus",
    "ValidationResult",
    "ValidationResultStatus",
    "ValidationRunner",
    "evaluate_validation_applicability",
    "parse_validation_profile",
    "validation_report_from_data",
]
