"""Bounded pre-coding repair for generic behavior probes."""

from __future__ import annotations

import json
import re
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

PROBE_REPAIR_SCHEMA_VERSION = "behavior-probe-repair-v3"
PROBE_REPAIR_BATCH_SIZE = 8
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
        "obligation_guidance": _obligation_guidance(
            scenarios={str(item.get("scenario_id", "")): item for item in originals},
            ledger_clauses=ledger_clauses,
        ),
        "initial_findings": initial_findings,
        "final_findings": initial_findings,
        "faithfulness_reviews": [],
        "repair_batches": [],
        "missing_scenario_ids": [],
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
        scenario_id: scenario
        for scenario_id, scenario in by_id.items()
        if scenario_id in finding_ids
        and all(
            isinstance(scenario.get(field), str) for field in ("given", "when", "then")
        )
    }
    missing_source_ids = sorted(finding_ids - set(repairable))
    artifact["missing_scenario_ids"] = missing_source_ids
    if not repairable:
        artifact["status"] = "partial"
        artifact["error"] = "no flagged scenarios had repairable text fields"
        return tuple(originals), artifact

    findings_by_id = {str(item["scenario_id"]): item for item in initial_findings}
    revisions: dict[str, Mapping[str, Any]] = {}
    repair_errors: list[str] = []
    requested_ids = list(repairable)
    successful_batches = 0
    batch_size = max(1, PROBE_REPAIR_BATCH_SIZE)

    for offset in range(0, len(requested_ids), batch_size):
        batch_ids = requested_ids[offset : offset + batch_size]
        batch_findings = [
            findings_by_id[scenario_id]
            for scenario_id in batch_ids
            if scenario_id in findings_by_id
        ]
        batch_scenarios = {
            scenario_id: repairable[scenario_id] for scenario_id in batch_ids
        }
        messages = _build_repair_messages(
            instruction=instruction,
            repository_contract=repository_contract or {},
            findings=batch_findings,
            scenarios=batch_scenarios,
            ledger_clauses=ledger_clauses,
        )
        batch_record: dict[str, Any] = {
            "requested_scenario_ids": batch_ids,
            "revised_scenario_ids": [],
            "missing_scenario_ids": [],
            "error": None,
        }
        try:
            response = complete_json(
                client, messages, response_schema=PROBE_REPAIR_RESPONSE_SCHEMA
            )
            batch_revisions, parse_errors = _parse_revision_batch(
                response, set(batch_ids)
            )
            successful_batches += 1
            revisions.update(batch_revisions)
            batch_record["revised_scenario_ids"] = list(batch_revisions)
            batch_record["missing_scenario_ids"] = sorted(
                set(batch_ids) - set(batch_revisions)
            )
            if parse_errors:
                batch_record["error"] = "; ".join(parse_errors)
                repair_errors.extend(parse_errors)
        except Exception as error:
            message = (
                f"batch {offset // batch_size + 1}: {type(error).__name__}: {error}"
            )
            batch_record["error"] = message
            batch_record["missing_scenario_ids"] = batch_ids
            repair_errors.append(message)
        artifact["repair_batches"].append(batch_record)

    all_missing_ids = set(missing_source_ids)
    all_missing_ids.update(
        scenario_id
        for batch in artifact["repair_batches"]
        for scenario_id in batch["missing_scenario_ids"]
    )
    artifact["missing_scenario_ids"] = sorted(all_missing_ids)
    artifact["proposed_revisions"] = [dict(item) for item in revisions.values()]
    if repair_errors:
        artifact["error"] = "; ".join(repair_errors)
    elif artifact["missing_scenario_ids"]:
        artifact["error"] = (
            "repair omitted or could not revise scenarios: "
            + ", ".join(artifact["missing_scenario_ids"])
        )

    if successful_batches == 0:
        artifact["status"] = "unavailable"
        return tuple(originals), artifact

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
        try:
            compile_behavior_scenarios((candidate,))
        except Exception as error:
            message = f"scenario {scenario_id}: {type(error).__name__}: {error}"
            repair_errors.append(message)
            artifact["missing_scenario_ids"] = sorted(
                {*artifact["missing_scenario_ids"], scenario_id}
            )
            artifact["error"] = "; ".join(repair_errors)
            candidates.append(original)
            continue
        if candidate != original:
            accepted, review = _check_revised_scenario_faithfulness(
                original,
                candidate,
                contract_directory=contract_directory,
                ledger_clauses=_faithfulness_source_context(
                    instruction, ledger_clauses
                ),
                client=faithfulness_client,
            )
            artifact["faithfulness_reviews"].append(review)
            candidates.append(candidate if accepted else original)
        else:
            candidates.append(original)
    try:
        compile_behavior_scenarios(candidates)
    except Exception as error:
        repair_errors.append(f"revised scenario set: {type(error).__name__}: {error}")
        artifact["error"] = "; ".join(repair_errors)
        candidates = originals
    final_findings = behavior_scenario_probe_findings(candidates)
    artifact["used_scenarios"] = candidates
    artifact["final_findings"] = final_findings
    artifact["status"] = (
        "repaired"
        if not final_findings
        and not repair_errors
        and not artifact["missing_scenario_ids"]
        else "partial"
    )
    return tuple(candidates), artifact


def _build_repair_messages(
    *,
    instruction: str,
    repository_contract: Mapping[str, Any],
    findings: Sequence[Mapping[str, Any]],
    scenarios: Mapping[str, Mapping[str, Any]],
    ledger_clauses: Sequence[Mapping[str, Any]],
) -> list[dict[str, str]]:
    return [
        {
            "role": "system",
            "content": (
                "Revise only the flagged behavior probes so each has a concrete "
                "setup, operation, and observable result. Return one revision for "
                "each supplied scenario ID in this batch. Each probe includes its "
                "authoritative source clause; treat that clause as the sole source "
                "for the required behavior. Make `then` one observable outcome "
                "strictly entailed by that clause. Do not add details or assumptions "
                "unless the clause states them. Use the supplied repository contract "
                "to make setup and operations concrete. Do not repeat the clause as "
                "the expected result. Name the public API call, property access, or "
                "input transformation in `when`; state an assertion-sized result in "
                "`then`. Use the full source instruction and related source clauses "
                "to understand how an atomic clause fits the requested deliverable; "
                "the verifier and hidden tests are never inputs. Follow the supplied "
                "obligation-specific probe guidance. For a derived property, assert "
                "its exact value from a setup with known inputs, not merely that it "
                "exists. For normalization, use a non-canonical input and assert the "
                "canonical value. For explicit pass-through behavior, compare the "
                "output with the original input. Do not invent behavior for a clause "
                "that only excludes work from scope. For a clause that names a public "
                "field, property, method, or type, test the stated member or type "
                "directly. Keep scenario IDs unchanged. If the "
                "clause truly does not support a safe observable check, return its "
                "fields unchanged so the pre-coding gate can stop the task."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "source_instruction": instruction,
                    "repository_contract": repository_contract,
                    "obligation_guidance": _obligation_guidance(
                        scenarios=scenarios,
                        ledger_clauses=ledger_clauses,
                    ),
                    "flagged_scenarios": _flagged_scenarios_with_sources(
                        findings,
                        scenarios,
                        ledger_clauses,
                        source_instruction=instruction,
                    ),
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
        },
    ]


def _parse_revision_batch(
    response: Mapping[str, Any], expected_ids: set[str]
) -> tuple[dict[str, Mapping[str, Any]], list[str]]:
    raw = response.get("revisions")
    if not isinstance(raw, list):
        raise ValueError("probe repair response must contain a revisions array")
    revisions: dict[str, Mapping[str, Any]] = {}
    errors: list[str] = []
    duplicates: set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, Mapping):
            errors.append(f"revision at index {index} is not an object")
            continue
        scenario_id = item.get("scenario_id")
        if not isinstance(scenario_id, str) or scenario_id not in expected_ids:
            errors.append(f"revision at index {index} has an unknown scenario ID")
            continue
        if scenario_id in revisions:
            duplicates.add(scenario_id)
            errors.append(f"duplicate revision for {scenario_id}")
            revisions.pop(scenario_id, None)
            continue
        if set(item) != {"scenario_id", "given", "when", "then"} or not all(
            isinstance(item.get(field), str) and item[field].strip()
            for field in ("given", "when", "then")
        ):
            errors.append(f"revision fields are invalid for {scenario_id}")
            continue
        revisions[scenario_id] = item
    for scenario_id in duplicates:
        revisions.pop(scenario_id, None)
    return revisions, errors


def _flagged_scenarios_with_sources(
    findings: Sequence[Mapping[str, Any]],
    scenarios: Mapping[str, Mapping[str, Any]],
    ledger_clauses: Sequence[Mapping[str, Any]],
    *,
    source_instruction: str,
) -> list[dict[str, Any]]:
    clauses = {
        str(clause.get("clause_id")): clause
        for clause in ledger_clauses
        if isinstance(clause.get("clause_id"), str)
    }
    result: list[dict[str, Any]] = []
    for finding in findings:
        scenario_id = str(finding["scenario_id"])
        source_id = scenario_id.removeprefix("scenario:")
        clause = clauses.get(source_id)
        source_clause = None
        if clause is not None:
            related_clauses = _related_source_clauses(clause, ledger_clauses)
            source_span = clause.get("source_span")
            span_text = None
            if isinstance(source_span, Mapping):
                start = source_span.get("start")
                end = source_span.get("end")
                if (
                    isinstance(start, int)
                    and not isinstance(start, bool)
                    and isinstance(end, int)
                    and not isinstance(end, bool)
                    and 0 <= start <= end <= len(source_instruction)
                ):
                    span_text = source_instruction[start:end]
            source_clause = {
                "clause_id": source_id,
                "text": clause.get("text"),
                "source_span_text": span_text,
                "related_clauses": related_clauses,
            }
        result.append(
            {
                "scenario": scenarios[scenario_id],
                "finding": finding,
                "authoritative_source_clause": source_clause,
            }
        )
    return result


def _related_source_clauses(
    clause: Mapping[str, Any], ledger_clauses: Sequence[Mapping[str, Any]]
) -> list[dict[str, str]]:
    validation_group_id = clause.get("validation_group_id")
    parent_clause_id = clause.get("parent_clause_id")
    clause_id = clause.get("clause_id")
    related: list[dict[str, str]] = []
    for candidate in ledger_clauses:
        candidate_id = candidate.get("clause_id")
        text = candidate.get("text")
        if not isinstance(candidate_id, str) or not isinstance(text, str):
            continue
        if candidate_id == clause_id:
            continue
        same_validation_group = (
            isinstance(validation_group_id, str)
            and candidate.get("validation_group_id") == validation_group_id
        )
        same_parent = (
            isinstance(parent_clause_id, str)
            and candidate.get("parent_clause_id") == parent_clause_id
        )
        direct_child = (
            isinstance(clause_id, str)
            and candidate.get("parent_clause_id") == f"candidate:{clause_id}"
        )
        if same_validation_group or same_parent or direct_child:
            related.append({"clause_id": candidate_id, "text": text})
    return related


def _faithfulness_source_context(
    instruction: str, ledger_clauses: Sequence[Mapping[str, Any]]
) -> tuple[Mapping[str, Any], ...]:
    """Let entailment review use the complete user request as source evidence."""
    context = tuple(ledger_clauses)
    if not instruction.strip() or any(
        clause.get("clause_id") == "instruction-source:full-request"
        for clause in context
    ):
        return context
    return (
        *context,
        {
            "clause_id": "instruction-source:full-request",
            "text": instruction,
        },
    )


def _obligation_guidance(
    *,
    scenarios: Mapping[str, Mapping[str, Any]],
    ledger_clauses: Sequence[Mapping[str, Any]],
) -> list[dict[str, str]]:
    clauses = {
        str(clause.get("clause_id")): clause
        for clause in ledger_clauses
        if isinstance(clause.get("clause_id"), str)
    }
    result: list[dict[str, str]] = []
    for scenario_id, scenario in scenarios.items():
        source_id = scenario_id.removeprefix("scenario:")
        clause = clauses.get(source_id, {})
        source = str(clause.get("text") or scenario.get("then") or "")
        kind, guidance = _classify_probe_obligation(source)
        result.append(
            {
                "scenario_id": scenario_id,
                "obligation_type": kind,
                "probe_guidance": guidance,
            }
        )
    return result


def _classify_probe_obligation(source: str) -> tuple[str, str]:
    normalized = re.sub(r"\s+", " ", source).strip().casefold()
    if "out of scope" in normalized:
        return (
            "scope_exclusion",
            "Do not invent an expected behavior for this excluded obligation. "
            "Use only an explicit pass-through requirement from related source "
            "clauses.",
        )
    if re.search(r"\bpass(?:es|ed)?\s+through\s+unchanged\b|\bunchanged\b", normalized):
        return (
            "pass_through",
            "Use a concrete in-scope input and assert that the output is unchanged.",
        )
    if re.search(
        r"\b(?:lowercase|lowercased|normalized|normalizes|normalization)\b", normalized
    ):
        return (
            "normalization",
            "Use a non-canonical input and assert the exact canonical value.",
        )
    if re.search(r"\bproperty\b|\bderived\b", normalized):
        return (
            "derived_property",
            "Construct an object with known source values and assert the exact "
            "derived property value, not just property presence.",
        )
    if re.search(r"\battribute\b|\bfield\b|\btype\s+str\b|\btype\s+bool\b", normalized):
        return (
            "public_member",
            "Access the named public member directly and assert its specified value "
            "or type.",
        )
    if re.search(
        r"\bvalid parsed representation\b|\bparsed list\[line\]\b", normalized
    ):
        return (
            "representation_robustness",
            "Exercise distinct valid parsed representations of the same input and "
            "assert the same parsed result.",
        )
    if re.search(r"\bparse\w*\b|\bsupport(?:s|ed)?\b", normalized):
        return (
            "parser_support",
            "Supply the named minimal supported syntax and assert the parsed "
            "structure and relevant values.",
        )
    if re.search(r"\bformat\w*\b|\bline\b|\bcomma\b|\bspace\b", normalized):
        return (
            "formatting_transformation",
            "Use the smallest input that exercises this formatting rule and assert "
            "the exact transformed output.",
        )
    return (
        "behavioral_result",
        "Name a concrete operation and setup, then assert one observable result "
        "entailed by the source instruction.",
    )


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
        if fingerprint != reference.get(
            "fingerprint"
        ) or fingerprint != content_fingerprint(artifact_body):
            raise ValueError("faithfulness artifact fingerprint is invalid")
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

        changed_claims = {
            "scenario_id": scenario_id,
            **{
                key: value
                for key, value in candidate.items()
                if key != "scenario_id" and original.get(key) != value
            },
        }
        plan = prepare_scenario_claim_reviews(
            contract, scenario=changed_claims, ledger_clauses=ledger_clauses
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
                            "Read the complete source-clause set together, including "
                            "clauses with the same parent or validation group. A "
                            "claim can be entailed by combining those clauses; do "
                            "not require one clause to restate the whole behavior. "
                            "A concrete observable check is entailed when it directly "
                            "checks an explicitly required behavior, even if the "
                            "source does not prescribe that exact test expression. "
                            "For example, checking that a named exposed property is "
                            "present tests the stated exposure requirement; do not "
                            "infer an unstated property value from that. Do not use "
                            "common practice or implementation guesses. "
                            "For resolved results, set reason_code to null. Use "
                            "unresolved with a null value and a reason code only "
                            "when the source cannot be classified."
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
            raw_results = response.get("results")
            if not isinstance(raw_results, list) or not all(
                isinstance(item, Mapping) for item in raw_results
            ):
                raise ValueError("faithfulness response has no results array")
            results = [
                {
                    "status": item.get("status"),
                    "value": item.get("value"),
                    "reason_code": (
                        None
                        if item.get("status") == "resolved"
                        else item.get("reason_code")
                    ),
                }
                for item in raw_results
            ]
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
