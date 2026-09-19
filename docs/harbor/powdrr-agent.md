# Powdrr Harbor agent

The Harbor adapter is
`integrations.harbor.powdrr_agent:PowdrrAgent`.

It passes Harbor's task instruction to Powdrr's `implement-feature` Procedrr
flow. Planning, implementation, validation, completeness review, scope review,
and repair remain defined by
`docs/procedrr/skill-definitions/implement-feature.yaml`.

The adapter runs the flow in the current Harbor checkout. It does not create a
nested worktree, fetch a branch, push, or open a pull request. The shared flow
commits the final result locally so Harbor/Pier can collect the commit.

## Running

From a checkout containing Powdrr and the integration module:

```bash
harbor run \
  -p deep-swe/tasks/<task-id> \
  --agent integrations.harbor.powdrr_agent:PowdrrAgent
```

The adapter installs `powdrr-lift` and OpenCode during Harbor setup unless the
image already contains them. To test a local Powdrr checkout, point
`POWDRR_INSTALL_SPEC` at an installable source, for example:

```text
POWDRR_INSTALL_SPEC=git+https://github.com/powdrr-io/powdrr-lift.git
```

For repeatable benchmark runs, bake pinned Powdrr and OpenCode versions into
the task image instead of installing them during each trial.

## Runtime configuration

Pass credentials through Harbor's agent environment mechanism (`--ae`), not in
the task image:

```bash
--ae DEEPINFRA_API_TOKEN=... \
--ae OPENAI_API_KEY=...
```

Useful optional variables are:

```text
POWDRR_INSTALL_SPEC       package, VCS URL, or pinned local install spec
POWDRR_VERSION            reported adapter version
POWDRR_ALLOWED_PATHS      comma-separated paths; defaults to .
POWDRR_VALIDATION_COMMAND explicit repository validation command
POWDRR_PLANNING_PROVIDER  Powdrr planning provider
POWDRR_PLANNING_MODEL     planning model override
OPENCODE_MODEL             OpenCode implementation/review model
```

The selected model API must be included in Harbor/Pier's network allowlist.
