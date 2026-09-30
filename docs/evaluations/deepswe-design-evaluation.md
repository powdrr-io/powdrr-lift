# Evaluating DeepSWE design-only runs

The first evaluator targets `python-statemachine-state-data-scoping`. Run the
Harbor adapter with `POWDRR_DESIGN_ONLY=1` to compile the real Procedrr design
without starting a coding agent. Retrieve the artifact directory, then point the
evaluation command at it and the local DeepSWE task directory:

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

uv run python -m science.deepswe.design_evaluation_cli design \
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
uv run python -m science.deepswe.design_evaluation_cli prompt \
  --task-dir /path/to/deep-swe/tasks/python-statemachine-state-data-scoping \
  --run-dir /tmp/state-data-design-run \
  --report /tmp/state-data-design-run/prompt-quality-evaluation.json
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
