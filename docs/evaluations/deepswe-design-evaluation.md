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
