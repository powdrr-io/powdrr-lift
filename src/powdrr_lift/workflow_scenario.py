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

from powdrr_lift.core import (
    AgentRole,
    AssigneeType,
    TaskComplexity,
    TaskStatus,
    WorkflowTask,
    load_skill,
    resolve_repo_root,
)
from powdrr_lift.core.workflow_template_specification import (
    instantiate_workflow_template,
)
from powdrr_lift.errors import PowdrrExecutionError
from powdrr_lift.execution.runtime import ExecutionRuntime
from powdrr_lift.intrinsic_git_gh import GH_TOOL, intrinsic_command
from powdrr_lift.workflow_chat_agent import (
    _DEFAULT_MODEL,
    LLMProviderRoles,
    SkillCatalogEntry,
    SkillChatConfig,
    SkillChatSelection,
    _build_chat_client,
    _ChatWorkflowExecutionStrategy,
    _initial_model_for_provider,
    _resolve_credentials,
    _workflow_action_signature,
    _WorkflowExecutionState,
    _WorkflowProgressDisplay,
    resolve_workflow_provider,
)
from powdrr_lift.workflow_llm import WorkflowStepRunner
from powdrr_lift.workflow_task_agent import _run_skill_for_agent
from powdrr_lift.workflow_task_scenario import run_workflow_task_scenario

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
    llm_exchanges: tuple[Any, ...] = ()
    analysis: dict[str, Any] | None = None
    stdout: str = ""
    stderr: str = ""
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
            "llm_exchanges": list(self.llm_exchanges),
            "analysis": self.analysis,
            "stdout": self.stdout,
            "stderr": self.stderr,
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
            raise PowdrrExecutionError(
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
    max_roundtrips_override: int | None = None,
    max_stalled_roundtrips_override: int | None = None,
    stream_live: bool = False,
    guidance: Sequence[str] = (),
) -> WorkflowScenarioResult:
    """Run one scripted skill scenario in a fresh temporary Git repository."""
    _validate_scenario(scenario)
    source_root = resolve_repo_root(repo_root)
    scenario_id = _required_text(scenario.get("id"), "scenario id")
    provider = _mapping(scenario.get("provider"), "scenario provider")
    provider_mode = _required_text(provider.get("mode"), "scenario provider.mode")
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
    if scenario["execution_mode"] == "workflow_task":
        generated_workflow_root: Path | None = None
        if "workflow_template" in scenario:
            generated_workflow_root = Path(
                tempfile.mkdtemp(prefix="powdrr-lift-live-workflow-template-")
            )
            workflow_dir, _ = instantiate_workflow_template(
                _resolve_path(
                    _required_text(
                        scenario.get("workflow_template"), "workflow_template"
                    ),
                    source_root,
                ),
                work_item_name=_required_text(
                    scenario.get("work_item_name"), "work_item_name"
                ),
                output_root=generated_workflow_root,
            )
        else:
            workflow_dir = _resolve_path(
                _required_text(scenario.get("workflow_dir"), "workflow_dir"),
                source_root,
            )
        expected = _mapping(scenario.get("expect"), "scenario expect")
        run_all = scenario.get("run_all", False)
        if not isinstance(run_all, bool):
            raise WorkflowScenarioError("workflow_task run_all must be a boolean.")
        try:
            result = run_workflow_task_scenario(
                workflow_source=workflow_dir,
                skill_definitions_source=(
                    source_root / "skill-definitions"
                    if (source_root / "skill-definitions").is_dir()
                    else None
                ),
                task_id=(
                    _required_text(scenario.get("task_id"), "task_id")
                    if not run_all
                    else None
                ),
                responses=responses,
                fixture_root=fixture_path,
                expected_output_state=expected.get("output_state"),
                run_all=run_all,
                live_provider=(
                    _optional_text(provider.get("provider")) or "auto"
                    if provider_mode == "live"
                    else None
                ),
                api_key=_optional_text(provider.get("api_key")),
                base_url=_optional_text(provider.get("base_url")),
                max_roundtrips=(
                    max_roundtrips_override
                    if max_roundtrips_override is not None
                    else (
                        _optional_positive_int(
                            provider.get("max_roundtrips"), default=100
                        )
                        if provider_mode == "scripted"
                        else _optional_positive_int_or_none(
                            provider.get("max_roundtrips")
                        )
                    )
                ),
                max_stalled_roundtrips=(
                    max_stalled_roundtrips_override
                    if max_stalled_roundtrips_override is not None
                    else _optional_non_negative_int(
                        provider.get("max_stalled_roundtrips"), default=3
                    )
                ),
                verbose=bool(provider.get("verbose", False)),
                stream_live=stream_live,
                guidance=guidance,
            )
        finally:
            if generated_workflow_root is not None:
                shutil.rmtree(generated_workflow_root, ignore_errors=True)
        assertions: list[dict[str, Any]] = []
        if provider_mode == "scripted" or "outcome" in expected:
            expected_outcome = expected.get("outcome", "complete")
            assertions.append(
                _assert(
                    "outcome",
                    (result["exit_code"] == 0)
                    if expected_outcome == "complete"
                    else result["exit_code"] != 0,
                    expected_outcome,
                    result["exit_code"],
                )
            )
        if provider_mode == "scripted" or "task_status" in expected:
            expected_status = expected.get("task_status", "completed")
            assertions.append(
                _assert(
                    "task_status",
                    result["task_status"] == expected_status,
                    expected_status,
                    result["task_status"],
                )
            )
        if "output_state" in expected:
            assertions.append(
                _assert(
                    "output_state",
                    result["output_matches"],
                    expected["output_state"],
                    result["output_state"],
                )
            )
        if run_all:
            assertions.append(
                _assert(
                    "all_tasks_completed",
                    result["all_tasks_completed"],
                    True,
                    result["all_tasks_completed"],
                )
            )
        return WorkflowScenarioResult(
            scenario_id=scenario_id,
            definition=str(workflow_dir),
            status="passed" if all(item["passed"] for item in assertions) else "failed",
            assertions=tuple(assertions),
            execution_events=(),
            audit_events=(),
            roundtrips=int(result["roundtrips"]),
            llm_exchanges=tuple(result.get("exchanges", ())),
            analysis=result.get("analysis"),
            stdout=str(result.get("stdout", "")),
            stderr=str(result.get("stderr", "")),
        )
    definition_path = _resolve_path(
        _required_text(scenario.get("definition"), "scenario definition"),
        source_root,
    )
    if not definition_path.is_file():
        raise WorkflowScenarioError(
            f"Scenario definition does not exist: {definition_path}"
        )

    temporary_root = Path(tempfile.mkdtemp(prefix="powdrr-lift-scenario-"))
    worktree_root = temporary_root / "repository"
    try:
        _build_fixture_repository(worktree_root, fixture_path)
        if guidance:
            guidance_runtime = ExecutionRuntime(
                "scenario-guidance",
                profile_id="default",
                workflow_directory=temporary_root / ".powdrr-execution",
                repo_root=worktree_root,
            )
            for rule in guidance:
                guidance_runtime.capture_guidance(
                    rule, source_ref=f"scenario:{scenario_id}", scope={}
                )
        scenario_inputs = _mapping(scenario.get("inputs", {}), "scenario inputs")
        provider_name = _optional_text(provider.get("provider")) or "auto"
        if provider_name == "auto":
            provider_name = resolve_workflow_provider()
        configured_model = _optional_text(provider.get("model")) or _DEFAULT_MODEL
        live_client = None
        execution_model = "scripted"
        if provider_mode == "live":
            credentials = _resolve_credentials(
                provider_name,
                _optional_text(provider.get("api_key")),
                _optional_text(provider.get("base_url")),
            )
            execution_model = _initial_model_for_provider(
                provider_name, configured_model
            )
            live_client = _build_chat_client(
                credentials,
                model=execution_model,
                model_cache_dir=temporary_root / "models",
            )
        if provider_mode == "live":
            execution = _run_live_skill(
                definition_path=definition_path,
                worktree_root=worktree_root,
                root_intent=_required_text(scenario.get("request"), "scenario request"),
                client=live_client,
                initial_inputs=scenario_inputs,
            )
        else:
            execution = _run_scripted_skill(
                definition_path=definition_path,
                worktree_root=worktree_root,
                root_intent=_required_text(scenario.get("request"), "scenario request"),
                responses=responses,
                max_roundtrips=max(1, len(responses) + 1),
                initial_inputs=scenario_inputs,
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
            llm_exchanges=tuple(execution.llm_exchanges),
            stdout=execution.stdout,
            stderr=execution.stderr,
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
    llm_exchanges: list[list[dict[str, str]]]
    stdout: str = ""
    stderr: str = ""


def _run_live_skill(
    *,
    definition_path: Path,
    worktree_root: Path,
    root_intent: str,
    client: Any,
    initial_inputs: Mapping[str, Any],
) -> _ScriptedSkillExecution:
    responses: list[dict[str, Any]] = []
    prompt_sizes: list[int] = []

    class _RecordingClient:
        def complete_json(self, messages: list[dict[str, str]]) -> dict[str, Any]:
            prompt_sizes.append(len(json.dumps(messages, ensure_ascii=False)))
            response = client.complete_json(messages)
            responses.append(dict(response))
            return response

    catalog = tuple(
        SkillCatalogEntry(path, load_skill(path))
        for path in sorted(definition_path.parent.glob("*.yaml"))
    )
    task = WorkflowTask(
        task_id="live-design-interview",
        status=TaskStatus.OPEN,
        description=root_intent,
        complexity=TaskComplexity.MEDIUM,
        input_state=dict(initial_inputs),
        assignee_type=AssigneeType.AGENT,
        assignee_role=AgentRole.CODER,
    )
    stdout = io.StringIO()
    stdout = io.StringIO()
    stderr = io.StringIO()
    runtime = ExecutionRuntime(
        "live-design-interview",
        profile_id="default",
        workflow_directory=worktree_root.parent / ".powdrr-execution",
        repo_root=worktree_root,
    )
    try:
        outcome = _run_skill_for_agent(
            "design-interview",
            catalog=catalog,
            client=_RecordingClient(),
            task=task,
            repo_root=worktree_root,
            stdout=stdout,
            stderr=stderr,
            max_timeout_retries=0,
            timeout_backoff_seconds=0,
            error_log_root=worktree_root,
            runtime=runtime,
        )
    except RuntimeError as exc:
        stderr.write(
            f"Live skill runner failed: {exc}; requests={len(responses)}; "
            f"prompt_sizes={prompt_sizes}\n"
        )
        return _ScriptedSkillExecution(
            skill=load_skill(definition_path),
            exit_code=1,
            execution_events=[],
            audit_events=[],
            roundtrips=0,
            llm_exchanges=[
                [{"role": "assistant", "content": json.dumps(response)}]
                for response in responses
            ],
            stdout=stdout.getvalue(),
            stderr=stderr.getvalue(),
        )
    return _ScriptedSkillExecution(
        skill=load_skill(definition_path),
        exit_code=0,
        execution_events=list(outcome.get("events", [])),
        audit_events=[],
        roundtrips=len(outcome.get("events", [])),
        llm_exchanges=[
            [{"role": "assistant", "content": json.dumps(response)}]
            for response in responses
        ],
        stdout=stdout.getvalue(),
        stderr=stderr.getvalue(),
    )


def _run_scripted_skill(
    *,
    definition_path: Path,
    worktree_root: Path,
    root_intent: str,
    responses: Sequence[Mapping[str, Any]],
    max_roundtrips: int,
    client: Any | None = None,
    provider: str = "local",
    model: str = "scripted",
    initial_inputs: Mapping[str, Any] | None = None,
) -> _ScriptedSkillExecution:
    skill = load_skill(definition_path)
    if client is None:
        _validate_scripted_responses(responses)
    entry = SkillCatalogEntry(definition_path, skill)
    execution_client = client or _ScriptedWorkflowClient(responses)
    config = SkillChatConfig(
        skills_dir=definition_path.parent,
        repo_root=worktree_root,
        provider=provider,
        model=model,
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
        runtime=ExecutionRuntime(
            f"scenario-{skill.name}",
            profile_id="default",
            workflow_directory=worktree_root.parent / ".powdrr-execution",
            repo_root=worktree_root,
        ),
        handoff_records={
            name: {
                "name": name,
                "type": "string" if isinstance(value, str) else "any",
                "value": value,
                "source": "caller",
                "produced_by": {"source": "caller"},
                "scope": "skill",
            }
            for name, value in (initial_inputs or {}).items()
        },
    )
    stdout = io.StringIO()
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
        stdout=stdout,
        stderr=stderr,
        client_for_model=lambda _model, _provider: execution_client,
        provider_roles=LLMProviderRoles(normal=provider),
        provider_role="normal",
        current_model=model,
        provider=provider,
        driver=WorkflowStepRunner(
            max_stalled_roundtrips=2,
            runtime=state.runtime,
            phase_type="build",
            actor_id="scenario",
        ),
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
        roundtrips=len(getattr(execution_client, "messages", [])),
        llm_exchanges=getattr(execution_client, "messages", []),
        stdout=stdout.getvalue(),
        stderr=stderr.getvalue(),
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
    mode = scenario.get("execution_mode")
    if mode == "workflow_chat":
        _required_text(scenario.get("definition"), "scenario definition")
        _required_text(scenario.get("request"), "scenario request")
    elif mode == "workflow_task":
        if "workflow_dir" not in scenario and "workflow_template" not in scenario:
            raise WorkflowScenarioError(
                "workflow_task requires workflow_dir or workflow_template."
            )
        if "workflow_template" in scenario:
            _required_text(scenario.get("workflow_template"), "workflow_template")
            _required_text(scenario.get("work_item_name"), "work_item_name")
        run_all = scenario.get("run_all", False)
        if not isinstance(run_all, bool):
            raise WorkflowScenarioError("workflow_task run_all must be a boolean.")
        if not run_all:
            _required_text(scenario.get("task_id"), "task_id")
    else:
        raise WorkflowScenarioError(
            "execution_mode must be workflow_chat or workflow_task."
        )
    provider = _mapping(scenario.get("provider"), "scenario provider")
    provider_mode = _required_text(provider.get("mode"), "scenario provider.mode")
    if provider_mode not in {"scripted", "live"}:
        raise WorkflowScenarioError("scenario provider.mode must be scripted or live.")
    if provider_mode == "scripted":
        _mapping_sequence(provider.get("responses"), "scenario provider.responses")
    if provider_mode == "live" and provider.get("provider") is not None:
        _required_text(provider.get("provider"), "scenario provider.provider")
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


def _optional_positive_int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    return _optional_positive_int(value, default=1)


def _optional_non_negative_int(value: Any, *, default: int) -> int:
    if value is None:
        return default
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise WorkflowScenarioError("max_repeated_action_count must be non-negative.")
    return value


def _raise_human_input_requested() -> str:
    raise PowdrrExecutionError(
        "Scenario requires human input; scripted scenarios must avoid prompt_user."
    )
