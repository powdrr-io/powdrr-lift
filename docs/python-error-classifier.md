# Python execution-error classifier

This repository contains a reproducible first stage of the repair-guidance
pipeline:

```text
Python stderr / error message -> error classification -> repair technique (later)
```

This change implements only classification. The classifier consumes the
normalized final exception line, while the JSONL dataset retains the source
snippet, scenario, and complete traceback for auditability and future repair
technique work.

## Generate ground truth

The generator executes each fixture in a fresh subprocess using the selected
Python interpreter. It verifies the expected exception class and records the
actual message:

```bash
uv run python scripts/generate_python_error_dataset.py \
  --output data/python-errors.jsonl --repetitions 20
```

Argument-binding failures are separate labels (missing, unexpected, duplicate,
positional-only, keyword-only, and wrong arity) even though Python reports
most of them as `TypeError`. Later repair techniques can therefore differ.

## Fine-tune and evaluate

The default base model is `answerdotai/ModernBERT-base`. In an ML environment
with PyTorch and Transformers installed, run:

```bash
uv run python scripts/finetune_python_error_classifier.py \
  --dataset data/python-errors.jsonl \
  --output-dir artifacts/python-error-classifier
uv run python scripts/evaluate_python_error_classifier.py \
  --dataset data/python-errors.jsonl \
  --model-dir artifacts/python-error-classifier \
  --output artifacts/python-error-classifier/test-metrics.json
```

Fine-tuning uses only `text`; code and traceback fields remain audit evidence.
The scripts save exact split IDs, labels, and per-class precision, recall, F1,
and support so evaluation cannot silently use a different random split.
