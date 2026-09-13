"""Provider-independent protocol for proposing agent actions."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from powdrr_lift.contracts import AgentInput, AgentProposal


class AgentProposalError(ValueError):
    """A provider response could not be represented as an agent proposal."""


class WorkflowLLMClient(Protocol):
    """Minimal provider surface used by every agent runner."""

    def complete_json(self, messages: list[dict[str, str]]) -> dict[str, Any]: ...


class SchemaAwareWorkflowLLMClient(Protocol):
    """Optional provider surface for strict response schemas."""

    def complete_json(
        self,
        messages: list[dict[str, str]],
        *,
        response_schema: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]: ...


class AgentClient(Protocol):
    """Minimal model-facing interface used by the future generic runner."""

    def propose(self, agent_input: AgentInput) -> AgentProposal:
        """Return an untrusted proposal for the kernel to validate."""
