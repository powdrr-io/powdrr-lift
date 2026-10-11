"""Reproduce the agent-authored static comparison, without evaluator model calls."""

import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent

# Labels are static reviewer judgments over the saved outputs, not inferred
# from shared words or template IDs. Each row preserves the cited decision IDs.
INSTRUCTION = [
    ("partial", "a003", "Aspect type export missing"),
    ("partial", "a001,a002,a005", "No deduplication/order rule"),
    ("full", "a001", "Creation errors explicit"),
    ("partial", "a002,a004", "Tag acceptance but no tag schema-field rule"),
    ("partial", "a001,a004", "Fresh instance/unique id but no numeric type"),
    ("missing", "", "No mutability policy"),
    ("partial", "a009", "Initialization but callable configuration/defaults unstated"),
    ("partial", "a006,a007", "Membership and merged get; tag-only data unstated"),
    (
        "partial",
        "a007,a017",
        "New merged object but copy depth and nested aliasing unstated",
    ),
    ("full", "a008", "No-op matches target, incorrectly attributed to instruction"),
    ("partial", "a008,a009,a022", "Unknown-field policy only given for updateEach"),
    ("missing", "", "No explicit notification control"),
    (
        "partial",
        "a009",
        "Missing-only and preserve existing; omitted-value defaults unstated",
    ),
    ("full", "a010", "Removes constituents including when some absent"),
    ("partial", "a011,a035", "Matching and composition but live updates unstated"),
    ("missing", "", "No data-slot rules"),
    (
        "wrong",
        "a021",
        (
            "Every field write notifies; unchanged constituents should not notify in "
            "updateEach"
        ),
    ),
    ("missing", "", "No never mode"),
    ("missing", "", "No non-never mode behavior"),
    ("missing", "", "No callback-destruction guard"),
    ("missing", "", "No notification timing"),
    ("full", "a012,a030", "Not predicate explicit"),
    (
        "wrong",
        "a013,a032",
        "Inconsistent sufficiency: a013 omits completeness whereas a032 includes it",
    ),
    (
        "partial",
        "a014,a015,a033,a034",
        "Transition rules but tracking baseline unspecified",
    ),
    (
        "full",
        "a031",
        "Constituent-level Or matches target, though its rationale is inconsistent",
    ),
    ("wrong", "a023,a024,a025,a026", "Invents merged data callback arguments"),
    ("full", "a039", "Unsubscribe stops all future callbacks"),
    ("missing", "", "No unit of onChange notification"),
    ("missing", "", "No query cache identity"),
    ("missing", "", "No multiple Not set policy"),
    ("missing", "", "No generation-boundary policy"),
    ("missing", "", "No function-schema field-map policy"),
    (
        "partial",
        "a040",
        "Reset state and callbacks but query matching after reset unstated",
    ),
    ("wrong", "a008", "Requires actual data change to notify ordinary set"),
]

REPOSITORY = [
    ("full", "a002", "Exports function and associated types"),
    ("partial", "a001,a004", "Flattening but no deduplication/order rule"),
    ("full", "a001", "Creation errors explicit"),
    ("partial", "a001,a003", "No explicit tag schema behavior"),
    ("full", "a001,a003", "Fresh aspect with unique numeric id"),
    ("partial", "a003", "Readonly id only; no container freezing"),
    ("partial", "a008", "No callable/default configuration form"),
    ("partial", "a005,a006", "has/get core behavior but no tag-only output"),
    ("partial", "a006,a011", "New object but no depth/aliasing contract"),
    ("missing", "", "No incomplete set rule"),
    ("partial", "a007,a008", "Field ownership but unknown/omitted fields unspecified"),
    ("missing", "", "No triggerChanged false"),
    ("partial", "a008", "Missing-only and preserve existing; defaults missing"),
    ("full", "a009", "Absent constituents explicitly tolerated"),
    ("partial", "a010", "All-present but no live matching/composition examples"),
    ("missing", "", "No callback-slot policy"),
    ("partial", "a012", "Changed fields but no strict shallow comparison"),
    ("missing", "", "No never mode"),
    ("missing", "", "No non-never behavior"),
    ("missing", "", "No destruction guard"),
    ("missing", "", "No dispatch timing"),
    ("full", "a014", "Not predicate explicit"),
    ("full", "a016", "Completeness and any changed constituent explicit"),
    ("partial", "a017,a018,a019", "Transition rules but tracking history unstated"),
    ("full", "a015", "Constituent alternatives match solution"),
    ("full", "a020,a021,a022", "Membership-gated callbacks with entity argument"),
    ("full", "a023", "Unsubscribe contract explicit"),
    ("full", "a013", "Per-constituent callback unit, explicitly a chosen assumption"),
    ("missing", "", "No query cache identity"),
    ("missing", "", "No multiple negative set policy"),
    ("missing", "", "No generation policy"),
    ("missing", "", "No function-schema field map behavior"),
    ("partial", "a024", "Reset/registration stated but query result behavior unstated"),
    (
        "full",
        "a007",
        "Writes notify each recipient constituent without requiring a value difference",
    ),
]

FOCUSED = [
    ("full", "g002", "Exports public associated type"),
    (
        "full",
        "f011,g001",
        "Deduplication, first order and unique-count minimum explicit",
    ),
    ("full", "g001", "Creation errors retained"),
    (
        "partial",
        "g001,f005",
        "Tag-only aspect accepted but schema contribution not explicit",
    ),
    ("full", "g001,g003", "Fresh unique numeric id retained"),
    ("wrong", "f012", "Deep freezing is stronger than the target's shallow freeze"),
    ("partial", "g008,f003", "No explicit callable configuration/default form"),
    ("partial", "g005,g006", "No explicit tag-only get output"),
    (
        "unresolved",
        "f004",
        "Correct shallow/shared-reference proposal but evidence failed verification",
    ),
    (
        "full",
        "f001",
        "No-op policy matches, but attribution to instruction is unsupported",
    ),
    ("full", "f003", "Ignored unknown fields and preserved omitted values"),
    (
        "unresolved",
        "f002",
        "Correct pass-through proposal; no verified citation and downgraded confidence",
    ),
    (
        "partial",
        "g008,f003",
        (
            "Missing-only/preserved existing, omitted-value initialization defaults "
            "unstated"
        ),
    ),
    ("full", "g009", "Absent removal tolerated"),
    ("partial", "g010", "Live membership and explicit composition details unstated"),
    (
        "wrong",
        "f005",
        (
            "Nested aspects incorrectly split into constituent callback slots; "
            "ordinary tag omission unclear"
        ),
    ),
    ("full", "f006,g012", "Shallow comparison and per-constituent writeback"),
    ("full", "f007", "Never commits without notification"),
    (
        "partial",
        "f007",
        (
            "Named modes inherit current behavior; shared non-never aspect branch "
            "not explicit"
        ),
    ),
    ("full", "f008", "Destroyed entities skipped"),
    ("full", "f009", "Notifications after iteration"),
    ("full", "g014", "Single Not predicate retained"),
    ("full", "f013", "Changed requires complete aspect"),
    ("partial", "g017,g018,g019", "Tracking baseline/history still unstated"),
    (
        "full",
        "f014",
        "Exact constituent-level Or matches; evidence attribution is overstated",
    ),
    ("full", "g020,g021,g022", "Membership-gated callbacks with entity argument"),
    ("full", "f015", "Detach constituent subscriptions, idempotent unsubscribe"),
    (
        "unresolved",
        "f010",
        "Correct per-constituent proposal but no verified repository citation",
    ),
    ("missing", "", "No aspect-instance query hash policy"),
    ("missing", "", "No aggregation of multiple Not sets"),
    ("missing", "", "No first-generation tracking/per-generation negative policy"),
    ("missing", "", "No function-schema field-map policy"),
    (
        "unresolved",
        "f016",
        "Correct reset proposal but no verified repository citation",
    ),
    (
        "unresolved",
        "f002",
        "Same-value notification proposed correctly, not a resolved mandate",
    ),
]


def main() -> None:
    oracle = json.loads((HERE / "oracle.json").read_text())
    manifest = json.loads((HERE / "manifest.json").read_text())
    oracle_hash = hashlib.sha256((HERE / "oracle.json").read_bytes()).hexdigest()
    assert oracle_hash == manifest["oracle_sha256_before_analysis"]
    arms = {
        "instruction-only": INSTRUCTION,
        "repository-grounded": REPOSITORY,
        "focused": FOCUSED,
    }
    artifacts = {"oracle": oracle_hash}
    for arm in arms:
        artifacts[arm] = hashlib.sha256(
            (HERE / arm / "analysis.json").read_bytes()
        ).hexdigest()
    prior = json.loads((HERE / "prior-prompt-review.json").read_text())
    rows = []
    for index, target in enumerate(oracle["decisions"]):
        row = {
            "id": target["id"],
            "target": target["target"],
            "category": target["category"],
        }
        row["prior-candidate"] = next(
            r for r in prior["rows"] if r["id"] == target["id"]
        )
        for arm, labels in arms.items():
            status, refs, note = labels[index]
            row[arm] = {
                "status": status,
                "decision_ids": refs.split(",") if refs else [],
                "explanation": note,
            }
        rows.append(row)
    summary = {}
    for arm in ["prior-candidate", *arms]:
        summary[arm] = {
            "all_target_entries": dict(Counter(r[arm]["status"] for r in rows)),
            "excluding_edge_entries": dict(
                Counter(r[arm]["status"] for r in rows if r["category"] != "edge")
            ),
        }
    write_json = runner_write_json
    write_json(
        HERE / "decision-review.json",
        {
            ("reviewer"): (
                "Codex agent-authored static review; not human gold or "
                "code-execution results"
            ),
            ("rubric"): (
                "full=entire target bundle resolved; partial=some facets absent; "
                "wrong=explicit conflict with authoritative target; missing=no "
                "policy; unresolved=matching proposal explicitly withheld. Evidence "
                "support is a separate concern. Focused policy statements override "
                "broad statements on the same topic."
            ),
            "target_entry_count": len(rows),
            "artifacts_sha256": artifacts,
            "summary": summary,
            "rows": rows,
        },
    )
    print(json.dumps(summary, indent=2))


def runner_write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n")


if __name__ == "__main__":
    main()
