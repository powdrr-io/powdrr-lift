# Powdrr Harbor agent

The Harbor adapter is
`powdrr_lift.integrations.harbor.powdrr_agent:PowdrrAgent`. It is included in the Powdrr
distribution, so the adapter does not require a checkout or `PYTHONPATH`.

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
  --agent powdrr_lift.integrations.harbor.powdrr_agent:PowdrrAgent
```

The adapter installs pinned Powdrr `0.1.0` and OpenCode `1.18.31` during Harbor
setup unless the image already contains them. To test a local Powdrr checkout,
point `POWDRR_INSTALL_SPEC` at an installable source, for example:

```text
POWDRR_INSTALL_SPEC=git+https://github.com/powdrr-io/powdrr-lift.git
```

For repeatable benchmark runs, bake those pinned versions into the task image
instead of installing them during each trial.

## Runtime configuration

Pass credentials through Harbor's agent environment mechanism (`--ae`), not in
the task image:

```bash
--ae DEEPINFRA_API_KEY=...
```

The adapter configures both Powdrr planning and OpenCode implementation/review
to use DeepInfra's `deepinfra/deepseek-ai/DeepSeek-V4-Flash-0731` model.

Useful optional variables are:

```text
POWDRR_INSTALL_SPEC       package, VCS URL, or pinned local install spec
POWDRR_VERSION            reported adapter version
POWDRR_ALLOWED_PATHS      comma-separated paths; defaults to .
POWDRR_PLANNING_MODEL     planning model override
POWDRR_OUTPUT_ROOT        telemetry output directory
```

Validation is discovered from the task repository during Structrr bootstrap.
The detector recognizes Python, JavaScript/TypeScript, Go, Rust, Maven, and
Gradle projects plus CI commands. An explicit validation command remains
available as an override, but is not required for Harbor runs.

Every run writes a summary to `.powdrr/feature-runs/<task>/run-result.json` and
detailed requests, attempts, validation reports, checkpoints, and OpenCode
JSONL diagnostics under the same directory. Harbor synchronizes that directory
as the agent session log bundle.

The selected model API must be included in Harbor/Pier's network allowlist.
