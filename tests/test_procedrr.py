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
    apply_fragment_edit,
    apply_json_edits,
    compile_workflow,
    parse_and_validate,
    render_document,
    set_value,
    start_fragment,
    validate_single_decision,
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
        prompt_system="Return only the declared answer.",
        instructions=("Use only the supplied subject.",),
        context_bindings=("context",),
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


def test_decision_contract_requires_prompt_context_rules() -> None:
    with pytest.raises(ValueError, match="prompt_system"):
        DecisionContract(
            DecisionKind.CLASSIFY_ONE,
            "Choose one",
            "context",
            "choice",
            {"type": "string"},
            "string",
            "decision_result",
        )


def test_parser_rejects_unknown_tool_and_validator_references() -> None:
    with pytest.raises(Exception, match="unknown tool"):
        parse_and_validate(
            "name: bad\nsteps:\n  - operation: {tool: imaginary, bind: x}\n"
        )


def test_parser_requires_explicit_collection_for_loop_outputs() -> None:
    with pytest.raises(Exception, match="collect binding"):
        parse_and_validate(
            "name: bad\nsteps:\n"
            "  - for_each:\n"
            "      item_binding: item\n"
            "      body:\n"
            "        - operation: {tool: internal, command: [echo], bind: output}\n"
        )
    with pytest.raises(Exception, match="unknown validator"):
        parse_and_validate(
            "name: bad\nsteps:\n  - judge:\n"
            "      question: q\n      subject: x\n"
            "      prompt_system: s\n      instructions: [i]\n"
            "      context: [x]\n      output: {name: y, schema: {type: string}}\n"
            "      validator: imaginary\n"
        )


def test_parser_validates_attempt_recovery_references_and_body() -> None:
    source = """name: recovery
inputs: [{name: ready}]
steps:
  - attempt:
      id: work
      max_attempts: 2
      body:
        - gate:
            subject: ready
            equals: true
            on_failure: {retry: {max_attempts: 1, on_exhausted: failed}}
      on_failure: {recovery: repair, resume: work}
recoveries:
  repair:
    steps:
      - operation: {tool: internal, command: [echo], bind: ready}
"""
    document = parse_and_validate(source)
    assert document["recoveries"]["repair"]["steps"]

    with pytest.raises(Exception, match="unknown recovery"):
        parse_and_validate(source.replace("recovery: repair", "recovery: missing"))


def test_single_decision_verifier_flags_multi_action_judges() -> None:
    diagnostics = validate_single_decision(
        {
            "steps": [
                {
                    "judge": {
                        "output": {
                            "schema": {
                                "type": "object",
                                "required": ["actions"],
                                "properties": {
                                    "actions": {
                                        "type": "array",
                                        "items": {"type": "object"},
                                    }
                                },
                            }
                        }
                    }
                }
            ]
        }
    )
    assert any("bounded loop" in diagnostic.message for diagnostic in diagnostics)


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
        "name: demo\nsteps:\n"
        "  - operation: {tool: internal, command: [echo], bind: inspected}\n"
    )
    changed = set_value(document, ("name",), "edited")
    extended = append_step(changed, ("steps",), {"terminal": "succeeded"})
    assert document["name"] == "demo"
    assert extended["name"] == "edited"
    assert len(extended["steps"]) == 2


def test_json_is_a_first_class_source_format() -> None:
    document = parse_and_validate(
        '{"name":"demo","steps":[{"terminal":"succeeded"}]}',
        source_format="json",
    )

    assert document["steps"] == [{"terminal": "succeeded"}]
    assert (
        parse_and_validate(
            render_document(document, source_format="json"), source_format="json"
        )
        == document
    )

    with pytest.raises(Exception, match=r"line 1, column 49"):
        parse_and_validate(
            '{"name":"demo","steps":[{"terminal":"succeeded",}]}',
            source_format="json",
        )


def test_diagnostics_expose_json_pointer_paths() -> None:
    diagnostics = validate_single_decision(
        {
            "steps": [
                {
                    "judge": {
                        "output": {
                            "schema": {
                                "type": "array",
                                "items": {"type": "object"},
                            }
                        }
                    }
                }
            ]
        }
    )

    assert diagnostics[0].json_pointer == "/steps/0/judge/output/schema"
    assert diagnostics[0].to_data()["json_pointer"] == diagnostics[0].json_pointer


def test_json_pointer_edits_are_persistent_and_support_arrays() -> None:
    document = {"name": "demo", "steps": [{"terminal": "failed"}]}

    changed = apply_json_edits(
        document,
        [
            {"op": "replace", "path": "/steps/0/terminal", "value": "succeeded"},
            {
                "op": "add",
                "path": "/steps/-",
                "value": {"terminal": "succeeded"},
            },
            {"op": "remove", "path": "/steps/0"},
        ],
    )

    assert document["steps"] == [{"terminal": "failed"}]
    assert changed["steps"] == [{"terminal": "succeeded"}]

    with pytest.raises(ValueError, match="value is required"):
        apply_json_edits(document, [{"op": "replace", "path": "/name"}])


def test_fragment_builder_accepts_one_step_and_rejects_repeated_invalid_edit() -> None:
    state = start_fragment(
        name="generated",
        available_bindings=["request"],
        allowed_tools=["read_document"],
        max_steps=3,
    )
    invalid = {
        "op": "add",
        "path": "/steps/-",
        "value": {"operation": {"tool": "edit", "bind": "changed"}},
    }

    rejected = apply_fragment_edit(state, invalid)
    repeated = apply_fragment_edit(rejected, invalid)
    accepted = apply_fragment_edit(
        repeated,
        {
            "op": "add",
            "path": "/steps/-",
            "value": {
                "operation": {
                    "tool": "read_document",
                    "parameters": {"file_path": "hello.py"},
                    "bind": "source",
                }
            },
        },
    )

    assert rejected["accepted"] is False
    assert repeated["diagnostic"]["code"] == "repeated_fragment_edit"
    assert accepted["accepted"] is True
    assert len(accepted["fragment"]["steps"]) == 1


def test_fragment_builder_rejects_repeated_accepted_step() -> None:
    state = start_fragment(
        name="generated", allowed_tools=["read_document"], max_steps=3
    )
    step = {
        "op": "add",
        "path": "/steps/-",
        "value": {
            "operation": {
                "tool": "read_document",
                "parameters": {"file_path": "hello.py"},
                "bind": "source",
            }
        },
    }
    accepted = apply_fragment_edit(state, step)
    repeated = apply_fragment_edit(accepted, step)
    assert repeated["accepted"] is True
    assert repeated["done"] is True
    assert repeated["diagnostic"]["code"] == "fragment_complete_after_replay"
    assert repeated["fragment"]["steps"][-1] == {"terminal": "succeeded"}


def test_fragment_builder_rejects_edit_not_grounded_in_source_context() -> None:
    state = start_fragment(
        name="generated",
        allowed_tools=["edit"],
        max_steps=2,
    )

    result = apply_fragment_edit(
        state,
        {
            "op": "add",
            "path": "/steps/-",
            "value": {
                "operation": {
                    "tool": "edit",
                    "parameters": {
                        "file_path": "hello.py",
                        "edits": [
                            {
                                "old_text": "invented source",
                                "new_text": "replacement",
                            }
                        ],
                    },
                    "bind": "changed",
                }
            },
        },
        evidence={"source": 'print("Hello, World")\n'},
    )

    assert result["accepted"] is False
    assert result["diagnostic"]["code"] == "ungrounded_edit"


def test_checked_in_generate_fragment_definition_is_single_decision() -> None:
    from pathlib import Path

    source = Path("docs/procedrr/skill-definitions/generate-fragment.yaml").read_text()
    document = parse_and_validate(source)

    assert document["name"] == "generate-fragment"
    assert validate_single_decision(document) == ()


def test_checked_in_design_interview_definition_parses() -> None:
    from pathlib import Path

    source = Path("docs/procedrr/skill-definitions/design-interview.yaml").read_text()
    document = parse_and_validate(source)
    assert document["name"] == "design-interview"


def test_checked_in_implement_feature_has_bounded_reviews_and_repairs() -> None:
    from pathlib import Path

    source = Path("docs/procedrr/skill-definitions/implement-feature.yaml").read_text()
    document = parse_and_validate(source)

    assert document["name"] == "implement-feature"
    assert validate_single_decision(document) == ()
    assert set(document["recoveries"]) == {
        "completeness-repair",
        "intent-repair",
        "scope-repair",
        "worker-repair",
    }
    step_text = str(document["steps"])
    assert "specification-completeness-review" in step_text
    assert "change-scope-review" in step_text
    assert "Does this sentence state a feature requirement" in step_text
    assert "Is this sentence's requirement explicitly reflected" in step_text
    assert (
        "Repair this design consequence so it preserves the instruction's intent"
        in step_text
    )
    assert "Treat statements that something is missing" in step_text
    assert "repaired_design_decisions" in step_text
    review_schemas = [
        step["judge"]["output"]["schema"]
        for step in document["steps"]
        if isinstance(step, dict) and "judge" in step
    ]
    assert [set(schema["properties"]) for schema in review_schemas] == [
        {"verdict"},
        {"verdict"},
    ]
