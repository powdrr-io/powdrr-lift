"""Bounded pre-coding repair for generic behavior probes."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from powdrr_lift.core.behavior_contract import (
    behavior_scenario_probe_findings,
    compile_behavior_scenarios,
)
from powdrr_lift.core.decision_obligation import content_fingerprint
from powdrr_lift.core.semantic_contract import PartialSemanticContract
from powdrr_lift.core.semantic_faithfulness import (
    finalize_scenario_claim_reviews,
    prepare_scenario_claim_reviews,
)
from powdrr_lift.workrr.llm import complete_json
from powdrr_lift.workrr.protocol import WorkflowLLMClient

PROBE_REPAIR_SCHEMA_VERSION = "behavior-probe-repair-v1"
PROBE_REPAIR_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "revisions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "scenario_id": {"type": "string"},
                    "given": {"type": "string"},
                    "when": {"type": "string"},
                    "then": {"type": "string"},
                },
                "required": ["scenario_id", "given", "when", "then"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["revisions"],
    "additionalProperties": False,
}
CLAIM_REVIEW_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": ["resolved", "unresolved"],
                    },
                    "value": {
                        "type": ["string", "null"],
                        "enum": ["entailed", "contradicted", "not_stated", None],
                    },
                    "reason_code": {"type": ["string", "null"]},
                },
                "required": ["status", "value", "reason_code"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["results"],
    "additionalProperties": False,
}


def repair_behavior_probes(
    client: WorkflowLLMClient | None,
    *,
    instruction: str,
    scenarios: Sequence[Mapping[str, Any]],
    contract_directory: Path,
    ledger_clauses: Sequence[Mapping[str, Any]],
    faithfulness_client: WorkflowLLMClient | None,
    repository_contract: Mapping[str, Any] | None = None,
) -> tuple[tuple[dict[str, Any], ...], dict[str, Any]]:
    """Revise degraded probes once, retaining only source-faithful candidates."""
    originals = [dict(item) for item in scenarios]
    initial_findings = behavior_scenario_probe_findings(originals)
    artifact: dict[str, Any] = {
        "schema_version": PROBE_REPAIR_SCHEMA_VERSION,
        "status": "not_needed" if not initial_findings else "unavailable",
        "original_scenarios": originals,
        "proposed_revisions": [],
        "used_scenarios": originals,
        "initial_findings": initial_findings,
        "final_findings": initial_findings,
        "faithfulness_reviews": [],
        "error": None,
    }
    if not initial_findings:
        return tuple(originals), artifact
    if client is None:
        artifact["error"] = "probe repair client is unavailable"
        return tuple(originals), artifact

    by_id = {str(item.get("scenario_id", "")): item for item in originals}
    finding_ids = {item["scenario_id"] for item in initial_findings}
    repairable = {
        scenario_id: by_id[scenario_id]
        for scenario_id in finding_ids
        if scenario_id in by_id
        and all(
            isinstance(by_id[scenario_id].get(field), str)
            for field in ("given", "when", "then")
        )
    }
    if len(repairable) != len(finding_ids):
        artifact["status"] = "partial"
        artifact["error"] = "some degraded scenarios have non-text probe fields"
        return tuple(originals), artifact

    messages = [
        {
            "role": "system",
            "content": (
                "Revise only the flagged behavior probes so each has a concrete "
                "setup, operation, and observable result. The source instruction "
                "and accepted scenario are authoritative. Use the supplied "
                "repository contract only to make examples concrete. Do not add "
                "capabilities, rules, edge cases, or assumptions absent from those "
                "sources. Keep scenario IDs unchanged. If a probe cannot be made "
                "more concrete from the sources, return its fields unchanged."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "source_instruction": instruction,
                    "repository_contract": repository_contract or {},
                    "flagged_scenarios": [
                        {"scenario": repairable[item["scenario_id"]], "finding": item}
                        for item in initial_findings
                    ],
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
        },
    ]
    try:
        response = complete_json(
            client, messages, response_schema=PROBE_REPAIR_RESPONSE_SCHEMA
        )
        revisions = _parse_revisions(response, set(repairable))
        artifact["proposed_revisions"] = [dict(item) for item in revisions.values()]
        candidates: list[dict[str, Any]] = []
        for original in originals:
            scenario_id = str(original.get("scenario_id", ""))
            revision = revisions.get(scenario_id)
            if revision is None:
                candidates.append(original)
                continue
            candidate = {
                **original,
                "given": revision["given"],
                "when": revision["when"],
                "then": revision["then"],
            }
            compile_behavior_scenarios((candidate,))
            if candidate != original:
                accepted, review = _check_revised_scenario_faithfulness(
                    original,
                    candidate,
                    contract_directory=contract_directory,
                    ledger_clauses=ledger_clauses,
                    client=faithfulness_client,
                )
                artifact["faithfulness_reviews"].append(review)
                candidates.append(candidate if accepted else original)
            else:
                candidates.append(original)
        compile_behavior_scenarios(candidates)
        final_findings = behavior_scenario_probe_findings(candidates)
        artifact["used_scenarios"] = candidates
        artifact["final_findings"] = final_findings
        artifact["status"] = "repaired" if not final_findings else "partial"
        return tuple(candidates), artifact
    except Exception as error:
        artifact["status"] = "unavailable"
        artifact["error"] = f"{type(error).__name__}: {error}"
        return tuple(originals), artifact


def _parse_revisions(
    response: Mapping[str, Any], expected_ids: set[str]
) -> dict[str, Mapping[str, Any]]:
    raw = response.get("revisions")
    if not isinstance(raw, list) or not all(isinstance(item, Mapping) for item in raw):
        raise ValueError("probe repair response must contain revision objects")
    revisions: dict[str, Mapping[str, Any]] = {}
    for item in raw:
        scenario_id = item.get("scenario_id")
        if not isinstance(scenario_id, str) or scenario_id not in expected_ids:
            raise ValueError("probe repair returned an unknown scenario")
        if scenario_id in revisions:
            raise ValueError("probe repair returned a duplicate scenario")
        if set(item) != {"scenario_id", "given", "when", "then"} or not all(
            isinstance(item.get(field), str) and item[field].strip()
            for field in ("given", "when", "then")
        ):
            raise ValueError(f"probe repair fields are invalid for {scenario_id}")
        revisions[scenario_id] = item
    if set(revisions) != expected_ids:
        raise ValueError("probe repair omitted one or more flagged scenarios")
    return revisions


def _check_revised_scenario_faithfulness(
    original: Mapping[str, Any],
    candidate: Mapping[str, Any],
    *,
    contract_directory: Path,
    ledger_clauses: Sequence[Mapping[str, Any]],
    client: WorkflowLLMClient | None,
) -> tuple[bool, dict[str, Any]]:
    """Run the scenario-claim entailment check used during prompt planning."""
    reference = original.get("faithfulness_ref")
    scenario_id = str(original.get("scenario_id", "unknown"))
    if not isinstance(reference, Mapping) or not isinstance(
        reference.get("artifact_path"), str
    ):
        return False, {
            "scenario_id": scenario_id,
            "status": "unavailable",
            "reason": "original scenario has no faithfulness artifact reference",
        }
    try:
        reference_path = Path(str(reference["artifact_path"]))
        faithfulness_artifact = json.loads(reference_path.read_text(encoding="utf-8"))
        if not isinstance(faithfulness_artifact, Mapping):
            raise ValueError("faithfulness artifact is malformed")
        fingerprint = faithfulness_artifact.get("fingerprint")
        artifact_body = {
            key: value
            for key, value in faithfulness_artifact.items()
            if key != "fingerprint"
        }
        original_without_ref = {
            key: value for key, value in original.items() if key != "faithfulness_ref"
        }
        if (
            fingerprint != reference.get("fingerprint")
            or fingerprint != content_fingerprint(artifact_body)
            or faithfulness_artifact.get("scenario_fingerprint")
            != content_fingerprint(original_without_ref)
        ):
            raise ValueError("faithfulness artifact does not match original scenario")
        source_ref = faithfulness_artifact.get("source_ref")
        if not isinstance(source_ref, str) or not source_ref.strip():
            raise ValueError("faithfulness artifact has no source reference")
        contract_path = contract_directory / source_ref / "partial-contract.json"
        contract_document = json.loads(contract_path.read_text(encoding="utf-8"))
        if not isinstance(contract_document, Mapping):
            raise ValueError("partial semantic contract is malformed")
        contract = PartialSemanticContract.from_data(contract_document)
        if (
            contract.fingerprint != faithfulness_artifact.get("contract_fingerprint")
            or contract.source_ref != source_ref
        ):
            raise ValueError("partial contract does not match faithfulness artifact")

        plan = prepare_scenario_claim_reviews(
            contract, scenario=candidate, ledger_clauses=ledger_clauses
        )
        requests = plan.get("requests")
        if not isinstance(requests, list):
            raise ValueError("scenario faithfulness plan is malformed")
        if requests:
            if client is None:
                raise ValueError("faithfulness classifier is unavailable")
            response = complete_json(
                client,
                [
                    {
                        "role": "system",
                        "content": (
                            "Classify whether each candidate scenario claim is "
                            "entailed by the exact source instruction and accepted "
                            "semantic contract. Use entailed only when the source "
                            "supports the claim, contradicted when it conflicts, "
                            "and not_stated when the source does not decide it. "
                            "Do not use common practice or implementation guesses."
                        ),
                    },
                    {
                        "role": "user",
                        "content": json.dumps(
                            {"requests": requests},
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                    },
                ],
                response_schema=CLAIM_REVIEW_RESPONSE_SCHEMA,
            )
            results = response.get("results")
            if not isinstance(results, list) or not all(
                isinstance(item, Mapping) for item in results
            ):
                raise ValueError("faithfulness response has no results array")
        else:
            results = []
        outcome = finalize_scenario_claim_reviews(plan, results, benchmark_mode=True)
        return bool(outcome.get("accepted")), {
            "scenario_id": scenario_id,
            "status": "accepted" if outcome.get("accepted") else "rejected",
            "outcome": outcome,
        }
    except Exception as error:
        return False, {
            "scenario_id": scenario_id,
            "status": "unavailable",
            "reason": f"{type(error).__name__}: {error}",
        }
