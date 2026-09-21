# Benchmarking Powdrr with DeepSWE

Powdrr can run the shared Procedrr `implement-feature` flow as an installed
Harbor/Pier agent. The Harbor adapter runs the design, implementation,
validation, completeness review, scope review, and repair stages in the task's
existing checkout. It commits the result locally; Harbor/Pier and the DeepSWE
verifier collect and grade that commit.

## Prerequisites

Install Pier outside the task container:

```bash
uv tool install datacurve-pier
```

Obtain a local Harbor-format DeepSWE task dataset. A task directory contains an
`instruction.md`, `task.toml`, and its environment and verifier files. The
first run should use one small task rather than the complete dataset.

The Powdrr adapter is included in the Powdrr wheel. It reports and installs
these pinned runtime versions when they are not already present in the image:

| Component | Version |
| --- | --- |
| Powdrr | `0.1.0` |
| OpenCode | `1.18.31` |

For a repeatable benchmark image, preinstall those versions. The adapter's
fallback installation is useful for a first smoke test but requires package
registry access.

## Credentials and model

Pass the DeepInfra key through the agent environment, never in the task image
or repository:

```bash
export DEEPINFRA_API_KEY="..."
```

The Harbor adapter configures both Powdrr planning and OpenCode implementation
and review to use:

```text
deepinfra/deepseek-ai/DeepSeek-V4-Flash-0731
```

Powdrr also accepts `DEEPINFRA_API_TOKEN`; it normalizes that spelling for
OpenCode when necessary. No OpenAI credential is required for this benchmark
configuration.

## First smoke test

Run one task with Pier and the packaged adapter:

```bash
export DEEPINFRA_API_KEY="..."

pier run \
  -p deep-swe/tasks/<task-id> \
  --agent powdrr_lift.integrations.harbor.powdrr_agent:PowdrrAgent \
  --env docker
```

If the task dataset is at another location, replace the `-p` value with that
task directory. Pier/Harbor must allow network access to DeepInfra during the
agent run, and to PyPI/npm if the image does not already contain Powdrr and
OpenCode. The task's own verifier remains the final correctness check.

The adapter expects the task checkout to start clean and configures git
identity in the task image or environment. It does not create a pull request,
push, or create a nested worktree.

## Validation discovery

No validation command is required on the command line. Structrr bootstrap
examines the task repository and discovers declared validation from project and
CI configuration, including Python, npm, Go, Rust, Maven, and Gradle projects.
All discovered profiles run before review and repair. An explicit
`POWDRR_VALIDATION_COMMAND` remains available as an override when a task has a
special command, but normal DeepSWE runs should omit it.

## Telemetry

The agent writes its run bundle under:

```text
.powdrr/feature-runs/<task-name>/
```

Important artifacts include:

- `run-metadata.json`: task ID and detected Powdrr/OpenCode runtime data;
- `run-result.json`: stable summary of the final status, commit/worktree,
  validation, review, and pull-request fields;
- `failure.json`: typed failure stage and references to preserved telemetry
  when a run cannot complete;
- `validation-bootstrap.yaml`: Structrr's repository inspection result;
- `implementation-request.json` and `requests/`: bounded worker requests;
- `artifacts/attempts/`: OpenCode attempt receipts and lifecycle events;
- `artifacts/validations/`: validation reports;
- `artifacts/checkpoints/`: per-operation diff checkpoints;
- `opencode/`: OpenCode JSONL lifecycle diagnostics.

Pier/Harbor synchronizes `.powdrr/feature-runs` as the agent's remote session
log directory. Completed trials are stored by Pier under its `jobs/` directory.

## Scaling up

After the single-task smoke test succeeds:

1. inspect the commit and `run-result.json`;
2. inspect the OpenCode and validation telemetry;
3. confirm the DeepSWE verifier result;
4. run a small deterministic sample;
5. only then run the larger benchmark set.

The first live run is expected to validate the container image, provider
network allowlist, git identity, adapter discovery, and telemetry collection
at the same time.

## Running a local Powdrr branch with Pier

Use the installed `datacurve-pier` runner for branch smoke tests. The custom
agent must be importable by the host Pier process, and the task container
installs the pushed Powdrr branch from GitHub. The direct `harbor` CLI has a
different setup path and should not be substituted for this command.

From the Powdrr worktree, run:

```bash
export DEEPINFRA_API_TOKEN="..."
export POWDRR_WORKTREE="$(pwd)"
export PYTHONPATH="$POWDRR_WORKTREE/src"
export POWDRR_BRANCH="codex/your-powdrr-branch"

/Users/gregory/.local/share/uv/tools/datacurve-pier/bin/pier run \
  --debug \
  --force-build \
  --no-delete \
  -p /path/to/deep-swe/tasks/python-statemachine-state-data-scoping \
  --agent-import-path \
    powdrr_lift.integrations.harbor.powdrr_agent:PowdrrAgent \
  --env docker \
  --ae DEEPINFRA_API_TOKEN="$DEEPINFRA_API_TOKEN" \
  --ae POWDRR_INSTALL_SPEC="git+https://github.com/powdrr-io/powdrr-lift.git@$POWDRR_BRANCH" \
  --ae POWDRR_OUTPUT_ROOT=/tmp/powdrr-feature-run \
  --ae POWDRR_COMMAND_LOG=/tmp/powdrr-agent-command.log \
  --artifact /tmp/powdrr-feature-run \
  --artifact /tmp/powdrr-agent-command.log \
  -o /tmp/powdrr-pier-jobs
```

`--no-delete` keeps the Docker container available for debugging after a
failure. The two artifact paths preserve the Powdrr run bundle and wrapper
command log in the Pier job directory. Use a fresh `-o` directory for each
run. The branch must be pushed before starting the run, and `PYTHONPATH` must
point at the host worktree so Pier can import the custom agent.
