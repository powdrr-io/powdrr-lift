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
    ReferenceRuntime,
    ResourceLimits,
    SequenceNode,
    SnapshotSpec,
    TerminalNode,
    TerminalStatus,
    WorkflowDefinition,
    compile_workflow,
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


def test_reference_runtime_owns_transition_and_binds_one_decision() -> None:
    workflow = compile_workflow(
        WorkflowDefinition(
            "run",
            SequenceNode(
                (JudgeNode(decision()), TerminalNode(TerminalStatus.SUCCEEDED))
            ),
            ResourceLimits(llm_activations=1),
        )
    )
    runtime = ReferenceRuntime(
        decision_handler=lambda _node, _state: "a",
        operation_handler=lambda _node, _state: None,
    )
    result = runtime.execute(workflow)
    assert result.status == TerminalStatus.SUCCEEDED
    assert result.values["choice"] == "a"
    assert [event.kind for event in result.events] == ["decision", "terminal"]


def test_runtime_rejects_snapshot_above_declared_bound() -> None:
    workflow = compile_workflow(
        WorkflowDefinition(
            "bounded",
            ForEachNode(
                SnapshotSpec("items", {"type": "string"}, "items", 1),
                "item",
                TerminalNode(TerminalStatus.SUCCEEDED),
            ),
        )
    )
    result = ReferenceRuntime(
        decision_handler=lambda _node, _state: None,
        operation_handler=lambda _node, _state: None,
    ).execute(workflow, {"items": ["a", "b"]})
    assert result.status == TerminalStatus.FAILED
    assert "exceeds" in (result.error or "")
