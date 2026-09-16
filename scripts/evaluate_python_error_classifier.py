#!/usr/bin/env python3
"""Evaluate a fine-tuned Python error classifier on its held-out split."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from powdrr_lift.python_error_classifier import classification_metrics, load_jsonl


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument(
        "--split", choices=("train", "validation", "test"), default="test"
    )
    parser.add_argument("--batch-size", type=int, default=32)
    parser.add_argument("--max-length", type=int, default=256)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        import torch  # pyright: ignore[reportMissingImports]
        from transformers import (  # pyright: ignore[reportMissingImports]
            AutoModelForSequenceClassification,
            AutoTokenizer,
        )
    except ImportError as error:
        raise SystemExit(
            "Evaluation requires optional 'torch' and 'transformers' packages."
        ) from error

    labels_metadata = json.loads(
        (args.model_dir / "labels.json").read_text(encoding="utf-8")
    )
    labels = [str(label) for label in labels_metadata["labels"]]
    split_metadata = json.loads(
        (args.model_dir / "splits.json").read_text(encoding="utf-8")
    )
    selected_ids = set(split_metadata[args.split])
    records = [
        record for record in load_jsonl(args.dataset) if record["id"] in selected_ids
    ]
    if not records:
        raise SystemExit(
            f"no records from split {args.split!r} were found in the dataset"
        )
    if len(records) != len(selected_ids):
        raise SystemExit("dataset is missing records from the saved evaluation split")

    tokenizer = AutoTokenizer.from_pretrained(args.model_dir)
    model = AutoModelForSequenceClassification.from_pretrained(args.model_dir)
    model.eval()
    label_to_id = {label: index for index, label in enumerate(labels)}
    predictions: list[int] = []
    actual: list[int] = []
    with torch.no_grad():
        for start in range(0, len(records), args.batch_size):
            batch = records[start : start + args.batch_size]
            encoded = tokenizer(
                [str(record["text"]) for record in batch],
                padding=True,
                truncation=True,
                max_length=args.max_length,
                return_tensors="pt",
            )
            logits = model(**encoded).logits
            predictions.extend(logits.argmax(dim=-1).tolist())
            actual.extend(label_to_id[str(record["label"])] for record in batch)
    metrics: dict[str, Any] = classification_metrics(actual, predictions, labels)
    metrics.update(
        {"split": args.split, "records": len(records), "model_dir": str(args.model_dir)}
    )
    rendered = json.dumps(metrics, indent=2, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
