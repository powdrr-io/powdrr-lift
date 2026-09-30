# Repository Instructions

- Use the existing shared Python environment at
  `/Users/gregory/code/powdrr-lift/.venv` for every worktree. Do not create a
  worktree-local environment or run `uv sync` to prepare routine checks.
- Run Python tools with that environment's interpreter, for example
  `rtk proxy /Users/gregory/code/powdrr-lift/.venv/bin/python -m pytest`.
  Set `PYTHONPATH` to the current worktree's `src` directory so imports use the
  code under review rather than the shared environment's editable checkout.
  Prepend `/Users/gregory/code/powdrr-lift/.venv/bin` to `PATH` and set
  `VIRTUAL_ENV=/Users/gregory/code/powdrr-lift/.venv` so subprocesses also use
  the shared environment.
- For scripts that invoke `uv run`, set `UV_PROJECT_ENVIRONMENT` to the shared
  environment and `UV_NO_SYNC=1`; do not reinstall the project into it.
- Always do repository work in a dedicated git worktree, not the primary checkout.
- If you are not already in a worktree, create one before editing code.
- Never push directly to `main` or any protected branch.
- Use a feature branch from the worktree for all changes.
- Open a pull request for every change set.
- Do not merge your own PR; the user must review and merge it.
- Keep changes scoped to the requested task and avoid unrelated edits.
- Before pushing a PR, run the full verification and validation suite for the
  change set, including tests, `ruff format --check`, linting, and type checks
  when available.
