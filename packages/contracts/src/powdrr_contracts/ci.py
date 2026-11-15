"""Public records for CI observations and local failure reproduction."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Self

from powdrr_contracts.models import ContractError, _object, _string

CIObservationStatus = Literal["queued", "in_progress", "completed", "unknown"]
CIConclusion = Literal[
    "success",
    "failure",
    "cancelled",
    "skipped",
    "neutral",
    "timed_out",
    "action_required",
    "stale",
    "unknown",
]
LocalReproductionStatus = Literal[
    "reproduced",
    "not_reproduced",
    "not_comparable",
    "not_executable_locally",
    "execution_error",
]
MAX_CI_LOG_EXCERPT_BYTES = 4_000


@dataclass(frozen=True, slots=True)
class CIRequiredCheck:
    """A required GitHub check as reported for the observed PR head."""

    name: str
    state: str
    head_sha: str
    workflow: str | None = None

    def __post_init__(self) -> None:
        if not self.name.strip() or not self.state.strip():
            raise ContractError("ci_required_check name and state are required")
        _validate_sha(self.head_sha, "ci_required_check.head_sha")
        if self.workflow is not None and not isinstance(self.workflow, str):
            raise ContractError("ci_required_check.workflow must be a string or null")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "name": self.name,
            "state": self.state,
            "head_sha": self.head_sha,
            "workflow": self.workflow,
        }

    @classmethod
    def from_dict(cls, value: object) -> Self:
        data = _object(value, "ci_required_check")
        if data.get("schema_version") != 1:
            raise ContractError("unsupported ci_required_check schema_version")
        workflow = data.get("workflow")
        if workflow is not None and not isinstance(workflow, str):
            raise ContractError("ci_required_check.workflow must be a string or null")
        return cls(
            name=_string(data.get("name"), "ci_required_check.name"),
            state=_string(data.get("state"), "ci_required_check.state"),
            head_sha=_string(data.get("head_sha"), "ci_required_check.head_sha"),
            workflow=workflow,
        )


@dataclass(frozen=True, slots=True)
class CIJobLogEvidence:
    """A bounded, redacted failed-job log excerpt with raw-content integrity."""

    status: Literal["available", "unavailable"]
    excerpt: str | None = None
    raw_sha256: str | None = None
    truncated: bool = False
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.status not in {"available", "unavailable"}:
            raise ContractError("ci_job_log_evidence.status is unsupported")
        if not isinstance(self.truncated, bool):
            raise ContractError("ci_job_log_evidence.truncated must be boolean")
        if self.status == "available":
            if (
                self.excerpt is None
                or self.raw_sha256 is None
                or self.reason is not None
            ):
                raise ContractError(
                    "available log evidence requires an excerpt and raw digest only"
                )
            if len(self.excerpt.encode("utf-8")) > MAX_CI_LOG_EXCERPT_BYTES:
                raise ContractError(
                    "ci_job_log_evidence.excerpt exceeds the size limit"
                )
            _validate_sha256(self.raw_sha256, "ci_job_log_evidence.raw_sha256")
        elif (
            self.excerpt is not None
            or self.raw_sha256 is not None
            or self.truncated
            or not isinstance(self.reason, str)
            or not self.reason.strip()
        ):
            raise ContractError("unavailable log evidence requires only a reason")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "status": self.status,
            "excerpt": self.excerpt,
            "raw_sha256": self.raw_sha256,
            "truncated": self.truncated,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, value: object) -> Self:
        data = _object(value, "ci_job_log_evidence")
        if data.get("schema_version") != 1:
            raise ContractError("unsupported ci_job_log_evidence schema_version")
        for name in ("excerpt", "raw_sha256", "reason"):
            if data.get(name) is not None and not isinstance(data[name], str):
                raise ContractError(
                    f"ci_job_log_evidence.{name} must be a string or null"
                )
        truncated = data.get("truncated", False)
        if not isinstance(truncated, bool):
            raise ContractError("ci_job_log_evidence.truncated must be boolean")
        return cls(
            status=data.get("status"),
            excerpt=data.get("excerpt"),
            raw_sha256=data.get("raw_sha256"),
            truncated=truncated,
            reason=data.get("reason"),
        )


@dataclass(frozen=True, slots=True)
class CIObservation:
    """Versioned envelope binding job evidence to one observed pull request head."""

    repository: str
    pull_request_number: int
    head_sha: str
    jobs: tuple[CIJobObservation, ...]
    observed_at: str | None = None
    tested_sha: str | None = None
    merge_base_sha: str | None = None
    required_checks_status: Literal["available", "unavailable"] = "unavailable"
    required_check_source: str | None = None
    required_checks: tuple[CIRequiredCheck, ...] = ()

    def __post_init__(self) -> None:
        if self.repository.count("/") != 1 or not all(self.repository.split("/")):
            raise ContractError("ci_observation.repository must use owner/name form")
        _positive_int(self.pull_request_number, "ci_observation.pull_request_number")
        if self.observed_at is not None and not isinstance(self.observed_at, str):
            raise ContractError("ci_observation.observed_at must be a string or null")
        _validate_sha(self.head_sha, "ci_observation.head_sha")
        for name in ("tested_sha", "merge_base_sha"):
            value = getattr(self, name)
            if value is not None:
                _validate_sha(value, f"ci_observation.{name}")
        if self.required_checks_status not in {"available", "unavailable"}:
            raise ContractError("ci_observation.required_checks_status is unsupported")
        if (
            self.required_checks_status == "available"
            and not self.required_check_source
        ):
            raise ContractError(
                "available required checks must identify their policy source"
            )
        if self.required_checks_status == "available" and not self.required_checks:
            raise ContractError(
                "available required-check policy must contain observed checks"
            )
        if any(check.head_sha != self.head_sha for check in self.required_checks):
            raise ContractError("required checks must match the observed PR head")
        if any(job.head_sha != self.head_sha for job in self.jobs):
            raise ContractError("ci_observation jobs must match its PR head SHA")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "repository": self.repository,
            "pull_request_number": self.pull_request_number,
            "head_sha": self.head_sha,
            "tested_sha": self.tested_sha,
            "merge_base_sha": self.merge_base_sha,
            "observed_at": self.observed_at,
            "required_checks_status": self.required_checks_status,
            "required_check_source": self.required_check_source,
            "required_checks": [check.to_dict() for check in self.required_checks],
            "jobs": [job.to_dict() for job in self.jobs],
        }

    @classmethod
    def from_dict(cls, value: object) -> Self:
        data = _object(value, "ci_observation")
        if data.get("schema_version") != 1:
            raise ContractError("unsupported ci_observation schema_version")
        jobs = data.get("jobs")
        if not isinstance(jobs, list):
            raise ContractError("ci_observation.jobs must be an array")
        required_checks = data.get("required_checks", [])
        if not isinstance(required_checks, list):
            raise ContractError("ci_observation.required_checks must be an array")
        parsed_jobs = tuple(CIJobObservation.from_dict(item) for item in jobs)
        parsed_required_checks = tuple(
            CIRequiredCheck.from_dict(item) for item in required_checks
        )
        observed_at = data.get("observed_at")
        source = data.get("required_check_source")
        if observed_at is not None and not isinstance(observed_at, str):
            raise ContractError("ci_observation.observed_at must be a string or null")
        if source is not None and not isinstance(source, str):
            raise ContractError(
                "ci_observation.required_check_source must be a string or null"
            )
        return cls(
            repository=_string(data.get("repository"), "ci_observation.repository"),
            pull_request_number=_positive_int(
                data.get("pull_request_number"), "ci_observation.pull_request_number"
            ),
            head_sha=_string(data.get("head_sha"), "ci_observation.head_sha"),
            tested_sha=_optional_sha(
                data.get("tested_sha"), "ci_observation.tested_sha"
            ),
            merge_base_sha=_optional_sha(
                data.get("merge_base_sha"), "ci_observation.merge_base_sha"
            ),
            observed_at=observed_at,
            required_checks_status=data.get("required_checks_status", "unavailable"),
            required_check_source=source,
            required_checks=parsed_required_checks,
            jobs=parsed_jobs,
        )


@dataclass(frozen=True, slots=True)
class CIFailureUnit:
    """Versioned, narrowly scoped failure evidence from one observed job."""

    failure_id: str
    observation_id: str
    job_id: int
    phase: str
    signature: str
    evidence: tuple[str, ...]
    locally_executable: bool
    head_sha: str
    run_id: int
    run_attempt: int
    workflow_path: str | None = None
    start_line: int | None = None
    end_line: int | None = None
    command: tuple[str, ...] = ()
    test_node_id: str | None = None

    def __post_init__(self) -> None:
        for name in ("failure_id", "observation_id", "phase", "signature"):
            if not getattr(self, name).strip():
                raise ContractError(f"ci_failure_unit.{name} must not be empty")
        _validate_sha(self.head_sha, "ci_failure_unit.head_sha")
        for name in ("job_id", "run_id", "run_attempt"):
            _positive_int(getattr(self, name), f"ci_failure_unit.{name}")
        if not self.evidence or any(not item.strip() for item in self.evidence):
            raise ContractError("ci_failure_unit.evidence must contain references")
        if self.test_node_id is not None and not self.test_node_id.strip():
            raise ContractError("ci_failure_unit.test_node_id must not be empty")
        if any(
            value is not None and value < 1
            for value in (self.start_line, self.end_line)
        ):
            raise ContractError("ci_failure_unit line numbers must be positive")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "failure_id": self.failure_id,
            "observation_id": self.observation_id,
            "job_id": self.job_id,
            "run_id": self.run_id,
            "run_attempt": self.run_attempt,
            "head_sha": self.head_sha,
            "phase": self.phase,
            "signature": self.signature,
            "evidence": list(self.evidence),
            "locally_executable": self.locally_executable,
            "workflow_path": self.workflow_path,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "command": list(self.command),
            "test_node_id": self.test_node_id,
        }

    @classmethod
    def from_dict(cls, value: object) -> Self:
        data = _object(value, "ci_failure_unit")
        if data.get("schema_version") != 1:
            raise ContractError("unsupported ci_failure_unit schema_version")
        evidence = data.get("evidence")
        command = data.get("command", [])
        if not isinstance(evidence, list) or not all(
            isinstance(item, str) for item in evidence
        ):
            raise ContractError("ci_failure_unit.evidence must be an array of strings")
        if not isinstance(command, list) or not all(
            isinstance(item, str) for item in command
        ):
            raise ContractError("ci_failure_unit.command must be an array of strings")
        executable = data.get("locally_executable")
        if not isinstance(executable, bool):
            raise ContractError("ci_failure_unit.locally_executable must be boolean")
        workflow_path = data.get("workflow_path")
        if workflow_path is not None and not isinstance(workflow_path, str):
            raise ContractError(
                "ci_failure_unit.workflow_path must be a string or null"
            )
        test_node_id = data.get("test_node_id")
        if test_node_id is not None and not isinstance(test_node_id, str):
            raise ContractError("ci_failure_unit.test_node_id must be a string or null")
        line_values: dict[str, int | None] = {}
        for name in ("start_line", "end_line"):
            item = data.get(name)
            if item is not None and (
                not isinstance(item, int) or isinstance(item, bool) or item < 1
            ):
                raise ContractError(f"ci_failure_unit.{name} must be positive or null")
            line_values[name] = item
        return cls(
            failure_id=_string(data.get("failure_id"), "ci_failure_unit.failure_id"),
            observation_id=_string(
                data.get("observation_id"), "ci_failure_unit.observation_id"
            ),
            job_id=_positive_int(data.get("job_id"), "ci_failure_unit.job_id"),
            phase=_string(data.get("phase"), "ci_failure_unit.phase"),
            signature=_string(data.get("signature"), "ci_failure_unit.signature"),
            evidence=tuple(evidence),
            locally_executable=executable,
            head_sha=_string(data.get("head_sha"), "ci_failure_unit.head_sha"),
            run_id=_positive_int(data.get("run_id"), "ci_failure_unit.run_id"),
            run_attempt=_positive_int(
                data.get("run_attempt"), "ci_failure_unit.run_attempt"
            ),
            workflow_path=workflow_path,
            start_line=line_values["start_line"],
            end_line=line_values["end_line"],
            command=tuple(command),
            test_node_id=test_node_id,
        )


def _validate_sha256(value: str, name: str) -> None:
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ContractError(f"{name} must be a SHA-256 digest")


def _validate_sha(value: str, name: str) -> None:
    if len(value) not in (40, 64) or any(
        char not in "0123456789abcdef" for char in value
    ):
        raise ContractError(f"{name} must be a full lowercase commit SHA")


def _optional_sha(value: object, name: str) -> str | None:
    if value is None:
        return None
    sha = _string(value, name)
    _validate_sha(sha, name)
    return sha


def _positive_int(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ContractError(f"{name} must be a positive integer")
    return value


@dataclass(frozen=True, slots=True)
class CIAnnotation:
    """A GitHub check annotation with its source check-run identity."""

    check_run_id: int
    path: str | None
    start_line: int | None
    end_line: int | None
    level: str | None
    message: str
    raw_sha256: str

    def __post_init__(self) -> None:
        if self.check_run_id < 1:
            raise ContractError("ci_annotation.check_run_id must be positive")
        if not self.message.strip():
            raise ContractError("ci_annotation.message must not be empty")
        if any(
            value is not None and value < 1
            for value in (self.start_line, self.end_line)
        ):
            raise ContractError("ci_annotation line numbers must be positive")
        _validate_sha256(self.raw_sha256, "ci_annotation.raw_sha256")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "check_run_id": self.check_run_id,
            "path": self.path,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "level": self.level,
            "message": self.message,
            "raw_sha256": self.raw_sha256,
        }

    @classmethod
    def from_dict(cls, value: object) -> Self:
        data = _object(value, "ci_annotation")
        if data.get("schema_version") != 1:
            raise ContractError("unsupported ci_annotation schema_version")
        check_run_id = data.get("check_run_id")
        if (
            not isinstance(check_run_id, int)
            or isinstance(check_run_id, bool)
            or check_run_id < 1
        ):
            raise ContractError("ci_annotation.check_run_id must be a positive integer")
        optional = ("path", "level")
        for name in optional:
            if data.get(name) is not None and not isinstance(data[name], str):
                raise ContractError(f"ci_annotation.{name} must be a string or null")
        for name in ("start_line", "end_line"):
            if data.get(name) is not None and (
                not isinstance(data[name], int)
                or isinstance(data[name], bool)
                or data[name] < 1
            ):
                raise ContractError(
                    f"ci_annotation.{name} must be a positive integer or null"
                )
        digest = _string(data.get("raw_sha256"), "ci_annotation.raw_sha256")
        _validate_sha256(digest, "ci_annotation.raw_sha256")
        return cls(
            check_run_id=check_run_id,
            path=data.get("path"),
            start_line=data.get("start_line"),
            end_line=data.get("end_line"),
            level=data.get("level"),
            message=_string(data.get("message"), "ci_annotation.message"),
            raw_sha256=digest,
        )


@dataclass(frozen=True, slots=True)
class CIJobObservation:
    """One Actions job and the evidence returned for its check runs."""

    job_id: int
    run_id: int
    run_attempt: int
    head_sha: str
    status: CIObservationStatus
    conclusion: CIConclusion | None
    tested_sha: str | None = None
    workflow_path: str | None = None
    matrix: tuple[tuple[str, str], ...] = ()
    check_context: str | None = None
    check_app_id: int | None = None
    runner_name: str | None = None
    runner_id: int | None = None
    runner_group_name: str | None = None
    runner_labels: tuple[str, ...] = ()
    steps: tuple[dict[str, Any], ...] = ()
    annotations: tuple[CIAnnotation, ...] = ()
    log_evidence: CIJobLogEvidence | None = None

    def __post_init__(self) -> None:
        for name in ("job_id", "run_id", "run_attempt"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ContractError(f"ci_job.{name} must be a positive integer")
        if len(self.head_sha) not in (40, 64) or any(
            c not in "0123456789abcdef" for c in self.head_sha
        ):
            raise ContractError("ci_job.head_sha must be a full lowercase commit SHA")
        if self.status not in {"queued", "in_progress", "completed", "unknown"}:
            raise ContractError("ci_job.status is unsupported")
        if self.conclusion not in {
            None,
            "success",
            "failure",
            "cancelled",
            "skipped",
            "neutral",
            "timed_out",
            "action_required",
            "stale",
            "unknown",
        }:
            raise ContractError("ci_job.conclusion is unsupported")
        if self.tested_sha is not None:
            _validate_sha(self.tested_sha, "ci_job.tested_sha")
        for name in ("workflow_path", "check_context"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, str):
                raise ContractError(f"ci_job.{name} must be a string or null")
        if self.check_app_id is not None and (
            not isinstance(self.check_app_id, int)
            or isinstance(self.check_app_id, bool)
            or self.check_app_id < 0
        ):
            raise ContractError("ci_job.check_app_id must be a non-negative integer")
        if any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in self.matrix
        ):
            raise ContractError("ci_job.matrix must contain string key/value pairs")
        if self.runner_id is not None and (
            not isinstance(self.runner_id, int)
            or isinstance(self.runner_id, bool)
            or self.runner_id < 0
        ):
            raise ContractError(
                "ci_job.runner_id must be a non-negative integer or null"
            )
        if self.log_evidence is not None and not isinstance(
            self.log_evidence, CIJobLogEvidence
        ):
            raise ContractError("ci_job.log_evidence must be CIJobLogEvidence or null")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "job_id": self.job_id,
            "run_id": self.run_id,
            "run_attempt": self.run_attempt,
            "head_sha": self.head_sha,
            "status": self.status,
            "conclusion": self.conclusion,
            "tested_sha": self.tested_sha,
            "workflow_path": self.workflow_path,
            "matrix": {key: value for key, value in self.matrix},
            "check_context": self.check_context,
            "check_app_id": self.check_app_id,
            "runner_name": self.runner_name,
            "runner_id": self.runner_id,
            "runner_group_name": self.runner_group_name,
            "runner_labels": list(self.runner_labels),
            "steps": list(self.steps),
            "annotations": [item.to_dict() for item in self.annotations],
            "log_evidence": (
                self.log_evidence.to_dict() if self.log_evidence is not None else None
            ),
        }

    @classmethod
    def from_dict(cls, value: object) -> Self:
        data = _object(value, "ci_job")
        if data.get("schema_version") != 1:
            raise ContractError("unsupported ci_job schema_version")
        labels = data.get("runner_labels", [])
        steps = data.get("steps", [])
        annotations = data.get("annotations", [])
        log_evidence = data.get("log_evidence")
        if not isinstance(labels, list) or not all(
            isinstance(item, str) for item in labels
        ):
            raise ContractError("ci_job.runner_labels must be an array of strings")
        if not isinstance(steps, list) or not all(
            isinstance(item, dict) for item in steps
        ):
            raise ContractError("ci_job.steps must be an array of objects")
        if not isinstance(annotations, list):
            raise ContractError("ci_job.annotations must be an array")
        if log_evidence is not None and not isinstance(log_evidence, dict):
            raise ContractError("ci_job.log_evidence must be an object or null")
        optional_strings = ("runner_name", "runner_group_name")
        for name in optional_strings:
            if data.get(name) is not None and not isinstance(data[name], str):
                raise ContractError(f"ci_job.{name} must be a string or null")
        runner_id = data.get("runner_id")
        if runner_id is not None and (
            not isinstance(runner_id, int) or isinstance(runner_id, bool)
        ):
            raise ContractError("ci_job.runner_id must be an integer or null")
        matrix = data.get("matrix", {})
        if not isinstance(matrix, dict) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in matrix.items()
        ):
            raise ContractError("ci_job.matrix must be an object of string values")
        tested_sha = _optional_sha(data.get("tested_sha"), "ci_job.tested_sha")
        for name in ("workflow_path", "check_context"):
            if data.get(name) is not None and not isinstance(data[name], str):
                raise ContractError(f"ci_job.{name} must be a string or null")
        check_app_id = data.get("check_app_id")
        if check_app_id is not None and (
            not isinstance(check_app_id, int) or isinstance(check_app_id, bool)
        ):
            raise ContractError("ci_job.check_app_id must be an integer or null")
        return cls(
            job_id=_positive_int(data.get("job_id"), "ci_job.job_id"),
            run_id=_positive_int(data.get("run_id"), "ci_job.run_id"),
            run_attempt=_positive_int(data.get("run_attempt"), "ci_job.run_attempt"),
            head_sha=_string(data.get("head_sha"), "ci_job.head_sha"),
            status=data.get("status"),
            conclusion=data.get("conclusion"),
            tested_sha=tested_sha,
            workflow_path=data.get("workflow_path"),
            matrix=tuple(sorted(matrix.items())),
            check_context=data.get("check_context"),
            check_app_id=check_app_id,
            runner_name=data.get("runner_name"),
            runner_id=runner_id,
            runner_group_name=data.get("runner_group_name"),
            runner_labels=tuple(labels),
            steps=tuple(steps),
            annotations=tuple(CIAnnotation.from_dict(item) for item in annotations),
            log_evidence=(
                CIJobLogEvidence.from_dict(log_evidence)
                if log_evidence is not None
                else None
            ),
        )


@dataclass(frozen=True, slots=True)
class LocalReproduction:
    """A conservative comparison of a CI failure with a local execution."""

    status: LocalReproductionStatus
    reason: str
    command: tuple[str, ...] = ()
    exit_code: int | None = None
    stdout_sha256: str | None = None
    stderr_sha256: str | None = None
    matching_failure: bool | None = None

    def __post_init__(self) -> None:
        if not self.reason.strip():
            raise ContractError("local_reproduction.reason must not be empty")
        if self.status == "reproduced" and self.matching_failure is not True:
            raise ContractError("reproduced requires matching_failure=true")
        if self.status == "not_reproduced" and self.matching_failure is not False:
            raise ContractError("not_reproduced requires matching_failure=false")
        if (
            self.status not in {"reproduced", "not_reproduced"}
            and self.matching_failure is not None
        ):
            raise ContractError(
                "inconclusive reproduction cannot assert a match result"
            )
        for name in ("stdout_sha256", "stderr_sha256"):
            digest = getattr(self, name)
            if digest is not None and (
                len(digest) != 64
                or any(char not in "0123456789abcdef" for char in digest)
            ):
                raise ContractError(
                    f"local_reproduction.{name} must be a SHA-256 digest"
                )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "status": self.status,
            "reason": self.reason,
            "command": list(self.command),
            "exit_code": self.exit_code,
            "stdout_sha256": self.stdout_sha256,
            "stderr_sha256": self.stderr_sha256,
            "matching_failure": self.matching_failure,
        }

    @classmethod
    def from_dict(cls, value: object) -> Self:
        data = _object(value, "local_reproduction")
        if data.get("schema_version") != 1:
            raise ContractError("unsupported local_reproduction schema_version")
        status = data.get("status")
        if status not in {
            "reproduced",
            "not_reproduced",
            "not_comparable",
            "not_executable_locally",
            "execution_error",
        }:
            raise ContractError("unsupported local_reproduction.status")
        command = data.get("command", [])
        if not isinstance(command, list) or not all(
            isinstance(item, str) for item in command
        ):
            raise ContractError(
                "local_reproduction.command must be an array of strings"
            )
        exit_code = data.get("exit_code")
        if exit_code is not None and (
            not isinstance(exit_code, int) or isinstance(exit_code, bool)
        ):
            raise ContractError(
                "local_reproduction.exit_code must be an integer or null"
            )
        matching = data.get("matching_failure")
        if matching is not None and not isinstance(matching, bool):
            raise ContractError(
                "local_reproduction.matching_failure must be boolean or null"
            )
        return cls(
            status=status,
            reason=_string(data.get("reason"), "local_reproduction.reason"),
            command=tuple(command),
            exit_code=exit_code,
            stdout_sha256=data.get("stdout_sha256"),
            stderr_sha256=data.get("stderr_sha256"),
            matching_failure=matching,
        )


@dataclass(frozen=True, slots=True)
class ProtectedArtifact:
    """A repository artifact that a CI correction must leave unchanged."""

    path: str
    sha256: str

    def __post_init__(self) -> None:
        _validate_repository_path(self.path, "protected_artifact.path")
        _validate_sha256(self.sha256, "protected_artifact.sha256")

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "sha256": self.sha256}

    @classmethod
    def from_dict(cls, value: object) -> Self:
        data = _object(value, "protected_artifact")
        return cls(
            path=_string(data.get("path"), "protected_artifact.path"),
            sha256=_string(data.get("sha256"), "protected_artifact.sha256"),
        )


@dataclass(frozen=True, slots=True)
class RepairContract:
    """Immutable, head-bound limits and obligations for one CI correction."""

    contract_id: str
    content_sha256: str
    repository: str
    pull_request_number: int
    head_sha: str
    observation_id: str
    failure_id: str
    reproduction_id: str
    reproduction_status: LocalReproductionStatus
    allowed_paths: tuple[str, ...]
    protected_artifacts: tuple[ProtectedArtifact, ...]
    required_behavior: tuple[str, ...]
    verification_commands: tuple[tuple[str, ...], ...]
    required_checks: tuple[str, ...]
    research_claim_refs: tuple[str, ...]
    stop_conditions: tuple[str, ...]
    expires_at: str

    def __post_init__(self) -> None:
        for name in ("contract_id", "observation_id", "failure_id", "reproduction_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ContractError(f"repair_contract.{name} must not be empty")
        if self.repository.count("/") != 1 or not all(self.repository.split("/")):
            raise ContractError("repair_contract.repository must use owner/name form")
        _positive_int(self.pull_request_number, "repair_contract.pull_request_number")
        _validate_sha(self.head_sha, "repair_contract.head_sha")
        if self.reproduction_status not in {"reproduced", "not_executable_locally"}:
            raise ContractError(
                "repair_contract reproduction is not eligible for repair"
            )
        if not 1 <= len(self.allowed_paths) <= 30:
            raise ContractError(
                "repair_contract.allowed_paths must contain 1 to 30 paths"
            )
        if not isinstance(self.allowed_paths, tuple):
            raise ContractError("repair_contract.allowed_paths must be immutable")
        if not 1 <= len(self.protected_artifacts) <= 100:
            raise ContractError(
                "repair_contract.protected_artifacts must contain 1 to 100 artifacts"
            )
        if not isinstance(self.protected_artifacts, tuple) or not all(
            isinstance(item, ProtectedArtifact) for item in self.protected_artifacts
        ):
            raise ContractError("repair_contract.protected_artifacts are malformed")
        _validate_unique_paths(self.allowed_paths, "repair_contract.allowed_paths")
        protected_paths = [artifact.path for artifact in self.protected_artifacts]
        _validate_unique_paths(protected_paths, "repair_contract.protected_artifacts")
        if any(
            _repository_paths_overlap(allowed, protected)
            for allowed in self.allowed_paths
            for protected in protected_paths
        ):
            raise ContractError(
                "repair_contract allowed paths overlap protected artifacts"
            )
        _validate_text_items(
            self.required_behavior, "repair_contract.required_behavior", 20
        )
        if not 1 <= len(self.verification_commands) <= 12:
            raise ContractError(
                "repair_contract.verification_commands must contain 1 to 12 commands"
            )
        for command in self.verification_commands:
            _validate_text_items(command, "repair_contract.verification_command", 32)
        _validate_text_items(
            self.required_checks, "repair_contract.required_checks", 20
        )
        if len(set(self.required_checks)) != len(self.required_checks):
            raise ContractError("repair_contract.required_checks contains duplicates")
        if (
            not isinstance(self.research_claim_refs, tuple)
            or len(self.research_claim_refs) > 6
        ):
            raise ContractError(
                "repair_contract.research_claim_refs exceeds six claims"
            )
        if any(not item.strip() for item in self.research_claim_refs):
            raise ContractError(
                "repair_contract.research_claim_refs contains an empty ref"
            )
        if len(set(self.research_claim_refs)) != len(self.research_claim_refs):
            raise ContractError(
                "repair_contract.research_claim_refs contains duplicates"
            )
        _validate_text_items(
            self.stop_conditions, "repair_contract.stop_conditions", 20
        )
        if not isinstance(self.expires_at, str):
            raise ContractError("repair_contract.expires_at must be an ISO timestamp")
        try:
            expiry = datetime.fromisoformat(self.expires_at.replace("Z", "+00:00"))
        except ValueError as error:
            raise ContractError(
                "repair_contract.expires_at must be an ISO timestamp"
            ) from error
        if expiry.tzinfo is None or expiry.utcoffset() is None:
            raise ContractError("repair_contract.expires_at must include a timezone")
        payload = self._content_payload()
        expected_digest = hashlib.sha256(_canonical_json(payload)).hexdigest()
        if self.content_sha256 != expected_digest:
            raise ContractError(
                "repair_contract.content_sha256 does not match its content"
            )
        if self.contract_id != f"repair-{expected_digest[:20]}":
            raise ContractError("repair_contract.contract_id is not deterministic")

    @classmethod
    def create(
        cls,
        *,
        repository: str,
        pull_request_number: int,
        head_sha: str,
        observation_id: str,
        failure_id: str,
        reproduction_id: str,
        reproduction_status: LocalReproductionStatus,
        allowed_paths: tuple[str, ...],
        protected_artifacts: tuple[ProtectedArtifact, ...],
        required_behavior: tuple[str, ...],
        verification_commands: tuple[tuple[str, ...], ...],
        required_checks: tuple[str, ...],
        research_claim_refs: tuple[str, ...],
        stop_conditions: tuple[str, ...],
        expires_at: str,
    ) -> Self:
        payload = {
            "schema_version": 1,
            "repository": repository,
            "pull_request_number": pull_request_number,
            "head_sha": head_sha,
            "observation_id": observation_id,
            "failure_id": failure_id,
            "reproduction_id": reproduction_id,
            "reproduction_status": reproduction_status,
            "allowed_paths": list(allowed_paths),
            "protected_artifacts": [item.to_dict() for item in protected_artifacts],
            "required_behavior": list(required_behavior),
            "verification_commands": [list(item) for item in verification_commands],
            "required_checks": list(required_checks),
            "research_claim_refs": list(research_claim_refs),
            "stop_conditions": list(stop_conditions),
            "expires_at": expires_at,
        }
        digest = hashlib.sha256(_canonical_json(payload)).hexdigest()
        return cls(
            contract_id=f"repair-{digest[:20]}",
            content_sha256=digest,
            repository=repository,
            pull_request_number=pull_request_number,
            head_sha=head_sha,
            observation_id=observation_id,
            failure_id=failure_id,
            reproduction_id=reproduction_id,
            reproduction_status=reproduction_status,
            allowed_paths=allowed_paths,
            protected_artifacts=protected_artifacts,
            required_behavior=required_behavior,
            verification_commands=verification_commands,
            required_checks=required_checks,
            research_claim_refs=research_claim_refs,
            stop_conditions=stop_conditions,
            expires_at=expires_at,
        )

    def _content_payload(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "repository": self.repository,
            "pull_request_number": self.pull_request_number,
            "head_sha": self.head_sha,
            "observation_id": self.observation_id,
            "failure_id": self.failure_id,
            "reproduction_id": self.reproduction_id,
            "reproduction_status": self.reproduction_status,
            "allowed_paths": list(self.allowed_paths),
            "protected_artifacts": [
                item.to_dict() for item in self.protected_artifacts
            ],
            "required_behavior": list(self.required_behavior),
            "verification_commands": [
                list(item) for item in self.verification_commands
            ],
            "required_checks": list(self.required_checks),
            "research_claim_refs": list(self.research_claim_refs),
            "stop_conditions": list(self.stop_conditions),
            "expires_at": self.expires_at,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self._content_payload(),
            "contract_id": self.contract_id,
            "content_sha256": self.content_sha256,
        }

    @classmethod
    def from_dict(cls, value: object) -> Self:
        data = _object(value, "repair_contract")
        if data.get("schema_version") != 1:
            raise ContractError("unsupported repair_contract schema_version")
        protected = data.get("protected_artifacts")
        if not isinstance(protected, list):
            raise ContractError("repair_contract.protected_artifacts must be an array")
        allowed_paths = _string_tuple(data.get("allowed_paths"), "allowed_paths")
        behavior = _string_tuple(data.get("required_behavior"), "required_behavior")
        checks = _string_tuple(data.get("required_checks"), "required_checks")
        claim_refs = _string_tuple(
            data.get("research_claim_refs"), "research_claim_refs"
        )
        stops = _string_tuple(data.get("stop_conditions"), "stop_conditions")
        raw_commands = data.get("verification_commands")
        if not isinstance(raw_commands, list):
            raise ContractError(
                "repair_contract.verification_commands must be an array"
            )
        commands = tuple(
            _string_tuple(item, "verification_command") for item in raw_commands
        )
        status = data.get("reproduction_status")
        if status not in {
            "reproduced",
            "not_reproduced",
            "not_comparable",
            "not_executable_locally",
            "execution_error",
        }:
            raise ContractError("repair_contract.reproduction_status is unsupported")
        return cls(
            contract_id=_string(data.get("contract_id"), "repair_contract.contract_id"),
            content_sha256=_string(
                data.get("content_sha256"), "repair_contract.content_sha256"
            ),
            repository=_string(data.get("repository"), "repair_contract.repository"),
            pull_request_number=_positive_int(
                data.get("pull_request_number"), "repair_contract.pull_request_number"
            ),
            head_sha=_string(data.get("head_sha"), "repair_contract.head_sha"),
            observation_id=_string(
                data.get("observation_id"), "repair_contract.observation_id"
            ),
            failure_id=_string(data.get("failure_id"), "repair_contract.failure_id"),
            reproduction_id=_string(
                data.get("reproduction_id"), "repair_contract.reproduction_id"
            ),
            reproduction_status=status,
            allowed_paths=allowed_paths,
            protected_artifacts=tuple(
                ProtectedArtifact.from_dict(item) for item in protected
            ),
            required_behavior=behavior,
            verification_commands=commands,
            required_checks=checks,
            research_claim_refs=claim_refs,
            stop_conditions=stops,
            expires_at=_string(data.get("expires_at"), "repair_contract.expires_at"),
        )


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _validate_repository_path(value: str, name: str) -> None:
    if (
        not isinstance(value, str)
        or not value.strip()
        or value.startswith("/")
        or "\\" in value
        or ":" in value
        or any(ord(char) < 32 for char in value)
        or any(part in {"", ".", ".."} for part in value.split("/"))
        or any(char in value for char in "*?[]")
    ):
        raise ContractError(f"{name} must be a normalized exact repository path")


def _validate_unique_paths(paths: list[str] | tuple[str, ...], name: str) -> None:
    if len(set(paths)) != len(paths):
        raise ContractError(f"{name} must not contain duplicate paths")
    for path in paths:
        _validate_repository_path(path, name)


def _repository_paths_overlap(left: str, right: str) -> bool:
    return (
        left == right
        or left == "."
        or right == "."
        or left.startswith(right + "/")
        or right.startswith(left + "/")
    )


def _validate_text_items(items: tuple[str, ...], name: str, maximum: int) -> None:
    if (
        not isinstance(items, tuple)
        or not 1 <= len(items) <= maximum
        or any(not isinstance(item, str) or not item.strip() for item in items)
    ):
        raise ContractError(f"{name} must contain 1 to {maximum} non-empty strings")


def _string_tuple(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ContractError(f"repair_contract.{name} must be an array of strings")
    return tuple(value)
