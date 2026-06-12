# Repository Instructions

- Always do repository work in a dedicated git worktree, not the primary checkout.
- If you are not already in a worktree, create one before editing code.
- Never push directly to `main` or any protected branch.
- Use a feature branch from the worktree for all changes.
- Open a pull request for every change set.
- Do not merge your own PR; the user must review and merge it.
- Keep changes scoped to the requested task and avoid unrelated edits.
- Before pushing a PR, run the full verification and validation suite for the
  change set, including tests, formatting, and type checks when available.
