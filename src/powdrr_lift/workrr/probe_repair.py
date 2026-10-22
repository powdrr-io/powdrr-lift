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

PROBE_REPAIR_SCHEMA_VERSION = "behavior-probe-repair-v5"
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
CLAIM_CATEGORY_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["resolved", "unresolved"]},
        "value": {
            "type": ["string", "null"],
            "enum": ["entailed", "contradicted", "not_stated", None],
        },
        "reason_code": {"type": ["string", "null"]},
    },
    "required": ["status", "value", "reason_code"],
    "additionalProperties": False,
}
CLAIM_DETAILS_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "unsupported_part": {"type": ["string", "null"]},
                    "source_evidence": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "clause_id": {"type": "string"},
                                "quote": {"type": "string"},
                            },
                            "required": ["clause_id", "quote"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": [
                    "unsupported_part",
                    "source_evidence",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["results"],
    "additionalProperties": False,
}
OBLIGATION_CATEGORY_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "category": {
            "type": "string",
            "enum": [
                "property_exposure",
                "derived_property",
                "public_member",
                "normalization",
                "pass_through",
                "scope_exclusion",
                "parser_support",
                "formatting_transformation",
                "behavioral_result",
                "uncertain",
            ],
        }
    },
    "required": ["category"],
    "additionalProperties": False,
}
PROBE_REPAIR_EXAMPLES: tuple[dict[str, str], ...] = (
    {
        "category": "property_exposure",
        "source": "A profile exposes a property display_name.",
        "given": (
            "A Profile instance created with its documented constructor is available."
        ),
        "when": "Read profile.display_name.",
        "then": "The property access completes without AttributeError.",
        "lesson": "Exposure entails access, not a particular returned value.",
    },
    {
        "category": "derived_property",
        "source": "The remaining quota equals granted quota minus consumed quota.",
        "given": "An account has 9 units granted and 4 units consumed.",
        "when": "Read the account's remaining quota.",
        "then": "The value is 5.",
        "lesson": "Assert a computed value when the source gives the derivation.",
    },
    {
        "category": "normalization",
        "source": "The identifier is normalized to lowercase.",
        "given": "The normalizer receives the identifier MiXeD.",
        "when": "Normalize the identifier.",
        "then": "The result is mixed.",
        "lesson": "Use a non-canonical input and assert the stated canonical form.",
    },
    {
        "category": "pass_through",
        "source": "Unknown input values pass through unchanged.",
        "given": "The processor receives an unknown input value.",
        "when": "The processor handles the input value.",
        "then": "The output string equals the input string exactly.",
        "lesson": "Assert only the preservation required by the source.",
    },
    {
        "category": "formatting_transformation",
        "source": "A comma in the formatted list is followed by one space.",
        "given": "The formatter receives the list text red,blue.",
        "when": "Format the list.",
        "then": "The output contains red, blue.",
        "lesson": (
            "Use a non-canonical input and assert the exact visible transformation."
        ),
    },
)


def repair_behavior_probes(
    client: WorkflowLLMClient | None,
    *,
    instruction: str,
    scenarios: Sequence[Mapping[str, Any]],
    contract_directory: Path,
    ledger_clauses: Sequence[Mapping[str, Any]],
    faithfulness_client: WorkflowLLMClient | None,
    faithfulness_detail_client: WorkflowLLMClient | None = None,
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
        "obligation_guidance": [],
        "initial_findings": initial_findings,
        "final_findings": initial_findings,
        "faithfulness_reviews": [],
        "faithfulness_retries": [],
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
    obligation_guidance = _obligation_guidance(
        client=faithfulness_client,
        scenarios={str(item.get("scenario_id", "")): item for item in originals},
        ledger_clauses=ledger_clauses,
        scenario_ids=set(repairable),
    )
    artifact["obligation_guidance"] = obligation_guidance
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
            obligation_guidance=obligation_guidance,
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
                detail_client=faithfulness_detail_client or client,
            )
            artifact["faithfulness_reviews"].append(review)
            if accepted:
                candidates.append(candidate)
                continue
            retry_candidate, retry_review = _retry_faithfulness_rejection(
                client,
                instruction=instruction,
                repository_contract=repository_contract or {},
                scenario=candidate,
                review=review,
                ledger_clauses=_faithfulness_source_context(
                    instruction, ledger_clauses
                ),
                contract_directory=contract_directory,
                faithfulness_client=faithfulness_client,
                faithfulness_detail_client=faithfulness_detail_client or client,
            )
            artifact["faithfulness_retries"].append(retry_review)
            candidates.append(
                retry_candidate if retry_candidate is not None else original
            )
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
    obligation_guidance: Sequence[Mapping[str, str]],
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
                "the verifier and hidden tests are never inputs. The obligation "
                "category was assigned by JEV. Use that category to select "
                "the right kind of probe, then derive all concrete setup and expected "
                "details from the source clause and repository contract. For a "
                "property_exposure obligation, assert that the named property can be "
                "accessed; do not infer its returned value. For derived_property, "
                "assert a value only when its derivation is specified by the source. "
                "For normalization, use a non-canonical input and assert the canonical "
                "value. For explicit pass-through behavior, compare output with input. "
                "Do not invent behavior for scope exclusions. The examples in the user "
                "payload demonstrate the level of specificity; their names and values "
                "are illustrative only, never evidence for the current task. Keep "
                "scenario IDs "
                "unchanged. If the "
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
                    "obligation_guidance": [
                        item
                        for item in obligation_guidance
                        if item["scenario_id"] in scenarios
                    ],
                    "examples": PROBE_REPAIR_EXAMPLES,
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


def _source_evidence_is_valid(evidence: Any, *, context_text: str) -> bool:
    """Check that cited source quotes are exact excerpts from cited clauses."""
    if not isinstance(evidence, list) or not evidence:
        return False
    try:
        context = json.loads(context_text)
    except (TypeError, ValueError):
        return False
    clauses = context.get("source_clauses") if isinstance(context, Mapping) else None
    if not isinstance(clauses, list):
        return False
    source_by_id = {
        clause.get("clause_id"): clause.get("text")
        for clause in clauses
        if isinstance(clause, Mapping)
        and isinstance(clause.get("clause_id"), str)
        and isinstance(clause.get("text"), str)
    }
    for item in evidence:
        if not isinstance(item, Mapping):
            return False
        clause_id = item.get("clause_id")
        quote = item.get("quote")
        source_text = (
            source_by_id.get(clause_id) if isinstance(clause_id, str) else None
        )
        if (
            not isinstance(quote, str)
            or not quote.strip()
            or not isinstance(source_text, str)
            or quote not in source_text
        ):
            return False
    return True


def _obligation_guidance(
    *,
    client: WorkflowLLMClient | None,
    scenarios: Mapping[str, Mapping[str, Any]],
    ledger_clauses: Sequence[Mapping[str, Any]],
    scenario_ids: set[str],
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
        if scenario_id not in scenario_ids:
            continue
        kind = "uncertain"
        if client is not None:
            try:
                response = complete_json(
                    client,
                    _build_jev_obligation_category_messages(source),
                    response_schema=OBLIGATION_CATEGORY_RESPONSE_SCHEMA,
                )
                candidate = response.get("category")
                if isinstance(candidate, str) and candidate in {
                    "property_exposure",
                    "derived_property",
                    "public_member",
                    "normalization",
                    "pass_through",
                    "scope_exclusion",
                    "parser_support",
                    "formatting_transformation",
                    "behavioral_result",
                    "uncertain",
                }:
                    kind = candidate
            except Exception:
                # Keep the category conservative. The probe authoring LLM still
                # receives the source and must not infer an unstated behavior.
                kind = "uncertain"
        result.append(
            {
                "scenario_id": scenario_id,
                "obligation_type": kind,
                "categorized_by": "jev" if kind != "uncertain" else "unresolved",
            }
        )
    return result


def _build_jev_obligation_category_messages(source: str) -> list[dict[str, str]]:
    categories = [
        "property_exposure: the source requires a named property to be exposed, but "
        "does not specify its value",
        "derived_property: the source specifies how a property's value is derived",
        "public_member: the source names a field, method, or type to expose",
        "normalization: the source requires canonicalization of an input value",
        "pass_through: the source requires preserving an input unchanged",
        "scope_exclusion: the source explicitly excludes behavior from scope",
        "parser_support: the source requires accepting or rejecting named syntax",
        "formatting_transformation: the source specifies an output formatting rule",
        "behavioral_result: the source specifies another observable behavior",
        "uncertain: the source does not establish one safe category",
    ]
    classifier = {
        "spec": {"decision_kind": "probe_obligation_category"},
        "question": "Which single category best describes the stated obligation?",
        "instructions": [
            "Select the category that matches only what the source states.",
            "A requirement to expose a named property is property_exposure unless "
            "the source also specifies its value or derivation.",
            "Do not infer behavior from repository conventions or likely intent.",
        ],
        "allowed_values": [
            "property_exposure",
            "derived_property",
            "public_member",
            "normalization",
            "pass_through",
            "scope_exclusion",
            "parser_support",
            "formatting_transformation",
            "behavioral_result",
            "uncertain",
        ],
        "subject_text": source,
        "candidate_field": "probe obligation category",
        "candidate_value": source,
        "category_definitions": categories,
    }
    return [
        {
            "role": "system",
            "content": "Classify the source obligation using one allowed category.",
        },
        {
            "role": "user",
            "content": (
                "Question:\nWhich single category best describes the stated "
                "obligation?\n"
                "Instructions:\nReturn only one allowed category. Select the category "
                "that matches only what the source states. A requirement to expose a "
                "named property is property_exposure unless the source also specifies "
                "its value or derivation. Do not infer behavior from repository "
                "conventions or likely intent.\n"
                "Context:\n"
                + json.dumps(
                    {"probe_obligation": classifier},
                    ensure_ascii=False,
                    sort_keys=True,
                )
            ),
        },
    ]


def _retry_faithfulness_rejection(
    client: WorkflowLLMClient | None,
    *,
    instruction: str,
    repository_contract: Mapping[str, Any],
    scenario: Mapping[str, Any],
    review: Mapping[str, Any],
    ledger_clauses: Sequence[Mapping[str, Any]],
    contract_directory: Path,
    faithfulness_client: WorkflowLLMClient | None,
    faithfulness_detail_client: WorkflowLLMClient | None,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Give one rejected revision a targeted, source-only repair attempt."""
    scenario_id = str(scenario.get("scenario_id", "unknown"))
    record: dict[str, Any] = {
        "scenario_id": scenario_id,
        "status": "unavailable",
        "faithfulness_review": dict(review),
        "error": None,
    }
    if client is None or review.get("status") != "rejected":
        record["status"] = "not_attempted"
        record["error"] = "revision was not rejected or repair client is unavailable"
        return None, record
    try:
        response = complete_json(
            client,
            _build_faithfulness_retry_messages(
                instruction=instruction,
                repository_contract=repository_contract,
                scenario=scenario,
                review=review,
                ledger_clauses=ledger_clauses,
            ),
            response_schema=PROBE_REPAIR_RESPONSE_SCHEMA,
        )
        revisions, errors = _parse_revision_batch(response, {scenario_id})
        retry = revisions.get(scenario_id)
        if retry is None:
            record["status"] = "no_revision"
            record["error"] = "; ".join(errors) or "repair returned no scenario"
            return None, record
        candidate = {
            **scenario,
            "given": retry["given"],
            "when": retry["when"],
            "then": retry["then"],
        }
        compile_behavior_scenarios((candidate,))
        accepted, retry_review = _check_revised_scenario_faithfulness(
            scenario,
            candidate,
            contract_directory=contract_directory,
            ledger_clauses=ledger_clauses,
            client=faithfulness_client,
            detail_client=faithfulness_detail_client,
        )
        record["proposed_revision"] = dict(retry)
        record["faithfulness_review"] = retry_review
        if accepted:
            record["status"] = "accepted"
            return candidate, record
        record["status"] = "rejected"
        return None, record
    except Exception as error:
        record["status"] = "unavailable"
        record["error"] = f"{type(error).__name__}: {error}"
        return None, record


def _build_faithfulness_retry_messages(
    *,
    instruction: str,
    repository_contract: Mapping[str, Any],
    scenario: Mapping[str, Any],
    review: Mapping[str, Any],
    ledger_clauses: Sequence[Mapping[str, Any]],
) -> list[dict[str, str]]:
    """Ask Miniswe to correct only a rejected probe assertion using source."""
    scenario_id = str(scenario.get("scenario_id", ""))
    finding = {
        "scenario_id": scenario_id,
        "reason": review.get("outcome", review),
    }
    sources = _flagged_scenarios_with_sources(
        [finding],
        {scenario_id: scenario},
        ledger_clauses,
        source_instruction=instruction,
    )
    return [
        {
            "role": "system",
            "content": (
                "Correct one behavior probe that failed source-faithfulness review. "
                "Use only the source instruction, related source clauses, and the "
                "provided repository contract. The verifier and hidden tests are "
                "never inputs. Read the review findings and source evidence, then "
                "change only the unsupported or contradictory assertion. Preserve "
                "supported setup, operation, and expectations. If the source does "
                "not support a safe probe, return the scenario unchanged. Return one "
                "revision with the same scenario ID."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "source_instruction": instruction,
                    "repository_contract": repository_contract,
                    "flagged_scenario": sources[0] if sources else scenario,
                    "faithfulness_review": review,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
        },
    ]


def _build_jev_claim_category_messages(
    request: Mapping[str, Any],
) -> list[dict[str, str]]:
    """Build the enum-classifier envelope recognized by the JEV adapter."""
    claim = request.get("claim")
    if not isinstance(claim, Mapping):
        raise ValueError("scenario claim request has no claim")
    source_text = request.get("source_text")
    if not isinstance(source_text, str):
        raise ValueError("scenario claim request has no source text")
    question = (
        "Is this candidate scenario claim entailed, contradicted, or not stated "
        "by the source instruction?"
    )
    instructions = [
        "Use entailed when the source supports the claim.",
        "Use contradicted when the source rules the claim out.",
        "Use not_stated when the source does not decide it.",
        "Read related source clauses together and interpret the claim with its "
        "given and when context.",
        "A concrete observable check is entailed when it directly checks required "
        "behavior, even if the exact test expression is not stated.",
        "Calibration example: source instruction 'A comma in the formatted list is "
        "followed by one space'; given 'red,blue', when the list is formatted, then "
        "the output contains 'red, blue' is entailed. The example is illustrative "
        "only and does not add requirements to the current source.",
        "Do not provide explanations or revise the candidate.",
    ]
    classifier = {
        "spec": {"decision_kind": "entailment"},
        "question": question,
        "instructions": instructions,
        "allowed_values": ["entailed", "contradicted", "not_stated"],
        "subject_text": source_text,
        "candidate_field": "scenario claim",
        "candidate_value": claim.get("candidate_value"),
        "claim": dict(claim),
        "source_context": request.get("context_text"),
    }
    context = json.dumps(
        {"scenario_claim": classifier}, ensure_ascii=False, sort_keys=True
    )
    return [
        {
            "role": "system",
            "content": "Classify the candidate using the allowed labels.",
        },
        {
            "role": "user",
            "content": (
                f"Question:\n{question}\n"
                f"Instructions:\n{' '.join(instructions)}\n"
                f"Context:\n{context}"
            ),
        },
    ]


def _check_revised_scenario_faithfulness(
    original: Mapping[str, Any],
    candidate: Mapping[str, Any],
    *,
    contract_directory: Path,
    ledger_clauses: Sequence[Mapping[str, Any]],
    client: WorkflowLLMClient | None,
    detail_client: WorkflowLLMClient | None = None,
) -> tuple[bool, dict[str, Any]]:
    """Run the scenario-claim entailment check used during prompt planning."""
    detail_client = detail_client or client
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
            contract,
            scenario=changed_claims,
            scenario_context=candidate,
            ledger_clauses=ledger_clauses,
        )
        requests = plan.get("requests")
        if not isinstance(requests, list):
            raise ValueError("scenario faithfulness plan is malformed")
        review_results: list[Mapping[str, Any]] = []
        if requests:
            if client is None:
                raise ValueError("faithfulness classifier is unavailable")
            category_results = [
                complete_json(
                    client,
                    _build_jev_claim_category_messages(request),
                    response_schema=CLAIM_CATEGORY_RESPONSE_SCHEMA,
                )
                for request in requests
                if isinstance(request, Mapping)
            ]
            if len(category_results) != len(requests):
                raise ValueError("scenario claim request is malformed")
            if detail_client is None:
                raise ValueError("faithfulness detail LLM is unavailable")
            detail_response = complete_json(
                detail_client,
                [
                    {
                        "role": "system",
                        "content": (
                            "JEV already classified each claim. Do not change or "
                            "second-guess its category. For each claim, provide exact "
                            "source quotes with clause IDs that explain that category. "
                            "Quotes must be exact substrings of the supplied source "
                            "clauses; never cite the candidate probe. For not_stated, "
                            "identify "
                            "the shortest exact unsupported substring in the candidate "
                            "claim, or use null if none can be isolated. For entailed "
                            "or contradicted claims, unsupported_part must be null. "
                            "Checking a named exposed property for presence supports "
                            "exposure requirement but does not imply its value. Do not "
                            "use common practice or implementation guesses. Return one "
                            "details result for each claim, in input order."
                        ),
                    },
                    {
                        "role": "user",
                        "content": json.dumps(
                            {
                                "requests": requests,
                                "jev_categories": [
                                    dict(item) for item in category_results
                                ],
                            },
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                    },
                ],
                response_schema=CLAIM_DETAILS_RESPONSE_SCHEMA,
            )
            detail_results = detail_response.get("results")
            if not isinstance(detail_results, list) or not all(
                isinstance(item, Mapping) for item in detail_results
            ):
                raise ValueError(
                    "faithfulness detail LLM response has no results array"
                )
            if len(detail_results) != len(requests):
                raise ValueError(
                    "faithfulness detail response count does not match claims"
                )
            review_results = [
                {**dict(category), **dict(details)}
                for category, details in zip(
                    category_results, detail_results, strict=True
                )
            ]
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
                for item in category_results
            ]
        else:
            results = []
        outcome = finalize_scenario_claim_reviews(plan, results, benchmark_mode=True)
        requests = plan.get("requests")
        outcome_reviews = outcome.get("reviews")
        if isinstance(requests, list) and isinstance(outcome_reviews, list):
            enriched_reviews = [
                dict(review)
                for review in outcome_reviews
                if isinstance(review, Mapping)
            ]
            review_by_claim = {
                str(review.get("claim_id")): review for review in enriched_reviews
            }
            for request, result in zip(requests, review_results, strict=True):
                claim = request.get("claim") if isinstance(request, Mapping) else None
                claim_id = claim.get("claim_id") if isinstance(claim, Mapping) else None
                if not isinstance(claim_id, str):
                    continue
                review = review_by_claim.get(claim_id)
                if review is None:
                    continue
                evidence = result.get("source_evidence")
                review["source_evidence"] = (
                    [dict(item) for item in evidence if isinstance(item, Mapping)]
                    if isinstance(evidence, list)
                    else []
                )
                review["source_evidence_valid"] = _source_evidence_is_valid(
                    evidence,
                    context_text=str(request.get("context_text") or ""),
                )
                unsupported_part = result.get("unsupported_part")
                claim_value = (
                    claim.get("candidate_value") if isinstance(claim, Mapping) else None
                )
                review["unsupported_part"] = unsupported_part
                review["unsupported_part_valid"] = (
                    isinstance(unsupported_part, str)
                    and bool(unsupported_part.strip())
                    and isinstance(claim_value, str)
                    and unsupported_part in claim_value
                )
            outcome["reviews"] = enriched_reviews
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
