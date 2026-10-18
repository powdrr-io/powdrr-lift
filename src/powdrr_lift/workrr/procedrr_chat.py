"""Procedrr-backed workflow chat orchestration and operation adapters."""

from __future__ import annotations

import hashlib
import json
import sys
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, TextIO, cast
from uuid import uuid4

from powdrr_lift.core import resolve_repo_root
from powdrr_lift.execution.runtime import ExecutionRuntime
from powdrr_lift.workrr.actions import (
    WorkflowAction,
    WorkflowEdit,
    WorkflowYamlOperation,
)
from powdrr_lift.workrr.chat_config import WorkflowChatConfig
from powdrr_lift.workrr.chat_context import (
    _load_workflow_context,
    _persist_workflow_context,
    _resolve_worktree_for_request,
)
from powdrr_lift.workrr.chat_io import _prompt_user
from powdrr_lift.workrr.execution_state import _WorkflowExecutionState
from powdrr_lift.workrr.procedrr import WorkrrProcedrrClient
from powdrr_lift.workrr.procedrr_operations import WorkrrOperationExecutor
from powdrr_lift.workrr.provider_runtime import WorkflowClientRegistry
from powdrr_lift.workrr.providers import (
    initial_model_for_provider,
    resolve_provider_roles,
)
from procedrr import ProcessCatalog, ProcessDefinition
from procedrr_evaluator.runtime import (
    ProcedrrExecutionService,
    ProcessExecutionEvent,
    ProcessTerminalStatus,
)


def run_procedrr_workflow_chat(
    config: WorkflowChatConfig,
    *,
    input_func: Callable[[], str] = input,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
    progress_callback: Callable[[Any, int, str, Any, int | None], None] | None = None,
    file_added_callback: Callable[[tuple[str, ...]], None] | None = None,
) -> int:
    """Select and run a catalogued Procedrr process for one chat request."""
    configured_repo_root = resolve_repo_root(config.repo_root)
    process_dir = config.processes_dir
    if not process_dir.is_absolute():
        process_dir = configured_repo_root / process_dir
    try:
        catalog = ProcessCatalog.load(process_dir)
    except ValueError as error:
        print(f"Could not load Procedrr processes: {error}", file=stderr)
        return 1

    workflow_context = _load_workflow_context(configured_repo_root)
    context_data = workflow_context.to_data() if workflow_context is not None else {}
    user_request = _prompt_user(
        "What do you want to do? ",
        input_func=input_func,
        stdout=stdout,
        status_stream=stderr,
    )

    provider_roles = resolve_provider_roles(
        config.provider,
        normal_provider=config.normal_provider,
        adversarial_provider=config.adversarial_provider,
    )
    client_registry = WorkflowClientRegistry(
        api_key=config.api_key,
        base_url=config.base_url,
        project_root=configured_repo_root,
        progress_stream=stderr,
    )
    process_clients: dict[str, WorkrrProcedrrClient] = {}

    def procedrr_client(role: str) -> WorkrrProcedrrClient:
        role_provider = provider_roles.provider_for(role)  # type: ignore[arg-type]
        role_model = initial_model_for_provider(role_provider, config.model)
        cache_key = f"{role_provider}:{role_model}"
        if cache_key not in process_clients:
            process_clients[cache_key] = WorkrrProcedrrClient(
                client_registry.client_for(
                    role_model,
                    role_provider,
                    credentials=client_registry.credentials_for(role_provider),
                ),
                process_directory=process_dir,
                max_retries=config.max_stalled_roundtrips,
                provider_retry_attempts=config.provider_retry_attempts,
                provider_retry_delay_seconds=config.provider_retry_delay_seconds,
            )
        return process_clients[cache_key]

    selection = _select_process(
        catalog,
        procedrr_client("normal"),
        user_request,
        context_data,
    )
    if selection is None:
        print("Could not select a Procedrr process.", file=stderr)
        return 1
    process = catalog.get(selection)
    print(f"Matched process: {selection}", file=stdout)

    bindings = _collect_process_inputs(
        process,
        user_request=user_request,
        workflow_context=context_data,
        input_func=input_func,
        stdout=stdout,
        stderr=stderr,
    )
    if bindings is None:
        return 1

    process_steps = process.document.get("steps", [])
    step_list = process_steps if isinstance(process_steps, list) else []
    progress_steps = tuple(
        SimpleNamespace(description=f"Procedrr process step {index + 1}")
        for index, _ in enumerate(step_list)
    )
    selected_adapter = SimpleNamespace(
        skill=SimpleNamespace(name=process.name, steps=progress_steps),
        path=process.path,
    )
    worktree_root = _resolve_worktree_for_request(
        configured_repo_root,
        request=user_request,
        selected_skill=cast(Any, selected_adapter),
        context=workflow_context,
        input_func=input_func,
        stdout=stdout,
        stderr=stderr,
        verbose=config.verbose,
    )
    execution_id = config.execution_id or f"procedrr-chat-{uuid4().hex}"
    if not all(character.isalnum() or character in "._-" for character in execution_id):
        print(
            "The configured execution id contains unsupported characters.", file=stderr
        )
        return 1
    runtime = ExecutionRuntime(
        execution_id,
        profile_id=process.name,
        workflow_directory=configured_repo_root / ".powdrr" / "procedrr-runs",
        repo_root=worktree_root,
    )
    runtime.capture_explicit_guidance(
        user_request,
        source_ref=f"{execution_id}:user-request",
    )
    state = _WorkflowExecutionState(
        selected_skill=cast(Any, selected_adapter),
        root_skill=cast(Any, selected_adapter),
        transcript=[{"role": "user", "content": user_request}],
        execution_events=[],
        audit_events=[],
        execution_context=[],
        step_index=0,
        worktree_root=worktree_root,
        error_log_root=configured_repo_root,
        handoff_records={},
        file_added_callback=file_added_callback,
        runtime=runtime,
    )
    operation_adapter = _ChatProcedrrOperations(
        state,
        config,
        input_func=input_func,
        stdout=stdout,
        stderr=stderr,
    )
    operation_names = (
        "internal",
        "edit",
        "yaml_edit",
        "file_management",
        "read_document",
        "list_files",
        "invoke_tool",
        "gather_context",
        "prompt_user",
        "respond",
        "workflow_chat_snapshot",
        "workflow_chat_changes",
    )

    def operation_handler(name: str) -> Callable[[Mapping[str, Any]], Any]:
        def dispatch(parameters: Mapping[str, Any]) -> Any:
            return operation_adapter.execute(name, parameters)

        return dispatch

    operation_executor = WorkrrOperationExecutor(
        {name: operation_handler(name) for name in operation_names}
    )
    judge_clients = {
        "planning": procedrr_client("normal"),
        "adversarial": procedrr_client("adversarial"),
        "jev": procedrr_client("normal"),
    }

    run_directory = configured_repo_root / ".powdrr" / "procedrr-runs"
    events_path = run_directory / f"{execution_id}.events.jsonl"
    manifest_path = run_directory / f"{execution_id}.json"
    run_directory.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "execution_id": execution_id,
        "process_id": process.identity.process_id,
        "process_fingerprint": process.identity.fingerprint,
        "bindings": bindings,
        "status": "running",
        "event_cursor": 0,
        "started_at": datetime.now(UTC).isoformat(),
    }
    _write_manifest(manifest_path, manifest)

    def persist_event(event: ProcessExecutionEvent) -> None:
        with events_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event.to_data(), ensure_ascii=False) + "\n")
        manifest["event_cursor"] = event.sequence + 1
        _write_manifest(manifest_path, manifest)
        if progress_callback is not None:
            progress_callback(
                cast(Any, selected_adapter),
                min(event.sequence, max(0, len(selected_adapter.skill.steps) - 1)),
                event.kind,
                None,
                None,
            )

    service = ProcedrrExecutionService(
        catalog,
        procedrr_client("normal"),
        operation_executor,
        judge_clients=judge_clients,
        event_sink=persist_event,
    )
    try:
        result = service.execute(process.name, bindings)
    except Exception as error:  # noqa: BLE001 - user-facing workflow boundary
        manifest["status"] = "failed"
        manifest["failure"] = f"{type(error).__name__}: {error}"
        manifest["finished_at"] = datetime.now(UTC).isoformat()
        _write_manifest(manifest_path, manifest)
        _persist_workflow_context(
            configured_repo_root,
            worktree_root,
            skill_name=process.name,
            request=user_request,
        )
        print(f"Procedrr process failed: {error}", file=stderr)
        return 1
    manifest["status"] = result.terminal_status.value
    manifest["result_bindings"] = dict(result.bindings)
    manifest["finished_at"] = datetime.now(UTC).isoformat()
    _write_manifest(manifest_path, manifest)
    _persist_workflow_context(
        configured_repo_root,
        worktree_root,
        skill_name=process.name,
        request=user_request,
    )
    if result.terminal_status is ProcessTerminalStatus.SUCCEEDED:
        print(f"Process completed: {process.name}", file=stdout)
        if result.bindings.get("response") is not None:
            print(str(result.bindings["response"]), file=stdout)
        return 0
    print(
        f"Process ended with status {result.terminal_status.value}: {process.name}",
        file=stderr,
    )
    return 1


def _write_manifest(path: Path, manifest: Mapping[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _select_process(
    catalog: ProcessCatalog,
    llm: WorkrrProcedrrClient,
    user_request: str,
    workflow_context: Mapping[str, Any],
) -> str | None:
    candidates = [
        process
        for process in catalog.list()
        if process.name not in {"select-process", "generate-fragment"}
    ]
    available = [
        {
            "name": process.name,
            "description": process.document.get("description", process.name),
        }
        for process in candidates
    ]
    selector = ProcessCatalog.load(catalog.get("select-process").path.parent)
    # The selector process has no operations; its only effect is a schema-checked
    # classification bound to the validated process catalog.
    service = ProcedrrExecutionService(
        selector,
        llm,
        WorkrrOperationExecutor({}),
    )
    try:
        result = service.execute(
            "select-process",
            {
                "user_request": user_request,
                "workflow_context": dict(workflow_context),
                "available_processes": available,
            },
        )
    except Exception:  # noqa: BLE001 - invalid model selection is a safe failure
        return None
    route = result.bindings.get("route")
    name = route.get("process_name") if isinstance(route, Mapping) else None
    valid_names = {process.name for process in candidates}
    return name if isinstance(name, str) and name in valid_names else None


def _collect_process_inputs(
    process: ProcessDefinition,
    *,
    user_request: str,
    workflow_context: Mapping[str, Any],
    input_func: Callable[[], str],
    stdout: TextIO,
    stderr: TextIO,
) -> dict[str, Any] | None:
    declarations = process.document.get("inputs", [])
    if not isinstance(declarations, list):
        return None
    bindings: dict[str, Any] = {
        "user_request": user_request,
        "workflow_context": dict(workflow_context),
        "language_labels": [],
    }
    for declaration in declarations:
        if not isinstance(declaration, Mapping):
            continue
        name = declaration.get("name")
        if not isinstance(name, str) or name in bindings:
            continue
        if declaration.get("required") is not True:
            continue
        prompt = declaration.get("question") or f"Provide {name.replace('_', ' ')}: "
        answer = _prompt_user(
            str(prompt),
            input_func=input_func,
            stdout=stdout,
            status_stream=stderr,
        )
        schema = declaration.get("schema")
        expected_type = (
            schema.get("type")
            if isinstance(schema, Mapping)
            else declaration.get("type")
        )
        if expected_type in {"object", "array"}:
            try:
                bindings[name] = json.loads(answer)
            except json.JSONDecodeError:
                print(f"Expected JSON for {name}.", file=stderr)
                return None
        elif expected_type == "integer":
            try:
                bindings[name] = int(answer)
            except ValueError:
                print(f"Expected an integer for {name}.", file=stderr)
                return None
        else:
            bindings[name] = answer
    try:
        return process.validate_inputs(bindings)
    except ValueError as error:
        print(f"Invalid process inputs: {error}", file=stderr)
        return None


class _ChatProcedrrOperations:
    """Translate process operations through existing authorized chat handlers."""

    def __init__(
        self,
        state: _WorkflowExecutionState,
        config: WorkflowChatConfig,
        *,
        input_func: Callable[[], str],
        stdout: TextIO,
        stderr: TextIO,
    ) -> None:
        from powdrr_lift.workrr.chat_actions import _workflow_action_handlers

        self.state = state
        self.config = config
        self.input_func = input_func
        self.stdout = stdout
        self.stderr = stderr
        self.handlers = _workflow_action_handlers()

    def execute(self, operation: str, parameters: Mapping[str, Any]) -> Any:
        if operation == "respond":
            text = parameters.get("text")
            if not isinstance(text, str):
                raise ValueError("respond requires a text string")
            print(text, file=self.stdout)
            return {"text": text}
        if operation == "workflow_chat_snapshot":
            return _worktree_snapshot(self.state.worktree_root)
        if operation == "workflow_chat_changes":
            snapshot = parameters.get("snapshot")
            if not isinstance(snapshot, Mapping):
                raise ValueError("workflow_chat_changes requires a snapshot mapping")
            current = _worktree_snapshot(self.state.worktree_root)
            before_files = snapshot.get("files", {})
            after_files = current.get("files", {})
            before = before_files if isinstance(before_files, Mapping) else {}
            after = after_files if isinstance(after_files, Mapping) else {}
            paths = sorted(
                str(path)
                for path in set(before) | set(after)
                if before.get(path) != after.get(path)
            )
            language_by_suffix = {
                ".py": "python",
                ".pyi": "python",
                ".md": "markdown",
                ".yaml": "yaml",
                ".yml": "yaml",
                ".toml": "toml",
                ".json": "json",
                ".js": "javascript",
                ".jsx": "javascript",
                ".ts": "typescript",
                ".tsx": "typescript",
                ".go": "go",
                ".rs": "rust",
            }
            labels = sorted(
                {
                    language_by_suffix[Path(path).suffix.lower()]
                    for path in paths
                    if Path(path).suffix.lower() in language_by_suffix
                }
            )
            return {
                "has_changes": bool(paths),
                "paths": paths,
                "language_labels": labels,
            }

        normalized = dict(parameters)
        if operation == "internal":
            normalized = {
                "kind": "invoke_tool",
                "tool": "internal",
                "parameters": dict(parameters),
            }
        elif operation == "invoke_tool":
            normalized["kind"] = "invoke_tool"
        elif operation == "file_management":
            normalized["kind"] = "file_management"
            normalized["file_operation"] = normalized.get("operation")
        else:
            normalized["kind"] = operation
        action = _operation_to_action(normalized)
        handler = self.handlers.get(action.kind)
        if handler is None:
            raise ValueError(f"Unsupported chat operation {action.kind!r}.")
        event_count = len(self.state.execution_events)
        handler(
            action,
            self.state,
            self.stdout,
            self.stderr,
            self.input_func,
            self.config,
        )
        if len(self.state.execution_events) > event_count:
            event = self.state.execution_events[-1]
            if "result" in event:
                return event["result"]
            if "answer" in event:
                return {"answer": event["answer"]}
            if "outputs" in event:
                return {"outputs": event["outputs"]}
        return {"ok": True}


def _operation_to_action(parameters: Mapping[str, Any]) -> WorkflowAction:
    operation = parameters.get("operation")
    if operation == "replace_lines":
        return WorkflowAction(
            kind="edit",
            file_path=_string(parameters.get("file_path"), "file_path"),
            edits=(
                WorkflowEdit(
                    "replace_lines",
                    _integer(parameters.get("start_line"), "start_line"),
                    _integer(
                        parameters.get("end_line", parameters.get("start_line")),
                        "end_line",
                    ),
                    _string(parameters.get("replacement"), "replacement"),
                ),
            ),
        )
    for tool_name in (
        "edit",
        "yaml_edit",
        "file_management",
        "read_document",
        "list_files",
        "gather_context",
        "invoke_tool",
    ):
        if tool_name in parameters:
            nested = parameters[tool_name]
            if isinstance(nested, Mapping):
                parameters = nested
            break
    if "file_operation" in parameters:
        return WorkflowAction(
            kind="file_management",
            file_operation=_string(parameters.get("file_operation"), "file_operation"),
            file_path=_string(parameters.get("file_path"), "file_path"),
            destination_path=(
                str(parameters["destination_path"])
                if parameters.get("destination_path") is not None
                else None
            ),
        )
    if "file_path" in parameters and "operations" in parameters:
        operations = parameters.get("operations")
        if not isinstance(operations, list):
            raise ValueError("yaml_edit operations must be a list")
        return WorkflowAction(
            kind="yaml_edit",
            file_path=_string(parameters.get("file_path"), "file_path"),
            yaml_operations=tuple(
                WorkflowYamlOperation(
                    operation=_string(item.get("operation"), "operation"),
                    section=_optional_string(item.get("section")),
                    item_id=_optional_string(item.get("item_id")),
                    item_index=item.get("item_index")
                    if type(item.get("item_index")) is int
                    else None,
                    path=tuple(str(part) for part in item.get("path", []))
                    if isinstance(item.get("path", []), list)
                    else (),
                    value=item.get("value"),
                )
                for item in operations
                if isinstance(item, Mapping)
            ),
        )
    if isinstance(parameters.get("edits"), list):
        edits = tuple(
            WorkflowEdit(
                "replace_lines",
                _integer(item.get("start"), "edits.start"),
                _integer(item.get("end"), "edits.end"),
                _string(item.get("new_text"), "edits.new_text"),
            )
            for item in parameters["edits"]
            if isinstance(item, Mapping)
        )
        return WorkflowAction(
            kind="edit",
            file_path=_string(parameters.get("file_path"), "file_path"),
            edits=edits,
        )
    kind = _optional_string(parameters.get("kind"))
    if kind is None:
        kind = _tool_kind(parameters)
    fields: dict[str, Any] = {"kind": kind}
    direct_string_fields = (
        "tool",
        "file_path",
        "destination_path",
        "file_operation",
        "directory",
        "pattern",
        "text",
        "feature_id",
        "capture_as",
    )
    for name in direct_string_fields:
        if isinstance(parameters.get(name), str):
            fields[name] = parameters[name]
    for name in ("start_line", "end_line"):
        if type(parameters.get(name)) is int:
            fields[name] = parameters[name]
    fields["recursive"] = parameters.get("recursive") is True
    fields["parameters"] = (
        dict(parameters.get("parameters", {}))
        if isinstance(parameters.get("parameters"), Mapping)
        else {}
    )
    fields["types"] = (
        tuple(str(item) for item in parameters.get("types", []))
        if isinstance(parameters.get("types"), list)
        else ()
    )
    fields["keywords"] = (
        tuple(str(item) for item in parameters.get("keywords", []))
        if isinstance(parameters.get("keywords"), list)
        else ()
    )
    fields["filters"] = (
        dict(parameters.get("filters", {}))
        if isinstance(parameters.get("filters"), Mapping)
        else {}
    )
    if kind == "read_document":
        fields.setdefault("start_line", 1)
        fields.setdefault("end_line", 2000)
    return WorkflowAction(**fields)


def _tool_kind(parameters: Mapping[str, Any]) -> str:
    if "command" in parameters or "tool" in parameters:
        return "invoke_tool"
    if "types" in parameters or "filters" in parameters:
        return "gather_context"
    if "start_line" in parameters or "end_line" in parameters:
        return "read_document"
    if "directory" in parameters:
        return "list_files"
    raise ValueError("Procedrr operation parameters do not identify an action")


def _string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a non-empty string")
    return value


def _optional_string(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _integer(value: Any, field: str) -> int:
    if type(value) is not int:
        raise ValueError(f"{field} must be an integer")
    return value


def _worktree_snapshot(worktree_root: Path) -> dict[str, Any]:
    from powdrr_lift.workrr.execution_state import _git_changed_paths

    files: dict[str, str] = {}
    for relative in sorted(_git_changed_paths(worktree_root)):
        path = worktree_root / relative
        if path.is_file():
            files[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
        else:
            files[relative] = "<missing>"
    return {"files": files}


__all__ = ["run_procedrr_workflow_chat"]
