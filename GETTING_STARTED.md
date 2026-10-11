# Getting Started

This guide installs Powdrr Lift from a local checkout, configures and checks its
provider credentials, bootstraps a repository, and runs one headless feature task.
Commands below assume macOS, a shell, Git, and Python 3.12 or newer.

## 1. Install Powdrr from this checkout

Install [`uv`](https://docs.astral.sh/uv/getting-started/installation/) if needed.
From the root of this repository, install Powdrr in `uv`'s isolated tool
environment. Editable mode means changes in this checkout are used directly.

```bash
uv tool install --editable --python 3.12 \
  --with-executables-from mini-swe-agent .
```

Make sure the `uv` tool directory is on `PATH` (commonly `~/.local/bin`), then
start a new terminal or reload your shell configuration and check the install:

```bash
powdrr-lift --help
mini --help
```

## 2. Set provider API keys

Powdrr's default planning and coding routes use DeepInfra. A single DeepInfra
token can serve both routes. Feature workflows can also use Typesafe AI's JEV
service for semantic classifications. Keep keys in your shell environment or a
secret manager; do not commit them to the repository.

```bash
export DEEPINFRA_API_TOKEN="your-deepinfra-token"
export TYPESAFEAI_API_KEY="your-typesafe-ai-key"
```

Powdrr also recognizes `DEEPINFRA_API_KEY` for DeepInfra and
`TYPESAFE_API_KEY` or `SYSTEM_ONE_API_KEY` for Typesafe AI. Put the exports in
your shell profile if you want them available in future terminals. Avoid putting
real secrets in shell history; use your shell's secure secret storage workflow
when available.

## 3. Validate credentials

Run the credential check from a Git repository. It makes authenticated requests
to the configured planning and coding model routes and checks that the default
mini-SWE-agent executable is available. It reports credential source and
failures without printing key values.

```bash
powdrr-lift check-credentials --repo-root /path/to/your/repo
```

With the defaults, this checks DeepInfra for planning and coding. To validate
GitHub access and repository write permission before asking Powdrr to publish a
pull request, include `--open-pr`:

```bash
powdrr-lift check-credentials --repo-root /path/to/your/repo --open-pr
```

The command does not directly check the Typesafe AI key. The JEV classifier uses
it when a feature workflow makes a semantic classification request, and falls
back to the planning model when it is missing or unavailable. Feature run logs
record `JEV_RESPONSE_RECEIVED` when Typesafe returns a response. Treat these
credential checks as live provider requests; provider usage may be billed.

## 4. Bootstrap your repository

Change to your own project's clean Git checkout. Bootstrap analyzes its source
and creates the initial Structrr specification artifacts on a dedicated branch
and worktree.

```bash
cd /path/to/your/repo
powdrr-lift bootstrap --repo-root .
```

Review the printed report, branch, and retained worktree. To have Powdrr publish
the onboarding branch as a GitHub pull request, first authenticate `gh` for an
account with write access, then run:

```bash
powdrr-lift bootstrap --repo-root . --open-pr
```

Review and merge the bootstrap pull request, then update your local checkout
before starting feature work. Keep the base checkout clean so Powdrr can create
and manage its task worktree.

## 5. Run a headless task

Write the requested outcome in a Markdown or text file in your repository, for
example `feature.md`. Be specific about behavior and constraints. Then run:

```bash
powdrr-lift implement --repo-root . --headless \
  --request-file feature.md \
  --work-item-name add-project-feature
```

Powdrr plans, implements, validates, and reviews the change locally. Headless
mode resolves unspecified details using documented defaults and records those
decisions in the report. The command does not publish by default. Add `--open-pr`
to push the verified branch and open a pull request; `gh` must be authenticated
with write access.

The command prints paths for the report and retained worktree. Run artifacts and
worktrees are kept under the repository's common Git directory in
`powdrr-lift/runs/` and `powdrr-lift/worktrees/`. Review the report and diff,
then remove retained task branches and worktrees when they are no longer needed.

For provider or model overrides, allowed-path limits, validation commands,
timeouts, and repair limits, see the `bootstrap` and `implement` command help or
the [README](README.md).
