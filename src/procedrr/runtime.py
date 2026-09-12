"""Small reference evaluator for compiled procedrr workflows.

The evaluator owns control flow. Callers provide narrow handlers for operations
and decisions; a handler returns data, never a next-step instruction.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from procedrr.compiler import CompiledWorkflow
from procedrr.model import (
    CallNode,
    ControlNode,
    ForEachNode,
    JudgeNode,
    MatchNode,
    OperationNode,
    ParallelNode,
    RepeatNode,
    RetryNode,
    SequenceNode,
    SuspendNode,
    TerminalNode,
    TerminalStatus,
    WorklistNode,
)


class ExecutionError(RuntimeError):
    """A handler failure or invalid runtime value."""


class RetryableFailure(ExecutionError):
    """A failure that may be retried when its code is declared."""

    def __init__(self, code: str, message: str = "") -> None:
        self.code = code
        super().__init__(message or code)


@dataclass(frozen=True, slots=True)
class ExecutionEvent:
    kind: str
    path: str
    data: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    status: TerminalStatus
    values: Mapping[str, Any]
    events: tuple[ExecutionEvent, ...]
    error: str | None = None


DecisionHandler = Callable[[JudgeNode, Mapping[str, Any]], Any]
OperationHandler = Callable[[OperationNode, Mapping[str, Any]], Any]


class ReferenceRuntime:
    """Execute the finite control tree while enforcing its compiled budget."""

    def __init__(
        self,
        *,
        decision_handler: DecisionHandler,
        operation_handler: OperationHandler,
    ) -> None:
        self.decision_handler = decision_handler
        self.operation_handler = operation_handler

    def execute(
        self, workflow: CompiledWorkflow, values: Mapping[str, Any] | None = None
    ) -> ExecutionResult:
        state = dict(values or {})
        events: list[ExecutionEvent] = []
        usage = {"llm": 0, "tools": 0}
        limits = workflow.definition.limits
        try:
            status = self._run(
                workflow.definition.body, state, events, usage, limits, "body"
            )
        except (ExecutionError, ValueError, TypeError) as exc:
            return ExecutionResult(
                TerminalStatus.FAILED, state, tuple(events), str(exc)
            )
        return ExecutionResult(status, state, tuple(events))

    def _run(
        self,
        node: ControlNode,
        state: dict[str, Any],
        events: list[ExecutionEvent],
        usage: dict[str, int],
        limits: Any,
        path: str,
    ) -> TerminalStatus:
        if isinstance(node, TerminalNode):
            events.append(
                ExecutionEvent("terminal", path, {"status": node.status.value})
            )
            return node.status
        if isinstance(node, SuspendNode):
            events.append(ExecutionEvent("suspend", path, {"reason": node.reason}))
            return TerminalStatus.SUSPENDED
        if isinstance(node, JudgeNode):
            usage["llm"] += 1 + node.decision.limits.response_repairs
            self._check_budget(usage, limits)
            state[node.decision.output_name] = self.decision_handler(node, dict(state))
            events.append(
                ExecutionEvent("decision", path, {"output": node.decision.output_name})
            )
            return TerminalStatus.SUCCEEDED
        if isinstance(node, OperationNode):
            usage["tools"] += 1
            self._check_budget(usage, limits)
            result = self.operation_handler(node, dict(state))
            if node.output_name is not None:
                state[node.output_name] = result
            events.append(ExecutionEvent("operation", path, {"name": node.name}))
            return TerminalStatus.SUCCEEDED
        if isinstance(node, SequenceNode):
            return self._run_sequence(node.nodes, state, events, usage, limits, path)
        if isinstance(node, MatchNode):
            value = _resolve(state, node.value_binding)
            for index, case in enumerate(node.cases):
                if value == case.value:
                    return self._run(
                        case.body,
                        state,
                        events,
                        usage,
                        limits,
                        f"{path}.cases[{index}]",
                    )
            if node.otherwise is None:
                raise ExecutionError(f"no match case for {node.value_binding!r}")
            return self._run(
                node.otherwise, state, events, usage, limits, f"{path}.otherwise"
            )
        if isinstance(node, ForEachNode):
            items = _snapshot(state, node.snapshot.name, node.snapshot.max_items)
            status = TerminalStatus.SUCCEEDED
            for index, item in enumerate(items):
                state[node.item_binding] = item
                status = self._run(
                    node.body, state, events, usage, limits, f"{path}[{index}]"
                )
                if status != TerminalStatus.SUCCEEDED:
                    return status
            return status
        if isinstance(node, WorklistNode):
            items = _snapshot(state, node.snapshot.name, node.max_admissions)
            for epoch in range(node.max_epochs):
                for index, item in enumerate(items):
                    state[node.item_binding] = item
                    status = self._run(
                        node.body,
                        state,
                        events,
                        usage,
                        limits,
                        f"{path}.epoch[{epoch}][{index}]",
                    )
                    if status != TerminalStatus.SUCCEEDED:
                        return status
            return TerminalStatus.SUCCEEDED
        if isinstance(node, RepeatNode):
            return self._run_repeated(
                node.body, node.budget, state, events, usage, limits, path
            )
        if isinstance(node, RetryNode):
            for attempt in range(node.budget + 1):
                try:
                    return self._run(
                        node.body,
                        state,
                        events,
                        usage,
                        limits,
                        f"{path}.attempt[{attempt}]",
                    )
                except RetryableFailure as exc:
                    if exc.code not in node.retry_on or attempt == node.budget:
                        raise
            raise AssertionError("retry loop is structurally bounded")
        if isinstance(node, ParallelNode):
            for index, branch in enumerate(node.branches):
                status = self._run(
                    branch, state, events, usage, limits, f"{path}.branches[{index}]"
                )
                if status != TerminalStatus.SUCCEEDED:
                    return status
            return TerminalStatus.SUCCEEDED
        if isinstance(node, CallNode):
            return self._run(
                node.body, state, events, usage, limits, f"{path}.call[{node.name}]"
            )
        raise ExecutionError(f"unsupported control node: {type(node).__name__}")

    def _run_sequence(
        self,
        nodes: Sequence[ControlNode],
        state: dict[str, Any],
        events: list[ExecutionEvent],
        usage: dict[str, int],
        limits: Any,
        path: str,
    ) -> TerminalStatus:
        for index, child in enumerate(nodes):
            status = self._run(
                child, state, events, usage, limits, f"{path}.nodes[{index}]"
            )
            if status != TerminalStatus.SUCCEEDED:
                return status
        return TerminalStatus.SUCCEEDED

    def _run_repeated(
        self,
        body: ControlNode,
        budget: int,
        state: dict[str, Any],
        events: list[ExecutionEvent],
        usage: dict[str, int],
        limits: Any,
        path: str,
    ) -> TerminalStatus:
        for index in range(budget):
            status = self._run(body, state, events, usage, limits, f"{path}[{index}]")
            if status != TerminalStatus.SUCCEEDED:
                return status
        return TerminalStatus.SUCCEEDED

    def _check_budget(self, usage: Mapping[str, int], limits: Any) -> None:
        if usage["llm"] > limits.llm_activations:
            raise ExecutionError("LLM activation budget exceeded")
        if usage["tools"] > limits.tool_calls:
            raise ExecutionError("tool-call budget exceeded")


def _resolve(state: Mapping[str, Any], path: str) -> Any:
    value: Any = state
    for segment in path.split("."):
        if not isinstance(value, Mapping) or segment not in value:
            raise ExecutionError(f"missing binding: {path}")
        value = value[segment]
    return value


def _snapshot(state: Mapping[str, Any], name: str, limit: int) -> list[Any]:
    value = _resolve(state, name)
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ExecutionError(f"snapshot {name!r} must be a sequence")
    if len(value) > limit:
        raise ExecutionError(f"snapshot {name!r} exceeds its admission bound")
    return list(value)


__all__ = [
    "DecisionHandler",
    "ExecutionError",
    "ExecutionEvent",
    "ExecutionResult",
    "OperationHandler",
    "ReferenceRuntime",
    "RetryableFailure",
]
