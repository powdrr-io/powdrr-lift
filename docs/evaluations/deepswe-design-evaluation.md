# Evaluating DeepSWE design-only runs

The initial prompt-level comparison covers three tasks with different
instruction shapes and implementation domains:

| Task | Domain | Rubric |
| --- | --- | --- |
| `python-statemachine-state-data-scoping` | Python state ownership, lifecycle, copy, and validation rules | `deepswe-python-statemachine-state-data.yaml` |
| `ytt-jsonpath-query-api` | Go API with a large set of selectors, conditional behavior, and error semantics | `deepswe-ytt-jsonpath-query-api.yaml` |
| `skrub-duration-encoding` | Python transformer configuration, data dependent resolution, and integration behavior | `deepswe-skrub-duration-encoding.yaml` |

For prompt captures, run the Harbor adapter with
`POWDRR_CAPTURE_WORKER_PROMPTS_ONLY=1` to compile the real Procedrr design and
worker request without starting a coding agent. Retrieve the artifact directory,
then evaluate it against the matching task and rubric. Use a fresh Pier job root
and output root for every capture; record the installed Powdrr revision and
provider configuration with the artifacts.

The design-only evaluator targets `python-statemachine-state-data-scoping`.
Run the Harbor adapter with `POWDRR_DESIGN_ONLY=1` to compile the real Procedrr
design without starting a coding agent. Retrieve the artifact directory, then
point the evaluation command at it and the local DeepSWE task directory:

```bash
/Users/gregory/.local/share/uv/tools/datacurve-pier/bin/pier run \
  --debug --force-build --no-delete \
  -p /path/to/deep-swe/tasks/python-statemachine-state-data-scoping \
  --agent-import-path \
    powdrr_lift.integrations.harbor.powdrr_agent:PowdrrAgent \
  --env docker \
  --ae DEEPINFRA_API_TOKEN="$DEEPINFRA_API_TOKEN" \
  --ae POWDRR_DESIGN_ONLY=1 \
  --ae POWDRR_OUTPUT_ROOT=/tmp/state-data-design-run \
  --artifact /tmp/state-data-design-run

uv run powdrr-lift evaluate-deepswe-design \
  --task-dir /path/to/deep-swe/tasks/python-statemachine-state-data-scoping \
  --run-dir /tmp/state-data-design-run \
  --report /tmp/state-data-design-run/design-quality-evaluation.json
```

The design evaluator reads the task instruction, `solution/solution.patch`, and
`tests/test.patch` after design generation. It checks that every rubric source
excerpt and reference test or solution symbol still exists in that task, then
asks a judge to classify each expected behavior against the canonical design
projection. The judge must quote text from the candidate design to support a
positive finding. The solution and verifier references are not passed to the
design generation process.

The report links findings to source excerpts, instruction-ledger clause IDs,
solution symbols, verifier test names, and candidate-design evidence. Critical
criteria must all be supported for `summary.passed` to be true. Weighted
coverage is a diagnostic; it cannot hide a critical failure.

## Capturing and evaluating worker prompts

To run the same `implement-feature` Procedrr through code-task compilation and
precondition review, but stop before invoking the coding agent, set
`POWDRR_CAPTURE_WORKER_PROMPTS_ONLY=1` for the Harbor run. This produces the
provider-ready prompt files and request metadata under
`artifacts/prompts/` and `artifacts/requests/` in the output directory. The
mode uses the same instruction, design interview, task planning, and request
compiler as a regular run; it does not hand the prompt to a worker.

Evaluate the captured prompts after generation:

```bash
uv run powdrr-lift evaluate-deepswe-prompt \
  --task-dir /path/to/deep-swe/tasks/python-statemachine-state-data-scoping \
  --run-dir /tmp/state-data-prompt-run \
  --rubric docs/evaluations/deepswe-python-statemachine-state-data.yaml \
  --report /tmp/state-data-prompt-run/prompt-quality-evaluation.json
```

For the two comparison tasks, set `--task-dir` to the corresponding task and
pass the matching rubric explicitly, for example:

```bash
uv run powdrr-lift evaluate-deepswe-prompt \
  --task-dir /path/to/deep-swe/tasks/ytt-jsonpath-query-api \
  --run-dir /path/to/captured-ytt-run \
  --rubric docs/evaluations/deepswe-ytt-jsonpath-query-api.yaml \
  --report /path/to/captured-ytt-run/prompt-quality-evaluation.json

uv run powdrr-lift evaluate-deepswe-prompt \
  --task-dir /path/to/deep-swe/tasks/skrub-duration-encoding \
  --run-dir /path/to/captured-skrub-run \
  --rubric docs/evaluations/deepswe-skrub-duration-encoding.yaml \
  --report /path/to/captured-skrub-run/prompt-quality-evaluation.json
```

The evaluator verifies task identity, exact instruction-ledger source text,
prompt/request pairing and fingerprints, and that rubric anchors still exist
in the reference solution and verifier patch. It then judges every criterion
against the captured worker prompts. The delivery-instructions criterion is an
explicit absence check: branch/commit directions must not leak into worker
prompts. Ground-truth files and rubric content are only read after prompt
capture; they are not supplied to the design interview or request compiler.

The prompt report gives a reproducible 0–100 `summary.prompt_score`. Each
criterion has weight 3 when critical and 1 when important; only `supported`
earns points. `missing`, `contradicted`, and `ambiguous` earn zero. The report
retains the weighted coverage ratio and adds verdict counts and per-dimension
scores for coverage, faithfulness, grounding, actionability, consistency/scope,
and clarity. Rubrics may assign a dimension per criterion; older rubrics without
one remain readable as `coverage` until annotated.

The separate `summary.gate` is `pass` only when every critical criterion is
supported and no evaluation errors occurred. It is `fail` for critical prompt
findings and `error` when the judge or evidence protocol fails. Findings retain
the source excerpt, rubric dimension, evidence quote, verdict, and reason;
`summary.evaluation_errors` identifies judge or evidence-protocol failures so
they are distinguishable from prompt defects. A high aggregate score never
overrides a critical failure.

### Evaluating a non-DeepSWE prompt capture

`evaluate-prompt` uses the same captured prompt index, hash checks, source
anchor validation, judge evidence protocol, and score aggregation without
requiring `task.toml`, a reference solution, or verifier patches. Create a case
manifest and a rubric with matching IDs:

```json
{
  "schema_version": "prompt-eval-case-v1",
  "case_id": "my-repository-feature",
  "instruction_path": "instructions.md"
}
```

```yaml
schema_version: prompt-eval-rubric-v1
case_id: my-repository-feature
criteria:
  - id: required-api-behavior
    instruction_excerpt: "The API must return the normalized value."
    expected_behavior: "The worker prompt directs the worker to return the normalized value."
    importance: critical
    dimension: coverage
    required_prompt_claims:
      - "return the normalized value"
    forbidden_prompt_claims: []
    validation_test_ids:
      - "tests/test_api.py::test_normalized_value"
```

Paths in the case manifest are relative to the manifest. Optional
`solution_patch_path` and `verifier_patch_path` entries allow offline reference
anchor checks using `solution_symbols` and `verifier_tests`; generic cases do
not need them. `validation_test_ids` are recorded for later benchmark
comparison and are not read or run by prompt evaluation.

```bash
uv run powdrr-lift evaluate-prompt \
  --case /path/to/prompt-eval-case.json \
  --run-dir /path/to/captured-run \
  --rubric /path/to/prompt-eval-rubric.yaml \
  --report /path/to/captured-run/prompt-quality-evaluation.json
```

The run metadata, rubric, and case must identify the same case ID. The case's
instruction file must exactly match the instruction recorded in the captured
run's ledger. Generic and DeepSWE prompt evaluations emit the same
`prompt-evaluation-report-v1` report and scoring contract. The
`evaluate-deepswe-prompt` command remains available for task-directory users.

### Comparing a prompt evaluation with benchmark validation

After a run completes validation, compare its final `run-result.json` or
`report.json` with the immutable prompt report:

```bash
uv run powdrr-lift compare-prompt-benchmark \
  --prompt-report /path/to/captured-run/prompt-quality-evaluation.json \
  --benchmark-run /path/to/benchmark-run \
  --report /path/to/benchmark-run/prompt-benchmark-comparison.json
```

The command also accepts a standalone
`coding-agent-validation-report-v1` JSON file. For a run directory it reads the
final `run-result.json` first, falling back to `report.json`; it does not infer
results from old attempts. A result's `test_id` is its mapping key, with its
validation profile name used when no test ID is emitted. Rubric
`validation_test_ids` must use those stable IDs. It checks the prompt report
fingerprint and, when present, task and request identity before comparing.

The separate `prompt-benchmark-comparison-v1` report copies the prompt score,
gate, and fingerprint, then records normalized pass, fail, not-run, and
infrastructure-error results; per-criterion mappings; unmapped failures; and
review interpretations. It also records run/task/request identity, Powdrr
revision and version, and worker profile/provider when the run artifacts
contain them. Missing mapped results become `not_run`. Timed-out or
blocked checks are `infrastructure_error`, excluded from the validation pass
rate along with not-run checks. Interpretations flag areas for review and do
not automatically assign a root cause or modify the prompt report.
The pass-rate denominator counts executed validation units; a unit is an
individual test when `test_id` is present, otherwise it is the named validation
profile. `validation_unit_count` includes passed, failed, not-run, and
infrastructure-error units.

The rubric covers distinctions exercised by the benchmark tests, including
state-owned versus callback-merged data, absent versus explicitly empty
declarations, callback mutation persistence, and shallow versus deep history.
Design and prompt reports measure different stages; neither alone predicts a
live coding score.

## Synthetic classifier cases

`semantic-prompt-cases-v1.jsonl` contains independently written source cases
for the six planned confusion families. Each row records the source text,
target proposition, allowed context, gold decisions, explicitly unspecified
details, and required or forbidden prompt claims. Cases from the
`configuration_cache` domain are held out; state-management and reporting/query
cases are in development. The loader rejects missing fields, duplicate IDs,
family/domain coverage gaps, contradictory prompt claims, and case groups or
domains that cross splits. Paraphrase variants must keep their base case's gold
decisions and remain in its split and group.

`semantic_prompt_variants.py` generates one paraphrase and one minimal
contrast proposal per base case through a provider-neutral JSON client. Pass
separately configured generation and review clients to
`generate_semantic_prompt_variants`. The generator supplies wording only. The
reviewer checks paraphrase meaning or independently annotates the contrast,
including its gold decisions and prompt claims. Accepted variants include the
review rationale and an exact source quote; invalid or rejected proposals are
returned separately with a reason. Contrast cases use `contrast_of`, must
change at least one gold decision, and share their base case's group, domain,
and split. A model reviewer is an automated quality gate, not proof of semantic
truth; disagreement or suspicious variants still need targeted human review.

Validate the case corpus with:

```bash
PYTHONPATH=src /Users/gregory/code/powdrr-lift/.venv/bin/python \
  -m powdrr_lift.workrr.semantic_prompt_cases
```

The synthetic cases complement the three real task prompt evaluations. They do
not use solution or verifier patches as source labels; those references remain
exclusive to the offline prompt evaluator.
