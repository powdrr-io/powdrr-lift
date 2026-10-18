from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from powdrr_lift.structrr.validation import DiscoveredValidationProfile
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


def test_generate_postrun_probes_excludes_unresolved_scenarios() -> None:
    client = _ProbeClient(
        {
            "probes": [
                {
                    "probe_id": "probe-1",
                    "scenario_id": "scenario-1",
                    "oracle_basis": "The instruction explicitly requires this result.",
                    "test_source": "def test_probe_result():\n    assert 2 + 2 == 4\n",
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
    assert client.messages is not None
    assert "candidate implementation" in client.messages[0]["content"]
    assert "scenario-2" not in client.messages[1]["content"]


@pytest.mark.parametrize(
    ("source", "expected_error"),
    [
        ("def test_not_a_probe():\n    assert True\n", "test_probe_"),
        ("def test_probe_broken():\n    assert True\n  assert False\n", "valid Python"),
        (
            "import subprocess\ndef test_probe_side_effect():\n    assert True\n",
            "disallowed system module",
        ),
        (
            "from pathlib import Path\n"
            "def test_probe_side_effect():\n    assert True\n",
            "disallowed system module",
        ),
        ("def test_probe_bad():\n    open('file', 'w')\n", "disallowed function"),
    ],
)
def test_generate_postrun_probes_rejects_unsafe_or_malformed_source(
    source: str, expected_error: str
) -> None:
    client = _ProbeClient(
        {
            "probes": [
                {
                    "probe_id": "probe-1",
                    "scenario_id": "scenario-1",
                    "oracle_basis": "explicit instruction behavior",
                    "test_source": source,
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
    profiles = (
        DiscoveredValidationProfile(
            name="pytest",
            command=(sys.executable, "-m", "pytest", "-q"),
            source="unit test",
            provider_name="pytest",
        ),
    )
    probes = (
        PostRunProbe(
            "good",
            "scenario-good",
            "explicit",
            "def test_probe_good():\n    assert 2 + 2 == 4\n",
        ),
        PostRunProbe(
            "bad",
            "scenario-bad",
            "explicit",
            "def test_probe_bad():\n    assert 2 + 2 == 5\n",
        ),
    )

    results = run_postrun_probes(probes, root=tmp_path, profiles=profiles)

    assert [item.status for item in results] == ["passed", "failed"]
    failures = probe_failures([item.to_data() for item in results])
    assert len(failures) == 1
    assert failures[0]["scenario_id"] == "scenario-bad"
    assert "assert 2 + 2 == 5" in failures[0]["reason"]
