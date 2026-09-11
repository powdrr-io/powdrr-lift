"""Validated action handlers for interactive workflow execution.

The chat agent owns orchestration and model interaction. This module owns the
state mutations and tool dispatch for each validated action kind.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, TextIO

from powdrr_lift.basedpyright_tools import is_basedpyright_tool
from powdrr_lift.builtin_tool_help import builtin_tool_help
from powdrr_lift.core.spec_context import (
    gather_specification_context,
    render_gather_context_report,
)
from powdrr_lift.errors import PowdrrExecutionError
from powdrr_lift.execution.builtin_tools import (
    invoke_basedpyright_capability,
    invoke_deferred_edit_capability,
    invoke_file_mutation,
    invoke_fuzzy_match_capability,
    invoke_intrinsic_capability,
    invoke_repository_read,
    invoke_shell_capability,
)
from powdrr_lift.file_management import manage_worktree_file
from powdrr_lift.fuzzy_match import fuzzy_match_json
from powdrr_lift.intrinsic_edit import APPLY_EDIT_TOOL, VALIDATE_EDIT_TOOL
from powdrr_lift.intrinsic_enrich import ENRICH_TOOL
from powdrr_lift.intrinsic_git_gh import GH_TOOL, GIT_TOOL
from powdrr_lift.workflow_action_operations import (
    WorkflowYamlEditError as _WorkflowYamlEditError,
)
from powdrr_lift.workflow_action_operations import (
    _apply_file_edits,
    _apply_yaml_operations,
    _list_worktree_files,
    _record_skill_pull_request,
)
from powdrr_lift.workflow_action_validation import (
    _command_items_for_validation,
    _validate_internal_command,
    _validate_structured_document_text,
    _worktree_relative_path,
)
from powdrr_lift.workflow_chat_selection import WorkflowChatConfig
from powdrr_lift.workflow_execution_loop import _execute_shell_tool
from powdrr_lift.workflow_execution_state import (
    _ensure_execution_runtime,
    _record_durable_fact,
    _WorkflowExecutionState,
)
from powdrr_lift.workflow_llm import (
    WorkflowAction as SkillChatAction,
)
from powdrr_lift.workflow_llm import (
    WorkflowFileEdits as SkillChatFileEdits,
)
from powdrr_lift.workflow_paths import resolve_worktree_file_path

_INTERNAL_TOOL = "internal"
_MAX_DOCUMENT_CONTEXT_LINES = 2000


def _chat_support(name: str) -> Any:
    from powdrr_lift import workflow_chat_agent

    return getattr(workflow_chat_agent, name)


def _support_call(name: str, *args: Any, **kwargs: Any) -> Any:
    return _chat_support(name)(*args, **kwargs)


def _file_edits_to_data(*args: Any, **kwargs: Any) -> Any:
    return _support_call("_file_edits_to_data", *args, **kwargs)


def _normalize_structured_document_text(*args: Any, **kwargs: Any) -> Any:
    return _support_call("_normalize_structured_document_text", *args, **kwargs)


def _verbose_print(*args: Any, **kwargs: Any) -> Any:
    return _support_call("_verbose_print", *args, **kwargs)


def _yaml_operation_to_data(*args: Any, **kwargs: Any) -> Any:
    return _support_call("_yaml_operation_to_data", *args, **kwargs)


def _prompt_user(*args: Any, **kwargs: Any) -> Any:
    return _support_call("_prompt_user", *args, **kwargs)


def _resolve_generated_file_path_from_command(*args: Any, **kwargs: Any) -> Any:
    return _support_call("_resolve_generated_file_path_from_command", *args, **kwargs)


def _workflow_action_handlers() -> dict[
    str,
    Callable[
        [
            SkillChatAction,
            _WorkflowExecutionState,
            TextIO,
            TextIO,
            Callable[[], str],
            WorkflowChatConfig,
        ],
        bool,
    ],
]:
    return {
        "complete": _handle_workflow_action_complete,
        "edit": _handle_workflow_action_edit,
        "yaml_edit": _handle_workflow_action_yaml_edit,
        "file_management": _handle_workflow_action_file_management,
        "delete_file": _handle_workflow_action_file_management,
        "read_document": _handle_workflow_action_read_document,
        "list_files": _handle_workflow_action_list_files,
        "next_step": _handle_workflow_action_next_step,
        "emit_outputs": _handle_workflow_action_emit_outputs,
        "prompt_user": _handle_workflow_action_prompt_user,
        "invoke_tool": _handle_workflow_action_invoke_tool,
        "gather_context": _handle_workflow_action_gather_context,
    }


def _handle_workflow_action_complete(
    action: SkillChatAction,
    state: _WorkflowExecutionState,
    stdout: TextIO,
    stderr: TextIO,
    input_func: Callable[[], str],
    config: WorkflowChatConfig,
) -> bool:
    _ = stderr
    _ = input_func
    _ = config
    if action.text:
        print(action.text, file=stdout)
    if action.decisions_and_context:
        _record_durable_fact(
            state,
            action.decisions_and_context,
            kind="decision",
            source=action.kind,
        )
    state.execution_events.append(
        {
            "kind": action.kind,
            "text": action.text,
            "decisions_and_context": action.decisions_and_context,
        }
    )
    return False


def _handle_workflow_action_edit(
    action: SkillChatAction,
    state: _WorkflowExecutionState,
    stdout: TextIO,
    stderr: TextIO,
    input_func: Callable[[], str],
    config: WorkflowChatConfig,
) -> bool:
    _ = input_func
    _ = config
    file_edits = action.file_edits
    if not file_edits:
        if action.file_path is None:
            raise PowdrrExecutionError("Workflow edit action must include file_path.")
        file_edits = (SkillChatFileEdits(action.file_path, action.edits),)

    pending_writes: list[tuple[Path, str]] = []
    results: list[dict[str, Any]] = []
    for file_edit in file_edits:
        target_path = resolve_worktree_file_path(
            file_edit.file_path,
            state.worktree_root,
        )
        current_text = ""
        if target_path.exists():
            current_text = target_path.read_text(encoding="utf-8")
        state.current_file_path = target_path
        updated_text = _apply_file_edits(current_text, file_edit.edits)
        updated_text = _normalize_structured_document_text(target_path, updated_text)
        _validate_structured_document_text(target_path, updated_text)
        pending_writes.append((target_path, updated_text))
        results.append(
            {
                "file_path": str(target_path),
                "line_count": len(updated_text.splitlines()),
            }
        )

    invoke_file_mutation(
        tuple(
            _worktree_relative_path(target_path, state.worktree_root)
            for target_path, _updated_text in pending_writes
        ),
        worktree_root=state.worktree_root,
        executor=lambda: _write_pending_file_mutations(pending_writes),
        runtime=_ensure_execution_runtime(state),
    )
    if state.file_added_callback is not None:
        state.file_added_callback(
            tuple(
                _worktree_relative_path(target_path, state.worktree_root)
                for target_path, _updated_text in pending_writes
            )
        )
    state.fuzzy_match_cache.clear()

    if action.decisions_and_context:
        _record_durable_fact(
            state,
            action.decisions_and_context,
            kind="decision",
            source=action.kind,
        )
    action_data = {
        "kind": action.kind,
        "file_edits": [_file_edits_to_data(group) for group in file_edits],
    }
    state.transcript.append(
        {
            "role": "assistant",
            "content": json.dumps(action_data, ensure_ascii=False),
        }
    )
    state.transcript.append(
        {
            "role": "user",
            "content": json.dumps({"edit_result": results}, ensure_ascii=False),
        }
    )
    state.execution_events.append(
        {
            "kind": action.kind,
            "file_edits": [_file_edits_to_data(group) for group in file_edits],
            "result": results,
            "decisions_and_context": action.decisions_and_context,
            "step_index": state.step_index,
        }
    )
    for result in results:
        print(f"Edited file: {result['file_path']}", file=stdout)
        _verbose_print(
            stderr,
            config.verbose,
            f"Applied edit to {result['file_path']}",
        )
    return True


def _write_pending_file_mutations(pending_writes: Sequence[tuple[Path, str]]) -> None:
    for target_path, updated_text in pending_writes:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text(updated_text, encoding="utf-8")


def _handle_workflow_action_yaml_edit(
    action: SkillChatAction,
    state: _WorkflowExecutionState,
    stdout: TextIO,
    stderr: TextIO,
    input_func: Callable[[], str],
    config: WorkflowChatConfig,
) -> bool:
    _ = input_func
    if action.file_path is None:
        raise _WorkflowYamlEditError(
            "yaml_edit requires file_path. Use a repository-relative .yaml or "
            ".yml path."
        )
    target_path = resolve_worktree_file_path(action.file_path, state.worktree_root)
    if not target_path.exists():
        raise _WorkflowYamlEditError(
            f"yaml_edit target {action.file_path!r} does not exist; no file was "
            "changed. Read or generate the YAML document before applying "
            "structural edits."
        )
    state.current_file_path = target_path
    current_text = target_path.read_text(encoding="utf-8")
    updated_text = _apply_yaml_operations(
        target_path,
        current_text,
        action.yaml_operations,
    )
    _validate_structured_document_text(target_path, updated_text)
    invoke_file_mutation(
        (action.file_path,),
        worktree_root=state.worktree_root,
        executor=lambda: target_path.write_text(updated_text, encoding="utf-8"),
        runtime=_ensure_execution_runtime(state),
    )
    if state.file_added_callback is not None:
        state.file_added_callback(
            (_worktree_relative_path(target_path, state.worktree_root),)
        )
    state.fuzzy_match_cache.clear()

    if action.decisions_and_context:
        _record_durable_fact(
            state,
            action.decisions_and_context,
            kind="decision",
            source=action.kind,
        )
    action_data = {
        "kind": action.kind,
        "file_path": action.file_path,
        "operations": [
            _yaml_operation_to_data(operation) for operation in action.yaml_operations
        ],
    }
    result = {
        "file_path": str(target_path),
        "line_count": len(updated_text.splitlines()),
    }
    state.transcript.extend(
        [
            {
                "role": "assistant",
                "content": json.dumps(action_data, ensure_ascii=False),
            },
            {
                "role": "user",
                "content": json.dumps({"yaml_edit_result": result}, ensure_ascii=False),
            },
        ]
    )
    state.execution_events.append(
        {
            **action_data,
            "result": result,
            "decisions_and_context": action.decisions_and_context,
            "step_index": state.step_index,
        }
    )
    print(f"Edited YAML file: {target_path}", file=stdout)
    _verbose_print(stderr, config.verbose, f"Applied YAML edit to {target_path}")
    return True


def _handle_workflow_action_file_management(
    action: SkillChatAction,
    state: _WorkflowExecutionState,
    stdout: TextIO,
    stderr: TextIO,
    input_func: Callable[[], str],
    config: WorkflowChatConfig,
) -> bool:
    _ = input_func
    if action.file_operation is None or action.file_path is None:
        raise PowdrrExecutionError(
            "file_management action requires operation and file_path."
        )
    file_operation = action.file_operation
    file_path = action.file_path
    mutation_paths: tuple[str, ...] = (file_path,)
    if action.destination_path is not None:
        mutation_paths += (action.destination_path,)
    result = invoke_file_mutation(
        mutation_paths,
        worktree_root=state.worktree_root,
        executor=lambda: manage_worktree_file(
            state.worktree_root,
            operation=file_operation,
            file_path=file_path,
            destination_path=action.destination_path,
        ),
        runtime=_ensure_execution_runtime(state),
    )
    if state.file_added_callback is not None:
        changed_paths = [action.file_path]
        if action.destination_path is not None:
            changed_paths.append(action.destination_path)
        state.file_added_callback(tuple(changed_paths))
    if action.decisions_and_context:
        _record_durable_fact(
            state,
            action.decisions_and_context,
            kind="decision",
            source=action.kind,
        )
    action_data = {
        "kind": action.kind,
        "operation": action.file_operation,
        "file_path": action.file_path,
        "destination_path": action.destination_path,
    }
    state.transcript.extend(
        [
            {"role": "assistant", "content": json.dumps(action_data)},
            {
                "role": "user",
                "content": json.dumps(
                    {"file_management_result": result}, ensure_ascii=False
                ),
            },
        ]
    )
    state.execution_events.append(
        {
            **action_data,
            "result": result,
            "decisions_and_context": action.decisions_and_context,
            "step_index": state.step_index,
        }
    )
    state.fuzzy_match_cache.clear()
    print(
        f"File {action.file_operation}: {result['file_path']}"
        + (
            f" -> {result['destination_path']}"
            if result["destination_path"] is not None
            else ""
        ),
        file=stdout,
    )
    _verbose_print(stderr, config.verbose, f"File management result: {result}")
    return True


def _handle_workflow_action_read_document(
    action: SkillChatAction,
    state: _WorkflowExecutionState,
    stdout: TextIO,
    stderr: TextIO,
    input_func: Callable[[], str],
    config: WorkflowChatConfig,
) -> bool:
    _ = stdout
    _ = input_func
    if action.file_path is None:
        raise PowdrrExecutionError(
            "Workflow read_document action must include file_path."
        )
    if action.start_line is None or action.end_line is None:
        raise PowdrrExecutionError(
            "Workflow read_document action must include start_line and end_line."
        )
    effective_start_line = max(1, action.start_line)
    if action.end_line < effective_start_line:
        raise PowdrrExecutionError(
            "Workflow read_document action end_line must be >= start_line."
        )
    target_path = resolve_worktree_file_path(action.file_path, state.worktree_root)
    if not target_path.exists() or not target_path.is_file():
        directory = target_path.parent
        if directory.is_dir():
            directory_files = sorted(
                path.name for path in directory.iterdir() if path.is_file()
            )
            directory_context = (
                f" Files currently in {directory.relative_to(state.worktree_root)}: "
                f"{', '.join(directory_files) or '<no files>'}."
            )
        else:
            directory_context = (
                f" Directory does not exist: "
                f"{directory.relative_to(state.worktree_root)}."
            )
        raise PowdrrExecutionError(
            f"Workflow read_document action file does not exist: {action.file_path}."
            f"{directory_context} Use an exact existing file path; do not infer or "
            "compose a filename from the template id or workflow description."
        )
    lines = target_path.read_text(encoding="utf-8").splitlines()
    if lines and (action.end_line < 1 or action.start_line > len(lines)):
        raise PowdrrExecutionError(
            f"Workflow read_document action line range {action.start_line}-"
            f"{action.end_line} has no overlap with the document, which has "
            f"{len(lines)} lines. Request an overlapping range."
        )

    excerpt_start_line = max(1, action.start_line)
    excerpt_end_line = min(
        action.end_line,
        len(lines),
        excerpt_start_line + _MAX_DOCUMENT_CONTEXT_LINES - 1,
    )
    document_complete = not lines or (
        excerpt_start_line == 1 and excerpt_end_line == len(lines)
    )
    excerpt = {
        "path": str(target_path.relative_to(state.worktree_root)),
        "requested_start_line": action.start_line,
        "requested_end_line": action.end_line,
        "start_line": excerpt_start_line,
        "end_line": excerpt_end_line,
        "document_line_count": len(lines),
        "document_complete": document_complete,
        "next_start_line": None if document_complete else excerpt_end_line + 1,
        "lines": [
            {
                "line_number": line_number,
                "text": lines[line_number - 1],
            }
            for line_number in range(excerpt_start_line, excerpt_end_line + 1)
        ],
    }
    excerpt_text = json.dumps(excerpt, ensure_ascii=False)
    action_data = {
        "kind": action.kind,
        "file_path": action.file_path,
        "start_line": action.start_line,
        "end_line": action.end_line,
    }
    state.transcript.append(
        {
            "role": "assistant",
            "content": json.dumps(action_data, ensure_ascii=False),
        }
    )
    state.transcript.append(
        {
            "role": "user",
            "content": json.dumps(
                {"document_context": excerpt},
                ensure_ascii=False,
            ),
        }
    )
    state.execution_context.append(f"Document context: {excerpt_text}")
    if action.decisions_and_context:
        _record_durable_fact(
            state,
            action.decisions_and_context,
            kind="decision",
            source=action.kind,
        )
    state.execution_events.append(
        {
            "kind": action.kind,
            "file_path": action.file_path,
            "start_line": action.start_line,
            "end_line": action.end_line,
            "result": excerpt,
            "decisions_and_context": action.decisions_and_context,
            "step_index": state.step_index,
        }
    )
    _verbose_print(
        stderr,
        config.verbose,
        f"Read document context {action.file_path}:{action.start_line}-"
        f"{action.end_line}",
    )
    return True


def _handle_workflow_action_list_files(
    action: SkillChatAction,
    state: _WorkflowExecutionState,
    stdout: TextIO,
    stderr: TextIO,
    input_func: Callable[[], str],
    config: WorkflowChatConfig,
) -> bool:
    _ = (stdout, input_func)
    directory = action.directory or "."
    result = invoke_repository_read(
        "list_files",
        {
            "directory": directory,
            "pattern": action.pattern,
            "recursive": action.recursive,
        },
        worktree_root=state.worktree_root,
        executor=lambda _arguments: _list_worktree_files(
            directory,
            action.pattern,
            action.recursive,
            state.worktree_root,
        ),
    )
    action_data = {
        "kind": action.kind,
        "directory": directory,
        "pattern": result["pattern"],
        "recursive": action.recursive,
    }
    state.transcript.extend(
        [
            {"role": "assistant", "content": json.dumps(action_data)},
            {
                "role": "user",
                "content": json.dumps({"list_files_result": result}),
            },
        ]
    )
    state.execution_events.append(
        {
            **action_data,
            "result": result,
            "decisions_and_context": action.decisions_and_context,
            "step_index": state.step_index,
        }
    )
    _verbose_print(stderr, config.verbose, f"Listed files: {result}")
    return True


def _handle_workflow_action_next_step(
    action: SkillChatAction,
    state: _WorkflowExecutionState,
    stdout: TextIO,
    stderr: TextIO,
    input_func: Callable[[], str],
    config: WorkflowChatConfig,
) -> bool:
    _ = stdout
    _ = stderr
    _ = input_func
    _ = config
    if action.decisions_and_context:
        _record_durable_fact(
            state,
            action.decisions_and_context,
            kind="decision",
            source=action.kind,
        )
    state.execution_events.append(
        {
            "kind": action.kind,
            "decisions_and_context": action.decisions_and_context,
            "step_index": state.step_index,
        }
    )
    state.step_index += 1
    return True


def _handle_workflow_action_emit_outputs(
    action: SkillChatAction,
    state: _WorkflowExecutionState,
    stdout: TextIO,
    stderr: TextIO,
    input_func: Callable[[], str],
    config: WorkflowChatConfig,
) -> bool:
    _ = stdout, stderr, input_func, config
    state.execution_events.append(
        {
            "kind": action.kind,
            "outputs": dict(action.outputs),
            "decisions_and_context": action.decisions_and_context,
            "step_index": state.step_index,
        }
    )
    return True


def _handle_workflow_action_prompt_user(
    action: SkillChatAction,
    state: _WorkflowExecutionState,
    stdout: TextIO,
    stderr: TextIO,
    input_func: Callable[[], str],
    config: WorkflowChatConfig,
) -> bool:
    print(action.text or "", file=stdout)
    answer = _prompt_user(
        "> ",
        input_func=input_func,
        stdout=stdout,
        status_stream=stderr,
    )
    _verbose_print(stderr, config.verbose, f"Follow-up answer: {answer}")
    state.transcript.append(
        {
            "role": "assistant",
            "content": action.text or "",
        }
    )
    state.transcript.append({"role": "user", "content": answer})
    if action.decisions_and_context:
        _record_durable_fact(
            state,
            action.decisions_and_context,
            kind="decision",
            source=action.kind,
        )
    state.execution_events.append(
        {
            "kind": action.kind,
            "text": action.text,
            "answer": answer,
            "decisions_and_context": action.decisions_and_context,
            "step_index": state.step_index,
        }
    )
    return True


def _handle_workflow_action_invoke_tool(
    action: SkillChatAction,
    state: _WorkflowExecutionState,
    stdout: TextIO,
    stderr: TextIO,
    input_func: Callable[[], str],
    config: WorkflowChatConfig,
) -> bool:
    _ = input_func
    if action.tool == "fuzzy-match":
        tool_result = invoke_fuzzy_match_capability(
            action.parameters,
            worktree_root=state.worktree_root,
            path_cache=state.fuzzy_match_cache,
            runtime=_ensure_execution_runtime(state),
        )
    elif action.tool == ENRICH_TOOL:
        tool_result = invoke_intrinsic_capability(
            ENRICH_TOOL,
            action.parameters,
            worktree_root=state.worktree_root,
            runtime=_ensure_execution_runtime(state),
        )
    elif action.tool == VALIDATE_EDIT_TOOL:
        tool_result = invoke_deferred_edit_capability(
            VALIDATE_EDIT_TOOL,
            action.parameters,
            worktree_root=state.worktree_root,
            runtime=_ensure_execution_runtime(state),
        )
    elif action.tool == APPLY_EDIT_TOOL:
        tool_result = invoke_deferred_edit_capability(
            APPLY_EDIT_TOOL,
            action.parameters,
            worktree_root=state.worktree_root,
            runtime=_ensure_execution_runtime(state),
        )
    elif action.tool in {"shell", _INTERNAL_TOOL}:
        if action.tool == _INTERNAL_TOOL and action.parameters.get("help") is not True:
            _validate_internal_command(action.parameters.get("command"))
        command_items = (
            []
            if action.parameters.get("help") is True
            else _command_items_for_validation(action.parameters.get("command"))
        )
        tool_result = invoke_shell_capability(
            {**action.parameters, "_tool_name": action.tool},
            worktree_root=state.worktree_root,
            executor=lambda invocation: _execute_shell_tool(
                dict(invocation),
                worktree_root=state.worktree_root,
                stdout=stdout,
                stderr=stderr,
                verbose=config.verbose,
                announce=False,
                print_stdout=not (
                    action.tool == _INTERNAL_TOOL
                    and command_items[1:2] == ["pull-request-description"]
                ),
            ),
            runtime=_ensure_execution_runtime(state),
        )
    elif action.tool in {GIT_TOOL, GH_TOOL}:
        if action.tool == GH_TOOL and action.parameters.get("operation") == "pr_create":
            _ensure_execution_runtime(state).require_publish_readiness()
        tool_result = invoke_intrinsic_capability(
            action.tool,
            action.parameters,
            worktree_root=state.worktree_root,
            runtime=_ensure_execution_runtime(state),
        )
        if tool_result.get("stdout"):
            print(str(tool_result["stdout"]), end="", file=stdout)
        if tool_result.get("stderr"):
            print(str(tool_result["stderr"]), end="", file=stderr)
    elif is_basedpyright_tool(action.tool or ""):
        assert action.tool is not None
        tool_result = invoke_basedpyright_capability(
            action.tool,
            action.parameters,
            worktree_root=state.worktree_root,
            runtime=_ensure_execution_runtime(state),
        )
    else:
        raise PowdrrExecutionError(
            f"Unsupported workflow tool {action.tool!r}; supported tools are shell, "
            "internal, git, gh, enrich, validate_edit, apply_edit, fuzzy-match, "
            "basedpyright-symbol, and basedpyright-structure."
        )
    if (
        action.tool == GIT_TOOL
        and action.parameters.get("operation") == "add"
        and tool_result.get("returncode") == 0
        and state.file_added_callback is not None
    ):
        paths = action.parameters.get("paths")
        if isinstance(paths, Sequence) and not isinstance(paths, (str, bytes)):
            added_paths = tuple(
                path.strip() for path in paths if isinstance(path, str) and path.strip()
            )
            if added_paths:
                state.file_added_callback(added_paths)
    if action.tool in {"shell", GH_TOOL}:
        _record_chat_pull_request(action, state, tool_result)
    inferred_path = _resolve_generated_file_path_from_command(
        action.parameters.get("command"),
        worktree_root=state.worktree_root,
    )
    if inferred_path is not None:
        state.current_file_path = inferred_path
    if action.tool == "shell":
        state.fuzzy_match_cache.clear()
    state.transcript.append(
        {
            "role": "assistant",
            "content": json.dumps(
                {
                    "kind": action.kind,
                    "parameters": action.parameters,
                },
                ensure_ascii=False,
            ),
        }
    )
    state.transcript.append(
        {
            "role": "user",
            "content": json.dumps(
                {"tool_result": tool_result},
                ensure_ascii=False,
            ),
        }
    )
    if action.decisions_and_context:
        _record_durable_fact(
            state,
            action.decisions_and_context,
            kind="decision",
            source=action.kind,
        )
    event = {
        "kind": action.kind,
        "tool": action.tool,
        "parameters": action.parameters,
        "result": tool_result,
        "decisions_and_context": action.decisions_and_context,
        "step_index": state.step_index,
    }
    state.execution_events.append(event)
    state.audit_events.append(event)
    return True


def _record_chat_pull_request(
    action: SkillChatAction,
    state: _WorkflowExecutionState,
    tool_result: Mapping[str, Any],
) -> None:
    _record_skill_pull_request(
        action,
        state.worktree_root,
        state.selected_skill,
        state.audit_events,
        tool_result,
        root_skill=state.root_skill or state.selected_skill,
        step_index=state.step_index,
        runtime=_ensure_execution_runtime(state),
    )


def _execute_fuzzy_match_tool(
    parameters: dict[str, Any],
    *,
    worktree_root: Path,
    path_cache: dict[tuple[str, int, int | None], tuple[Path, ...]] | None = None,
) -> dict[str, Any]:
    if parameters.get("help") is True:
        return builtin_tool_help("fuzzy-match")
    command = parameters.get("command")
    if not isinstance(command, (str, list, tuple)):
        raise PowdrrExecutionError(
            "Workflow fuzzy-match tool parameters must include a command array."
        )
    return {
        "tool": "fuzzy-match",
        "command": list(command) if not isinstance(command, str) else command,
        "result": json.loads(
            fuzzy_match_json(
                command, worktree_root=worktree_root, path_cache=path_cache
            )
        ),
    }


def _handle_workflow_action_gather_context(
    action: SkillChatAction,
    state: _WorkflowExecutionState,
    stdout: TextIO,
    stderr: TextIO,
    input_func: Callable[[], str],
    config: WorkflowChatConfig,
) -> bool:
    _ = input_func
    gathered_context = invoke_repository_read(
        "gather_context",
        {
            "types": list(action.types),
            "keywords": list(action.keywords) if action.keywords else None,
            "filters": action.filters,
            "feature_id": action.feature_id,
        },
        worktree_root=state.worktree_root,
        executor=lambda _arguments: gather_specification_context(
            state.worktree_root,
            types=list(action.types),
            keywords=list(action.keywords) if action.keywords else None,
            filters=action.filters,
            feature_id=action.feature_id,
        ),
        runtime=_ensure_execution_runtime(state),
    )
    gathered_context_text = render_gather_context_report(gathered_context)
    _verbose_print(
        stderr,
        config.verbose,
        (
            "Gathered context for "
            f"types={list(action.types)} keywords={list(action.keywords)}"
        ),
    )
    if action.decisions_and_context:
        _record_durable_fact(
            state,
            action.decisions_and_context,
            kind="decision",
            source=action.kind,
        )
    state.execution_context.append(f"Gathered context:\n{gathered_context_text}")
    state.execution_events.append(
        {
            "kind": action.kind,
            "types": list(action.types),
            "keywords": list(action.keywords),
            "filters": action.filters,
            "result": json.loads(gathered_context_text),
            "decisions_and_context": action.decisions_and_context,
            "step_index": state.step_index,
        }
    )
    _ = stdout
    return True
