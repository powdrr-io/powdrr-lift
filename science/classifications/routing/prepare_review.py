#!/usr/bin/env python3
"""Prepare two blinded, family-balanced routing annotation sheets."""

from __future__ import annotations

import argparse
import hashlib
import json
import random
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CANDIDATES = ROOT / "science/classifications/routing/data/candidates.jsonl"
ROUTES = ("context", "include", "include_prohibition", "exclude", "unclear")
DEFAULT_QUOTAS = {
    "context": 30,
    "include": 30,
    "include_prohibition": 30,
    "exclude": 30,
    "unclear": 30,
    "unlabeled": 90,
}


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text(
        "".join(
            json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows
        ),
        encoding="utf-8",
    )


def _review_id(seed: int, example_id: str) -> str:
    return "review:" + hashlib.sha256(f"{seed}:{example_id}".encode()).hexdigest()[:20]


def _stratum(row: dict[str, Any]) -> str:
    suggested = row.get("annotation", {}).get("suggested_label")
    if suggested in ROUTES:
        return suggested
    tags = set(row.get("slice_tags", []))
    context = row.get("inputs", {}).get("local_context", {})
    if "negation_or_absence" in tags:
        return "include_prohibition"
    if "present_state_cue" in tags and not context.get("section_heading"):
        return "unclear"
    return "unlabeled"


def _family_sample(
    rows: list[dict[str, Any]], quota: int, seed: int
) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[row["source"]["family_id"]].append(row)
    families = sorted(grouped)
    random.Random(seed).shuffle(families)
    for family in families:
        random.Random(f"{seed}:{family}").shuffle(grouped[family])
    result = []
    while len(result) < quota:
        progressed = False
        for family in families:
            if grouped[family]:
                result.append(grouped[family].pop())
                progressed = True
                if len(result) == quota:
                    break
        if not progressed:
            break
    return result


def _select(
    candidates: list[dict[str, Any]], *, sample_count: int, seed: int
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    eligible = [row for row in candidates if row.get("split") == "development"]
    by_stratum: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in eligible:
        by_stratum[_stratum(row)].append(row)
    selected = []
    counts = Counter()
    for stratum, quota in DEFAULT_QUOTAS.items():
        batch = _family_sample(
            by_stratum[stratum],
            min(quota, sample_count - len(selected)),
            seed + len(selected),
        )
        selected.extend(batch)
        counts[stratum] = len(batch)
    if len(selected) < sample_count:
        already = {row["example_id"] for row in selected}
        remainder = [row for row in eligible if row["example_id"] not in already]
        extra = _family_sample(remainder, sample_count - len(selected), seed + 100003)
        selected.extend(extra)
        counts["additional_unstratified"] = len(extra)
    if len(selected) != min(sample_count, len(eligible)):
        raise ValueError("could not construct a unique development review sample")
    random.Random(seed).shuffle(selected)
    return selected, dict(sorted(counts.items()))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidates", type=Path, default=DEFAULT_CANDIDATES)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--sample-count", type=int, default=240)
    parser.add_argument("--seed", type=int, default=20261008)
    args = parser.parse_args()
    if args.sample_count < 1:
        parser.error("--sample-count must be positive")
    if args.output_dir.exists() and any(args.output_dir.iterdir()):
        raise FileExistsError(f"refusing to overwrite review packet: {args.output_dir}")

    candidates = _read_jsonl(args.candidates)
    selected, counts = _select(
        candidates, sample_count=args.sample_count, seed=args.seed
    )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    key_rows = []
    reviewer_rows = []
    for row in selected:
        review_id = _review_id(args.seed, row["example_id"])
        key_rows.append(
            {
                "review_id": review_id,
                "example_id": row["example_id"],
                "family_id": row["source"]["family_id"],
                "task_id": row["source"]["task_id"],
                "development_only": True,
                "hidden_suggested_label": row["annotation"]["suggested_label"],
            }
        )
        reviewer_rows.append(
            {
                "review_id": review_id,
                "input_revision": row["input_revision"],
                "rubric_revision": row["rubric_revision"],
                "proposition": row["inputs"]["proposition"],
                "local_context": row["inputs"]["local_context"],
                "scope_relations": row["inputs"]["scope_relations"],
                "label": None,
                "confidence": None,
                "rationale": None,
                "evidence_quotes": [],
                "ambiguity_reason": None,
            }
        )
    _write_jsonl(args.output_dir / "selection_key.jsonl", key_rows)
    for reviewer, salt in (("a", 101), ("b", 202)):
        copy = list(reviewer_rows)
        random.Random(args.seed + salt).shuffle(copy)
        _write_jsonl(args.output_dir / f"reviewer_{reviewer}.jsonl", copy)
    manifest = {
        "schema_version": "routing-review-packet-v1",
        "status": "awaiting_independent_human_annotations",
        "seed": args.seed,
        "candidate_sha256": hashlib.sha256(args.candidates.read_bytes()).hexdigest(),
        "sample_count": len(selected),
        "sample_counts_by_sampling_stratum": counts,
        "actual_hidden_suggestion_counts": dict(
            sorted(
                Counter(
                    row["annotation"]["suggested_label"] or "none" for row in selected
                ).items()
            )
        ),
        "families_in_packet": len({row["source"]["family_id"] for row in selected}),
        "split": "development_only",
        "reviewers": ["a", "b"],
        "gold_status": "not_gold_until_two_independent_human_reviews_and_adjudication",
        "blinding": (
            "reviewer sheets exclude source IDs, family IDs, split, and prior "
            "label suggestions"
        ),
        "do_not_share": (
            "selection_key.jsonl is coordinator-only and must not be shared "
            "with reviewers"
        ),
    }
    (args.output_dir / "review-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "output_dir": str(args.output_dir),
                "sample_count": len(selected),
                "families": manifest["families_in_packet"],
                "sampling_strata": counts,
                "actual_hidden_suggestions": manifest[
                    "actual_hidden_suggestion_counts"
                ],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
