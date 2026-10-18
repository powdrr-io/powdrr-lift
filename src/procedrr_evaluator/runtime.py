"""Shared execution boundary for validated Procedrr processes."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from procedrr.catalog import ProcessCatalog, ProcessIdentity
from procedrr_evaluator.evaluator import (
    EvaluationError,
    EvaluationEvent,
    EvaluationResult,
    Evaluator,
    OperationExecutor,
)

if TYPE_CHECKING:
    from powdrr_lift.workrr.protocol import WorkflowLLMClient


class ProcessTerminalStatus(StrEnum):
    """Terminal outcome reported by a Procedrr process."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"
    SUSPENDED = "suspended"


@dataclass(frozen=True, slots=True)
class ProcessExecutionEvent:
    """One evaluator event bound to the exact process revision."""

    sequence: int
    identity: ProcessIdentity
    kind: str
    path: str
    data: Mapping[str, Any] = field(default_factory=dict)

    def to_data(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "process_id": self.identity.process_id,
            "process_fingerprint": self.identity.fingerprint,
            "kind": self.kind,
            "path": self.path,
            "data": dict(self.data),
        }


@dataclass(frozen=True, slots=True)
class ProcessExecutionResult:
    """Bindings, events, resource usage, and terminal status for one run."""

    identity: ProcessIdentity
    bindings: Mapping[str, Any]
    events: tuple[ProcessExecutionEvent, ...]
    terminal_status: ProcessTerminalStatus
    llm_activations: int
    tool_calls: int

    @property
    def event_cursor(self) -> int:
        return len(self.events)


class ProcedrrExecutionService:
    """Run catalogued processes using caller-authorized operation handlers."""

    def __init__(
        self,
        catalog: ProcessCatalog,
        llm: WorkflowLLMClient,
        operation_executor: OperationExecutor,
        *,
        judge_clients: Mapping[str, WorkflowLLMClient] | None = None,
        command_catalog: Any | None = None,
        event_sink: Callable[[ProcessExecutionEvent], None] | None = None,
    ) -> None:
        self.catalog = catalog
        self.llm = llm
        self.operation_executor = operation_executor
        self.judge_clients = dict(judge_clients or {})
        self.command_catalog = command_catalog
        self.event_sink = event_sink

    def execute(
        self,
        process_id: str,
        bindings: Mapping[str, Any] | None = None,
    ) -> ProcessExecutionResult:
        """Validate bindings and execute one exact catalog revision."""
        process = self.catalog.get(process_id)
        initial_bindings = process.validate_inputs(bindings or {})
        events: list[ProcessExecutionEvent] = []

        def record(event: EvaluationEvent) -> None:
            wrapped = ProcessExecutionEvent(
                sequence=len(events),
                identity=process.identity,
                kind=event.kind,
                path=event.path,
                data=dict(event.data),
            )
            events.append(wrapped)
            if self.event_sink is not None:
                self.event_sink(wrapped)

        evaluator = Evaluator(
            self.llm,
            self.operation_executor,
            process_directory=process.path.parent,
            judge_clients=self.judge_clients,
            command_catalog=self.command_catalog,
            event_sink=record,
        )
        try:
            result = evaluator.evaluate(process.document, initial_bindings)
        except EvaluationError:
            raise
        status = _terminal_status(result)
        return ProcessExecutionResult(
            identity=process.identity,
            bindings=result.bindings,
            events=tuple(events),
            terminal_status=status,
            llm_activations=result.llm_activations,
            tool_calls=result.tool_calls,
        )


def _terminal_status(result: EvaluationResult) -> ProcessTerminalStatus:
    terminal_events = [event for event in result.events if event.kind == "terminal"]
    if not terminal_events:
        return ProcessTerminalStatus.SUCCEEDED
    value = terminal_events[-1].data.get("status")
    try:
        return ProcessTerminalStatus(str(value))
    except ValueError as error:
        raise EvaluationError(
            f"unsupported Procedrr terminal status: {value!r}"
        ) from error


__all__ = [
    "ProcessExecutionEvent",
    "ProcessExecutionResult",
    "ProcessTerminalStatus",
    "ProcedrrExecutionService",
]
