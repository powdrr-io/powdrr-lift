# Live implement-feature smoke run

Use `bin/test-live-implement-feature` when validating a change to the
production implement-feature flow. This is an opt-in acceptance run, not a
regression test: it creates a disposable Python repository, invokes the real
`workrr-feature` CLI, and uses the configured planning provider and OpenCode
executable.

## Run

Configure credentials for the selected planning provider and make `opencode`
available on `PATH`, then run:

```bash
bin/test-live-implement-feature
```

Useful overrides include:

```bash
bin/test-live-implement-feature \
  --planning-model <model> \
  --opencode-executable /path/to/opencode \
  --opencode-model <model> \
  --timeout 1800 \
  --keep-repository
```

The command creates a local bare `origin` and a tiny repository containing an
incomplete greeting program and a failing feature test. It runs with
`--no-open-pr`, so it exercises planning, Structrr proposal generation and
review, OpenCode implementation, validation, evidence, and post-implementation
review without creating an external PR.

Run artifacts are written under `.powdrr/live-implement-feature/<run-id>/`.
The repository and artifacts are retained on failure. On success, the
repository is removed unless `--keep-repository` is supplied.

The command exits nonzero unless the endpoint reports `completed`, validation
and review pass, verification obligations are non-empty and failure-free, the
expected implementation exists in the implementation worktree, and the
implementation commit leaves both repositories clean.
