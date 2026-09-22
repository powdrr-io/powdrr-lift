"""Interpret procedrr documents at the agent boundary.

This module deliberately does not implement repository tools.  The caller
injects an operation executor, normally an adapter around powdrr-lift's
existing workflow action handlers.  The evaluator owns only binding,
prompt construction, schema validation, and bounded control traversal.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jsonschema import ValidationError as JsonSchemaError
from jsonschema import validate as validate_json

from powdrr_lift.workrr.protocol import WorkflowLLMClient

_REFERENCE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_.-]*)\}")


class EvaluationError(RuntimeError):
    """A declaration, model response, or injected operation failed."""


class ValidationGateError(EvaluationError):
    """A validation gate failed with its bound evidence preserved."""

    def __init__(self, evidence: Any) -> None:
        self.evidence = evidence
        super().__init__("validation gate failed")


OperationExecutor = Callable[[str, Mapping[str, Any]], Any]


@dataclass(frozen=True, slots=True)
class EvaluationEvent:
    kind: str
    path: str
    data: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class EvaluationResult:
    bindings: Mapping[str, Any]
    events: tuple[EvaluationEvent, ...]
    llm_activations: int
    tool_calls: int
    binding_schemas: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)


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
    ) -> None:
        self.llm = llm
        self.operation_executor = operation_executor
        self.judge_clients = dict(judge_clients or {})
        self.process_directory = process_directory or Path(
            "docs/procedrr/skill-definitions"
        )
        self.command_catalog = command_catalog
        self._schemas_by_state: dict[int, dict[str, Mapping[str, Any]]] = {}

    @classmethod
    def with_workrr(
        cls,
        client: WorkflowLLMClient,
        operation_executor: OperationExecutor,
        *,
        skills_dir: Path,
        max_retries: int = 3,
    ) -> Evaluator:
        """Construct an evaluator using Workrr's structured repair boundary."""
        from powdrr_lift.workrr.procedrr import WorkrrProcedrrClient

        return cls(
            WorkrrProcedrrClient(
                client,
                skills_dir=skills_dir,
                max_retries=max_retries,
            ),
            operation_executor,
            process_directory=Path("docs/procedrr/skill-definitions"),
        )

    def evaluate(
        self,
        document: Mapping[str, Any],
        bindings: Mapping[str, Any] | None = None,
    ) -> EvaluationResult:
        state = dict(bindings or {})
        self._schemas_by_state = {id(state): {}}
        self._document_recoveries = document.get("recoveries", {})
        events: list[EvaluationEvent] = []
        usage = {"llm": 0, "tools": 0}
        limits = document.get("limits", {})
        try:
            self._steps(document["steps"], state, events, usage, limits, "steps")
        except (KeyError, TypeError, ValueError, JsonSchemaError) as exc:
            raise EvaluationError(str(exc)) from exc
        return EvaluationResult(
            state,
            tuple(events),
            usage["llm"],
            usage["tools"],
            dict(self._schemas_by_state[id(state)]),
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
                events.append(
                    EvaluationEvent("terminal", step_path, {"status": step["terminal"]})
                )
            elif "gate" in step:
                self._gate(step["gate"], state, step_path)
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
        value_limit = _positive_limit(limits, "context_value_chars", 6000)
        context_data = {
            name: _compact_value(_resolve_binding(state, name), max_chars=value_limit)
            for name in context
        }
        allowed_tools = declaration.get("allowed_tools")
        process_name = declaration.get("process", "generate-fragment")
        if not isinstance(process_name, str) or not process_name:
            raise EvaluationError(f"{path}.specialize.process must be a name")
        process = self._load_process(process_name)
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
        self._steps(
            process["steps"],
            generator_state,
            events,
            generator_usage,
            process.get("limits", {}),
            f"{path}.subprocess[{process_name}]",
        )
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
                {"bind": bind, "name": fragment.get("name")},
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
        self._steps(
            fragment["steps"], state, events, usage, limits, f"{path}.call.body"
        )

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
        declared_outputs = process.get("outputs")
        if not isinstance(declared_outputs, Mapping) or not declared_outputs:
            raise EvaluationError(f"{path}.call process must declare named outputs")
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
        self._steps(steps, child_state, events, usage, child_limits, f"{path}.call")
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
                {"name": process_name, "outputs": list(outputs)},
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
        usage["tools"] += 1
        self._limit(usage, limits, "tool_calls", "tool calls")
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
        result = self.operation_executor(tool, parameters)
        returns = operation.get("returns")
        output_schema = returns if isinstance(returns, Mapping) else None
        if isinstance(output_schema, Mapping):
            validate_json(result, output_schema)
        bind = operation.get("bind")
        if isinstance(bind, str):
            state[bind] = result
            if isinstance(output_schema, Mapping):
                self._schemas_for_state(state)[bind] = dict(output_schema)
        events.append(EvaluationEvent("operation", path, {"tool": tool, "bind": bind}))

    def _judge(
        self,
        judge: Mapping[str, Any],
        state: dict[str, Any],
        events: list[EvaluationEvent],
        usage: dict[str, int],
        limits: Mapping[str, Any],
        path: str,
    ) -> None:
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
        usage["llm"] += 1
        self._limit(usage, limits, "llm_activations", "LLM activations")
        schema = judge["output"]["schema"]
        client = self.judge_clients.get(str(judge.get("provider", "")), self.llm)
        try:
            output = client.complete_json(messages, response_schema=schema)  # type: ignore[call-arg]
        except TypeError:
            output = client.complete_json(messages)
        validate_json(output, judge["output"]["schema"])
        state[judge["output"]["name"]] = output
        self._schemas_for_state(state)[judge["output"]["name"]] = dict(schema)
        events.append(
            EvaluationEvent(
                "judge",
                path,
                {
                    "output": judge["output"]["name"],
                    # Preserve the validated intermediate result so live
                    # validation can inspect classification and contract
                    # quality, rather than checking only final artifacts.
                    "value": output,
                    "messages": messages,
                    "context_metrics": {
                        "raw_chars": sum(raw_binding_chars.values()),
                        "compacted_chars": sum(compact_binding_chars.values()),
                        "serialized_chars": len(context_text),
                        "limit_chars": context_limit,
                        "truncated": len(context_text) >= context_limit
                        and not _context_fits(context_data, context_limit),
                        "raw_binding_chars": raw_binding_chars,
                        "compacted_binding_chars": compact_binding_chars,
                    },
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
        self, gate: Mapping[str, Any], state: Mapping[str, Any], path: str
    ) -> None:
        if _resolve_binding(state, str(gate["subject"])) != gate["equals"]:
            subject = str(gate["subject"])
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
