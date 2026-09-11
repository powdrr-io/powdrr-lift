"""Agent proposal interfaces and provider adapters."""

from powdrr_lift.agent.actions import (
    WorkflowAction,
    WorkflowEdit,
    WorkflowFileEdits,
    WorkflowYamlOperation,
)
from powdrr_lift.agent.loop import (
    WorkflowActionObservation,
    WorkflowActionOutcome,
    WorkflowActionProgressStrategy,
    WorkflowActionRequest,
    WorkflowExecutionObserver,
    WorkflowExecutionStrategy,
)
from powdrr_lift.agent.progress import (
    ProgressDecision,
    WorkflowExecutionController,
    no_progress_feedback,
)
from powdrr_lift.agent.protocol import (
    AgentClient,
    AgentProposalError,
    SchemaAwareWorkflowLLMClient,
    WorkflowLLMClient,
)
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
    "SchemaAwareWorkflowLLMClient",
    "WorkflowAction",
    "WorkflowActionObservation",
    "WorkflowActionOutcome",
    "WorkflowActionProgressStrategy",
    "WorkflowActionRequest",
    "WorkflowEdit",
    "WorkflowFileEdits",
    "WorkflowLLMClient",
    "WorkflowYamlOperation",
    "ProposalKernel",
    "ProgressDecision",
    "RepairContext",
    "RepairDirective",
    "RepairExhaustionReport",
    "RepairFailure",
    "RepairFailureClass",
    "RepairPolicy",
    "RepairStage",
    "classify_repair_failure",
    "no_progress_feedback",
    "run_proposal_round",
    "WorkflowExecutionController",
    "WorkflowExecutionObserver",
    "WorkflowExecutionStrategy",
]
