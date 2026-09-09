# Agent-driven feature run

`agent-feature-e2e` is the end-to-end proving ground for a feature. It prompts
the live Powdrr agent; it does not implement the feature itself.

From a dedicated worktree with the required LLM credentials configured:

```bash
uv run powdrr-lift agent-feature-e2e \
  --feature-name interaction-file-log \
  --feature-request 'I want to specify a feature where all human and LLM interactions are written to a file log

interaction-file-logging, capture all human and llm interaction inputs and outputs. format should be json, use a hidden directory like .powdrr and file name "interaction-log.json". Interactions are what was the input and output of every interaction with the user or with the LLM.' \
  --provider deepinfra-cheap \
  --answer 'Use sensible defaults for unspecified details and keep the feature narrowly scoped.'
```

The live harness defaults to `deepinfra-cheap`; pass `--provider` explicitly to
select another configured provider.

The runner performs these phases in order:

1. `specify-a-feature` receives the feature request and writes the specification.
2. `start-implementing-feature` plans the implementation from those generated
   artifacts.
3. Every generated PR workflow is handed to `process-workflow-task`, so the
   agent performs the planned test, implementation, review, and validation work.
4. The generated `feature-pr-specification.yaml` is evaluated after the design
   interview completes.

The report and phase transcripts are written under
`.powdrr/agent-feature-run/`. They are deliberately separate from deliverable
files and can be retained as the source for a later deterministic replay
fixture. A failed phase stops the run and leaves the report available for
diagnosis; no later phase is silently run against incomplete context.
