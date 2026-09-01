"""Shared Powdrr error contracts.

These exceptions live in a dependency-light module so capability adapters can
report agent-correctable failures without importing the workflow loop.
"""

from __future__ import annotations

from collections.abc import Mapping


class PowdrrExecutionError(RuntimeError):
    """An action failed in Powdrr and should be corrected by the agent."""

    def __init__(
        self,
        message: str,
        *,
        error_code: str = "action_error",
        action_kind: str | None = None,
        remediation: str | None = None,
        details: Mapping[str, str] | None = None,
        cause_error: Exception | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.action_kind = action_kind
        self.remediation = remediation
        self.details = dict(details or {})
        self.cause_error = cause_error


class AgentCorrectableError(PowdrrExecutionError):
    """An action can be repaired by returning a corrected request."""


class ProviderExecutionError(PowdrrExecutionError):
    """The model/provider boundary failed; it is not an action correction."""


class ExecutionCancelled(PowdrrExecutionError):
    """Execution was deliberately cancelled by its owner or user."""


class PersistenceCorruptionError(PowdrrExecutionError):
    """Durable execution state is unreadable or internally inconsistent."""


class ProgrammerInvariantError(PowdrrExecutionError):
    """A programming invariant failed and must not be sent back to the model."""
