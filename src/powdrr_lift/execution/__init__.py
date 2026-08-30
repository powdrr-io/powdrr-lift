"""Typed execution-kernel building blocks."""

from powdrr_lift.execution.builtin_tools import (
    EnrichmentAdapter,
    IntrinsicRepositoryAdapter,
    ShellAdapter,
    builtin_tool_registry,
    invoke_intrinsic_capability,
    invoke_shell_capability,
)
from powdrr_lift.execution.capabilities import (
    CapabilityBroker,
    CapabilityDecision,
    CapabilityExceptionStore,
    CapabilityRequest,
    CapabilityResolution,
    CapabilityResolutionKind,
    FileCapabilityExceptionStore,
)
from powdrr_lift.execution.checkpoints import (
    Checkpoint,
    ContentAddressedCheckpointStore,
    DiagnosticResult,
    run_diagnostics,
)
from powdrr_lift.execution.compaction import (
    compact_execution_context,
    compatibility_diagnostic,
)
from powdrr_lift.execution.compile import compile_execution_plan
from powdrr_lift.execution.evidence import (
    EvidenceRequirement,
    FindingDisposition,
    PublishRequirements,
    ReadinessEvaluator,
    ReadinessReport,
    dispose_finding,
    evaluate_review_agreement,
    invalidate_evidence,
)
from powdrr_lift.execution.guidance import load_applicable_guidance
from powdrr_lift.execution.kernel import (
    ActionKernel,
    ActionLifecycleEvent,
    ActionLifecyclePhase,
)
from powdrr_lift.execution.personas import (
    HandoffValidation,
    PersonaPacket,
    PersonaRun,
    PersonaRunStatus,
    build_persona_packet,
    validate_handoff,
)
from powdrr_lift.execution.phases import (
    DEFAULT_PHASE_TRANSITIONS,
    PhaseController,
    PhaseTransitionDecision,
)
from powdrr_lift.execution.relationships import (
    RelationshipExpansion,
    action_can_complete,
    expand_execution_obligations,
    expand_obligations,
    explain_obligation,
    satisfy_execution_obligation,
    satisfy_obligation,
    unresolved_obligations,
)
from powdrr_lift.execution.shadow import ShadowExecutionRecorder
from powdrr_lift.execution.store import (
    ExecutionStateConflict,
    ExecutionStateStore,
    FileExecutionStateStore,
)
from powdrr_lift.execution.tools import (
    ToolAdapter,
    ToolContext,
    ToolRegistry,
    ToolResult,
    ToolValidationReport,
)

__all__ = [
    "DEFAULT_PHASE_TRANSITIONS",
    "ExecutionStateConflict",
    "ExecutionStateStore",
    "FileExecutionStateStore",
    "HandoffValidation",
    "PhaseController",
    "PhaseTransitionDecision",
    "PersonaPacket",
    "PersonaRun",
    "PersonaRunStatus",
    "ShadowExecutionRecorder",
    "build_persona_packet",
    "validate_handoff",
    "load_applicable_guidance",
    "ActionKernel",
    "ActionLifecycleEvent",
    "ActionLifecyclePhase",
    "RelationshipExpansion",
    "action_can_complete",
    "expand_obligations",
    "expand_execution_obligations",
    "explain_obligation",
    "satisfy_obligation",
    "satisfy_execution_obligation",
    "unresolved_obligations",
    "CapabilityBroker",
    "CapabilityDecision",
    "EnrichmentAdapter",
    "Checkpoint",
    "ContentAddressedCheckpointStore",
    "CapabilityExceptionStore",
    "CapabilityRequest",
    "CapabilityResolution",
    "CapabilityResolutionKind",
    "FileCapabilityExceptionStore",
    "IntrinsicRepositoryAdapter",
    "DiagnosticResult",
    "EvidenceRequirement",
    "FindingDisposition",
    "ToolAdapter",
    "ToolContext",
    "ToolRegistry",
    "ToolResult",
    "ToolValidationReport",
    "run_diagnostics",
    "ReadinessEvaluator",
    "ReadinessReport",
    "PublishRequirements",
    "dispose_finding",
    "compile_execution_plan",
    "compatibility_diagnostic",
    "compact_execution_context",
    "evaluate_review_agreement",
    "invalidate_evidence",
    "builtin_tool_registry",
    "invoke_intrinsic_capability",
    "invoke_shell_capability",
    "ShellAdapter",
]
