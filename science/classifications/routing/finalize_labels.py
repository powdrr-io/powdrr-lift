#!/usr/bin/env python3
"""Validate blinded reviews and emit agreement labels plus adjudication work."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROUTES = {"context", "include", "include_prohibition", "exclude", "unclear"}
REASONS = {"source_ambiguous", "source_underspecified", "unsupported_concept"}
CONFIDENCE = {"high", "medium", "low"}


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(
            json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows
        ),
        encoding="utf-8",
    )


def _index(rows: list[dict[str, Any]], name: str) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        review_id = row.get("review_id")
        if not isinstance(review_id, str) or review_id in result:
            raise ValueError(f"{name} has a missing or duplicate review_id")
        result[review_id] = row
    return result


def _validate_review(row: Mapping[str, Any], reviewer: str) -> None:
    if row.get("label") not in ROUTES:
        raise ValueError(f"{reviewer}: invalid label for {row.get('review_id')}")
    if row.get("confidence") not in CONFIDENCE:
        raise ValueError(f"{reviewer}: invalid confidence for {row.get('review_id')}")
    if not isinstance(row.get("rationale"), str) or not row["rationale"].strip():
        raise ValueError(
            f"{reviewer}: rationale is required for {row.get('review_id')}"
        )
    quotes = row.get("evidence_quotes")
    if not isinstance(quotes, list) or not all(
        isinstance(quote, str) and quote for quote in quotes
    ):
        raise ValueError(
            f"{reviewer}: evidence_quotes must be a list of non-empty strings"
        )
    evidence_text = json.dumps(
        {
            "proposition": row.get("proposition"),
            "local_context": row.get("local_context"),
            "scope_relations": row.get("scope_relations"),
        },
        ensure_ascii=False,
    )
    if any(quote not in evidence_text for quote in quotes):
        raise ValueError(
            f"{reviewer}: evidence quote is absent from supplied input for "
            f"{row.get('review_id')}"
        )
    reason = row.get("ambiguity_reason")
    if row["label"] == "unclear" and reason not in REASONS:
        raise ValueError(f"{reviewer}: unclear label needs an ambiguity_reason")
    if row["label"] != "unclear" and reason is not None:
        raise ValueError(f"{reviewer}: non-unclear label must clear ambiguity_reason")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--packet-dir", type=Path, required=True)
    parser.add_argument(
        "--candidate-file",
        type=Path,
        default=Path(__file__).resolve().parent / "data/candidates.jsonl",
    )
    parser.add_argument(
        "--reviewer-a-kind", choices=("human", "assistant_model"), required=True
    )
    parser.add_argument("--reviewer-a-id", required=True)
    parser.add_argument(
        "--reviewer-b-kind", choices=("human", "assistant_model"), required=True
    )
    parser.add_argument("--reviewer-b-id", required=True)
    parser.add_argument("--adjudications", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    if args.output_dir.exists() and any(args.output_dir.iterdir()):
        raise FileExistsError(
            f"refusing to overwrite finalized review output: {args.output_dir}"
        )
    if (
        args.reviewer_a_kind == args.reviewer_b_kind == "human"
        and args.reviewer_a_id == args.reviewer_b_id
    ):
        parser.error("independent human reviewers must have distinct reviewer IDs")

    key_rows = _read_jsonl(args.packet_dir / "selection_key.jsonl")
    key_by_id = _index(key_rows, "selection key")
    reviewer_a = _index(_read_jsonl(args.packet_dir / "reviewer_a.jsonl"), "reviewer A")
    reviewer_b = _index(_read_jsonl(args.packet_dir / "reviewer_b.jsonl"), "reviewer B")
    if set(reviewer_a) != set(key_by_id) or set(reviewer_b) != set(key_by_id):
        raise ValueError("both completed reviewer sheets must match the selection key")
    candidates = {row["example_id"]: row for row in _read_jsonl(args.candidate_file)}
    adjudications = (
        _index(_read_jsonl(args.adjudications), "adjudication file")
        if args.adjudications
        else {}
    )

    events = []
    agreements = []
    worklist = []
    for review_id in sorted(key_by_id):
        key = key_by_id[review_id]
        candidate = candidates.get(key["example_id"])
        left, right = reviewer_a[review_id], reviewer_b[review_id]
        if candidate is None:
            raise ValueError(f"missing candidate for {review_id}")
        for reviewer_name, review in (("a", left), ("b", right)):
            _validate_review(review, reviewer_name)
            for field in (
                "proposition",
                "local_context",
                "scope_relations",
                "input_revision",
                "rubric_revision",
            ):
                expected = (
                    candidate["inputs"].get(field)
                    if field in {"proposition", "local_context", "scope_relations"}
                    else candidate[field]
                )
                if review.get(field) != expected:
                    raise ValueError(
                        f"{reviewer_name} packet input mismatch for "
                        f"{review_id}: {field}"
                    )
            events.append(
                {
                    "schema_version": "routing-annotation-event-v1",
                    "event_id": f"{review_id}:{reviewer_name}",
                    "example_id": key["example_id"],
                    "review_id": review_id,
                    "event": "independent_review",
                    "reviewer_kind": args.reviewer_a_kind
                    if reviewer_name == "a"
                    else args.reviewer_b_kind,
                    "reviewer_id": args.reviewer_a_id
                    if reviewer_name == "a"
                    else args.reviewer_b_id,
                    "label": review["label"],
                    "confidence": review["confidence"],
                    "rationale": review["rationale"],
                    "evidence_quotes": review["evidence_quotes"],
                    "ambiguity_reason": review["ambiguity_reason"],
                    "recorded_at": datetime.now(UTC).isoformat(),
                }
            )
        independent_humans = (
            args.reviewer_a_kind == args.reviewer_b_kind == "human"
            and args.reviewer_a_id != args.reviewer_b_id
        )
        if (
            independent_humans
            and left["label"] == right["label"]
            and left["confidence"] != "low"
            and right["confidence"] != "low"
        ):
            agreements.append(
                {
                    "schema_version": "routing-gold-label-v1",
                    "example_id": key["example_id"],
                    "review_id": review_id,
                    "final_label": left["label"],
                    "status": "reviewer_agreement",
                    "reviewer_kinds": ["human", "human"],
                    "reviewer_ids": [args.reviewer_a_id, args.reviewer_b_id],
                    "ambiguity_reason": left["ambiguity_reason"],
                    "annotation_event_ids": [f"{review_id}:a", f"{review_id}:b"],
                }
            )
        else:
            adjudication = adjudications.pop(review_id, None)
            if adjudication is not None:
                if not independent_humans:
                    raise ValueError(
                        "gold adjudication requires two independent human reviews"
                    )
                final_label = adjudication.get("final_label")
                adjudicator_id = adjudication.get("adjudicator_id")
                rationale = adjudication.get("rationale")
                ambiguity_reason = adjudication.get("ambiguity_reason")
                if (
                    final_label not in ROUTES
                    or not isinstance(adjudicator_id, str)
                    or not adjudicator_id.strip()
                    or adjudicator_id in {args.reviewer_a_id, args.reviewer_b_id}
                    or not isinstance(rationale, str)
                    or not rationale.strip()
                ):
                    raise ValueError(f"incomplete adjudication for {review_id}")
                if final_label == "unclear" and ambiguity_reason not in REASONS:
                    raise ValueError(
                        f"unclear adjudication needs ambiguity_reason for {review_id}"
                    )
                if final_label != "unclear" and ambiguity_reason is not None:
                    raise ValueError(
                        "non-unclear adjudication must clear ambiguity_reason "
                        f"for {review_id}"
                    )
                event_id = f"{review_id}:adjudication"
                events.append(
                    {
                        "schema_version": "routing-annotation-event-v1",
                        "event_id": event_id,
                        "example_id": key["example_id"],
                        "review_id": review_id,
                        "event": "human_adjudication",
                        "reviewer_kind": "human",
                        "reviewer_id": adjudicator_id,
                        "label": final_label,
                        "confidence": "high",
                        "rationale": rationale,
                        "evidence_quotes": [],
                        "ambiguity_reason": ambiguity_reason,
                        "recorded_at": datetime.now(UTC).isoformat(),
                    }
                )
                agreements.append(
                    {
                        "schema_version": "routing-gold-label-v1",
                        "example_id": key["example_id"],
                        "review_id": review_id,
                        "final_label": final_label,
                        "status": "adjudicated",
                        "reviewer_kinds": ["human", "human"],
                        "reviewer_ids": [args.reviewer_a_id, args.reviewer_b_id],
                        "adjudicator_id": adjudicator_id,
                        "ambiguity_reason": ambiguity_reason,
                        "annotation_event_ids": [
                            f"{review_id}:a",
                            f"{review_id}:b",
                            event_id,
                        ],
                    }
                )
                continue
            worklist.append(
                {
                    "example_id": key["example_id"],
                    "review_id": review_id,
                    "proposition": left["proposition"],
                    "reviewer_a": {
                        key: left[key]
                        for key in (
                            "label",
                            "confidence",
                            "rationale",
                            "ambiguity_reason",
                        )
                    },
                    "reviewer_b": {
                        key: right[key]
                        for key in (
                            "label",
                            "confidence",
                            "rationale",
                            "ambiguity_reason",
                        )
                    },
                    "resolution": None,
                    "adjudicator": None,
                }
            )
    if adjudications:
        raise ValueError(
            "adjudication file has unknown or already-agreed review IDs: "
            + ", ".join(sorted(adjudications))
        )

    args.output_dir.mkdir(parents=True, exist_ok=True)
    _write_jsonl(args.output_dir / "annotation-ledger.jsonl", events)
    _write_jsonl(args.output_dir / "gold-labels.jsonl", agreements)
    _write_jsonl(args.output_dir / "adjudication-worklist.jsonl", worklist)
    manifest = {
        "schema_version": "routing-label-finalization-v1",
        "review_packet": str(args.packet_dir),
        "candidate_sha256": hashlib.sha256(
            args.candidate_file.read_bytes()
        ).hexdigest(),
        "review_count": len(key_by_id),
        "independent_human_agreements": len(agreements),
        "requires_adjudication": len(worklist),
        "gold_status": "partial_pending_adjudication" if worklist else "human_reviewed",
        "reviewers": [
            {"kind": args.reviewer_a_kind, "id": args.reviewer_a_id},
            {"kind": args.reviewer_b_kind, "id": args.reviewer_b_id},
        ],
    }
    (args.output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "reviewed": len(key_by_id),
                "human_agreements": len(agreements),
                "adjudication_items": len(worklist),
                "gold_status": manifest["gold_status"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
