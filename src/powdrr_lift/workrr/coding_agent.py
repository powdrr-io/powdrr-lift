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
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, cast

from powdrr_lift.core.execution_plan import ExecutionPlan, ExecutionUnit
from powdrr_lift.core.implementation_packet import ImplementationPacket
from powdrr_lift.core.intent_packet import IntentPacket
from powdrr_lift.minisweagent_monitor import run_minisweagent
from powdrr_lift.opencode_monitor import run_opencode

CODING_AGENT_REQUEST_SCHEMA_VERSION = "implementation-request-v2"
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
    intent_packet: IntentPacket | None = None
    implementation_packet: ImplementationPacket | None = None
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
            "intent_packet": (
                self.intent_packet.to_data() if self.intent_packet is not None else None
            ),
            "implementation_packet": (
                self.implementation_packet.to_data()
                if self.implementation_packet is not None
                else None
            ),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_data(), indent=2, sort_keys=True) + "\n"

    def repair_prompt(self, issue: Mapping[str, Any]) -> str:
        """Render a finding-specific prompt without replaying the full packet.

        The original implementation handoff intentionally contains the full
        operation contract.  Reusing that packet for repair is harmful: the
        packet's preservation criteria are already expanded from the feature
        obligations, tests, and active intents, so rendering it again makes a
        repair request substantially larger and gives the coding agent several
        competing representations of the same requirements.  Repair is a
        targeted operation; the durable request and evidence artifacts remain
        available to Workrr, while the model sees only the failed finding and
        the boundaries needed to fix it.
        """
        observed_issue = json.dumps(dict(issue), indent=2, sort_keys=True, default=str)
        paths = ", ".join(self.allowed_paths) or "none declared"
        commands = _render_allowed_command_forms(self.allowed_commands)
        return (
            "Repair only the reported issue in the existing worktree, then stop.\n"
            "Do not re-plan the feature or revisit unrelated changes.\n\n"
            f"Feature objective:\n{self.objective}\n\n"
            "Observed issue:\n"
            f"{observed_issue}\n\n"
            f"Allowed durable paths: {paths}\n"
            f"{commands}\n"
            "Change only what is necessary to resolve this finding. Do not add "
            "new product behavior, tests, dependencies, or files unless the "
            "finding explicitly requires them. Do not prepend environment "
            "variables, `cd`, pipes, redirects, or unapproved flags to a "
            "validation command. The surrounding workflow owns the full "
            "validation profiles; if "
            "you validate locally, run only a focused selector using one of the "
            "listed command forms. The surrounding workflow will rerun the "
            "affected validators "
            "and invalidate evidence affected by your diff."
        )

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
            intent_packet=(
                IntentPacket.from_data(cast(Mapping[str, Any], packet))
                if isinstance(packet := data.get("intent_packet"), Mapping)
                else None
            ),
            implementation_packet=(
                ImplementationPacket.from_data(cast(Mapping[str, Any], packet))
                if isinstance(packet := data.get("implementation_packet"), Mapping)
                else None
            ),
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
        implementation_packet: ImplementationPacket | None = None,
    ) -> ImplementationRequest:
        """Compile one validated execution unit into a worker handoff."""
        criteria = (
            "\n".join(f"- {criterion}" for criterion in unit.acceptance_criteria)
            or "- No acceptance criteria declared."
        )
        validation_profiles = ", ".join(unit.validation_profiles) or "none"
        allowed_paths = ", ".join(unit.paths) or "none"
        ephemeral_paths = ", ".join(unit.ephemeral_paths) or "none"
        allowed_commands_text = _render_allowed_command_forms(allowed_commands)
        intent_packet = IntentPacket(
            operation_id=unit.unit_id,
            required_operations=tuple(
                {"kind": "addition", "change": dict(change)}
                for change in unit.planned_additions
            )
            + tuple(
                {"kind": "deletion", "change": dict(change)}
                for change in unit.planned_deletions
            ),
            must_preserve=unit.must_preserve or unit.acceptance_criteria,
            non_goals=unit.non_goals
            or ("Do not implement unplanned product behavior.",),
            source_refs=tuple(dict.fromkeys((*unit.source_refs, *context_refs))),
            selection_explanations=(
                "Required operations come from the selected Structrr execution unit.",
                "Preservation constraints come from the unit acceptance contract.",
            ),
        )
        implementation_packet_text = (
            implementation_packet.render() if implementation_packet is not None else ""
        )
        product_changes = ""
        if unit.planned_additions or unit.planned_deletions:
            product_changes = (
                "\nRequired product changes:\n"
                f"Additions: {_format_planned_changes(unit.planned_additions)}\n"
                f"Deletions: {_format_planned_changes(unit.planned_deletions)}\n"
            )
        non_goals = (
            "\nNon-goals:\n" + "\n".join(f"- {item}" for item in unit.non_goals) + "\n"
            if unit.non_goals
            else ""
        )
        validation_contract = (
            f"{implementation_packet_text}\n"
            if implementation_packet is not None
            else f"Acceptance criteria:\n{criteria}\n"
        )
        worker_policy = (
            f"Allowed paths: {allowed_paths}\n"
            "Ephemeral paths (removed after the attempt): "
            f"{ephemeral_paths}\n"
            f"Validation profiles that will run: {validation_profiles}\n\n"
            f"{allowed_commands_text}\n"
            "Validation command rules: use a discovered command prefix from the "
            "list. Do not prepend environment variables, "
            "`cd`, pipes, redirects, or unapproved flags. Run focused tests "
            "only; the surrounding workflow owns the full validation profile.\n\n"
            "Work in the existing task worktree. Do not create branches, "
            "commits, pull requests, or generated repository metadata. Use only "
            "the allowed paths or declared ephemeral paths. Temporary helpers "
            "are permitted only in declared ephemeral paths. Stay in the current "
            "working directory; do not cd to, inspect, or select sibling "
            "worktrees or paths outside it. Do not alter files outside the "
            "request."
        )
        product_objective = (
            implementation_packet.objective
            if implementation_packet is not None
            else unit.objective
        )
        prompt = (
            "Product contract:\n"
            f"Implement this feature: {product_objective}{product_changes}{non_goals}\n"
            "Validation contract:\n"
            f"{validation_contract}\n"
            "Worker policy:\n"
            f"{worker_policy}"
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
            intent_packet=intent_packet,
            implementation_packet=implementation_packet,
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
        implementation_packet: ImplementationPacket | None = None,
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
            implementation_packet=implementation_packet,
        )


def _format_planned_changes(changes: Sequence[Mapping[str, Any]]) -> str:
    if not changes:
        return "- None declared. Do not invent additional product changes."
    return "\n".join(
        f"- {json.dumps(dict(change), sort_keys=True, ensure_ascii=False)}"
        for change in changes
    )


def _render_allowed_command_forms(commands: Sequence[str]) -> str:
    """Render discovered command prefixes without inventing command syntax."""
    if not commands:
        return "No validation command is available to the coding agent."
    return "Discovered validation command prefixes:\n" + "\n".join(
        f"- {command}" for command in commands
    )


class CodingAgentStatus(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    POLICY_DENIED = "policy_denied"


class CodingAgentOutcome(StrEnum):
    """Deterministic classification used to choose bounded recovery."""

    COMPLETED_WITH_PROGRESS = "completed_with_progress"
    COMPLETED_WITHOUT_PROGRESS = "completed_without_progress"
    TIMED_OUT_WITH_PARTIAL_PROGRESS = "timed_out_with_partial_progress"
    TIMED_OUT_WITHOUT_PROGRESS = "timed_out_without_progress"
    POLICY_VIOLATION = "policy_violation"
    FAILED = "failed"


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


def classify_coding_agent_attempt(
    attempt: CodingAgentAttempt,
    *,
    previous_diff_fingerprint: str | None = None,
) -> CodingAgentOutcome:
    """Classify an attempt from process status and observed repository change.

    This is deliberately independent of model output. A changed diff is
    material progress only when it differs from the previous accepted state;
    callers can therefore route a partial timeout into a continuation task
    while routing a repeated no-op into a narrower retry or terminal failure.
    """
    changed = bool(attempt.changed_paths) and (
        attempt.diff_fingerprint is not None
        and attempt.diff_fingerprint != previous_diff_fingerprint
    )
    if attempt.status is CodingAgentStatus.POLICY_DENIED:
        return CodingAgentOutcome.POLICY_VIOLATION
    if attempt.status is CodingAgentStatus.TIMED_OUT:
        return (
            CodingAgentOutcome.TIMED_OUT_WITH_PARTIAL_PROGRESS
            if changed
            else CodingAgentOutcome.TIMED_OUT_WITHOUT_PROGRESS
        )
    if attempt.status is CodingAgentStatus.COMPLETED:
        return (
            CodingAgentOutcome.COMPLETED_WITH_PROGRESS
            if changed
            else CodingAgentOutcome.COMPLETED_WITHOUT_PROGRESS
        )
    return CodingAgentOutcome.FAILED


class CodingAgentProvider(Protocol):
    """Worker interface implemented by supported coding-agent targets."""

    provider_name: str

    def run(
        self, request: ImplementationRequest, *, worktree_root: Path, attempt_id: str
    ) -> subprocess.CompletedProcess[str]: ...


@dataclass(frozen=True, slots=True)
class OpenCodePermissionPolicy:
    """Noninteractive OpenCode permissions for one implementation attempt."""

    allowed_commands: tuple[str, ...] = ()

    def to_data(self) -> dict[str, Any]:
        bash_rules = {
            f"{command.removesuffix(' *').rstrip()} *": "allow"
            for command in self.allowed_commands
        }
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
    absolute_timeout_seconds: float | None = 900.0
    max_events: int | None = 256
    permission_policy: OpenCodePermissionPolicy = field(
        default_factory=OpenCodePermissionPolicy
    )
    model: str | None = None
    diagnostics_root: Path | None = None
    provider_name: str = "opencode"
    session_id: str | None = field(default=None, init=False)
    prompt_prefix: str = ""
    prompt_suffix: str = ""

    def prepare_request(self, request: ImplementationRequest) -> ImplementationRequest:
        return _tailor_request_prompt(request, self.prompt_prefix, self.prompt_suffix)

    def run(
        self, request: ImplementationRequest, *, worktree_root: Path, attempt_id: str
    ) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        # Do not let a caller's activated environment point uv at another
        # checkout when the worker runs inside its own worktree.
        environment.pop("VIRTUAL_ENV", None)
        # Powdrr accepts both DeepInfra spellings; OpenCode's built-in
        # provider uses the API_KEY spelling. Normalize the Harbor/Powdrr
        # environment once before starting implementation or review.
        if environment.get("DEEPINFRA_API_KEY") is None:
            token = environment.get("DEEPINFRA_API_TOKEN")
            if token:
                environment["DEEPINFRA_API_KEY"] = token
        # Some OpenCode integrations resolve the project root from PWD rather
        # than the subprocess cwd. Keep both locations aligned so a worker
        # cannot accidentally inspect or edit the caller's repository.
        environment["PWD"] = str(worktree_root.resolve())
        # OpenCode writes its own application log under the XDG data
        # directory. Keep that state inside the disposable worktree so
        # headless runs do not depend on (or mutate) the invoking home.
        opencode_state = (
            self.diagnostics_root.parent / "opencode-data"
            if self.diagnostics_root is not None
            else worktree_root / ".opencode-data"
        )
        environment["XDG_DATA_HOME"] = str(opencode_state)
        environment["XDG_STATE_HOME"] = str(opencode_state)
        environment["XDG_CACHE_HOME"] = str(
            self.diagnostics_root.parent / "opencode-cache"
            if self.diagnostics_root is not None
            else worktree_root / ".opencode-cache"
        )
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
            log_path=(
                self.diagnostics_root / f"{attempt_id}.ndjson"
                if self.diagnostics_root is not None
                else None
            ),
            cwd=worktree_root,
            env=environment,
            inactivity_timeout=self.timeout_seconds,
            absolute_timeout=self.absolute_timeout_seconds,
            max_events=self.max_events,
        )
        session_id = _extract_opencode_session_id(_json_events(completed.stdout))
        if session_id is not None:
            self.session_id = session_id
        return completed


@dataclass(slots=True)
class MiniSWEAgentProvider:
    """Invoke mini-SWE-agent in noninteractive local mode."""

    executable: str = "mini"
    model: str | None = None
    timeout_seconds: float = 300.0
    diagnostics_root: Path | None = None
    provider_name: str = "minisweagent"
    prompt_prefix: str = ""
    prompt_suffix: str = ""

    def prepare_request(self, request: ImplementationRequest) -> ImplementationRequest:
        return _tailor_request_prompt(request, self.prompt_prefix, self.prompt_suffix)

    def run(
        self, request: ImplementationRequest, *, worktree_root: Path, attempt_id: str
    ) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment.pop("VIRTUAL_ENV", None)
        # mini-swe-agent v2 otherwise launches its first-run interactive setup
        # when the container has no user config. Coding-agent runs are always
        # headless, so mark setup complete and rely on the explicit model and
        # provider credentials supplied by Powdrr.
        environment["MSWEA_CONFIGURED"] = "true"
        if environment.get("DEEPINFRA_API_KEY") is None:
            token = environment.get("DEEPINFRA_API_TOKEN")
            if token:
                environment["DEEPINFRA_API_KEY"] = token
        environment["PWD"] = str(worktree_root.resolve())
        mini_config = _miniswe_builtin_config(self.executable)
        command = [
            self.executable,
        ]
        if mini_config is not None:
            # mini's LocalEnvironment gives its configured cwd precedence over
            # the subprocess cwd. Preserve the built-in defaults, then bind
            # that setting to the task worktree so a global config cannot make
            # the worker inspect a sibling checkout.
            command.extend(
                (
                    "--config",
                    str(mini_config),
                    "--config",
                    f"environment.cwd={worktree_root.resolve()}",
                )
            )
        command.extend(
            [
                "--task",
                request.prompt,
                "--yolo",
                "--exit-immediately",
                # Powdrr owns the wall-clock budget and can continue a partial
                # attempt locally. mini's default dollar limit otherwise exits
                # cleanly in the middle of an implementation.
                "--cost-limit",
                "0",
            ]
        )
        if self.model is not None:
            command.extend(("--model", self.model))
        if self.diagnostics_root is not None:
            self.diagnostics_root.mkdir(parents=True, exist_ok=True)
            command.extend(
                ("--output", str(self.diagnostics_root / f"{attempt_id}.traj.json"))
            )
        completed = run_minisweagent(
            command,
            trajectory_path=(
                self.diagnostics_root / f"{attempt_id}.traj.json"
                if self.diagnostics_root is not None
                else None
            ),
            log_path=(
                self.diagnostics_root / f"{attempt_id}.ndjson"
                if self.diagnostics_root is not None
                else None
            ),
            cwd=worktree_root,
            env=environment,
            timeout_seconds=self.timeout_seconds,
        )
        if completed.returncode == 0 and self.diagnostics_root is not None:
            trajectory_path = self.diagnostics_root / f"{attempt_id}.traj.json"
            exit_status = _mini_trajectory_exit_status(trajectory_path)
            if exit_status is not None and exit_status != "Submitted":
                diagnostic = (
                    "mini-swe-agent did not submit the task; "
                    f"trajectory exit_status={exit_status!r}"
                )
                stderr = f"{completed.stderr}\n{diagnostic}".strip()
                return subprocess.CompletedProcess(
                    completed.args,
                    125,
                    completed.stdout,
                    stderr,
                )
        return completed


def _miniswe_builtin_config(executable: str) -> Path | None:
    """Locate mini's built-in config so cwd overrides retain its defaults."""
    executable_path = Path(shutil.which(executable) or executable).resolve()
    package_root = executable_path.parent.parent / "lib"
    matches = sorted(
        package_root.glob("python*/site-packages/minisweagent/config/mini.yaml")
    )
    return matches[0] if matches else None


def _mini_trajectory_exit_status(path: Path) -> str | None:
    """Read mini's terminal status without trusting its process exit code."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, Mapping):
        return None
    info = data.get("info")
    if not isinstance(info, Mapping):
        return None
    status = info.get("exit_status")
    return status if isinstance(status, str) else None


def build_coding_agent_provider(
    target: str,
    *,
    opencode_executable: str,
    opencode_model: str | None,
    minisweagent_executable: str,
    minisweagent_model: str | None,
    timeout_seconds: float,
    diagnostics_root: Path | None,
    permission_policy: OpenCodePermissionPolicy,
    prompt_prefix: str = "",
    prompt_suffix: str = "",
) -> CodingAgentProvider:
    """Build the configured provider behind the common coding-agent boundary."""
    if target == "opencode":
        return OpenCodeProvider(
            executable=opencode_executable,
            model=opencode_model,
            timeout_seconds=timeout_seconds,
            diagnostics_root=diagnostics_root,
            permission_policy=permission_policy,
            prompt_prefix=prompt_prefix,
            prompt_suffix=prompt_suffix,
        )
    if target == "minisweagent":
        return MiniSWEAgentProvider(
            executable=minisweagent_executable,
            model=minisweagent_model,
            timeout_seconds=timeout_seconds,
            diagnostics_root=diagnostics_root,
            prompt_prefix=prompt_prefix,
            prompt_suffix=prompt_suffix,
        )
    raise ValueError(f"unsupported code agent target {target!r}")


def _tailor_request_prompt(
    request: ImplementationRequest, prefix: str, suffix: str
) -> ImplementationRequest:
    if not prefix and not suffix:
        return request
    return replace(
        request,
        prompt=(f"{prefix.rstrip()}\n\n" if prefix else "")
        + request.prompt
        + (f"\n\n{suffix.strip()}" if suffix else ""),
    )


class CodingAgentAttemptStore:
    """Persist worker requests and observations for one Workrr run."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.requests_root = root / "requests"
        self.attempts_root = root / "attempts"
        self.prompts_root = root / "prompts"

    def save_request(self, request: ImplementationRequest) -> Path:
        path = self._path(self.requests_root, request.request_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(request.to_json(), encoding="utf-8")
        return path

    def save_prompt(
        self, request: ImplementationRequest, *, attempt_id: str, provider: str
    ) -> Path:
        """Persist the exact prompt sent for one attempt without overwriting history."""
        prompt_path = self._path(self.prompts_root, attempt_id, suffix=".txt")
        prompt_path.parent.mkdir(parents=True, exist_ok=True)
        prompt_path.write_text(request.prompt, encoding="utf-8")
        metadata_path = self._path(self.prompts_root, attempt_id)
        metadata_path.write_text(
            json.dumps(
                {
                    "attempt_id": attempt_id,
                    "request_id": request.request_id,
                    "provider": provider,
                    "prompt_path": str(prompt_path.relative_to(self.root)),
                    "prompt_sha256": hashlib.sha256(
                        request.prompt.encode("utf-8")
                    ).hexdigest(),
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        index_path = self.prompts_root / "index.json"
        records: list[dict[str, Any]] = []
        if index_path.exists():
            existing = json.loads(index_path.read_text(encoding="utf-8"))
            if isinstance(existing, list):
                records = [dict(item) for item in existing if isinstance(item, Mapping)]
        records = [item for item in records if item.get("attempt_id") != attempt_id]
        records.append(json.loads(metadata_path.read_text(encoding="utf-8")))
        index_path.write_text(
            json.dumps(
                sorted(records, key=lambda item: str(item["attempt_id"])), indent=2
            )
            + "\n",
            encoding="utf-8",
        )
        return prompt_path

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
    def _path(root: Path, artifact_id: str, *, suffix: str = ".json") -> Path:
        if not artifact_id or Path(artifact_id).name != artifact_id:
            raise ValueError("artifact ids must be simple file names")
        return root / f"{artifact_id}{suffix}"


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
        prepare_request = getattr(self.provider, "prepare_request", None)
        if callable(prepare_request):
            request = prepare_request(request)
        self.store.save_request(request)
        self.store.save_prompt(
            request, attempt_id=attempt_id, provider=self.provider.provider_name
        )
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
    new_untracked = _untracked_paths(worktree_root) - before_status
    for relative_path in current_paths - before_status:
        if _path_is_allowed(relative_path, request.ephemeral_paths):
            should_remove = True
        else:
            # Validation and agent tooling may create untracked files. Keep
            # the worker boundary structural: newly created untracked files
            # outside the declared scopes are disposable, while tracked
            # out-of-scope edits remain visible for policy enforcement.
            should_remove = relative_path in new_untracked and not _path_is_allowed(
                relative_path, request.allowed_paths
            )
        if not should_remove:
            continue
        target = worktree_root / relative_path
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        elif target.exists() or target.is_symlink():
            target.unlink()


def _untracked_paths(worktree_root: Path) -> set[str]:
    output = _git_output(worktree_root, "ls-files", "--others", "--exclude-standard")
    return {line for line in output.splitlines() if line}


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
