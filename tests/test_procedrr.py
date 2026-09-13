import pytest

from procedrr import (
    CallNode,
    CompilationError,
    DecisionContract,
    DecisionKind,
    ForEachNode,
    Guarantee,
    JudgeNode,
    MatchCase,
    MatchNode,
    OperationNode,
    SequenceNode,
    SnapshotSpec,
    TerminalNode,
    TerminalStatus,
    WorkflowDefinition,
    append_step,
    compile_workflow,
    design_interview,
    parse_and_validate,
    set_value,
)


def decision(name: str = "choice") -> DecisionContract:
    return DecisionContract(
        DecisionKind.CLASSIFY_ONE,
        "Which option applies?",
        "the feature context",
        name,
        {"type": "string", "enum": ["a", "b"]},
        "enum:a,b",
        "decision_result",
    )


def test_single_decision_rejects_model_owned_transition() -> None:
    with pytest.raises(ValueError, match="transport action"):
        DecisionContract(
            DecisionKind.CLASSIFY_ONE,
            "Choose one",
            "context",
            "choice",
            {"type": "string"},
            "string",
            "next_step",
        )


def test_compiler_proves_bounded_sequence_and_counts_repairs() -> None:
    workflow = WorkflowDefinition(
        "feature",
        SequenceNode((JudgeNode(decision()), OperationNode("write_file"))),
    )
    compiled = compile_workflow(workflow)
    assert compiled.max_llm_activations == 1
    assert compiled.max_tool_calls == 1
    assert compiled.certificate.status(Guarantee.DECISION_SAFE) == "proven"


def test_compiler_counts_data_driven_fanout_and_retries() -> None:
    body = ForEachNode(
        snapshot=SnapshotSpec("files", {"type": "string"}, "files", 3),
        item_binding="file",
        body=CallNode("edit", JudgeNode(decision("edit"))),
        item_retry_budget=1,
    )
    compiled = compile_workflow(WorkflowDefinition("fanout", body))
    assert compiled.max_llm_activations == 6


def test_non_exhaustive_match_is_rejected() -> None:
    workflow = WorkflowDefinition(
        "match",
        MatchNode("choice", (MatchCase("a", TerminalNode(TerminalStatus.SUCCEEDED)),)),
    )
    with pytest.raises(CompilationError, match="non_exhaustive_match"):
        compile_workflow(workflow)


def test_parser_validates_declarative_steps_and_editor_is_persistent() -> None:
    document = parse_and_validate(
        "name: demo\nsteps:\n  - operation: {name: inspect}\n"
    )
    changed = set_value(document, ("name",), "edited")
    extended = append_step(changed, ("steps",), {"terminal": "succeeded"})
    assert document["name"] == "demo"
    assert extended["name"] == "edited"
    assert len(extended["steps"]) == 2


def test_checked_in_design_interview_definition_parses() -> None:
    from pathlib import Path

    source = Path("docs/procedrr/skill-definitions/design-interview.yaml").read_text()
    document = parse_and_validate(source)
    assert document["name"] == "design-interview"


def test_design_interview_uses_real_context_types_and_bounded_repairs() -> None:
    compiled = compile_workflow(design_interview())
    data = compiled.definition.to_data()
    encoded = str(data)
    for category in (
        "requirements",
        "entity-relationships",
        "acceptance_criteria",
        "tools",
    ):
        assert category in encoded
    assert compiled.max_llm_activations == 212
    assert compiled.max_tool_calls == 217
