from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from powdrr_lift.workrr.postrun_probes import (
    PostRunProbe,
    generate_postrun_probes,
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


class _ProbeRetryClient:
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = list(responses)
        self.messages: list[list[dict[str, str]]] = []

    def complete_json(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        self.messages.append(messages)
        return self.responses.pop(0)


def test_generate_postrun_probes_excludes_unresolved_scenarios() -> None:
    client = _ProbeClient(
        {
            "probes": [
                {
                    "probe_id": "probe-1",
                    "scenario_id": "scenario-1",
                    "oracle_basis": "The instruction explicitly requires this result.",
                    "imports": ["math"],
                    "body": ["assert math.factorial(2) == 2"],
                }
            ]
        }
    )
    probes = generate_postrun_probes(
        client,
        instruction="Return the accumulated result.",
        scenarios=(
            {"scenario_id": "scenario-1", "given": {"x": 1}, "then": {"x": 1}},
            {
                "scenario_id": "scenario-2",
                "unresolved_dimensions": ["overlap"],
            },
        ),
    )

    assert [probe.scenario_id for probe in probes] == ["scenario-1"]
    assert probes[0].test_source == (
        "import math\n\n"
        "def test_probe_generated():\n"
        "    assert math.factorial(2) == 2\n"
    )
    assert client.messages is not None
    assert "candidate implementation" in client.messages[0]["content"]
    assert "scenario-2" not in client.messages[1]["content"]


def test_generate_postrun_probes_repairs_invalid_python_once() -> None:
    valid_probe = {
        "probe_id": "probe-1",
        "scenario_id": "scenario-1",
        "oracle_basis": "The instruction explicitly requires this result.",
        "imports": ["math"],
        "body": ["assert math.factorial(2) == 2"],
    }
    invalid_probe = {
        **valid_probe,
        "probe_id": "probe-2",
        "body": ["assert math.factorial(2) == 2\n  assert False"],
    }
    corrected_probe = {
        **invalid_probe,
        "body": ["assert math.factorial(2) == 2"],
    }
    client = _ProbeRetryClient(
        [
            {"probes": [valid_probe, invalid_probe]},
            {"probes": [corrected_probe]},
        ]
    )

    probes = generate_postrun_probes(
        client,
        instruction="Return the accumulated result.",
        scenarios=({"scenario_id": "scenario-1"},),
    )

    assert len(client.messages) == 2
    feedback = client.messages[1][-1]["content"]
    assert "validation issues" in feedback
    assert "probe-2" in feedback
    assert [probe.probe_id for probe in probes] == ["probe-1", "probe-2"]
    assert all("assert False" not in probe.test_source for probe in probes)


def test_probe_syntax_repair_cannot_change_imports_or_provenance() -> None:
    invalid_probe = {
        "probe_id": "probe-1",
        "scenario_id": "scenario-1",
        "oracle_basis": "The instruction explicitly requires this result.",
        "imports": ["math"],
        "body": ["assert math.factorial(2) == 2\n  assert False"],
    }
    altered_probe = {
        **invalid_probe,
        "imports": ["json"],
        "body": ["assert json.loads('2') == 2"],
    }
    client = _ProbeRetryClient(
        [{"probes": [invalid_probe]}, {"probes": [altered_probe]}]
    )

    with pytest.raises(ValueError, match="changed probe provenance or imports"):
        generate_postrun_probes(
            client,
            instruction="Return the accumulated result.",
            scenarios=({"scenario_id": "scenario-1"},),
        )


def test_probe_generation_retries_when_probe_has_no_assertion() -> None:
    incomplete_probe = {
        "probe_id": "probe-1",
        "scenario_id": "scenario-1",
        "oracle_basis": "The instruction explicitly requires this result.",
        "imports": ["math"],
        "body": ["value = math.factorial(2)"],
    }
    corrected_probe = {
        **incomplete_probe,
        "body": ["assert math.factorial(2) == 2"],
    }
    client = _ProbeRetryClient(
        [{"probes": [incomplete_probe]}, {"probes": [corrected_probe]}]
    )

    probes = generate_postrun_probes(
        client,
        instruction="Return the accumulated result.",
        scenarios=({"scenario_id": "scenario-1"},),
    )

    assert len(client.messages) == 2
    assert "observable assertion" in client.messages[1][-1]["content"]
    assert len(probes) == 1
    assert "assert math.factorial(2) == 2" in probes[0].test_source


@pytest.mark.parametrize(
    ("imports", "body", "expected_error"),
    [
        ([], ["answer = 42"], "observable assertion"),
        ([], ["assert True\n  assert False"], "valid Python"),
        (
            ["subprocess"],
            ["assert subprocess.run(['true']).returncode == 0"],
            "disallowed system module",
        ),
        (
            ["pathlib"],
            ["assert pathlib.Path('.').exists()"],
            "disallowed system module",
        ),
        ([], ["assert open('file', 'w')"], "disallowed function"),
        (
            ["bandit"],
            ["assert hasattr(bandit, 'process')"],
            "non-built-in software call",
        ),
    ],
)
def test_generate_postrun_probes_rejects_unsafe_or_malformed_source(
    imports: list[str], body: list[str], expected_error: str
) -> None:
    client = _ProbeClient(
        {
            "probes": [
                {
                    "probe_id": "probe-1",
                    "scenario_id": "scenario-1",
                    "oracle_basis": "explicit instruction behavior",
                    "imports": imports,
                    "body": body,
                }
            ]
        }
    )

    with pytest.raises(ValueError, match=expected_error):
        generate_postrun_probes(
            client,
            instruction="Do a thing.",
            scenarios=({"scenario_id": "scenario-1"},),
        )


def test_run_postrun_probes_executes_each_probe_and_returns_repair_findings(
    tmp_path: Path,
) -> None:
    probes = (
        PostRunProbe(
            "good",
            "scenario-good",
            "explicit",
            "import math\ndef test_probe_good():\n    assert math.factorial(2) == 2\n",
        ),
        PostRunProbe(
            "bad",
            "scenario-bad",
            "explicit",
            "import math\ndef test_probe_bad():\n    assert math.factorial(2) == 5\n",
        ),
    )

    results = run_postrun_probes(probes, root=tmp_path, profiles=())

    assert [item.status for item in results] == ["passed", "failed"]
    assert all(item.command[0] == sys.executable for item in results)
    failures = probe_failures([item.to_data() for item in results])
    assert len(failures) == 1
    assert failures[0]["scenario_id"] == "scenario-bad"
    assert "assert math.factorial(2) == 5" in failures[0]["reason"]
