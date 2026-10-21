"""Interpret procedrr documents at the agent boundary.

This module deliberately does not implement repository tools.  The caller
injects an operation executor, normally an adapter around powdrr-lift's
existing workflow action handlers.  The evaluator owns only binding,
prompt construction, schema validation, and bounded control traversal.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jsonschema import ValidationError as JsonSchemaError
from jsonschema import validate as validate_json

if TYPE_CHECKING:
    from powdrr_lift.workrr.protocol import WorkflowLLMClient

_REFERENCE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_.-]*)\}")


class EvaluationError(RuntimeError):
    """A declaration, model response, or injected operation failed."""


class ValidationGateError(EvaluationError):
    """A validation gate failed with its bound evidence preserved."""

    def __init__(self, evidence: Any) -> None:
        self.evidence = evidence
        super().__init__("validation gate failed")


class SuspendEvaluation(Exception):
    """Request a durable pause after recording an operation result."""

    def __init__(
        self,
        result: Any,
        reason: str,
        *,
        resume_data: Mapping[str, Any] | None = None,
        resume_with_executor: bool = False,
    ) -> None:
        self.result = result
        self.reason = reason
        self.resume_data = dict(resume_data or {})
        self.resume_with_executor = resume_with_executor
        super().__init__(reason)


class _Suspended(Exception):
    def __init__(self, path: str, reason: str) -> None:
        self.path = path
        self.reason = reason
        super().__init__(reason)


class _TerminalReached(Exception):
    """Stop the current process after a declared terminal control node."""

    def __init__(self, status: str) -> None:
        self.status = status
        super().__init__(status)


@dataclass(frozen=True, slots=True)
class EvaluationEvent:
    kind: str
    path: str
    data: Mapping[str, Any] = field(default_factory=dict)


OperationExecutor = Callable[[str, Mapping[str, Any]], Any]
EventSink = Callable[[EvaluationEvent], None]


class _EventLog(list[EvaluationEvent]):
    """Event list that can stream evaluator events to a run artifact."""

    def __init__(self, sink: EventSink | None = None) -> None:
        super().__init__()
        self.sink = sink

    def append(self, event: EvaluationEvent) -> None:
        super().append(event)
        if self.sink is not None:
            self.sink(event)


@dataclass(frozen=True, slots=True)
class EvaluationResult:
    bindings: Mapping[str, Any]
    events: tuple[EvaluationEvent, ...]
    llm_activations: int
    tool_calls: int
    binding_schemas: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    suspended: bool = False
    checkpoint: EvaluationCheckpoint | None = None


@dataclass(frozen=True, slots=True)
class EvaluationCheckpoint:
    """Replayable evaluator state at an explicit operation suspension point."""

    initial_bindings: Mapping[str, Any]
    events: tuple[EvaluationEvent, ...]
    suspended_at: str
    event_cursor: int

    def to_data(self) -> dict[str, Any]:
        return {
            "initial_bindings": dict(self.initial_bindings),
            "events": [
                {"kind": event.kind, "path": event.path, "data": dict(event.data)}
                for event in self.events
            ],
            "suspended_at": self.suspended_at,
            "event_cursor": self.event_cursor,
        }

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> EvaluationCheckpoint:
        initial_bindings = data.get("initial_bindings")
        events_data = data.get("events")
        suspended_at = data.get("suspended_at")
        event_cursor = data.get("event_cursor")
        if (
            not isinstance(initial_bindings, Mapping)
            or not isinstance(events_data, list)
            or not isinstance(suspended_at, str)
            or not suspended_at
            or type(event_cursor) is not int
            or event_cursor != len(events_data)
        ):
            raise EvaluationError("invalid Procedrr evaluation checkpoint")
        events: list[EvaluationEvent] = []
        for item in events_data:
            if (
                not isinstance(item, Mapping)
                or not isinstance(item.get("kind"), str)
                or not isinstance(item.get("path"), str)
                or not isinstance(item.get("data", {}), Mapping)
            ):
                raise EvaluationError("invalid event in Procedrr checkpoint")
            events.append(
                EvaluationEvent(item["kind"], item["path"], dict(item.get("data", {})))
            )
        if not events or not any(
            event.kind == "suspend" and event.path == suspended_at for event in events
        ):
            raise EvaluationError("checkpoint has no matching suspension event")
        return cls(dict(initial_bindings), tuple(events), suspended_at, event_cursor)


class Evaluator:
    """Run a parsed, procedrr-validated document using agent-owned adapters."""

    def __init__(
        self,
        llm: WorkflowLLMClient,
        operation_executor: OperationExecutor,
        *,
        process_directory: Path | None = None,
        judge_clients: Mapping[str, WorkflowLLMClient] | None = None,
        command_catalog: Any | None = None,
        event_sink: EventSink | None = None,
    ) -> None:
        self.llm = llm
        self.operation_executor = operation_executor
        self.judge_clients = dict(judge_clients or {})
        self.process_directory = process_directory or Path("docs/procedrr/processes")
        self.command_catalog = command_catalog
        self.event_sink = event_sink
        self._schemas_by_state: dict[int, dict[str, Mapping[str, Any]]] = {}
        self._replay_events: dict[tuple[str, str], EvaluationEvent] = {}
        self._suspended_at: str | None = None
        self._suspension_reason: str | None = None
        self._resume_value: Any = None
        self._resume_path: str | None = None

    @classmethod
    def with_workrr(
        cls,
        client: WorkflowLLMClient,
        operation_executor: OperationExecutor,
        *,
        process_directory: Path,
        max_retries: int = 3,
    ) -> Evaluator:
        """Construct an evaluator using Workrr's structured repair boundary."""
        from powdrr_lift.workrr.procedrr import WorkrrProcedrrClient

        return cls(
            WorkrrProcedrrClient(
                client,
                process_directory=process_directory,
                max_retries=max_retries,
            ),
            operation_executor,
            process_directory=Path("docs/procedrr/processes"),
        )

    def evaluate(
        self,
        document: Mapping[str, Any],
        bindings: Mapping[str, Any] | None = None,
        *,
        checkpoint: EvaluationCheckpoint | None = None,
        resume_value: Any = None,
    ) -> EvaluationResult:
        if checkpoint is not None and resume_value is None:
            raise EvaluationError("resuming a checkpoint requires a resume value")
        initial_bindings = (
            dict(checkpoint.initial_bindings)
            if checkpoint is not None
            else dict(bindings or {})
        )
        state = dict(initial_bindings)
        self._schemas_by_state = {id(state): {}}
        self._document_recoveries = document.get("recoveries", {})
        events: _EventLog = _EventLog(self.event_sink)
        if checkpoint is not None:
            list.extend(events, checkpoint.events)
            self._replay_events = {
                (event.kind, event.path): event for event in checkpoint.events
            }
            self._resume_value = resume_value
            self._resume_path = checkpoint.suspended_at
        else:
            self._replay_events = {}
            self._resume_value = None
            self._resume_path = None
        self._suspended_at = None
        self._suspension_reason = None
        usage = {
            "llm": sum(event.kind == "judge" for event in events),
            "tools": sum(event.kind == "operation" for event in events),
        }
        limits = document.get("limits", {})
        try:
            self._steps(document["steps"], state, events, usage, limits, "steps")
        except _TerminalReached:
            pass
        except _Suspended as suspended:
            self._suspended_at = suspended.path
            self._suspension_reason = suspended.reason
        except (KeyError, TypeError, ValueError, JsonSchemaError) as exc:
            raise EvaluationError(str(exc)) from exc
        evaluation_checkpoint = (
            EvaluationCheckpoint(
                initial_bindings=initial_bindings,
                events=tuple(events),
                suspended_at=self._suspended_at,
                event_cursor=len(events),
            )
            if self._suspended_at is not None
            else None
        )
        return EvaluationResult(
            state,
            tuple(events),
            usage["llm"],
            usage["tools"],
            dict(self._schemas_by_state[id(state)]),
            self._suspended_at is not None,
            evaluation_checkpoint,
        )

    def _schemas_for_state(self, state: dict[str, Any]) -> dict[str, Mapping[str, Any]]:
        return self._schemas_by_state.setdefault(id(state), {})

    def _steps(
        self,
        steps: Sequence[Any],
        state: dict[str, Any],
        events: list[EvaluationEvent],
        usage: dict[str, int],
        limits: Mapping[str, Any],
        path: str,
    ) -> None:
        for index, step in enumerate(steps):
            step_path = f"{path}[{index}]"
            if not isinstance(step, Mapping):
                raise EvaluationError(f"{step_path} must be a mapping")
            if "operation" in step:
                self._operation(
                    step["operation"], state, events, usage, limits, step_path
                )
            elif "judge" in step:
                self._judge(step["judge"], state, events, usage, limits, step_path)
            elif "for_each" in step or "worklist" in step:
                key = "for_each" if "for_each" in step else "worklist"
                self._loop(key, step[key], state, events, usage, limits, step_path)
            elif "attempt" in step:
                self._attempt(step["attempt"], state, events, usage, limits, step_path)
            elif "specialize" in step:
                self._specialize(
                    step["specialize"], state, events, usage, limits, step_path
                )
            elif "call" in step:
                declaration = step["call"]
                if isinstance(declaration, Mapping) and "process" in declaration:
                    self._call_process(
                        declaration, state, events, usage, limits, step_path
                    )
                else:
                    self._call_fragment(
                        declaration, state, events, usage, limits, step_path
                    )
            elif "repeat" in step:
                self._repeat(step["repeat"], state, events, usage, limits, step_path)
            elif "branch" in step:
                self._branch(step["branch"], state, events, usage, limits, step_path)
            elif "terminal" in step:
                status = str(step["terminal"])
                events.append(
                    EvaluationEvent("terminal", step_path, {"status": status})
                )
                raise _TerminalReached(status)
            elif "gate" in step:
                self._gate(step["gate"], state, events, step_path)
            else:
                raise EvaluationError(f"{step_path} has no supported control")

    def _specialize(
        self,
        declaration: Mapping[str, Any],
        state: dict[str, Any],
        events: list[EvaluationEvent],
        usage: dict[str, int],
        limits: Mapping[str, Any],
        path: str,
    ) -> None:
        if not isinstance(declaration, Mapping):
            raise EvaluationError(f"{path}.specialize is malformed")
        context = declaration.get("context")
        bind = declaration.get("bind")
        if not isinstance(context, list) or not isinstance(bind, str):
            raise EvaluationError(f"{path}.specialize is malformed")
        process_name = declaration.get("process", "generate-fragment")
        if not isinstance(process_name, str) or not process_name:
            raise EvaluationError(f"{path}.specialize.process must be a name")
        process = self._load_process(process_name)
        process_fingerprint = _fingerprint_document(process)
        cached = self._replay_events.get(("specialize", path))
        if cached is not None:
            if cached.data.get("process_fingerprint") != process_fingerprint:
                raise EvaluationError(
                    f"{path}.specialize subprocess revision changed since checkpoint"
                )
            fragment = cached.data.get("fragment")
            if not isinstance(fragment, Mapping):
                raise EvaluationError(
                    f"{path}.specialize checkpoint has no generated fragment"
                )
            state[bind] = dict(fragment)
            return
        value_limit = _positive_limit(limits, "context_value_chars", 6000)
        context_data = {
            name: _compact_value(_resolve_binding(state, name), max_chars=value_limit)
            for name in context
        }
        allowed_tools = declaration.get("allowed_tools")
        maximum = declaration.get("max_steps", 64)
        if not isinstance(maximum, int) or maximum <= 0:
            raise EvaluationError(f"{path}.specialize.max_steps must be positive")
        generator_state = {
            "fragment_name": f"{bind}-generated",
            "fragment_goal": str(declaration.get("question")),
            "fragment_context": context_data,
            "fragment_available_bindings": list(context),
            "fragment_allowed_tools": (
                list(allowed_tools) if isinstance(allowed_tools, list) else []
            ),
            "fragment_max_steps": maximum,
        }
        generator_usage = {"llm": 0, "tools": 0}
        try:
            self._steps(
                process["steps"],
                generator_state,
                events,
                generator_usage,
                process.get("limits", {}),
                f"{path}.subprocess[{process_name}]",
            )
        except _TerminalReached as terminal:
            if terminal.status != "succeeded":
                raise
        fragment_state = generator_state.get("fragment_state")
        fragment = (
            fragment_state.get("fragment")
            if isinstance(fragment_state, Mapping)
            else None
        )
        if not isinstance(fragment_state, Mapping):
            raise EvaluationError(f"{path}.specialize subprocess returned no state")
        if not isinstance(fragment, Mapping) or fragment_state.get("done") is not True:
            raise EvaluationError(f"{path}.specialize subprocess returned no fragment")
        usage["llm"] += generator_usage["llm"]
        usage["tools"] += generator_usage["tools"]
        self._limit(usage, limits, "llm_activations", "LLM activations")
        self._limit(usage, limits, "tool_calls", "tool calls")
        state[bind] = fragment
        events.append(
            EvaluationEvent(
                "specialize",
                path,
                {
                    "bind": bind,
                    "name": fragment.get("name"),
                    "fragment": fragment,
                    "process_fingerprint": process_fingerprint,
                },
            )
        )

    def _load_process(self, name: str) -> Mapping[str, Any]:
        from procedrr import parse_and_validate

        if not re.fullmatch(r"[a-z][a-z0-9-]*", name):
            raise EvaluationError(f"invalid subprocess name: {name!r}")
        path = self.process_directory / f"{name}.yaml"
        if not path.is_file():
            raise EvaluationError(f"Procedrr subprocess does not exist: {path}")
        return parse_and_validate(
            path.read_text(encoding="utf-8"),
            command_catalog=self.command_catalog,
        )

    def _call_fragment(
        self,
        declaration: Mapping[str, Any],
        state: dict[str, Any],
        events: list[EvaluationEvent],
        usage: dict[str, int],
        limits: Mapping[str, Any],
        path: str,
    ) -> None:
        if not isinstance(declaration, Mapping):
            raise EvaluationError(f"{path}.call is malformed")
        reference = declaration.get("fragment")
        if not isinstance(reference, str):
            raise EvaluationError(f"{path}.call.fragment is required")
        fragment = _resolve_value(reference, state)
        if not isinstance(fragment, Mapping) or not isinstance(
            fragment.get("steps"), list
        ):
            raise EvaluationError(f"{path}.call.fragment must resolve to a fragment")
        maximum = declaration.get("max_steps", len(fragment["steps"]))
        if not isinstance(maximum, int) or len(fragment["steps"]) > maximum:
            raise EvaluationError(f"{path}.call.fragment exceeds its step bound")
        try:
            self._steps(
                fragment["steps"], state, events, usage, limits, f"{path}.call.body"
            )
        except _TerminalReached as terminal:
            if terminal.status != "succeeded":
                raise

    def _call_process(
        self,
        declaration: Mapping[str, Any],
        state: dict[str, Any],
        events: list[EvaluationEvent],
        usage: dict[str, int],
        limits: Mapping[str, Any],
        path: str,
    ) -> None:
        process_name = declaration.get("process")
        inputs = declaration.get("inputs", {})
        outputs = declaration.get("outputs", {})
        if not isinstance(process_name, str) or not isinstance(inputs, Mapping):
            raise EvaluationError(f"{path}.call process is malformed")
        if not isinstance(outputs, Mapping) or not outputs:
            raise EvaluationError(f"{path}.call.outputs is required")
        process = self._load_process(process_name)
        process_fingerprint = _fingerprint_document(process)
        declared_outputs = process.get("outputs")
        if not isinstance(declared_outputs, Mapping) or not declared_outputs:
            raise EvaluationError(f"{path}.call process must declare named outputs")
        cached = self._replay_events.get(("process", path))
        if cached is not None:
            if cached.data.get("process_fingerprint") != process_fingerprint:
                raise EvaluationError(
                    f"{path}.call process revision changed since checkpoint"
                )
            cached_outputs = cached.data.get("output_bindings")
            if not isinstance(cached_outputs, Mapping):
                raise EvaluationError(
                    f"{path}.call checkpoint has no recorded output bindings"
                )
            if set(cached_outputs) != set(outputs):
                raise EvaluationError(f"{path}.call checkpoint outputs do not match")
            for name, child_reference in outputs.items():
                if not isinstance(name, str) or not isinstance(child_reference, str):
                    raise EvaluationError(f"{path}.call output mapping is malformed")
                if child_reference not in declared_outputs:
                    raise EvaluationError(
                        f"{path}.call output is not declared by {process_name}: "
                        f"{child_reference}"
                    )
                value = cached_outputs[name]
                schema = declared_outputs[child_reference]
                if isinstance(schema, Mapping):
                    validate_json(value, schema)
                state[name] = value
                if isinstance(schema, Mapping):
                    self._schemas_for_state(state)[name] = dict(schema)
            return
        process_inputs = process.get("inputs", [])
        if not isinstance(process_inputs, list):
            raise EvaluationError(f"{path}.call process has malformed inputs")
        declared_inputs = {
            item.get("name"): item
            for item in process_inputs
            if isinstance(item, Mapping) and isinstance(item.get("name"), str)
        }
        undeclared = sorted(set(inputs) - set(declared_inputs))
        if undeclared:
            raise EvaluationError(
                f"{path}.call supplied undeclared inputs: {', '.join(undeclared)}"
            )
        required_inputs = {
            name
            for item in process_inputs
            if isinstance(item, Mapping)
            and item.get("required") is True
            and isinstance((name := item.get("name")), str)
        }
        child_state = {
            str(name): _resolve_value(value, state) for name, value in inputs.items()
        }
        child_schemas: dict[str, Mapping[str, Any]] = {
            str(name): dict(self._schemas_for_state(state).get(str(name), {}))
            for name in inputs
            if str(name) in self._schemas_for_state(state)
        }
        self._schemas_by_state[id(child_state)] = child_schemas
        missing = sorted(name for name in required_inputs if name not in child_state)
        if missing:
            raise EvaluationError(
                f"{path}.call process is missing inputs: {', '.join(missing)}"
            )
        for name, value in child_state.items():
            input_declaration = declared_inputs[name]
            schema = input_declaration.get("schema")
            if not isinstance(schema, Mapping):
                schema_type = input_declaration.get("type")
                if schema_type in {
                    "string",
                    "integer",
                    "number",
                    "boolean",
                    "object",
                    "array",
                }:
                    schema = {"type": schema_type}
            if isinstance(schema, Mapping):
                validate_json(value, schema)
        child_limits = _bounded_limits(limits, process.get("limits"))
        steps = process.get("steps")
        if not isinstance(steps, list):
            raise EvaluationError(f"{path}.call process has no steps")
        terminal_status = "succeeded"
        parent_recoveries = self._document_recoveries
        child_recoveries = process.get("recoveries", {})
        self._document_recoveries = (
            child_recoveries if isinstance(child_recoveries, Mapping) else {}
        )
        try:
            try:
                self._steps(
                    steps, child_state, events, usage, child_limits, f"{path}.call"
                )
            except _TerminalReached as terminal:
                terminal_status = terminal.status
                if terminal_status != "succeeded":
                    raise
        finally:
            self._document_recoveries = parent_recoveries
        for parent_name, child_reference in outputs.items():
            if not isinstance(parent_name, str) or not isinstance(child_reference, str):
                raise EvaluationError(
                    f"{path}.call.outputs must map names to references"
                )
            if child_reference not in declared_outputs:
                raise EvaluationError(
                    f"{path}.call output is not declared by {process_name}: "
                    f"{child_reference}"
                )
            value = _resolve_binding(child_state, child_reference)
            schema = declared_outputs[child_reference]
            if isinstance(schema, Mapping):
                validate_json(value, schema)
            state[parent_name] = value
            if isinstance(schema, Mapping):
                self._schemas_for_state(state)[parent_name] = dict(schema)
        events.append(
            EvaluationEvent(
                "process",
                path,
                {
                    "name": process_name,
                    "process_fingerprint": process_fingerprint,
                    "outputs": list(outputs),
                    "output_bindings": {
                        parent_name: state[parent_name] for parent_name in outputs
                    },
                    "terminal_status": terminal_status,
                },
            )
        )

    def _attempt(
        self,
        declaration: Mapping[str, Any],
        state: dict[str, Any],
        events: list[EvaluationEvent],
        usage: dict[str, int],
        limits: Mapping[str, Any],
        path: str,
    ) -> None:
        body = declaration.get("body")
        route = declaration.get("on_failure")
        maximum = declaration.get("max_attempts")
        attempt_id = declaration.get("id")
        if (
            not isinstance(body, list)
            or not isinstance(route, Mapping)
            or not isinstance(attempt_id, str)
        ):
            raise EvaluationError(f"{path}.attempt is malformed")
        if route.get("resume") != attempt_id:
            raise EvaluationError(f"{path}.attempt resume must match its id")
        if not isinstance(maximum, int) or maximum <= 0:
            raise EvaluationError(f"{path}.attempt.max_attempts must be positive")
        recovery_name = route.get("recovery")
        recoveries = getattr(self, "_document_recoveries", {})
        recovery = (
            recoveries.get(recovery_name) if isinstance(recovery_name, str) else None
        )
        if not isinstance(recovery, Mapping) or not isinstance(
            recovery.get("steps"), list
        ):
            raise EvaluationError(f"{path}.attempt recovery is not declared")
        for attempt in range(1, maximum + 1):
            try:
                self._steps(
                    body, state, events, usage, limits, f"{path}.attempt[{attempt}]"
                )
                return
            except EvaluationError as exc:
                failure: dict[str, Any] = {}
                if isinstance(exc, ValidationGateError):
                    failure["category"] = _validation_category(exc.evidence)
                    failure["validation"] = exc.evidence
                else:
                    failure["category"] = "execution_error"
                    failure["error"] = str(exc)
                state["failure"] = failure
                events.append(EvaluationEvent("recovery", path, state["failure"]))
                self._steps(
                    recovery["steps"],
                    state,
                    events,
                    usage,
                    limits,
                    f"{path}.recovery[{attempt}]",
                )
        raise EvaluationError(f"{path}.attempt exhausted after {maximum} attempts")

    def _repeat(
        self,
        declaration: Mapping[str, Any],
        state: dict[str, Any],
        events: list[EvaluationEvent],
        usage: dict[str, int],
        limits: Mapping[str, Any],
        path: str,
    ) -> None:
        body = declaration.get("body")
        maximum = declaration.get("max_iterations")
        until = declaration.get("until")
        collect = declaration.get("collect")
        if (
            not isinstance(body, list)
            or not isinstance(maximum, int)
            or maximum <= 0
            or not isinstance(until, Mapping)
        ):
            raise EvaluationError(f"{path}.repeat is malformed")
        collected: list[Any] = []
        for iteration in range(1, maximum + 1):
            self._steps(
                body, state, events, usage, limits, f"{path}.repeat[{iteration}]"
            )
            if isinstance(collect, Mapping) and isinstance(collect.get("value"), str):
                value = _resolve_binding(state, collect["value"])
                if value is not None:
                    collected.append(value)
            if _resolve_binding(state, str(until.get("subject"))) == until.get(
                "equals"
            ):
                if isinstance(collect, Mapping) and isinstance(
                    collect.get("binding"), str
                ):
                    state[collect["binding"]] = collected
                return
        subject = str(until.get("subject"))
        root = subject.split(".", 1)[0]
        last_state = _compact_value(state.get(root), max_chars=2000)
        raise EvaluationError(
            f"{path}.repeat exhausted after {maximum} iterations; "
            f"last {root}={_json_text(last_state)}"
        )

    def _branch(
        self,
        declaration: Mapping[str, Any],
        state: dict[str, Any],
        events: list[EvaluationEvent],
        usage: dict[str, int],
        limits: Mapping[str, Any],
        path: str,
    ) -> None:
        subject = declaration.get("subject")
        cases = declaration.get("cases")
        if not isinstance(subject, str) or not isinstance(cases, Mapping):
            raise EvaluationError(f"{path}.branch is malformed")
        value = _resolve_binding(state, subject)
        body = cases.get(value, declaration.get("default"))
        if body is None:
            raise EvaluationError(f"{path}.branch has no case for {value!r}")
        if not isinstance(body, list):
            raise EvaluationError(f"{path}.branch case must be a list")
        self._steps(body, state, events, usage, limits, f"{path}.branch[{value!r}]")

    def _operation(
        self,
        operation: Mapping[str, Any],
        state: dict[str, Any],
        events: list[EvaluationEvent],
        usage: dict[str, int],
        limits: Mapping[str, Any],
        path: str,
    ) -> None:
        tool = operation.get("tool")
        if not isinstance(tool, str):
            raise EvaluationError(f"{path}.operation.tool is required")
        raw_parameters = operation.get("parameters")
        if raw_parameters is None:
            raw_parameters = {
                key: value
                for key, value in operation.items()
                if key not in {"tool", "bind", "returns"}
            }
        parameters = _resolve_value(raw_parameters, state)
        if tool == "internal" and "command" in operation:
            if not isinstance(parameters, Mapping):
                raise EvaluationError(f"{path}.operation.parameters must be a mapping")
            parameters = {
                "command": _resolve_value(operation["command"], state),
                **parameters,
            }
        cached = self._replay_events.get(("operation", path))
        suspended_reason: str | None
        if cached is not None:
            if cached.data.get("tool") != tool:
                raise EvaluationError(f"{path}.operation differs from checkpoint")
            replay_inputs = cached.data.get("replay_inputs")
            if isinstance(replay_inputs, Mapping) and dict(replay_inputs) != dict(
                parameters
            ):
                raise EvaluationError(
                    f"{path}.operation inputs differ from checkpoint replay state"
                )
            result = cached.data.get("replay_output", cached.data.get("output"))
            resumed = path == self._resume_path
            if resumed:
                if cached.data.get("resume_with_executor") is True:
                    resume_parameters = dict(parameters)
                    resume_parameters["__procedrr_resume__"] = {
                        "value": self._resume_value,
                        "data": cached.data.get("resume_data", {}),
                    }
                    try:
                        result = self.operation_executor(tool, resume_parameters)
                    except SuspendEvaluation as suspension:
                        result = suspension.result
                        suspended_reason = suspension.reason
                        resume_data = suspension.resume_data
                        resume_with_executor = suspension.resume_with_executor
                    else:
                        suspended_reason = None
                        resume_data = {}
                        resume_with_executor = False
                else:
                    result = self._resume_value
                    suspended_reason = None
                    resume_data = {}
                    resume_with_executor = False
                returns = operation.get("returns")
                if isinstance(returns, Mapping):
                    validate_json(result, returns)
                bind = operation.get("bind")
                if isinstance(bind, str):
                    state[bind] = result
                    if isinstance(returns, Mapping):
                        self._schemas_for_state(state)[bind] = dict(returns)
                events.append(
                    EvaluationEvent(
                        "resume",
                        path,
                        {
                            "output": _compact_value(result, max_chars=12000),
                            "suspended": suspended_reason is not None,
                        },
                    )
                )
                if suspended_reason is not None:
                    events.append(
                        EvaluationEvent(
                            "suspend",
                            path,
                            {"reason": suspended_reason},
                        )
                    )
                    raise _Suspended(path, suspended_reason)
            else:
                returns = operation.get("returns")
                if isinstance(returns, Mapping):
                    validate_json(result, returns)
                if isinstance((bind := operation.get("bind")), str):
                    state[bind] = result
                    if isinstance(returns, Mapping):
                        self._schemas_for_state(state)[bind] = dict(returns)
            return
        usage["tools"] += 1
        self._limit(usage, limits, "tool_calls", "tool calls")
        if self.command_catalog is not None and tool == "internal":
            command = (
                parameters.get("command") if isinstance(parameters, Mapping) else None
            )
            if (
                isinstance(command, Sequence)
                and command
                and isinstance(command[0], str)
            ):
                spec = self.command_catalog.get(command[0])
                if spec is None:
                    raise EvaluationError(
                        f"unknown cataloged internal command: {command[0]!r}"
                    )
                try:
                    spec.validate_input(
                        {
                            key: value
                            for key, value in parameters.items()
                            if key != "command"
                        }
                    )
                except ValueError as exc:
                    raise EvaluationError(str(exc)) from exc
        try:
            result = self.operation_executor(tool, parameters)
        except SuspendEvaluation as suspension:
            result = suspension.result
            suspended_reason = suspension.reason
            resume_data = suspension.resume_data
            resume_with_executor = suspension.resume_with_executor
        else:
            suspended_reason = None
            resume_data = {}
            resume_with_executor = False
        returns = operation.get("returns")
        output_schema = returns if isinstance(returns, Mapping) else None
        if isinstance(output_schema, Mapping):
            validate_json(result, output_schema)
        bind = operation.get("bind")
        if isinstance(bind, str):
            state[bind] = result
            if isinstance(output_schema, Mapping):
                self._schemas_for_state(state)[bind] = dict(output_schema)
        events.append(
            EvaluationEvent(
                "operation",
                path,
                {
                    "tool": tool,
                    "bind": bind,
                    "inputs": _compact_value(dict(parameters), max_chars=12000),
                    "replay_inputs": dict(parameters),
                    "output": _compact_value(result, max_chars=12000),
                    "replay_output": result,
                    "resume_data": resume_data,
                    "resume_with_executor": resume_with_executor,
                },
            )
        )
        if suspended_reason is not None:
            events.append(
                EvaluationEvent("suspend", path, {"reason": suspended_reason})
            )
            raise _Suspended(path, suspended_reason)

    def _judge(
        self,
        judge: Mapping[str, Any],
        state: dict[str, Any],
        events: list[EvaluationEvent],
        usage: dict[str, int],
        limits: Mapping[str, Any],
        path: str,
    ) -> None:
        cached = self._replay_events.get(("judge", path))
        if cached is not None:
            output_name = cached.data.get("output")
            output = cached.data.get("value")
            if not isinstance(output_name, str):
                raise EvaluationError(f"{path}.judge checkpoint is malformed")
            validate_json(output, judge["output"]["schema"])
            state[output_name] = output
            self._schemas_for_state(state)[output_name] = dict(
                judge["output"]["schema"]
            )
            return
        messages, context_metrics = self._prepare_judge(judge, state, limits, path)
        usage["llm"] += 1
        self._limit(usage, limits, "llm_activations", "LLM activations")
        schema = judge["output"]["schema"]
        client = self.judge_clients.get(str(judge.get("provider", "")), self.llm)
        trace_id = hashlib.sha256(
            json.dumps(messages, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        try:
            try:
                output = client.complete_json(  # type: ignore[call-arg]
                    messages, response_schema=schema
                )
            except TypeError:
                output = client.complete_json(messages)
        except Exception as exc:
            events.append(
                EvaluationEvent(
                    "judge_failed",
                    path,
                    {
                        "provider": str(judge.get("provider", "default")),
                        "output": str(judge["output"]["name"]),
                        "trace_id": trace_id,
                        "error_type": type(exc).__name__,
                    },
                )
            )
            raise EvaluationError(
                f"{path}.judge provider={judge.get('provider', 'default')} "
                f"output={judge['output']['name']} trace_id={trace_id} "
                f"failed with {type(exc).__name__}: {exc}"
            ) from exc
        validate_json(output, judge["output"]["schema"])
        state[judge["output"]["name"]] = output
        self._schemas_for_state(state)[judge["output"]["name"]] = dict(schema)
        events.append(
            EvaluationEvent(
                "judge",
                path,
                {
                    "output": judge["output"]["name"],
                    "value": output,
                    "replay_value": output,
                    "messages": messages,
                    "context_metrics": context_metrics,
                },
            )
        )

    def _prepare_judge(
        self,
        judge: Mapping[str, Any],
        state: dict[str, Any],
        limits: Mapping[str, Any],
        path: str,
    ) -> tuple[list[dict[str, str]], dict[str, Any]]:
        context = judge.get("context", [])
        if not isinstance(context, list):
            raise EvaluationError(f"{path}.judge.context must be a list")
        value_limit = _positive_limit(limits, "context_value_chars", 6000)
        resolved_context = {name: _resolve_binding(state, name) for name in context}
        context_data = {
            name: _compact_value(value, max_chars=value_limit)
            for name, value in resolved_context.items()
        }
        raw_binding_chars = {
            name: len(_json_text(value)) for name, value in resolved_context.items()
        }
        compact_binding_chars = {
            name: len(_json_text(value)) for name, value in context_data.items()
        }
        context_limit = _positive_limit(limits, "context_chars", 24000)
        context_text = _bounded_context_text(
            context_data,
            context_limit,
        )
        instructions = _resolve_prompt_instructions(
            judge.get("instructions", []),
            judge.get("prompt_rules", []),
            state,
            path,
        )
        messages = [
            {"role": "system", "content": str(judge["prompt_system"])},
            {
                "role": "user",
                "content": (
                    "Instructions:\n- "
                    + "\n- ".join(instructions)
                    + "\n\nQuestion:\n"
                    + str(judge["question"])
                    + "\n\nContext:\n"
                    + context_text
                ),
            },
        ]
        return messages, {
            "raw_chars": sum(raw_binding_chars.values()),
            "compacted_chars": sum(compact_binding_chars.values()),
            "serialized_chars": len(context_text),
            "limit_chars": context_limit,
            "truncated": len(context_text) >= context_limit
            and not _context_fits(context_data, context_limit),
            "raw_binding_chars": raw_binding_chars,
            "compacted_binding_chars": compact_binding_chars,
        }

    def _judge_batch(
        self,
        judge: Mapping[str, Any],
        item_states: list[tuple[dict[str, Any], str]],
        events: list[EvaluationEvent],
        usage: dict[str, int],
        limits: Mapping[str, Any],
    ) -> None:
        schema = judge["output"]["schema"]
        provider = str(judge.get("provider", ""))
        client = self.judge_clients.get(provider, self.llm)
        batch_complete = getattr(client, "complete_json_batch", None)
        if not callable(batch_complete):
            for state, path in item_states:
                self._judge(judge, state, events, usage, limits, path)
            return

        pending: list[
            tuple[dict[str, Any], str, list[dict[str, str]], dict[str, Any]]
        ] = []
        for state, path in item_states:
            cached = self._replay_events.get(("judge", path))
            if cached is not None:
                output_name = cached.data.get("output")
                output = cached.data.get("value")
                if not isinstance(output_name, str):
                    raise EvaluationError(f"{path}.judge checkpoint is malformed")
                validate_json(output, schema)
                state[output_name] = output
                self._schemas_for_state(state)[output_name] = dict(schema)
                continue
            messages, metrics = self._prepare_judge(judge, state, limits, path)
            pending.append((state, path, messages, metrics))
        if not pending:
            return
        usage["llm"] += len(pending)
        self._limit(usage, limits, "llm_activations", "LLM activations")
        try:
            outputs = batch_complete(
                [(messages, schema) for _, _, messages, _ in pending]
            )
            if len(outputs) != len(pending):
                raise EvaluationError("batch judge returned an unexpected result count")
        except Exception as exc:
            first_path = pending[0][1]
            raise EvaluationError(
                f"{first_path}.judge provider={provider or 'default'} batch "
                f"failed with {type(exc).__name__}: {exc}"
            ) from exc
        for (state, path, messages, metrics), output in zip(
            pending, outputs, strict=True
        ):
            validate_json(output, schema)
            output_name = str(judge["output"]["name"])
            state[output_name] = output
            self._schemas_for_state(state)[output_name] = dict(schema)
            events.append(
                EvaluationEvent(
                    "judge",
                    path,
                    {
                        "output": output_name,
                        "value": output,
                        "replay_value": output,
                        "messages": messages,
                        "context_metrics": metrics,
                    },
                )
            )

    def _loop(
        self,
        kind: str,
        declaration: Mapping[str, Any],
        state: dict[str, Any],
        events: list[EvaluationEvent],
        usage: dict[str, int],
        limits: Mapping[str, Any],
        path: str,
    ) -> None:
        snapshot = declaration["snapshot"]
        name = snapshot["name"] if isinstance(snapshot, Mapping) else snapshot
        items = (
            snapshot["values"]
            if isinstance(snapshot, Mapping) and "values" in snapshot
            else _resolve_binding(state, name)
        )
        if not isinstance(items, Sequence) or isinstance(items, (str, bytes)):
            raise EvaluationError(f"{path}.{kind}.snapshot must resolve to a sequence")
        max_items = (
            snapshot.get("max_items")
            if isinstance(snapshot, Mapping)
            else declaration.get("max_admissions")
        )
        if max_items is not None and (
            not isinstance(max_items, int) or len(items) > max_items
        ):
            raise EvaluationError(f"{path}.{kind}.snapshot exceeds its bound")
        epochs = declaration.get("max_epochs", 1) if kind == "worklist" else 1
        body = declaration.get("body", declaration.get("steps"))
        if not isinstance(body, list):
            raise EvaluationError(f"{path}.{kind}.body must be a list")
        collect = declaration.get("collect")
        collect_mode = (
            collect.get("mode", "map") if isinstance(collect, Mapping) else "map"
        )
        collected: dict[str, Any] = {}
        collected_list: list[Any] = []
        batch_judge = (
            body[0].get("judge")
            if kind == "for_each"
            and len(body) == 1
            and isinstance(body[0], Mapping)
            and set(body[0]) == {"judge"}
            else None
        )
        if isinstance(batch_judge, Mapping):
            provider = str(batch_judge.get("provider", ""))
            client = self.judge_clients.get(provider, self.llm)
            batch_method = getattr(client, "complete_json_batch", None)
            supports_batch = getattr(client, "supports_batch", callable(batch_method))
        else:
            supports_batch = False
        if isinstance(batch_judge, Mapping) and supports_batch:
            binding = declaration.get("item_binding", declaration.get("item"))
            if not isinstance(binding, str):
                raise EvaluationError(f"{path}.{kind}.item_binding is required")
            item_states: list[tuple[dict[str, Any], str]] = []
            for index, item in enumerate(items):
                item_state = dict(state)
                item_state[binding] = item
                item_states.append((item_state, f"{path}.{kind}[0][{index}][0]"))
            self._judge_batch(batch_judge, item_states, events, usage, limits)
            if isinstance(collect, Mapping):
                value_binding = collect.get("value")
                for (item_state, _), item in zip(item_states, items, strict=True):
                    output = (
                        item_state.get(value_binding)
                        if isinstance(value_binding, str)
                        else item
                    )
                    if output is not None:
                        if collect_mode == "list":
                            key_name = collect.get("key", "item")
                            collected_list.append(
                                {str(key_name): item, "result": output}
                            )
                        else:
                            collected[str(item)] = output
                if isinstance(collect.get("binding"), str):
                    state[collect["binding"]] = (
                        collected_list if collect_mode == "list" else collected
                    )
            self._limit(usage, limits, "llm_activations", "LLM activations")
            return
        max_parallel = declaration.get("max_parallel", 1)
        if (
            not isinstance(max_parallel, int)
            or isinstance(max_parallel, bool)
            or not 1 <= max_parallel <= 32
        ):
            raise EvaluationError(f"{path}.{kind}.max_parallel is malformed")
        if max_parallel > 1:
            if kind != "for_each" or not _judge_only_steps(body):
                raise EvaluationError(
                    f"{path}.{kind}.max_parallel supports judge-only for_each bodies"
                )
            binding = declaration.get("item_binding", declaration.get("item"))
            if not isinstance(binding, str):
                raise EvaluationError(f"{path}.{kind}.item_binding is required")

            def evaluate_item(
                index: int, item: Any
            ) -> tuple[
                dict[str, Any],
                list[EvaluationEvent],
                dict[str, int],
                Exception | None,
            ]:
                item_state = dict(state)
                item_state[binding] = item
                item_events: list[EvaluationEvent] = []
                item_usage = {"llm": 0, "tools": 0}
                try:
                    self._steps(
                        body,
                        item_state,
                        item_events,
                        item_usage,
                        limits,
                        f"{path}.{kind}[0][{index}]",
                    )
                except Exception as exc:  # preserve each item's failure event
                    return item_state, item_events, item_usage, exc
                return item_state, item_events, item_usage, None

            start = 0
            while start < len(items):
                max_chunk = max_parallel
                activation_limit = limits.get("llm_activations")
                if isinstance(activation_limit, int):
                    remaining = max(0, activation_limit - usage["llm"])
                    max_chunk = min(max_chunk, max(1, remaining + 1))
                chunk = items[start : start + max_chunk]
                with ThreadPoolExecutor(max_workers=len(chunk)) as pool:
                    futures = [
                        pool.submit(evaluate_item, start + offset, item)
                        for offset, item in enumerate(chunk)
                    ]
                    parallel_results = [future.result() for future in futures]

                item_errors: list[Exception] = []
                for item, (item_state, item_events, item_usage, item_error) in zip(
                    chunk, parallel_results, strict=True
                ):
                    usage["llm"] += item_usage["llm"]
                    usage["tools"] += item_usage["tools"]
                    for event in item_events:
                        events.append(event)
                    if item_error is not None:
                        item_errors.append(item_error)
                        continue
                    if isinstance(collect, Mapping):
                        value_binding = collect.get("value")
                        output = (
                            item_state.get(value_binding)
                            if isinstance(value_binding, str)
                            else item
                        )
                        if output is not None:
                            if collect_mode == "list":
                                key_name = collect.get("key", "item")
                                collected_list.append(
                                    {str(key_name): item, "result": output}
                                )
                            else:
                                collected[str(item)] = output
                self._limit(usage, limits, "llm_activations", "LLM activations")
                self._limit(usage, limits, "tool_calls", "tool calls")
                if item_errors:
                    raise item_errors[0]
                start += len(chunk)
            if isinstance(collect, Mapping) and isinstance(collect.get("binding"), str):
                state[collect["binding"]] = (
                    collected_list if collect_mode == "list" else collected
                )
            return
        for epoch in range(epochs):
            for index, item in enumerate(items):
                binding = declaration.get("item_binding", declaration.get("item"))
                if not isinstance(binding, str):
                    raise EvaluationError(f"{path}.{kind}.item_binding is required")
                state[binding] = item
                self._steps(
                    body,
                    state,
                    events,
                    usage,
                    limits,
                    f"{path}.{kind}[{epoch}][{index}]",
                )
                if isinstance(collect, Mapping):
                    value_binding = collect.get("value")
                    if isinstance(value_binding, str):
                        output = state.get(value_binding)
                    elif collect.get("key") == "category":
                        output = state.get("category_edits")
                    else:
                        output = item
                    if output is not None:
                        if collect_mode == "list":
                            key_name = collect.get("key", "item")
                            collected_list.append(
                                {str(key_name): item, "result": output}
                            )
                        else:
                            collected[str(item)] = output
        if isinstance(collect, Mapping) and isinstance(collect.get("binding"), str):
            state[collect["binding"]] = (
                collected_list if collect_mode == "list" else collected
            )

    def _gate(
        self,
        gate: Mapping[str, Any],
        state: dict[str, Any],
        events: list[EvaluationEvent],
        path: str,
    ) -> None:
        subject = str(gate["subject"])
        observed = _resolve_binding(state, subject)
        if observed == gate["equals"]:
            return
        if state.get("benchmark_mode") is True:
            warning = {
                "subject": subject,
                "expected": gate["equals"],
                "observed": observed,
            }
            state.setdefault("benchmark_gate_warnings", []).append(warning)
            events.append(EvaluationEvent("benchmark_gate_warning", path, warning))
            return
        root = subject.split(".", 1)[0]
        raise ValidationGateError(_resolve_binding(state, root))

    @staticmethod
    def _limit(
        usage: Mapping[str, int], limits: Mapping[str, Any], key: str, label: str
    ) -> None:
        bound = limits.get(key)
        usage_key = "llm" if key == "llm_activations" else "tools"
        if isinstance(bound, int) and usage[usage_key] > bound:
            raise EvaluationError(f"{label} budget exceeded")


def _judge_only_steps(steps: Sequence[Any]) -> bool:
    """Parallel loop iterations may run only independent model judgments."""
    return bool(steps) and all(
        isinstance(step, Mapping) and set(step) == {"judge"} for step in steps
    )


def _fingerprint_document(document: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        document, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _resolve_binding(state: Mapping[str, Any], path: str) -> Any:
    value: Any = state
    for segment in path.split("."):
        if not isinstance(value, Mapping) or segment not in value:
            raise EvaluationError(f"unknown binding: {path}")
        value = value[segment]
    return value


def _bounded_limits(parent: Mapping[str, Any], child: Any) -> Mapping[str, Any]:
    """Keep a subprocess inside both its declaration and its caller budget."""
    if not isinstance(child, Mapping):
        return parent
    bounded = dict(parent)
    for key in ("llm_activations", "tool_calls", "max_epochs"):
        child_value = child.get(key)
        parent_value = parent.get(key)
        if isinstance(child_value, int) and isinstance(parent_value, int):
            bounded[key] = min(child_value, parent_value)
        elif isinstance(child_value, int):
            bounded[key] = child_value
    return bounded


def _resolve_value(value: Any, state: Mapping[str, Any]) -> Any:
    if isinstance(value, Mapping) and value.get("type") in {"literal", "reference"}:
        kind = value.get("type")
        if kind == "literal":
            return value.get("value")
        reference = value.get("value")
        if not isinstance(reference, str):
            raise EvaluationError("reference value must be a binding path")
        return _resolve_binding(state, reference)
    if isinstance(value, str):
        match = _REFERENCE.fullmatch(value)
        if match:
            return _resolve_binding(state, match.group(1))

        def replace_reference(reference: re.Match[str]) -> str:
            resolved = _resolve_binding(state, reference.group(1))
            if isinstance(resolved, (Mapping, list, tuple)):
                return _json_text(resolved)
            return str(resolved)

        return _REFERENCE.sub(replace_reference, value)
    if isinstance(value, Mapping):
        return {key: _resolve_value(item, state) for key, item in value.items()}
    if isinstance(value, list):
        return [_resolve_value(item, state) for item in value]
    return value


def _resolve_prompt_instructions(
    base: Any,
    rules: Any,
    state: Mapping[str, Any],
    path: str,
) -> list[str]:
    if not isinstance(base, list):
        raise EvaluationError(f"{path}.judge.instructions must be a list")
    instructions = [str(item) for item in base]
    if rules in (None, []):
        return instructions
    if not isinstance(rules, list):
        raise EvaluationError(f"{path}.judge.prompt_rules must be a list")
    for index, rule in enumerate(rules):
        if not isinstance(rule, Mapping):
            raise EvaluationError(f"{path}.judge.prompt_rules[{index}] is malformed")
        condition = rule.get("when")
        if not isinstance(condition, Mapping):
            raise EvaluationError(
                f"{path}.judge.prompt_rules[{index}].when is malformed"
            )
        binding = condition.get("binding")
        if not isinstance(binding, str):
            raise EvaluationError(
                f"{path}.judge.prompt_rules[{index}].when.binding is required"
            )
        operators = [key for key in condition if key != "binding"]
        if len(operators) != 1:
            raise EvaluationError(
                f"{path}.judge.prompt_rules[{index}].when needs one comparison"
            )
        operator = operators[0]
        if operator not in {"equals", "not_equals", "in", "contains"}:
            raise EvaluationError(
                f"{path}.judge.prompt_rules[{index}] has unsupported comparison"
            )
        actual = _resolve_binding(state, binding)
        expected = condition[operator]
        matched = {
            "equals": actual == expected,
            "not_equals": actual != expected,
            "in": isinstance(expected, list) and actual in expected,
            "contains": (
                expected in actual
                if isinstance(actual, (str, list, tuple, set, Mapping))
                else False
            ),
        }[operator]
        if not matched:
            continue
        rule_instructions = rule.get("instructions")
        if not isinstance(rule_instructions, list) or not all(
            isinstance(item, str) for item in rule_instructions
        ):
            raise EvaluationError(
                f"{path}.judge.prompt_rules[{index}].instructions is malformed"
            )
        if rule.get("mode", "append") == "replace":
            instructions = list(rule_instructions)
        else:
            instructions.extend(rule_instructions)
    return instructions


def _json_text(value: Any) -> str:
    import json

    return json.dumps(value, sort_keys=True, default=str)


def _validation_category(evidence: Any) -> str:
    text = _json_text(evidence).lower()
    if "syntaxerror" in text or "syntax error" in text:
        return "syntax_error"
    if "assertionerror" in text or "assertion failed" in text:
        return "assertion_failure"
    if "filenotfounderror" in text or "no such file" in text:
        return "missing_file"
    if "traceback" in text or "error" in text:
        return "runtime_error"
    return "validation_failure"


def _positive_limit(limits: Mapping[str, Any], key: str, default: int) -> int:
    value = limits.get(key)
    return value if isinstance(value, int) and value > 0 else default


def _compact_value(value: Any, *, max_chars: int) -> Any:
    if isinstance(value, str):
        if len(value) <= max_chars:
            return value
        return value[:max_chars] + "...<truncated>"
    if isinstance(value, Mapping):
        items = list(value.items())
        compacted = {
            str(key): _compact_value(item, max_chars=max_chars)
            for key, item in items[:32]
        }
        if len(items) > 32:
            compacted["__truncated_items__"] = len(items) - 32
        return compacted
    if isinstance(value, (list, tuple)):
        compacted_list = [
            _compact_value(item, max_chars=max_chars) for item in value[:32]
        ]
        if len(value) > 32:
            compacted_list.append({"__truncated_items__": len(value) - 32})
        return compacted_list
    return value


def _bounded_context_text(value: Any, max_chars: int) -> str:
    encoded = _json_text(value)
    if len(encoded) <= max_chars:
        return encoded
    return encoded[:max_chars] + "...<context truncated>"


def _context_fits(value: Any, max_chars: int) -> bool:
    return len(_json_text(value)) <= max_chars


__all__ = [
    "EvaluationError",
    "EvaluationEvent",
    "EvaluationResult",
    "Evaluator",
    "OperationExecutor",
]
