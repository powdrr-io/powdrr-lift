# Root disposition classifier pilot

This pilot builds root disposition examples from every Python task in the local
DeepSWE corpus. `build_dataset.py` uses the existing instruction ledger,
atomicity contract, disposition prompt definitions, semantic decision binder,
and structured Workrr provider client. It stops after root disposition; it does
not generate full feature designs, verification matrices, or implementation
plans.

The labels are **LLM teacher labels**, not adjudicated gold. The current LLM
response contract has no confidence field, so examples preserve `confidence:
null`. Model probabilities from the later fine-tuning run measure agreement
with this teacher-labeled dataset until separately validated against human
adjudication.

Run the focused harness from the repository root:

```bash
uv run python science/classifications/root_disposition/build_dataset.py \
  --tasks-dir /path/to/powdrr-deep-swe/tasks \
  --output-dir science/classifications/root_disposition/data \
  --provider deepinfra-cheap
```

Use `--task-id` to run one task or `--limit` for a small pilot. The harness runs
independent clause-level atomicity and root-disposition requests concurrently
(`--clause-workers`, default 4 for hosted providers and 1 for the local provider);
`--workers` controls concurrent tasks. The local provider requires serial
requests because it shares one in-process model instance. Completed task artifacts
are resumable. Each task artifact includes the original instruction,
deterministic sentence ledger, atomicity decisions, atomic ledger, and bound
root decisions with the teacher responses. The consolidated
`root_disposition.jsonl` contains one example per atomic clause with
repository-family provenance for leakage-safe splits; tasks from the same
repository stay in the same split. These task runs, the source instructions,
and the consolidated dataset are checked in, so retraining from these labels
does not call an LLM.

When the isolated root pass returns `source_ambiguous` or
`source_underspecified`, the harness retries once with the containing source
sentence and its immediate neighbors. The task artifact records this under
`context_refinement` (including `needs_context`, the trigger, context level,
context text, and both responses); it is workflow metadata, not a disposition
label. The consolidated examples retain the context status and local context,
and the fine-tuning and prediction scripts serialize that context alongside
the proposition. This first version only retries explicit
ambiguity/underspecification abstentions, so it does not catch
context-dependent cases that the first pass labels confidently.

The training entry point fine-tunes `microsoft/MiniLM-L12-H384-uncased` with
Hugging Face Transformers. It groups train/validation/test partitions by
DeepSWE repository family so tasks from one repository cannot leak across
partitions. Repository families reserved by the human annotation packet stay
out of training on future runs. It reports coverage and metrics. This dataset
is a feasibility pilot, not evidence that the model is ready to replace the
LLM. Its calibrated scores estimate agreement with the unadjudicated teacher
labels, not human correctness.

The `adjudication/` directory contains the rubric and a deterministic,
125-example double-label packet drawn from the pilot's held-out repository
families. The packet remains unadjudicated until two human reviewers label it
independently and a reviewer resolves disagreements. See
[`adjudication/README.md`](adjudication/README.md) before distributing the
blinded sheets.

## Jev comparison

`compare_jev.py` runs the same root-disposition choice over the completed
human review sheet and compares Jev with the teacher LLM labels and that
reviewer's labels. Set `TYPESAFEAI_API_KEY` (or the official SDK environment
variable `TYPESAFE_API_KEY`) and run:

```bash
uv run python science/classifications/root_disposition/compare_jev.py \
  --annotator a --resume
```

The script writes resumable per-example predictions and an aggregate report to
`jev-comparison/`. The first run uses the held-out 125-example review batch.
Human agreement is preliminary: annotator A has completed their sheet, but
annotator B and final adjudication are still pending. Since the production
disposition schema has no `formatting_artifact` value, those reviewer labels
are normalized to `unresolved` for the comparison. Jev uses `jev-latest`, so
the provider may update the underlying model version over time.

Install the optional training dependencies and fine-tune:

```bash
uv sync --extra classifier-training
uv run --extra classifier-training python \
  science/classifications/root_disposition/train_classifier.py
```

The model, tokenizer, label mapping, and model training report are stored under
`artifacts/minilm-root-disposition/`; the large weight file uses Git LFS.
Temporary checkpoints are removed after the final model is saved. The compact
evaluation report is also kept beside this README. To inspect one prediction,
pass an explicit abstention threshold:

```bash
uv run --extra classifier-training python \
  science/classifications/root_disposition/predict.py \
  "Do not add automatic retries." --minimum-confidence 0.8
```
