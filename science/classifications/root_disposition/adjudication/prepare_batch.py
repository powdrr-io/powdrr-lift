#!/usr/bin/env python3
"""Prepare a blinded, repository-held-out root disposition annotation batch."""

from __future__ import annotations

import argparse
import hashlib
import json
import random
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[4]
BATCH_DIR = Path(__file__).resolve().parent
DATASET_PATH = (
    ROOT / "science/classifications/root_disposition/data/root_disposition.jsonl"
)
TRAINING_REPORT_PATH = (
    ROOT / "science/classifications/root_disposition/training-report.json"
)
SEED = 20260928
MAX_PER_STRATUM = 20
LABELS = (
    "context",
    "entity",
    "feature",
    "guidance",
    "interface",
    "invariant",
    "non_goal",
    "nonactionable",
    "unresolved",
)


def _read_rows(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _stratum(row: dict[str, Any]) -> str:
    label = row["labels"].get("class")
    return str(label) if row["labels"].get("answerable") is True else "unresolved"


def _balanced_sample(
    rows: list[dict[str, Any]], *, stratum: str, count: int, seed: int
) -> list[dict[str, Any]]:
    by_family: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if _stratum(row) == stratum:
            by_family[row["source"]["source_family_id"]].append(row)

    rng = random.Random(f"{seed}:{stratum}")
    families = sorted(by_family)
    rng.shuffle(families)
    for family in families:
        random.Random(f"{seed}:{stratum}:{family}").shuffle(by_family[family])

    selected: list[dict[str, Any]] = []
    while len(selected) < count:
        advanced = False
        for family in families:
            if by_family[family]:
                selected.append(by_family[family].pop())
                advanced = True
                if len(selected) == count:
                    break
        if not advanced:
            break
    return selected


def _review_id(example_id: str, seed: int) -> str:
    digest = hashlib.sha256(f"{seed}:{example_id}".encode()).hexdigest()
    return f"review-{digest[:16]}"


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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DATASET_PATH)
    parser.add_argument("--training-report", type=Path, default=TRAINING_REPORT_PATH)
    parser.add_argument("--output-dir", type=Path, default=BATCH_DIR)
    parser.add_argument("--seed", type=int, default=SEED)
    parser.add_argument("--max-per-stratum", type=int, default=MAX_PER_STRATUM)
    args = parser.parse_args()
    if args.max_per_stratum < 1:
        parser.error("--max-per-stratum must be positive")
    generated_files = (
        "annotator_a.jsonl",
        "annotator_b.jsonl",
        "selection_key.jsonl",
        "adjudication.jsonl",
        "batch-manifest.json",
        "holdout-families.json",
    )
    existing_files = [
        filename
        for filename in generated_files
        if (args.output_dir / filename).exists()
    ]
    if existing_files:
        raise FileExistsError(
            "refusing to overwrite an existing annotation packet: "
            + ", ".join(existing_files)
            + "; choose a new --output-dir"
        )

    dataset_bytes = args.dataset.read_bytes()
    rows = [json.loads(line) for line in dataset_bytes.decode().splitlines() if line]
    report = json.loads(args.training_report.read_text(encoding="utf-8"))
    holdout_families = sorted(report["split"]["test_source_family_ids"])
    eligible_rows = [
        row for row in rows if row["source"]["source_family_id"] in holdout_families
    ]
    if not eligible_rows:
        raise ValueError("no examples found for the model's held-out source families")

    selected: list[dict[str, Any]] = []
    population_counts = Counter(_stratum(row) for row in eligible_rows)
    for label in LABELS:
        available = population_counts[label]
        if available == 0:
            raise ValueError(f"held-out source families contain no {label!r} examples")
        selected.extend(
            _balanced_sample(
                eligible_rows,
                stratum=label,
                count=min(available, args.max_per_stratum),
                seed=args.seed,
            )
        )

    selected.sort(key=lambda row: _stratum(row))
    random.Random(args.seed).shuffle(selected)
    if len({row["example_id"] for row in selected}) != len(selected):
        raise ValueError("stratified sample contains duplicate examples")
    args.output_dir.mkdir(parents=True, exist_ok=True)

    key_rows = []
    reviewer_rows = []
    for row in selected:
        review_id = _review_id(row["example_id"], args.seed)
        key_rows.append(
            {
                "review_id": review_id,
                "example_id": row["example_id"],
                "source_family_id": row["source"]["source_family_id"],
                "task_id": row["source"]["task_id"],
                "teacher_stratum": _stratum(row),
                "teacher_reason_code": row["labels"].get("reason_code"),
            }
        )
        reviewer_rows.append(
            {
                "review_id": review_id,
                "proposition": row["inputs"]["proposition"],
                "label": None,
                "unresolved_reason": None,
                "confidence": None,
                "notes": None,
            }
        )

    _write_jsonl(args.output_dir / "selection_key.jsonl", key_rows)
    for reviewer, reviewer_seed in (("a", args.seed + 101), ("b", args.seed + 202)):
        reviewer_copy = list(reviewer_rows)
        random.Random(reviewer_seed).shuffle(reviewer_copy)
        _write_jsonl(args.output_dir / f"annotator_{reviewer}.jsonl", reviewer_copy)
    _write_jsonl(
        args.output_dir / "adjudication.jsonl",
        [
            {
                "review_id": _review_id(row["example_id"], args.seed),
                "final_label": None,
                "unresolved_reason": None,
                "rationale": None,
                "adjudicator": None,
            }
            for row in selected
        ],
    )

    manifest = {
        "schema_version": "root-disposition-human-annotation-batch-v1",
        "status": "awaiting_independent_human_labels",
        "dataset_sha256": hashlib.sha256(dataset_bytes).hexdigest(),
        "source_example_count": len(rows),
        "eligible_example_count": len(eligible_rows),
        "sample_count": len(selected),
        "stratification_field": "existing_teacher_label_or_unresolved",
        "max_per_stratum": args.max_per_stratum,
        "seed": args.seed,
        "heldout_source_family_ids": holdout_families,
        "eligible_counts_by_teacher_stratum": dict(sorted(population_counts.items())),
        "sample_counts_by_teacher_stratum": dict(
            sorted(Counter(_stratum(row) for row in selected).items())
        ),
        "reviewers": ["a", "b"],
        "gold_status": "not_gold_until_both_reviews_and_adjudication_are_complete",
    }
    _write_json(args.output_dir / "batch-manifest.json", manifest)
    _write_json(
        args.output_dir / "holdout-families.json",
        {"source_family_ids": holdout_families},
    )
    print(
        json.dumps(
            {
                "output_dir": str(args.output_dir),
                "sample_count": len(selected),
                "counts_by_teacher_stratum": manifest[
                    "sample_counts_by_teacher_stratum"
                ],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
