#!/usr/bin/env python3
"""Run the fine-tuned classifier and expose calibrated scores plus abstention."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import torch
from transformers import AutoModelForSequenceClassification, AutoTokenizer

from powdrr_lift.core.classifier_input import format_classifier_input


def classify(
    text: str,
    *,
    model_dir: Path,
    report_path: Path,
    minimum_confidence: float,
    context: str | None = None,
) -> dict[str, Any]:
    if not text.strip():
        raise ValueError("text must not be empty")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    temperature = float(report["confidence"]["temperature"])
    tokenizer = AutoTokenizer.from_pretrained(model_dir)
    model = AutoModelForSequenceClassification.from_pretrained(model_dir)
    model.eval()
    local_context = {"surrounding_text": context} if context else None
    encoded = tokenizer(
        format_classifier_input(text, local_context),
        return_tensors="pt",
        truncation=True,
        max_length=256,
    )
    with torch.inference_mode():
        logits = model(**encoded).logits[0] / temperature
        probabilities = torch.softmax(logits, dim=-1)
    scores = {
        model.config.id2label[index]: float(probability)
        for index, probability in enumerate(probabilities)
    }
    label, confidence = max(scores.items(), key=lambda item: item[1])
    accepted = confidence >= minimum_confidence
    return {
        "status": "resolved" if accepted else "unresolved",
        "value": label if accepted else None,
        "reason_code": None if accepted else "classifier_abstained",
        "confidence": confidence,
        "minimum_confidence": minimum_confidence,
        "scores": scores,
        "confidence_target": "agreement with the unadjudicated LLM teacher labels",
        "provider": "local-classifier",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("text")
    parser.add_argument(
        "--context",
        help="Optional local source context used when a context retry was needed.",
    )
    parser.add_argument(
        "--model-dir",
        type=Path,
        default=Path(__file__).parent / "artifacts/minilm-root-disposition/model",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=Path(__file__).with_name("training-report.json"),
    )
    parser.add_argument("--minimum-confidence", type=float, required=True)
    args = parser.parse_args()
    if not 0.0 <= args.minimum_confidence <= 1.0:
        parser.error("--minimum-confidence must be between 0 and 1")
    print(
        json.dumps(
            classify(
                args.text,
                model_dir=args.model_dir,
                report_path=args.report,
                minimum_confidence=args.minimum_confidence,
                context=args.context,
            ),
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
