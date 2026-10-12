"""Static solution review; kept separate from candidate generation inputs."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
RUNS = HERE.parent
ORACLE = RUNS / "operation-analysis-v1/oracle.json"
GENERATED = RUNS / "operation-analysis-v1/focused/prompt.md"
HAND = RUNS / "six-section-hard-task-v1/koota-composite-trait-aspects/prompt.md"
CANDIDATE = HERE / "candidate-prompt.md"
CONTROL = HERE / "same-analysis-prompt.md"

CONTROL_IDS = [
    "g002",
    "f011",
    "g001",
    "f005",
    "g003",
    "f012",
    "g008",
    "g006",
    "g006",
    "f001",
    "f003",
    "g007",
    "g008",
    "g009",
    "g010",
    "f005",
    "f006",
    "f007",
    "f007",
    "f008",
    "f009",
    "g014",
    "f013",
    "g017",
    "f014",
    "g020",
    "f015",
    "g022",
    "g003",
    "g014",
    "g019",
    "g003",
    "g024",
    "g007",
]
CONTROL_OVERRIDES = {
    "D07": (
        "partial",
        "No callable configured add signature; invented two-argument add is gone.",
    ),
    "D09": ("missing", "The shallow/shared read rule f004 is unresolved and withheld."),
    "D19": (
        "partial",
        (
            "Mode behavior refers to currently implemented behavior; exact "
            "non-never equivalence is not established."
        ),
    ),
    "D28": (
        "missing",
        "Per-constituent aspect callback multiplicity f010 is unresolved and withheld.",
    ),
    "D33": (
        "missing",
        (
            "g024 describes reset subscriptions/registration, but the "
            "query-result reset rule f016 is unresolved and withheld."
        ),
    ),
    "D34": (
        "partial",
        (
            "Per-written-constituent detection is stated, but explicit "
            "equal-value notification rule f002 is unresolved and withheld."
        ),
    ),
}

# Explicit single-author judgments on 34 existing target bundles. Each tuple:
# previous generated status, new status, previous exact quote, new exact quote,
# reason for the new status. No model or solution input feeds the builder.
REVIEWS = {
    "D01": (
        "full",
        "partial",
        "its associated types",
        "The core exports a new `createAspect`",
        "Aspect type export remains unspecified.",
    ),
    "D02": (
        "full",
        "partial",
        "deduplicated, preserving first occurrence order",
        "Nested aspects flatten to their individual traits.",
        (
            "No identity deduplication, first-occurrence order or minimum "
            "unique flattened count."
        ),
    ),
    "D03": (
        "full",
        "full",
        "It throws an error if any constituent is a relation",
        "Relation constituents throw at creation time.",
        "Both construction errors are explicit, including overlapping fields in R2.",
    ),
    "D04": (
        "full",
        "partial",
        "tag-only aspects yielding an empty object",
        "Tag traits are valid constituents.",
        (
            "Tag validity is retained; merged schema contents and tag schema "
            "contribution are not specified."
        ),
    ),
    "D05": (
        "full",
        "partial",
        "unique numeric `id`",
        "Each `createAspect` call returns a distinct instance.",
        (
            "Distinct instances are retained, but numeric and distinct IDs are "
            "not established."
        ),
    ),
    "D06": (
        "wrong",
        "partial",
        "deeply frozen `schema`",
        "The aspect's id, traits and schema properties cannot be reassigned.",
        (
            "Assignment protection is correct; shallow traits/schema freezes "
            "and nested reference preservation are absent. Deep-freeze "
            "contradiction removed."
        ),
    ),
    "D07": (
        "wrong",
        "partial",
        "adding traitB via `add(aspect, data)`",
        "`add` distributes initial values by field.",
        (
            "Initial data behavior is retained, but callable aspect(data), "
            "configured add and default behavior are not specified. Wrong "
            "two-argument add signature removed."
        ),
    ),
    "D08": (
        "full",
        "full",
        "returns undefined if any constituent is missing",
        (
            "`get` returns a merged object of all constituent fields, or "
            "undefined if any constituent is missing."
        ),
        (
            "Has/get membership and merged field behavior are explicit; "
            "fieldless tag-only merge necessarily yields an empty object."
        ),
    ),
    "D09": (
        "partial",
        "missing",
        "Unresolved choice: Merged aspect get/readEach data should be a shallow copy",
        "`readEach` delivers a merged data object.",
        (
            "Merged does not decide fresh top-level copies, shared nested "
            "references or write-through."
        ),
    ),
    "D10": (
        "full",
        "missing",
        "it is a no-op if any constituent is missing",
        "`set` distributes each field to its owning constituent.",
        "No set behavior for an entity missing a constituent.",
    ),
    "D11": (
        "full",
        "partial",
        "unknown fields are ignored and omitted fields are left unchanged",
        "`set` distributes each field to its owning constituent.",
        (
            "Ownership routing is explicit; unknown-field ignoring and "
            "omitted-value preservation are not."
        ),
    ),
    "D12": (
        "missing",
        "missing",
        "it always triggers change detection for written fields",
        "when triggerChanged=true",
        (
            "Neither prompt specifies false still writes and suppresses "
            "notifications. Positive-only flag wording does not establish false "
            "behavior."
        ),
    ),
    "D13": (
        "partial",
        "partial",
        "leaves already-present constituents unchanged",
        "`add` adds only the constituents the entity does not already have.",
        (
            "Missing-only addition retained; trait defaults and preservation of "
            "existing constituent data are not spelled out. Previous generated "
            "prompt states preservation but not defaults."
        ),
    ),
    "D14": (
        "full",
        "partial",
        "does nothing for constituents that are already absent",
        "`remove` removes all constituent traits.",
        (
            "Missing-constituent tolerance and unrelated-trait preservation are "
            "not explicit."
        ),
    ),
    "D15": (
        "partial",
        "partial",
        "the query matches only entities that have all constituent traits",
        "An aspect used as a query parameter requires all its constituents.",
        (
            "Complete matching is explicit; live result updates and ordinary "
            "trait/other aspect composition lack concrete conditions in both "
            "prompts."
        ),
    ),
    "D16": (
        "wrong",
        "partial",
        (
            "nested aspects flatten to their constituent traits, each occupying "
            "its own slot"
        ),
        "including when constructed from nested aspects",
        (
            "Nested readEach grouping is correct; tag-only slot, omitted "
            "ordinary tag/Not slots, surrounding order and updateEach grouping "
            "remain unspecified."
        ),
    ),
    "D17": (
        "partial",
        "partial",
        "not shallow-equal to the previously stored value",
        "`updateEach` distributes writes back to constituent stores.",
        (
            "No top-level strict-inequality definition, existing-field limit or "
            "precise per-constituent detection. Earlier prompt mentions shallow "
            "equality without settling all facets."
        ),
    ),
    "D18": (
        "full",
        "missing",
        "'never' commits writes without triggering change detection or notifications",
        "`updateEach` distributes writes back to constituent stores.",
        "Disabled-detection mode semantics absent.",
    ),
    "D19": (
        "full",
        "missing",
        "'always' and 'auto' commit writes and trigger change detection/notifications",
        "`updateEach` distributes writes back to constituent stores.",
        "Non-never modes and their shared detected-change behavior absent.",
    ),
    "D20": (
        "full",
        "missing",
        "aspect writes and notifications for that entity are skipped entirely",
        "`updateEach` distributes writes back to constituent stores.",
        "No destroyed-entity guard.",
    ),
    "D21": (
        "full",
        "missing",
        (
            "dispatches constituent change notifications after the callback "
            "iteration completes"
        ),
        "`updateEach` distributes writes back to constituent stores.",
        "No dispatch timing.",
    ),
    "D22": (
        "partial",
        "partial",
        "matches an entity if and only if the entity lacks at least one",
        "`Not` with an aspect matches entities missing at least one constituent.",
        (
            "Correct predicate includes none, but live membership updates and "
            "composition are not specified."
        ),
    ),
    "D23": (
        "full",
        "partial",
        "all constituent traits are present and at least one constituent has changed",
        "`Changed` matches when any constituent data changed.",
        (
            "Any-change condition retained; current completeness and "
            "recorded-change history remain ambiguous."
        ),
    ),
    "D24": (
        "partial",
        "partial",
        (
            "transitions from having at least one missing constituent to having "
            "all constituents"
        ),
        "`Added` matches the transition to all-present.",
        (
            "Transitions retained, but history observation boundary, spawn and "
            "previously-incomplete removal cases are not settled."
        ),
    ),
    "D25": (
        "full",
        "missing",
        "a disjunction over each individual constituent trait and the regular trait",
        "Aspects compose with all query modifiers.",
        "Compose does not choose constituent OR versus grouped aspect conjunction.",
    ),
    "D26": (
        "partial",
        "partial",
        (
            "`onAdd` fires exactly once when an entity transitions from having "
            "at least one but not all"
        ),
        "`onChange` fires when any constituent changes while all are present.",
        (
            "Core transition/completeness conditions retained; callback "
            "signature and exact constituent subscription gates are not "
            "specified. Earlier generated prose narrows incomplete states "
            "unnecessarily."
        ),
    ),
    "D27": (
        "full",
        "missing",
        "detaches the callback from the aspect's constituent trait subscription sets",
        "`onRemove` fires when an entity transitions from complete to incomplete.",
        "No unsubscribe contract.",
    ),
    "D28": (
        "full",
        "missing",
        "one notification per constituent trait that changed",
        "`onChange` fires when any constituent changes while all are present.",
        "No aspect subscriber multiplicity when two constituents are changed.",
    ),
    "D29": (
        "missing",
        "missing",
        "returns a distinct aspect object",
        "Each `createAspect` call returns a distinct instance.",
        (
            "Instance identity alone does not establish distinct query-cache "
            "representation."
        ),
    ),
    "D30": (
        "missing",
        "missing",
        "matches an entity if and only if the entity lacks at least one",
        "`Not` with an aspect matches entities missing at least one constituent.",
        "No multiple-Not aggregate policy.",
    ),
    "D31": (
        "missing",
        "missing",
        "tracking groups are created per modifier",
        "`Added` matches the transition to all-present.",
        "No first relevant tracking generation or per-generation negative-mask policy.",
    ),
    "D32": (
        "missing",
        "missing",
        "merges the schemas of all constituents",
        "Each aspect exposes `id`, `traits`, and `schema`.",
        "No function-valued schema field ownership policy.",
    ),
    "D33": (
        "full",
        "missing",
        "world.reset clears all aspect query results",
        "Aspects compose with all query modifiers.",
        "No aspect reset lifecycle behavior.",
    ),
    "D34": (
        "full",
        "full",
        "regardless of whether the value changed",
        (
            "still triggers that constituent's change notification when "
            "triggerChanged=true"
        ),
        "Equal-value ordinary setter notifications are correctly specified.",
    ),
}


def load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError("expected an object")
    return value


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def evidence(path: Path, quote: str) -> dict[str, Any]:
    text = path.read_text()
    offset = text.index(quote)
    return {
        "file": str(path.relative_to(RUNS)),
        "line": text[:offset].count("\n") + 1,
        "quote": quote,
    }


def main() -> None:
    oracle = load(ORACLE)
    # Hindsight reference only; the candidate builder never reads this output.
    lines = [
        "# Objective",
        "",
        "Add composite trait aspects with unified entity operations, merged query "
        "data and completeness-aware subscriptions. Preserve the existing "
        "constituent trait storage and notification behavior.",
        "",
        "# Requirements",
        "",
    ]
    for index, row in enumerate(oracle["decisions"], 1):
        lines.append(f"{index}. {row['target']}")
    lines.append(
        "35. Aspect onAdd, onRemove and onChange callbacks are invoked with "
        "the affected entity as the first argument and undefined as the "
        "second argument."
    )
    lines += [
        "",
        "# Constraints",
        "",
        "- Preserve existing ordinary trait, query and subscription APIs.",
        (
            "- Powdrr manages branching and commits; implement the task in the "
            "supplied checkout."
        ),
        "",
        "# Relevant context",
        "",
        "- Existing regular-trait setters notify when triggerChanged is true without "
        "checking equality; aspect updateEach separately compares stored field values.",
        "- Relevant existing paths are packages/core/src/trait, "
        "packages/core/src/query "
        "and packages/core/src/world. The core package is @koota/core.",
        "",
        "# Acceptance criteria",
        "",
        "1. For nested inputs containing repeated trait identities, construction "
        "produces the first-occurrence unique trait list; fewer than two unique "
        "traits, relations and overlapping field names fail.",
        "2. Metadata reassignment and top-level traits/schema mutation cannot change "
        "the public metadata; nested schema objects retain identity and "
        "remain unfrozen.",
        "3. A complete entity returns merged data; an incomplete entity returns "
        "undefined and aspect set performs no writes or notifications. Modifying "
        "a returned top-level field does not persist, while nested "
        "references are shared.",
        "4. Configured add accepts aspect(data), adds missing constituents "
        "with defaults "
        "for omitted fields and preserves existing data. Unknown fields are ignored.",
        "5. Equal-value aspect set notifies an affected constituent with "
        "triggerChanged "
        "true; false still writes but emits no notification. Updating two constituents "
        "can deliver two aspect onChange callbacks while the entity remains complete.",
        "6. A directly supplied nested aspect contributes one merged callback slot; "
        "a tag-only aspect contributes {}; ordinary tags and Not contribute no slots.",
        "7. Aspect updateEach commits recognized fields, compares top-level values "
        "with strict inequality and skips destroyed entities. Never mode commits "
        "without notifications; other modes notify detected changes after iteration.",
        "8. Changed requires current completeness. Or(aspect(A,B),C) matches A OR "
        "B OR C. Added/Removed observe completeness transitions against existing "
        "tracking history, including complete spawn and excluding incomplete removals.",
        "9. Unsubscribe detaches all constituent hooks, distinct aspect "
        "query identities "
        "remain distinct, and aspect querying works after world.reset.",
        "",
        "The remaining numbered requirements, including multiple-Not aggregation, "
        "mask generations and function-schema ownership, are required "
        "completion conditions.",
        "",
        "# Verification and completion",
        "",
        "- Add focused validations for each numbered requirement and run existing "
        "core tests once: pnpm --filter @koota/core test --run.",
        "- Run core lint: pnpm --filter @koota/core lint.",
        "- Report changed files, performed checks, failures and unresolved issues.",
        "",
    ]
    reference = HERE / "ideal-reference-prompt.md"
    reference.write_text("\n".join(lines))
    hand_review = load(RUNS / "operation-analysis-v1/prior-prompt-review.json")
    hand_rows = {row["id"]: row for row in hand_review["rows"]}
    control_records = {
        row["id"]: row
        for row in load(RUNS / "operation-analysis-v1/focused/analysis.json")[
            "decisions"
        ]
    }
    rows = []
    for target in oracle["decisions"]:
        old_status, new_status, old_quote, new_quote, reason = REVIEWS[target["id"]]
        control_status, control_reason = CONTROL_OVERRIDES.get(
            target["id"], (old_status, "Retains the relevant declared analysis rule.")
        )
        control_id = CONTROL_IDS[int(target["id"][1:]) - 1]
        rows.append(
            {
                **target,
                "earlier_hand_candidate": hand_rows[target["id"]],
                "previous_generated": {
                    "status": old_status,
                    "evidence": evidence(GENERATED, old_quote),
                },
                "new_candidate": {
                    "status": new_status,
                    "evidence": evidence(CANDIDATE, new_quote),
                    "reason": reason,
                },
                "same_analysis_control": {
                    "status": control_status,
                    "record_id": control_id,
                    "evidence": evidence(CONTROL, control_records[control_id]["rule"]),
                    "reason": control_reason,
                },
                "ideal_reference": {
                    "status": "full",
                    "evidence": evidence(reference, target["target"]),
                },
            }
        )
    summaries = {}
    for key in (
        "earlier_hand_candidate",
        "previous_generated",
        "new_candidate",
        "same_analysis_control",
        "ideal_reference",
    ):
        summaries[key] = dict(Counter(row[key]["status"] for row in rows))
    report = {
        "reviewer": (
            "Codex static agent review; not human adjudication or a coding experiment"
        ),
        "rubric": (
            "Full: all target facets specified or necessary implications; "
            "partial: some facets; missing: no deciding policy; wrong: explicit "
            "contradiction takes priority."
        ),
        "units": (
            "34 pre-existing target bundles, not atomic requirements or "
            "pass-rate counts."
        ),
        "comparison_limit": (
            "New candidate uses only three added decisions; previous generated "
            "prompt uses a broader analysis. Coverage loss is not a controlled "
            "effect of removing synthesis."
        ),
        "reference_provenance": (
            "No complete saved Koota ideal text found. New offline ideal text "
            "renders the existing frozen solution oracle; earlier hand "
            "candidate kept separate. Existing state-machine ideal is a "
            "different task."
        ),
        "standard_prompt": (
            "Captured standard Powdrr prompt for this task unavailable; "
            "previous_generated is the saved science LLM output."
        ),
        "summaries": summaries,
        "non_edge_summaries": {
            key: dict(
                Counter(row[key]["status"] for row in rows if row["category"] != "edge")
            )
            for key in summaries
        },
        "rows": rows,
        "supplementary_solution_finding": {
            "id": "E01",
            "target": (
                "Aspect callbacks receive (entity, undefined), not exactly one "
                "argument."
            ),
            "solution_patch_lines": [1177, 1218, 1246],
            "previous_generated": {
                "status": "wrong",
                "evidence": evidence(
                    GENERATED, "receive only the entity as the callback argument"
                ),
            },
            "new_candidate": {"status": "missing"},
            "same_analysis_control": {
                "status": "wrong",
                "evidence": evidence(
                    CONTROL, "the callback receives the entity as its sole argument"
                ),
            },
            "earlier_hand_candidate": {
                "status": "partial",
                "reason": "Affected entity specified, second argument omitted.",
            },
            "ideal_reference": {"status": "full", "requirement": 35},
            "counting": (
                "Additional observable patch detail, kept outside the unchanged "
                "34-bundle counts."
            ),
        },
        "artifacts": {
            str(path.relative_to(RUNS)): {
                "sha256": sha(path),
                "words": len(path.read_text().split()),
            }
            for path in (HAND, GENERATED, CANDIDATE, CONTROL, reference, ORACLE)
        },
        "recommendation": (
            "Do not replace the earlier prompt with this candidate. Keep direct "
            "rendering as an assembly principle while restoring lost analysis "
            "coverage with explicit scoped decisions."
        ),
    }
    (HERE / "comparison.json").write_text(json.dumps(report, indent=2) + "\n")
    matrix = [
        "# Solution coverage matrix",
        "",
        (
            "| ID | Target | Earlier hand | Previous generated | New focused "
            "candidate | Same-analysis control |"
        ),
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        matrix.append(
            f"| {row['id']} | {row['operation']} | "
            f"{row['earlier_hand_candidate']['status']} | "
            f"{row['previous_generated']['status']} | "
            f"{row['new_candidate']['status']} | "
            f"{row['same_analysis_control']['status']} |"
        )
    matrix += [
        "",
        (
            "All 34 ideal-reference targets are explicit by construction. Exact "
            "quotations, line locations, source/patch references and reasons "
            "are in comparison.json."
        ),
        "",
    ]
    (HERE / "coverage-matrix.md").write_text("\n".join(matrix))
    print(json.dumps(summaries))


if __name__ == "__main__":
    main()
