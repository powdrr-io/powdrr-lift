# powdrr-lift

`powdrr-lift` parses structured changelog YAML into typed Python objects.

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

## Example

```python
from powdrr_lift import parse_change_log

change_log = parse_change_log(your_yaml_text)
print(change_log.title)
```
