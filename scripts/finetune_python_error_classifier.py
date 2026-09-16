#!/usr/bin/env python3
"""Fine-tune a sequence classifier on generated Python error messages.

The optional ML dependencies are intentionally imported inside ``main``. Normal
Powdrr development and CI can therefore use the dataset tooling without
installing a CUDA/PyTorch stack.
"""

from __future__ import annotations

import argparse
import inspect
import json
from pathlib import Path
from typing import Any

from powdrr_lift.python_error_classifier import load_jsonl, split_records


def write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--base-model", default="answerdotai/ModernBERT-base")
    parser.add_argument("--epochs", type=float, default=3.0)
    parser.add_argument("--learning-rate", type=float, default=2e-5)
    parser.add_argument("--max-length", type=int, default=256)
    parser.add_argument("--train-batch-size", type=int, default=16)
    parser.add_argument("--eval-batch-size", type=int, default=32)
    parser.add_argument("--validation-size", type=float, default=0.1)
    parser.add_argument("--test-size", type=float, default=0.2)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    try:
        import torch  # pyright: ignore[reportMissingImports]
        from transformers import (  # pyright: ignore[reportMissingImports]
            AutoModelForSequenceClassification,
            AutoTokenizer,
            DataCollatorWithPadding,
            Trainer,
            TrainingArguments,
        )
    except ImportError as error:
        raise SystemExit(
            "Fine-tuning requires optional 'torch' and 'transformers' packages; "
            "install them in the ML environment before running this script."
        ) from error

    records = load_jsonl(args.dataset)
    splits = split_records(
        records,
        seed=args.seed,
        validation_size=args.validation_size,
        test_size=args.test_size,
    )
    labels = sorted({str(record["label"]) for record in records})
    label_to_id = {label: index for index, label in enumerate(labels)}
    id_to_label = {index: label for label, index in label_to_id.items()}

    class ErrorDataset(torch.utils.data.Dataset):  # pyright: ignore[reportUntypedBaseClass]
        def __init__(self, rows: list[dict[str, Any]]) -> None:  # pyright: ignore[reportMissingSuperCall]
            self.rows = rows

        def __len__(self) -> int:
            return len(self.rows)

        def __getitem__(self, index: int) -> dict[str, Any]:
            row = self.rows[index]
            encoded = tokenizer(
                str(row["text"]), truncation=True, max_length=args.max_length
            )
            encoded["labels"] = label_to_id[str(row["label"])]
            return encoded

    tokenizer = AutoTokenizer.from_pretrained(args.base_model)
    model = AutoModelForSequenceClassification.from_pretrained(
        args.base_model,
        num_labels=len(labels),
        id2label=id_to_label,
        label2id=label_to_id,
    )

    def compute_metrics(evaluation: Any) -> dict[str, float]:
        from powdrr_lift.python_error_classifier import classification_metrics

        predictions = evaluation.predictions.argmax(axis=-1).tolist()
        metrics = classification_metrics(
            evaluation.label_ids.tolist(), predictions, labels
        )
        return {"accuracy": metrics["accuracy"], "macro_f1": metrics["macro_f1"]}

    output_dir = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    training_kwargs: dict[str, Any] = {
        "output_dir": str(output_dir / "checkpoints"),
        "num_train_epochs": args.epochs,
        "learning_rate": args.learning_rate,
        "per_device_train_batch_size": args.train_batch_size,
        "per_device_eval_batch_size": args.eval_batch_size,
        "weight_decay": 0.01,
        "logging_strategy": "epoch",
        "save_strategy": "epoch",
        "load_best_model_at_end": True,
        "metric_for_best_model": "macro_f1",
        "greater_is_better": True,
        "report_to": [],
        "seed": args.seed,
    }
    # Transformers renamed this argument; support both installed API versions.
    if "eval_strategy" in inspect.signature(TrainingArguments.__init__).parameters:
        training_kwargs["eval_strategy"] = "epoch"
    else:
        training_kwargs["evaluation_strategy"] = "epoch"
    trainer = Trainer(
        model=model,
        args=TrainingArguments(**training_kwargs),
        train_dataset=ErrorDataset(splits["train"]),
        eval_dataset=ErrorDataset(splits["validation"] or splits["test"]),
        tokenizer=tokenizer,
        data_collator=DataCollatorWithPadding(tokenizer=tokenizer),
        compute_metrics=compute_metrics,
    )
    trainer.train()
    trainer.save_model(output_dir)
    tokenizer.save_pretrained(output_dir)
    write_json(
        output_dir / "labels.json", {"labels": labels, "label_to_id": label_to_id}
    )
    write_json(
        output_dir / "splits.json",
        {key: [str(row["id"]) for row in value] for key, value in splits.items()},
    )
    write_json(
        output_dir / "training-config.json",
        {
            "base_model": args.base_model,
            "seed": args.seed,
            "max_length": args.max_length,
        },
    )
    print(f"saved fine-tuned classifier to {output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
