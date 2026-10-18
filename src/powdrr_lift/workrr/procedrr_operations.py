"""Shared authorized operation dispatch for Procedrr callers."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from powdrr_lift.errors import PowdrrExecutionError
from procedrr.command_catalog import CommandCatalog

OperationHandler = Callable[[Mapping[str, Any]], Any]


class WorkrrOperationExecutor:
    """Validate Procedrr operations and dispatch through caller-owned handlers.

    Each caller supplies the handlers that its existing authorization boundary
    permits. The declaration can select among those handlers, but cannot add a
    handler or broaden its capabilities.
    """

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
            raise PowdrrExecutionError(
                f"Procedrr requested unsupported operation {tool!r}."
            )
        if tool == "internal":
            self._validate_internal_command(parameters)
        return handler(parameters)

    def _validate_internal_command(self, parameters: Mapping[str, Any]) -> None:
        command = parameters.get("command")
        if not isinstance(command, list) or not command:
            raise PowdrrExecutionError(
                "Procedrr internal operation command must be a non-empty list."
            )
        name = command[0]
        if not isinstance(name, str):
            raise PowdrrExecutionError(
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
            raise PowdrrExecutionError(str(error)) from error


__all__ = ["OperationHandler", "WorkrrOperationExecutor"]
