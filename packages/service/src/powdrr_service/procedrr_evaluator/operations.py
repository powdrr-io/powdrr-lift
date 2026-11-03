"""Validated operation dispatch for service-owned Procedrr processes."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from powdrr_service.procedrr.command_catalog import CommandCatalog
from powdrr_service.procedrr_evaluator.evaluator import EvaluationError

OperationHandler = Callable[[Mapping[str, Any]], Any]


class ProcedrrOperationExecutor:
    """Validate declared operations and dispatch to caller-authorized handlers."""

    def __init__(
        self,
        handlers: Mapping[str, OperationHandler],
        *,
        command_catalog: CommandCatalog | None = None,
    ) -> None:
        self._handlers = dict(handlers)
        self._command_catalog = command_catalog

    def __call__(self, tool: str, parameters: Mapping[str, Any]) -> Any:
        handler = self._handlers.get(tool)
        if handler is None:
            raise EvaluationError(f"Procedrr requested unsupported operation {tool!r}.")
        if tool == "internal":
            self._validate_internal_command(parameters)
        return handler(parameters)

    def _validate_internal_command(self, parameters: Mapping[str, Any]) -> None:
        command = parameters.get("command")
        if not isinstance(command, list) or not command:
            raise EvaluationError(
                "Procedrr internal operation command must be a non-empty list."
            )
        name = command[0]
        if not isinstance(name, str):
            raise EvaluationError(
                "Procedrr internal operation command name must be a string."
            )
        if self._command_catalog is None:
            return
        spec = self._command_catalog.get(name)
        if spec is None:
            return
        try:
            spec.validate_input(
                {key: value for key, value in parameters.items() if key != "command"}
            )
        except ValueError as error:
            raise EvaluationError(str(error)) from error


__all__ = ["OperationHandler", "ProcedrrOperationExecutor"]
