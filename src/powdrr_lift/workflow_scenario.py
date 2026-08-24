"""Deterministic, isolated scenarios for workflow skill definitions.

Scenarios deliberately reuse the production chat execution strategy.  The only
substitution is the LLM transport: a scripted client supplies checked-in JSON
responses, so CI never needs provider credentials or network access.
"""

from __future__ import annotations

import io
import json
import shutil
import subprocess
import tempfile
from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from powdrr_lift.core import load_skill, resolve_repo_root
from powdrr_lift.intrinsic_git_gh import GH_TOOL, intrinsic_command
from powdrr_lift.workflow_chat_agent import (
    LLMProviderRoles,
    SkillCatalogEntry,
    SkillChatConfig,
    SkillChatSelection,
    _ChatWorkflowExecutionStrategy,
    _workflow_action_signature,
    _WorkflowExecutionState,
    _WorkflowProgressDisplay,
)
from powdrr_lift.workflow_llm import WorkflowLLMExecutionDriver

WORKFLOW_SCENARIO_SCHEMA_VERSION = 1


class WorkflowScenarioError(ValueError):
    """Raised when a scenario is malformed or cannot be run safely."""


@dataclass(frozen=True, slots=True)
class WorkflowScenarioResult:
    """Machine-readable result of one isolated scripted scenario."""

    scenario_id: str
    definition: str
    status: str
    assertions: tuple[dict[str, Any], ...]
    execution_events: tuple[dict[str, Any], ...]
    audit_events: tuple[dict[str, Any], ...]
    roundtrips: int
    worktree_root: Path | None = None

    def to_data(self) -> dict[str, Any]:
        return {
            "schema_version": WORKFLOW_SCENARIO_SCHEMA_VERSION,
            "scenario_id": self.scenario_id,
            "definition": self.definition,
            "status": self.status,
            "assertions": list(self.assertions),
            "execution_events": list(self.execution_events),
            "audit_events": list(self.audit_events),
            "roundtrips": self.roundtrips,
            "worktree_root": str(self.worktree_root) if self.worktree_root else None,
        }


class _ScriptedWorkflowClient:
    def __init__(self, responses: Sequence[Mapping[str, Any]]) -> None:
        self._responses = iter(dict(response) for response in responses)
        self.messages: list[list[dict[str, str]]] = []

    def complete_json(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        self.messages.append([dict(message) for message in messages])
        try:
            return next(self._responses)
        except StopIteration as exc:
            raise RuntimeError(
                "Scenario exhausted scripted responses before the workflow completed."
            ) from exc


def load_workflow_scenario(path: Path) -> dict[str, Any]:
    """Load and validate a versioned YAML or JSON scenario document."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise WorkflowScenarioError(f"Could not read scenario {path}: {exc}") from exc
    try:
        data = json.loads(raw) if path.suffix == ".json" else yaml.safe_load(raw)
    except (json.JSONDecodeError, yaml.YAMLError) as exc:
        raise WorkflowScenarioError(f"Could not parse scenario {path}: {exc}") from exc
    if not isinstance(data, Mapping):
        raise WorkflowScenarioError("scenario must decode to an object.")
    scenario = dict(data)
    _validate_scenario(scenario)
    return scenario


def run_workflow_scenario(
    scenario: Mapping[str, Any],
    *,
    scenario_path: Path,
    repo_root: Path | None = None,
    keep_failed: bool = False,
) -> WorkflowScenarioResult:
    """Run one scripted skill scenario in a fresh temporary Git repository."""
    _validate_scenario(scenario)
    source_root = resolve_repo_root(repo_root)
    scenario_id = _required_text(scenario.get("id"), "scenario id")
    definition_path = _resolve_path(
        _required_text(scenario.get("definition"), "scenario definition"),
        source_root,
    )
    if not definition_path.is_file():
        raise WorkflowScenarioError(
            f"Scenario definition does not exist: {definition_path}"
        )
    provider = _mapping(scenario.get("provider"), "scenario provider")
    responses = _mapping_sequence(
        provider.get("responses"), "scenario provider.responses"
    )
    fixture = scenario.get("fixture")
    fixture_path = (
        _resolve_path(fixture, scenario_path.parent)
        if isinstance(fixture, str) and fixture
        else None
    )
    if fixture_path is not None and not fixture_path.is_dir():
        raise WorkflowScenarioError(f"Scenario fixture does not exist: {fixture_path}")

    temporary_root = Path(tempfile.mkdtemp(prefix="powdrr-lift-scenario-"))
    worktree_root = temporary_root / "repository"
    try:
        _build_fixture_repository(worktree_root, fixture_path)
        execution = _run_scripted_skill(
            definition_path=definition_path,
            worktree_root=worktree_root,
            root_intent=_required_text(scenario.get("request"), "scenario request"),
            responses=responses,
            max_roundtrips=_optional_positive_int(
                _mapping(scenario.get("expect"), "scenario expect").get(
                    "max_roundtrips"
                ),
                default=max(1, len(responses) + 1),
            ),
        )
        assertions = _evaluate_assertions(
            _mapping(scenario.get("expect"), "scenario expect"),
            skill=execution.skill,
            exit_code=execution.exit_code,
            execution_events=execution.execution_events,
            audit_events=execution.audit_events,
            roundtrips=execution.roundtrips,
            worktree_root=worktree_root,
        )
        status = "passed" if all(item["passed"] for item in assertions) else "failed"
        retained_root = worktree_root if status == "failed" and keep_failed else None
        return WorkflowScenarioResult(
            scenario_id=scenario_id,
            definition=str(definition_path),
            status=status,
            assertions=tuple(assertions),
            execution_events=tuple(execution.execution_events),
            audit_events=tuple(execution.audit_events),
            roundtrips=execution.roundtrips,
            worktree_root=retained_root,
        )
    finally:
        if not keep_failed or not (
            worktree_root.exists() and _scenario_failed_marker(worktree_root)
        ):
            shutil.rmtree(temporary_root, ignore_errors=True)


@dataclass(frozen=True, slots=True)
class _ScriptedSkillExecution:
    skill: Any
    exit_code: int
    execution_events: list[dict[str, Any]]
    audit_events: list[dict[str, Any]]
    roundtrips: int


def _run_scripted_skill(
    *,
    definition_path: Path,
    worktree_root: Path,
    root_intent: str,
    responses: Sequence[Mapping[str, Any]],
    max_roundtrips: int,
) -> _ScriptedSkillExecution:
    skill = load_skill(definition_path)
    _validate_scripted_responses(responses)
    entry = SkillCatalogEntry(definition_path, skill)
    client = _ScriptedWorkflowClient(responses)
    config = SkillChatConfig(
        skills_dir=definition_path.parent,
        repo_root=worktree_root,
        provider="local",
        model="scripted",
        max_stalled_roundtrips=2,
    )
    state = _WorkflowExecutionState(
        selected_skill=entry,
        root_skill=entry,
        transcript=[{"role": "user", "content": root_intent}],
        execution_events=[],
        audit_events=[],
        execution_context=[],
        step_index=0,
        worktree_root=worktree_root,
        error_log_root=worktree_root,
    )
    stderr = io.StringIO()
    strategy = _ChatWorkflowExecutionStrategy(
        config=config,
        selection=SkillChatSelection(
            selected_skill_path=definition_path,
            selected_skill_reason="scripted scenario",
            ready_to_execute=True,
        ),
        catalog=(entry,),
        workflow_context=None,
        state=state,
        progress=_WorkflowProgressDisplay(stderr),
        input_func=lambda: _raise_human_input_requested(),
        stdout=io.StringIO(),
        stderr=stderr,
        client_for_model=lambda _model, _provider: client,
        provider_roles=LLMProviderRoles(normal="local"),
        provider_role="normal",
        current_model="scripted",
        provider="local",
        driver=WorkflowLLMExecutionDriver(max_stalled_roundtrips=2),
    )
    with _stub_github_intrinsic():
        try:
            exit_code = strategy.driver.run(
                strategy,
                max_roundtrips=max_roundtrips,
                signature=_workflow_action_signature,
            )
        except RuntimeError as exc:
            state.audit_events.append(
                {
                    "kind": "execution_failed",
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "step_index": state.step_index,
                }
            )
            exit_code = 1
    return _ScriptedSkillExecution(
        skill=skill,
        exit_code=exit_code,
        execution_events=state.execution_events,
        audit_events=state.audit_events,
        roundtrips=len(client.messages),
    )


def _validate_scripted_responses(responses: Sequence[Mapping[str, Any]]) -> None:
    """Reject direct network commands before they can run in an isolated fixture."""
    network_executables = {"curl", "gh", "ssh", "scp", "wget"}
    for index, response in enumerate(responses):
        if response.get("action") != "invoke_tool":
            continue
        tool = response.get("tool")
        parameters = response.get("parameters")
        if tool not in {"shell", "internal"} or not isinstance(parameters, Mapping):
            continue
        command = parameters.get("command")
        if isinstance(command, str):
            executable = command.strip().split(maxsplit=1)[0] if command.strip() else ""
        elif isinstance(command, Sequence) and not isinstance(command, (str, bytes)):
            executable = str(command[0]) if command else ""
        else:
            continue
        if executable in network_executables:
            raise WorkflowScenarioError(
                f"scenario provider.responses[{index}] invokes {executable!r} through "
                "shell; use the structured gh intrinsic, which scenarios stub."
            )


def _build_fixture_repository(destination: Path, fixture: Path | None) -> None:
    if fixture is not None:
        _copy_fixture(fixture, destination)
    else:
        destination.mkdir(parents=True)
    marker = destination / ".scenario-fixture"
    if not any(destination.iterdir()):
        marker.write_text("isolated workflow scenario\n", encoding="utf-8")
    _run_git(destination, "init", "-b", "main")
    _run_git(destination, "config", "user.name", "Workflow Scenario")
    _run_git(destination, "config", "user.email", "workflow-scenario@example.invalid")
    _run_git(destination, "add", ".")
    _run_git(destination, "commit", "-m", "Scenario fixture")


def _copy_fixture(source: Path, destination: Path) -> None:
    for path in source.rglob("*"):
        relative = path.relative_to(source)
        if ".git" in relative.parts:
            continue
        if path.is_symlink():
            raise WorkflowScenarioError(
                f"Scenario fixture may not contain symlinks: {path}"
            )
    shutil.copytree(source, destination, ignore=shutil.ignore_patterns(".git"))


def _run_git(repo_root: Path, *arguments: str) -> None:
    result = subprocess.run(
        ["git", *arguments], cwd=repo_root, capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise WorkflowScenarioError(
            f"Could not initialize scenario repository: git {' '.join(arguments)}: "
            f"{result.stderr.strip()}"
        )


@contextmanager
def _stub_github_intrinsic() -> Iterator[None]:
    """Keep scripted scenarios offline while preserving the GH tool result shape."""
    import powdrr_lift.workflow_chat_agent as workflow_chat_agent

    original = workflow_chat_agent.execute_intrinsic_git_gh_tool

    def execute(
        tool: str, parameters: Mapping[str, Any], *, worktree_root: Path
    ) -> dict[str, Any]:
        if tool != GH_TOOL:
            return original(tool, parameters, worktree_root=worktree_root)
        command = intrinsic_command(parameters, tool=tool)
        stdout = ""
        if parameters.get("operation") == "pr_create":
            stdout = "https://github.com/example/scenario/pull/1\n"
        return {
            "tool": tool,
            "command": ["gh", *command],
            "returncode": 0,
            "stdout": stdout,
            "stderr": "",
            "stubbed": True,
        }

    workflow_chat_agent.execute_intrinsic_git_gh_tool = execute
    try:
        yield
    finally:
        workflow_chat_agent.execute_intrinsic_git_gh_tool = original


def _evaluate_assertions(
    expect: Mapping[str, Any],
    *,
    skill: Any,
    exit_code: int,
    execution_events: Sequence[Mapping[str, Any]],
    audit_events: Sequence[Mapping[str, Any]],
    roundtrips: int,
    worktree_root: Path,
) -> list[dict[str, Any]]:
    assertions: list[dict[str, Any]] = []
    expected_outcome = _optional_text(expect.get("outcome"))
    if expected_outcome is not None:
        actual = "complete" if exit_code == 0 else "failed"
        assertions.append(
            _assert("outcome", actual == expected_outcome, expected_outcome, actual)
        )
    if "max_roundtrips" in expect:
        maximum = _optional_positive_int(expect.get("max_roundtrips"), default=1)
        assertions.append(
            _assert("max_roundtrips", roundtrips <= maximum, maximum, roundtrips)
        )

    visited = [
        skill.steps[event["step_index"]].id
        for event in execution_events
        if isinstance(event.get("step_index"), int)
        and 0 <= event["step_index"] < len(skill.steps)
    ]
    visit_spec = expect.get("visited_steps")
    if isinstance(visit_spec, Mapping):
        ordered = _string_sequence(visit_spec.get("ordered"), "visited_steps.ordered")
        if ordered:
            assertions.append(
                _assert(
                    "visited_steps.ordered",
                    _is_subsequence(ordered, visited),
                    ordered,
                    visited,
                )
            )
        required_steps = _string_sequence(
            visit_spec.get("required"), "visited_steps.required"
        )
        if required_steps:
            assertions.append(
                _assert(
                    "visited_steps.required",
                    all(step in visited for step in required_steps),
                    required_steps,
                    visited,
                )
            )

    action_events = [
        event for event in execution_events if isinstance(event.get("kind"), str)
    ]
    for required_action in _mapping_sequence(
        expect.get("required_actions"), "required_actions"
    ):
        assertions.append(
            _assert(
                "required_actions",
                any(_matches(required_action, event) for event in action_events),
                required_action,
                action_events,
            )
        )
    for forbidden in _mapping_sequence(
        expect.get("forbidden_actions"), "forbidden_actions"
    ):
        assertions.append(
            _assert(
                "forbidden_actions",
                not any(_matches(forbidden, event) for event in action_events),
                forbidden,
                action_events,
            )
        )
    if "max_repeated_action_count" in expect:
        maximum = _optional_non_negative_int(
            expect.get("max_repeated_action_count"), default=0
        )
        fingerprints = Counter(_action_fingerprint(event) for event in action_events)
        repeated = sum(count - 1 for count in fingerprints.values() if count > 1)
        assertions.append(
            _assert("max_repeated_action_count", repeated <= maximum, maximum, repeated)
        )
    for field, should_exist in (("required_files", True), ("forbidden_files", False)):
        for relative_path in _string_sequence(expect.get(field), field):
            exists = (worktree_root / relative_path).is_file()
            assertions.append(
                _assert(field, exists is should_exist, relative_path, exists)
            )
    if expect.get("all_gates_passed") is True:
        failed_gates = [
            event
            for event in execution_events
            if event.get("kind") == "goto_step" and event.get("source") == "gate"
        ]
        assertions.append(
            _assert(
                "all_gates_passed", not failed_gates, "no failed gate", failed_gates
            )
        )
    if not assertions:
        assertions.append(
            _assert(
                "scenario",
                exit_code == 0,
                "complete",
                "complete" if exit_code == 0 else "failed",
            )
        )
    if not all(item["passed"] for item in assertions):
        (worktree_root / ".scenario-failed").write_text("failed\n", encoding="utf-8")
    _ = audit_events
    return assertions


def _scenario_failed_marker(worktree_root: Path) -> bool:
    return (worktree_root / ".scenario-failed").is_file()


def _action_fingerprint(event: Mapping[str, Any]) -> str:
    return json.dumps(
        {
            key: value
            for key, value in event.items()
            if key not in {"result", "decisions_and_context"}
        },
        sort_keys=True,
        default=str,
    )


def _matches(expected: Mapping[str, Any], actual: Mapping[str, Any]) -> bool:
    return all(actual.get(key) == value for key, value in expected.items())


def _is_subsequence(expected: Sequence[str], actual: Sequence[str]) -> bool:
    position = 0
    for item in actual:
        if position < len(expected) and item == expected[position]:
            position += 1
    return position == len(expected)


def _assert(name: str, passed: bool, expected: Any, actual: Any) -> dict[str, Any]:
    return {"name": name, "passed": passed, "expected": expected, "actual": actual}


def _validate_scenario(scenario: Mapping[str, Any]) -> None:
    if scenario.get("schema_version") != WORKFLOW_SCENARIO_SCHEMA_VERSION:
        raise WorkflowScenarioError("scenario schema_version must be 1.")
    _required_text(scenario.get("id"), "scenario id")
    _required_text(scenario.get("definition"), "scenario definition")
    if scenario.get("execution_mode") != "workflow_chat":
        raise WorkflowScenarioError(
            "only workflow_chat scenarios are supported initially."
        )
    _required_text(scenario.get("request"), "scenario request")
    provider = _mapping(scenario.get("provider"), "scenario provider")
    if provider.get("mode") != "scripted":
        raise WorkflowScenarioError("scenario provider.mode must be scripted.")
    _mapping_sequence(provider.get("responses"), "scenario provider.responses")
    _mapping(scenario.get("expect"), "scenario expect")


def _resolve_path(value: str, base: Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else base / path


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise WorkflowScenarioError(f"{label} must be an object.")
    return value


def _mapping_sequence(value: Any, label: str) -> list[Mapping[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(
        isinstance(item, Mapping) for item in value
    ):
        raise WorkflowScenarioError(f"{label} must be a list of objects.")
    return list(value)


def _string_sequence(value: Any, label: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise WorkflowScenarioError(f"{label} must be a list of strings.")
    return list(value)


def _required_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WorkflowScenarioError(f"{label} must be a non-empty string.")
    return value


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise WorkflowScenarioError("outcome must be a string.")
    return value


def _optional_positive_int(value: Any, *, default: int) -> int:
    if value is None:
        return default
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise WorkflowScenarioError("max_roundtrips must be a positive integer.")
    return value


def _optional_non_negative_int(value: Any, *, default: int) -> int:
    if value is None:
        return default
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise WorkflowScenarioError("max_repeated_action_count must be non-negative.")
    return value


def _raise_human_input_requested() -> str:
    raise RuntimeError(
        "Scenario requires human input; scripted scenarios must avoid prompt_user."
    )
