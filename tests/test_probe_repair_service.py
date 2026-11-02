from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from powdrr_contracts.models import OperationRequest
from powdrr_local import JsonServiceBridge, LocalPowdrrService
from powdrr_service.behavior_contract import BEHAVIOR_DIMENSIONS
from powdrr_service.decision_obligation import content_fingerprint
from powdrr_service.verification import probe_repair

from powdrr_lift.workrr.feature_endpoint import _load_probe_faithfulness_evidence


class RepairProvider:
    def complete_json(
        self,
        messages: list[dict[str, str]],
        *,
        response_schema: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        assert response_schema is not None
        properties = response_schema.get("properties", {})
        if "category" in properties:
            return {"category": "behavioral_result"}
        payload = json.loads(messages[-1]["content"])
        return {
            "revisions": [
                {
                    "scenario_id": item["scenario"]["scenario_id"],
                    "given": "An implementation of the requested behavior exists.",
                    "when": "Exercise the requested behavior.",
                    "then": "The requested behavior is observed.",
                }
                for item in payload["flagged_scenarios"]
            ]
        }


class FaithfulnessClassifier:
    def complete_json(
        self,
        messages: list[dict[str, str]],
        *,
        response_schema: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        del messages
        assert response_schema is not None
        if "category" in response_schema.get("properties", {}):
            return {"category": "behavioral_result"}
        return {"status": "resolved", "value": "entailed", "reason_code": None}


def test_verification_health_runs_probe_repair_through_json_service_bridge(
    monkeypatch: Any,
) -> None:
    def accept(
        original: Mapping[str, Any], candidate: Mapping[str, Any], **_: Any
    ) -> tuple[bool, dict[str, Any]]:
        return True, {"scenario_id": original["scenario_id"], "status": "accepted"}

    monkeypatch.setattr(probe_repair, "_check_revised_scenario_faithfulness", accept)
    service = LocalPowdrrService(
        probe_repair_clients=(FaithfulnessClassifier(), RepairProvider())
    )
    request = OperationRequest(
        request_id="probe-repair-integration",
        operation_kind="verification_health",
        inputs={
            "verification": "behavior_probe_repair",
            "instruction": "Expose and implement the requested behavior.",
            "scenarios": [
                {
                    "scenario_id": "scenario-1",
                    "subject": "the requested behavior",
                    "given": (
                        "The implementation is evaluated against: Expose and "
                        "implement the requested behavior."
                    ),
                    "when": "The instruction's behavior is exercised",
                    "then": "Expose and implement the requested behavior.",
                    "dimensions": {
                        name: "not_applicable" for name in BEHAVIOR_DIMENSIONS
                    },
                    "evidence": ["scenario-1"],
                    "validator": "Exercise the requested behavior.",
                    "faithfulness_ref": {"fingerprint": "sha256:source-artifact"},
                }
            ],
            "faithfulness_evidence": {},
            "ledger_clauses": [
                {
                    "clause_id": "scenario-1",
                    "text": "Expose and implement the requested behavior.",
                }
            ],
            "repository_contract": {},
            "provider": "test-provider",
            "model": "test-model",
        },
    )

    update = JsonServiceBridge(service).start(request)

    assert update.status == "succeeded"
    artifact = update.output_artifacts[0]
    assert artifact.artifact_id == "verification/behavior-probe-repair.json"
    result = json.loads(artifact.data)
    assert result["report"]["status"] == "repaired"
    assert result["scenarios"][0]["when"] == "Exercise the requested behavior."
    assert "artifact_path" not in result["scenarios"][0].get("faithfulness_ref", {})


def test_agent_loads_faithfulness_json_and_strips_local_paths(tmp_path: Path) -> None:
    source_ref = "instruction-001"
    contract_directory = tmp_path / "semantic-contracts"
    contract_path = contract_directory / source_ref / "partial-contract.json"
    contract_path.parent.mkdir(parents=True)
    contract = {"source_ref": source_ref, "schema_version": "partial-v1"}
    contract_path.write_text(json.dumps(contract), encoding="utf-8")
    artifact_body = {"source_ref": source_ref, "contract_fingerprint": "fingerprint"}
    fingerprint = content_fingerprint(artifact_body)
    artifact_path = tmp_path / "scenario-faithfulness.json"
    artifact_path.write_text(
        json.dumps({**artifact_body, "fingerprint": fingerprint}), encoding="utf-8"
    )

    scenarios, evidence = _load_probe_faithfulness_evidence(
        [
            {
                "scenario_id": source_ref,
                "faithfulness_ref": {
                    "artifact_path": str(artifact_path),
                    "fingerprint": fingerprint,
                },
            }
        ],
        contract_directory,
    )

    assert scenarios == [
        {"scenario_id": source_ref, "faithfulness_ref": {"fingerprint": fingerprint}}
    ]
    assert evidence[source_ref] == {
        "faithfulness_artifact": {**artifact_body, "fingerprint": fingerprint},
        "partial_contract": contract,
    }
