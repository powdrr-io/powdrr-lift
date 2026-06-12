#!/usr/bin/env bash

set -euo pipefail

branch_name="${1:-${BRANCH_NAME:-}}"
worktree_path="${2:-${WORKTREE_PATH:-}}"

if [[ -z "${branch_name}" ]]; then
  echo "usage: $0 <branch-name> [worktree-path]" >&2
  exit 1
fi

repo_root="$(git rev-parse --show-toplevel)"

if [[ -z "${worktree_path}" ]]; then
  worktree_path="${repo_root}/.worktrees/${branch_name}"
fi

mkdir -p "${repo_root}/.worktrees"
git -C "${repo_root}" worktree add "${worktree_path}" -b "${branch_name}" HEAD

echo "${worktree_path}"
