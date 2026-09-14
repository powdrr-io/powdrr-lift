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
        )

    def evaluate(
        self,
        document: Mapping[str, Any],
        bindings: Mapping[str, Any] | None = None,
    ) -> EvaluationResult:
        state = dict(bindings or {})
        self._document_recoveries = document.get("recoveries", {})
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
            elif "attempt" in step:
                self._attempt(step["attempt"], state, events, usage, limits, step_path)
            elif "terminal" in step:
                events.append(
                    EvaluationEvent("terminal", step_path, {"status": step["terminal"]})
                )
            elif "gate" in step:
                self._gate(step["gate"], state, step_path)
            else:
                raise EvaluationError(f"{step_path} has no supported control")

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
                state["failure"] = {"message": str(exc), "attempt": attempt}
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
                    + context_text
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
                "judge",
                path,
                {
                    "output": judge["output"]["name"],
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
        if not isinstance(max_items, int) or len(items) > max_items:
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


def _json_text(value: Any) -> str:
    import json

    return json.dumps(value, sort_keys=True, default=str)


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
