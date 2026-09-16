"""Shared data and metric helpers for the Python execution-error classifier."""

from __future__ import annotations

import json
import random
from collections import defaultdict
from pathlib import Path
from typing import Any


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    """Load non-empty JSON Lines records and reject malformed records early."""
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), 1
    ):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"{path}:{line_number} is not a JSON object")
        if not isinstance(value.get("id"), str) or not isinstance(
            value.get("text"), str
        ):
            raise ValueError(f"{path}:{line_number} needs string id and text fields")
        if not isinstance(value.get("label"), str):
            raise ValueError(f"{path}:{line_number} needs a string label field")
        records.append(value)
    if not records:
        raise ValueError(f"{path} contains no records")
    return records


def split_records(
    records: list[dict[str, Any]],
    *,
    seed: int = 42,
    validation_size: float = 0.1,
    test_size: float = 0.2,
) -> dict[str, list[dict[str, Any]]]:
    """Create deterministic, label-stratified train/validation/test partitions."""
    if validation_size < 0 or test_size < 0 or validation_size + test_size >= 1:
        raise ValueError("validation_size and test_size must sum to less than one")
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        groups[str(record["label"])].append(record)
    rng = random.Random(seed)
    result = {"train": [], "validation": [], "test": []}
    for label in sorted(groups):
        group = list(groups[label])
        rng.shuffle(group)
        count = len(group)
        test_count = max(1, round(count * test_size)) if count >= 3 else 0
        validation_count = max(1, round(count * validation_size)) if count >= 5 else 0
        if test_count + validation_count >= count:
            validation_count = 0
            test_count = 1 if count >= 3 else 0
        result["test"].extend(group[:test_count])
        result["validation"].extend(group[test_count : test_count + validation_count])
        result["train"].extend(group[test_count + validation_count :])
    for split in result.values():
        split.sort(key=lambda record: str(record["id"]))
    if not result["train"]:
        raise ValueError("split produced an empty training set")
    return result


def classification_metrics(
    labels: list[int], predictions: list[int], label_names: list[str]
) -> dict[str, Any]:
    """Compute dependency-free accuracy, macro F1, and per-label metrics."""
    if len(labels) != len(predictions):
        raise ValueError("labels and predictions must have equal length")
    per_label: dict[str, dict[str, float | int]] = {}
    f1_values: list[float] = []
    for index, name in enumerate(label_names):
        true_positive = sum(
            y == index and p == index for y, p in zip(labels, predictions, strict=True)
        )
        false_positive = sum(
            y != index and p == index for y, p in zip(labels, predictions, strict=True)
        )
        false_negative = sum(
            y == index and p != index for y, p in zip(labels, predictions, strict=True)
        )
        precision = (
            true_positive / (true_positive + false_positive)
            if true_positive + false_positive
            else 0.0
        )
        recall = (
            true_positive / (true_positive + false_negative)
            if true_positive + false_negative
            else 0.0
        )
        f1 = (
            2 * precision * recall / (precision + recall) if precision + recall else 0.0
        )
        f1_values.append(f1)
        per_label[name] = {
            "precision": precision,
            "recall": recall,
            "f1": f1,
            "support": sum(y == index for y in labels),
        }
    return {
        "accuracy": sum(y == p for y, p in zip(labels, predictions, strict=True))
        / len(labels)
        if labels
        else 0.0,
        "macro_f1": sum(f1_values) / len(f1_values) if f1_values else 0.0,
        "per_label": per_label,
    }
