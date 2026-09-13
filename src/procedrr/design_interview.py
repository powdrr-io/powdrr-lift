"""Concrete procedrr representation of the repository's ``design-interview`` skill."""

from __future__ import annotations

from procedrr.model import (
    DecisionContract,
    DecisionKind,
    JudgeNode,
    OperationNode,
    ResourceLimits,
    SequenceNode,
    SnapshotSpec,
    TerminalNode,
    TerminalStatus,
    WorkflowDefinition,
    WorklistNode,
)

_CATEGORIES = (
    "requirements",
    "approach",
    "entities",
    "entity-relationships",
    "invariants",
    "guidance",
    "features",
    "human-decisions",
    "intent",
    "intents",
    "acceptance_criteria",
    "expected_tests",
    "required_test_cases",
    "expected_outcomes",
    "non_goals",
    "risks",
    "decisions",
    "proposed_prs",
    "modules",
    "tools",
)

_EDIT_SCHEMA = {
    "type": "object",
    "required": ["added", "deleted"],
    "additionalProperties": False,
    "properties": {
        "added": {"type": "array", "items": {"type": "object"}},
        "deleted": {"type": "array", "items": {"type": "object", "required": ["id"]}},
    },
}


def _gather(category: str) -> SequenceNode:
    output = f"{category.replace('-', '_')}_edits"
    context = f"{category.replace('-', '_')}_context"
    return SequenceNode(
        (
            OperationNode(
                "gather_context",
                {"types": [category]},
                context,
            ),
            JudgeNode(
                DecisionContract(
                    DecisionKind.CONSTRUCT_ONE,
                    f"What proposed {category} edits follow from this exact "
                    "gathered context?",
                    context,
                    output,
                    _EDIT_SCHEMA,
                    "validate_proposal_edits",
                    "decision_result",
                )
            ),
        )
    )


def design_interview() -> WorkflowDefinition:
    """Return a fully bounded, tool-explicit design-interview workflow."""
    steps: list = [
        OperationNode(
            "internal",
            {
                "command": [
                    "powdrr-lift",
                    "design-interview-input",
                    "--work-item-name",
                    "${work_item_name}",
                ]
            },
            "interview_input_template",
        )
    ]
    for category in _CATEGORIES:
        steps.append(_gather(category))
    steps.extend(
        (
            OperationNode(
                "edit",
                {
                    "file_path": (
                        "docs/proposals/${work_item_name}/design-interview-input.json"
                    ),
                    "source": "all_category_edits",
                },
                "interview_input",
            ),
            OperationNode(
                "internal",
                {
                    "command": [
                        "powdrr-lift",
                        "feature-pr-specification",
                        "--work-item-name",
                        "${work_item_name}",
                        "--interview-input",
                        "docs/proposals/${work_item_name}/design-interview-input.json",
                    ],
                    "interview_input": "interview_input",
                },
                "proposal_template",
            ),
            OperationNode(
                "internal",
                {
                    "command": [
                        "powdrr-lift",
                        "evaluate",
                        "docs/proposals/${work_item_name}/feature-pr-specification.yaml",
                    ]
                },
                "proposal_evaluation",
            ),
            WorklistNode(
                SnapshotSpec(
                    "proposal_issues", {"type": "object"}, "proposal_issues", 64
                ),
                "proposal_issue",
                SequenceNode(
                    (
                        JudgeNode(
                            DecisionContract(
                                DecisionKind.CONSTRUCT_ONE,
                                "What one YAML edit repairs this exact evaluator "
                                "issue?",
                                "proposal_issue",
                                "repair_edit",
                                {"type": "object", "required": ["path", "edits"]},
                                "validate_yaml_edit",
                                "decision_result",
                            )
                        ),
                        OperationNode(
                            "yaml_edit",
                            {
                                "file_path": (
                                    "docs/proposals/${work_item_name}/"
                                    "feature-pr-specification.yaml"
                                ),
                                "edit": "repair_edit",
                            },
                        ),
                    )
                ),
                max_admissions=64,
                max_epochs=3,
            ),
            OperationNode(
                "internal",
                {
                    "command": [
                        "powdrr-lift",
                        "evaluate",
                        "docs/proposals/${work_item_name}/feature-pr-specification.yaml",
                    ]
                },
                "final_proposal_evaluation",
            ),
            TerminalNode(TerminalStatus.SUCCEEDED),
        )
    )
    return WorkflowDefinition(
        "design-interview",
        SequenceNode(steps),
        ResourceLimits(llm_activations=1024, tool_calls=2048, max_epochs=3),
    )


__all__ = ["design_interview"]
