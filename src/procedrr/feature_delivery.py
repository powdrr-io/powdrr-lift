"""A complete, single-decision feature-delivery process.

This is the executable counterpart of the repository's
``specify-a-feature``, ``start-implementing-feature``, and
``execute-proposed-pr`` flows.  Operations are deliberately named registry
keys: the host supplies their implementations, while procedrr owns ordering,
branching, admission, and termination.
"""

from __future__ import annotations

from procedrr.model import (
    CallNode,
    DecisionContract,
    DecisionKind,
    ForEachNode,
    JudgeNode,
    OperationNode,
    ResourceLimits,
    SequenceNode,
    SnapshotSpec,
    TerminalNode,
    TerminalStatus,
    WorkflowDefinition,
)


def _judge(
    name: str,
    question: str,
    subject: str,
    kind: DecisionKind = DecisionKind.CLASSIFY_ONE,
) -> JudgeNode:
    return JudgeNode(
        DecisionContract(
            kind=kind,
            question=question,
            subject=subject,
            output_name=name,
            output_schema={"type": "string"},
            validator=f"validate_{name}",
            transport_action_const="decision_result",
        )
    )


def _op(name: str, output: str | None = None) -> OperationNode:
    return OperationNode(name=name, output_name=output)


def specify_a_feature() -> CallNode:
    """Capture, interview, validate, and publish one feature specification."""
    return CallNode(
        "specify-a-feature",
        SequenceNode(
            (
                _op("capture_user_request", "request"),
                _op("extract_feature_name_candidates", "feature_name_candidates"),
                _judge(
                    "feature_name",
                    "What stable identifier names this feature?",
                    "feature_name_candidates",
                    DecisionKind.CONSTRUCT_ONE,
                ),
                _op("gather_repository_context", "repository_context"),
                _judge(
                    "feature_intent",
                    "What is the one intended feature behavior?",
                    "request",
                    DecisionKind.CONSTRUCT_ONE,
                ),
                _op("gather_design_interview_categories", "interview_categories"),
                ForEachNode(
                    SnapshotSpec(
                        "interview_categories",
                        {"type": "string"},
                        "interview_categories",
                        12,
                    ),
                    "category",
                    SequenceNode(
                        (
                            _op("gather_category_evidence", "category_evidence"),
                            _judge(
                                "category_decision",
                                "What one requirement follows from this category "
                                "evidence?",
                                "category_evidence",
                                DecisionKind.CONSTRUCT_ONE,
                            ),
                            _op("store_category_input"),
                        )
                    ),
                ),
                _op("construct_feature_specification", "feature_specification"),
                _op("validate_feature_specification"),
                _op("stage_specification_changes"),
                _op("create_specification_pr", "specification_pr"),
            )
        ),
    )


def start_implementing_feature() -> CallNode:
    """Turn the accepted specification into bounded proposed-PR workflows."""
    return CallNode(
        "start-implementing-feature",
        SequenceNode(
            (
                _op("capture_accepted_specification", "accepted_specification"),
                _op("gather_implementation_context", "implementation_context"),
                _judge(
                    "implementation_shape",
                    "What one decomposition best covers this feature?",
                    "accepted_specification",
                    DecisionKind.CONSTRUCT_ONE,
                ),
                _op("construct_change_units", "change_units"),
                ForEachNode(
                    SnapshotSpec(
                        "change_units", {"type": "object"}, "change_units", 16
                    ),
                    "change_unit",
                    SequenceNode(
                        (
                            _judge(
                                "proposed_pr",
                                "What one proposed PR implements this change unit?",
                                "change_unit",
                                DecisionKind.CONSTRUCT_ONE,
                            ),
                            _op("validate_proposed_pr"),
                            _op("persist_proposed_pr_workflow"),
                        )
                    ),
                ),
                _op("validate_proposed_pr_dependency_graph"),
                _op("publish_implementation_plan_pr", "plan_pr"),
            )
        ),
    )


def execute_proposed_pr() -> CallNode:
    """Execute one proposed PR with deterministic checks around each decision."""
    return CallNode(
        "execute-proposed-pr",
        SequenceNode(
            (
                _op("capture_proposed_pr_context", "pr_context"),
                _op("inspect_target_structure", "target_structure"),
                _op("inspect_target_symbols", "target_symbols"),
                _op("load_change_units", "pr_change_units"),
                ForEachNode(
                    SnapshotSpec(
                        "pr_change_units", {"type": "object"}, "pr_change_units", 32
                    ),
                    "change_unit",
                    SequenceNode(
                        (
                            _judge(
                                "change_effect",
                                "What one authoritative effect does this change "
                                "unit require?",
                                "change_unit",
                                DecisionKind.CONSTRUCT_ONE,
                            ),
                            _op("apply_change_unit"),
                            _op("run_targeted_validation"),
                            _judge(
                                "validation_finding",
                                "Does this one validation result identify a defect?",
                                "targeted_validation",
                            ),
                            _op("record_change_unit_receipt"),
                        )
                    ),
                ),
                _op("run_full_validation"),
                _op("run_invariant_checks"),
                _op("run_security_checks"),
                _op("review_diff"),
                _judge(
                    "review_disposition",
                    "Is this one reviewed diff ready for publication?",
                    "review_diff",
                ),
                _op("stage_proposed_pr_changes"),
                _op("publish_proposed_pr"),
            )
        ),
    )


def feature_delivery_process(*, max_proposed_prs: int = 16) -> WorkflowDefinition:
    """Build the full lifecycle with an explicit finite PR admission bound."""
    if max_proposed_prs <= 0:
        raise ValueError("max_proposed_prs must be positive")
    execute = execute_proposed_pr()
    body = SequenceNode(
        (
            specify_a_feature(),
            start_implementing_feature(),
            ForEachNode(
                SnapshotSpec(
                    "ready_proposed_prs",
                    {"type": "object"},
                    "ready_proposed_prs",
                    max_proposed_prs,
                ),
                "proposed_pr",
                execute,
            ),
            _op("run_feature_acceptance"),
            _op("verify_feature_invariants"),
            _op("verify_feature_security"),
            TerminalNode(TerminalStatus.SUCCEEDED),
        )
    )
    return WorkflowDefinition(
        "feature-delivery",
        body,
        # The admission bound is part of the process contract; the aggregate
        # budget leaves room for the worst-case per-PR decision sequence.
        ResourceLimits(llm_activations=2048, tool_calls=4096, max_epochs=3),
    )


__all__ = [
    "execute_proposed_pr",
    "feature_delivery_process",
    "specify_a_feature",
    "start_implementing_feature",
]
