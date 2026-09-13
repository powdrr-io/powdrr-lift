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
from typing import Any

from jsonschema import ValidationError as JsonSchemaError
from jsonschema import validate as validate_json

from powdrr_lift.workrr.protocol import WorkflowLLMClient

_REFERENCE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_.-]*)\}")


class EvaluationError(RuntimeError):
    """A declaration, model response, or injected operation failed."""


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


class Evaluator:
    """Run a parsed, procedrr-validated document using agent-owned adapters."""

    def __init__(
        self, llm: WorkflowLLMClient, operation_executor: OperationExecutor
    ) -> None:
        self.llm = llm
        self.operation_executor = operation_executor

    def evaluate(
        self,
        document: Mapping[str, Any],
        bindings: Mapping[str, Any] | None = None,
    ) -> EvaluationResult:
        state = dict(bindings or {})
        events: list[EvaluationEvent] = []
        usage = {"llm": 0, "tools": 0}
        limits = document.get("limits", {})
        try:
            self._steps(document["steps"], state, events, usage, limits, "steps")
        except (KeyError, TypeError, ValueError, JsonSchemaError) as exc:
            raise EvaluationError(str(exc)) from exc
        return EvaluationResult(state, tuple(events), usage["llm"], usage["tools"])

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
            elif "terminal" in step:
                events.append(
                    EvaluationEvent("terminal", step_path, {"status": step["terminal"]})
                )
            elif "gate" in step:
                self._gate(step["gate"], state, step_path)
            else:
                raise EvaluationError(f"{step_path} has no supported control")

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
                if key not in {"tool", "bind"}
            }
        parameters = _resolve_value(raw_parameters, state)
        result = self.operation_executor(tool, parameters)
        bind = operation.get("bind")
        if isinstance(bind, str):
            state[bind] = result
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
        context_data = {name: _resolve_binding(state, name) for name in context}
        messages = [
            {"role": "system", "content": str(judge["prompt_system"])},
            {
                "role": "user",
                "content": (
                    "Instructions:\n- "
                    + "\n- ".join(str(item) for item in judge["instructions"])
                    + "\n\nQuestion:\n"
                    + str(judge["question"])
                    + "\n\nContext:\n"
                    + _json_text(context_data)
                ),
            },
        ]
        usage["llm"] += 1
        self._limit(usage, limits, "llm_activations", "LLM activations")
        schema = judge["output"]["schema"]
        try:
            output = self.llm.complete_json(messages, response_schema=schema)  # type: ignore[call-arg]
        except TypeError:
            output = self.llm.complete_json(messages)
        validate_json(output, judge["output"]["schema"])
        state[judge["output"]["name"]] = output
        events.append(
            EvaluationEvent(
                "judge", path, {"output": judge["output"]["name"], "messages": messages}
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
        items = _resolve_binding(state, name)
        if not isinstance(items, Sequence) or isinstance(items, (str, bytes)):
            raise EvaluationError(f"{path}.{kind}.snapshot must resolve to a sequence")
        max_items = (
            snapshot.get("max_items")
            if isinstance(snapshot, Mapping)
            else declaration.get("max_admissions")
        )
        if not isinstance(max_items, int) or len(items) > max_items:
            raise EvaluationError(f"{path}.{kind}.snapshot exceeds its bound")
        epochs = declaration.get("max_epochs", 1) if kind == "worklist" else 1
        body = declaration.get("body", declaration.get("steps"))
        if not isinstance(body, list):
            raise EvaluationError(f"{path}.{kind}.body must be a list")
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

    def _gate(
        self, gate: Mapping[str, Any], state: Mapping[str, Any], path: str
    ) -> None:
        if _resolve_binding(state, str(gate["subject"])) != gate["equals"]:
            raise EvaluationError(f"{path} failed")

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


def _resolve_value(value: Any, state: Mapping[str, Any]) -> Any:
    if isinstance(value, str):
        match = _REFERENCE.fullmatch(value)
        return _resolve_binding(state, match.group(1)) if match else value
    if isinstance(value, Mapping):
        return {key: _resolve_value(item, state) for key, item in value.items()}
    if isinstance(value, list):
        return [_resolve_value(item, state) for item in value]
    return value


def _json_text(value: Any) -> str:
    import json

    return json.dumps(value, sort_keys=True, default=str)


__all__ = [
    "EvaluationError",
    "EvaluationEvent",
    "EvaluationResult",
    "Evaluator",
    "OperationExecutor",
]
