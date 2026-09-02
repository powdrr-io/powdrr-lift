"""Structured, single-pass repository state inspection."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any


def repository_state(repo_root: Path) -> dict[str, Any]:
    """Return branch and file state using one porcelain Git scan."""
    result = subprocess.run(
        ["git", "status", "--porcelain=v1", "-z", "--branch"],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    )
    records = result.stdout.split("\0")
    branch = ""
    upstream: str | None = None
    ahead = 0
    behind = 0
    files: list[dict[str, Any]] = []
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if not record:
            continue
        if record.startswith("## "):
            branch, upstream, ahead, behind = _parse_branch_record(record[3:])
            continue
        status = record[:2]
        path = record[3:] if len(record) >= 3 else ""
        if status[0] in {"R", "C"} and index < len(records):
            path = records[index]
            index += 1
        files.append(
            {
                "path": path,
                "index_status": status[0],
                "worktree_status": status[1],
                "staged": status[0] != " " and status != "??",
                "unstaged": status[1] != " ",
                "untracked": status == "??",
                "conflicted": status[0] == "U" or status[1] == "U",
            }
        )
    return {
        "root": str(repo_root.resolve()),
        "branch": branch,
        "upstream": upstream,
        "ahead": ahead,
        "behind": behind,
        "clean": not files,
        "files": files,
    }


def render_repository_state(repo_root: Path) -> str:
    return json.dumps(repository_state(repo_root), indent=2, ensure_ascii=False) + "\n"


def _parse_branch_record(value: str) -> tuple[str, str | None, int, int]:
    branch_part, separator, tracking_part = value.partition("...")
    if not separator:
        return branch_part, None, 0, 0
    upstream_match = re.match(r"([^ ]+)(?: \[(.*)\])?$", tracking_part)
    if upstream_match is None:
        return branch_part, tracking_part, 0, 0
    upstream = upstream_match.group(1)
    ahead = 0
    behind = 0
    for item in (upstream_match.group(2) or "").split(", "):
        count_match = re.match(r"(ahead|behind) (\d+)$", item)
        if count_match is None:
            continue
        if count_match.group(1) == "ahead":
            ahead = int(count_match.group(2))
        else:
            behind = int(count_match.group(2))
    return branch_part, upstream, ahead, behind
