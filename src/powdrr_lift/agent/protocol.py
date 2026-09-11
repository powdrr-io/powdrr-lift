"""Provider-independent protocol for proposing agent actions."""

from __future__ import annotations

from typing import Protocol

from powdrr_lift.contracts import AgentInput, AgentProposal


class AgentProposalError(ValueError):
    """A provider response could not be represented as an agent proposal."""


class AgentClient(Protocol):
    """Minimal model-facing interface used by the future generic runner."""

    def propose(self, agent_input: AgentInput) -> AgentProposal:
        """Return an untrusted proposal for the kernel to validate."""
