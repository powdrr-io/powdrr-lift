# DeepSWE test case prediction pilot

The predictor uses a task's original `instruction.md` and pre-implementation
validation configuration to predict the new test cases represented by
`tests/test.patch`. The patch is ground truth for collection and human review;
the predictor receives only the instruction and validation data. The collector
never reads `solution/` or verifier-run output.

## Data collection

```bash
uv run python -m science.deepswe.test_case_prediction.cli collect \
  --tasks-dir /path/to/powdrr-deep-swe/tasks \
  --repository-roots /path/to/task-repository-roots.json \
  --output-dir /path/to/prediction-data
```

The optional repository roots JSON maps task IDs to base repository checkouts,
for example `{"task-id": "/worktrees/task-id"}`. When supplied, validation
profiles come from Powdrr's validation discovery. Otherwise, only commands
declared in the task's `task.toml` verifier section are included. An empty
validation list means no usable pre-implementation validation metadata was
available.

The collector parses named test declarations and their added source excerpts
from each task's `tests/test.patch`. It supports common Python, Go, JavaScript,
and Rust test naming forms; patches with other naming conventions remain
visible in the audit but may need a parser extension or manual labeling. Patch
content is stored only as ground truth for blinded human review and is never
sent to the prediction provider. One record per task and `audit.json` are
written to the output directory.

## Predict

Use the same pre-implementation record with the existing Powdrr provider
interface. For DeepInfra, set `DEEPINFRA_API_TOKEN` or `DEEPINFRA_API_KEY`:

```bash
uv run python -m science.deepswe.test_case_prediction.cli predict \
  --record /path/to/prediction-data/task-id.json \
  --output /path/to/predictions/task-id.json \
  --provider deepinfra \
  --model deepseek-ai/DeepSeek-V4-Flash-0731
```

The prompt includes only the instruction and validation profile names,
commands, and sources. Each ranked scenario contains `given`, `when`, `then`,
category, confidence, instruction clause IDs, and whether it is explicit or
inferred. The predictor returns exact source text for cited clauses. Inferred
cases require a rationale. The response validator rejects unsupported clause
IDs, empty fields, invalid scores, and duplicate scenarios.

## Semantic review and scoring

Prepare a pairwise review sheet for each task:

```bash
uv run python -m science.deepswe.test_case_prediction.cli prepare-review \
  --record /path/to/prediction-data/task-id.json \
  --predictions /path/to/predictions/task-id.json \
  --output /path/to/reviews/task-id.json
```

The sheet includes prediction details, test identifiers, and added test source
excerpts from the patch. Set each `ground_truth_dispositions` item's
`behavior_status` to `known` or `unknown`. Use `unknown` when the extracted test
does not establish its behavior and explain why; these cases are excluded from
semantic metrics. For known cases, judge every prediction pair as `exact`,
`partial`, or `no_match`, with a rationale. A prediction may cover multiple
parameterized tests that assert the same behavior. Reviewers may group such
cases together, but each known test case can match only one prediction.

Score completed review sheets:

```bash
uv run python -m science.deepswe.test_case_prediction.cli score \
  --records-dir /path/to/prediction-data \
  --predictions-dir /path/to/predictions \
  --reviews-dir /path/to/reviews \
  --output /path/to/evaluation.json
```

The report includes exact and coverage precision/recall, unsupported
predictions, uncovered cases, per-task scores, and precision/recall at 5, 10,
and 20 predictions. Tasks without extractable named test declarations remain
visible in the audit but cannot contribute to semantic metrics.

## Pilot interpretation

Run an instruction-only pass by creating a temporary record with the same
instruction and an empty `validation` array. Compare it with a pass using
collected validation metadata. Keep train/test splits grouped by repository
family if a later iteration trains a model.

The earlier CTRF-based pilot scores have been removed: verifier reports were
the wrong target. Evaluate predictions against cases extracted from
`tests/test.patch` instead.

The local task bundle has 114 patches. The current parser extracts 5,388 named
test declarations and 161 file-level fallback labels across all 114 tasks.
These are raw patch-derived labels, not a manually normalized case set; review
them before interpreting scores. The bundle does not include usable
pre-implementation validation metadata, so this snapshot supports an
instruction-only baseline unless repository roots are supplied.
