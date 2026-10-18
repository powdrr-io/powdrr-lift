"""Deterministic scenarios for Procedrr-backed durable workflow tasks."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from powdrr_lift.core import resolve_repo_root
from powdrr_lift.process.templates import (
    instantiate_workflow_template,
)
from powdrr_lift.workrr.task_scenario import run_workflow_task_scenario

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
    usage_summary: dict[str, Any] | None = None
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
            "usage_summary": self.usage_summary,
            "analysis": self.analysis,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "worktree_root": str(self.worktree_root) if self.worktree_root else None,
        }


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
    """Run one scripted Procedrr workflow-task scenario in an isolated repository."""
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
                process_catalog_source=(
                    source_root / "docs" / "procedrr" / "processes"
                    if (source_root / "docs" / "procedrr" / "processes").is_dir()
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
                    or _optional_positive_int(
                        provider.get("max_roundtrips"), default=100
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
            usage_summary=result.get("usage_summary"),
            analysis=result.get("analysis"),
            stdout=str(result.get("stdout", "")),
            stderr=str(result.get("stderr", "")),
        )
    raise WorkflowScenarioError("Only Procedrr workflow_task scenarios can run.")


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
    try:
        for phase_index, raw_phase in enumerate(scenario["phases"]):
            phase = _mapping(raw_phase, f"scenario phases[{phase_index}]")
            if phase.get("execution_mode", "workflow_task") == "command":
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
            if phase.get("execution_mode", "workflow_task") == "workflow_task":
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
                    process_catalog_source=(
                        source_root / "docs" / "procedrr" / "processes"
                        if (source_root / "docs" / "procedrr" / "processes").is_dir()
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
            raise WorkflowScenarioError(
                "workflow_chain phases must be workflow_task or command."
            )
        return WorkflowScenarioResult(
            scenario_id=scenario_id,
            definition="workflow-task-chain",
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
        if phase.get("execution_mode", "workflow_task") == "command":
            continue
        if phase.get("execution_mode", "workflow_task") == "workflow_task":
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
            raise WorkflowScenarioError(
                f"scenario phases[{index}] must be workflow_task or command."
            )


def _assert(name: str, passed: bool, expected: Any, actual: Any) -> dict[str, Any]:
    return {"name": name, "passed": passed, "expected": expected, "actual": actual}


def _validate_scenario(scenario: Mapping[str, Any]) -> None:
    if scenario.get("schema_version") != WORKFLOW_SCENARIO_SCHEMA_VERSION:
        raise WorkflowScenarioError("scenario schema_version must be 1.")
    _required_text(scenario.get("id"), "scenario id")
    mode = scenario.get("execution_mode")
    if mode == "workflow_task":
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
            phase_mode = phase_mapping.get("execution_mode", "workflow_task")
            phase_id = _required_text(
                phase_mapping.get("id"), f"scenario phases[{index}].id"
            )
            if phase_id in phase_ids:
                raise WorkflowScenarioError(
                    f"scenario phases[{index}].id must be unique: {phase_id}."
                )
            phase_ids.add(phase_id)
            if phase_mode not in {"workflow_task", "command"}:
                raise WorkflowScenarioError(
                    f"scenario phases[{index}].execution_mode must be "
                    "workflow_task or command."
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
    else:
        raise WorkflowScenarioError(
            "execution_mode must be workflow_task or workflow_chain."
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
