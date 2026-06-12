.PHONY: format format-check lint test typecheck worktree

format:
	uv run ruff format .

format-check:
	uv run ruff format --check .

lint:
	uv run ruff check .

test:
	uv run pytest

typecheck:
	uv run mypy src tests

worktree:
	BRANCH_NAME="$${BRANCH_NAME:-}" WORKTREE_PATH="$${WORKTREE_PATH:-}" ./scripts/create-worktree.sh
