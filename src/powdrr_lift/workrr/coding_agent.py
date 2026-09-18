"""Bounded external coding-agent workers for Workrr orchestration.

This module deliberately treats a coding agent as an untrusted worker. Workrr
owns the request, worktree, permission policy, observed diff, and terminal
classification; the worker only attempts the implementation.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, cast

from powdrr_lift.core.execution_plan import ExecutionPlan, ExecutionUnit
from powdrr_lift.opencode_monitor import run_opencode

CODING_AGENT_REQUEST_SCHEMA_VERSION = "implementation-request-v1"
CODING_AGENT_ATTEMPT_SCHEMA_VERSION = "implementation-attempt-v1"


@dataclass(frozen=True, slots=True)
class ImplementationRequest:
    """The bounded, versioned handoff from Workrr to a coding worker."""

    request_id: str
    objective: str
    prompt: str
    base_commit: str
    plan_fingerprint: str
    allowed_paths: tuple[str, ...]
    acceptance_criteria: tuple[str, ...]
    validation_profiles: tuple[str, ...]
    context_refs: tuple[str, ...] = ()
    allowed_commands: tuple[str, ...] = ()
    ephemeral_paths: tuple[str, ...] = ()
    planned_additions: tuple[Mapping[str, Any], ...] = ()
    planned_deletions: tuple[Mapping[str, Any], ...] = ()
    allow_existing_changes: bool = False
    schema_version: str = CODING_AGENT_REQUEST_SCHEMA_VERSION

    def to_data(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "request_id": self.request_id,
            "objective": self.objective,
            "prompt": self.prompt,
            "base_commit": self.base_commit,
            "plan_fingerprint": self.plan_fingerprint,
            "allowed_paths": list(self.allowed_paths),
            "acceptance_criteria": list(self.acceptance_criteria),
            "validation_profiles": list(self.validation_profiles),
            "context_refs": list(self.context_refs),
            "allowed_commands": list(self.allowed_commands),
            "ephemeral_paths": list(self.ephemeral_paths),
            "planned_additions": [dict(item) for item in self.planned_additions],
            "planned_deletions": [dict(item) for item in self.planned_deletions],
            "allow_existing_changes": self.allow_existing_changes,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_data(), indent=2, sort_keys=True) + "\n"

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> ImplementationRequest:
        return cls(
            request_id=cast(str, data["request_id"]),
            objective=cast(str, data["objective"]),
            prompt=cast(str, data["prompt"]),
            base_commit=cast(str, data["base_commit"]),
            plan_fingerprint=cast(str, data["plan_fingerprint"]),
            allowed_paths=tuple(cast(list[str], data["allowed_paths"])),
            acceptance_criteria=tuple(cast(list[str], data["acceptance_criteria"])),
            validation_profiles=tuple(cast(list[str], data["validation_profiles"])),
            context_refs=tuple(cast(list[str], data.get("context_refs", []))),
            allowed_commands=tuple(cast(list[str], data.get("allowed_commands", []))),
            ephemeral_paths=tuple(cast(list[str], data.get("ephemeral_paths", []))),
            planned_additions=tuple(
                cast(list[Mapping[str, Any]], data.get("planned_additions", []))
            ),
            planned_deletions=tuple(
                cast(list[Mapping[str, Any]], data.get("planned_deletions", []))
            ),
            allow_existing_changes=bool(data.get("allow_existing_changes", False)),
            schema_version=cast(str, data["schema_version"]),
        )

    @classmethod
    def from_execution_unit(
        cls,
        unit: ExecutionUnit,
        *,
        request_id: str,
        base_commit: str,
        plan_fingerprint: str,
        context_refs: tuple[str, ...] = (),
        allowed_commands: tuple[str, ...] = (),
    ) -> ImplementationRequest:
        """Compile one validated execution unit into a worker handoff."""
        criteria = (
            "\n".join(f"- {criterion}" for criterion in unit.acceptance_criteria)
            or "- No acceptance criteria declared."
        )
        validation_profiles = ", ".join(unit.validation_profiles) or "none"
        allowed_paths = ", ".join(unit.paths) or "none"
        ephemeral_paths = ", ".join(unit.ephemeral_paths) or "none"
        planned_additions = _format_planned_changes(unit.planned_additions)
        planned_deletions = _format_planned_changes(unit.planned_deletions)
        prompt = (
            f"Implement execution unit {unit.unit_id}: {unit.objective}\n\n"
            f"Allowed paths: {allowed_paths}\n"
            "Ephemeral paths (Workrr removes these after the attempt): "
            f"{ephemeral_paths}\n"
            f"Acceptance criteria:\n{criteria}\n"
            "Planned Structrr additions (implement these intentionally):\n"
            f"{planned_additions}\n"
            "Planned Structrr deletions (remove or retire these intentionally):\n"
            f"{planned_deletions}\n"
            f"Validation profiles Workrr will run: {validation_profiles}\n\n"
            "Use only the allowed paths or declared ephemeral paths. Temporary "
            "helpers are permitted only in the declared ephemeral paths; Workrr "
            "removes them before evaluating the durable diff. Workrr runs the "
            "declared validation profiles after you "
            "finish. Do not commit, push, or alter files outside the request."
        )
        return cls(
            request_id=request_id,
            objective=unit.objective,
            prompt=prompt,
            base_commit=base_commit,
            plan_fingerprint=plan_fingerprint,
            allowed_paths=unit.paths,
            acceptance_criteria=unit.acceptance_criteria,
            validation_profiles=unit.validation_profiles,
            context_refs=context_refs,
            allowed_commands=allowed_commands,
            ephemeral_paths=unit.ephemeral_paths,
            planned_additions=unit.planned_additions,
            planned_deletions=unit.planned_deletions,
        )

    @classmethod
    def from_execution_plan(
        cls,
        plan: ExecutionPlan,
        *,
        unit_id: str,
        request_id: str,
        base_commit: str,
        context_refs: tuple[str, ...] = (),
        allowed_commands: tuple[str, ...] = (),
    ) -> ImplementationRequest:
        """Compile one unit from a typed execution plan into a worker handoff."""
        try:
            unit = next(unit for unit in plan.units if unit.unit_id == unit_id)
        except StopIteration as error:
            raise ValueError(f"execution plan has no unit {unit_id!r}") from error
        return cls.from_execution_unit(
            unit,
            request_id=request_id,
            base_commit=base_commit,
            plan_fingerprint=plan.proposed_pr_fingerprint,
            context_refs=context_refs,
            allowed_commands=allowed_commands,
        )


def _format_planned_changes(changes: Sequence[Mapping[str, Any]]) -> str:
    if not changes:
        return "- None declared. Do not invent additional product changes."
    return "\n".join(
        f"- {json.dumps(dict(change), sort_keys=True, ensure_ascii=False)}"
        for change in changes
    )


class CodingAgentStatus(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    POLICY_DENIED = "policy_denied"


@dataclass(frozen=True, slots=True)
class CodingAgentAttempt:
    """Observed result of one bounded worker invocation."""

    attempt_id: str
    request_id: str
    provider: str
    status: CodingAgentStatus
    exit_code: int | None
    changed_paths: tuple[str, ...] = ()
    out_of_scope_paths: tuple[str, ...] = ()
    diff_fingerprint: str | None = None
    events: tuple[Mapping[str, Any], ...] = ()
    stdout: str = ""
    stderr: str = ""
    error: str | None = None
    schema_version: str = CODING_AGENT_ATTEMPT_SCHEMA_VERSION

    def to_data(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "attempt_id": self.attempt_id,
            "request_id": self.request_id,
            "provider": self.provider,
            "status": self.status.value,
            "exit_code": self.exit_code,
            "changed_paths": list(self.changed_paths),
            "out_of_scope_paths": list(self.out_of_scope_paths),
            "diff_fingerprint": self.diff_fingerprint,
            "events": [dict(event) for event in self.events],
            "stdout": self.stdout,
            "stderr": self.stderr,
            "error": self.error,
        }


class CodingAgentProvider(Protocol):
    """Worker interface implemented by OpenCode and deterministic test agents."""

    provider_name: str

    def run(
        self, request: ImplementationRequest, *, worktree_root: Path, attempt_id: str
    ) -> subprocess.CompletedProcess[str]: ...


@dataclass(frozen=True, slots=True)
class OpenCodePermissionPolicy:
    """Noninteractive OpenCode permissions for one implementation attempt."""

    allowed_commands: tuple[str, ...] = ()

    def to_data(self) -> dict[str, Any]:
        bash_rules = {command: "allow" for command in self.allowed_commands}
        bash_rules.update(
            {
                "git commit *": "deny",
                "git push *": "deny",
                "gh *": "deny",
            }
        )
        return {
            "*": "deny",
            "read": "allow",
            "edit": "allow",
            "bash": bash_rules,
            "task": "deny",
            "question": "deny",
            "external_directory": "deny",
            "webfetch": "deny",
        }

    def to_json(self) -> str:
        return json.dumps(self.to_data(), sort_keys=True)


@dataclass(slots=True)
class OpenCodeProvider:
    """Invoke OpenCode in headless JSON-event mode inside a worktree."""

    executable: str = "opencode"
    agent: str = "build"
    timeout_seconds: float = 300.0
    permission_policy: OpenCodePermissionPolicy = field(
        default_factory=OpenCodePermissionPolicy
    )
    model: str | None = None
    provider_name: str = "opencode"
    session_id: str | None = field(default=None, init=False)

    def run(
        self, request: ImplementationRequest, *, worktree_root: Path, attempt_id: str
    ) -> subprocess.CompletedProcess[str]:
        del attempt_id
        environment = os.environ.copy()
        # Do not let a caller's activated environment point uv at another
        # checkout when the worker runs inside its own worktree.
        environment.pop("VIRTUAL_ENV", None)
        # Some OpenCode integrations resolve the project root from PWD rather
        # than the subprocess cwd. Keep both locations aligned so a worker
        # cannot accidentally inspect or edit the caller's repository.
        environment["PWD"] = str(worktree_root.resolve())
        environment["OPENCODE_PERMISSION"] = self.permission_policy.to_json()
        command = [
            self.executable,
            "run",
            "--format",
            "json",
            "--agent",
            self.agent,
        ]
        if self.model is not None:
            command.extend(("--model", self.model))
        if self.session_id is not None:
            command.extend(("--session", self.session_id))
        command.append(request.prompt)
        completed = run_opencode(
            command,
            log_path=None,
            cwd=worktree_root,
            env=environment,
            inactivity_timeout=self.timeout_seconds,
        )
        session_id = _extract_opencode_session_id(_json_events(completed.stdout))
        if session_id is not None:
            self.session_id = session_id
        return completed


class CodingAgentAttemptStore:
    """Persist worker requests and observations for one Workrr run."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.requests_root = root / "requests"
        self.attempts_root = root / "attempts"

    def save_request(self, request: ImplementationRequest) -> Path:
        path = self._path(self.requests_root, request.request_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(request.to_json(), encoding="utf-8")
        return path

    def save_attempt(self, attempt: CodingAgentAttempt) -> Path:
        path = self._path(self.attempts_root, attempt.attempt_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(attempt.to_data(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return path

    def save_validation_report(self, report: Any) -> Path:
        path = self._path(self.root / "validations", report.attempt_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(report.to_data(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return path

    def load_validation_report(self, attempt_id: str) -> Any:
        from powdrr_lift.workrr.coding_agent_validation import (
            validation_report_from_data,
        )

        data = json.loads(
            self._path(self.root / "validations", attempt_id).read_text(
                encoding="utf-8"
            )
        )
        if not isinstance(data, dict):
            raise ValueError("validation report artifact must contain an object")
        return validation_report_from_data(data)

    def load_request(self, request_id: str) -> ImplementationRequest:
        data = json.loads(
            self._path(self.requests_root, request_id).read_text(encoding="utf-8")
        )
        if not isinstance(data, dict):
            raise ValueError("implementation request artifact must contain an object")
        return ImplementationRequest.from_data(data)

    def load_attempt(self, attempt_id: str) -> CodingAgentAttempt:
        data = json.loads(
            self._path(self.attempts_root, attempt_id).read_text(encoding="utf-8")
        )
        if not isinstance(data, dict):
            raise ValueError("implementation attempt artifact must contain an object")
        return CodingAgentAttempt(
            attempt_id=cast(str, data["attempt_id"]),
            request_id=cast(str, data["request_id"]),
            provider=cast(str, data["provider"]),
            status=CodingAgentStatus(cast(str, data["status"])),
            exit_code=cast(int | None, data["exit_code"]),
            changed_paths=tuple(cast(list[str], data.get("changed_paths", []))),
            out_of_scope_paths=tuple(
                cast(list[str], data.get("out_of_scope_paths", []))
            ),
            diff_fingerprint=cast(str | None, data.get("diff_fingerprint")),
            events=tuple(cast(list[Mapping[str, Any]], data.get("events", []))),
            stdout=cast(str, data.get("stdout", "")),
            stderr=cast(str, data.get("stderr", "")),
            error=cast(str | None, data.get("error")),
            schema_version=cast(str, data["schema_version"]),
        )

    @staticmethod
    def _path(root: Path, artifact_id: str) -> Path:
        if not artifact_id or Path(artifact_id).name != artifact_id:
            raise ValueError("artifact ids must be simple file names")
        return root / f"{artifact_id}.json"


@dataclass(slots=True)
class CodingAgentRunner:
    """Execute and persist one bounded implementation attempt."""

    provider: CodingAgentProvider
    store: CodingAgentAttemptStore

    def run(
        self,
        request: ImplementationRequest,
        *,
        worktree_root: Path,
        attempt_id: str,
    ) -> CodingAgentAttempt:
        self.store.save_request(request)
        attempt = run_coding_agent(
            self.provider,
            request,
            worktree_root=worktree_root,
            attempt_id=attempt_id,
        )
        self.store.save_attempt(attempt)
        return attempt


def run_coding_agent(
    provider: CodingAgentProvider,
    request: ImplementationRequest,
    *,
    worktree_root: Path,
    attempt_id: str,
) -> CodingAgentAttempt:
    """Run one worker and derive the result from the observed repository state."""
    before_head = _git_output(worktree_root, "rev-parse", "HEAD")
    if before_head != request.base_commit:
        return CodingAgentAttempt(
            attempt_id,
            request.request_id,
            provider.provider_name,
            CodingAgentStatus.POLICY_DENIED,
            None,
            error=(
                "worktree HEAD does not match implementation request base commit: "
                f"expected {request.base_commit}, found {before_head}"
            ),
        )
    before_status = _working_paths(worktree_root)
    if before_status and not request.allow_existing_changes:
        return CodingAgentAttempt(
            attempt_id,
            request.request_id,
            provider.provider_name,
            CodingAgentStatus.POLICY_DENIED,
            None,
            error="coding-agent worktree must be clean before an attempt",
        )
    try:
        completed = provider.run(
            request, worktree_root=worktree_root, attempt_id=attempt_id
        )
    except OSError as error:
        return CodingAgentAttempt(
            attempt_id=attempt_id,
            request_id=request.request_id,
            provider=provider.provider_name,
            status=CodingAgentStatus.FAILED,
            exit_code=None,
            error=f"could not start coding-agent provider: {error}",
        )
    _remove_ephemeral_paths(worktree_root, request, before_status)
    after_head = _git_output(worktree_root, "rev-parse", "HEAD")
    changed_paths = tuple(sorted(_working_paths(worktree_root)))
    out_of_scope = tuple(
        path
        for path in changed_paths
        if not _path_is_allowed(
            path, (*request.allowed_paths, *request.ephemeral_paths)
        )
    )
    fingerprint = _worktree_fingerprint(worktree_root, changed_paths)
    if before_head != after_head or out_of_scope:
        status = CodingAgentStatus.POLICY_DENIED
        terminal_error = (
            "worker changed HEAD"
            if before_head != after_head
            else "worker changed paths outside the implementation request"
        )
    elif completed.returncode == 124:
        status, terminal_error = (
            CodingAgentStatus.TIMED_OUT,
            "coding-agent process timed out",
        )
    elif completed.returncode == 0:
        status, terminal_error = CodingAgentStatus.COMPLETED, None
    else:
        status, terminal_error = CodingAgentStatus.FAILED, "coding-agent process failed"
    return CodingAgentAttempt(
        attempt_id=attempt_id,
        request_id=request.request_id,
        provider=provider.provider_name,
        status=status,
        exit_code=completed.returncode,
        changed_paths=changed_paths,
        out_of_scope_paths=out_of_scope,
        diff_fingerprint=fingerprint,
        events=_json_events(completed.stdout),
        stdout=completed.stdout,
        stderr=completed.stderr,
        error=terminal_error,
    )


def _git_output(worktree_root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=worktree_root,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _working_paths(worktree_root: Path) -> set[str]:
    paths: set[str] = set()
    for arguments in (
        ("diff", "--name-only"),
        ("diff", "--cached", "--name-only"),
        ("ls-files", "--others", "--exclude-standard"),
    ):
        output = _git_output(worktree_root, *arguments)
        paths.update(line for line in output.splitlines() if line)
    return paths


def _path_is_allowed(path: str, allowed_paths: Sequence[str]) -> bool:
    candidate = Path(path)
    return any(
        scope == "." or candidate == Path(scope) or Path(scope) in candidate.parents
        for scope in allowed_paths
    )


def _remove_ephemeral_paths(
    worktree_root: Path,
    request: ImplementationRequest,
    before_status: set[str],
) -> None:
    """Remove worker-created ephemeral artifacts before final diff evaluation."""
    current_paths = _working_paths(worktree_root)
    for relative_path in current_paths - before_status:
        if not _path_is_allowed(relative_path, request.ephemeral_paths):
            continue
        target = worktree_root / relative_path
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        elif target.exists() or target.is_symlink():
            target.unlink()


def _worktree_fingerprint(worktree_root: Path, paths: Sequence[str]) -> str:
    digest = hashlib.sha256()
    for relative_path in paths:
        path = worktree_root / relative_path
        digest.update(relative_path.encode())
        digest.update(b"\0")
        if path.is_file():
            digest.update(path.read_bytes())
    return digest.hexdigest()


def _json_events(stdout: str) -> tuple[Mapping[str, Any], ...]:
    events: list[Mapping[str, Any]] = []
    for line in stdout.splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, Mapping):
            events.append(dict(value))
    return tuple(events)


def _extract_opencode_session_id(
    events: Sequence[Mapping[str, Any]],
) -> str | None:
    """Extract the resumable session id from OpenCode lifecycle events."""
    for event in events:
        event_type = event.get("type")
        if not isinstance(event_type, str) or not event_type.startswith("session."):
            continue
        candidate = _find_session_id(event)
        if candidate is not None:
            return candidate
    return None


def _find_session_id(value: Any) -> str | None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            normalized = str(key).replace("-", "_").lower()
            if normalized in {"sessionid", "session_id", "session"} and isinstance(
                nested, str
            ):
                return nested
            if key == "id" and isinstance(nested, str):
                return nested
            found = _find_session_id(nested)
            if found is not None:
                return found
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for nested in value:
            found = _find_session_id(nested)
            if found is not None:
                return found
    return None


__all__ = [
    "CODING_AGENT_ATTEMPT_SCHEMA_VERSION",
    "CODING_AGENT_REQUEST_SCHEMA_VERSION",
    "CodingAgentAttempt",
    "CodingAgentAttemptStore",
    "CodingAgentProvider",
    "CodingAgentStatus",
    "CodingAgentRunner",
    "ImplementationRequest",
    "OpenCodePermissionPolicy",
    "OpenCodeProvider",
    "run_coding_agent",
]
