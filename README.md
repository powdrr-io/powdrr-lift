# powdrr-lift

`powdrr-lift` parses structured changelog YAML into typed Python objects and
exposes the same core logic through a CLI and an MCP server.

## Layout

- `src/powdrr_lift/` contains the importable library code.
- `tests/` contains `pytest`-based tests.
- `pyproject.toml` is the single source of packaging and tooling config.

## Setup

```bash
uv sync --extra dev
```

## Common commands

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests
```

## CLI

The CLI is designed for a PR workflow with progressive disclosure:

1. Generate a template in `docs/changelogs/PR-<num>-changelog.yaml`.
2. Fill out the template using the inline instructions.
3. Validate the filled-out file.
4. Include the validated file in the PR.

Create a template for a PR:

```bash
powdrr-lift init --pr-number 123
```

Validate the PR changelog file:

```bash
powdrr-lift evaluate-pr-against-changelog --pr-number 123
```

## Example

```python
from powdrr_lift import parse_change_log

change_log = parse_change_log(your_yaml_text)
print(change_log.title)
```

## ChangeLog template

Generate a parser-safe template from a branch diff:

```bash
uv run python -m powdrr_lift.change_log_template feature/my-branch
```

## Validation

Validate a proposed ChangeLog YAML file against a branch diff:

```python
from powdrr_lift import validate_change_log_yaml

report_yaml = validate_change_log_yaml(proposed_yaml, branch_name="feature/my-branch")
```

## Edit context

Inspect prior intent for a file and line ranges before editing code:

```bash
powdrr-lift edit-context --file src/app.py --range 10:20 --parent-branch main
```

## Blame UI

Open a local blame-style view of the repository, powered by the cached index:

```bash
powdrr-lift blame-ui --parent-branch main --file src/app.py
```

The UI has three panes:

- a file tree on the left
- blame-grouped source in the center
- PR intent and change rationale on the right

The first time you open it, the CLI refreshes the local SQLite index if needed.
After that, the UI reads only from the repository and local index files.

## Skills

Installable skills live under `skills/`. The repo currently ships a
`prepare-pr-changelog` skill that drives the PR changelog workflow with the
`powdrr-lift` CLI, plus a `review-pr-changelog` skill that checks PRs for a
changelog and reviews each changelog change against the PR intent. It also
ships a `code-edit-context` skill that asks for index-backed context before
editing code so prior intent can be preserved or explicitly superseded.

## MCP

Run the MCP server locally:

```bash
powdrr-lift-mcp
```

The server exposes `get_edit_context`, `get_blame_view`, and the changelog
workflow tools.
