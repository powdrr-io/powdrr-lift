# DeepSWE test case prediction pilot

This pilot predicts observable test scenarios from a task's original
`instruction.md` and validation configuration available before implementation.
Verifier reports provide labels only. Prediction code receives neither verifier
data nor patches, solution files, agent plans, generated specifications, or
post-implementation validation output.

## Data collection

Build one record per task and deduplicate repeated verifier runs by task ID:

```bash
uv run python -m science.deepswe.test_case_prediction.cli collect \
  --tasks-dir /path/to/powdrr-deep-swe/tasks \
  --runs-dir /path/to/pier-runs \
  --repository-roots /path/to/task-repository-roots.json \
  --output-dir /path/to/prediction-data
```

Repeat `--runs-dir` to scan multiple Pier output roots. The optional
`repository-roots.json` maps task IDs to base repository checkouts, for example
`{"task-id": "/worktrees/task-id"}`. When supplied,
validation profiles come from Powdrr's existing repository validation
discovery. Otherwise, only commands explicitly declared in the task's
`task.toml` verifier section are included. An empty validation list means the
task did not provide usable pre-implementation validation metadata; it does not
mean that no tests exist.

The collector reads task instructions and TOML metadata, repository
configuration used by validation discovery, and `verifier/ctrf.json` files. It
never opens the task's `solution/` directory or test patch. It labels only
individual CTRF entries explicitly marked `[f2p]`. Repeated runs merge by test
name and preserve all observed statuses and artifact paths. Existing/base tests
marked `[p2p]` are excluded. The command writes one `<task-id>.json` record and
an `audit.json` coverage report per output directory. Keep benchmark artifacts
outside the repository and pass their local paths when reproducing the audit.

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
commands, and sources. Each scenario is ranked by list order and contains
`given`, `when`, `then`, category, confidence, instruction clause IDs, and
whether it is explicit or inferred. The predictor returns exact source text for
the cited clauses. Inferred cases require a rationale. The response validator
rejects unsupported clause IDs, empty fields, invalid scores, and duplicate
scenarios. Validation commands provide context about how tests run; they are not
treated as evidence for a particular behavior.

## Semantic review and scoring

Prepare a pairwise review sheet for each task:

```bash
uv run python -m science.deepswe.test_case_prediction.cli prepare-review \
  --record /path/to/prediction-data/task-id.json \
  --predictions /path/to/predictions/task-id.json \
  --output /path/to/reviews/task-id.json
```

The sheet includes prediction details and test identifiers. First set each
`ground_truth_dispositions` item's `behavior_status` to `known` or `unknown`.
Use `unknown` when the report does not establish what the test asserts and add
a rationale; those cases are excluded from semantic metrics. For known cases,
set each pair's `judgment` to `exact`, `partial`, or `no_match`, with a short
`rationale`. Compare behavior and expected outcome; shared words alone do not
make a match. Leave no pair for a known case as `unreviewed`. A prediction may
cover multiple parametrized tests in the same behavior group. The default group
removes trailing bracketed parameters from the test name. Reviewers
may merge groups only when cases assert the same behavior and expected outcome;
each known case still matches only one prediction. Give every behavior status
and reviewed pair a short rationale.

Score reviewed records after placing task records, predictions, and completed
review sheets in their respective directories:

```bash
uv run python -m science.deepswe.test_case_prediction.cli score \
  --records-dir /path/to/prediction-data \
  --predictions-dir /path/to/predictions \
  --reviews-dir /path/to/reviews \
  --output /path/to/evaluation.json
```

The report includes exact precision and recall, coverage precision and recall
(partial matches count as covered), unsupported predictions, uncovered cases,
per-task scores, and precision/recall at 5, 10, and 20 predictions. Tasks without
individual feature-test labels remain visible in the data audit but cannot
contribute to semantic metrics. Aggregate cutoffs pool unique tasks, so repeated
runs never inflate the sample size.

## Pilot interpretation

Run one predictor pass with the instruction-only ablation by creating a
temporary record with the same instruction and an empty `validation` array. Run
it again with collected validation metadata. Review both sets against the same
ground truth without exposing labels to prediction. Compare the two reports and
record missed requirements, broad predictions, unsupported predictions, and
unreviewable labels. Keep train/test splits grouped by repository family if a
later iteration trains a model.

The initial local Pier artifacts include repeated runs of a small number of
tasks. Count unique task IDs, not runs, when reporting pilot size. Individual
CTRF names are weak semantic labels; reviewers should mark cases whose behavior
cannot be established from the available verifier report as `unknown`.

### Local data audit (2026-09-28)

The current local snapshot contains 114 unique DeepSWE task instructions. The
retained Pier outputs include 17 runs for 2 unique tasks: 5 runs for
`gql-incremental-graphql-delivery` (17 distinct `[f2p]` tests after
deduplication) and 12 runs for `python-statemachine-state-data-scoping` (72
distinct `[f2p]` tests). The task bundles provide no usable pre-implementation
validation commands, so this snapshot supports an instruction-only pilot, not a
measured instruction-plus-validation comparison. Do not count the 17 runs as 17
independent examples. A broader semantic score needs predictions and reviewed
test behavior for more unique tasks; until then, use the data audit and
qualitative review, and do not claim generalization.

An instruction-only baseline was generated for both labeled tasks with
`deepinfra-cheap` / `deepseek-ai/DeepSeek-V4-Flash-0731`, prompt
`test-scenario-baseline-v3`, and a maximum of 8 ranked scenarios per task. A
manual review of the test identifiers produced 16 predictions against 89 test
cases. Exact precision was 0.75 and exact recall was 0.315; counting partial
matches as coverage gives precision 0.813 and recall 0.483. At 5 predictions,
precision was 0.80 and recall was 0.371. The 10- and 20-case scores equal the
8-case result because of the current output cap. Review decisions use CTRF test
identifiers rather than test source, so these scores are exploratory. They
measure this two-task sample and do not establish performance on unseen tasks.

| Task | Predictions | Labeled tests | Exact precision / recall | Coverage precision / recall |
| --- | ---: | ---: | ---: | ---: |
| `gql-incremental-graphql-delivery` | 8 | 17 | 0.625 / 0.294 | 0.750 / 0.529 |
| `python-statemachine-state-data-scoping` | 8 | 72 | 0.875 / 0.319 | 0.875 / 0.472 |
