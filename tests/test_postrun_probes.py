from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from powdrr_lift.workrr.postrun_probes import (
    PostRunProbe,
    PostRunProbeResult,
    baseline_probe_failures,
    generate_probe_interaction_plan,
    parse_generated_postrun_probe_source,
    probe_failures,
    resolve_probe_interaction_plan,
    run_postrun_probes,
    validate_probe_interaction_plan,
)


def _source(body: str = "assert module.public_api('value') == 'handled:value'") -> str:
    obligations = [
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
            "evidence": [{"source": "instruction", "quote": "Add selector handling."}],
            "unresolved_reason": "",
        }
    ]
    return (
        f'"""{json.dumps({"obligations": obligations})}"""\n'
        "import module\n\n"
        "def test_probe_selector():\n"
        '    """{"probe_id": "selector-result", '
        '"scenario_id": "selector", '
        '"obligation_id": "selector-obligation", '
        '"oracle_basis": "The instruction defines this result."}"""\n'
        f"    {body}\n"
    )


def _parse(
    source: str, scenarios: tuple[dict[str, object], ...]
) -> tuple[tuple[dict[str, Any], ...], tuple[PostRunProbe, ...]]:
    return parse_generated_postrun_probe_source(
        source,
        scenarios,
        instruction="Add selector handling.",
        repository_contract={},
    )


def test_parse_generated_probe_source_preserves_complete_code_and_metadata() -> None:
    source = _source()
    obligations, probes = _parse(source, ({"scenario_id": "selector"},))

    assert len(probes) == 1
    assert probes[0].probe_id == "selector-result"
    assert probes[0].function_name == "test_probe_selector"
    assert probes[0].test_source == source
    assert obligations[0]["category"] == "transformation"
    assert probes[0].obligation_id == "selector-obligation"


def test_probe_imports_inside_function_are_valid_python() -> None:
    source = _source(
        "import module\n    assert module.public_api('value') == 'handled:value'"
    )
    _, probes = _parse(source, ({"scenario_id": "selector"},))
    assert len(probes) == 1


def test_probe_can_call_public_api_in_exception_handling_before_assertion() -> None:
    source = _source(
        "raised = False\n    try:\n        module.public_api('invalid')\n"
        "    except ValueError:\n        raised = True\n    assert raised"
    )

    _, probes = _parse(source, ({"scenario_id": "selector"},))

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
            "module docstring",
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
        {"scenario_id": "special-value"},
        {"scenario_id": "operator"},
    )
    interaction = {
        "interaction_id": "special-value-with-operator",
        "scenario_ids": ["special-value", "operator"],
        "condition": "A special value is used as an operator operand.",
        "operation": "Apply the operator to the special value and a concrete value.",
        "expected": "The result contains only the values selected by the operation.",
        "evidence_quotes": ["all values are selected", "difference removes a value"],
    }

    validated = validate_probe_interaction_plan(
        [interaction],
        instruction="all values are selected; difference removes a value",
        scenarios=scenarios,
    )

    assert validated == (interaction,)
    invalid = {**interaction, "evidence_quotes": ["invented", "difference"]}
    with pytest.raises(ValueError, match="exact quotes"):
        validate_probe_interaction_plan(
            [invalid],
            instruction="all values are selected; difference removes a value",
            scenarios=scenarios,
        )


def test_interaction_plan_is_generated_as_structured_metadata() -> None:
    class _PlanningClient:
        def complete_json(
            self, messages: list[dict[str, str]], **_: Any
        ) -> dict[str, Any]:
            assert "interactions" in messages[0]["content"]
            if "proposed_interactions" in messages[1]["content"]:
                return {
                    "interactions": [
                        {
                            "interaction_id": "special-value-with-operator",
                            "scenario_ids": ["special-value", "operator"],
                            "condition": "A special value is an operand.",
                            "operation": "Evaluate the expression.",
                            "expected": "Only selected results are suppressed.",
                            "evidence_quotes": [
                                "all values",
                                "operators are supported",
                            ],
                        }
                    ]
                }
            return {"interactions": []}

    interactions = generate_probe_interaction_plan(
        _PlanningClient(),
        instruction="all values are selected; operators are supported",
        scenarios=(
            {"scenario_id": "special-value"},
            {"scenario_id": "operator"},
        ),
    )

    assert interactions[0]["interaction_id"] == "special-value-with-operator"


def test_interaction_plan_result_distinguishes_empty_from_failure() -> None:
    class _EmptyClient:
        def complete_json(self, *_: Any, **__: Any) -> dict[str, Any]:
            return {"interactions": []}

    class _InvalidClient:
        def complete_json(self, *_: Any, **__: Any) -> dict[str, Any]:
            return {
                "interactions": [
                    {
                        "interaction_id": "bad-interaction",
                        "scenario_ids": ["special-value", "operator"],
                        "condition": "The requirements are combined.",
                        "operation": "Apply the operation.",
                        "expected": "The combined result is returned.",
                        "evidence_quotes": ["invented quote", "another invented quote"],
                    }
                ]
            }

    scenarios = (
        {"scenario_id": "special-value"},
        {"scenario_id": "operator"},
    )
    instruction = "Special values are operands. Operators combine values."

    empty = resolve_probe_interaction_plan(
        _EmptyClient(),
        instruction=instruction,
        scenarios=scenarios,
    )
    failed = resolve_probe_interaction_plan(
        _InvalidClient(),
        instruction=instruction,
        scenarios=scenarios,
    )
    unconfigured = resolve_probe_interaction_plan(
        None, instruction=instruction, scenarios=scenarios
    )

    assert empty.status == "empty"
    assert empty.interactions == ()
    assert empty.error is None
    assert failed.status == "failed"
    assert failed.interactions == ()
    assert failed.error is not None and "exact quotes" in failed.error
    assert unconfigured.status == "not_configured"
    assert unconfigured.error is None


def test_probe_suite_must_cover_each_required_interaction() -> None:
    instruction = "all values are selected; difference removes a value"
    scenarios = (
        {"scenario_id": "special-value"},
        {"scenario_id": "operator"},
    )
    interaction = {
        "interaction_id": "special-value-with-operator",
        "scenario_ids": ["special-value", "operator"],
        "condition": "A special value is used as an operator operand.",
        "operation": "Apply the operator to the special value and a concrete value.",
        "expected": "The result contains only the values selected by the operation.",
        "evidence_quotes": ["all values are selected", "difference removes a value"],
    }
    obligations = [
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
            "evidence": [{"source": "instruction", "quote": "all values are selected"}],
            "unresolved_reason": "",
        }
        for scenario_id in ("special-value", "operator")
    ]

    def source(interaction_ids: list[str]) -> str:
        lines = [
            f'"""{json.dumps({"obligations": obligations})}"""',
            "import module",
        ]
        for index, scenario_id in enumerate(("special-value", "operator")):
            metadata = {
                "probe_id": f"probe-{index}",
                "scenario_id": scenario_id,
                "obligation_id": f"obligation-{scenario_id}",
                "oracle_basis": "The instruction defines the interaction.",
                "interaction_ids": interaction_ids if index == 0 else [],
            }
            lines.extend(
                [
                    "",
                    f"def test_probe_{index}():",
                    f'    """{json.dumps(metadata)}"""',
                    "    assert module.public_api('value') == 'handled:value'",
                ]
            )
        return "\n".join(lines) + "\n"

    interactions = validate_probe_interaction_plan(
        [interaction], instruction=instruction, scenarios=scenarios
    )
    _, probes = parse_generated_postrun_probe_source(
        source(["special-value-with-operator"]),
        scenarios,
        instruction=instruction,
        interaction_obligations=interactions,
    )
    assert probes[0].interaction_ids == ("special-value-with-operator",)

    with pytest.raises(ValueError, match="omitted required interactions"):
        parse_generated_postrun_probe_source(
            source([]),
            scenarios,
            instruction=instruction,
            interaction_obligations=interactions,
        )


def test_probe_parser_preserves_mapping_key_and_value_roles() -> None:
    source = _source()
    document = json.loads(source.split('"""', 2)[1])
    base = document["obligations"][0]
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
            "evidence": [{"source": "instruction", "quote": "invalid rename keys"}],
        }
    )
    source = source.replace(
        '"obligation_id": "selector-obligation"',
        '"obligation_id": "rename-source-key"',
    ).replace('"scenario_id": "selector"', '"scenario_id": "rename"')
    module_doc = json.dumps(document)
    first = source.index('"""')
    last = source.index('"""', first + 3)
    source = source[: first + 3] + module_doc + source[last:]
    source = source.replace(
        '"obligation_id": "selector-obligation"', '"obligation_id": "rename-source-key"'
    )
    obligations, probes = parse_generated_postrun_probe_source(
        source,
        ({"scenario_id": "rename"},),
        instruction="invalid rename keys",
    )
    assert obligations[0]["operands"][0]["role"] == "mapping_key"
    assert probes[0].obligation_category == "validation"


def test_run_postrun_probes_executes_each_function_from_same_module(
    tmp_path: Path,
) -> None:
    module = tmp_path / "module.py"
    module.write_text("def public_api(value): return 'handled:' + value\n")
    source = (
        '"""'
        + json.dumps(
            {
                "obligations": [
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
                        "evidence": [{"source": "instruction", "quote": "explicit"}],
                        "unresolved_reason": "",
                    }
                    for scenario in ("good", "bad")
                ]
            }
        )
        + '"""\nimport module\n\n'
        "def test_probe_good():\n"
        '    """{"probe_id": "good", "scenario_id": "good", '
        '"obligation_id": "obligation-good", '
        '"oracle_basis": "explicit"}"""\n'
        "    assert module.public_api('value') == 'handled:value'\n\n"
        "def test_probe_bad():\n"
        '    """{"probe_id": "bad", "scenario_id": "bad", '
        '"obligation_id": "obligation-bad", '
        '"oracle_basis": "explicit"}"""\n'
        "    assert module.public_api('value') == 'wrong'\n"
    )
    _, probes = parse_generated_postrun_probe_source(
        source,
        ({"scenario_id": "good"}, {"scenario_id": "bad"}),
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


def test_baseline_requires_assertion_failure_not_a_pass_or_execution_error() -> None:
    results = (
        PostRunProbeResult("pass", "scenario", "passed"),
        PostRunProbeResult("import", "scenario", "failed", output="ImportError"),
        PostRunProbeResult("expected", "scenario", "failed", output="AssertionError"),
    )
    problems = baseline_probe_failures(results)
    assert len(problems) == 2
    assert any("pass" in item for item in problems)
    assert any("import" in item for item in problems)


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
