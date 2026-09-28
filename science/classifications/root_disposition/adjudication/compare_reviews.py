#!/usr/bin/env python3
"""Compare two blinded annotation sheets and prepare adjudication work."""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

LABELS = (
    "context",
    "entity",
    "feature",
    "formatting_artifact",
    "guidance",
    "interface",
    "invariant",
    "non_goal",
    "nonactionable",
    "unresolved",
)
UNRESOLVED_REASONS = {
    "source_ambiguous",
    "source_underspecified",
    "unsupported_concept",
}
CONFIDENCE_LEVELS = {"high", "medium", "low"}
ROOT = Path(__file__).resolve().parents[4]


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _write_jsonl(path: Path, values: list[dict[str, Any]]) -> None:
    path.write_text(
        "".join(
            json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n"
            for value in values
        ),
        encoding="utf-8",
    )


def _index_reviews(
    rows: list[dict[str, Any]], reviewer: str
) -> dict[str, dict[str, Any]]:
    indexed = {row["review_id"]: row for row in rows}
    if len(indexed) != len(rows):
        raise ValueError(f"annotator {reviewer} sheet contains duplicate review IDs")
    for review_id, row in indexed.items():
        label = row.get("label")
        if label not in LABELS:
            raise ValueError(
                f"{reviewer} has an invalid label for {review_id}: {label!r}"
            )
        if row.get("confidence") not in CONFIDENCE_LEVELS:
            raise ValueError(f"{reviewer} has no valid confidence for {review_id}")
        reason = row.get("unresolved_reason")
        if label == "unresolved" and reason not in UNRESOLVED_REASONS:
            raise ValueError(
                f"{reviewer} must add an unresolved reason for {review_id}"
            )
        if label != "unresolved" and reason is not None:
            raise ValueError(f"{reviewer} must clear unresolved_reason for {review_id}")
    return indexed


def _cohen_kappa(
    counts_a: Counter[str], counts_b: Counter[str], total: int, agreements: int
) -> float | None:
    if not total:
        return None
    expected = sum(counts_a[label] * counts_b[label] for label in LABELS) / total**2
    observed = agreements / total
    if expected == 1:
        return 1.0 if observed == 1 else 0.0
    return (observed - expected) / (1 - expected)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--batch-dir", type=Path, default=Path(__file__).resolve().parent
    )
    parser.add_argument(
        "--dataset",
        type=Path,
        default=ROOT
        / "science/classifications/root_disposition/data/root_disposition.jsonl",
    )
    args = parser.parse_args()

    key_rows = _read_jsonl(args.batch_dir / "selection_key.jsonl")
    key_by_id = {row["review_id"]: row for row in key_rows}
    if len(key_by_id) != len(key_rows):
        raise ValueError("selection key contains duplicate review IDs")
    source_rows = {row["example_id"]: row for row in _read_jsonl(args.dataset)}
    reviewer_a = _index_reviews(_read_jsonl(args.batch_dir / "annotator_a.jsonl"), "a")
    reviewer_b = _index_reviews(_read_jsonl(args.batch_dir / "annotator_b.jsonl"), "b")
    expected_ids = set(key_by_id)
    if set(reviewer_a) != expected_ids or set(reviewer_b) != expected_ids:
        raise ValueError(
            "both reviewer sheets must contain exactly the selected item IDs"
        )

    previous_adjudication_path = args.batch_dir / "adjudication.jsonl"
    if previous_adjudication_path.exists():
        previous_rows = _read_jsonl(previous_adjudication_path)
        if any(
            row.get("final_label") or row.get("adjudicator") for row in previous_rows
        ):
            raise FileExistsError(
                "adjudication.jsonl already has human resolutions; preserve it and "
                "compare into a new --batch-dir"
            )

    counts_a: Counter[str] = Counter()
    counts_b: Counter[str] = Counter()
    confusion: dict[str, Counter[str]] = defaultdict(Counter)
    per_label: dict[str, Counter[str]] = defaultdict(Counter)
    adjudication_rows = []
    worklist = []
    agreements = 0
    for review_id in sorted(expected_ids):
        left = reviewer_a[review_id]
        right = reviewer_b[review_id]
        if left["proposition"] != right["proposition"]:
            raise ValueError(f"reviewers saw different propositions for {review_id}")
        key = key_by_id[review_id]
        source = source_rows.get(key["example_id"])
        if source is None:
            raise ValueError(f"source example is missing for {review_id}")
        if (
            source["inputs"]["proposition"] != left["proposition"]
            or source["source"]["source_family_id"] != key["source_family_id"]
        ):
            raise ValueError(f"review packet provenance mismatch for {review_id}")
        label_a = left["label"]
        label_b = right["label"]
        counts_a[label_a] += 1
        counts_b[label_b] += 1
        confusion[label_a][label_b] += 1
        per_label[label_a]["reviewer_a_items"] += 1
        if label_a == label_b:
            agreements += 1
            per_label[label_a]["agreements"] += 1
        needs_adjudication = (
            label_a != label_b or label_a == "unresolved" or label_b == "unresolved"
        )
        final_label = label_a if not needs_adjudication else None
        adjudication_rows.append(
            {
                "review_id": review_id,
                "final_label": final_label,
                "resolution_status": (
                    "reviewer_agreement" if final_label else "needs_adjudication"
                ),
                "unresolved_reason": None,
                "rationale": None,
                "adjudicator": None,
            }
        )
        if needs_adjudication:
            worklist.append(
                {
                    "review_id": review_id,
                    "proposition": left["proposition"],
                    "reviewer_a": {
                        "label": label_a,
                        "unresolved_reason": left["unresolved_reason"],
                        "confidence": left["confidence"],
                        "notes": left["notes"],
                    },
                    "reviewer_b": {
                        "label": label_b,
                        "unresolved_reason": right["unresolved_reason"],
                        "confidence": right["confidence"],
                        "notes": right["notes"],
                    },
                    "final_label": None,
                    "unresolved_reason": None,
                    "rationale": None,
                    "adjudicator": None,
                }
            )

    total = len(expected_ids)
    report = {
        "schema_version": "root-disposition-interannotator-report-v1",
        "item_count": total,
        "agreements": agreements,
        "raw_agreement": agreements / total if total else None,
        "cohen_kappa": _cohen_kappa(counts_a, counts_b, total, agreements),
        "needs_adjudication": len(worklist),
        "gold_set_complete": not worklist,
        "label_marginals": {
            label: {"reviewer_a": counts_a[label], "reviewer_b": counts_b[label]}
            for label in LABELS
        },
        "confusion_matrix": {
            label: {candidate: confusion[label][candidate] for candidate in LABELS}
            for label in LABELS
        },
        "reviewer_a_agreement_by_label": {
            label: {
                "items": per_label[label]["reviewer_a_items"],
                "agreements": per_label[label]["agreements"],
                "agreement_rate": (
                    per_label[label]["agreements"]
                    / per_label[label]["reviewer_a_items"]
                    if per_label[label]["reviewer_a_items"]
                    else None
                ),
            }
            for label in LABELS
        },
    }
    _write_json(args.batch_dir / "review-agreement-report.json", report)
    _write_jsonl(args.batch_dir / "adjudication.jsonl", adjudication_rows)
    _write_jsonl(args.batch_dir / "adjudication-worklist.jsonl", worklist)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
