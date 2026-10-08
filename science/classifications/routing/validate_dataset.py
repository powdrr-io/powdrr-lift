#!/usr/bin/env python3
"""Validate routing sources, candidate rows, split isolation, and reviewed labels."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from science.classifications.routing.input_contract import (  # noqa: E402
    inference_input_sha256,
)

ROUTES = {"context", "include", "include_prohibition", "exclude", "unclear"}


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def validate(
    data_dir: Path, raw_dir: Path, splits_path: Path, gold_path: Path | None
) -> dict[str, Any]:
    docs = _read_jsonl(raw_dir / "source_documents.jsonl")
    candidates = _read_jsonl(data_dir / "candidates.jsonl")
    family_rows = _read_jsonl(splits_path)
    documents = {row["document_id"]: row for row in docs}
    _require(len(documents) == len(docs), "duplicate source document IDs")
    split_by_family = {row["family_id"]: row["split"] for row in family_rows}
    _require(len(split_by_family) == len(family_rows), "duplicate family split records")
    _require(
        set(split_by_family.values()) <= {"development", "fresh_holdout"},
        "invalid family split",
    )
    example_ids: set[str] = set()
    groups: dict[str, str] = {}
    label_counts: Counter[str] = Counter()
    suggestion_counts: Counter[str] = Counter()

    for document in docs:
        source_text = document["text"]
        _require(
            _sha256(source_text.encode("utf-8")) == document["source_sha256"],
            f"source hash mismatch for {document['document_id']}",
        )
        for clause in document["clauses"]:
            span = clause.get("source_span", {})
            start, end = span.get("start"), span.get("end")
            _require(
                isinstance(start, int)
                and isinstance(end, int)
                and 0 <= start < end <= len(source_text),
                f"bad source clause span in {document['document_id']}",
            )

    for row in candidates:
        _require(
            row.get("schema_version") == "routing-example-v1",
            f"unsupported schema at {row.get('example_id')}",
        )
        example_id = row.get("example_id")
        if not isinstance(example_id, str) or example_id in example_ids:
            raise ValueError(f"duplicate or missing example ID: {example_id}")
        example_ids.add(example_id)
        _require(
            row.get("decision_kind") == "routing", f"wrong decision kind: {example_id}"
        )
        _require(
            row.get("input_revision") == "routing-input-v1",
            f"wrong input revision: {example_id}",
        )
        inputs = row.get("inputs", {})
        _ = inference_input_sha256(inputs)
        source = row.get("source", {})
        candidate_document = documents.get(source.get("document_id"))
        if candidate_document is None:
            raise ValueError(f"missing source document for {example_id}")
        document = candidate_document
        _require(
            source.get("family_id") == document["family_id"],
            f"family mismatch for {example_id}",
        )
        _require(
            source.get("source_sha256") == document["source_sha256"],
            f"source hash pointer mismatch for {example_id}",
        )
        span = source.get("target_span")
        _require(isinstance(span, dict), f"target span missing for {example_id}")
        start, end = span.get("start"), span.get("end")
        _require(
            isinstance(start, int)
            and isinstance(end, int)
            and 0 <= start < end <= len(document["text"]),
            f"target span invalid for {example_id}",
        )
        _require(
            document["text"][start:end] == inputs.get("proposition"),
            f"target text does not match its source span for {example_id}",
        )
        context = inputs["local_context"]
        _require(
            context.get("source_sentence") == inputs["proposition"],
            f"context target mismatch for {example_id}",
        )
        _require(
            row.get("split") == split_by_family.get(source.get("family_id")),
            f"family split mismatch for {example_id}",
        )
        group = source.get("derivation_group_id")
        _require(
            isinstance(group, str) and bool(group),
            f"missing derivation group for {example_id}",
        )
        prior_split = groups.setdefault(group, row["split"])
        _require(
            prior_split == row["split"], f"derivation group crosses splits: {group}"
        )
        annotation = row.get("annotation", {})
        suggested = annotation.get("suggested_label")
        _require(
            suggested is None or suggested in ROUTES,
            f"invalid proposed label for {example_id}",
        )
        _require(
            annotation.get("status") in {"proposed", "unlabeled"},
            f"candidate row contains a non-candidate status: {example_id}",
        )
        _require(
            annotation.get("status") == ("proposed" if suggested else "unlabeled"),
            f"proposal status mismatch for {example_id}",
        )
        if suggested:
            _require(
                row["split"] == "development",
                f"historical silver suggestion leaked into holdout: {example_id}",
            )
            suggestion_counts[suggested] += 1
        label_counts[row["split"]] += 1

    if gold_path is not None and gold_path.is_file():
        for row in _read_jsonl(gold_path):
            _require(
                row.get("final_label") in ROUTES,
                f"invalid final label: {row.get('example_id')}",
            )
            _require(
                row.get("status") in {"reviewer_agreement", "adjudicated"},
                f"unreviewed label must not enter gold: {row.get('example_id')}",
            )
            _require(
                row.get("reviewer_kinds") == ["human", "human"],
                f"gold labels require two human reviews: {row.get('example_id')}",
            )
            if row.get("status") == "adjudicated":
                _require(
                    isinstance(row.get("adjudicator_id"), str)
                    and bool(row["adjudicator_id"]),
                    "adjudicated label lacks human adjudicator: "
                    f"{row.get('example_id')}",
                )

    return {
        "source_documents": len(docs),
        "candidate_examples": len(candidates),
        "candidate_counts_by_split": dict(sorted(label_counts.items())),
        "historical_silver_suggestions": dict(sorted(suggestion_counts.items())),
        "families": len(split_by_family),
        "gold_rows": len(_read_jsonl(gold_path))
        if gold_path is not None and gold_path.is_file()
        else 0,
        "status": "valid_candidate_dataset_gold_may_still_be_empty",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-dir", type=Path, default=ROOT / "science/classifications/routing/data"
    )
    parser.add_argument(
        "--raw-dir", type=Path, default=ROOT / "science/classifications/routing/raw"
    )
    parser.add_argument(
        "--splits",
        type=Path,
        default=ROOT / "science/classifications/routing/splits/family-splits.jsonl",
    )
    parser.add_argument("--gold", type=Path)
    args = parser.parse_args()
    print(
        json.dumps(
            validate(args.data_dir, args.raw_dir, args.splits, args.gold),
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
