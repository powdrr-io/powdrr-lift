"""Prepare blinded semantic reviews and score reviewed predictions."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .records import load_json, load_task_record

JUDGMENTS = {"exact", "partial", "no_match", "unreviewed"}


def prepare_review(
    record: dict[str, Any], predictions: dict[str, Any]
) -> dict[str, Any]:
    if record["task_id"] != predictions.get("task_id"):
        raise ValueError("task record and prediction task IDs do not match")
    cases = record["ground_truth"].get("cases", [])
    predicted = predictions.get("cases", [])
    pairs = [
        {
            "prediction_id": prediction["id"],
            "ground_truth_id": case["id"],
            "judgment": "unreviewed",
            "rationale": "",
        }
        for prediction in predicted
        for case in cases
    ]
    return {
        "schema_version": "deepswe-test-prediction-review-v1",
        "task_id": record["task_id"],
        "ground_truth_availability": record["ground_truth"]["availability"],
        "predictions": predicted,
        "ground_truth": cases,
        "ground_truth_dispositions": [
            {
                "ground_truth_id": case["id"],
                "behavior_status": "unreviewed",
                "behavior_group_id": case.get("behavior_group_id", case["id"]),
                "rationale": "",
            }
            for case in cases
        ],
        "prediction_ids": [item["id"] for item in predicted],
        "ground_truth_ids": [item["id"] for item in cases],
        "pairs": pairs,
    }


def score_reviews(
    entries: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]],
    *,
    cutoffs: tuple[int, ...] = (5, 10, 20),
) -> dict[str, Any]:
    task_reports = []
    all_exact_predictions = 0
    all_exact_truth = 0
    all_covered_predictions = 0
    all_covered_truth = 0
    all_predictions = 0
    all_truth = 0
    tasks_with_labels = 0
    cutoff_totals = {
        str(k): {
            "prediction_hits": 0,
            "prediction_count": 0,
            "ground_truth_hits": 0,
            "ground_truth_count": 0,
        }
        for k in cutoffs
    }
    for record, predictions, review in entries:
        task_report = _score_one(record, predictions, review, cutoffs)
        task_reports.append(task_report)
        if task_report["ground_truth_count"]:
            tasks_with_labels += 1
            all_exact_predictions += task_report["exact_prediction_count"]
            all_exact_truth += task_report["matched_exact"]
            all_covered_predictions += task_report["covered_prediction_count"]
            all_covered_truth += (
                task_report["matched_exact"] + task_report["matched_partial"]
            )
            all_predictions += task_report["prediction_count"]
            all_truth += task_report["ground_truth_count"]
        for k in cutoffs:
            metrics = task_report["at_k"][str(k)]
            totals = cutoff_totals[str(k)]
            if task_report["ground_truth_count"]:
                totals["prediction_hits"] += metrics["matched_predictions"]
                totals["prediction_count"] += metrics["prediction_count"]
                totals["ground_truth_hits"] += metrics["matched_ground_truth"]
                totals["ground_truth_count"] += task_report["ground_truth_count"]

    return {
        "schema_version": "deepswe-test-prediction-score-v1",
        "task_count": len(task_reports),
        "tasks_with_reviewable_ground_truth": tasks_with_labels,
        "aggregate": {
            "exact_precision": _ratio(all_exact_predictions, all_predictions),
            "exact_recall": _ratio(all_exact_truth, all_truth),
            "coverage_precision": _ratio(all_covered_predictions, all_predictions),
            "coverage_recall": _ratio(all_covered_truth, all_truth),
            "unsupported_predictions": all_predictions - all_covered_predictions,
            "prediction_count": all_predictions,
            "ground_truth_count": all_truth,
            "at_k": {
                k: {
                    "precision": _ratio(
                        values["prediction_hits"], values["prediction_count"]
                    ),
                    "recall": _ratio(
                        values["ground_truth_hits"], values["ground_truth_count"]
                    ),
                }
                for k, values in cutoff_totals.items()
            },
        },
        "tasks": task_reports,
    }


def score_files(
    record_paths: list[Path],
    prediction_paths: list[Path],
    review_paths: list[Path],
    *,
    cutoffs: tuple[int, ...] = (5, 10, 20),
) -> dict[str, Any]:
    records = {
        record["task_id"]: record for record in map(load_task_record, record_paths)
    }
    predictions = {item["task_id"]: item for item in map(load_json, prediction_paths)}
    reviews = {item["task_id"]: item for item in map(load_json, review_paths)}
    if not predictions:
        raise ValueError("no prediction files found")
    if set(predictions) != set(reviews):
        raise ValueError(
            "prediction and review directories must contain the same task IDs"
        )
    if not set(predictions).issubset(records):
        raise ValueError("every prediction must have a corresponding task record")
    task_ids = sorted(predictions)
    entries = [(records[key], predictions[key], reviews[key]) for key in task_ids]
    return score_reviews(entries, cutoffs=cutoffs)


def write_json(value: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _score_one(
    record: dict[str, Any],
    predictions: dict[str, Any],
    review: dict[str, Any],
    cutoffs: tuple[int, ...],
) -> dict[str, Any]:
    if record["task_id"] != predictions.get("task_id") or record[
        "task_id"
    ] != review.get("task_id"):
        raise ValueError("record, prediction, and review task IDs must match")
    predicted = predictions.get("cases", [])
    truth = record["ground_truth"].get("cases", [])
    prediction_ids = {item["id"] for item in predicted}
    all_truth_ids = {item["id"] for item in truth}
    dispositions = {
        item.get("ground_truth_id"): item
        for item in review.get("ground_truth_dispositions", [])
    }
    raw_dispositions = review.get("ground_truth_dispositions", [])
    if len(dispositions) != len(raw_dispositions) or set(dispositions) != all_truth_ids:
        raise ValueError(
            f"review behavior statuses do not match task {record['task_id']}"
        )
    for ground_truth_id, disposition in dispositions.items():
        if disposition.get("behavior_status") not in {"known", "unknown"}:
            raise ValueError(
                f"task {record['task_id']} has unreviewed ground truth behavior"
            )
        if not str(disposition.get("rationale", "")).strip():
            raise ValueError(
                "ground truth behavior status for "
                f"{ground_truth_id} requires a rationale"
            )
    truth_ids = {
        ground_truth_id
        for ground_truth_id, disposition in dispositions.items()
        if disposition["behavior_status"] == "known"
    }
    unknown_truth_count = len(all_truth_ids) - len(truth_ids)
    truth = [item for item in truth if item["id"] in truth_ids]
    for ground_truth_id in truth_ids:
        if not str(dispositions[ground_truth_id].get("behavior_group_id", "")).strip():
            raise ValueError(
                f"known ground truth {ground_truth_id} requires a behavior group"
            )
    if set(review.get("ground_truth_ids", [])) != all_truth_ids:
        raise ValueError(
            f"review ground truth IDs do not match task {record['task_id']}"
        )
    if set(review.get("prediction_ids", [])) != prediction_ids:
        raise ValueError(f"review prediction IDs do not match task {record['task_id']}")

    judgments: dict[tuple[str, str], str] = {}
    for pair in review.get("pairs", []):
        key = (pair.get("prediction_id"), pair.get("ground_truth_id"))
        judgment = pair.get("judgment")
        if (
            key in judgments
            or key[0] not in prediction_ids
            or key[1] not in all_truth_ids
        ):
            raise ValueError(
                f"invalid or duplicate review pair in task {record['task_id']}"
            )
        if judgment not in JUDGMENTS:
            raise ValueError(f"invalid review judgment: {judgment}")
        judgments[key] = judgment
    expected_pairs = len(prediction_ids) * len(all_truth_ids)
    if len(judgments) != expected_pairs:
        raise ValueError(f"task {record['task_id']} has an incomplete review matrix")
    if any(
        judgment == "unreviewed"
        for (prediction_id, truth_id), judgment in judgments.items()
        if truth_id in truth_ids
    ):
        raise ValueError(
            f"task {record['task_id']} still has unreviewed scenario pairs"
        )
    if any(
        judgment != "unreviewed" and not str(review_pair.get("rationale", "")).strip()
        for review_pair in review.get("pairs", [])
        for judgment in [review_pair.get("judgment")]
        if review_pair.get("ground_truth_id") in truth_ids
    ):
        raise ValueError(
            f"task {record['task_id']} has a reviewed pair without rationale"
        )
    for pair in judgments:
        if pair[1] not in truth_ids:
            judgments[pair] = "no_match"

    ranked_ids = [item["id"] for item in predicted]
    truth_groups = {
        item["id"]: dispositions[item["id"]].get(
            "behavior_group_id", item.get("behavior_group_id", item["id"])
        )
        for item in truth
    }
    prediction_rank = {item["id"]: index for index, item in enumerate(predicted)}
    matches = _assign_matches(judgments, truth_groups, prediction_rank)
    exact_pairs = [pair for pair in matches if pair[2] == "exact"]
    partial_pairs = [pair for pair in matches if pair[2] == "partial"]
    covered_prediction_ids = {pair[0] for pair in matches}
    truth_hit_ids = {pair[1] for pair in matches}
    exact_prediction_ids = {pair[0] for pair in exact_pairs}
    at_k = {}
    for k in cutoffs:
        selected = set(ranked_ids[:k])
        selected_judgments = {
            pair: judgment
            for pair, judgment in judgments.items()
            if pair[0] in selected
        }
        matched = _assign_matches(selected_judgments, truth_groups, prediction_rank)
        at_k[str(k)] = {
            "prediction_count": min(k, len(ranked_ids)),
            "matched_predictions": len({pair[0] for pair in matched}),
            "matched_ground_truth": len({pair[1] for pair in matched}),
        }
    return {
        "task_id": record["task_id"],
        "ground_truth_availability": record["ground_truth"]["availability"],
        "prediction_count": len(predicted),
        "ground_truth_count": len(truth),
        "ground_truth_behavior_unknown_count": unknown_truth_count,
        "matched_exact": len(exact_pairs),
        "matched_partial": len(partial_pairs),
        "exact_prediction_count": len(exact_prediction_ids),
        "covered_prediction_count": len(covered_prediction_ids),
        "unsupported_predictions": len(predicted) - len(covered_prediction_ids),
        "uncovered_ground_truth": len(truth) - len(truth_hit_ids),
        "exact_precision": _ratio(len(exact_prediction_ids), len(predicted)),
        "exact_recall": _ratio(len(exact_pairs), len(truth)),
        "coverage_precision": _ratio(len(covered_prediction_ids), len(predicted)),
        "coverage_recall": _ratio(len(matches), len(truth)),
        "at_k": at_k,
        "matches": [
            {
                "prediction_id": prediction_id,
                "ground_truth_id": truth_id,
                "judgment": judgment,
            }
            for prediction_id, truth_id, judgment in matches
        ],
    }


def _assign_matches(
    judgments: dict[tuple[str, str], str],
    truth_groups: dict[str, str],
    prediction_rank: dict[str, int],
) -> list[tuple[str, str, str]]:
    rank = {"exact": 0, "partial": 1}
    candidates = [
        (prediction_id, truth_id, judgment)
        for (prediction_id, truth_id), judgment in judgments.items()
        if judgment in rank
    ]
    candidates.sort(key=lambda item: (rank[item[2]], prediction_rank[item[0]], item[1]))
    used_truth: set[str] = set()
    prediction_groups: dict[str, str] = {}
    matches = []
    for prediction_id, truth_id, judgment in candidates:
        group = truth_groups[truth_id]
        if (
            truth_id in used_truth
            or prediction_groups.get(prediction_id, group) != group
        ):
            continue
        prediction_groups[prediction_id] = group
        used_truth.add(truth_id)
        matches.append((prediction_id, truth_id, judgment))
    return matches


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None
