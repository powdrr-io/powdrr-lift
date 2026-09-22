import pytest

from procedrr import (
    CallNode,
    CommandCatalog,
    CommandSpec,
    CompilationError,
    DecisionContract,
    DecisionKind,
    ForEachNode,
    Guarantee,
    JudgeNode,
    MatchCase,
    MatchNode,
    OperationNode,
    PromptRule,
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
    parse_document,
    render_document,
    set_value,
    start_fragment,
    validate_document,
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


def test_prompt_rule_requires_declared_context_and_supports_replacement() -> None:
    contract = decision()
    replaced = DecisionContract(
        contract.kind,
        contract.question,
        contract.subject,
        contract.output_name,
        contract.output_schema,
        contract.validator,
        contract.transport_action_const,
        prompt_system=contract.prompt_system,
        instructions=contract.instructions,
        context_bindings=contract.context_bindings,
        prompt_rules=(
            PromptRule(
                "context",
                "equals",
                "special",
                ("Use the special contract.",),
                mode="replace",
            ),
        ),
    )

    assert replaced.to_data()["prompt_rules"][0]["mode"] == "replace"
    with pytest.raises(ValueError, match="unsupported"):
        PromptRule("context", "greater_than", 2, ("Never used.",))


def test_parser_rejects_unknown_tool_and_validator_references() -> None:
    with pytest.raises(Exception, match="unknown tool"):
        parse_and_validate(
            "name: bad\nsteps:\n  - operation: {tool: imaginary, bind: x}\n"
        )


def test_parser_validates_internal_commands_against_catalog() -> None:
    catalog = CommandCatalog(
        (
            CommandSpec(
                "known",
                {
                    "type": "object",
                    "required": ["value"],
                    "properties": {"value": {"type": "string"}},
                    "additionalProperties": False,
                },
                {"type": "object"},
            ),
        )
    )
    with pytest.raises(Exception, match="unknown parameters"):
        parse_and_validate(
            "name: bad\nsteps:\n"
            "  - operation:\n"
            "      tool: internal\n"
            "      command: [known]\n"
            "      parameters: {wrong: value}\n",
            command_catalog=catalog,
        )
    with pytest.raises(Exception, match="unknown cataloged internal command"):
        parse_and_validate(
            "name: bad\nsteps:\n  - operation: {tool: internal, command: [missing]}\n",
            command_catalog=catalog,
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


def test_parser_validates_loop_snapshots_and_dataflow_references() -> None:
    source = """name: invalid-loop
inputs: [{name: ready}]
steps:
  - for_each:
      snapshot: {name: missing_items, max_items: 0}
      body: []
  - branch:
      subject: missing_branch
      cases: {true: []}
  - repeat:
      max_iterations: 1
      until: {subject: missing_repeat, equals: true}
      body: []
"""
    diagnostics = validate_document(parse_document(source))
    paths = {diagnostic.path for diagnostic in diagnostics}

    assert "steps[0].for_each.item_binding" in paths
    assert "steps[0].for_each.snapshot.name" in paths
    assert "steps[0].for_each.snapshot.max_items" in paths
    assert "steps[1].branch.subject" in paths
    assert "steps[2].repeat.until.subject" in paths


def test_parser_allows_dotted_bindings_and_validates_limits() -> None:
    source = """name: valid-references
inputs: [{name: context}]
limits: {llm_activations: 2, tool_calls: 4}
steps:
  - judge:
      question: Choose.
      subject: context.value
      prompt_system: Return JSON.
      instructions: [Use the context.]
      context: [context.value]
      output:
        name: result
        schema: {type: object}
      validation: {kind: json_schema}
"""

    assert validate_document(parse_document(source)) == ()
    invalid_limits = parse_document(source.replace("tool_calls: 4", "tool_calls: 0"))
    assert any(
        diagnostic.path == "limits.tool_calls"
        for diagnostic in validate_document(invalid_limits)
    )


def test_parser_rejects_unknown_fields_and_limits() -> None:
    source = """name: invalid-shape
limits: {llm_activations: 2, mystery: 1}
inputs: [{name: request}]
steps:
  - operation:
      tool: internal
      command: [echo]
      bind: result
      unexpected: true
"""

    paths = {
        diagnostic.path for diagnostic in validate_document(parse_document(source))
    }
    assert "limits.mystery" in paths
    assert "steps[0].operation.unexpected" in paths


def test_parser_rejects_invalid_terminal_and_binding_names() -> None:
    source = """name: invalid-values
inputs: [{name: request}]
steps:
  - operation: {tool: internal, command: [echo], bind: bad.name}
  - terminal: complete
"""

    paths = {
        diagnostic.path for diagnostic in validate_document(parse_document(source))
    }
    assert "steps[0].operation.bind" in paths
    assert "steps[1].terminal" in paths


def test_parser_requires_valid_judge_schema_and_unique_context() -> None:
    source = """name: invalid-judge
inputs: [{name: request}]
steps:
  - judge:
      question: Decide
      subject: request
      prompt_system: Return JSON
      instructions: [Decide]
      context: [request, request]
      output:
        name: decision
        schema: {type: definitely-not-a-json-schema-type}
      validation: {kind: json_schema}
"""

    paths = {
        diagnostic.path for diagnostic in validate_document(parse_document(source))
    }
    assert "steps[0].judge.context" in paths
    assert "steps[0].judge.output.schema" in paths


def test_parser_does_not_expose_branch_local_bindings_after_branch() -> None:
    source = """name: branch-scope
inputs: [{name: request}]
steps:
  - branch:
      subject: request
      cases:
        ready:
          - operation: {tool: internal, command: [echo], bind: only_ready}
        waiting: []
  - gate:
      subject: only_ready
      equals: true
      on_failure: {terminal: failed}
"""

    paths = {
        diagnostic.path for diagnostic in validate_document(parse_document(source))
    }
    assert "steps[1].gate.subject" in paths


def test_parser_requires_positive_worklist_admission_limit() -> None:
    source = """name: invalid-worklist
inputs: [{name: request}]
steps:
  - worklist:
      snapshot: {name: request, max_items: 1}
      item_binding: item
      max_admissions: 0
      body: []
"""

    paths = {
        diagnostic.path for diagnostic in validate_document(parse_document(source))
    }
    assert "steps[0].worklist.max_admissions" in paths


def test_parser_rejects_model_owned_identity_and_integrity_fields() -> None:
    source = """
name: metadata
steps:
  - judge:
      question: Decide
      subject: input
      prompt_system: Return JSON
      instructions: [Decide]
      context: [input]
      output:
        name: decision
        schema:
          type: object
          properties:
            outcome: {type: string}
            nested:
              type: object
              properties:
                evidence_fingerprint: {type: string}
                explanation: {type: string}
            decision_ref: {type: string}
      validation: {kind: json_schema}
inputs: [{name: input}]
"""

    diagnostics = validate_document(parse_document(source))

    metadata_diagnostics = [
        diagnostic
        for diagnostic in diagnostics
        if diagnostic.code == "model_owned_metadata"
    ]
    assert {diagnostic.path for diagnostic in metadata_diagnostics} == {
        "steps[0].judge.output.schema.properties.nested.properties.evidence_fingerprint",
        "steps[0].judge.output.schema.properties.decision_ref",
    }


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
    fragment_judge = document["steps"][1]["repeat"]["body"][0]["judge"]
    assert [rule["when"]["binding"] for rule in fragment_judge["prompt_rules"]] == [
        "fragment_state.accepted",
        "fragment_state.diagnostic.code",
    ]


def test_checked_in_design_interview_definition_parses() -> None:
    from pathlib import Path

    source = Path("docs/procedrr/skill-definitions/design-interview.yaml").read_text()
    document = parse_and_validate(source)
    assert document["name"] == "design-interview"


def test_design_interview_bounds_semantic_obligation_prompts() -> None:
    from pathlib import Path

    source = Path("docs/procedrr/skill-definitions/design-interview.yaml").read_text()
    document = parse_and_validate(source)
    atomicity_body = document["steps"][1]["for_each"]["body"]
    atomicity_judge = atomicity_body[0]["judge"]
    split_body = document["steps"][3]["for_each"]["body"]
    split_judge = split_body[0]["judge"]
    body = document["steps"][5]["for_each"]["body"]
    judges = [step["judge"] for step in body if "judge" in step]
    obligation_judge = judges[1]
    acceptance_judge = judges[2]
    population_judge = judges[3]
    operation_judge = judges[4]
    oracle_judge = judges[5]
    evidence_judge = judges[6]

    assert atomicity_judge["question"] == (
        "Does this one instruction clause contain more than one independently "
        "verifiable requirement?"
    )
    assert atomicity_judge["output"]["schema"]["required"] == ["multiple"]
    assert split_judge["question"] == (
        "What are the smallest independently verifiable requirements contained in "
        "this one instruction clause?"
    )
    assert split_judge["output"]["schema"]["required"] == ["statements"]

    assert obligation_judge["question"] == (
        "What is the one concrete semantic obligation expressed by this instruction "
        "clause?"
    )
    assert all(
        "For non_goal" not in instruction
        for instruction in obligation_judge["instructions"]
    )
    assert [rule["when"] for rule in obligation_judge["prompt_rules"]] == [
        {"binding": "semantic_kind.kind", "equals": "non_goal"},
        {"binding": "semantic_kind.kind", "equals": "nonactionable"},
    ]
    assert (
        obligation_judge["output"]["schema"]["properties"]["description"]["maxLength"]
        == 500
    )
    assert acceptance_judge["question"] == (
        "What one observable result would prove this one semantic obligation?"
    )
    assert population_judge["output"]["name"] == "semantic_population"
    assert operation_judge["output"]["name"] == "semantic_operation"
    assert oracle_judge["output"]["name"] == "semantic_oracle"
    assert evidence_judge["output"]["name"] == "semantic_evidence_case"
    for judge in judges:
        example_lines = [
            instruction
            for instruction in judge["instructions"]
            if "Examples:" in instruction or "Counterexample:" in instruction
        ]
        assert len(example_lines) >= 2, judge["question"]


def test_checked_in_implement_feature_has_bounded_task_reviews() -> None:
    from pathlib import Path

    source = Path("docs/procedrr/skill-definitions/implement-feature.yaml").read_text()
    document = parse_and_validate(source)

    assert document["name"] == "implement-feature"
    assert validate_single_decision(document) == ()
    flow_text = str(document["steps"])
    assert "'process': 'design-interview'" in flow_text
    assert "feature_design" in flow_text
    assert "feature_obligations" in flow_text
    assert "decompose_feature_description" not in flow_text
    assert "design_decisions" not in flow_text
    assert "requirement_decisions" not in flow_text
    assert "reflection_decisions" not in flow_text
    step_text = flow_text
    assert "compile_obligation_verification_plans" in step_text
    assert "resolve_obligation_populations" in step_text
    assert "compile_code_task_plan" in step_text
    assert "run_code_task_agent" in step_text
    assert "finalize_code_task_receipt" in step_text
    assert "finalize_obligation_closure" in step_text
    assert "verify_code_task_decision" in flow_text
    assert "verify_implementation_decision" in flow_text
    assert "Does this one" in flow_text
