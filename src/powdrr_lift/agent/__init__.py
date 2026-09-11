"""Agent proposal interfaces and provider adapters."""

from powdrr_lift.agent.actions import (
    WorkflowAction,
    WorkflowEdit,
    WorkflowFileEdits,
    WorkflowYamlOperation,
)
from powdrr_lift.agent.protocol import AgentClient, AgentProposalError
from powdrr_lift.agent.repair import (
    RepairContext,
    RepairDirective,
    RepairExhaustionReport,
    RepairFailure,
    RepairFailureClass,
    RepairPolicy,
    RepairStage,
    classify_repair_failure,
)
from powdrr_lift.agent.runner import ProposalKernel, run_proposal_round

__all__ = [
    "AgentClient",
    "AgentProposalError",
    "WorkflowAction",
    "WorkflowEdit",
    "WorkflowFileEdits",
    "WorkflowYamlOperation",
    "ProposalKernel",
    "RepairContext",
    "RepairDirective",
    "RepairExhaustionReport",
    "RepairFailure",
    "RepairFailureClass",
    "RepairPolicy",
    "RepairStage",
    "classify_repair_failure",
    "run_proposal_round",
]
