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

Runs are isolated in a temporary repository and do not mutate a real worktree.
Use deterministic scenarios for CI regression coverage after changing
guidance.
