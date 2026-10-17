from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from powdrr_lift.core.behavior_contract import BEHAVIOR_DIMENSIONS
from powdrr_lift.workrr import probe_repair


class RepairClient:
    def __init__(self, revisions: list[dict[str, str]]) -> None:
        self.revisions = revisions
        self.messages: list[dict[str, str]] | None = None

    def complete_json(
        self,
        messages: list[dict[str, str]],
        *,
        response_schema: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.messages = messages
        assert response_schema is not None
        return {"revisions": self.revisions}


def _scenario() -> dict[str, Any]:
    return {
        "schema_version": "behavior-scenario-v1",
        "scenario_id": "instruction-001",
        "subject": "incremental GraphQL response",
        "given": (
            "The implementation is evaluated against: "
            "Deferred fields merge at their path."
        ),
        "when": "The instruction's behavior is exercised",
        "then": "Deferred fields merge at their path.",
        "dimensions": {name: "not_applicable" for name in BEHAVIOR_DIMENSIONS},
        "evidence": ["instruction-001"],
        "validator": "pytest -q tests/test_incremental.py",
        "faithfulness_ref": {
            "artifact_path": (
                "/run/semantic-contracts/instruction-001/scenario-faithfulness.json"
            ),
            "fingerprint": "sha256:faithfulness",
        },
    }


def _revision() -> dict[str, str]:
    return {
        "scenario_id": "instruction-001",
        "given": "An initial payload contains a user object without an email field.",
        "when": "The iterator processes a deferred payload at the user path.",
        "then": "The accumulated user object contains the deferred email field.",
    }


def test_probe_repair_revises_only_flagged_scenario_after_faithfulness(
    monkeypatch: Any, tmp_path: Path
) -> None:
    client = RepairClient([_revision()])
    checked: list[tuple[Mapping[str, Any], Mapping[str, Any]]] = []

    def accept(
        original: Mapping[str, Any], candidate: Mapping[str, Any], **_: Any
    ) -> tuple[bool, dict[str, Any]]:
        checked.append((original, candidate))
        return True, {"scenario_id": "instruction-001", "status": "accepted"}

    monkeypatch.setattr(probe_repair, "_check_revised_scenario_faithfulness", accept)
    revised, artifact = probe_repair.repair_behavior_probes(
        client,
        instruction="Deferred fields merge at the path given.",
        scenarios=(_scenario(),),
        contract_directory=tmp_path,
        ledger_clauses=(),
        faithfulness_client=client,
        repository_contract={"records": []},
    )

    assert len(checked) == 1
    assert revised[0]["given"] == _revision()["given"]
    assert revised[0]["subject"] == _scenario()["subject"]
    assert artifact["status"] == "repaired"
    assert artifact["original_scenarios"] == [_scenario()]
    assert artifact["used_scenarios"] == list(revised)
    assert artifact["initial_findings"]
    assert artifact["final_findings"] == []
    assert "Do not add capabilities" in str(client.messages)


def test_probe_repair_discards_candidate_rejected_by_faithfulness(
    monkeypatch: Any, tmp_path: Path
) -> None:
    client = RepairClient([_revision()])
    monkeypatch.setattr(
        probe_repair,
        "_check_revised_scenario_faithfulness",
        lambda *_args, **_kwargs: (
            False,
            {"scenario_id": "instruction-001", "status": "rejected"},
        ),
    )

    revised, artifact = probe_repair.repair_behavior_probes(
        client,
        instruction="Deferred fields merge at the path given.",
        scenarios=(_scenario(),),
        contract_directory=tmp_path,
        ledger_clauses=(),
        faithfulness_client=client,
    )

    assert revised == (_scenario(),)
    assert artifact["status"] == "partial"
    assert artifact["used_scenarios"] == [_scenario()]
    assert artifact["final_findings"] == artifact["initial_findings"]
    assert artifact["faithfulness_reviews"][0]["status"] == "rejected"


def test_probe_repair_fails_open_when_repair_response_is_malformed(
    tmp_path: Path,
) -> None:
    client = RepairClient([])

    revised, artifact = probe_repair.repair_behavior_probes(
        client,
        instruction="Deferred fields merge at the path given.",
        scenarios=(_scenario(),),
        contract_directory=tmp_path,
        ledger_clauses=(),
        faithfulness_client=client,
    )

    assert revised == (_scenario(),)
    assert artifact["status"] == "unavailable"
    assert "omitted" in artifact["error"]


def test_faithfulness_recheck_accepts_canonicalized_original_scenario(
    monkeypatch: Any, tmp_path: Path
) -> None:
    contract_dir = tmp_path / "contract-1"
    contract_dir.mkdir()
    (contract_dir / "partial-contract.json").write_text("{}", encoding="utf-8")
    artifact_path = tmp_path / "scenario-faithfulness.json"
    artifact_body = {
        "source_ref": "contract-1",
        "contract_fingerprint": "sha256:contract",
        "scenario_fingerprint": "sha256:earlier-canonical-form",
    }
    fingerprint = probe_repair.content_fingerprint(artifact_body)
    artifact_path.write_text(
        json.dumps({**artifact_body, "fingerprint": fingerprint}), encoding="utf-8"
    )

    class Contract:
        source_ref = "contract-1"
        fingerprint = "sha256:contract"

        @classmethod
        def from_data(cls, _: Mapping[str, Any]) -> Contract:
            return cls()

    monkeypatch.setattr(probe_repair, "PartialSemanticContract", Contract)
    monkeypatch.setattr(
        probe_repair,
        "prepare_scenario_claim_reviews",
        lambda *_args, **_kwargs: {"requests": []},
    )
    monkeypatch.setattr(
        probe_repair,
        "finalize_scenario_claim_reviews",
        lambda *_args, **_kwargs: {"accepted": True},
    )

    accepted, review = probe_repair._check_revised_scenario_faithfulness(
        {
            "scenario_id": "instruction-001",
            "faithfulness_ref": {
                "artifact_path": str(artifact_path),
                "fingerprint": fingerprint,
            },
        },
        {"scenario_id": "instruction-001", "then": "Revised supported claim."},
        contract_directory=tmp_path,
        ledger_clauses=(),
        client=None,
    )

    assert accepted is True
    assert review["status"] == "accepted"
