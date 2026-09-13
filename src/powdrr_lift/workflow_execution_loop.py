"""Execution-loop mechanics for validated workflow steps.

This module owns deterministic execution and evidence-producing checks.
The chat agent orchestrates model interaction but does not implement these
operations.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
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
from powdrr_lift.workflow_action_operations import _resolve_pre_step_template
from powdrr_lift.workflow_action_validation import (
    _command_items_for_validation,
    _record_runtime_readiness_from_pre_step,
    _runtime_readiness_report,
    _validate_internal_command,
)
from powdrr_lift.workflow_chat_io import _verbose_print
from powdrr_lift.workrr.context import WorkflowContext

_INTERNAL_TOOL = "internal"


def _chat_support(name: str) -> Any:
    from powdrr_lift import workflow_chat_agent

    return getattr(workflow_chat_agent, name)


def _support_call(name: str, *args: Any, **kwargs: Any) -> Any:
    return _chat_support(name)(*args, **kwargs)


def _empty_pull_request_error(*args: Any, **kwargs: Any) -> Any:
    return _support_call("_empty_pull_request_error", *args, **kwargs)


def _normalize_noop_git_commit_result(*args: Any, **kwargs: Any) -> Any:
    return _support_call("_normalize_noop_git_commit_result", *args, **kwargs)


def _required_shell_command_item(*args: Any, **kwargs: Any) -> Any:
    return _support_call("_required_shell_command_item", *args, **kwargs)


def _rtk_command_display(*args: Any, **kwargs: Any) -> Any:
    return _support_call("_rtk_command_display", *args, **kwargs)


def _wrap_argument_command(*args: Any, **kwargs: Any) -> Any:
    return _support_call("_wrap_argument_command", *args, **kwargs)


def _wrap_shell_command(*args: Any, **kwargs: Any) -> Any:
    return _support_call("_wrap_shell_command", *args, **kwargs)


def _latest_deterministic_pre_step(*args: Any, **kwargs: Any) -> Any:
    return _support_call("_latest_deterministic_pre_step", *args, **kwargs)


def _pre_step_context_values(*args: Any, **kwargs: Any) -> Any:
    return _support_call("_pre_step_context_values", *args, **kwargs)


def _wire_previous_tool_output(*args: Any, **kwargs: Any) -> Any:
    return _support_call("_wire_previous_tool_output", *args, **kwargs)


def _extract_command_option(*args: Any, **kwargs: Any) -> Any:
    return _support_call("_extract_command_option", *args, **kwargs)


def _is_authoritative_effect_command(*args: Any, **kwargs: Any) -> Any:
    return _support_call("_is_authoritative_effect_command", *args, **kwargs)


def _gate_outcome_matches(*args: Any, **kwargs: Any) -> Any:
    return _support_call("_gate_outcome_matches", *args, **kwargs)


def _run_deterministic_pre_step(
    step: Any,
    *,
    skill_name: str,
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
        skill_name=skill_name,
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
        elif tool == _INTERNAL_TOOL and _is_authoritative_effect_command(
            parameters.get("command")
        ):
            command = _command_items_for_validation(parameters.get("command"))
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
                _validate_internal_command(parameters.get("command"))
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
            "skill_name": skill_name,
            "step_type": step.step_type,
            "action": pre_step.action,
            "tool": tool,
            "template": template,
            "result": result,
            "step_index": step_index,
        }
        execution_events.append(event)
        _record_runtime_readiness_from_pre_step(step, runtime)
        if isinstance(handoff_records, dict):
            for output in step.outputs:
                output_value = (
                    _runtime_readiness_report(runtime)
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
        "skill_name": skill_name,
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


def _run_gate(
    step: Any,
    *,
    skill_name: str,
    worktree_root: Path,
    execution_events: list[dict[str, Any]],
    execution_context: list[str],
    handoff_records: Mapping[str, Mapping[str, Any]],
    step_index: int,
    workflow_context: WorkflowContext | None,
    stdout: TextIO,
    stderr: TextIO,
    verbose: bool,
    runtime: ExecutionRuntime | None = None,
) -> bool:
    if step.gate is None or step.pre_step is None:
        raise PowdrrExecutionError(
            "gate steps require gate and invoke_tool pre_step settings."
        )
    before = len(execution_events)
    _run_deterministic_pre_step(
        step,
        skill_name=skill_name,
        worktree_root=worktree_root,
        execution_events=execution_events,
        execution_context=execution_context,
        handoff_records=handoff_records,
        step_index=step_index,
        workflow_context=workflow_context,
        stdout=stdout,
        stderr=stderr,
        verbose=verbose,
        force=True,
        runtime=runtime,
    )
    event = execution_events[-1] if len(execution_events) > before else None
    if not isinstance(event, Mapping) or not isinstance(event.get("result"), Mapping):
        raise PowdrrExecutionError("Gate tool did not produce a structured result.")
    passed = _gate_outcome_matches(event["result"], step.gate.outcome)
    gate_event = {
        "kind": "gate",
        "skill_name": skill_name,
        "step_index": step_index,
        "passed": passed,
        "outcome": dict(step.gate.outcome),
        "result": event["result"],
    }
    execution_events.append(gate_event)
    step_id = getattr(step, "id", None) or f"step-{step_index + 1}"
    status = "passed" if passed else "failed"
    print(
        f"Workflow gate evaluation ({skill_name}/{step_id}): {status}\n"
        "Workflow gate result: "
        + json.dumps(event["result"], ensure_ascii=False, sort_keys=True),
        file=stderr,
        flush=True,
    )
    if not passed:
        execution_context.append(
            f"Gate failed: {json.dumps(event['result'], ensure_ascii=False)}. "
            + step.gate.retry_context
        )
    return passed


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


def _require_coding_loop_verification(
    step: Any,
    events: Sequence[Mapping[str, Any]],
    *,
    step_index: int | None = None,
    worktree_root: Path | None = None,
) -> None:
    _validate_coding_loop_action(
        step,
        events,
        action_kind="next_step",
        step_index=step_index,
        worktree_root=worktree_root,
    )


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


def _print_waiting_for_model(stderr: TextIO, model: str) -> None:
    print(f"waiting for {model} LLM response...", file=stderr, flush=True)
