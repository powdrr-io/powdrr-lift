# Getting Started

This guide installs Powdrr Lift from a local checkout, configures and checks its
provider credentials, bootstraps a repository, and runs one headless feature task.
Commands below assume macOS, a shell, Git, and Python 3.12 or newer.

## 1. Clone and install Powdrr

Clone this repository, then install Powdrr from the local checkout:

```bash
git clone https://github.com/powdrr-io/powdrr-lift.git
cd powdrr-lift
```

Install [`uv`](https://docs.astral.sh/uv/getting-started/installation/) if needed.
From the repository root, install Powdrr in `uv`'s isolated tool environment.
Editable mode means changes in this checkout are used directly.

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

Put the exports in your shell profile if you want them available in future
terminals. Avoid putting real secrets in shell history; use your shell's secure
secret storage workflow when available.

## 3. Validate credentials

Change to the root of your project repository, then run the credential check.
The `--repo-root` option defaults to the current directory, so it can be omitted
when you are already in the repository. The check makes authenticated requests
to the configured planning and coding model routes, checks that the default
mini-SWE-agent executable is available, and checks GitHub authentication and
write access to the repository. It also sends an authenticated request to
Typesafe AI to verify that API key. It reports credential sources and failures
without printing key values. These provider checks make live requests and may
incur usage charges.

```bash
cd /path/to/your/repo
powdrr-lift check-credentials
```

## 4. Bootstrap your repository

From your own project's clean Git checkout, bootstrap analyzes its source and
creates the initial Structrr specification artifacts on a dedicated branch and
worktree. `--repo-root` defaults to the current directory and can be omitted.
Bootstrap opens a pull request by default; `--no-open-pr` keeps the generated
branch local.

```bash
powdrr-lift bootstrap
```

Review and merge the bootstrap pull request, then update your local checkout
before starting feature work. Keep the base checkout clean so Powdrr can create
and manage its task worktree.

## 5. Run a headless task

Write the requested outcome in a Markdown or text file in your repository, for
example `feature.md`. Be specific about behavior and constraints. Then run:

```bash
powdrr-lift implement --headless \
  --request-file feature.md \
  --work-item-name add-project-feature
```

Powdrr plans, implements, validates, and reviews the change locally. Headless
mode resolves unspecified details using documented defaults and records those
decisions in the report. The command does not publish a pull request by default.
Add `--open-pr` to push the verified branch and open a pull request; `gh` must be
authenticated with write access.

The command prints paths for the report and retained worktree. Run artifacts and
worktrees are kept under the repository's common Git directory in
`powdrr-lift/runs/` and `powdrr-lift/worktrees/`. Review the report and diff,
then remove retained task branches and worktrees when they are no longer needed.

For provider or model overrides, allowed-path limits, validation commands,
timeouts, and repair limits, see the `bootstrap` and `implement` command help or
the [README](README.md).
