"""Agent proposal interfaces and provider adapters."""

from powdrr_lift.workrr.actions import (
    WorkflowAction,
    WorkflowEdit,
    WorkflowFileEdits,
    WorkflowYamlOperation,
)
from powdrr_lift.workrr.context import WorkflowContext
from powdrr_lift.workrr.exchanges import ExchangeRecordingClient
from powdrr_lift.workrr.loop import (
    WorkflowActionObservation,
    WorkflowActionOutcome,
    WorkflowActionProgressStrategy,
    WorkflowActionRequest,
    WorkflowExecutionObserver,
    WorkflowExecutionStrategy,
)
from powdrr_lift.workrr.progress import (
    ProgressDecision,
    WorkflowExecutionController,
    no_progress_feedback,
)
from powdrr_lift.workrr.protocol import (
    AgentClient,
    AgentProposalError,
    SchemaAwareWorkflowLLMClient,
    WorkflowLLMClient,
)
from powdrr_lift.workrr.provider_config import (
    LLMModelLimits,
    LLMModelMapping,
    LLMProviderDefinition,
    LLMProviderRole,
    LLMProviderRoles,
)
from powdrr_lift.workrr.repair import (
    RepairContext,
    RepairDirective,
    RepairExhaustionReport,
    RepairFailure,
    RepairFailureClass,
    RepairPolicy,
    RepairStage,
    classify_repair_failure,
)
from powdrr_lift.workrr.runner import ProposalKernel, run_proposal_round

__all__ = [
    "AgentClient",
    "AgentProposalError",
    "ExchangeRecordingClient",
    "LLMModelLimits",
    "LLMModelMapping",
    "LLMProviderDefinition",
    "LLMProviderRole",
    "LLMProviderRoles",
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
    "WorkflowContext",
]
