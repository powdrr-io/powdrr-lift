.PHONY: format format-check lint test typecheck worktree

PYTHON ?= python

format:
	$(PYTHON) -m ruff format .

format-check:
	$(PYTHON) -m ruff format --check .

lint:
	$(PYTHON) -m ruff check .

test:
	$(PYTHON) -m pytest

typecheck:
	$(PYTHON) -m mypy src tests

worktree:
	BRANCH_NAME="$${BRANCH_NAME:-}" WORKTREE_PATH="$${WORKTREE_PATH:-}" ./scripts/create-worktree.sh
