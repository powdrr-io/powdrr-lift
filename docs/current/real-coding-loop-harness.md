# Real coding-loop harness

The opt-in harness copies this repository into a temporary checkout, creates a
deliberately failing priority-queue feature and test, and runs the real
`execute-proposed-pr` coding-loop task against that copy. It verifies that the
model writes a non-trivial implementation, the initial failure is repaired, and
the copied repository's complete test suite passes.

It is outside the configured pytest `testpaths`, so it does not run in CI:

```bash
POWDRR_LIFT_RUN_LIVE_CODING_LOOP=1 \
  uv run pytest -q hardening_tests/test_real_execute_proposed_pr.py -s
```

The harness uses the normal DeepInfra credentials (`DEEPINFRA_API_TOKEN` or
`DEEPINFRA_API_KEY`) and the production client/model selection path. Temporary
repositories and workflow state are removed after each run.
