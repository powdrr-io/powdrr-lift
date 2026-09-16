"""Bounded external coding-agent workers for Workrr orchestration.

This module deliberately treats a coding agent as an untrusted worker. Workrr
owns the request, worktree, permission policy, observed diff, and terminal
classification; the worker only attempts the implementation.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from powdrr_lift.core.execution_plan import ExecutionUnit

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
        }

    def to_json(self) -> str:
        return json.dumps(self.to_data(), indent=2, sort_keys=True) + "\n"

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
        prompt = (
            f"Implement execution unit {unit.unit_id}: {unit.objective}\n"
            "Use only the allowed paths. Satisfy every acceptance criterion and "
            "run the declared validation profiles. Do not commit, push, or alter "
            "files outside the request."
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
    timeout_seconds: float = 1800.0
    permission_policy: OpenCodePermissionPolicy = field(
        default_factory=OpenCodePermissionPolicy
    )
    provider_name: str = "opencode"

    def run(
        self, request: ImplementationRequest, *, worktree_root: Path, attempt_id: str
    ) -> subprocess.CompletedProcess[str]:
        del attempt_id
        environment = os.environ.copy()
        environment["OPENCODE_PERMISSION"] = self.permission_policy.to_json()
        command = [
            self.executable,
            "run",
            "--format",
            "json",
            "--agent",
            self.agent,
            request.prompt,
        ]
        try:
            return subprocess.run(
                command,
                cwd=worktree_root,
                env=environment,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            stdout = _as_text(error.stdout)
            stderr = _as_text(error.stderr)
            return subprocess.CompletedProcess(command, 124, stdout, stderr)


def run_coding_agent(
    provider: CodingAgentProvider,
    request: ImplementationRequest,
    *,
    worktree_root: Path,
    attempt_id: str,
) -> CodingAgentAttempt:
    """Run one worker and derive the result from the observed repository state."""
    before_head = _git_output(worktree_root, "rev-parse", "HEAD")
    before_status = _working_paths(worktree_root)
    if before_status:
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
    after_head = _git_output(worktree_root, "rev-parse", "HEAD")
    changed_paths = tuple(sorted(_working_paths(worktree_root)))
    out_of_scope = tuple(
        path
        for path in changed_paths
        if not _path_is_allowed(path, request.allowed_paths)
    )
    fingerprint = _worktree_fingerprint(worktree_root, changed_paths)
    if before_head != after_head or out_of_scope:
        status = CodingAgentStatus.POLICY_DENIED
        error = (
            "worker changed HEAD"
            if before_head != after_head
            else "worker changed paths outside the implementation request"
        )
    elif completed.returncode == 124:
        status, error = CodingAgentStatus.TIMED_OUT, "coding-agent process timed out"
    elif completed.returncode == 0:
        status, error = CodingAgentStatus.COMPLETED, None
    else:
        status, error = CodingAgentStatus.FAILED, "coding-agent process failed"
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
        error=error,
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


def _as_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    return value.decode(errors="replace") if isinstance(value, bytes) else value


__all__ = [
    "CODING_AGENT_ATTEMPT_SCHEMA_VERSION",
    "CODING_AGENT_REQUEST_SCHEMA_VERSION",
    "CodingAgentAttempt",
    "CodingAgentProvider",
    "CodingAgentStatus",
    "ImplementationRequest",
    "OpenCodePermissionPolicy",
    "OpenCodeProvider",
    "run_coding_agent",
]
