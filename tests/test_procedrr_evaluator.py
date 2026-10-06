# ruff: noqa: I001

import os
import json
import subprocess
import sys
import threading
from threading import Barrier
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

import pytest

from powdrr_lift.workrr.provider_config import DEEPINFRA_CHEAP_MODEL
from powdrr_lift.workrr.procedrr import StructuredToolExecutor
from procedrr_evaluator import EvaluationError, Evaluator
from procedrr_evaluator.evaluator import EvaluationEvent, ValidationGateError


def test_benchmark_gate_records_issue_and_continues() -> None:
    state: dict[str, Any] = {"benchmark_mode": True, "ready": False}
    events: list[EvaluationEvent] = []

    Evaluator(FakeLLM(), lambda *_: None)._gate(
        {"subject": "ready", "equals": True}, state, events, "steps[0].gate"
    )

    assert state["benchmark_gate_warnings"] == [
        {"subject": "ready", "expected": True, "observed": False}
    ]
    assert events[0].kind == "benchmark_gate_warning"


def test_non_benchmark_gate_still_raises() -> None:
    with pytest.raises(ValidationGateError):
        Evaluator(FakeLLM(), lambda *_: None)._gate(
            {"subject": "ready", "equals": True},
            {"ready": False},
            [],
            "steps[0].gate",
        )


class FakeLLM:
    def __init__(self) -> None:
        self.messages: list[dict[str, str]] = []

    def complete_json(self, messages: list[dict[str, str]], **_: Any) -> dict[str, Any]:
        self.messages = messages
        return {
            "added": [{"id": "req-1", "description": "Do the thing"}],
            "deleted": [],
        }


def test_for_each_runs_judge_only_items_concurrently_and_collects_in_order() -> None:
    from procedrr import parse_and_validate

    barrier = threading.Barrier(2)

    class ConcurrentLLM:
        def complete_json(
            self, messages: list[dict[str, str]], **_: Any
        ) -> dict[str, Any]:
            barrier.wait(timeout=5)
            return {"multiple": "compound" in messages[1]["content"]}

    document = parse_and_validate(
        """\
name: concurrent-classification
inputs:
  - {name: clauses, type: array, required: true}
steps:
  - for_each:
      snapshot: {name: clauses, max_items: 2}
      item_binding: clause
      collect: {binding: decisions, mode: list, value: decision}
      max_parallel: 2
      body:
        - judge:
            kind: classify_one
            provider: planning
            question: Classify this clause's multiplicity.
            subject: clause
            prompt_system: Return only JSON.
            instructions: [Judge this clause alone.]
            context: [clause]
            output:
              name: decision
              schema:
                type: object
                required: [multiple]
                additionalProperties: false
                properties:
                  multiple: {type: boolean}
            validation: {kind: json_schema}
"""
    )
    result = Evaluator(ConcurrentLLM(), lambda *_: None).evaluate(
        document, {"clauses": ["simple", "compound"]}
    )

    assert result.bindings["decisions"] == [
        {"item": "simple", "result": {"multiple": False}},
        {"item": "compound", "result": {"multiple": True}},
    ]
    assert result.llm_activations == 2


def test_evaluator_enforces_declared_operation_return_schema() -> None:
    from procedrr import parse_and_validate

    document = parse_and_validate(
        """
version: 1
name: typed-operation
steps:
  - operation:
      tool: internal
      parameters: {command: [demo]}
      returns:
        type: object
        required: [path]
        properties: {path: {type: string}}
      bind: result
  - terminal: succeeded
"""
    )

    with pytest.raises(EvaluationError):
        Evaluator(FakeLLM(), lambda _tool, _parameters: {"wrong": "shape"}).evaluate(
            document
        )


def test_evaluator_operation_event_contains_step_inputs_and_output() -> None:
    from procedrr import parse_and_validate

    document = parse_and_validate(
        """
version: 1
name: logged-operation
steps:
  - operation:
      tool: internal
      parameters: {command: [record], value: hello}
      bind: result
  - terminal: succeeded
"""
    )

    streamed: list[Any] = []
    result = Evaluator(
        FakeLLM(),
        lambda _tool, parameters: {"seen": parameters["value"]},
        event_sink=streamed.append,
    ).evaluate(document)

    operation = next(event for event in result.events if event.kind == "operation")
    assert streamed == list(result.events)
    assert operation.data["inputs"] == {"command": ["record"], "value": "hello"}
    assert operation.data["output"] == {"seen": "hello"}


def test_evaluator_calls_a_named_procedrr_process(tmp_path: Path) -> None:
    from procedrr import parse_and_validate

    (tmp_path / "design-interview.yaml").write_text(
        """
version: 1
name: design-interview
inputs: [{name: feature_description, type: string, required: true}]
outputs: {receipt: {type: object, required: [feature]}}
limits: {llm_activations: 2, tool_calls: 2}
steps:
  - operation:
      tool: internal
      parameters: {command: [record_feature, "${feature_description}"]}
      bind: receipt
  - terminal: succeeded
""",
        encoding="utf-8",
    )
    document = parse_and_validate(
        """
version: 1
name: implement-feature
inputs: [{name: feature_description, type: string, required: true}]
steps:
  - call:
      process: design-interview
      inputs: {feature_description: {type: reference, value: feature_description}}
      outputs: {interview: receipt}
  - terminal: succeeded
"""
    )
    calls: list[dict[str, Any]] = []

    def execute(_tool: str, parameters: Mapping[str, Any]) -> Any:
        calls.append(dict(parameters))
        return {"feature": parameters["command"][1]}

    result = Evaluator(FakeLLM(), execute, process_directory=tmp_path).evaluate(
        document, {"feature_description": "Add a greeting"}
    )

    assert result.bindings["interview"] == {"feature": "Add a greeting"}
    assert calls == [{"command": ["record_feature", "Add a greeting"]}]


def test_evaluator_resolves_tool_output_into_declared_judge_context() -> None:
    llm = FakeLLM()
    calls: list[tuple[str, dict[str, Any]]] = []

    def execute(tool: str, parameters: Mapping[str, Any]) -> Any:
        calls.append((tool, dict(parameters)))
        return [{"id": "existing", "description": "Old requirement"}]

    result = Evaluator(llm, execute).evaluate(
        {
            "name": "demo",
            "inputs": [{"name": "feature_description"}],
            "limits": {"llm_activations": 2, "tool_calls": 2},
            "steps": [
                {
                    "operation": {
                        "tool": "gather_context",
                        "parameters": {"types": ["requirements"]},
                        "bind": "requirements_context",
                    }
                },
                {
                    "judge": {
                        "prompt_system": "Return JSON only.",
                        "instructions": ["Preserve existing items."],
                        "question": "What requirement edits are needed?",
                        "context": ["feature_description", "requirements_context"],
                        "output": {
                            "name": "requirements_edits",
                            "schema": {
                                "type": "object",
                                "required": ["added", "deleted"],
                            },
                        },
                    }
                },
            ],
        },
        {"feature_description": "Add a thing"},
    )

    assert calls == [("gather_context", {"types": ["requirements"]})]
    assert result.bindings["requirements_edits"]["added"][0]["id"] == "req-1"
    assert "requirements_context" in llm.messages[1]["content"]
    metrics = next(
        event.data["context_metrics"]
        for event in result.events
        if event.kind == "judge"
    )
    assert metrics["raw_binding_chars"]["requirements_context"] > 0
    assert metrics["serialized_chars"] <= 24000


def test_evaluator_applies_prompt_rules_deterministically() -> None:
    llm = FakeLLM()
    from procedrr import parse_and_validate

    document = parse_and_validate(
        """
name: conditional-prompt
inputs: [{name: category, type: string, required: true}]
steps:
  - judge:
      prompt_system: Return JSON only.
      instructions: [Use only the supplied context.]
      prompt_rules:
        - when: {binding: category, equals: tests}
          instructions: [Describe the semantic obligation only.]
        - when: {binding: category, equals: other}
          mode: replace
          instructions: [Use the alternate contract.]
      question: What follows?
      subject: category
      context: [category]
      output: {name: answer, schema: {type: object}}
      validation: {kind: json_schema}
"""
    )

    Evaluator(llm, lambda _tool, _parameters: None).evaluate(
        document, {"category": "tests"}
    )

    prompt = llm.messages[1]["content"]
    assert "Use only the supplied context." in prompt
    assert "Describe the semantic obligation only." in prompt
    assert "Use the alternate contract." not in prompt


def test_evaluator_bounds_large_judge_context() -> None:
    llm = FakeLLM()
    Evaluator(FakeLLM(), lambda _tool, _parameters: None).evaluate(
        {
            "name": "bounded",
            "limits": {"context_chars": 120, "context_value_chars": 40},
            "steps": [
                {
                    "judge": {
                        "prompt_system": "Return JSON only.",
                        "instructions": ["Be concise."],
                        "question": "Summarize.",
                        "context": ["large"],
                        "output": {
                            "name": "summary",
                            "schema": {"type": "object"},
                        },
                    }
                }
            ],
        },
        {"large": {"document": "x" * 10000}},
    )

    assert len(llm.messages) == 0
    bounded = FakeLLM()
    Evaluator(bounded, lambda _tool, _parameters: None).evaluate(
        {
            "name": "bounded",
            "limits": {"context_chars": 120, "context_value_chars": 40},
            "steps": [
                {
                    "judge": {
                        "prompt_system": "Return JSON only.",
                        "instructions": ["Be concise."],
                        "question": "Summarize.",
                        "context": ["large"],
                        "output": {
                            "name": "summary",
                            "schema": {"type": "object"},
                        },
                    }
                }
            ],
        },
        {"large": {"document": "x" * 10000}},
    )
    assert len(bounded.messages[1]["content"]) < 500
    assert "<truncated>" in bounded.messages[1]["content"]


def test_evaluator_resolves_embedded_references_in_operation_parameters() -> None:
    calls: list[dict[str, Any]] = []

    def execute(tool: str, parameters: Mapping[str, Any]) -> Any:
        calls.append(dict(parameters))
        return None

    Evaluator(FakeLLM(), execute).evaluate(
        {
            "name": "interpolation",
            "steps": [
                {
                    "operation": {
                        "tool": "internal",
                        "parameters": {
                            "command": [
                                "powdrr-lift",
                                "evaluate",
                                "docs/proposals/${work_item_name}/design.yaml",
                            ]
                        },
                    }
                }
            ],
        },
        {"work_item_name": "demo-feature"},
    )

    assert calls == [
        {
            "command": [
                "powdrr-lift",
                "evaluate",
                "docs/proposals/demo-feature/design.yaml",
            ]
        }
    ]


def test_attempt_runs_declared_recovery_and_resumes() -> None:
    calls = 0

    def execute(tool: str, parameters: Mapping[str, Any]) -> Any:
        nonlocal calls
        calls += 1
        return True

    result = Evaluator(FakeLLM(), execute).evaluate(
        {
            "name": "recovery",
            "steps": [
                {
                    "attempt": {
                        "id": "work",
                        "max_attempts": 2,
                        "body": [
                            {
                                "gate": {
                                    "subject": "ready",
                                    "equals": True,
                                    "on_failure": {
                                        "retry": {
                                            "target": "repair",
                                            "max_attempts": 1,
                                            "on_exhausted": "failed",
                                        }
                                    },
                                }
                            }
                        ],
                        "on_failure": {"recovery": "repair", "resume": "work"},
                    }
                }
            ],
            "recoveries": {
                "repair": {
                    "steps": [
                        {
                            "operation": {
                                "tool": "internal",
                                "bind": "ready",
                            }
                        }
                    ]
                }
            },
        },
        {"ready": False},
    )

    assert result.bindings["ready"] is True
    assert calls == 1
    assert any(event.kind == "recovery" for event in result.events)


def test_for_each_collects_structured_results_in_order() -> None:
    result = Evaluator(
        FakeLLM(), lambda _tool, parameters: {"returncode": parameters["code"]}
    ).evaluate(
        {
            "name": "structured-results",
            "steps": [
                {
                    "for_each": {
                        "snapshot": {"name": "codes", "max_items": 2},
                        "item_binding": "code",
                        "collect": {
                            "binding": "validation_results",
                            "mode": "list",
                            "key": "command",
                            "value": "validation_result",
                        },
                        "body": [
                            {
                                "operation": {
                                    "tool": "check",
                                    "parameters": {"code": "${code}"},
                                    "bind": "validation_result",
                                }
                            }
                        ],
                    }
                }
            ],
        },
        {"codes": [0, 1]},
    )

    assert result.bindings["validation_results"] == [
        {"command": 0, "result": {"returncode": 0}},
        {"command": 1, "result": {"returncode": 1}},
    ]


def test_for_each_parallel_judgments_run_concurrently_and_collect_in_order() -> None:
    barrier = Barrier(2)

    class ConcurrentLLM:
        def complete_json(
            self,
            messages: list[dict[str, str]],
            **_: Any,
        ) -> dict[str, Any]:
            barrier.wait(timeout=5)
            content = messages[-1]["content"]
            answer = "item-0" if '"instruction": "item-0"' in content else "item-1"
            return {"answer": answer}

    result = Evaluator(ConcurrentLLM(), lambda _tool, _parameters: None).evaluate(
        {
            "name": "parallel-instruction-judgments",
            "steps": [
                {
                    "for_each": {
                        "snapshot": {"name": "instructions", "max_items": 2},
                        "item_binding": "instruction",
                        "max_parallel": 2,
                        "collect": {
                            "binding": "answers",
                            "mode": "list",
                            "value": "judgment",
                        },
                        "body": [
                            {
                                "judge": {
                                    "question": "Classify the instruction",
                                    "prompt_system": "Return JSON",
                                    "context": ["instruction"],
                                    "output": {
                                        "name": "judgment",
                                        "schema": {
                                            "type": "object",
                                            "required": ["answer"],
                                            "properties": {
                                                "answer": {"type": "string"}
                                            },
                                        },
                                    },
                                }
                            }
                        ],
                    }
                }
            ],
        },
        {"instructions": ["item-0", "item-1"]},
    )

    assert result.bindings["answers"] == [
        {"item": "item-0", "result": {"answer": "item-0"}},
        {"item": "item-1", "result": {"answer": "item-1"}},
    ]
    assert [event.path for event in result.events] == [
        "steps[0].for_each[0][0][0]",
        "steps[0].for_each[0][1][0]",
    ]


def test_repeat_and_branch_are_bounded_and_data_driven() -> None:
    calls = 0

    def execute(_tool: str, _parameters: Mapping[str, Any]) -> Any:
        nonlocal calls
        calls += 1
        return calls >= 2

    result = Evaluator(FakeLLM(), execute).evaluate(
        {
            "name": "repeat-branch",
            "steps": [
                {
                    "repeat": {
                        "max_iterations": 3,
                        "until": {"subject": "done", "equals": True},
                        "body": [{"operation": {"tool": "check", "bind": "done"}}],
                    }
                },
                {
                    "branch": {
                        "subject": "done",
                        "cases": {True: [{"terminal": "succeeded"}]},
                        "default": [{"terminal": "failed"}],
                    }
                },
            ],
        }
    )

    assert calls == 2
    assert result.events[-1].data["status"] == "succeeded"


def test_for_each_without_max_items_reviews_the_entire_snapshot() -> None:
    result = Evaluator(
        FakeLLM(), lambda _tool, parameters: parameters["sentence"]
    ).evaluate(
        {
            "name": "unbounded-sentence-review",
            "steps": [
                {
                    "for_each": {
                        "snapshot": {"name": "sentences"},
                        "item_binding": "sentence",
                        "collect": {"binding": "reviewed", "mode": "list"},
                        "body": [
                            {
                                "operation": {
                                    "tool": "review",
                                    "parameters": {"sentence": "${sentence}"},
                                    "bind": "review_result",
                                }
                            }
                        ],
                    }
                }
            ],
        },
        {"sentences": [f"sentence-{index}" for index in range(65)]},
    )

    assert result.bindings["reviewed"] == [
        {"item": f"sentence-{index}", "result": f"sentence-{index}"}
        for index in range(65)
    ]


def test_evaluator_runs_checked_in_design_interview_definition() -> None:
    class DesignInterviewLLM:
        source_values = iter(
            (
                "include",
                "feature",
                "required",
                "unspecified",
                "must",
                "absent",
                "absent",
                "absent",
                "unspecified",
                "not_stated",
                "product_semantics_present",
            )
        )

        def complete_json(
            self, messages: list[dict[str, str]], **_: Any
        ) -> dict[str, Any]:
            question = messages[1]["content"]
            if "independently verifiable requirement" in question:
                return {"multiple": False}
            if (
                "route this exact instruction clause" in question
                or "child decision" in question
            ):
                return {
                    "status": "resolved",
                    "value": next(self.source_values),
                    "reason_code": None,
                }
            if "candidate field" in question:
                return {
                    "status": "resolved",
                    "value": "entailed",
                    "reason_code": None,
                }
            if "one registered behavior family" in question:
                return {
                    "status": "resolved",
                    "value": "create",
                    "reason_code": None,
                }
            if "source-supported operation and behavior rule" in question:
                return {
                    "subject": None,
                    "operation": None,
                    "affected_value": None,
                    "rule": None,
                    "contrast": None,
                    "behavior_form": "unclear",
                    "result_presence": "unspecified",
                    "event_scope": "unspecified",
                    "contrast_presence": "absent",
                    "unresolved_fields": [
                        "subject|source_underspecified",
                        "operation|source_underspecified",
                        "affected_value|source_underspecified",
                        "rule|source_underspecified",
                        "behavior_form|source_underspecified",
                    ],
                    "field_evidence": [],
                }
            if "lossless behavior scenario" in question:
                return {
                    "status": "resolved",
                    "unresolved_dimensions": [],
                    "scenario": {
                        "subject": "the feature",
                        "given": "the declared feature input",
                        "when": "the feature is invoked",
                        "then": "the acceptance result is observed",
                        "related_requirements": [],
                        "dimensions": {
                            "normal_result": ("the acceptance result is observed"),
                            "error_behavior": "not_applicable",
                            "continuation": "not_applicable",
                            "unsupported_behavior": "not_applicable",
                            "cancellation_cleanup": "not_applicable",
                            "compatibility": "not_applicable",
                            "negative_boundaries": "not_applicable",
                        },
                        "capability_matrix": [],
                    },
                }
            if "exact subject of the instruction proposition" in question:
                return {
                    "status": "resolved",
                    "value": "insufficient_evidence",
                    "reason_code": None,
                }
            if "recorded defaults coherent" in question:
                return {"consistency_review": {"updates": []}}
            raise AssertionError(question)

    llm = DesignInterviewLLM()

    def execute(tool: str, parameters: Mapping[str, Any]) -> Any:
        if tool == "gather_context":
            return [{"id": "existing", "description": "Existing evidence"}]
        if tool == "internal":
            command = parameters.get("command", [])
            if command[0] == "compile_instruction_ledger":
                return {
                    "path": "instruction-ledger.json",
                    "fingerprint": "sha256:ledger",
                    "source_text": "Add a thing",
                    "clauses": [
                        {
                            "clause_id": "instruction-001",
                            "text": "Add a thing",
                            "source_span": {"start": 0, "end": 10},
                            "fingerprint": "sha256:clause",
                        }
                    ],
                    "source": {"text": "Add a thing"},
                }
            if command[0] == "prepare_atomicity_split_requests":
                return {"split_requests": []}
            if command[0] == "apply_atomicity_splits":
                return {
                    "path": "instruction-ledger.json",
                    "fingerprint": "sha256:atomic-ledger",
                    "source_text": "Add a thing",
                    "clauses": [
                        {
                            "clause_id": "instruction-001",
                            "text": "Add a thing",
                            "source_span": {"start": 0, "end": 10},
                            "fingerprint": "sha256:clause",
                        }
                    ],
                    "source": {"text": "Add a thing"},
                }
            if command[0] == "prepare_source_semantic_decisions":
                return {
                    "resolved_decisions": [],
                    "pending_specs": [
                        {
                            "spec": {"decision_kind": "disposition"},
                            "subject_text": "Add a thing",
                        }
                    ],
                }
            if command[0] == "prepare_dependent_source_semantic_decisions":
                return {
                    "resolved_decisions": [{"decision_kind": "disposition"}],
                    "pending_specs": [
                        {
                            "spec": {"decision_kind": f"child-{kind}"},
                            "subject_text": "Add a thing",
                        }
                        for kind in range(9)
                    ],
                }
            if command[0] == "bind_source_semantic_decisions":
                return {"path": "source-decisions.json", "decisions": [1]}
            if command[0] == "compile_deterministic_source_extractions":
                return {"path": "source-extractions.json", "extractions": [1, 2]}
            if command[0] == "prepare_behavior_family_decision":
                return {
                    "spec": {},
                    "question": "Classify behavior.",
                    "instructions": ["Choose one value."],
                    "allowed_values": ["create"],
                    "subject_text": "Add",
                }
            if command[0] == "prepare_source_interpretation":
                return {
                    "request": {"subject_text": "Interpret Add a thing."},
                    "behavior_family_decision": {},
                }
            if command[0] == "prepare_behavioral_contracts":
                return {
                    "ledger_fingerprint": "sha256:ledger",
                    "requirement_ids": ["instruction-001"],
                    "context_ids": [],
                    "role_interpretations": {},
                    "requests": [],
                }
            if command[0] == "bind_behavioral_contracts":
                return {
                    "schema_version": "behavioral-contract-collection-v1",
                    "ledger_fingerprint": "sha256:ledger",
                    "contracts": [],
                    "covered_requirement_ids": ["instruction-001"],
                }
            if command[0] == "prepare_acceptance_criteria":
                return {
                    "schema_version": "acceptance-criterion-v1",
                    "ledger_fingerprint": "sha256:ledger",
                    "requirement_ids": ["instruction-001"],
                    "requests": [],
                }
            if command[0] == "bind_acceptance_criteria":
                return {
                    "schema_version": "acceptance-criterion-v1",
                    "ledger_fingerprint": "sha256:ledger",
                    "criteria": [],
                    "requirement_coverage": {},
                    "counts": {
                        "requirements": 1,
                        "with_criteria": 0,
                        "unassessed": 0,
                        "source_only": 1,
                    },
                }
            if command[0] == "prepare_acceptance_criterion_reviews":
                return {
                    "schema_version": "acceptance-criterion-review-plan-v1",
                    "ledger_fingerprint": "sha256:ledger",
                    "requests": [],
                }
            if command[0] == "bind_acceptance_criterion_reviews":
                return {
                    "schema_version": "acceptance-criterion-v1",
                    "ledger_fingerprint": "sha256:ledger",
                    "criteria": [],
                    "requirement_coverage": {},
                    "counts": {
                        "requirements": 1,
                        "with_criteria": 0,
                        "needs_repair": 0,
                    },
                    "reviews": [],
                }
            if command[0] == "prepare_acceptance_criterion_repairs":
                return {
                    "done": True,
                    "criterion_collection": parameters["criteria"],
                    "plan": {
                        "schema_version": "acceptance-criterion-repair-plan-v1",
                        "ledger_fingerprint": "sha256:ledger",
                        "requirement_ids": [],
                        "contracts": [],
                        "requests": [],
                        "replaces": {},
                    },
                    "requests": [],
                }
            if command[0] == "bind_acceptance_criterion_repairs":
                collection = parameters["plan"]["criterion_collection"]
                return {"criterion_collection": collection, "criteria": []}
            if command[0] == "compile_partial_semantic_contract":
                return {
                    "kind": "feature",
                    "description": "Add a thing",
                    "acceptance_criterion": "Add a thing",
                    "expected_test": "Add a thing",
                    "population": "thing",
                    "operation": "Add",
                    "oracle": "Add a thing",
                    "evidence_case": "Add a thing",
                    "partial_contract_path": "partial-contract.json",
                    "partial_contract_fingerprint": "sha256:contract",
                    "partial_contract": {
                        "schema_version": "partial-semantic-contract-v3",
                        "contract_id": "contract:instruction-001",
                        "source_ref": "instruction-001",
                        "source_fingerprint": "sha256:clause",
                        "proposition_text": "Add a thing",
                        "routing": "include",
                        "disposition": "feature",
                    },
                }
            if command[0] == "prepare_repository_subject_binding":
                return {
                    "query": {},
                    "candidates": {
                        "query_fingerprint": "sha256:query",
                        "inventory_fingerprint": "sha256:inventory",
                        "candidates": [],
                        "retrieval_status": "complete",
                    },
                    "requests": [],
                }
            if command[0] == "finalize_repository_subject_binding":
                return {
                    "status": "unresolved",
                    "binding_ref": None,
                    "reason_code": "no_candidate",
                    "candidate_ids": [],
                    "retrieval_status": "complete",
                }
            if command[0] == "prepare_field_entailment_reviews":
                return {"requests": [{"source_text": "Add a thing", "spec": {}}]}
            if command[0] == "bind_field_entailment_reviews":
                return {"reviews": [{}]}
            if command[0] == "finalize_source_faithfulness":
                return {"accepted": True, "unresolved_fields": [], "findings": []}
            if command[0] == "prepare_scenario_claim_reviews":
                return {
                    "source_ref": "instruction-001",
                    "source_fingerprint": "sha256:clause",
                    "contract_fingerprint": "sha256:contract",
                    "scenario_fingerprint": "sha256:scenario",
                    "context_fingerprint": "sha256:context",
                    "deterministic_claims": [],
                    "decision_specs": {},
                    "requests": [],
                    "overflow": False,
                    "candidate_count": 0,
                }
            if command[0] == "finalize_scenario_claim_reviews":
                return {
                    "accepted": True,
                    "findings": [],
                    "unresolved_fields": [],
                    "reviews": [],
                    "artifact_path": "scenario-faithfulness.json",
                    "artifact_fingerprint": "sha256:scenario-faithfulness",
                }
            if command[0] == "enforce_scenario_claim_faithfulness":
                return parameters["design"]
            if command[0] == "compile_canonical_feature_design":
                return {
                    "path": "canonical-feature-design.json",
                    "fingerprint": "sha256:design",
                    "obligations": [
                        {
                            "id": "sentence-1",
                            "description": "The feature is implemented.",
                            "design": {
                                "kind": "feature",
                                "description": "The feature is implemented.",
                                "acceptance_criterion": (
                                    "The feature behavior is observable."
                                ),
                                "population": "all feature instances",
                                "operation": "invoke the feature",
                                "oracle": "the feature result matches the requirement",
                                "evidence_case": (
                                    "invoke one representative feature instance"
                                ),
                            },
                        }
                    ],
                    "verification_contracts": [
                        {
                            "id": "contract-sentence-1",
                            "obligation_ref": "sentence-1",
                            "population": "all feature instances",
                            "operation": "invoke the feature",
                            "oracle": "the feature result matches the requirement",
                            "evidence_case": (
                                "invoke one representative feature instance"
                            ),
                        }
                    ],
                    "required_test_cases": [
                        {
                            "id": "test-sentence-1",
                            "behavior_scenario": {
                                "schema_version": "behavior-scenario-v1",
                                "scenario_id": "scenario:instruction-001",
                                "subject": "the feature",
                                "given": "the declared feature input",
                                "when": "the feature is invoked",
                                "then": "the acceptance result is observed",
                                "dimensions": {
                                    "normal_result": (
                                        "the acceptance result is observed"
                                    ),
                                    "error_behavior": "not_applicable",
                                    "continuation": "not_applicable",
                                    "unsupported_behavior": "not_applicable",
                                    "cancellation_cleanup": "not_applicable",
                                    "compatibility": "not_applicable",
                                    "negative_boundaries": "not_applicable",
                                },
                                "evidence": ["focused test"],
                                "validator": "focused test",
                                "capability_matrix": [],
                            },
                        }
                    ],
                }
            if command[0] == "merge_behavior_scenario":
                return {
                    **parameters["design"],
                    "behavior_scenario": {
                        "schema_version": "behavior-scenario-v1",
                        "scenario_id": "scenario:instruction-001",
                        "subject": "the feature",
                        "given": "the declared feature input",
                        "when": "the feature is invoked",
                        "then": "the acceptance result is observed",
                        "dimensions": {
                            "normal_result": "the acceptance result is observed",
                            "error_behavior": "not_applicable",
                            "continuation": "not_applicable",
                            "unsupported_behavior": "not_applicable",
                            "cancellation_cleanup": "not_applicable",
                            "compatibility": "not_applicable",
                            "negative_boundaries": "not_applicable",
                        },
                        "evidence": ["focused test"],
                        "validator": "focused test",
                        "capability_matrix": [],
                    },
                }
            return {"ok": True}

    source = Path("docs/procedrr/skill-definitions/design-interview.yaml").read_text()
    from procedrr import parse_and_validate

    document = parse_and_validate(source)
    result = Evaluator(llm, execute).evaluate(
        document,
        {
            "work_item_name": "demo",
            "feature_description": "Add a thing",
            "benchmark_mode": False,
            "uncertainty_policy": "clarify",
        },
    )
    assert result.bindings["feature_design"]["obligations"][0]["id"] == "sentence-1"
    assert result.llm_activations == 16
    judge_values = {
        event.data["output"]: event.data["value"]
        for event in result.events
        if event.kind == "judge" and "value" in event.data
    }
    assert judge_values["behavior_family_result"] == {
        "status": "resolved",
        "value": "create",
        "reason_code": None,
    }
    assert "source_extraction_result" not in judge_values


@pytest.mark.live_provider
def test_live_parallel_atomicity_matches_serial_judgments(tmp_path: Path) -> None:
    """Check exact per-clause prompts under serial and bounded parallel execution."""
    if os.environ.get("POWDRR_LIVE_LLM") != "1":
        pytest.skip("set POWDRR_LIVE_LLM=1 to run the paid live-provider test")

    import copy
    import time
    from importlib import import_module

    import yaml
    from procedrr import parse_and_validate

    build_probe_client = import_module(
        "powdrr_lift.workrr.prompt_probe"
    ).build_probe_client
    llm = build_probe_client(
        provider="deepinfra-cheap",
        model=DEEPINFRA_CHEAP_MODEL,
        api_key=None,
        base_url=None,
        repo_root=tmp_path,
        progress_stream=sys.stderr,
    )
    clauses = [
        {"clause_id": "instruction-001", "text": "On entry, data is copied."},
        {"clause_id": "instruction-002", "text": "On exit, data is removed."},
        {
            "clause_id": "instruction-003",
            "text": (
                "set_state_data validates the key and value, and raises "
                "InvalidDefinition when either is invalid."
            ),
        },
    ]
    production = parse_and_validate(
        Path("docs/procedrr/skill-definitions/design-interview.yaml").read_text()
    )
    loop = copy.deepcopy(production["steps"][1]["for_each"])
    loop["snapshot"]["name"] = "atomicity_clauses"

    def stage(max_parallel: int) -> dict[str, Any]:
        declaration = copy.deepcopy(loop)
        declaration["max_parallel"] = max_parallel
        return parse_and_validate(
            yaml.safe_dump(
                {
                    "name": "atomicity-comparison",
                    "inputs": [
                        {"name": "atomicity_clauses", "type": "array", "required": True}
                    ],
                    "steps": [{"for_each": declaration}],
                },
                sort_keys=False,
            )
        )

    started = time.perf_counter()
    serial = Evaluator(llm, lambda *_: None).evaluate(
        stage(1), {"atomicity_clauses": clauses}
    )
    serial_seconds = time.perf_counter() - started
    started = time.perf_counter()
    parallel = Evaluator(llm, lambda *_: None).evaluate(
        stage(4), {"atomicity_clauses": clauses}
    )
    parallel_seconds = time.perf_counter() - started
    assert (
        parallel.bindings["atomicity_decisions"]
        == serial.bindings["atomicity_decisions"]
    )
    assert serial.llm_activations == parallel.llm_activations == len(clauses)
    print(
        f"atomicity A/B: serial={serial_seconds:.2f}s, parallel={parallel_seconds:.2f}s"
    )


@pytest.mark.live_provider
def test_live_design_interview_decomposes_compound_state_data_lifecycle(
    tmp_path: Path,
) -> None:
    """Require the live planning model to split a compound source clause."""
    if os.environ.get("POWDRR_LIVE_LLM") != "1":
        pytest.skip("set POWDRR_LIVE_LLM=1 to run the paid live-provider test")

    from importlib import import_module

    from powdrr_lift.workrr.command_catalog import (
        FeatureCommandRuntime,
        feature_command_catalog,
    )
    from procedrr import parse_and_validate

    build_probe_client = import_module(
        "powdrr_lift.workrr.prompt_probe"
    ).build_probe_client
    llm = build_probe_client(
        provider="deepinfra-cheap",
        model=DEEPINFRA_CHEAP_MODEL,
        api_key=None,
        base_url=None,
        repo_root=tmp_path,
        progress_stream=sys.stderr,
    )
    catalog = feature_command_catalog()
    runtime = FeatureCommandRuntime(
        config=None,
        runner=None,
        worktree=tmp_path,
        output_root=tmp_path,
        branch="feature/live-test",
        slug="live-state-data-atomicity",
        state={},
        catalog=catalog,
    )

    def execute(tool: str, parameters: Mapping[str, Any]) -> Any:
        assert tool == "internal"
        command = parameters["command"]
        return runtime.dispatch(command[0], list(command), parameters)

    document = parse_and_validate(
        Path("docs/procedrr/skill-definitions/design-interview.yaml").read_text()
    )
    result = Evaluator(llm, execute).evaluate(
        document,
        {
            "work_item_name": "live-state-data-atomicity",
            "feature_description": (
                "On entry, state data initializes as a fresh copy of the defaults "
                "and on exit state data is removed."
            ),
        },
    )

    judge_values = {
        event.data["output"]: event.data["value"]
        for event in result.events
        if event.kind == "judge" and "value" in event.data
    }
    assert judge_values["atomicity_decision"] == {"multiple": True}
    statements = judge_values["atomicity_split"]["statements"]
    groups = judge_values["atomicity_split"]["validation_groups"]
    assert len(statements) >= 2
    assert all(statement.strip() for statement in statements)
    assert all(
        set(group) == {"members", "relation"}
        and all(1 <= member <= len(statements) for member in group["members"])
        for group in groups
    )
    assert len(result.bindings["feature_design"]["obligations"]) == len(statements)


def test_evaluator_tracks_operation_output_schema_on_binding() -> None:
    from procedrr import parse_and_validate

    source = """
name: schema-tracking
steps:
  - operation:
      tool: internal
      command: [produce]
      returns:
        type: object
        required: [value]
        properties:
          value: {type: string}
      bind: produced
  - terminal: succeeded
"""
    result = Evaluator(
        object(),  # type: ignore[arg-type]
        lambda _tool, _parameters: {"value": "ok"},
    ).evaluate(parse_and_validate(source))
    assert result.binding_schemas["produced"]["required"] == ["value"]


class HelloWorldLLM:
    def __init__(self) -> None:
        self.plan_round = 0
        self.fragment_round = 0

    def complete_json(self, messages: list[dict[str, str]], **_: Any) -> dict[str, Any]:
        question = messages[1]["content"]
        if "Which exact repository files" in question:
            return {"file_path": "hello.py", "reason": "read the program"}
        if "Which exact file must be read" in question:
            return {"file_path": "hello.py", "reason": "confirm current output"}
        if "smallest ordered implementation plan" in question:
            return {
                "validation_commands": [
                    [sys.executable, "-m", "pytest", "-q"],
                    ["ruff", "check", "hello.py"],
                ],
                "acceptance_conditions": [
                    "hello.py prints Hello, World and Here I Am on separate lines",
                    "the test asserting both lines passes",
                    "ruff reports no issues",
                ],
            }
        if "What one JSON-encoded Procedrr step should be appended" in question:
            self.fragment_round += 1
            value: Any
            if self.fragment_round == 1:
                value = {
                    "operation": {
                        "tool": "read_document",
                        "parameters": {"file_path": "hello.py"},
                        "bind": "source",
                    }
                }
            elif self.fragment_round == 2:
                value = {
                    "operation": {
                        "tool": "edit",
                        "parameters": {
                            "file_path": "hello.py",
                            "edits": [
                                {
                                    "old_text": 'print("Hello, World")\n',
                                    "new_text": (
                                        'print("Hello, World")\nprint("Here I Am")\n'
                                    ),
                                }
                            ],
                        },
                        "bind": "applied_edit",
                    }
                }
            else:
                value = {"terminal": "succeeded"}
            return {"step_json": json.dumps(value)}
        if "Is implementation planning complete" in question:
            self.plan_round += 1
            if self.plan_round > 1:
                return {
                    "done": True,
                    "action": {
                        "id": "done",
                        "file_path": "hello.py",
                        "intent": "complete",
                    },
                }
            return {
                "done": False,
                "action": {
                    "id": "add-second-line",
                    "file_path": "hello.py",
                    "intent": "Print Here I Am after Hello, World.",
                },
            }
        if "What one edit implements" in question:
            return {
                "file_path": "hello.py",
                "edit": {
                    "old_text": 'print("Hello, World")\n',
                    "new_text": 'print("Hello, World")\nprint("Here I Am")\n',
                },
            }
        if "Are all actionable validation failures" in question:
            return {
                "done": True,
                "action": {"id": "done", "file_path": "hello.py", "intent": "complete"},
            }
        if "What is the smallest safe repair" in question:
            return {
                "file_path": "hello.py",
                "start": 1,
                "end": 1,
                "new_text": 'print("Hello, World")\nprint("Here I Am")\n',
            }
        if (
            "Is there one remaining validation failure" in question
            or "Are all validations passing" in question
        ):
            return {
                "done": True,
                "failure_index": -1,
            }
        if "Which validation failures require" in question:
            return {
                "done": True,
                "action": {"id": "done", "file_path": "hello.py", "intent": "complete"},
            }
        if "Does the implemented tree" in question:
            return {"complete": True, "missing": []}
        if "Are all declared invariants" in question:
            return {"preserved": True, "violations": []}
        if "security regression" in question:
            return {"safe": True, "findings": []}
        raise AssertionError(f"unexpected judge question: {question}")


def test_execute_proposed_pr_hello_world_end_to_end(tmp_path: Path) -> None:
    (tmp_path / "hello.py").write_text('print("Hello, World")\n', encoding="utf-8")
    proposal_dir = tmp_path / "docs" / "proposals" / "hello-world"
    proposal_dir.mkdir(parents=True)
    (proposal_dir / "proposed-pr-specification.yaml").write_text(
        "id: hello-world\nfeatures: [{id: hello-world, action: added}]\n",
        encoding="utf-8",
    )
    (proposal_dir / "implementation-specification.yaml").write_text(
        "id: hello-world\nmodules: [{id: hello, action: added}]\n",
        encoding="utf-8",
    )
    (tmp_path / "test_hello.py").write_text(
        "import subprocess\n"
        "import sys\n\n"
        "def test_hello_has_two_lines():\n"
        "    result = subprocess.run(\n"
        "        [sys.executable, 'hello.py'], capture_output=True, text=True,\n"
        "        check=True,\n"
        "    )\n"
        "    assert result.stdout.splitlines() == ['Hello, World', 'Here I Am']\n",
        encoding="utf-8",
    )
    source = Path(
        "docs/procedrr/skill-definitions/execute-proposed-pr.yaml"
    ).read_text()
    from procedrr import parse_and_validate

    document = parse_and_validate(source)

    def execute(tool: str, parameters: Mapping[str, Any]) -> Any:
        if tool == "internal":
            command = parameters.get("command", [])
            if command[:2] == ["powdrr-lift", "show-proposed-pr"]:
                return {"id": "hello-world", "intent": "Add a second output line."}
            completed = subprocess.run(
                command, cwd=tmp_path, capture_output=True, text=True
            )
            return {
                "returncode": completed.returncode,
                "stdout": completed.stdout,
                "stderr": completed.stderr,
            }
        if tool == "gather_context":
            return {
                "types": parameters["types"],
                "feature_id": parameters.get("feature_id"),
            }
        if tool == "read_document":
            return (tmp_path / parameters["file_path"]).read_text(encoding="utf-8")
        if tool == "invoke_tool":
            return {"tool": parameters["tool"], "returncode": 0}
        if tool == "edit":
            path = tmp_path / parameters["file_path"]
            text = path.read_text(encoding="utf-8")
            for edit in parameters["edits"]:
                text = text.replace(edit["old_text"], edit["new_text"], 1)
            path.write_text(text, encoding="utf-8")
            return {"changed": True, "path": parameters["file_path"]}
        raise AssertionError(f"unexpected operation: {tool}")

    result = Evaluator(HelloWorldLLM(), StructuredToolExecutor(execute)).evaluate(
        document,
        {
            "work_item_name": "hello-world",
            "proposed_pr_id": "hello-world",
            "feature_description": "Print an additional line: Here I Am.",
        },
    )

    assert result.bindings["completeness_review"]["complete"] is True
    assert result.bindings["invariant_review"]["preserved"] is True
    assert result.bindings["security_review"]["safe"] is True
    assert (
        tmp_path / "hello.py"
    ).read_text() == 'print("Hello, World")\nprint("Here I Am")\n'


def test_operation_resolves_explicit_literal_and_reference_values() -> None:
    from procedrr import parse_and_validate

    document = parse_and_validate(
        """
version: 1
name: values
inputs: [{name: source, type: string, required: true}]
steps:
  - operation:
      tool: internal
      parameters:
        command:
          - {type: literal, value: echo}
          - {type: reference, value: source}
      bind: result
"""
    )
    seen: list[dict[str, Any]] = []

    def execute(tool: str, parameters: Mapping[str, Any]) -> Any:
        seen.append(dict(parameters))
        return {"ok": True}

    Evaluator(cast(Any, lambda *_args, **_kwargs: {}), execute).evaluate(
        document, {"source": "hello"}
    )
    assert seen == [{"command": ["echo", "hello"]}]


def test_execute_proposed_pr_hello_world_with_live_llm(tmp_path: Path) -> None:
    """Exercise the same flow with a real provider when explicitly enabled."""
    if os.environ.get("POWDRR_LIVE_LLM") != "1":
        import pytest

        pytest.skip("set POWDRR_LIVE_LLM=1 to run the paid live-provider test")

    (tmp_path / "hello.py").write_text('print("Hello, World")\n', encoding="utf-8")
    (tmp_path / "test_hello.py").write_text(
        "import subprocess\nimport sys\n\n"
        "def test_hello_has_two_lines():\n"
        "    result = subprocess.run(\n"
        "        [sys.executable, 'hello.py'], capture_output=True, text=True,\n"
        "        check=True,\n"
        "    )\n"
        "    assert result.stdout.splitlines() == ['Hello, World', 'Here I Am']\n",
        encoding="utf-8",
    )
    proposal_dir = tmp_path / "docs" / "proposals" / "hello-world-live"
    proposal_dir.mkdir(parents=True)
    (proposal_dir / "proposed-pr-specification.yaml").write_text(
        "id: hello-world-live\nfeatures: [{id: hello-world, action: added}]\n",
        encoding="utf-8",
    )
    (proposal_dir / "implementation-specification.yaml").write_text(
        "id: hello-world-live\nmodules: [{id: hello, action: added}]\n",
        encoding="utf-8",
    )
    from procedrr import parse_and_validate

    from importlib import import_module

    build_probe_client = import_module(
        "powdrr_lift.workrr.prompt_probe"
    ).build_probe_client
    document = parse_and_validate(
        Path("docs/procedrr/skill-definitions/execute-proposed-pr.yaml").read_text()
    )
    llm = build_probe_client(
        provider="deepinfra-cheap",
        model=DEEPINFRA_CHEAP_MODEL,
        api_key=None,
        base_url=None,
        repo_root=tmp_path,
        progress_stream=sys.stderr,
    )

    def execute(tool: str, parameters: Mapping[str, Any]) -> Any:
        if tool == "internal":
            command = list(parameters.get("command", []))
            if command[:2] == ["powdrr-lift", "show-proposed-pr"]:
                return {"id": "hello-world-live", "intent": "Add a second output line."}
            if command and command[0] in {"python", "python3"}:
                command[0] = sys.executable
            if command and command[0] == "pytest":
                command = [sys.executable, "-m", "pytest", *command[1:]]
            if command[:2] not in ([sys.executable, "-m"], ["ruff", "check"]):
                return {"returncode": 1, "stderr": "unsupported command", "stdout": ""}
            completed = subprocess.run(
                command, cwd=tmp_path, capture_output=True, text=True
            )
            return {
                "returncode": completed.returncode,
                "stdout": completed.stdout,
                "stderr": completed.stderr,
            }
        if tool == "gather_context":
            return {
                "types": parameters["types"],
                "feature_id": parameters.get("feature_id"),
            }
        if tool == "read_document":
            return (tmp_path / parameters["file_path"]).read_text(encoding="utf-8")
        if tool == "invoke_tool":
            return {"tool": parameters["tool"], "returncode": 0}
        if tool == "edit":
            path = tmp_path / parameters["file_path"]
            text = path.read_text(encoding="utf-8")
            for edit in parameters["edits"]:
                text = text.replace(edit["old_text"], edit["new_text"], 1)
            path.write_text(text, encoding="utf-8")
            return {"changed": True, "path": parameters["file_path"]}
        raise AssertionError(f"unexpected operation: {tool}")

    Evaluator.with_workrr(
        llm,
        StructuredToolExecutor(
            execute,
            available_paths=lambda: ["hello.py", "test_hello.py"],
        ),
        skills_dir=tmp_path,
    ).evaluate(
        document,
        {
            "work_item_name": "hello-world-live",
            "proposed_pr_id": "hello-world-live",
            "feature_description": "Print an additional line: Here I Am.",
        },
    )
    assert (
        tmp_path / "hello.py"
    ).read_text() == 'print("Hello, World")\nprint("Here I Am")\n'
