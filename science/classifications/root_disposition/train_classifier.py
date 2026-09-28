#!/usr/bin/env python3
"""Fine-tune a Hugging Face MiniLM classifier on root-disposition examples."""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import shutil
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import numpy as np
import torch
from torch.utils.data import Dataset
from transformers import (
    AutoModelForSequenceClassification,
    AutoTokenizer,
    Trainer,
    TrainingArguments,
)

DEFAULT_MODEL = "microsoft/MiniLM-L12-H384-uncased"
DEFAULT_MODEL_REVISION = "44acabbec0ef496f6dbc93adadea57f376b7c0ec"


class ClauseDataset(Dataset[dict[str, torch.Tensor]]):
    def __init__(
        self,
        rows: list[dict[str, Any]],
        tokenizer: Any,
        label_to_id: dict[str, int],
        max_length: int,
    ) -> None:
        self.rows = rows
        self.encodings = tokenizer(
            [row["inputs"]["proposition"] for row in rows],
            truncation=True,
            padding=True,
            max_length=max_length,
            return_tensors="pt",
        )
        self.labels = torch.tensor(
            [label_to_id[row["labels"]["class"]] for row in rows],
            dtype=torch.long,
        )

    def __len__(self) -> int:
        return len(self.rows)

    def __getitem__(self, index: int) -> dict[str, torch.Tensor]:
        row = {key: value[index] for key, value in self.encodings.items()}
        row["labels"] = self.labels[index]
        return row


def _load_examples(path: Path) -> list[dict[str, Any]]:
    rows = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("schema_version") != "semantic-classifier-example-v1":
            raise ValueError(f"unsupported example schema on line {line_number}")
        if row.get("adjudication", {}).get("status") != "silver_unadjudicated":
            raise ValueError("pilot training expects explicitly marked teacher labels")
        if row.get("labels", {}).get("answerable") is True:
            rows.append(row)
    if len(rows) < 10:
        raise ValueError(f"need at least 10 resolved examples; found {len(rows)}")
    return rows


def _split_by_source_family(
    rows: list[dict[str, Any]], seed: int
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    label_families: dict[str, set[str]] = defaultdict(set)
    family_tasks: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        family_id = row["source"].get(
            "source_family_id", f"family:deepswe-task:{row['source']['task_id']}"
        )
        label = row["labels"]["class"]
        grouped[family_id].append(row)
        label_families[label].add(family_id)
        family_tasks[family_id].add(row["source"]["task_id"])
    family_ids = sorted(grouped)
    if len(family_ids) < 5:
        raise ValueError(
            "need at least five distinct repository families for leakage-safe "
            f"splits; got {len(family_ids)}"
        )

    # Labels seen in only one repository family stay in training rather than
    # disappearing from the model vocabulary or leaking into evaluation.
    rare_label_families = {
        next(iter(source_families))
        for source_families in label_families.values()
        if len(source_families) == 1
    }
    movable = [family_id for family_id in family_ids if family_id not in rare_label_families]
    rng = random.Random(seed)
    rng.shuffle(movable)
    eval_count = max(2, round(len(movable) * 0.2))
    eval_count = min(eval_count, max(0, len(movable) - 2))
    test_ids = set(movable[: eval_count // 2])
    validation_ids = set(movable[eval_count // 2 : eval_count])
    train_ids = set(family_ids) - test_ids - validation_ids

    train_labels = {
        row["labels"]["class"] for family_id in train_ids for row in grouped[family_id]
    }
    test_labels = {
        row["labels"]["class"] for family_id in test_ids for row in grouped[family_id]
    }
    validation_labels = {
        row["labels"]["class"]
        for family_id in validation_ids
        for row in grouped[family_id]
    }
    metadata = {
        "seed": seed,
        "split_unit": "DeepSWE repository family",
        "train_source_family_ids": sorted(train_ids),
        "validation_source_family_ids": sorted(validation_ids),
        "test_source_family_ids": sorted(test_ids),
        "train_task_ids": sorted(
            task for family_id in train_ids for task in family_tasks[family_id]
        ),
        "validation_task_ids": sorted(
            task for family_id in validation_ids for task in family_tasks[family_id]
        ),
        "test_task_ids": sorted(
            task for family_id in test_ids for task in family_tasks[family_id]
        ),
        "single_family_labels_kept_in_train": sorted(
            label
            for label, source_families in label_families.items()
            if len(source_families) == 1
        ),
        "labels_missing_from_train": sorted(set(label_families) - train_labels),
        "validation_labels_unseen_in_train": sorted(validation_labels - train_labels),
        "test_labels_unseen_in_train": sorted(test_labels - train_labels),
    }
    return (
        [row for task_id in sorted(train_ids) for row in grouped[task_id]],
        [row for task_id in sorted(validation_ids) for row in grouped[task_id]],
        [row for task_id in sorted(test_ids) for row in grouped[task_id]],
        metadata,
    )


def _classification_report(
    logits: np.ndarray, target_ids: np.ndarray, label_names: list[str]
) -> dict[str, Any]:
    predicted_ids = np.argmax(logits, axis=-1)
    per_label: dict[str, dict[str, float | int]] = {}
    f1_values = []
    weighted_f1 = 0.0
    confusion: dict[str, dict[str, int]] = {}
    for target_id, target_name in enumerate(label_names):
        true_positive = int(np.sum((target_ids == target_id) & (predicted_ids == target_id)))
        actual_count = int(np.sum(target_ids == target_id))
        predicted_count = int(np.sum(predicted_ids == target_id))
        precision = true_positive / predicted_count if predicted_count else 0.0
        recall = true_positive / actual_count if actual_count else 0.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        per_label[target_name] = {
            "support": actual_count,
            "precision": precision,
            "recall": recall,
            "f1": f1,
        }
        f1_values.append(f1)
        weighted_f1 += actual_count * f1
        confusion[target_name] = {
            label_name: int(
                np.sum((target_ids == target_id) & (predicted_ids == predicted_id))
            )
            for predicted_id, label_name in enumerate(label_names)
        }
    support_total = int(len(target_ids))
    return {
        "accuracy": float(np.mean(predicted_ids == target_ids)) if support_total else 0.0,
        "macro_f1": float(np.mean(f1_values)) if f1_values else 0.0,
        "weighted_f1": weighted_f1 / support_total if support_total else 0.0,
        "majority_baseline_accuracy": (
            float(max(Counter(target_ids.tolist()).values()) / support_total)
            if support_total
            else 0.0
        ),
        "per_label": per_label,
        "confusion_matrix": confusion,
    }


def _fit_temperature(logits: np.ndarray, labels: np.ndarray) -> float:
    if not len(labels):
        return 1.0
    best_temperature = 1.0
    best_loss = float("inf")
    for temperature in np.linspace(0.5, 4.0, 351):
        scaled = logits / temperature
        shifted = scaled - scaled.max(axis=1, keepdims=True)
        log_probs = shifted - np.log(np.exp(shifted).sum(axis=1, keepdims=True))
        loss = float(-log_probs[np.arange(len(labels)), labels].mean())
        if loss < best_loss:
            best_loss = loss
            best_temperature = float(temperature)
    return best_temperature


def _calibration_metrics(
    logits: np.ndarray, labels: np.ndarray, temperature: float
) -> dict[str, Any]:
    if not len(labels):
        return {"n": 0, "ece": 0.0, "brier": 0.0}
    scaled = logits / temperature
    shifted = scaled - scaled.max(axis=1, keepdims=True)
    exp = np.exp(shifted)
    probabilities = exp / exp.sum(axis=1, keepdims=True)
    predictions = probabilities.argmax(axis=1)
    confidence = probabilities.max(axis=1)
    correct = (predictions == labels).astype(float)
    ece = 0.0
    for lower in np.linspace(0.0, 0.9, 10):
        upper = lower + 0.1
        selected = (confidence >= lower) & (
            (confidence < upper) if upper < 1.0 else (confidence <= upper)
        )
        if selected.any():
            ece += float(selected.mean()) * abs(
                float(confidence[selected].mean()) - float(correct[selected].mean())
            )
    one_hot = np.eye(logits.shape[1])[labels]
    brier = float(np.square(probabilities - one_hot).sum(axis=1).mean())
    risk_coverage = []
    for threshold in (0.4, 0.5, 0.6, 0.7, 0.8, 0.9):
        accepted = confidence >= threshold
        risk_coverage.append(
            {
                "threshold": threshold,
                "accepted": int(accepted.sum()),
                "coverage": float(accepted.mean()),
                "accuracy_on_accepted": (
                    float(correct[accepted].mean()) if accepted.any() else None
                ),
            }
        )
    return {
        "n": int(len(labels)),
        "ece": ece,
        "brier": brier,
        "risk_coverage": risk_coverage,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dataset",
        type=Path,
        default=Path(__file__).parent / "data/root_disposition.jsonl",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(__file__).parent / "artifacts/minilm-root-disposition",
    )
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--revision", default=DEFAULT_MODEL_REVISION)
    parser.add_argument("--seed", type=int, default=41)
    parser.add_argument("--epochs", type=float, default=5.0)
    parser.add_argument("--batch-size", type=int, default=16)
    parser.add_argument("--max-length", type=int, default=256)
    args = parser.parse_args()

    rows = _load_examples(args.dataset)
    train_rows, validation_rows, test_rows, split = _split_by_source_family(
        rows, args.seed
    )
    labels = sorted({row["labels"]["class"] for row in train_rows})
    label_to_id = {label: index for index, label in enumerate(labels)}
    validation_rows = [
        row for row in validation_rows if row["labels"]["class"] in label_to_id
    ]
    test_rows = [row for row in test_rows if row["labels"]["class"] in label_to_id]
    if len(labels) < 2:
        raise ValueError(f"need at least two disposition labels in training: {labels}")
    if not validation_rows or not test_rows:
        raise ValueError(
            "task-grouped split produced an empty validation or test set; add source tasks"
        )

    args.output_dir.mkdir(parents=True, exist_ok=True)
    tokenizer = AutoTokenizer.from_pretrained(args.model, revision=args.revision)
    model = AutoModelForSequenceClassification.from_pretrained(
        args.model,
        revision=args.revision,
        num_labels=len(labels),
        id2label={index: label for label, index in label_to_id.items()},
        label2id=label_to_id,
    )
    train_dataset = ClauseDataset(train_rows, tokenizer, label_to_id, args.max_length)
    validation_dataset = ClauseDataset(
        validation_rows, tokenizer, label_to_id, args.max_length
    )
    test_dataset = ClauseDataset(test_rows, tokenizer, label_to_id, args.max_length)
    training_args = TrainingArguments(
        output_dir=str(args.output_dir / "checkpoints"),
        learning_rate=2e-5,
        per_device_train_batch_size=args.batch_size,
        per_device_eval_batch_size=args.batch_size,
        num_train_epochs=args.epochs,
        weight_decay=0.01,
        eval_strategy="epoch",
        save_strategy="epoch",
        load_best_model_at_end=True,
        metric_for_best_model="eval_loss",
        greater_is_better=False,
        save_total_limit=2,
        report_to=[],
        seed=args.seed,
        data_seed=args.seed,
        use_cpu=not torch.cuda.is_available() and not torch.backends.mps.is_available(),
        dataloader_pin_memory=False,
    )
    trainer = Trainer(
        model=model,
        args=training_args,
        train_dataset=train_dataset,
        eval_dataset=validation_dataset,
        compute_metrics=lambda prediction: {
            "accuracy": _classification_report(
                prediction.predictions, prediction.label_ids, labels
            )["accuracy"],
            "macro_f1": _classification_report(
                prediction.predictions, prediction.label_ids, labels
            )["macro_f1"],
        },
    )
    trainer.train()
    validation_output = trainer.predict(validation_dataset)
    temperature = _fit_temperature(validation_output.predictions, validation_output.label_ids)
    test_output = trainer.predict(test_dataset)
    trainer.save_model(str(args.output_dir / "model"))
    tokenizer.save_pretrained(args.output_dir / "model")

    metrics = {
        "schema_version": "root-disposition-training-report-v1",
        "teacher_label_status": "silver_unadjudicated",
        "model": args.model,
        "model_revision": args.revision,
        "seed": args.seed,
        "dataset_sha256": hashlib.sha256(args.dataset.read_bytes()).hexdigest(),
        "artifact_dir": str(args.output_dir),
        "device": "cuda" if torch.cuda.is_available() else "mps" if torch.backends.mps.is_available() else "cpu",
        "counts": {
            "examples_total_resolved": len(rows),
            "train": len(train_rows),
            "validation": len(validation_rows),
            "test": len(test_rows),
            "labels": dict(Counter(row["labels"]["class"] for row in rows)),
        },
        "labels": labels,
        "split": split,
        "validation": trainer.evaluate(validation_dataset),
        "test": trainer.evaluate(test_dataset),
        "test_classification_report": _classification_report(
            test_output.predictions, test_output.label_ids, labels
        ),
        "confidence": {
            "temperature": temperature,
            "calibrated_on": "validation teacher agreement",
            "test_teacher_agreement_calibration": _calibration_metrics(
                test_output.predictions, test_output.label_ids, temperature
            ),
            "meaning": (
                "Calibrated agreement with LLM teacher labels only; not calibrated "
                "probability of human correctness."
            ),
            "minimum_abstention_threshold": None,
        },
    }
    report_text = json.dumps(metrics, indent=2, sort_keys=True) + "\n"
    (args.output_dir / "training-report.json").write_text(report_text, encoding="utf-8")
    (Path(__file__).parent / "training-report.json").write_text(
        report_text, encoding="utf-8"
    )
    (args.output_dir / "labels.json").write_text(
        json.dumps({"label_to_id": label_to_id, "id_to_label": {v: k for k, v in label_to_id.items()}}, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    shutil.rmtree(args.output_dir / "checkpoints", ignore_errors=True)
    print(json.dumps(metrics, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
