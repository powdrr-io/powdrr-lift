from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from powdrr_lift.workrr.postrun_probes import (
    PostRunProbe,
    PostRunProbeResult,
    baseline_probe_failures,
    generate_postrun_probes,
    parse_generated_postrun_probe_source,
    probe_failures,
    run_postrun_probes,
)


class _ProbeClient:
    def __init__(self, response: dict[str, Any]) -> None:
        self.response = response
        self.messages: list[dict[str, str]] | None = None

    def complete_json(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        self.messages = messages
        return self.response


def _source(body: str = "assert module.public_api('value') == 'handled:value'") -> str:
    return (
        "import module\n\n"
        "def test_probe_selector():\n"
        '    """{"probe_id": "selector-result", '
        '"scenario_id": "selector", '
        '"oracle_basis": "The instruction defines this result."}"""\n'
        f"    {body}\n"
    )


def test_parse_generated_probe_source_preserves_complete_code_and_metadata() -> None:
    source = _source()
    probes = parse_generated_postrun_probe_source(
        source, ({"scenario_id": "selector"},)
    )

    assert len(probes) == 1
    assert probes[0].probe_id == "selector-result"
    assert probes[0].function_name == "test_probe_selector"
    assert probes[0].test_source == source


def test_probe_imports_inside_function_are_valid_python() -> None:
    source = _source(
        "import module\n    assert module.public_api('value') == 'handled:value'"
    )
    probes = parse_generated_postrun_probe_source(
        source, ({"scenario_id": "selector"},)
    )
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
            "metadata docstring",
        ),
    ],
)
def test_parser_rejects_invalid_or_unsafe_probe_code(
    source: str, expected_error: str
) -> None:
    with pytest.raises(ValueError, match=expected_error):
        parse_generated_postrun_probe_source(source, ({"scenario_id": "selector"},))


def test_probe_parser_rejects_unknown_scenario() -> None:
    with pytest.raises(ValueError, match="unknown scenario"):
        parse_generated_postrun_probe_source(_source(), ({"scenario_id": "other"},))


def test_generate_postrun_probes_requests_complete_executable_source() -> None:
    client = _ProbeClient({"source": _source()})
    probes = generate_postrun_probes(
        client,
        instruction="Add selector handling.",
        scenarios=(
            {"scenario_id": "selector"},
            {"scenario_id": "unresolved", "unresolved_dimensions": ["oracle"]},
        ),
    )
    assert len(probes) == 1
    assert probes[0].test_source == _source()
    assert client.messages is not None
    assert "complete Python module" in client.messages[0]["content"]


def test_run_postrun_probes_executes_each_function_from_same_module(
    tmp_path: Path,
) -> None:
    module = tmp_path / "module.py"
    module.write_text("def public_api(value): return 'handled:' + value\n")
    source = (
        "import module\n\n"
        "def test_probe_good():\n"
        '    """{"probe_id": "good", "scenario_id": "good", '
        '"oracle_basis": "explicit"}"""\n'
        "    assert module.public_api('value') == 'handled:value'\n\n"
        "def test_probe_bad():\n"
        '    """{"probe_id": "bad", "scenario_id": "bad", '
        '"oracle_basis": "explicit"}"""\n'
        "    assert module.public_api('value') == 'wrong'\n"
    )
    probes = parse_generated_postrun_probe_source(
        source, ({"scenario_id": "good"}, {"scenario_id": "bad"})
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


def test_postrun_probe_execution_reports_runtime_failure(tmp_path: Path) -> None:
    probe = PostRunProbe(
        "bad",
        "scenario",
        "explicit",
        _source("assert module.public_api('value') == 'handled:value'"),
        "test_probe_selector",
    )
    (tmp_path / "module.py").write_text("def public_api(value): return value\n")
    result = run_postrun_probes((probe,), root=tmp_path, profiles=())[0]
    assert result.status == "failed"
    assert "AssertionError" in result.output
