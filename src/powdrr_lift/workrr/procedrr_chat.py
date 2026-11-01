"""Procedrr-backed workflow chat orchestration and operation adapters."""

from __future__ import annotations

import hashlib
import json
import sys
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO
from uuid import uuid4

from powdrr_service.decision_classifier import JevSemanticClassifierClient
from powdrr_service.providers.selection import (
    initial_model_for_provider,
    resolve_provider_roles,
)

from powdrr_lift.core import resolve_repo_root
from powdrr_lift.errors import PowdrrExecutionError
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
    _resolve_worktree_context,
    _resolve_worktree_for_request,
    _workflow_context_pr_is_closed,
    _worktree_reuse_decision,
)
from powdrr_lift.workrr.chat_events import (
    WorkflowChatEvent,
    WorkflowChatEventCallback,
    WorkflowChatEventEmitter,
    WorkflowChatEventKind,
)
from powdrr_lift.workrr.chat_io import _prompt_user
from powdrr_lift.workrr.execution_state import (
    ProcessExecutionReference,
    _WorkflowExecutionState,
)
from powdrr_lift.workrr.paths import is_dedicated_worktree
from powdrr_lift.workrr.procedrr import WorkrrProcedrrClient
from powdrr_lift.workrr.procedrr_operations import WorkrrOperationExecutor
from powdrr_lift.workrr.provider_runtime import WorkflowClientRegistry
from powdrr_lift.workrr.review_feedback import prepare_review_feedback_ledger
from procedrr import ProcessCatalog, ProcessDefinition
from procedrr_evaluator.evaluator import SuspendEvaluation
from procedrr_evaluator.runtime import (
    ProcedrrExecutionService,
    ProcessExecutionCheckpoint,
    ProcessExecutionEvent,
    ProcessTerminalStatus,
)

WORKFLOW_CHAT_WAITING_FOR_INPUT = 3


def run_procedrr_workflow_chat(
    config: WorkflowChatConfig,
    *,
    procedrr_client_factory: Callable[[str], WorkrrProcedrrClient] | None = None,
    input_func: Callable[[], str] = input,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
    progress_callback: Callable[[Any, int, str, Any, int | None], None] | None = None,
    file_added_callback: Callable[[tuple[str, ...]], None] | None = None,
    event_callback: WorkflowChatEventCallback | None = None,
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

    execution_id = config.execution_id or f"procedrr-chat-{uuid4().hex}"
    if not all(character.isalnum() or character in "._-" for character in execution_id):
        print(
            "The configured execution id contains unsupported characters.", file=stderr
        )
        return 1
    run_directory = configured_repo_root / ".powdrr" / "procedrr-runs"
    events_path = run_directory / f"{execution_id}.events.jsonl"
    chat_events_path = run_directory / f"{execution_id}.chat-events.jsonl"
    manifest_path = run_directory / f"{execution_id}.json"
    resuming = config.resume_value is not None
    resume_kind: str | None = None
    resume_worktree_choice: bool | None = None
    process_input_transcript: list[dict[str, str]] = []
    resume_manifest: dict[str, Any] | None = None
    resume_checkpoint: ProcessExecutionCheckpoint | None = None
    if resuming:
        if config.execution_id is None:
            print("Resuming a chat requires its execution id.", file=stderr)
            return 1
        try:
            loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict) or loaded.get("status") != "suspended":
                raise ValueError("execution is not waiting for user input")
            if loaded.get("conversation_id") != config.conversation_id:
                raise ValueError("execution belongs to a different conversation")
            resume_kind = loaded.get("suspension_kind")
            if resume_kind == "interpreter":
                checkpoint_data = loaded.get("checkpoint")
                if not isinstance(checkpoint_data, Mapping):
                    raise ValueError("execution manifest has no checkpoint")
                resume_checkpoint = ProcessExecutionCheckpoint.from_data(
                    checkpoint_data
                )
            elif resume_kind not in {"process_input", "worktree_choice"}:
                raise ValueError("execution has an unsupported suspension type")
            resume_manifest = loaded
        except (OSError, json.JSONDecodeError, ValueError) as error:
            print(f"Could not resume Procedrr chat: {error}", file=stderr)
            return 1
        request = resume_manifest.get("user_request")
        if not isinstance(request, str) or not request:
            print("Execution manifest has no original chat request.", file=stderr)
            return 1
        if config.initial_message is not None and config.initial_message != request:
            print("Resume message differs from the original chat request.", file=stderr)
            return 1
        user_request = request
        workflow_context = _load_workflow_context(
            configured_repo_root, conversation_id=config.conversation_id
        )
        saved_bindings = resume_manifest.get("bindings")
        context_data = (
            dict(saved_bindings.get("workflow_context", {}))
            if isinstance(saved_bindings, Mapping)
            and isinstance(saved_bindings.get("workflow_context", {}), Mapping)
            else {}
        )

    else:
        workflow_context = _load_workflow_context(
            configured_repo_root, conversation_id=config.conversation_id
        )
        context_data = (
            workflow_context.to_data() if workflow_context is not None else {}
        )
        if config.initial_message is None:
            user_request = _prompt_user(
                "What do you want to do? ",
                input_func=input_func,
                stdout=stdout,
                status_stream=stderr,
            )
        else:
            user_request = config.initial_message
            if not user_request.strip():
                print("The initial chat message must not be empty.", file=stderr)
                return 1

    saved_event_cursor = (
        resume_manifest.get("chat_event_cursor", 0)
        if isinstance(resume_manifest, Mapping)
        else 0
    )
    if type(saved_event_cursor) is not int or saved_event_cursor < 0:
        print("Execution manifest has an invalid chat event cursor.", file=stderr)
        return 1
    if chat_events_path.is_file():
        with chat_events_path.open(encoding="utf-8") as stream:
            saved_event_cursor = sum(1 for line in stream if line.strip())
    active_manifest: dict[str, Any] | None = None

    def persist_and_publish_chat_event(event: WorkflowChatEvent) -> None:
        run_directory.mkdir(parents=True, exist_ok=True)
        with chat_events_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event.to_dict(), ensure_ascii=False) + "\n")
        if manifest_path.is_file():
            try:
                current_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                current_manifest = None
            if isinstance(current_manifest, dict):
                current_manifest["chat_event_cursor"] = event.sequence + 1
                _write_manifest(manifest_path, current_manifest)
            if active_manifest is not None:
                active_manifest["chat_event_cursor"] = event.sequence + 1
        if event_callback is not None:
            event_callback(event)

    chat_events = WorkflowChatEventEmitter(
        execution_id,
        config.conversation_id,
        persist_and_publish_chat_event,
        first_sequence=saved_event_cursor,
    )

    provider_roles = resolve_provider_roles(
        config.provider,
        normal_provider=config.normal_provider,
        adversarial_provider=config.adversarial_provider,
    )
    client_registry = (
        None
        if procedrr_client_factory is not None
        else WorkflowClientRegistry(
            api_key=config.api_key,
            base_url=config.base_url,
            project_root=configured_repo_root,
            progress_stream=stderr,
        )
    )
    process_clients: dict[str, WorkrrProcedrrClient] = {}

    def record_provider_attempt(record: Mapping[str, Any]) -> None:
        if record.get("outcome") != "retrying":
            return
        retry_data = {
            key: record[key]
            for key in (
                "attempt",
                "trace_id",
                "provider_client",
                "model",
                "error_type",
                "error_status_code",
            )
            if record.get(key) is not None
        }
        chat_events.emit(WorkflowChatEventKind.RETRY, data=retry_data)

    def procedrr_client(role: str) -> WorkrrProcedrrClient:
        if procedrr_client_factory is not None:
            if role not in process_clients:
                process_clients[role] = procedrr_client_factory(role)
            return process_clients[role]
        assert client_registry is not None
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
                telemetry_sink=record_provider_attempt,
            )
        return process_clients[cache_key]

    resuming_interpreter = resuming and resume_kind == "interpreter"
    resuming_process_input = resuming and resume_kind in {
        "process_input",
        "worktree_choice",
    }
    if resuming_interpreter:
        assert resume_manifest is not None and resume_checkpoint is not None
        process_id = resume_manifest.get("process_id")
        if not isinstance(process_id, str):
            print("Execution manifest has no process id.", file=stderr)
            return 1
        try:
            process = catalog.get(process_id)
        except KeyError:
            print(
                f"The suspended process {process_id!r} is no longer available.",
                file=stderr,
            )
            return 1
        if process.identity != resume_checkpoint.identity:
            print("The suspended process definition has changed.", file=stderr)
            return 1
        raw_bindings = resume_manifest.get("bindings")
        if not isinstance(raw_bindings, Mapping):
            print("Execution manifest has no initial process bindings.", file=stderr)
            return 1
        bindings = dict(raw_bindings)
    elif resuming_process_input:
        assert resume_manifest is not None
        process_id = resume_manifest.get("process_id")
        if not isinstance(process_id, str):
            print("Execution manifest has no process id.", file=stderr)
            return 1
        try:
            process = catalog.get(process_id)
        except KeyError:
            print(
                f"The suspended process {process_id!r} is no longer available.",
                file=stderr,
            )
            return 1
        fingerprint = resume_manifest.get("process_fingerprint")
        if fingerprint != process.identity.fingerprint:
            print("The suspended process definition has changed.", file=stderr)
            return 1
        raw_bindings = resume_manifest.get("bindings")
        pending_name = resume_manifest.get("pending_input_name")
        if not isinstance(raw_bindings, Mapping) or not isinstance(pending_name, str):
            print("Execution manifest has incomplete process input state.", file=stderr)
            return 1
        bindings = dict(raw_bindings)
        saved_transcript = resume_manifest.get("process_input_transcript")
        if isinstance(saved_transcript, list):
            process_input_transcript = [
                dict(entry)
                for entry in saved_transcript
                if isinstance(entry, Mapping)
                and isinstance(entry.get("role"), str)
                and isinstance(entry.get("content"), str)
            ]
        process_input_transcript.extend(
            [
                {
                    "role": "assistant",
                    "content": str(resume_manifest.get("pending_question", "")),
                },
                {"role": "user", "content": config.resume_value or ""},
            ]
        )
        if resume_kind == "worktree_choice":
            answer = (config.resume_value or "").casefold()
            if answer not in {
                "y",
                "yes",
                "reuse",
                "same",
                "continue",
                "n",
                "no",
                "new",
            }:
                print("Please answer yes or no.", file=stderr)
                chat_events.emit(
                    WorkflowChatEventKind.QUESTION,
                    process_id=process.name,
                    text=str(resume_manifest.get("pending_question", "")),
                    data={
                        "input_name": "__worktree_reuse__",
                        "resumable": True,
                        "response_schema": {"type": "string"},
                    },
                )
                print(
                    str(resume_manifest.get("pending_question", "")),
                    file=stdout,
                    flush=True,
                )
                return WORKFLOW_CHAT_WAITING_FOR_INPUT
            resume_worktree_choice = answer in {"y", "yes", "reuse", "same", "continue"}
            try:
                bindings = process.validate_inputs(bindings)
            except ValueError as error:
                print(f"Invalid process inputs: {error}", file=stderr)
                return 1
        else:
            declaration = _process_input_declaration(process, pending_name)
            if declaration is None:
                print("The pending process input is no longer declared.", file=stderr)
                return 1
            try:
                bindings[pending_name] = _parse_process_input(
                    config.resume_value or "", declaration
                )
            except ValueError as error:
                print(str(error), file=stderr)
                question = str(resume_manifest.get("pending_question", ""))
                chat_events.emit(
                    WorkflowChatEventKind.QUESTION,
                    process_id=process.name,
                    text=question,
                    data={
                        "input_name": pending_name,
                        "resumable": True,
                        "response_schema": _process_input_response_schema(
                            process, pending_name
                        ),
                    },
                )
                print(question, file=stdout, flush=True)
                return WORKFLOW_CHAT_WAITING_FOR_INPUT
            collected_bindings, pending_input = _collect_process_inputs(
                process,
                user_request=user_request,
                workflow_context=context_data,
                input_func=input_func,
                stdout=stdout,
                stderr=stderr,
                initial_bindings=bindings,
                defer_user_input=config.defer_user_input,
            )
            if pending_input is not None:
                _write_preexecution_suspension(
                    manifest_path,
                    resume_manifest,
                    process,
                    collected_bindings,
                    pending_input,
                    suspension_kind="process_input",
                    process_input_transcript=process_input_transcript,
                )
                chat_events.emit(
                    WorkflowChatEventKind.QUESTION,
                    process_id=process.name,
                    text=pending_input[1],
                    data={
                        "input_name": pending_input[0],
                        "resumable": True,
                        "response_schema": _process_input_response_schema(
                            process, pending_input[0]
                        ),
                    },
                )
                print(pending_input[1], file=stdout, flush=True)
                return WORKFLOW_CHAT_WAITING_FOR_INPUT
            if collected_bindings is None:
                return 1
            bindings = collected_bindings
    else:
        selection = _select_process(
            catalog,
            procedrr_client("normal"),
            user_request,
            context_data,
        )
        if selection is None:
            print("Could not select a Procedrr process.", file=stderr)
            chat_events.emit(
                WorkflowChatEventKind.OUTCOME,
                data={"status": "failed", "reason": "process_selection_failed"},
            )
            return 1
        process = catalog.get(selection)
        print(f"Matched process: {selection}", file=stdout)
        chat_events.emit(
            WorkflowChatEventKind.STAGE,
            process_id=process.name,
            data={"stage": "process_selected"},
        )

        collected_bindings, pending_input = _collect_process_inputs(
            process,
            user_request=user_request,
            workflow_context=context_data,
            input_func=input_func,
            stdout=stdout,
            stderr=stderr,
            defer_user_input=config.defer_user_input,
        )
        if pending_input is not None:
            _write_preexecution_suspension(
                manifest_path,
                {
                    "schema_version": 1,
                    "execution_id": execution_id,
                    "user_request": user_request,
                    "conversation_id": config.conversation_id,
                    "workflow_context": context_data,
                    "process_input_transcript": process_input_transcript,
                    "started_at": datetime.now(UTC).isoformat(),
                },
                process,
                collected_bindings,
                pending_input,
                suspension_kind="process_input",
                process_input_transcript=process_input_transcript,
            )
            chat_events.emit(
                WorkflowChatEventKind.QUESTION,
                process_id=process.name,
                text=pending_input[1],
                data={
                    "input_name": pending_input[0],
                    "resumable": True,
                    "response_schema": _process_input_response_schema(
                        process, pending_input[0]
                    ),
                },
            )
            print(pending_input[1], file=stdout, flush=True)
            return WORKFLOW_CHAT_WAITING_FOR_INPUT
        if collected_bindings is None:
            return 1
        bindings = collected_bindings

    process_steps = process.document.get("steps", [])
    step_list = process_steps if isinstance(process_steps, list) else []
    selected_process = ProcessExecutionReference(
        name=process.name,
        path=process.path,
        steps=tuple(dict(step) for step in step_list if isinstance(step, Mapping)),
    )
    if resuming_interpreter:
        assert resume_manifest is not None
        worktree_value = resume_manifest.get("worktree_root")
        if not isinstance(worktree_value, str):
            print("Execution manifest has no worktree root.", file=stderr)
            return 1
        worktree_root = Path(worktree_value).expanduser().resolve()
        if not worktree_root.is_dir():
            print("The suspended chat worktree is no longer available.", file=stderr)
            return 1
    else:
        if resume_worktree_choice is not None and workflow_context is not None:
            worktree_root = (
                workflow_context.worktree_root
                if resume_worktree_choice
                else _resolve_worktree_context(
                    configured_repo_root,
                    stderr=stderr,
                    verbose=config.verbose,
                )
            )
        else:
            reuse_decision = _worktree_reuse_decision(
                user_request, selected_process, workflow_context
            )
            if (
                config.defer_user_input
                and not is_dedicated_worktree(configured_repo_root)
                and workflow_context is not None
                and reuse_decision is None
                and not _workflow_context_pr_is_closed(workflow_context)
            ):
                question = (
                    "A previous workflow worktree is available. "
                    "Do you want to reuse it? "
                )
                _write_preexecution_suspension(
                    manifest_path,
                    {
                        "schema_version": 1,
                        "execution_id": execution_id,
                        "user_request": user_request,
                        "conversation_id": config.conversation_id,
                        "workflow_context": context_data,
                        "process_input_transcript": process_input_transcript,
                    },
                    process,
                    bindings,
                    ("__worktree_reuse__", question),
                    suspension_kind="worktree_choice",
                    process_input_transcript=process_input_transcript,
                )
                chat_events.emit(
                    WorkflowChatEventKind.QUESTION,
                    process_id=process.name,
                    text=question,
                    data={
                        "input_name": "__worktree_reuse__",
                        "resumable": True,
                        "response_schema": {"type": "string"},
                    },
                )
                print(question, file=stdout, flush=True)
                return WORKFLOW_CHAT_WAITING_FOR_INPUT
            worktree_root = _resolve_worktree_for_request(
                configured_repo_root,
                request=user_request,
                selected_process=selected_process,
                context=workflow_context,
                input_func=input_func,
                stdout=stdout,
                stderr=stderr,
                verbose=config.verbose,
            )
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
    if process.name == "address-review-comments":
        existing_context = bindings.get("workflow_context")
        review_context = (
            dict(existing_context)
            if isinstance(existing_context, Mapping)
            else dict(context_data)
        )
        has_resumed_ledger = resuming_interpreter and isinstance(
            review_context.get("review_feedback_ledger"), Mapping
        )
        if not has_resumed_ledger:
            try:
                review_context["review_feedback_ledger"] = (
                    prepare_review_feedback_ledger(
                        worktree_root=str(worktree_root),
                        runtime=runtime,
                        workflow_context=review_context,
                    )
                )
            except PowdrrExecutionError as error:
                print(
                    f"Could not prepare review feedback: {error}",
                    file=stderr,
                )
                chat_events.emit(
                    WorkflowChatEventKind.OUTCOME,
                    process_id=process.name,
                    data={"status": "failed", "reason": "review_feedback_failed"},
                )
                return 1
        context_data = review_context
        bindings["workflow_context"] = review_context

    def report_added_files(paths: tuple[str, ...]) -> None:
        chat_events.emit(
            WorkflowChatEventKind.ARTIFACT,
            process_id=process.name,
            data={"paths": list(paths)},
        )
        if file_added_callback is not None:
            file_added_callback(paths)

    state = _WorkflowExecutionState(
        selected_process=selected_process,
        root_process=selected_process,
        transcript=[
            {"role": "user", "content": user_request},
            *process_input_transcript,
        ],
        execution_events=[],
        audit_events=[],
        execution_context=[],
        step_index=0,
        worktree_root=worktree_root,
        error_log_root=configured_repo_root,
        handoff_records={},
        file_added_callback=report_added_files,
        runtime=runtime,
    )
    if resuming_interpreter and resume_manifest is not None:
        saved_state = resume_manifest.get("workflow_state")
        if isinstance(saved_state, Mapping):
            _restore_chat_state(state, saved_state)
    operation_adapter = _ChatProcedrrOperations(
        state,
        config,
        input_func=input_func,
        stdout=stdout,
        stderr=stderr,
        event_emitter=chat_events,
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
    planning_client = procedrr_client("normal")
    jev_classifier = JevSemanticClassifierClient(planning_client)
    jev_client = WorkrrProcedrrClient(
        jev_classifier,
        process_directory=process_dir,
        max_retries=config.max_stalled_roundtrips,
        provider_retry_attempts=config.provider_retry_attempts,
        provider_retry_delay_seconds=config.provider_retry_delay_seconds,
        telemetry_sink=record_provider_attempt,
    )
    judge_clients = {
        "planning": planning_client,
        "adversarial": procedrr_client("adversarial"),
        "jev": jev_client,
    }

    run_directory.mkdir(parents=True, exist_ok=True)
    if resuming_interpreter and resume_manifest is not None:
        manifest = resume_manifest
        manifest["status"] = "running"
        manifest["resumed_at"] = datetime.now(UTC).isoformat()
        manifest.pop("suspension_kind", None)
    else:
        manifest = {
            "schema_version": 1,
            "execution_id": execution_id,
            "process_id": process.identity.process_id,
            "process_fingerprint": process.identity.fingerprint,
            "bindings": bindings,
            "user_request": user_request,
            "conversation_id": config.conversation_id,
            "worktree_root": str(worktree_root),
            "workflow_state": _chat_state_to_data(state),
            "status": "running",
            "event_cursor": 0,
            "chat_event_cursor": chat_events.next_sequence,
            "started_at": datetime.now(UTC).isoformat(),
        }
    active_manifest = manifest
    _write_manifest(manifest_path, manifest)

    def persist_event(event: ProcessExecutionEvent) -> None:
        with events_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event.to_data(), ensure_ascii=False) + "\n")
        manifest["event_cursor"] = event.sequence + 1
        manifest["chat_event_cursor"] = chat_events.next_sequence
        _write_manifest(manifest_path, manifest)
        chat_events.emit(
            WorkflowChatEventKind.STAGE,
            process_id=selected_process.name,
            path=event.path,
            data={
                "event_kind": event.kind,
                "step_index": min(
                    event.sequence, max(0, len(selected_process.steps) - 1)
                ),
            },
        )
        if progress_callback is not None:
            progress_callback(
                selected_process,
                min(event.sequence, max(0, len(selected_process.steps) - 1)),
                event.kind,
                None,
                None,
            )

    service = ProcedrrExecutionService(
        catalog,
        planning_client,
        operation_executor,
        judge_clients=judge_clients,
        event_sink=persist_event,
    )
    try:
        result = service.execute(
            process.name,
            bindings,
            checkpoint=resume_checkpoint if resuming_interpreter else None,
            resume_value=config.resume_value if resuming_interpreter else None,
        )
    except Exception as error:  # noqa: BLE001 - user-facing workflow boundary
        manifest["status"] = "failed"
        manifest["failure"] = f"{type(error).__name__}: {error}"
        manifest["finished_at"] = datetime.now(UTC).isoformat()
        _write_manifest(manifest_path, manifest)
        _persist_workflow_context(
            configured_repo_root,
            worktree_root,
            process_id=process.name,
            request=user_request,
            conversation_id=config.conversation_id,
        )
        print(f"Procedrr process failed: {error}", file=stderr)
        chat_events.emit(
            WorkflowChatEventKind.OUTCOME,
            process_id=process.name,
            data={"status": "failed", "error_type": type(error).__name__},
        )
        return 1
    if result.terminal_status is ProcessTerminalStatus.SUSPENDED:
        if result.checkpoint is None or operation_adapter.pending_question is None:
            manifest["status"] = "failed"
            manifest["failure"] = (
                "execution suspended without a durable question checkpoint"
            )
            manifest["finished_at"] = datetime.now(UTC).isoformat()
            _write_manifest(manifest_path, manifest)
            print(manifest["failure"], file=stderr)
            chat_events.emit(
                WorkflowChatEventKind.OUTCOME,
                process_id=process.name,
                data={"status": "failed", "reason": "missing_suspension_checkpoint"},
            )
            return 1
        manifest["status"] = "suspended"
        manifest["suspension_kind"] = "interpreter"
        manifest["checkpoint"] = result.checkpoint.to_data()
        manifest["pending_question"] = operation_adapter.pending_question
        manifest["workflow_state"] = _chat_state_to_data(state)
        manifest["event_cursor"] = result.event_cursor
        manifest["suspended_at"] = datetime.now(UTC).isoformat()
        _write_manifest(manifest_path, manifest)
        chat_events.emit(
            WorkflowChatEventKind.OUTCOME,
            process_id=process.name,
            data={"status": "waiting_for_input"},
        )
        print(operation_adapter.pending_question, file=stdout, flush=True)
        return WORKFLOW_CHAT_WAITING_FOR_INPUT
    manifest.pop("checkpoint", None)
    manifest.pop("pending_question", None)
    manifest.pop("suspension_kind", None)
    manifest["workflow_state"] = _chat_state_to_data(state)
    manifest["status"] = result.terminal_status.value
    manifest["result_bindings"] = dict(result.bindings)
    manifest["finished_at"] = datetime.now(UTC).isoformat()
    _write_manifest(manifest_path, manifest)
    _persist_workflow_context(
        configured_repo_root,
        worktree_root,
        process_id=process.name,
        request=user_request,
        conversation_id=config.conversation_id,
    )
    if result.terminal_status is ProcessTerminalStatus.SUCCEEDED:
        response = result.bindings.get("response")
        if response is not None and not operation_adapter.assistant_messages:
            chat_events.emit(
                WorkflowChatEventKind.ASSISTANT_MESSAGE,
                process_id=process.name,
                text=str(response),
            )
        chat_events.emit(
            WorkflowChatEventKind.OUTCOME,
            process_id=process.name,
            data={"status": ProcessTerminalStatus.SUCCEEDED.value},
        )
        print(f"Process completed: {process.name}", file=stdout)
        if response is not None:
            print(str(response), file=stdout)
        return 0
    chat_events.emit(
        WorkflowChatEventKind.OUTCOME,
        process_id=process.name,
        data={"status": result.terminal_status.value},
    )
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


def _chat_state_to_data(state: _WorkflowExecutionState) -> dict[str, Any]:
    """Serialize the workflow state needed to continue a suspended chat."""
    return {
        "transcript": state.transcript,
        "execution_events": state.execution_events,
        "audit_events": state.audit_events,
        "execution_context": state.execution_context,
        "step_index": state.step_index,
        "worktree_root": str(state.worktree_root),
        "error_log_root": str(state.error_log_root),
        "handoff_records": state.handoff_records,
        "durable_facts": state.durable_facts,
        "current_file_path": (
            str(state.current_file_path)
            if state.current_file_path is not None
            else None
        ),
        "stalled_step_context": state.stalled_step_context,
    }


def _restore_chat_state(
    state: _WorkflowExecutionState, data: Mapping[str, Any]
) -> None:
    """Restore persisted workflow facts while discarding process-local caches."""
    for key in ("transcript", "execution_events", "audit_events", "execution_context"):
        value = data.get(key)
        if isinstance(value, list):
            setattr(state, key, value)
    step_index = data.get("step_index")
    if isinstance(step_index, int) and not isinstance(step_index, bool):
        state.step_index = step_index
    for key in ("worktree_root", "error_log_root"):
        value = data.get(key)
        if isinstance(value, str):
            setattr(state, key, Path(value).expanduser().resolve())
    for key in ("handoff_records", "durable_facts"):
        value = data.get(key)
        if isinstance(value, dict):
            setattr(state, key, value)
    current_file_path = data.get("current_file_path")
    if isinstance(current_file_path, str):
        state.current_file_path = Path(current_file_path).expanduser().resolve()
    elif current_file_path is None:
        state.current_file_path = None
    stalled_context = data.get("stalled_step_context")
    if isinstance(stalled_context, list):
        state.stalled_step_context = stalled_context
    state.current_file_context_cache.clear()
    state.fuzzy_match_cache.clear()


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
    initial_bindings: Mapping[str, Any] | None = None,
    defer_user_input: bool = False,
) -> tuple[dict[str, Any] | None, tuple[str, str] | None]:
    declarations = process.document.get("inputs", [])
    if not isinstance(declarations, list):
        return None, None
    bindings: dict[str, Any] = {
        "user_request": user_request,
        "workflow_context": dict(workflow_context),
        "language_labels": [],
    }
    if initial_bindings is not None:
        bindings.update(initial_bindings)
    for declaration in declarations:
        if not isinstance(declaration, Mapping):
            continue
        name = declaration.get("name")
        if not isinstance(name, str) or name in bindings:
            continue
        if declaration.get("required") is not True:
            continue
        prompt = declaration.get("question") or f"Provide {name.replace('_', ' ')}: "
        if defer_user_input:
            return bindings, (name, str(prompt))
        answer = _prompt_user(
            str(prompt),
            input_func=input_func,
            stdout=stdout,
            status_stream=stderr,
        )
        try:
            bindings[name] = _parse_process_input(answer, declaration)
        except ValueError as error:
            print(str(error), file=stderr)
            return None, None
    try:
        return process.validate_inputs(bindings), None
    except ValueError as error:
        print(f"Invalid process inputs: {error}", file=stderr)
        return None, None


def _process_input_declaration(
    process: ProcessDefinition, name: str
) -> Mapping[str, Any] | None:
    declarations = process.document.get("inputs", [])
    if not isinstance(declarations, list):
        return None
    return next(
        (
            declaration
            for declaration in declarations
            if isinstance(declaration, Mapping) and declaration.get("name") == name
        ),
        None,
    )


def _process_input_response_schema(
    process: ProcessDefinition, name: str
) -> dict[str, Any]:
    declaration = _process_input_declaration(process, name)
    if declaration is None:
        return {"type": "string"}
    schema = declaration.get("schema")
    if isinstance(schema, Mapping):
        return dict(schema)
    input_type = declaration.get("type")
    if input_type in {"string", "integer", "number", "boolean", "object", "array"}:
        return {"type": input_type}
    return {"type": "string"}


def _parse_process_input(answer: str, declaration: Mapping[str, Any]) -> Any:
    name = declaration.get("name")
    label = name if isinstance(name, str) else "process input"
    schema = declaration.get("schema")
    expected_type = (
        schema.get("type") if isinstance(schema, Mapping) else declaration.get("type")
    )
    if expected_type in {"object", "array"}:
        try:
            return json.loads(answer)
        except json.JSONDecodeError as error:
            raise ValueError(f"Expected JSON for {label}.") from error
    if expected_type == "integer":
        try:
            return int(answer)
        except ValueError as error:
            raise ValueError(f"Expected an integer for {label}.") from error
    return answer


def _write_preexecution_suspension(
    manifest_path: Path,
    manifest: Mapping[str, Any],
    process: ProcessDefinition,
    bindings: Mapping[str, Any] | None,
    pending_input: tuple[str, str],
    *,
    suspension_kind: str,
    process_input_transcript: list[dict[str, str]],
) -> None:
    input_name, question = pending_input
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    suspended = dict(manifest)
    suspended.update(
        {
            "process_id": process.identity.process_id,
            "process_fingerprint": process.identity.fingerprint,
            "bindings": dict(bindings or {}),
            "pending_input_name": input_name,
            "pending_question": question,
            "process_input_transcript": process_input_transcript,
            "suspension_kind": suspension_kind,
            "status": "suspended",
            "suspended_at": datetime.now(UTC).isoformat(),
        }
    )
    _write_manifest(manifest_path, suspended)


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
        event_emitter: WorkflowChatEventEmitter | None = None,
    ) -> None:
        from powdrr_lift.workrr.chat_actions import _workflow_action_handlers

        self.state = state
        self.config = config
        self.input_func = input_func
        self.stdout = stdout
        self.stderr = stderr
        self.event_emitter = event_emitter
        self.handlers = _workflow_action_handlers()
        self.pending_question: str | None = None
        self.assistant_messages: list[str] = []

    def execute(self, operation: str, parameters: Mapping[str, Any]) -> Any:
        if operation == "prompt_user":
            resume = parameters.get("__procedrr_resume__")
            if isinstance(resume, Mapping):
                resume_data = resume.get("data")
                saved_state = (
                    resume_data.get("workflow_state")
                    if isinstance(resume_data, Mapping)
                    else None
                )
                if isinstance(saved_state, Mapping):
                    _restore_chat_state(self.state, saved_state)
                answer = resume.get("value")
                if not isinstance(answer, str):
                    raise ValueError("resumed prompt_user requires a string answer")
                prompt = _string(parameters.get("text"), "text")
                self.state.transcript.extend(
                    [
                        {"role": "assistant", "content": prompt},
                        {"role": "user", "content": answer},
                    ]
                )
                self.state.execution_events.append(
                    {
                        "kind": "prompt_user",
                        "text": prompt,
                        "answer": answer,
                        "step_index": self.state.step_index,
                    }
                )
                return {"answer": answer}
            if self.config.defer_user_input:
                prompt = _string(parameters.get("text"), "text")
                self.pending_question = prompt
                if self.event_emitter is not None:
                    self.event_emitter.emit(
                        WorkflowChatEventKind.QUESTION,
                        process_id=self.state.selected_process.name,
                        text=prompt,
                        data={"resumable": True, "response_schema": {"type": "string"}},
                    )
                raise SuspendEvaluation(
                    {"answer": ""},
                    "waiting for a user response",
                    resume_data={"workflow_state": _chat_state_to_data(self.state)},
                    resume_with_executor=True,
                )
        if operation == "respond":
            text = parameters.get("text")
            if not isinstance(text, str):
                raise ValueError("respond requires a text string")
            self.assistant_messages.append(text)
            if self.event_emitter is not None:
                self.event_emitter.emit(
                    WorkflowChatEventKind.ASSISTANT_MESSAGE,
                    process_id=self.state.selected_process.name,
                    text=text,
                )
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
