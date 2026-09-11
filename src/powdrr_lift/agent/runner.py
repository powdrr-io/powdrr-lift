"""Provider-independent proposal loop for the agent boundary."""

from __future__ import annotations

from typing import Protocol

from powdrr_lift.agent.protocol import AgentClient
from powdrr_lift.contracts import AgentInput, AgentProposal, KernelResult


class ProposalKernel(Protocol):
    """Execution boundary that validates and processes untrusted proposals."""

    def process_proposal(self, proposal: AgentProposal) -> KernelResult:
        """Validate the proposal and commit at most one kernel operation."""


def run_proposal_round(
    client: AgentClient,
    kernel: ProposalKernel,
    agent_input: AgentInput,
) -> KernelResult:
    """Run one model proposal through the kernel.

    The client cannot receive a kernel or mutate execution state. The kernel
    receives only the returned proposal and remains the sole owner of
    authorization, operation execution, and transition commitment.
    """

    proposal = client.propose(agent_input)
    return kernel.process_proposal(proposal)
