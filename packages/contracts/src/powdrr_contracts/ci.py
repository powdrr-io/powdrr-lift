"""Public records for CI observations and local failure reproduction."""

from __future__ import annotations

from dataclasses import dataclass
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
        if len(self.raw_sha256) != 64 or any(
            char not in "0123456789abcdef" for char in self.raw_sha256
        ):
            raise ContractError("ci_annotation.raw_sha256 must be a SHA-256 digest")

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
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise ContractError("ci_annotation.raw_sha256 must be a SHA-256 digest")
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
    runner_name: str | None = None
    runner_id: int | None = None
    runner_group_name: str | None = None
    runner_labels: tuple[str, ...] = ()
    steps: tuple[dict[str, Any], ...] = ()
    annotations: tuple[CIAnnotation, ...] = ()

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
        if self.runner_id is not None and (
            not isinstance(self.runner_id, int)
            or isinstance(self.runner_id, bool)
            or self.runner_id < 0
        ):
            raise ContractError(
                "ci_job.runner_id must be a non-negative integer or null"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "job_id": self.job_id,
            "run_id": self.run_id,
            "run_attempt": self.run_attempt,
            "head_sha": self.head_sha,
            "status": self.status,
            "conclusion": self.conclusion,
            "runner_name": self.runner_name,
            "runner_id": self.runner_id,
            "runner_group_name": self.runner_group_name,
            "runner_labels": list(self.runner_labels),
            "steps": list(self.steps),
            "annotations": [item.to_dict() for item in self.annotations],
        }


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
