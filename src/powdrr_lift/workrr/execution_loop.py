"""Execution-loop mechanics for validated workflow steps.

This module owns deterministic execution and evidence-producing checks.
The chat agent orchestrates model interaction but does not implement these
operations.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, TextIO

from powdrr_lift.basedpyright_tools import is_basedpyright_tool
from powdrr_lift.builtin_tool_help import builtin_tool_help
from powdrr_lift.core.pr_specification import build_authoritative_effect_handoff
from powdrr_lift.core.python_tool_commands import (
    dependency_backed_command_variants,
    missing_executable_output,
)
from powdrr_lift.core.spec_context import (
    gather_specification_context,
    render_gather_context_report,
)
from powdrr_lift.errors import PowdrrExecutionError
from powdrr_lift.execution.builtin_tools import (
    invoke_basedpyright_capability,
    invoke_deferred_edit_capability,
    invoke_fuzzy_match_capability,
    invoke_intrinsic_capability,
    invoke_repository_read,
    invoke_shell_capability,
)
from powdrr_lift.execution.runtime import ExecutionRuntime
from powdrr_lift.intrinsic_edit import APPLY_EDIT_TOOL, VALIDATE_EDIT_TOOL
from powdrr_lift.intrinsic_enrich import ENRICH_TOOL
from powdrr_lift.intrinsic_git_gh import GH_TOOL, GIT_TOOL
from powdrr_lift.workrr.action_operations import _resolve_pre_step_template
from powdrr_lift.workrr.chat_io import _verbose_print
from powdrr_lift.workrr.context import WorkflowContext
from powdrr_lift.workrr.feature_context import resolve_feature_context
from powdrr_lift.workrr.operation_validation import (
    command_items_for_validation,
    record_runtime_readiness_from_pre_step,
    runtime_readiness_report,
    validate_internal_command,
)

_INTERNAL_TOOL = "internal"


_RTK_BYPASS_COMMANDS = frozenset({"test"})


def _extract_command_option(
    command_items: Sequence[str], option_name: str
) -> str | None:
    for index, item in enumerate(command_items):
        if item == option_name and index + 1 < len(command_items):
            return command_items[index + 1]
    return None


def _latest_deterministic_pre_step(
    execution_events: Sequence[Mapping[str, Any]],
    *,
    process_id: str,
    step_index: int,
) -> Mapping[str, Any] | None:
    for event in reversed(execution_events):
        if (
            event.get("kind") == "deterministic_pre_step"
            and event.get("process_id") == process_id
            and event.get("step_index") == step_index
        ):
            return event
    return None


def _pre_step_context_values(
    handoff_records: Mapping[str, Mapping[str, Any]],
    workflow_context: WorkflowContext | None,
    execution_events: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    values: dict[str, Any] = {}

    def add(name: object, value: object, *, include_none: bool = False) -> None:
        if not isinstance(name, str) or (value is None and not include_none):
            return
        normalized = re.sub(r"[-\s]+", "_", name.strip().lower())
        if normalized:
            values[normalized] = value

    if workflow_context is not None:
        for name, value in workflow_context.to_data().items():
            add(name, value)
    for name, record in handoff_records.items():
        if isinstance(record, Mapping):
            add(name, record.get("value"), include_none="value" in record)
    for event in execution_events:
        parameters = event.get("parameters")
        if not isinstance(parameters, Mapping):
            continue
        command = parameters.get("command")
        if not isinstance(command, Sequence) or isinstance(
            command, (str, bytes, bytearray)
        ):
            continue
        command_items = [item for item in command if isinstance(item, str)]
        if "--work-item-name" in command_items:
            option_index = command_items.index("--work-item-name")
            if option_index + 1 < len(command_items):
                add("work-item-name", command_items[option_index + 1])
        for item in command_items:
            proposal_prefix = "docs/proposals/"
            if item.startswith(proposal_prefix):
                proposal_name = item[len(proposal_prefix) :].split("/", 1)[0]
                add("work-item-name", proposal_name)
    return values


def _wire_previous_tool_output(
    parameters: dict[str, Any],
    execution_events: Sequence[Mapping[str, Any]],
    handoff_records: Mapping[str, Mapping[str, Any]],
) -> None:
    reference = parameters.get("tool_output")
    if isinstance(reference, Mapping) and reference.get("source") == "handoff":
        name = reference.get("name")
        record = handoff_records.get(name) if isinstance(name, str) else None
        if record is None or "value" not in record:
            raise PowdrrExecutionError(
                "enrich tool_output handoff source requires a prior named output."
            )
        parameters["tool_output"] = record["value"]
        return
    if reference != {"source": "previous_tool_output"}:
        return
    previous = next(
        (
            event.get("result")
            for event in reversed(execution_events)
            if event.get("kind") == "deterministic_pre_step"
            and isinstance(event.get("result"), Mapping)
        ),
        None,
    )
    if previous is None:
        raise PowdrrExecutionError(
            "enrich tool_output source previous_tool_output requires a prior "
            "deterministic tool result."
        )
    parameters["tool_output"] = previous


def _is_authoritative_effect_command(command: object) -> bool:
    command_items = command_items_for_validation(command)
    return len(command_items) >= 2 and command_items[:2] == [
        "powdrr-lift",
        "authoritative-pr-effects",
    ]


def _is_feature_context_command(command: object) -> bool:
    command_items = command_items_for_validation(command)
    return command_items[:2] == ["powdrr-lift", "resolve-feature-context"]


def _normalize_noop_git_commit_result(
    process: subprocess.CompletedProcess[str],
    command: str | Sequence[str],
    worktree_root: Path,
) -> subprocess.CompletedProcess[str]:
    """Treat a clean-worktree empty commit as a successful no-op."""
    command_items = shlex.split(command) if isinstance(command, str) else list(command)
    if command_items[:2] != ["git", "commit"] or process.returncode == 0:
        return process
    combined_output = f"{process.stdout}\n{process.stderr}".lower()
    if not any(
        marker in combined_output
        for marker in (
            "nothing to commit",
            "nothing added to commit",
            "no changes added to commit",
        )
    ):
        return process
    status = subprocess.run(
        ["git", "status", "--short"],
        cwd=worktree_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if status.returncode != 0 or status.stdout.strip():
        paths = [line[3:] for line in status.stdout.splitlines() if len(line) >= 4]
        path_hint = ", ".join(paths) if paths else "the reported files"
        guidance = (
            "Commit was skipped because Git found no staged changes, but the "
            f"worktree is not clean ({path_hint}). For each intended path, use "
            "the intrinsic git add action, for example "
            '{"action":"invoke_tool","tool":"git","parameters":'
            '{"operation":"add","paths":["path/to/file"]}}. '
            "For an unintended untracked file, delete it with the file-management "
            "action or remove it explicitly, then rerun git status before retrying "
            "the commit."
        )
        return subprocess.CompletedProcess(
            process.args,
            process.returncode,
            stdout=process.stdout,
            stderr=f"{process.stderr.rstrip()}\n{guidance}\n",
        )
    return subprocess.CompletedProcess(
        process.args,
        0,
        stdout=f"{process.stdout}No changes to commit; existing HEAD retained.\n",
        stderr=process.stderr,
    )


def _empty_pull_request_error(
    command: str | Sequence[str],
    worktree_root: Path,
) -> str | None:
    """Prevent an avoidable GitHub error when a branch has no committed delta."""
    if isinstance(command, str):
        try:
            command_items = shlex.split(command)
        except ValueError:
            return None
    else:
        command_items = list(command)
    if command_items[:3] != ["gh", "pr", "create"]:
        return None

    base = "main"
    for index, item in enumerate(command_items[:-1]):
        if item == "--base":
            base = command_items[index + 1]
            break
    ahead = subprocess.run(
        ["git", "rev-list", "--count", f"{base}..HEAD"],
        cwd=worktree_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if ahead.returncode != 0 or ahead.stdout.strip() != "0":
        return None
    status = subprocess.run(
        ["git", "status", "--short"],
        cwd=worktree_root,
        capture_output=True,
        text=True,
        check=False,
    )
    detail = (
        "Uncommitted changes are present."
        if status.stdout.strip()
        else "There are no uncommitted changes either."
    )
    return (
        f"Cannot create a pull request: no commits exist between {base} and HEAD. "
        f"{detail} Stage the exact files_to_publish paths, commit them, push the "
        "branch, and retry the pull-request creation step. Do not claim PR "
        "creation succeeded until a PR URL is returned."
    )


def _required_shell_command_item(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PowdrrExecutionError(
            "Workflow invoke_tool action command items must be non-empty strings."
        )
    return value.strip()


def _wrap_shell_command(command: str) -> str:
    """Run a shell tool command through rtk without wrapping it twice."""
    if (
        shutil.which("rtk") is None
        or _shell_command_starts_with_rtk(command)
        or _shell_command_is_unwrapped(command)
    ):
        return command
    return f"rtk {command}"


def _rtk_command_display(command: str) -> str:
    return _wrap_shell_command(command)


def _wrap_argument_command(command: list[str]) -> list[str]:
    if shutil.which("rtk") is None or (
        command and (command[0] == "rtk" or command[0] in _RTK_BYPASS_COMMANDS)
    ):
        return command
    return ["rtk", *command]


def _shell_command_starts_with_rtk(command: str) -> bool:
    try:
        command_items = shlex.split(command)
    except ValueError:
        return False
    return bool(command_items) and command_items[0] == "rtk"


def _shell_command_is_unwrapped(command: str) -> bool:
    try:
        command_items = shlex.split(command)
    except ValueError:
        return False
    return bool(command_items) and command_items[0] in _RTK_BYPASS_COMMANDS


def _run_deterministic_pre_step(
    step: Any,
    *,
    process_id: str,
    worktree_root: Path,
    execution_events: list[dict[str, Any]],
    execution_context: list[str],
    handoff_records: Mapping[str, Mapping[str, Any]],
    step_index: int,
    workflow_context: WorkflowContext | None,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
    verbose: bool = False,
    force: bool = False,
    runtime: ExecutionRuntime | None = None,
) -> None:
    if runtime is None:
        runtime = ExecutionRuntime(
            "chat-pre-step-"
            + hashlib.sha256(str(worktree_root).encode()).hexdigest()[:24],
            profile_id="default",
            workflow_directory=worktree_root.parent / ".powdrr-execution",
            repo_root=worktree_root,
        )
    if not force and _latest_deterministic_pre_step(
        execution_events,
        process_id=process_id,
        step_index=step_index,
    ):
        return
    pre_step = step.pre_step
    if pre_step is None:
        raise PowdrrExecutionError("invoke_tool steps require a pre_step.")
    template = _resolve_pre_step_template(
        pre_step.template,
        _pre_step_context_values(
            handoff_records,
            workflow_context,
            execution_events=execution_events,
        ),
    )
    if not isinstance(template, Mapping):
        raise PowdrrExecutionError(
            "Deterministic pre-step template must resolve to an object."
        )
    if pre_step.action == "invoke_tool":
        tool = template.get("tool")
        if not isinstance(tool, str):
            raise PowdrrExecutionError("Invoke tool pre-step template requires a tool.")
        parameters = dict(template)
        parameters.pop("tool", None)
        if tool == ENRICH_TOOL:
            parameters.pop("tool", None)
            _wire_previous_tool_output(parameters, execution_events, handoff_records)
            result = invoke_intrinsic_capability(
                ENRICH_TOOL, parameters, worktree_root=worktree_root, runtime=runtime
            )
        elif tool == VALIDATE_EDIT_TOOL:
            parameters.pop("tool", None)
            result = invoke_deferred_edit_capability(
                VALIDATE_EDIT_TOOL,
                parameters,
                worktree_root=worktree_root,
                runtime=runtime,
            )
        elif tool == APPLY_EDIT_TOOL:
            parameters.pop("tool", None)
            result = invoke_deferred_edit_capability(
                APPLY_EDIT_TOOL,
                parameters,
                worktree_root=worktree_root,
                runtime=runtime,
            )
        elif tool == "fuzzy-match":
            result = invoke_fuzzy_match_capability(
                parameters,
                worktree_root=worktree_root,
                runtime=runtime,
            )
        elif tool == _INTERNAL_TOOL and _is_feature_context_command(
            parameters.get("command")
        ):
            command = command_items_for_validation(parameters.get("command"))
            feature_query = _extract_command_option(command, "--feature-query")
            if feature_query is None:
                raise PowdrrExecutionError(
                    "resolve-feature-context requires --feature-query."
                )
            result = invoke_repository_read(
                "feature_context",
                {"feature_query": feature_query},
                worktree_root=worktree_root,
                executor=lambda _arguments: resolve_feature_context(
                    feature_query, worktree_root
                ),
                runtime=runtime,
            )
        elif tool == _INTERNAL_TOOL and _is_authoritative_effect_command(
            parameters.get("command")
        ):
            command = command_items_for_validation(parameters.get("command"))
            work_item_name = _extract_command_option(command, "--work-item-name")
            if work_item_name is None:
                raise PowdrrExecutionError(
                    "authoritative-pr-effects requires --work-item-name."
                )
            result = invoke_repository_read(
                "authoritative_pr_effects",
                {"work_item_name": work_item_name},
                worktree_root=worktree_root,
                executor=lambda _arguments: build_authoritative_effect_handoff(
                    work_item_name=work_item_name,
                    repo_root=worktree_root,
                ),
                runtime=runtime,
            )
        elif tool in {"shell", _INTERNAL_TOOL}:
            if tool == _INTERNAL_TOOL and parameters.get("help") is not True:
                validate_internal_command(parameters.get("command"))
            result = invoke_shell_capability(
                {**parameters, "_tool_name": tool},
                worktree_root=worktree_root,
                executor=lambda invocation: _execute_shell_tool(
                    dict(invocation),
                    worktree_root=worktree_root,
                    stdout=stdout,
                    stderr=stderr,
                    verbose=verbose,
                    announce=False,
                ),
                runtime=runtime,
            )
        elif tool in {GIT_TOOL, GH_TOOL}:
            result = invoke_intrinsic_capability(
                tool, parameters, worktree_root=worktree_root, runtime=runtime
            )
        elif is_basedpyright_tool(tool):
            result = invoke_basedpyright_capability(
                tool,
                parameters,
                worktree_root=worktree_root,
                runtime=runtime,
            )
        else:
            raise PowdrrExecutionError(
                f"Unsupported invoke_tool pre-step tool: {tool!r}"
            )
        event = {
            "kind": "deterministic_pre_step",
            "process_id": process_id,
            "step_type": step.step_type,
            "action": pre_step.action,
            "tool": tool,
            "template": template,
            "result": result,
            "step_index": step_index,
        }
        execution_events.append(event)
        record_runtime_readiness_from_pre_step(step, runtime)
        if isinstance(handoff_records, dict):
            for output in step.outputs:
                output_value = (
                    runtime_readiness_report(runtime)
                    if output.name == "readiness_report"
                    else result
                )
                handoff_records[output.name] = {
                    "name": output.name,
                    "type": output.type,
                    "value": output_value,
                    "produced_by": {
                        "step_index": step_index,
                        "action": "deterministic_pre_step",
                    },
                    "scope": output.scope,
                }
        execution_context.append(
            "Deterministic invoke_tool result:\n"
            + json.dumps(result, ensure_ascii=False)
        )
        return
    if pre_step.action != "gather_context":
        raise PowdrrExecutionError(
            "invoke_tool pre-steps must use gather_context or invoke_tool."
        )
    raw_types = template.get("types")
    if (
        not isinstance(raw_types, Sequence)
        or isinstance(raw_types, (str, bytes, bytearray))
        or not raw_types
    ):
        raise PowdrrExecutionError(
            "Deterministic gather_context template requires types."
        )
    feature_id = template.get("feature_id")
    if not isinstance(feature_id, str) or not feature_id.strip():
        raise PowdrrExecutionError(
            "Deterministic gather_context template requires feature_id."
        )
    keywords = template.get("keywords")
    filters = template.get("filters")
    gathered_context = invoke_repository_read(
        "gather_context",
        dict(template),
        worktree_root=worktree_root,
        executor=lambda _arguments: gather_specification_context(
            worktree_root,
            types=[str(value) for value in raw_types],
            keywords=(
                [str(value) for value in keywords]
                if isinstance(keywords, Sequence)
                and not isinstance(keywords, (str, bytes, bytearray))
                else None
            ),
            filters=dict(filters) if isinstance(filters, Mapping) else None,
            feature_id=feature_id,
        ),
        runtime=runtime,
    )
    result = json.loads(render_gather_context_report(gathered_context))
    event = {
        "kind": "deterministic_pre_step",
        "process_id": process_id,
        "step_type": step.step_type,
        "action": pre_step.action,
        "template": template,
        "result": result,
        "step_index": step_index,
    }
    execution_events.append(event)
    if isinstance(handoff_records, dict):
        for output in step.outputs:
            if output.required_for_next_step:
                handoff_records[output.name] = {
                    "name": output.name,
                    "type": output.type,
                    "value": result,
                    "produced_by": {
                        "step_index": step_index,
                        "action": "deterministic_pre_step",
                    },
                    "scope": output.scope,
                }
    execution_context.append(
        "Deterministic pre-step gather_context result:\n"
        + json.dumps(result, ensure_ascii=False)
    )


def _execute_shell_tool(
    parameters: dict[str, Any],
    *,
    worktree_root: Path,
    stdout: TextIO,
    stderr: TextIO,
    verbose: bool,
    announce: bool = True,
    print_stdout: bool = True,
) -> dict[str, Any]:
    if parameters.get("help") is True:
        return builtin_tool_help(str(parameters.get("_tool_name", "shell")))
    command = parameters.get("command")
    validation_command: str | Sequence[str]
    retry_command_source: list[str] | None = None
    if isinstance(command, str):
        command_display = _rtk_command_display(command)
        run_command: str | list[str] = _wrap_shell_command(command)
        use_shell = True
        validation_command = command
        try:
            command_items = shlex.split(command)
        except ValueError:
            command_items = []
        if not any(item in {"&&", ";", "|", ">", "<"} for item in command_items):
            retry_command_source = command_items
    elif isinstance(command, Sequence) and not isinstance(
        command,
        (str, bytes, bytearray),
    ):
        normalized_command = [_required_shell_command_item(item) for item in command]
        wrapped_command = _wrap_argument_command(normalized_command)
        command_display = " ".join(shlex.quote(item) for item in wrapped_command)
        run_command = wrapped_command
        use_shell = False
        validation_command = normalized_command
        retry_command_source = normalized_command
    else:
        raise PowdrrExecutionError(
            "Workflow invoke_tool action parameters must include a command."
        )

    empty_pr_error = _empty_pull_request_error(validation_command, worktree_root)
    if empty_pr_error is not None:
        print(empty_pr_error, file=stderr)
        return {
            "command": command_display,
            "cwd": str(worktree_root.resolve()),
            "returncode": 2,
            "stdout": "",
            "stderr": empty_pr_error,
        }

    cwd_value = parameters.get("cwd")
    if cwd_value is None:
        resolved_cwd = worktree_root
    elif isinstance(cwd_value, str) and cwd_value.strip():
        cwd_path = Path(cwd_value.strip())
        resolved_cwd = (
            cwd_path if cwd_path.is_absolute() else worktree_root / cwd_path
        ).resolve(strict=False)
        resolved_worktree_root = worktree_root.resolve(strict=False)
        if not resolved_cwd.is_relative_to(resolved_worktree_root):
            raise PowdrrExecutionError(
                "Workflow shell tool cwd must stay within the current worktree: "
                f"{resolved_worktree_root}"
            )
    else:
        raise PowdrrExecutionError("Workflow invoke_tool action cwd must be a string.")

    env_value = parameters.get("env")
    env = os.environ.copy()
    env["POWDRR_FILE_ADDED_EVENTS"] = "1"
    worktree_python_path = str(worktree_root.resolve())
    existing_python_path = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        worktree_python_path
        if not existing_python_path
        else worktree_python_path + os.pathsep + existing_python_path
    )
    if env_value is not None:
        if not isinstance(env_value, dict):
            raise PowdrrExecutionError(
                "Workflow invoke_tool action env must be an object."
            )
        for key, value in env_value.items():
            if not isinstance(key, str) or not key:
                raise PowdrrExecutionError(
                    "Workflow invoke_tool action env keys must be non-empty strings."
                )
            if not isinstance(value, str):
                raise PowdrrExecutionError(
                    "Workflow invoke_tool action env values must be strings."
                )
            env[key] = value

    if announce:
        print(f"Invoking shell tool: {command_display}", file=stdout)
        _verbose_print(stderr, verbose, f"Invoking shell tool: {command_display}")
    process = subprocess.run(
        run_command,
        shell=use_shell,
        check=False,
        capture_output=True,
        text=True,
        cwd=resolved_cwd,
        env=env,
    )
    attempted_commands = [command_display]
    if (
        process.returncode != 0
        and retry_command_source is not None
        and missing_executable_output(
            stdout=process.stdout,
            stderr=process.stderr,
        )
    ):
        for variant in dependency_backed_command_variants(
            retry_command_source,
            project_root=resolved_cwd,
        ):
            retry_command = list(variant.command)
            if use_shell:
                retry_run_command: str | list[str] = _wrap_shell_command(
                    shlex.join(retry_command)
                )
                retry_shell = True
            else:
                retry_run_command = _wrap_argument_command(retry_command)
                retry_shell = False
            retry_display = (
                retry_run_command
                if isinstance(retry_run_command, str)
                else " ".join(shlex.quote(item) for item in retry_run_command)
            )
            _verbose_print(
                stderr,
                verbose,
                f"Retrying shell tool with project dependency metadata: "
                f"{retry_display} ({variant.reason})",
            )
            process = subprocess.run(
                retry_run_command,
                shell=retry_shell,
                check=False,
                capture_output=True,
                text=True,
                cwd=resolved_cwd,
                env=env,
            )
            attempted_commands.append(retry_display)
            command_display = retry_display
            if process.returncode == 0 or not missing_executable_output(
                stdout=process.stdout,
                stderr=process.stderr,
            ):
                break
    original_returncode = process.returncode
    process = _normalize_noop_git_commit_result(
        process, validation_command, resolved_cwd
    )
    if process.stdout:
        if print_stdout:
            print(process.stdout, end="", file=stdout)
            _verbose_print(
                stderr, verbose, f"Shell tool stdout:\n{process.stdout.rstrip()}"
            )
    if process.stderr:
        print(process.stderr, end="", file=stderr)
    _verbose_print(
        stderr,
        verbose,
        f"Shell tool exited with code {process.returncode}",
    )
    result = {
        "command": command_display,
        "cwd": str(resolved_cwd),
        "returncode": process.returncode,
        "stdout": process.stdout,
        "stderr": process.stderr,
        "no_op": original_returncode != 0 and process.returncode == 0,
    }
    if len(attempted_commands) > 1:
        result["attempted_commands"] = attempted_commands
    return result


def _run_coding_loop_verification(
    step: Any,
    *,
    worktree_root: Path,
    stdout: TextIO,
    stderr: TextIO,
    verbose: bool,
    runtime: ExecutionRuntime | None,
) -> dict[str, Any] | None:
    coding_loop = getattr(step, "coding_loop", None)
    if coding_loop is None or not coding_loop.verification:
        return None
    results: list[dict[str, Any]] = []
    for verification in coding_loop.verification:
        command = (
            verification.command
            if isinstance(verification.command, str)
            else list(verification.command)
        )
        result = invoke_shell_capability(
            {
                "command": command,
                "cwd": verification.cwd,
                "env": dict(verification.env),
                "_tool_name": "coding_loop_verification",
            },
            worktree_root=worktree_root,
            executor=lambda parameters: _execute_shell_tool(
                dict(parameters),
                worktree_root=worktree_root,
                stdout=stdout,
                stderr=stderr,
                verbose=verbose,
                announce=False,
                print_stdout=False,
            ),
            runtime=runtime,
        )
        passed = isinstance(result, Mapping) and result.get("returncode") == 0
        status = "passed" if passed else "failed"
        returncode = result.get("returncode") if isinstance(result, Mapping) else None
        print(
            f"Coding-loop verification {verification.id}: {status} (exit {returncode})",
            file=stderr,
            flush=True,
        )
        if not passed and isinstance(result, Mapping):
            verification_stdout = result.get("stdout")
            if isinstance(verification_stdout, str) and verification_stdout:
                print(
                    f"Coding-loop verification output:\n{verification_stdout[-4000:]}",
                    file=stderr,
                    flush=True,
                )
        results.append(
            {
                "id": verification.id,
                "command": command,
                "passed": passed,
                "result": result,
            }
        )
    all_passed = all(item["passed"] for item in results)
    return {
        "results": results,
        "all_passed": all_passed,
        "worktree_fingerprint": _coding_loop_worktree_fingerprint(worktree_root),
        "error": None if all_passed else "One or more coding-loop checks failed.",
    }


def _coding_loop_worktree_fingerprint(worktree_root: Path) -> str:
    """Hash material worktree contents for verification evidence binding."""
    digest = hashlib.sha256()
    ignored_directories = {".git", ".venv", "__pycache__", ".pytest_cache"}
    for directory, dirnames, filenames in os.walk(worktree_root, followlinks=False):
        dirnames[:] = sorted(
            name for name in dirnames if name not in ignored_directories
        )
        for filename in sorted(filenames):
            path = Path(directory) / filename
            relative_path = path.relative_to(worktree_root).as_posix()
            digest.update(relative_path.encode("utf-8"))
            digest.update(b"\0")
            try:
                if path.is_symlink():
                    digest.update(b"symlink\0")
                    digest.update(os.readlink(path).encode("utf-8"))
                else:
                    digest.update(b"file\0")
                    digest.update(path.read_bytes())
            except OSError as error:
                digest.update(f"unreadable:{error}".encode())
            digest.update(b"\0")
    return digest.hexdigest()


def _validate_coding_loop_action(
    step: Any,
    events: Sequence[Mapping[str, Any]],
    *,
    action_kind: str,
    step_index: int | None = None,
    worktree_root: Path | None = None,
) -> None:
    """Enforce coding-loop completion from typed events, not model guidance."""
    coding_loop = getattr(step, "coding_loop", None)
    if coding_loop is None or not coding_loop.verification:
        return
    latest = next(
        (
            event
            for event in reversed(events)
            if event.get("kind") == "coding_loop_verification"
            and (step_index is None or event.get("step_index") == step_index)
        ),
        None,
    )
    if action_kind == "next_step" and (
        latest is None or latest.get("all_passed") is not True
    ):
        raise PowdrrExecutionError(
            "This coding_loop step cannot choose next_step until its declared "
            "verification commands pass. Make or repair the implementation, "
            "then wait for the automatic verification result."
        )
    if (
        latest is not None
        and latest.get("all_passed") is True
        and action_kind != "next_step"
    ):
        raise PowdrrExecutionError(
            "This coding_loop step has already passed all declared checks. "
            "Choose next_step with the required output_state; do not perform "
            "another edit, read, or verification run."
        )
    if latest is not None and latest.get("all_passed") is True:
        recorded_fingerprint = latest.get("worktree_fingerprint")
        if (
            worktree_root is not None
            and isinstance(recorded_fingerprint, str)
            and recorded_fingerprint != _coding_loop_worktree_fingerprint(worktree_root)
        ):
            raise PowdrrExecutionError(
                "This coding_loop verification is stale because the worktree "
                "changed after the checks passed. Run the declared verification "
                "commands again before choosing next_step."
            )
