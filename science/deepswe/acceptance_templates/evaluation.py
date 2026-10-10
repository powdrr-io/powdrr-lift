"""Two-direction semantic review and deterministic precision/recall accounting."""

from __future__ import annotations

from copy import deepcopy
from functools import partial
from pathlib import Path
from typing import Any

from .common import (
    IDS,
    TEXT,
    Recorder,
    array_schema,
    checked_ids,
    checked_rows,
    choice_schema,
    digest,
    object_schema,
    require_text,
)
from .generation import requirements

COVERAGE_SCHEMA = object_schema(
    coverage=array_schema(
        object_schema(
            reference_id=TEXT,
            inventory_status=choice_schema("full", "partial", "missing"),
            inventory_requirement_ids=IDS,
            criterion_status=choice_schema("full", "partial", "missing"),
            criterion_ids=IDS,
            rationale=TEXT,
        )
    )
)
SUPPORT_SCHEMA = object_schema(
    support=array_schema(
        object_schema(
            criterion_id=TEXT,
            status=choice_schema("supported", "partial", "unsupported", "uncertain"),
            source_ids=IDS,
            rationale=TEXT,
        )
    )
)

COVERAGE_PROMPT = """Review acceptance-criterion coverage against an independent
instruction-grounded reference inventory. References are evaluation input only;
the generator did not receive them. For each reference, independently assess
whether the extracted product requirements and the generated criteria explicitly
capture its complete required behavior. Different wording or a different valid
template is fine. All conditions, exceptions, timing, scope, quantifiers, and
expected outcomes must survive for full. A generic 'supports X' is not full.
Combine several criteria if needed. Do not credit a missing outcome because an
agent could infer it from the original instruction or from unrendered slots.
Residual requirement text is a fallback, not a generated validation, and is not
scored as criterion coverage. Do not evaluate code execution or reproduce an
incidental solution choice. Identify partial coverage rather than claiming full.
Every supplied reference must have one row. Full/partial requires cited IDs;
missing requires an empty ID list. Coverage is satisfied by a sufficient set of
criteria: a weak, duplicate, or unsupported extra criterion does not erase a
correct criterion that fully states the reference. Evaluate those extras in the
separate support audit. Do not downgrade coverage merely because another related
criterion is vague. Return JSON only."""


def coverage_response_schema(
    references: list[dict[str, Any]], generation: dict[str, Any]
) -> dict[str, Any]:
    """Constrain citation namespaces and verdict count before decoding."""
    schema = deepcopy(COVERAGE_SCHEMA)
    props = schema["properties"]["coverage"]["items"]["properties"]
    props["reference_id"] = choice_schema(*(row["id"] for row in references))
    for field, rows in (
        ("inventory_requirement_ids", requirements(generation["inventory"])),
        ("criterion_ids", generation["criteria"]),
    ):
        props[field] = (
            array_schema(choice_schema(*(row["id"] for row in rows)))
            if rows
            else {"type": "array", "items": TEXT, "maxItems": 0}
        )
    schema["properties"]["coverage"].update(
        minItems=len(references), maxItems=len(references)
    )
    return schema


def rendered_criteria(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Grade final text; hide unrendered slots and generator explanations."""
    return [{"id": row["id"], "text": row["text"]} for row in rows]


SUPPORT_PROMPT = """Review every emitted acceptance criterion against the original
instruction. This is a separate precision audit, not a patch-test similarity
check. Supported means every mandated predicate is source-supported and concrete
enough to verify. Partial means some predicates are supported but others add or
strengthen obligations, or remain vague. Unsupported means the criterion adds
an obligation without support. Uncertain means the source itself is ambiguous.
A cited source ID alone is not entailment. Check defaults, negation, copy depth,
cardinality, timing, errors, identity, and allowed alternatives. Illustrative
values are permissible only as examples of a general supported rule. Duplicate
wording is not by itself a false positive. Process/branch/commit work must not be
mandated of the coding agent. Return one row per supplied criterion with exact
source IDs and an explanation. Read-only or equal contents do not imply object
identity; ordering does not imply conflict precedence. Respecting a flag does
not invent its policy for result fields. Evaluate all clauses of compound
criteria independently before assigning their combined support status. Preserve
operation scope and exceptions in the full source; a rule for coalescing does
not automatically apply during merging. Return JSON only."""


def validate_coverage(
    raw: dict[str, Any], *, references: list[dict[str, Any]], generation: dict[str, Any]
) -> list[dict[str, Any]]:
    expected = {row["id"] for row in references}
    reqs = {row["id"] for row in requirements(generation["inventory"])}
    criteria = {row["id"] for row in generation["criteria"]}
    rows = checked_rows(raw, "coverage")
    seen = set()
    for row in rows:
        rid = row.get("reference_id")
        if rid not in expected or rid in seen:
            raise ValueError("unknown/duplicate coverage reference")
        seen.add(rid)
        for prefix, field, allowed in (
            ("inventory", "inventory_requirement_ids", reqs),
            ("criterion", "criterion_ids", criteria),
        ):
            status = row.get(prefix + "_status")
            if status not in {"full", "partial", "missing"}:
                raise ValueError("unknown coverage status")
            ids = checked_ids(row.get(field), allowed, field)
            if bool(ids) != (status != "missing"):
                raise ValueError(
                    "missing coverage must have no IDs; other statuses need IDs"
                )
        require_text(row, "rationale")
    if seen != expected:
        raise ValueError("coverage review omitted references")
    return rows


def validate_support(
    raw: dict[str, Any], *, criteria: list[dict[str, Any]], task: dict[str, Any]
) -> list[dict[str, Any]]:
    expected = {row["id"] for row in criteria}
    sources = {row["id"] for row in task["source_spans"]}
    rows = checked_rows(raw, "support")
    seen = set()
    for row in rows:
        cid = row.get("criterion_id")
        if cid not in expected or cid in seen:
            raise ValueError("unknown/duplicate support criterion")
        seen.add(cid)
        if row.get("status") not in {
            "supported",
            "partial",
            "unsupported",
            "uncertain",
        }:
            raise ValueError("unknown support status")
        ids = checked_ids(row.get("source_ids"), sources, "support sources")
        if row["status"] == "supported" and not ids:
            raise ValueError("supported criterion requires evidence")
        require_text(row, "rationale")
    if seen != expected:
        raise ValueError("support review omitted criteria")
    return rows


def review(
    task: dict[str, Any],
    generation: dict[str, Any],
    reference: dict[str, Any],
    recorder: Recorder,
    batch_size: int = 10,
    atomic_support: bool = False,
) -> dict[str, Any]:
    if reference["instruction_sha256"] != digest(task["instruction"]):
        raise ValueError(
            "reference instruction fingerprint differs from generation input"
        )
    coverage = []
    support = []
    references = reference["validations"]
    for offset in range(0, len(references), batch_size):
        batch = references[offset : offset + batch_size]
        coverage.extend(
            recorder.call(
                f"coverage-{offset // batch_size:03}",
                COVERAGE_PROMPT,
                {
                    **task,
                    "references": batch,
                    "requirements": requirements(generation["inventory"]),
                    "criteria": rendered_criteria(generation["criteria"]),
                },
                coverage_response_schema(batch, generation),
                partial(validate_coverage, references=batch, generation=generation),
            )
        )
    criteria = generation["criteria"]
    if atomic_support:
        from .atomic_review import review_assertions

        for criterion in criteria:
            result = review_assertions(task, criterion, recorder)
            support.append(
                {
                    "criterion_id": criterion["id"],
                    "status": result["derived_status"],
                    "source_ids": sorted(
                        {
                            sid
                            for row in result["assertions"]
                            for sid in row["source_ids"]
                        }
                    ),
                    "rationale": "Derived from isolated assertion judgments: "
                    + "; ".join(
                        f"{row['text']}: {row['status']} — {row['rationale']}"
                        for row in result["assertions"]
                    ),
                    "assertions": result["assertions"],
                }
            )
    for offset in range(0, 0 if atomic_support else len(criteria), batch_size):
        batch = criteria[offset : offset + batch_size]
        support.extend(
            recorder.call(
                f"support-{offset // batch_size:03}",
                SUPPORT_PROMPT,
                {**task, "criteria": rendered_criteria(batch)},
                SUPPORT_SCHEMA,
                partial(validate_support, criteria=batch, task=task),
            )
        )
    return {
        "task_id": task["task_id"],
        "arm": generation["arm"],
        "generation_sha256": digest(generation),
        "reference_sha256": digest(reference),
        "review_status": "automated",
        "support_method": "isolated_assertions"
        if atomic_support
        else "criterion_batches",
        "reviewer": {"provider": recorder.provider, "model": recorder.model},
        "coverage": coverage,
        "support": support,
        "calls": recorder.calls,
    }


def ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def score(
    generation: dict[str, Any],
    reference: dict[str, Any],
    judgments: dict[str, Any],
    task: dict[str, Any],
) -> dict[str, Any]:
    if (
        generation.get("input_sha256") != digest(task)
        or generation.get("task_id") != task["task_id"]
        or judgments.get("task_id") != task["task_id"]
        or judgments.get("arm") != generation["arm"]
    ):
        raise ValueError("score inputs must describe the same task and instruction")
    if judgments.get("generation_sha256") != digest(generation) or judgments.get(
        "reference_sha256"
    ) != digest(reference):
        raise ValueError(
            "review does not describe these exact generation/reference artifacts"
        )
    coverage = validate_coverage(
        judgments, references=reference["validations"], generation=generation
    )
    support = validate_support(judgments, criteria=generation["criteria"], task=task)
    support_by_id = {row["criterion_id"]: row for row in support}
    route_metrics: dict[str, dict[str, int | float]] = {}
    for criterion in generation["criteria"]:
        route = criterion.get("route") or "unrouted"
        metrics = route_metrics.setdefault(
            route,
            {
                "criteria": 0,
                "supported": 0,
                "unsupported": 0,
                "partial": 0,
                "uncertain": 0,
            },
        )
        metrics["criteria"] += 1
        status = support_by_id[criterion["id"]]["status"]
        status_key = {"partly_supported": "partial"}.get(status, status)
        if status_key in metrics:
            metrics[status_key] += 1
    status = judgments.get("review_status")
    if status not in {"automated", "human_reviewed"}:
        raise ValueError("review status must remain explicit")
    n = len(coverage)
    full = sum(row["criterion_status"] == "full" for row in coverage)
    partial = sum(row["criterion_status"] == "partial" for row in coverage)
    supported = sum(row["status"] == "supported" for row in support)
    reference_map = {row["id"]: row for row in reference["validations"]}
    selected = {
        row["requirement_id"]: set(row["template_ids"])
        for row in generation["selections"]
    }
    accepted: dict[str, set[str]] = {}
    for row in generation["decisions"]:
        if row["applicability"] == "yes":
            accepted.setdefault(row["requirement_id"], set()).add(row["template_id"])
    candidate_hits = accepted_hits = 0
    eligible = 0
    for row in coverage:
        groups = reference_map[row["reference_id"]].get("template_groups", [])
        if groups and generation["arm"] == "templates":
            eligible += 1
            candidates = set().union(
                *(selected.get(rid, set()) for rid in row["inventory_requirement_ids"])
            )
            bound = set().union(
                *(accepted.get(rid, set()) for rid in row["inventory_requirement_ids"])
            )
            candidate_hits += all(set(group) & candidates for group in groups)
            accepted_hits += all(set(group) & bound for group in groups)
    calls = generation.get("calls", [])
    usage_records = [row["usage"] for row in calls if row.get("usage")]
    return {
        "task_id": generation["task_id"],
        "arm": generation["arm"],
        "input_sha256": generation["input_sha256"],
        "generation_sha256": digest(generation),
        "reference_sha256": digest(reference),
        "review_status": status,
        "reference_label_status": reference["label_status"],
        "reference_count": n,
        "full": full,
        "partial": partial,
        "missed_or_partial": n - full,
        "missing": n - full - partial,
        "strict_recall": ratio(full, n),
        "including_partial_recall": ratio(full + partial, n),
        "inventory_recall": ratio(
            sum(row["inventory_status"] == "full" for row in coverage), n
        ),
        "all_reference_validations_present": full == n,
        "criterion_count": len(support),
        "supported": supported,
        "strict_precision": ratio(supported, len(support)),
        "unsupported": sum(row["status"] == "unsupported" for row in support),
        "partly_supported": sum(row["status"] == "partial" for row in support),
        "uncertain": sum(row["status"] == "uncertain" for row in support),
        "criteria_by_route": route_metrics,
        "candidate_template_label_recall": ratio(candidate_hits, eligible),
        "accepted_template_label_recall": ratio(accepted_hits, eligible),
        "template_label_reference_count": eligible,
        "template_label_note": "Diagnostic agreement with reference template groups, "
        "not semantic correctness; valid alternative templates can disagree.",
        "residual_requirement_count": len(generation["residual_requirements"]),
        "generation_call_count": len(calls),
        "criterion_characters": sum(
            len(row.get("text", "")) for row in generation["criteria"]
        ),
        "generation_elapsed_seconds": round(
            sum(row["elapsed_seconds"] for row in calls), 3
        ),
        "generation_usage": {
            key: (
                sum(row.get(key, 0) or 0 for row in usage_records)
                if usage_records
                else None
            )
            for key in ("prompt_tokens", "completion_tokens", "total_tokens")
        },
        "generation_usage_record_count": len(usage_records),
        "shared_inventory_calls": generation.get("shared_inventory_calls", []),
        "misses": [row for row in coverage if row["criterion_status"] != "full"],
        "precision_flags": [row for row in support if row["status"] != "supported"],
    }


def aggregate(scores: list[dict[str, Any]]) -> dict[str, Any]:
    result = {}
    for arm in sorted({row["arm"] for row in scores}):
        rows = [row for row in scores if row["arm"] == arm]
        refs = sum(row["reference_count"] for row in rows)
        criteria = sum(row["criterion_count"] for row in rows)
        result[arm] = {
            "task_count": len(rows),
            "review_statuses": sorted({row["review_status"] for row in rows}),
            "reference_label_statuses": sorted(
                {row["reference_label_status"] for row in rows}
            ),
            "reference_count": refs,
            "criterion_count": criteria,
            "strict_recall": ratio(sum(row["full"] for row in rows), refs),
            "strict_precision": ratio(sum(row["supported"] for row in rows), criteria),
            "missed_or_partial": sum(row["missed_or_partial"] for row in rows),
            "unsupported": sum(row["unsupported"] for row in rows),
            "partly_supported": sum(row["partly_supported"] for row in rows),
            "uncertain": sum(row["uncertain"] for row in rows),
            "complete_task_fraction": ratio(
                sum(row["all_reference_validations_present"] for row in rows), len(rows)
            ),
        }
    return result


def report_markdown(report: dict[str, Any], output: Path) -> None:
    lines = [
        "# Acceptance criteria pilot",
        "",
        "Scores are automated semantic estimates against agent-authored reference "
        "labels, unless a review is explicitly marked human_reviewed. They do not "
        "measure coding outcomes or certify correctness.",
        "",
        "| Task | Arm | Full / reference | Recall | Supported / criteria | Precision |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for row in report["scores"]:
        recall = (
            "n/a" if row["strict_recall"] is None else f"{row['strict_recall']:.1%}"
        )
        precision = (
            "n/a"
            if row["strict_precision"] is None
            else f"{row['strict_precision']:.1%}"
        )
        lines.append(
            f"| {row['task_id']} | {row['arm']} | "
            f"{row['full']}/{row['reference_count']} | "
            f"{recall} | {row['supported']}/{row['criterion_count']} | {precision} |"
        )
    for row in report["scores"]:
        lines += ["", f"## {row['task_id']}: {row['arm']}", ""]
        if not row["misses"] and not row["precision_flags"]:
            lines.append(
                "No omissions or unsupported assertions flagged "
                "by this automated review."
            )
        for miss in row["misses"]:
            lines.append(
                f"- {miss['reference_id']} ({miss['criterion_status']}): "
                f"{miss['rationale']}"
            )
        for flag in row["precision_flags"]:
            lines.append(
                f"- {flag['criterion_id']} ({flag['status']}): {flag['rationale']}"
            )
    if report["failures"]:
        lines += ["", "## Incomplete runs", ""]
        lines.extend(
            f"- {row['task_id']} / "
            f"{row.get('arm', row.get('stage', 'unknown'))}: {row['error']}"
            for row in report["failures"]
        )
    output.write_text("\n".join(lines) + "\n")
