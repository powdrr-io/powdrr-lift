# Benchmarking Powdrr with DeepSWE

Powdrr runs the shared Procedrr `implement-feature` flow through its Harbor/Pier
adapter. Powdrr plans and reviews the change, uses mini-SWE-agent for coding, and
uses Typesafe AI's JEV for semantic classification. The adapter works in the task's
existing checkout and commits the result locally. Pier and the DeepSWE verifier
collect and grade that commit.

## Prerequisites

Install Pier with Powdrr available to its host Python environment:

```bash
uv tool install --force --upgrade --with \
  'git+https://github.com/powdrr-io/powdrr-lift.git@main' \
  datacurve-pier
```

Docker must be installed and running. Obtain a local Harbor-format DeepSWE task
dataset. A task directory contains `instruction.md`, `task.toml`, and its
environment and verifier files. Start with one task.

The adapter's default runtime versions are:

| Component | Version |
| --- | --- |
| Powdrr | `0.1.0` |
| mini-SWE-agent | `2.4.6` |

The `mini` executable comes from mini-SWE-agent. The adapter selects
`minisweagent` by default; the commands below select it explicitly as well.
Preinstall the runtimes for a repeatable image, or allow the adapter to install
them from PyPI. Set `POWDRR_INSTALL_SPEC` to a current GitHub revision to select
the container's Powdrr code rather than relying on its default package spec.

## Credentials and models

Set both credentials on the host and pass them explicitly through the agent
environment. Keep their values out of task images and committed files:

```bash
export DEEPINFRA_API_TOKEN="..."
export TYPESAFEAI_API_KEY="..."
```

DeepInfra serves the planning and coding models. The default mini-SWE-agent
coding model is:

```text
deepinfra/deepseek-ai/DeepSeek-V4-Flash-0731
```

Typesafe AI serves JEV classification at `https://api.typesafe.ai/v1/systemone`.
The intended benchmark configuration requires a Typesafe AI key in addition to
the DeepInfra key. Supported aliases are `DEEPINFRA_API_KEY` for DeepInfra, and
`TYPESAFE_API_KEY` or `SYSTEM_ONE_API_KEY` for Typesafe AI. The examples use
`DEEPINFRA_API_TOKEN` and `TYPESAFEAI_API_KEY` consistently.

`POWDRR_PLANNING_MODEL` and `POWDRR_MINISWEAGENT_MODEL` override the planning and
coding models, respectively. Inspect the effective models in `run-metadata.json`
when comparing runs.

## First smoke test

After setting both credentials, run one task with a fresh Docker build, one
attempt, and explicit artifact collection:

```bash
: "${DEEPINFRA_API_TOKEN:?Set the DeepInfra API token}"
: "${TYPESAFEAI_API_KEY:?Set the Typesafe AI API key}"
export POWDRR_TASK_DIR="/path/to/deep-swe/tasks/your-task-id"
export POWDRR_INSTALL_SPEC="git+https://github.com/powdrr-io/powdrr-lift.git@main"
export POWDRR_JOB_DIR="$(mktemp -d /tmp/powdrr-pier-smoke.XXXXXX)"

pier run \
  --debug \
  --force-build \
  --no-delete \
  -n 1 -k 1 -r 0 \
  -p "$POWDRR_TASK_DIR" \
  --agent-import-path \
    powdrr_lift.integrations.harbor.powdrr_agent:PowdrrAgent \
  --env docker \
  --ae POWDRR_CODE_AGENT=minisweagent \
  --ae DEEPINFRA_API_TOKEN="$DEEPINFRA_API_TOKEN" \
  --ae TYPESAFEAI_API_KEY="$TYPESAFEAI_API_KEY" \
  --ae POWDRR_INSTALL_SPEC="$POWDRR_INSTALL_SPEC" \
  --ae POWDRR_OUTPUT_ROOT=/tmp/powdrr-feature-run \
  --ae POWDRR_COMMAND_LOG=/tmp/powdrr-agent-command.log \
  --artifact /tmp/powdrr-feature-run \
  --artifact /tmp/powdrr-agent-command.log \
  -o "$POWDRR_JOB_DIR"
```

Replace `POWDRR_TASK_DIR` with the actual task directory. Pier must allow network
access to DeepInfra, Typesafe AI, and GitHub/PyPI for runtime installation. The
adapter includes `api.typesafe.ai` in its network allowlist, along with the host
of any configured `SYSTEM_ONE_BASE_URL`.

Use `--agent-import-path` for this custom adapter. Pier's `--agent` option selects
a built-in agent by name. For repeatable comparisons, replace `@main` in the
install spec with a pushed commit SHA and record it with
`--ae POWDRR_REVISION=COMMIT_SHA`.

The adapter expects the task checkout to start clean and configures Git identity
in the task environment. It commits locally without publishing a PR or creating
a nested worktree. The task's verifier remains the final correctness check.

## Validation discovery

No validation command is required on the command line. Structrr bootstrap
examines the task repository and discovers declared validation from project and
CI configuration, including Python, npm, Go, Rust, Maven, and Gradle projects.
An explicit `POWDRR_VALIDATION_COMMAND` remains available for tasks with a special
command, but normal DeepSWE runs should omit it.

## Telemetry and credential delivery

The default run bundle is stored under:

```text
.powdrr/feature-runs/<task-name>/
```

The examples override the output root to `/tmp/powdrr-feature-run` and collect
that directory with `--artifact`, along with `/tmp/powdrr-agent-command.log`.
Important artifacts include:

- `run-metadata.json`: task ID, runtime data, and effective planning/coding profile;
- `run-result.json`: final status, commit/worktree, validation, review, and PR fields;
- `failure.json`: failure stage and preserved telemetry references, when applicable;
- `validation-bootstrap.yaml`: Structrr's validation discovery result;
- `implementation-request.json` and `requests/`: bounded coding requests;
- `artifacts/attempts/`: coding attempt receipts and lifecycle events;
- `artifacts/validations/`: validation reports;
- `artifacts/checkpoints/`: diff checkpoints;
- `minisweagent/`: mini-SWE-agent diagnostics and trajectories;
- `procedrr-events.jsonl`: operation execution events;
- `console.log`: full diagnostics, including JEV delivery markers.

Pier synchronizes `.powdrr/feature-runs` as the adapter's default session log
directory. Explicit artifacts preserve the overridden output root. Completed
trials are stored under the chosen `-o` directory.

Confirm a `JEV_RESPONSE_RECEIVED` marker in `console.log` to establish that the
Typesafe AI request reached the classifier. `JEV_RESULT_ACCEPTED` records
successful response mapping. Investigate `JEV_CALL_SKIPPED`, `JEV_FALLBACK`, or
`JEV_FAIL_CLOSED` before treating the run as a successful test of the intended
JEV configuration. A key present on the host alone does not prove delivery into
the task container.

`--no-delete` retains the container for debugging. The wrapper command log
contains concise progress; `console.log` contains the detailed run diagnostics.
Use a fresh job directory for each run.

## Running a local Powdrr revision with Pier

Use the installed `pier` runner from the Powdrr worktree. The host imports the
adapter from the worktree, while the task container installs the pushed revision
from GitHub. After setting the two credentials above:

```bash
: "${DEEPINFRA_API_TOKEN:?Set the DeepInfra API token}"
: "${TYPESAFEAI_API_KEY:?Set the Typesafe AI API key}"
export POWDRR_WORKTREE="$(pwd)"
export PYTHONPATH="$POWDRR_WORKTREE/src"
export POWDRR_REVISION="$(git rev-parse HEAD)"
export POWDRR_INSTALL_SPEC="git+https://github.com/powdrr-io/powdrr-lift.git@$POWDRR_REVISION"
export POWDRR_TASK_DIR="/path/to/deep-swe/tasks/your-task-id"
export POWDRR_JOB_DIR="$(mktemp -d /tmp/powdrr-pier-branch.XXXXXX)"

pier run \
  --debug \
  --force-build \
  --no-delete \
  -n 1 -k 1 -r 0 \
  -p "$POWDRR_TASK_DIR" \
  --agent-import-path \
    powdrr_lift.integrations.harbor.powdrr_agent:PowdrrAgent \
  --env docker \
  --ae POWDRR_CODE_AGENT=minisweagent \
  --ae DEEPINFRA_API_TOKEN="$DEEPINFRA_API_TOKEN" \
  --ae TYPESAFEAI_API_KEY="$TYPESAFEAI_API_KEY" \
  --ae POWDRR_INSTALL_SPEC="$POWDRR_INSTALL_SPEC" \
  --ae POWDRR_REVISION="$POWDRR_REVISION" \
  --ae POWDRR_OUTPUT_ROOT=/tmp/powdrr-feature-run \
  --ae POWDRR_COMMAND_LOG=/tmp/powdrr-agent-command.log \
  --artifact /tmp/powdrr-feature-run \
  --artifact /tmp/powdrr-agent-command.log \
  -o "$POWDRR_JOB_DIR"
```

Push the selected revision before starting the run so the container can install
that exact commit. `PYTHONPATH` selects the host adapter source; it does not
install or update code in the task container. The install spec selects the
container code, and `POWDRR_REVISION` records the revision in telemetry.

## Scaling up

After a single-task smoke test succeeds:

1. Inspect the commit and `run-result.json`.
2. Inspect mini-SWE-agent and validation telemetry, including JEV delivery.
3. Confirm the DeepSWE verifier result.
4. Run a small deterministic sample.
5. Run the larger benchmark set.

The first live run checks the container image, both provider connections, Git
identity, adapter import, and telemetry collection together.

## Implementation references

The current defaults, environment forwarding, and telemetry are defined here:

- [Powdrr Harbor/Pier adapter](src/powdrr_lift/integrations/harbor/powdrr_agent.py)
- [Forwarded credentials and network allowlist](src/powdrr_lift/integrations/harbor/_env.py)
- [JEV classification and delivery markers](src/powdrr_lift/workrr/jev_classifier.py)
- [Feature execution and run metadata](src/powdrr_lift/workrr/feature_endpoint.py)
