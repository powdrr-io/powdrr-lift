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

uv run powdrr-lift evaluate-deepswe-design \
  --task-dir /path/to/deep-swe/tasks/python-statemachine-state-data-scoping \
  --run-dir /tmp/state-data-design-run \
  --report /tmp/state-data-design-run/design-quality-evaluation.json
```

The evaluator reads the task instruction, `solution/solution.patch`, and
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

The rubric is curated from the task instructions, reference solution, and
verifier tests. It covers the behaviors and distinctions that the task's tests
exercise, including state-owned versus callback-merged data, absent versus
explicitly empty declarations, callback mutation persistence, and shallow
versus deep history. The evaluator checks the design projection. It does not
yet grade the later task packet or exact worker-facing coding prompt, and a
passing design report does not predict a live coding score by itself.
