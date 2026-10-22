from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from powdrr_lift.core.behavior_contract import BEHAVIOR_DIMENSIONS
from powdrr_lift.workrr import jev_classifier, probe_repair


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
        if "category" in response_schema.get("properties", {}):
            return {"category": "property_exposure"}
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


def _revision(scenario_id: str = "instruction-001") -> dict[str, str]:
    return {
        "scenario_id": scenario_id,
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
        ledger_clauses=(
            {
                "clause_id": "instruction-001",
                "text": "Deferred fields merge at the path given.",
            },
        ),
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
    assert "Do not add details or assumptions" in str(client.messages)
    assert "property_exposure" in str(client.messages)
    assert "pre-coding gate can stop the task" in str(client.messages)
    assert "authoritative_source_clause" in str(client.messages)
    assert "instruction-001" in str(client.messages)
    assert "source_instruction" in str(client.messages)


def test_probe_repair_adds_related_source_and_typed_obligation_guidance(
    monkeypatch: Any, tmp_path: Path
) -> None:
    instruction = (
        "DdlTable exposes property column_count. "
        "DdlTable exposes property constraint_count."
    )
    scenario = _scenario()
    scenario["scenario_id"] = "scenario:instruction-001"
    source_clauses = (
        {
            "clause_id": "instruction-001",
            "text": "DdlTable exposes property column_count.",
            "validation_group_id": "validation:table-properties",
            "source_span": {"start": 0, "end": 39},
        },
        {
            "clause_id": "instruction-002",
            "text": "DdlTable exposes property constraint_count.",
            "validation_group_id": "validation:table-properties",
        },
    )
    client = RepairClient([_revision("scenario:instruction-001")])
    reviewed_contexts: list[tuple[Mapping[str, Any], ...]] = []

    def accept(
        _original: Mapping[str, Any],
        _candidate: Mapping[str, Any],
        **kwargs: Any,
    ) -> tuple[bool, dict[str, Any]]:
        reviewed_contexts.append(kwargs["ledger_clauses"])
        return True, {"scenario_id": "scenario:instruction-001", "status": "accepted"}

    monkeypatch.setattr(probe_repair, "_check_revised_scenario_faithfulness", accept)
    probe_repair.repair_behavior_probes(
        client,
        instruction=instruction,
        scenarios=(scenario,),
        contract_directory=tmp_path,
        ledger_clauses=source_clauses,
        faithfulness_client=client,
    )

    request = json.loads(client.messages[1]["content"])  # type: ignore[index]
    flagged = request["flagged_scenarios"][0]
    source = flagged["authoritative_source_clause"]
    assert request["source_instruction"] == instruction
    assert source["source_span_text"] == "DdlTable exposes property column_count."
    assert source["related_clauses"] == [
        {
            "clause_id": "instruction-002",
            "text": "DdlTable exposes property constraint_count.",
        }
    ]
    assert request["obligation_guidance"] == [
        {
            "scenario_id": "scenario:instruction-001",
            "obligation_type": "property_exposure",
            "categorized_by": "jev",
        }
    ]
    assert client.messages is not None
    assert "property_exposure" in client.messages[1]["content"]
    assert "do not infer its returned value" in client.messages[0]["content"]
    examples = json.loads(client.messages[1]["content"])["examples"]
    property_example = next(
        item for item in examples if item["category"] == "property_exposure"
    )
    assert property_example["then"] == (
        "The property access completes without AttributeError."
    )
    assert "never evidence for the current task" in client.messages[0]["content"]
    assert any(
        clause["clause_id"] == "instruction-source:full-request"
        and clause["text"] == instruction
        for clause in reviewed_contexts[0]
    )


def test_jev_obligation_classifier_distinguishes_property_exposure() -> None:
    messages = probe_repair._build_jev_obligation_category_messages(
        "DdlTable exposes a property constrained_columns."
    )
    payload = json.loads(messages[1]["content"].split("Context:\n", 1)[1])
    classifier = payload["probe_obligation"]
    assert classifier["spec"]["decision_kind"] == "probe_obligation_category"
    assert "property_exposure" in classifier["allowed_values"]
    assert "unless the source also specifies its value or derivation" in " ".join(
        classifier["instructions"]
    )

    classifier_request = jev_classifier._classifier_request(
        messages, probe_repair.OBLIGATION_CATEGORY_RESPONSE_SCHEMA
    )

    assert classifier_request is not None
    assert classifier_request["kind"] == "probe_obligation_category"
    assert classifier_request["allowed_values"] == classifier["allowed_values"]
    assert (
        classifier_request["state"]["source_text"]
        == "DdlTable exposes a property constrained_columns."
    )


def test_faithfulness_source_evidence_requires_exact_quote_from_named_clause() -> None:
    context_text = json.dumps(
        {
            "source_clauses": [
                {
                    "clause_id": "instruction-001",
                    "text": "DdlTable exposes a property column_count.",
                }
            ]
        }
    )

    assert probe_repair._source_evidence_is_valid(
        [
            {
                "clause_id": "instruction-001",
                "quote": "exposes a property column_count",
            }
        ],
        context_text=context_text,
    )
    assert not probe_repair._source_evidence_is_valid(
        [
            {
                "clause_id": "instruction-001",
                "quote": "column_count is always positive",
            }
        ],
        context_text=context_text,
    )
    assert not probe_repair._source_evidence_is_valid(
        [
            {
                "clause_id": "instruction-999",
                "quote": "exposes a property column_count",
            }
        ],
        context_text=context_text,
    )


def test_probe_repair_discards_candidate_rejected_by_faithfulness(
    monkeypatch: Any, tmp_path: Path
) -> None:
    client = RepairClient([_revision()])
    faithfulness_checks = 0

    def reject(*_args: Any, **_kwargs: Any) -> tuple[bool, dict[str, Any]]:
        nonlocal faithfulness_checks
        faithfulness_checks += 1
        return False, {
            "scenario_id": "instruction-001",
            "status": "rejected",
            "outcome": {
                "findings": [
                    {
                        "classification": "not_stated",
                        "field_paths": ["then"],
                        "reason_code": "source_underspecified",
                    }
                ]
            },
        }

    monkeypatch.setattr(
        probe_repair,
        "_check_revised_scenario_faithfulness",
        reject,
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
    assert faithfulness_checks == 2
    assert artifact["faithfulness_retries"][0]["status"] == "rejected"


def test_probe_repair_retries_rejected_claim_with_review_feedback(
    monkeypatch: Any, tmp_path: Path
) -> None:
    corrected = {
        **_revision(),
        "then": "The accumulated user object's email field equals the deferred value.",
    }

    class RetryClient:
        def __init__(self) -> None:
            self.responses = [
                {"revisions": [_revision()]},
                {"revisions": [corrected]},
            ]
            self.messages: list[list[dict[str, str]]] = []

        def complete_json(
            self,
            messages: list[dict[str, str]],
            *,
            response_schema: Mapping[str, Any] | None = None,
        ) -> dict[str, Any]:
            assert response_schema is not None
            self.messages.append(messages)
            if "category" in response_schema.get("properties", {}):
                return {"category": "behavioral_result"}
            return self.responses.pop(0)

    client = RetryClient()
    review_count = 0

    def review(
        _original: Mapping[str, Any], candidate: Mapping[str, Any], **_: Any
    ) -> tuple[bool, dict[str, Any]]:
        nonlocal review_count
        review_count += 1
        if review_count == 1:
            return False, {
                "scenario_id": "instruction-001",
                "status": "rejected",
                "outcome": {
                    "findings": [
                        {
                            "classification": "not_stated",
                            "field_paths": ["then"],
                            "reason_code": "source_underspecified",
                        }
                    ]
                },
            }
        return True, {
            "scenario_id": candidate["scenario_id"],
            "status": "accepted",
        }

    monkeypatch.setattr(
        probe_repair,
        "_check_revised_scenario_faithfulness",
        review,
    )
    revised, artifact = probe_repair.repair_behavior_probes(
        client,
        instruction="Deferred fields merge at the path given.",
        scenarios=(_scenario(),),
        contract_directory=tmp_path,
        ledger_clauses=(
            {
                "clause_id": "instruction-001",
                "text": "Deferred fields merge at the path given.",
            },
        ),
        faithfulness_client=client,
    )

    assert len(client.messages) == 3
    retry_payload = json.loads(client.messages[2][1]["content"])
    assert (
        retry_payload["faithfulness_review"]["outcome"]["findings"][0]["reason_code"]
        == "source_underspecified"
    )
    assert (
        "The verifier and hidden tests are never inputs"
        in client.messages[2][0]["content"]
    )
    assert revised[0]["then"] == corrected["then"]
    assert artifact["faithfulness_retries"][0]["status"] == "accepted"


def test_probe_repair_records_empty_response_as_partial_not_failure(
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
    assert artifact["status"] == "partial"
    assert artifact["missing_scenario_ids"] == ["instruction-001"]
    assert "instruction-001" in artifact["error"]
    assert artifact["repair_batches"][0]["error"] is None


def test_probe_repair_keeps_valid_revisions_from_partial_batches(
    monkeypatch: Any, tmp_path: Path
) -> None:
    class PartialBatchClient:
        def __init__(self) -> None:
            self.requested_batches: list[list[str]] = []

        def complete_json(
            self,
            messages: list[dict[str, str]],
            *,
            response_schema: Mapping[str, Any] | None = None,
        ) -> dict[str, Any]:
            assert response_schema is not None
            payload = json.loads(messages[1]["content"])
            requested = [
                item["scenario"]["scenario_id"] for item in payload["flagged_scenarios"]
            ]
            self.requested_batches.append(requested)
            revised_id = requested[0]
            return {"revisions": [_revision(revised_id)]}

    client = PartialBatchClient()
    monkeypatch.setattr(probe_repair, "PROBE_REPAIR_BATCH_SIZE", 2)
    monkeypatch.setattr(
        probe_repair,
        "_check_revised_scenario_faithfulness",
        lambda original, candidate, **_: (
            True,
            {"scenario_id": candidate["scenario_id"], "status": "accepted"},
        ),
    )
    scenarios = (
        _scenario(),
        {**_scenario(), "scenario_id": "instruction-002"},
        {**_scenario(), "scenario_id": "instruction-003"},
    )

    revised, artifact = probe_repair.repair_behavior_probes(
        client,
        instruction="Deferred fields merge at the path given.",
        scenarios=scenarios,
        contract_directory=tmp_path,
        ledger_clauses=(),
        faithfulness_client=client,
    )

    assert len(client.requested_batches) == 2
    assert [len(batch) for batch in client.requested_batches] == [2, 1]
    returned_ids = [batch[0] for batch in client.requested_batches]
    omitted_ids = [batch[1] for batch in client.requested_batches if len(batch) > 1]
    revised_by_id = {str(item["scenario_id"]): item for item in revised}
    for scenario_id in returned_ids:
        assert revised_by_id[scenario_id]["given"] == _revision(scenario_id)["given"]
    for scenario_id in omitted_ids:
        original = next(
            item for item in scenarios if item["scenario_id"] == scenario_id
        )
        assert revised_by_id[scenario_id] == original
    assert artifact["status"] == "partial"
    assert artifact["missing_scenario_ids"] == omitted_ids
    assert len(artifact["proposed_revisions"]) == len(returned_ids)
    assert artifact["repair_batches"][0]["revised_scenario_ids"] == [returned_ids[0]]
    assert artifact["repair_batches"][0]["missing_scenario_ids"] == omitted_ids


def test_probe_repair_marks_failed_response_unavailable(
    tmp_path: Path,
) -> None:
    class FailedClient:
        def complete_json(
            self,
            _: list[dict[str, str]],
            *,
            response_schema: Mapping[str, Any] | None = None,
        ) -> dict[str, Any]:
            raise RuntimeError("repair service unavailable")

    revised, artifact = probe_repair.repair_behavior_probes(
        FailedClient(),
        instruction="Deferred fields merge at the path given.",
        scenarios=(_scenario(),),
        contract_directory=tmp_path,
        ledger_clauses=(),
        faithfulness_client=None,
    )

    assert revised == (_scenario(),)
    assert artifact["status"] == "unavailable"
    assert "repair service unavailable" in artifact["error"]
    assert artifact["missing_scenario_ids"] == ["instruction-001"]


def test_probe_repair_keeps_original_when_revision_fails_validation(
    monkeypatch: Any, tmp_path: Path
) -> None:
    client = RepairClient([_revision()])
    compile_scenarios = probe_repair.compile_behavior_scenarios

    def reject_revision(scenarios: Any) -> Any:
        if scenarios[0]["given"] == _revision()["given"]:
            raise ValueError("invalid compiled scenario")
        return compile_scenarios(scenarios)

    monkeypatch.setattr(probe_repair, "compile_behavior_scenarios", reject_revision)
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
    assert artifact["missing_scenario_ids"] == ["instruction-001"]
    assert "invalid compiled scenario" in artifact["error"]


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

    reviewed_scenarios: list[Mapping[str, Any]] = []
    reviewed_contexts: list[Mapping[str, Any]] = []
    finalized_results: list[Mapping[str, Any]] = []
    categorizer_messages: list[list[dict[str, str]]] = []
    detail_messages: list[list[dict[str, str]]] = []

    class CategorizerClient:
        def complete_json(
            self,
            messages: list[dict[str, str]],
            *,
            response_schema: Mapping[str, Any] | None = None,
        ) -> dict[str, Any]:
            assert response_schema is not None
            categorizer_messages.append(messages)
            return {
                "status": "resolved",
                "value": "entailed",
                "reason_code": None,
            }

    class DetailClient:
        def complete_json(
            self,
            messages: list[dict[str, str]],
            *,
            response_schema: Mapping[str, Any] | None = None,
        ) -> dict[str, Any]:
            assert response_schema is not None
            detail_messages.append(messages)
            detail_request = json.loads(messages[-1]["content"])
            assert detail_request["jev_categories"][0]["value"] == "entailed"
            return {
                "results": [
                    {
                        "unsupported_part": None,
                        "source_evidence": [
                            {
                                "clause_id": "instruction-001",
                                "quote": "The source instruction",
                            }
                        ],
                    }
                ]
            }

    def prepare(
        _: Any,
        *,
        scenario: Mapping[str, Any],
        scenario_context: Mapping[str, Any],
        **__: Any,
    ) -> dict[str, Any]:
        reviewed_scenarios.append(scenario)
        reviewed_contexts.append(scenario_context)
        context_text = json.dumps(
            {
                "source_clauses": [
                    {
                        "clause_id": "instruction-001",
                        "text": "The source instruction",
                    }
                ]
            }
        )
        return {
            "requests": [
                {
                    "source_text": "The source instruction",
                    "claim": {
                        "claim_id": "claim-1",
                        "candidate_value": "Revised supported claim.",
                    },
                    "context_text": context_text,
                }
            ]
        }

    def finalize(
        _: Mapping[str, Any], results: list[Mapping[str, Any]], **__: Any
    ) -> dict[str, Any]:
        finalized_results.extend(results)
        return {
            "accepted": True,
            "reviews": [
                {
                    "claim_id": "claim-1",
                    "decision": {"result": results[0]},
                }
            ],
        }

    monkeypatch.setattr(
        probe_repair,
        "prepare_scenario_claim_reviews",
        prepare,
    )
    monkeypatch.setattr(
        probe_repair,
        "finalize_scenario_claim_reviews",
        finalize,
    )

    accepted, review = probe_repair._check_revised_scenario_faithfulness(
        {
            "scenario_id": "instruction-001",
            "subject": "The source instruction",
            "faithfulness_ref": {
                "artifact_path": str(artifact_path),
                "fingerprint": fingerprint,
            },
        },
        {
            "scenario_id": "instruction-001",
            "subject": "The source instruction",
            "then": "Revised supported claim.",
        },
        contract_directory=tmp_path,
        ledger_clauses=(),
        client=CategorizerClient(),
        detail_client=DetailClient(),
    )

    assert accepted is True
    assert review["status"] == "accepted"
    assert reviewed_scenarios == [
        {"scenario_id": "instruction-001", "then": "Revised supported claim."}
    ]
    assert reviewed_contexts == [
        {
            "scenario_id": "instruction-001",
            "subject": "The source instruction",
            "then": "Revised supported claim.",
        }
    ]
    assert finalized_results == [
        {"status": "resolved", "value": "entailed", "reason_code": None}
    ]
    assert review["outcome"]["reviews"][0]["source_evidence_valid"] is True
    assert "given and when context" in categorizer_messages[0][1]["content"]
    assert "source_evidence" not in categorizer_messages[0][1]["content"]
    assert "JEV already classified" in detail_messages[0][0]["content"]


def test_jev_claim_category_messages_use_classifier_adapter_envelope() -> None:
    request = {
        "source_text": "The source instruction",
        "context_text": '{"source_clauses": []}',
        "claim": {
            "claim_id": "claim-1",
            "candidate_value": "The candidate claim",
        },
    }

    classifier_request = jev_classifier._classifier_request(
        probe_repair._build_jev_claim_category_messages(request),
        probe_repair.CLAIM_CATEGORY_RESPONSE_SCHEMA,
    )

    assert classifier_request is not None
    assert classifier_request["kind"] == "entailment"
    assert classifier_request["allowed_values"] == [
        "entailed",
        "contradicted",
        "not_stated",
    ]
    assert any(
        "Calibration example" in instruction
        for instruction in classifier_request["state"]["context"]["scenario_claim"][
            "instructions"
        ]
    )
    assert (
        classifier_request["state"]["context"]["scenario_claim"]["candidate_value"]
        == "The candidate claim"
    )


def test_formatting_repair_example_demonstrates_a_visible_transformation() -> None:
    example = next(
        item
        for item in probe_repair.PROBE_REPAIR_EXAMPLES
        if item["category"] == "formatting_transformation"
    )

    assert example["given"] == "The formatter receives the list text red,blue."
    assert example["then"] == "The output contains red, blue."
