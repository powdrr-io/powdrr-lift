from __future__ import annotations

import ast
import sys
from pathlib import Path
from typing import Any

import pytest

from powdrr_lift.workrr.postrun_probes import (
    PostRunProbe,
    PostRunProbeResult,
    baseline_probe_failures,
    parse_generated_postrun_probe_source,
    probe_failures,
    run_postrun_probes,
    validate_probe_interaction_plan,
)


def _source(body: str = "assert module.public_api('value') == 'handled:value'") -> str:
    obligations: list[dict[str, Any]] = [
        {
            "obligation_id": "selector-obligation",
            "scenario_id": "selector",
            "category": "transformation",
            "rule": "other",
            "supported": True,
            "summary": "The selector returns the matching result.",
            "condition": "A selector matches a finding.",
            "operation": "Call the public API.",
            "expected": "The matching result is returned.",
            "operands": [],
            "unresolved_reason": "",
        }
    ]
    return (
        f"OBLIGATIONS = {obligations!r}\n"
        "INTERACTIONS = []\n"
        "PROBE_METADATA = {'test_probe_selector': "
        "{'probe_id': 'selector-result', 'scenario_id': 'selector', "
        "'obligation_id': 'selector-obligation', "
        "'oracle_basis': 'The instruction defines this result.'}}\n"
        "import module\n\n"
        "def test_probe_selector():\n"
        f"    {body}\n"
    )


def _parse(
    source: str,
    scenarios: tuple[dict[str, Any], ...],
    *,
    instruction: str = "Add selector handling.",
    instruction_ledger_clauses: tuple[dict[str, object], ...] = (),
) -> tuple[
    tuple[dict[str, Any], ...],
    tuple[dict[str, Any], ...],
    tuple[PostRunProbe, ...],
]:
    return parse_generated_postrun_probe_source(
        source,
        tuple(
            {
                **scenario,
                "evidence": scenario.get("evidence", [instruction]),
            }
            for scenario in scenarios
        ),
        instruction=instruction,
        repository_contract={},
        instruction_ledger_clauses=instruction_ledger_clauses,
    )


def test_parse_generated_probe_source_preserves_complete_code_and_metadata() -> None:
    source = _source()
    obligations, interactions, probes = _parse(source, ({"scenario_id": "selector"},))

    assert len(probes) == 1
    assert probes[0].probe_id == "selector-result"
    assert probes[0].function_name == "test_probe_selector"
    assert probes[0].test_source == source
    assert obligations[0]["category"] == "transformation"
    assert interactions == ()
    assert probes[0].obligation_id == "selector-obligation"


def test_probe_obligation_evidence_resolves_from_scenario_deterministically() -> None:
    obligations, _, _ = _parse(_source(), ({"scenario_id": "selector"},))
    assert obligations[0]["evidence"] == [
        {"source": "instruction", "quote": "Add selector handling."}
    ]


def test_supported_obligation_drops_empty_optional_operand_meaning() -> None:
    source = _source().replace(
        "'operands': []",
        "'operands': [{'role': 'input', 'name': 'state', 'meaning': ''}]",
    )

    obligations, _, _ = _parse(source, ({"scenario_id": "selector"},))

    assert obligations[0]["operands"] == [{"role": "input", "name": "state"}]


def test_supported_obligation_drops_unusable_operand_metadata() -> None:
    source = _source().replace(
        "'operands': []",
        "'operands': [{'role': 'invented', 'name': 'state'}, "
        "{'role': 'input'}, 'not-an-operand']",
    )

    obligations, _, probes = _parse(source, ({"scenario_id": "selector"},))

    assert obligations[0]["operands"] == []
    assert len(probes) == 1


def test_supported_obligation_drops_non_list_operand_metadata() -> None:
    source = _source().replace("'operands': []", "'operands': 'malformed'")

    obligations, _, probes = _parse(source, ({"scenario_id": "selector"},))

    assert obligations[0]["operands"] == []
    assert len(probes) == 1


def test_probe_obligation_evidence_uses_instruction_ledger_by_scenario_id() -> None:
    source = _source().replace(
        "'scenario_id': 'selector'", "'scenario_id': 'scenario:instruction-001'"
    )
    scenario = {
        "scenario_id": "scenario:instruction-001",
        "evidence": ["generated rationale is not a source quote"],
    }

    obligations, _, _ = _parse(
        source,
        (scenario,),
        instruction="Add selector handling.",
        instruction_ledger_clauses=(
            {"clause_id": "instruction-001", "text": "Add selector handling."},
        ),
    )

    assert obligations[0]["evidence"] == [
        {"source": "instruction", "quote": "Add selector handling."}
    ]


def test_probe_obligation_evidence_falls_back_to_ledger_source_span() -> None:
    instruction = (
        "Opening ( follows the table name on the same line; closing ) follows."
    )
    source = _source().replace(
        "'scenario_id': 'selector'",
        "'scenario_id': 'scenario:instruction-003'",
    )
    scenario = {
        "scenario_id": "scenario:instruction-003",
        "evidence": ["model-authored paraphrase is not a source quote"],
    }
    source_span = {"start": 0, "end": len(instruction)}

    obligations, _, _ = _parse(
        source,
        (scenario,),
        instruction=instruction,
        instruction_ledger_clauses=(
            {
                "clause_id": "instruction-003",
                "text": "Opening follows the table name on its line.",
                "source_span": source_span,
            },
        ),
    )

    assert obligations[0]["evidence"] == [
        {"source": "instruction", "quote": instruction}
    ]


def test_miniswe_cannot_override_deterministically_bound_evidence() -> None:
    source = _source().replace(
        "'operands': []",
        "'operands': [], 'evidence': "
        "[{'source': 'instruction', 'quote': 'fabricated'}]",
    )

    obligations, _, _ = _parse(source, ({"scenario_id": "selector"},))

    assert obligations[0]["evidence"] == [
        {"source": "instruction", "quote": "Add selector handling."}
    ]


def test_probe_imports_inside_function_are_valid_python() -> None:
    source = _source(
        "import module\n    assert module.public_api('value') == 'handled:value'"
    )
    _, _, probes = _parse(source, ({"scenario_id": "selector"},))
    assert len(probes) == 1


def test_probe_can_call_public_api_in_exception_handling_before_assertion() -> None:
    source = _source(
        "raised = False\n    try:\n        module.public_api('invalid')\n"
        "    except ValueError:\n        raised = True\n    assert raised"
    )

    _, _, probes = _parse(source, ({"scenario_id": "selector"},))

    assert len(probes) == 1


@pytest.mark.parametrize(
    ("source", "expected_error"),
    [
        ("def test_probe_bad(:\n    pass\n", "valid Python"),
        (
            _source("assert True"),
            "non-built-in software call",
        ),
        (
            _source("import subprocess\n    assert True"),
            "disallowed system module",
        ),
        (
            _source("import pytest\n    assert module.public_api('x')"),
            "must not depend on pytest",
        ),
        (
            "def test_probe_selector():\n    assert module.public_api('x')\n",
            "Python literals",
        ),
    ],
)
def test_parser_rejects_invalid_or_unsafe_probe_code(
    source: str, expected_error: str
) -> None:
    with pytest.raises(ValueError, match=expected_error):
        _parse(source, ({"scenario_id": "selector"},))


def test_probe_parser_rejects_unknown_scenario() -> None:
    with pytest.raises(ValueError, match="unknown scenario"):
        _parse(_source(), ({"scenario_id": "other"},))


def test_interaction_plan_requires_multiple_scenarios_and_exact_evidence() -> None:
    scenarios = (
        {
            "scenario_id": "special-value",
            "evidence": ["all values are selected;"],
        },
        {"scenario_id": "operator", "evidence": ["difference removes a value"]},
    )
    interaction = {
        "interaction_id": "special-value-with-operator",
        "scenario_ids": ["special-value", "operator"],
        "condition": "A special value is used as an operator operand.",
        "operation": "Apply the operator to the special value and a concrete value.",
        "expected": "The result contains only the values selected by the operation.",
        "evidence_quotes": ["all values are selected;", "difference removes a value"],
    }
    model_interaction = {
        **{
            key: value for key, value in interaction.items() if key != "evidence_quotes"
        },
    }

    validated = validate_probe_interaction_plan(
        [model_interaction],
        instruction="all values are selected; difference removes a value",
        scenarios=scenarios,
    )

    assert validated == (interaction,)
    invalid = {**model_interaction, "scenario_ids": ["special-value"]}
    with pytest.raises(ValueError, match="distinct scenarios"):
        validate_probe_interaction_plan(
            [invalid],
            instruction="all values are selected; difference removes a value",
            scenarios=scenarios,
        )


def test_unverifiable_interaction_does_not_discard_individual_probes() -> None:
    source = _source()
    source = source.replace(
        "INTERACTIONS = []",
        "INTERACTIONS = [{'interaction_id': 'weak-combination', "
        "'scenario_ids': ['selector', 'selector'], "
        "'condition': 'Both scenarios apply.', 'operation': 'Combine them.', "
        "'expected': 'Both outcomes hold.'}]",
    )
    rejected: list[dict[str, str]] = []

    obligations, interactions, probes = parse_generated_postrun_probe_source(
        source,
        ({"scenario_id": "selector", "evidence": ["select values"]},),
        instruction="select values",
        rejected_interactions=rejected,
    )

    assert obligations[0]["supported"] is True
    assert interactions == ()
    assert len(probes) == 1
    assert probes[0].interaction_ids == ()
    assert rejected == [
        {
            "interaction_id": "weak-combination",
            "error": "interaction 'weak-combination' must reference at least two "
            "distinct scenarios",
        }
    ]


def test_probe_suite_must_cover_each_required_interaction() -> None:
    instruction = "all values are selected; difference removes a value"
    scenarios = (
        {
            "scenario_id": "special-value",
            "evidence": ["all values are selected"],
        },
        {"scenario_id": "operator", "evidence": ["difference removes a value"]},
    )
    obligations: list[dict[str, Any]] = [
        {
            "obligation_id": f"obligation-{scenario_id}",
            "scenario_id": scenario_id,
            "category": "transformation",
            "rule": "other",
            "supported": True,
            "summary": "The selector result is observed.",
            "condition": "The API receives a selector.",
            "operation": "Call the public API.",
            "expected": "The selected result is returned.",
            "operands": [],
            "unresolved_reason": "",
        }
        for scenario_id in ("special-value", "operator")
    ]

    model_interaction = {
        "interaction_id": "special-value-with-operator",
        "scenario_ids": ["special-value", "operator"],
        "condition": "A special value is used as an operator operand.",
        "operation": "Apply the operator to the special value and a concrete value.",
        "expected": "The result contains only the values selected by the operation.",
    }

    def source(interaction_ids: list[str]) -> str:
        metadata_by_function = {}
        lines = [
            f"OBLIGATIONS = {obligations!r}",
            f"INTERACTIONS = {[model_interaction]!r}",
        ]
        for index, scenario_id in enumerate(("special-value", "operator")):
            metadata = {
                "probe_id": f"probe-{index}",
                "scenario_id": scenario_id,
                "obligation_id": f"obligation-{scenario_id}",
                "oracle_basis": "The instruction defines the interaction.",
                "interaction_ids": interaction_ids if index == 0 else [],
            }
            metadata_by_function[f"test_probe_{index}"] = metadata
            lines.extend(
                [
                    "",
                    f"def test_probe_{index}():",
                    "    assert module.public_api('value') == 'handled:value'",
                ]
            )
        lines.insert(2, f"PROBE_METADATA = {metadata_by_function!r}")
        lines.insert(3, "import module")
        return "\n".join(lines) + "\n"

    _, interactions, probes = parse_generated_postrun_probe_source(
        source(["special-value-with-operator"]),
        scenarios,
        instruction=instruction,
    )
    assert len(interactions) == 1
    assert probes[0].interaction_ids == ("special-value-with-operator",)

    with pytest.raises(ValueError, match="omitted required interactions"):
        parse_generated_postrun_probe_source(
            source([]),
            scenarios,
            instruction=instruction,
        )


def test_probe_parser_preserves_mapping_key_and_value_roles() -> None:
    source = _source()
    obligation_literal = source.split("OBLIGATIONS = ", 1)[1].split("\n", 1)[0]
    document = ast.literal_eval(obligation_literal)
    base = document[0]
    base.update(
        {
            "obligation_id": "rename-source-key",
            "scenario_id": "rename",
            "category": "validation",
            "rule": "reference_integrity",
            "summary": "A rename source exists.",
            "condition": "The source key is unknown.",
            "operation": "Create with the rename mapping.",
            "expected": "Creation rejects the unknown key.",
            "operands": [
                {
                    "name": "rename source",
                    "role": "mapping_key",
                    "meaning": "child field",
                }
            ],
        }
    )
    source = (
        source.replace(
            "OBLIGATIONS = " + obligation_literal,
            "OBLIGATIONS = " + repr(document),
        )
        .replace("'scenario_id': 'selector'", "'scenario_id': 'rename'")
        .replace(
            "'obligation_id': 'selector-obligation'",
            "'obligation_id': 'rename-source-key'",
        )
    )
    obligations, _, probes = parse_generated_postrun_probe_source(
        source,
        ({"scenario_id": "rename", "evidence": ["invalid rename keys"]},),
        instruction="invalid rename keys",
    )
    assert obligations[0]["operands"][0]["role"] == "mapping_key"
    assert probes[0].obligation_category == "validation"


def test_run_postrun_probes_executes_each_function_from_same_module(
    tmp_path: Path,
) -> None:
    module = tmp_path / "module.py"
    module.write_text("def public_api(value): return 'handled:' + value\n")
    obligation_data: list[dict[str, Any]] = [
        {
            "obligation_id": f"obligation-{scenario}",
            "scenario_id": scenario,
            "category": "other",
            "rule": "other",
            "supported": True,
            "summary": "Check the result.",
            "condition": "The API receives a value.",
            "operation": "Call the public API.",
            "expected": "The documented result is returned.",
            "operands": [],
            "unresolved_reason": "",
        }
        for scenario in ("good", "bad")
    ]
    source = (
        f"OBLIGATIONS = {obligation_data!r}\nINTERACTIONS = []\n"
        "PROBE_METADATA = {'test_probe_good': {'probe_id': 'good', "
        "'scenario_id': 'good', "
        "'obligation_id': 'obligation-good', 'oracle_basis': 'explicit'}, "
        "'test_probe_bad': {'probe_id': 'bad', 'scenario_id': 'bad', "
        "'obligation_id': 'obligation-bad', 'oracle_basis': 'explicit'}}\n"
        "import module\n\n"
        "def test_probe_good():\n"
        "    assert module.public_api('value') == 'handled:value'\n\n"
        "def test_probe_bad():\n    assert module.public_api('value') == 'wrong'\n"
    )
    _, _, probes = parse_generated_postrun_probe_source(
        source,
        (
            {"scenario_id": "good", "evidence": ["explicit"]},
            {"scenario_id": "bad", "evidence": ["explicit"]},
        ),
        instruction="explicit",
    )

    results = run_postrun_probes(probes, root=tmp_path, profiles=())

    assert [item.status for item in results] == ["passed", "failed"]
    assert all(item.command[0] == sys.executable for item in results)
    baseline_problems = baseline_probe_failures(results)
    assert len(baseline_problems) == 1
    assert "good" in baseline_problems[0]
    failures = probe_failures([item.to_data() for item in results])
    assert len(failures) == 1
    assert failures[0]["scenario_id"] == "bad"
    assert "assert module.public_api('value') == 'wrong'" in failures[0]["reason"]


def test_baseline_accepts_assertion_type_and_import_failures() -> None:
    results = (
        PostRunProbeResult("pass", "scenario", "passed"),
        PostRunProbeResult("value", "scenario", "failed", output="ValueError"),
        PostRunProbeResult("expected", "scenario", "failed", output="AssertionError"),
        PostRunProbeResult("type", "scenario", "failed", output="TypeError"),
        PostRunProbeResult("import", "scenario", "failed", output="ImportError"),
        PostRunProbeResult(
            "missing-module", "scenario", "failed", output="ModuleNotFoundError"
        ),
    )
    problems = baseline_probe_failures(results)
    assert len(problems) == 2
    assert any("pass" in item for item in problems)
    assert any("value" in item for item in problems)


def test_baseline_classifies_the_terminal_exception_not_traceback_text() -> None:
    results = (
        PostRunProbeResult(
            "expected",
            "scenario",
            "failed",
            output="AssertionError mentioned in source\nTypeError: unexpected keyword",
        ),
        PostRunProbeResult(
            "unexpected",
            "scenario",
            "failed",
            output="TypeError mentioned in source\nValueError: bad fixture",
        ),
    )

    problems = baseline_probe_failures(results)

    assert len(problems) == 1
    assert "unexpected" in problems[0]


def test_unavailable_postrun_probe_is_a_repair_finding() -> None:
    failures = probe_failures(
        [
            {
                "probe_id": "public-api",
                "scenario_id": "public-api-works",
                "status": "unavailable",
                "error": "probe runner could not start",
            }
        ]
    )

    assert len(failures) == 1
    assert failures[0]["scenario_id"] == "public-api-works"


def test_postrun_probe_execution_reports_runtime_failure(tmp_path: Path) -> None:
    probe = PostRunProbe(
        "bad",
        "scenario",
        "explicit",
        _source("assert module.public_api('value') == 'handled:value'"),
        "test_probe_selector",
        "selector-obligation",
        "transformation",
        "other",
    )
    (tmp_path / "module.py").write_text("def public_api(value): return value\n")
    result = run_postrun_probes((probe,), root=tmp_path, profiles=())[0]
    assert result.status == "failed"
    assert "AssertionError" in result.output
