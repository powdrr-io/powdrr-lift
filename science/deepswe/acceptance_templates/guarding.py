"""Confirm one semantic prerequisite per call before rendering guarded instances."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from .common import (
    IDS,
    TEXT,
    Recorder,
    array_schema,
    checked_ids,
    checked_rows,
    choice_schema,
    object_schema,
    require_text,
)

GUARD_SCHEMA = object_schema(
    support_explanation=TEXT,
    source_ids=IDS,
    source_quotes=array_schema(object_schema(source_id=TEXT, quote=TEXT)),
    decision=choice_schema("yes", "no", "unknown"),
)
GUARD_PROMPT = """Decide one semantic prerequisite for one proposed acceptance
criterion. The proposed criterion and parameter values are hypotheses, not facts.
Use the full instruction to determine whether the prerequisite is required for
this specific requirement, operation, scope, and proposed parameter values.
Answer yes only when the source requires the assertion the renderer would make.
Could an implementation satisfy the instruction while violating that assertion?
If so, answer no and explain that counterexample. Return unknown if the source
is ambiguous. Read-only/equal values do not require the same object reference.
Array ordering is not a collision winner: concatenating both elements does not
choose a winning value for a repeated key. Do not transfer a merge-mode winner
into append mode or another operation. A cited related clause is not entailment.
For yes, provide separate verbatim source quotes for all cited IDs. Negative
decisions may have no quotes when the requisite behavior is absent. Write the
explanation before the final decision; that decision must agree with the source
analysis you just gave. Return JSON."""

RELATION_PROMPT = """Classify one relationship required by the original instruction
for the supplied requirement and operation context. Choose the relation class
describing the observable result. The operation context is an untrusted locator,
not an extra requirement. Do not transfer a rule from another operation or scope.
Do not infer missing outcomes. If both inputs' elements survive concatenation,
classify retaining both in order, not selecting a winner. A keyed conflict
selects a source value only when one input's value is chosen for the same
key/field lookup. Read-only or equal contents do not require a shared reference.
Write source analysis first and the relation class last. Supply separate source
quotes for cited IDs; absent/unclear relations may have empty evidence. Return JSON."""


def confirm_guards(
    task: dict[str, Any],
    inv: dict[str, Any],
    catalog: dict[str, Any],
    decisions: list[dict[str, Any]],
    recorder: Recorder,
) -> list[dict[str, Any]]:
    from .generation import render_instance, validate_evidence

    result = deepcopy(decisions)
    reqs = {row["id"]: row for row in inv["items"]}
    cards = {card["id"]: card for card in catalog["templates"]}
    for row in result:
        card = cards[row["template_id"]]
        if row["applicability"] != "yes" or not card.get("prerequisites"):
            continue
        accepted: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        for i, instance in enumerate(row["instances"]):
            checks = []
            for prerequisite in card["prerequisites"]:
                classes = prerequisite.get("relation_classes")

                def validate(
                    raw: dict[str, Any],
                    classes: dict[str, str] | None = classes,
                    prerequisite: dict[str, Any] = prerequisite,
                ) -> dict[str, Any]:
                    if classes:
                        relation = raw.get("relation")
                        if relation not in classes:
                            raise ValueError("unknown semantic relation class")
                        raw["decision"] = (
                            "yes"
                            if relation == prerequisite["required_relation"]
                            else "unknown"
                            if relation in {"unspecified", "uncertain"}
                            else "no"
                        )
                    if raw.get("decision") not in {"yes", "no", "unknown"}:
                        raise ValueError("unknown prerequisite decision")
                    require_text(raw, "support_explanation")
                    checked_ids(
                        raw.get("source_ids"),
                        {s["id"] for s in task["source_spans"]},
                        "guard sources",
                    )
                    checked_rows(raw, "source_quotes")
                    if (
                        raw["decision"] == "yes"
                        or raw.get("source_ids")
                        or raw.get("source_quotes")
                    ):
                        validate_evidence(raw, task)
                    return raw

                try:
                    prompt = GUARD_PROMPT
                    schema = GUARD_SCHEMA
                    payload = {**task, "requirement": reqs[row["requirement_id"]]}
                    if classes:
                        prompt = RELATION_PROMPT
                        schema = object_schema(
                            support_explanation=TEXT,
                            source_ids=IDS,
                            source_quotes=array_schema(
                                object_schema(source_id=TEXT, quote=TEXT)
                            ),
                            relation=choice_schema(*classes),
                        )
                        payload.update(
                            question=prerequisite["question"],
                            relation_classes=classes,
                            operation_context={
                                slot["name"]: slot["value"]
                                for slot in instance["slots"]
                                if slot["kind"] == "Scope"
                                or slot["name"] in {"operation", "target", "setting"}
                            },
                        )
                    else:
                        payload.update(
                            prerequisite=prerequisite,
                            proposed_criterion=render_instance(card, instance),
                            parameter_values={
                                slot["name"]: slot["value"]
                                for slot in instance["slots"]
                            },
                        )
                    verdict = recorder.call(
                        f"guard-{row['requirement_id']}-{row['template_id']}-{i:03}-{prerequisite['id']}",
                        prompt,
                        payload,
                        schema,
                        validate,
                    )
                except RuntimeError as exc:
                    verdict = {
                        "decision": "unknown",
                        "source_ids": [],
                        "source_quotes": [],
                        "support_explanation": "Prerequisite confirmation unavailable; "
                        "omit this instance and retain the requirement.",
                        "error": str(exc),
                    }
                checks.append({"prerequisite_id": prerequisite["id"], **verdict})
            instance["guard_checks"] = checks
            (
                accepted
                if all(check["decision"] == "yes" for check in checks)
                else rejected
            ).append(instance)
        row["instances"] = accepted
        row["rejected_instances"] = rejected
        if not accepted:
            row["applicability"] = (
                "unknown"
                if any(
                    check["decision"] == "unknown"
                    for instance in rejected
                    for check in instance["guard_checks"]
                )
                else "no"
            )
            row["reason"] = (
                "Independent prerequisite confirmation rejected all instances. "
                + row["reason"]
            )
    return result
