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

from powdrr_lift.agent.provider_config import DEFAULT_MODEL
from powdrr_lift.agent.providers import (
    build_workflow_client,
    initial_model_for_provider,
    resolve_provider_credentials,
)
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
    LLMProviderRoles,
    SkillChatConfig,
    SkillChatSelection,
    _ChatWorkflowExecutionStrategy,
    _workflow_action_signature,
    _WorkflowExecutionState,
    _WorkflowProgressDisplay,
    resolve_workflow_provider,
)
from powdrr_lift.workflow_llm import WorkflowStepRunner
from powdrr_lift.workflow_models import SkillCatalogEntry
from powdrr_lift.workflow_task_agent import _run_skill_for_agent
from powdrr_lift.workflow_task_scenario import run_workflow_task_scenario

WORKFLOW_SCENARIO_SCHEMA_VERSION = 1


class WorkflowScenarioError(ValueError):
    """Raised when a scenario is malformed or cannot be run safely."""


def extract_scripted_responses(report_path: Path) -> list[dict[str, Any]]:
    """Extract parsed model outputs from a live scenario report for replay."""
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkflowScenarioError(
            f"Could not read scenario report {report_path}: {exc}"
        ) from exc
    return extract_scripted_responses_from_report(report)


def extract_scripted_responses_from_report(
    report: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Extract replay responses from an already-loaded scenario report."""
    exchanges = report.get("llm_exchanges")
    if not isinstance(exchanges, list):
        raise WorkflowScenarioError("Scenario report does not contain llm_exchanges.")
    responses: list[dict[str, Any]] = []
    for index, exchange in enumerate(exchanges):
        response: Any = None
        if isinstance(exchange, Mapping) and isinstance(
            exchange.get("output"), Mapping
        ):
            response = exchange["output"]
        elif isinstance(exchange, list):
            for message in reversed(exchange):
                if isinstance(message, Mapping) and message.get("role") == "assistant":
                    content = message.get("content")
                    if isinstance(content, str):
                        try:
                            parsed = json.loads(content)
                        except json.JSONDecodeError:
                            continue
                        if isinstance(parsed, Mapping):
                            response = parsed
                            break
        if not isinstance(response, Mapping):
            raise WorkflowScenarioError(
                f"Scenario report exchange {index} has no parsed assistant output."
            )
        responses.append(dict(response))
    return responses


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
    if scenario.get("execution_mode") == "workflow_chain":
        _validate_workflow_chain_paths(scenario, source_root)
        return _run_workflow_chain_scenario(
            scenario,
            scenario_path=scenario_path,
            source_root=source_root,
            keep_failed=keep_failed,
        )
    provider = _mapping(scenario.get("provider"), "scenario provider")
    provider_mode = _required_text(provider.get("mode"), "scenario provider.mode")
    responses = (
        _scenario_responses(provider, scenario_path.parent, "scenario provider")
        if provider_mode == "scripted"
        else []
    )
    fixture = scenario.get("fixture")
    fixture_path = (
        _resolve_path(fixture, scenario_path.parent)
        if isinstance(fixture, str) and fixture
        else None
    )
    if fixture_path is not None and not fixture_path.is_dir():
        raise WorkflowScenarioError(f"Scenario fixture does not exist: {fixture_path}")
    max_roundtrips = (
        max_roundtrips_override
        if max_roundtrips_override is not None
        else _optional_positive_int_or_none(provider.get("max_roundtrips"))
        or (100 if provider_mode == "scripted" else 128)
    )
    if scenario["execution_mode"] == "workflow_task":
        generated_workflow_root: Path | None = None
        if "workflow_template" in scenario:
            generated_workflow_root = Path(
                tempfile.mkdtemp(prefix="powdrr-lift-live-workflow-template-")
            )
            template_values = _template_values(scenario.get("template_values"))
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
                template_values=template_values,
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
    # Resolve the temporary path so macOS's /var -> /private/var symlink does
    # not make paths produced by the execution strategy compare unequal.
    worktree_root = (temporary_root / "repository").resolve()
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
        configured_model = _optional_text(provider.get("model")) or DEFAULT_MODEL
        live_client = None
        execution_model = "scripted"
        if provider_mode == "live":
            credentials = resolve_provider_credentials(
                provider_name,
                _optional_text(provider.get("api_key")),
                _optional_text(provider.get("base_url")),
            )
            execution_model = initial_model_for_provider(
                provider_name, configured_model
            )
            live_client = build_workflow_client(
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
                max_roundtrips=max_roundtrips,
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
    max_roundtrips: int,
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
            max_roundtrips=max_roundtrips,
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


def _evaluate_task_phase_assertions(
    expect: Mapping[str, Any], result: Mapping[str, Any]
) -> list[dict[str, Any]]:
    assertions: list[dict[str, Any]] = []
    if "outcome" in expect:
        actual = "complete" if result.get("exit_code") == 0 else "failed"
        assertions.append(
            _assert("outcome", actual == expect["outcome"], expect["outcome"], actual)
        )
    if "all_tasks_completed" in expect:
        actual_bool = bool(result.get("all_tasks_completed"))
        assertions.append(
            _assert(
                "all_tasks_completed",
                actual_bool == expect["all_tasks_completed"],
                expect["all_tasks_completed"],
                actual_bool,
            )
        )
    if "output_matches" in expect:
        actual_bool = bool(result.get("output_matches"))
        assertions.append(
            _assert(
                "output_matches",
                actual_bool == expect["output_matches"],
                expect["output_matches"],
                actual_bool,
            )
        )
    if not assertions:
        assertions.append(
            _assert(
                "task_phase",
                result.get("exit_code") == 0,
                "complete",
                result.get("exit_code"),
            )
        )
    return assertions


def _evaluate_command_phase_assertions(
    expect: Mapping[str, Any], result: Mapping[str, Any]
) -> list[dict[str, Any]]:
    assertions: list[dict[str, Any]] = []
    expected_exit = expect.get("exit_code", 0)
    if not isinstance(expected_exit, int) or isinstance(expected_exit, bool):
        raise WorkflowScenarioError(
            "command phase expect.exit_code must be an integer."
        )
    actual_exit = result["exit_code"]
    assertions.append(
        _assert("exit_code", actual_exit == expected_exit, expected_exit, actual_exit)
    )
    if "stdout_contains" in expect:
        needle = _required_text(
            expect["stdout_contains"], "command phase stdout_contains"
        )
        assertions.append(
            _assert(
                "stdout_contains", needle in result["stdout"], needle, result["stdout"]
            )
        )
    if "stderr_contains" in expect:
        needle = _required_text(
            expect["stderr_contains"], "command phase stderr_contains"
        )
        assertions.append(
            _assert(
                "stderr_contains", needle in result["stderr"], needle, result["stderr"]
            )
        )
    return assertions


def _scenario_failed_marker(worktree_root: Path) -> bool:
    return (worktree_root / ".scenario-failed").is_file()


def _run_workflow_chain_scenario(
    scenario: Mapping[str, Any],
    *,
    scenario_path: Path,
    source_root: Path,
    keep_failed: bool,
) -> WorkflowScenarioResult:
    """Run scripted workflow-chat phases against one shared fixture repository."""
    scenario_id = _required_text(scenario.get("id"), "scenario id")
    fixture = scenario.get("fixture")
    fixture_path = (
        _resolve_path(fixture, scenario_path.parent)
        if isinstance(fixture, str) and fixture
        else None
    )
    if fixture_path is not None and not fixture_path.is_dir():
        raise WorkflowScenarioError(f"Scenario fixture does not exist: {fixture_path}")
    temporary_root = Path(tempfile.mkdtemp(prefix="powdrr-lift-chain-scenario-"))
    worktree_root = (temporary_root / "repository").resolve()
    _build_fixture_repository(worktree_root, fixture_path)
    all_events: list[dict[str, Any]] = []
    all_audit_events: list[dict[str, Any]] = []
    all_exchanges: list[Any] = []
    assertions: list[dict[str, Any]] = []
    roundtrips = 0
    status = "passed"
    last_definition = ""
    try:
        for phase_index, raw_phase in enumerate(scenario["phases"]):
            phase = _mapping(raw_phase, f"scenario phases[{phase_index}]")
            if phase.get("execution_mode", "workflow_chat") == "command":
                try:
                    completed = subprocess.run(
                        phase["command"],
                        cwd=worktree_root,
                        capture_output=True,
                        text=True,
                        check=False,
                        timeout=phase.get("timeout_seconds", 120),
                    )
                    command_result = {
                        "exit_code": completed.returncode,
                        "stdout": completed.stdout,
                        "stderr": completed.stderr,
                        "command": list(phase["command"]),
                    }
                except subprocess.TimeoutExpired as error:
                    stderr = error.stderr or ""
                    if isinstance(stderr, bytes):
                        stderr = stderr.decode(errors="replace")
                    command_result = {
                        "exit_code": 124,
                        "stdout": error.stdout or "",
                        "stderr": f"{stderr}command timed out",
                        "command": list(phase["command"]),
                    }
                phase_assertions = _evaluate_command_phase_assertions(
                    _mapping(phase.get("expect", {}), "phase expect"),
                    command_result,
                )
                assertions.extend(
                    {"phase": phase_index, **item} for item in phase_assertions
                )
                if not all(item["passed"] for item in phase_assertions):
                    status = "failed"
                    break
                continue
            if phase.get("execution_mode", "workflow_chat") == "workflow_task":
                phase_provider = _mapping(
                    phase.get("provider"), f"scenario phases[{phase_index}].provider"
                )
                task_result = run_workflow_task_scenario(
                    workflow_source=_resolve_path(
                        _required_text(
                            phase.get("workflow_dir"),
                            f"scenario phases[{phase_index}].workflow_dir",
                        ),
                        source_root,
                    ),
                    skill_definitions_source=(
                        source_root / "skill-definitions"
                        if (source_root / "skill-definitions").is_dir()
                        else None
                    ),
                    responses=_scenario_responses(
                        phase_provider,
                        scenario_path.parent,
                        f"scenario phases[{phase_index}].provider",
                    ),
                    task_id=_optional_text(phase.get("task_id")),
                    expected_output_state=phase.get("expected_output_state"),
                    run_all=bool(phase.get("run_all", False)),
                    shared_repo_root=worktree_root,
                )
                roundtrips += int(task_result["roundtrips"])
                phase_assertions = _evaluate_task_phase_assertions(
                    _mapping(phase.get("expect", {}), "phase expect"), task_result
                )
                assertions.extend(
                    {"phase": phase_index, **item} for item in phase_assertions
                )
                if task_result["exit_code"] != 0 or not all(
                    item["passed"] for item in phase_assertions
                ):
                    status = "failed"
                    break
                continue
            definition_path = _resolve_path(
                _required_text(
                    phase.get("definition"),
                    f"scenario phases[{phase_index}].definition",
                ),
                source_root,
            )
            phase_provider = _mapping(
                phase.get("provider"), f"scenario phases[{phase_index}].provider"
            )
            phase_responses = _scenario_responses(
                phase_provider,
                scenario_path.parent,
                f"scenario phases[{phase_index}].provider",
            )
            execution = _run_scripted_skill(
                definition_path=definition_path,
                worktree_root=worktree_root,
                root_intent=_required_text(
                    phase.get("request"), f"scenario phases[{phase_index}].request"
                ),
                responses=phase_responses,
                max_roundtrips=len(phase_responses) + 1,
                initial_inputs=_mapping(phase.get("inputs", {}), "phase inputs"),
            )
            phase_events = [
                {"phase": phase_index, "phase_id": phase.get("id"), **event}
                for event in execution.execution_events
            ]
            all_events.extend(phase_events)
            all_audit_events.extend(execution.audit_events)
            all_exchanges.extend(execution.llm_exchanges)
            roundtrips += execution.roundtrips
            last_definition = str(definition_path)
            phase_assertions = _evaluate_assertions(
                _mapping(phase.get("expect", {}), "phase expect"),
                skill=execution.skill,
                exit_code=execution.exit_code,
                execution_events=execution.execution_events,
                audit_events=execution.audit_events,
                roundtrips=execution.roundtrips,
                worktree_root=worktree_root,
            )
            assertions.extend(
                {"phase": phase_index, **assertion} for assertion in phase_assertions
            )
            if execution.exit_code != 0 or not all(
                assertion["passed"] for assertion in phase_assertions
            ):
                status = "failed"
                break
        final_expect = _mapping(scenario.get("expect", {}), "scenario expect")
        if last_definition:
            final_skill = load_skill(Path(last_definition))
            final_assertions = _evaluate_assertions(
                final_expect,
                skill=final_skill,
                exit_code=0 if status == "passed" else 1,
                execution_events=all_events,
                audit_events=all_audit_events,
                roundtrips=roundtrips,
                worktree_root=worktree_root,
            )
            assertions.extend({"phase": "final", **item} for item in final_assertions)
            if not all(item["passed"] for item in final_assertions):
                status = "failed"
        return WorkflowScenarioResult(
            scenario_id=scenario_id,
            definition=last_definition,
            status=status,
            assertions=tuple(assertions),
            execution_events=tuple(all_events),
            audit_events=tuple(all_audit_events),
            roundtrips=roundtrips,
            llm_exchanges=tuple(all_exchanges),
            worktree_root=worktree_root if status == "failed" and keep_failed else None,
        )
    finally:
        if status == "passed" or not keep_failed:
            shutil.rmtree(temporary_root, ignore_errors=True)


def _validate_workflow_chain_paths(
    scenario: Mapping[str, Any], source_root: Path
) -> None:
    """Validate every phase reference before creating or mutating a fixture."""
    for index, raw_phase in enumerate(scenario["phases"]):
        phase = _mapping(raw_phase, f"scenario phases[{index}]")
        if phase.get("execution_mode", "workflow_chat") == "command":
            continue
        if phase.get("execution_mode", "workflow_chat") == "workflow_task":
            path = _resolve_path(
                _required_text(
                    phase.get("workflow_dir"),
                    f"scenario phases[{index}].workflow_dir",
                ),
                source_root,
            )
            if not path.is_dir():
                raise WorkflowScenarioError(
                    f"Scenario phase workflow directory does not exist: {path}"
                )
        else:
            path = _resolve_path(
                _required_text(
                    phase.get("definition"),
                    f"scenario phases[{index}].definition",
                ),
                source_root,
            )
            if not path.is_file():
                raise WorkflowScenarioError(
                    f"Scenario phase definition does not exist: {path}"
                )


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
    elif mode == "workflow_chain":
        phases = scenario.get("phases")
        if not isinstance(phases, list) or not phases:
            raise WorkflowScenarioError("workflow_chain requires non-empty phases.")
        phase_ids: set[str] = set()
        for index, phase in enumerate(phases):
            phase_mapping = _mapping(phase, f"scenario phases[{index}]")
            phase_mode = phase_mapping.get("execution_mode", "workflow_chat")
            phase_id = _required_text(
                phase_mapping.get("id"), f"scenario phases[{index}].id"
            )
            if phase_id in phase_ids:
                raise WorkflowScenarioError(
                    f"scenario phases[{index}].id must be unique: {phase_id}."
                )
            phase_ids.add(phase_id)
            if phase_mode not in {"workflow_chat", "workflow_task", "command"}:
                raise WorkflowScenarioError(
                    f"scenario phases[{index}].execution_mode must be "
                    "workflow_chat, workflow_task, or command."
                )
            if phase_mode == "command":
                command = phase_mapping.get("command")
                if (
                    not isinstance(command, list)
                    or not command
                    or not all(isinstance(item, str) and item for item in command)
                ):
                    raise WorkflowScenarioError(
                        f"scenario phases[{index}].command must be a non-empty "
                        "list of strings."
                    )
                timeout = phase_mapping.get("timeout_seconds", 120)
                if (
                    not isinstance(timeout, (int, float))
                    or isinstance(timeout, bool)
                    or timeout <= 0
                ):
                    raise WorkflowScenarioError(
                        f"scenario phases[{index}].timeout_seconds must be positive."
                    )
                _mapping(
                    phase_mapping.get("expect", {}),
                    f"scenario phases[{index}].expect",
                )
                continue
            if phase_mode == "workflow_task":
                _required_text(
                    phase_mapping.get("workflow_dir"),
                    f"scenario phases[{index}].workflow_dir",
                )
                run_all = phase_mapping.get("run_all", False)
                if not isinstance(run_all, bool):
                    raise WorkflowScenarioError(
                        f"scenario phases[{index}].run_all must be a boolean."
                    )
                if not run_all:
                    _required_text(
                        phase_mapping.get("task_id"),
                        f"scenario phases[{index}].task_id",
                    )
                phase_provider = _mapping(
                    phase_mapping.get("provider"), f"scenario phases[{index}].provider"
                )
                if (
                    _required_text(
                        phase_provider.get("mode"),
                        f"scenario phases[{index}].provider.mode",
                    )
                    != "scripted"
                ):
                    raise WorkflowScenarioError(
                        "workflow_chain currently requires scripted phase providers."
                    )
                _validate_response_source(
                    phase_provider, f"scenario phases[{index}].provider"
                )
                _mapping(
                    phase_mapping.get("expect", {}), f"scenario phases[{index}].expect"
                )
                continue
            _required_text(
                phase_mapping.get("definition"),
                f"scenario phases[{index}].definition",
            )
            _required_text(
                phase_mapping.get("request"),
                f"scenario phases[{index}].request",
            )
            phase_provider = _mapping(
                phase_mapping.get("provider"),
                f"scenario phases[{index}].provider",
            )
            if (
                _required_text(
                    phase_provider.get("mode"),
                    f"scenario phases[{index}].provider.mode",
                )
                != "scripted"
            ):
                raise WorkflowScenarioError(
                    "workflow_chain currently requires scripted phase providers."
                )
            _validate_response_source(
                phase_provider, f"scenario phases[{index}].provider"
            )
            _mapping(
                phase_mapping.get("expect", {}), f"scenario phases[{index}].expect"
            )
    else:
        raise WorkflowScenarioError(
            "execution_mode must be workflow_chat, workflow_task, or workflow_chain."
        )
    if mode != "workflow_chain":
        provider = _mapping(scenario.get("provider"), "scenario provider")
        provider_mode = _required_text(provider.get("mode"), "scenario provider.mode")
        if provider_mode not in {"scripted", "live"}:
            raise WorkflowScenarioError(
                "scenario provider.mode must be scripted or live."
            )
        if provider_mode == "scripted":
            _validate_response_source(provider, "scenario provider")
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


def _validate_response_source(provider: Mapping[str, Any], label: str) -> None:
    responses = provider.get("responses")
    response_file = provider.get("responses_file")
    if responses is not None and response_file is not None:
        raise WorkflowScenarioError(
            f"{label} may set responses or responses_file, not both."
        )
    if responses is None and response_file is None:
        raise WorkflowScenarioError(f"{label} requires responses or responses_file.")
    if response_file is not None:
        _required_text(response_file, f"{label}.responses_file")
    else:
        _mapping_sequence(responses, f"{label}.responses")


def _scenario_responses(
    provider: Mapping[str, Any], base: Path, label: str
) -> list[Mapping[str, Any]]:
    _validate_response_source(provider, label)
    response_file = provider.get("responses_file")
    if response_file is None:
        return _mapping_sequence(provider.get("responses"), f"{label}.responses")
    path = _resolve_path(response_file, base)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise WorkflowScenarioError(
            f"Could not read {label}.responses_file {path}: {exc}"
        ) from exc
    try:
        data = json.loads(raw) if path.suffix == ".json" else yaml.safe_load(raw)
    except (json.JSONDecodeError, yaml.YAMLError) as exc:
        raise WorkflowScenarioError(
            f"Could not parse {label}.responses_file {path}: {exc}"
        ) from exc
    return _mapping_sequence(data, f"{label}.responses_file")


def _template_values(value: Any) -> dict[str, str]:
    if value is None:
        return {}
    mapping = _mapping(value, "scenario template_values")
    if not all(
        isinstance(key, str) and isinstance(item, str) for key, item in mapping.items()
    ):
        raise WorkflowScenarioError(
            "scenario template_values must map string names to string values."
        )
    return dict(mapping)


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
