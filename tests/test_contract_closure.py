from __future__ import annotations

import json
from pathlib import Path

import pytest

from powdrr_lift.core.contract_closure import (
    ContractClosureError,
    RepositoryEvidence,
    collect_python_evidence,
    compile_contract_closure,
    render_contract_closure,
    validate_contract_closure,
)
from powdrr_lift.core.implementation_packet import (
    ImplementationPacket,
    compile_implementation_packet,
)


def test_python_evidence_records_nested_signatures_from_tracked_paths(
    tmp_path: Path,
) -> None:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "validation").mkdir()
    (tmp_path / "solutions").mkdir()
    (tmp_path / "benchmarks").mkdir()
    (tmp_path / "pkg" / "service.py").write_text(
        "class Service:\n"
        "    def run(self, item: str, *, strict: bool = False) -> str:\n"
        "        return self._run(item)\n"
        "\n"
        "    def _run(self, item: str) -> str:\n"
        "        return item\n",
        encoding="utf-8",
    )
    (tmp_path / "tests" / "test_service.py").write_text(
        "def test_run():\n    assert True\n", encoding="utf-8"
    )
    (tmp_path / "validation" / "secret.py").write_text(
        "def hidden_expected_behavior():\n    return True\n", encoding="utf-8"
    )
    (tmp_path / "solutions" / "reference_solution.py").write_text(
        "def gold_answer():\n    return True\n", encoding="utf-8"
    )
    (tmp_path / "benchmarks" / "verifier.py").write_text(
        "def hidden_assertion():\n    return True\n", encoding="utf-8"
    )

    evidence = collect_python_evidence(
        tmp_path,
        base_commit="git:base",
        tracked_paths=("pkg/service.py",),
    )
    assert RepositoryEvidence.from_data(evidence.to_data()) == evidence

    assert {item.name for item in evidence.records} == {"Service", "run", "_run"}
    run = next(item for item in evidence.records if item.name == "run")
    assert "strict: bool=False" in run.signature
    assert run.calls == ("self._run",)
    assert {item.path for item in evidence.records} == {"pkg/service.py"}


def test_contract_closure_binds_scenario_to_source_and_renders_evidence(
    tmp_path: Path,
) -> None:
    source = tmp_path / "service.py"
    source.write_text(
        "class Service:\n"
        "    def run(self, item: str) -> str:\n"
        "        return self._run(item)\n"
        "\n"
        "    def _run(self, item: str) -> str:\n"
        "        return item\n",
        encoding="utf-8",
    )
    evidence = collect_python_evidence(
        tmp_path, base_commit="git:base", tracked_paths=("service.py",)
    )
    scenario = {
        "scenario_id": "scenario:service-run",
        "subject": "Service.run(item)",
        "given": "a valid item",
        "when": "Service.run(item) is called",
        "then": "the returned value contains the item",
        "dimensions": {"normal_result": "value"},
        "evidence": ["source clause 1"],
        "validator": "focused behavior check",
        "assumptions": [],
    }

    closure = compile_contract_closure(
        [scenario], evidence, design_revision="sha256:design"
    )
    validated = validate_contract_closure(closure, evidence)
    rendered = render_contract_closure(validated)

    assert closure["operations"][0]["surface_search"]["result"] == "matched"
    assert any(
        item["qualified_name"].endswith("Service.run")
        for item in closure["operations"][0]["surfaces"]
    )
    assert "def run(self, item: str) -> str" in rendered
    assert "Static call lists can omit dynamic dispatch" in rendered


def test_contract_closure_allows_new_operations_and_rejects_stale_evidence(
    tmp_path: Path,
) -> None:
    evidence = collect_python_evidence(
        tmp_path, base_commit="git:base", tracked_paths=()
    )
    scenario = {
        "scenario_id": "scenario:new-api",
        "subject": "NewService.create(value)",
        "given": "a value",
        "when": "the new operation is called",
        "then": "a resource is returned",
        "dimensions": {},
        "evidence": ["source clause 2"],
        "validator": "behavior check",
    }
    closure = compile_contract_closure(
        [scenario], evidence, design_revision="sha256:design"
    )
    assert closure["operations"][0]["surface_search"]["result"] == "new_or_unmatched"
    assert validate_contract_closure(closure, evidence) == closure

    changed = type(evidence)("git:other", evidence.revision, evidence.records)
    with pytest.raises(ContractClosureError, match="base commit is stale"):
        validate_contract_closure(closure, changed)


def test_contract_closure_rejects_forged_evidence_references(tmp_path: Path) -> None:
    source = tmp_path / "service.py"
    source.write_text("def run():\n    return None\n", encoding="utf-8")
    evidence = collect_python_evidence(
        tmp_path, base_commit="git:base", tracked_paths=("service.py",)
    )
    closure = compile_contract_closure(
        [
            {
                "scenario_id": "scenario:run",
                "subject": "run()",
                "given": "input",
                "when": "called",
                "then": "returns",
                "dimensions": {},
                "evidence": ["source"],
                "validator": "check",
            }
        ],
        evidence,
        design_revision="sha256:design",
    )
    closure["operations"][0]["surfaces"][0]["evidence_ref"] = "invented"
    # Recompute neither record nor evidence fingerprints: forged references
    # must be rejected before a worker prompt can be rendered.
    with pytest.raises(ContractClosureError, match="unknown repository evidence"):
        validate_contract_closure(closure, evidence)


def test_implementation_packet_renders_closure_without_validation_metadata(
    tmp_path: Path,
) -> None:
    source = tmp_path / "service.py"
    source.write_text("def run(value):\n    return value\n", encoding="utf-8")
    evidence = collect_python_evidence(
        tmp_path, base_commit="git:base", tracked_paths=("service.py",)
    )
    scenario = {
        "scenario_id": "scenario:run",
        "subject": "run(value)",
        "given": "a value",
        "when": "run(value) is called",
        "then": "the value is returned",
        "dimensions": {
            "normal_result": "value",
            "error_behavior": "not_applicable: source does not describe errors",
            "continuation": "not_applicable: source does not describe continuation",
            "unsupported_behavior": (
                "not_applicable: source does not describe unsupported inputs"
            ),
            "cancellation_cleanup": (
                "not_applicable: source does not describe cancellation"
            ),
            "compatibility": "not_applicable: source does not describe compatibility",
            "negative_boundaries": (
                "not_applicable: source does not describe negative inputs"
            ),
        },
        "evidence": ["instruction clause 1"],
        "validator": "pytest -q hidden_test.py",
    }
    closure = compile_contract_closure(
        [scenario], evidence, design_revision="sha256:design"
    )
    packet = compile_implementation_packet(
        objective="Implement run(value)",
        obligations=("Return the input value.",),
        required_tests=({"description": "the input value is returned"},),
        allowed_paths=("src/",),
        validation_profiles=("pytest",),
        behavior_scenarios=(scenario,),
        contract_closure=closure,
    )
    packet = ImplementationPacket.from_data(packet.to_data())
    task_packet = packet.for_task(
        objective="Implement run(value)",
        acceptance_criteria=("the input value is returned",),
    )

    rendered = task_packet.render()
    assert "hidden_test.py" not in json.dumps(closure)
    assert "Repository evidence relevant to the requested behavior" in rendered
    assert "def run(value)" in rendered
    assert "hidden_test.py" not in rendered
    assert "validation_profiles" not in rendered
