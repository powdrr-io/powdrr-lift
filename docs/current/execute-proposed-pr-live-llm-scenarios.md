# Live LLM workflow scenarios

Checked-in workflow scenarios use `provider.mode: scripted` so CI remains
deterministic. To observe how a real model responds, use a scenario with
`provider.mode: live`:

```bash
uv run powdrr-lift workflow-scenario \
  --scenario workflow-evals/scenarios/execute-proposed-pr/live-task-001.yaml \
  --repo-root . \
  --report /tmp/execute-proposed-pr-live.json \
  --json
```

`provider: auto` uses the same configured-provider lookup as workflow-task
execution. Use `provider: deepinfra-cheap` when you want an explicit provider.
Credentials come from the normal provider environment variables and are never
stored in the scenario.

The report contains complete prompt messages, parsed model outputs, transport
errors, workflow stdout/stderr, roundtrip count, and final task state. This
makes it possible to inspect the exact response that caused a repair or stall
and improve the task guidance. Live scenarios do not assert success unless
`expect` contains explicit expectations.

If `max_roundtrips` is omitted from a live scenario, there is no arbitrary
roundtrip cap. The existing stalled-action guard remains active, so a run stops
when it is repeating actions without material progress. Add
`max_roundtrips: N` when deliberately limiting cost for an experiment.

Runs are isolated in a temporary repository and do not mutate a real worktree.
Use deterministic scenarios for CI regression coverage after changing
guidance.

## Scripted response fixtures

Large deterministic transcripts can be kept in a separate JSON or YAML file:

```yaml
provider:
  mode: scripted
  responses_file: fixtures/interaction-file-log-specification.yaml
```

The file must contain a top-level list of response objects. A scenario must
provide exactly one of `responses` or `responses_file`; the referenced path is
resolved relative to the scenario file. Chain phases use the same format. This
keeps the scenario readable while making the exact model transcript reviewable,
diffable, and reusable.

Generate that fixture from a live report with:

```bash
uv run powdrr-lift extract-workflow-responses \
  --report /tmp/live-report.json \
  --output workflow-evals/scenarios/fixtures/interaction-file-log.yaml
```

The extractor accepts both live task reports (which store parsed `output`
objects) and live chat reports (which store assistant JSON messages). Review the
generated fixture before committing it, then point the scenario at it with
`responses_file`.

For a live run, generation can be done in one command by adding
`--extract-responses path/to/fixture.yaml` to `workflow-scenario`; the run still
writes its normal report when `--report` is supplied.

Add `--verify-extracted` to immediately replay the emitted responses through the
same single-phase scenario. Recording fails if the fixture cannot complete the
scenario, which makes transcript generation a self-checking operation.
