"""Safe intrinsic tools for the small set of Git and GitHub operations agents need."""

from __future__ import annotations

import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from powdrr_lift.builtin_tool_help import builtin_tool_help

GIT_TOOL = "git"
GH_TOOL = "gh"


def execute_intrinsic_git_gh_tool(
    tool: str,
    parameters: Mapping[str, Any],
    *,
    worktree_root: Path,
) -> dict[str, Any]:
    """Execute one allow-listed Git/GitHub command in the active worktree."""
    if parameters.get("help") is True:
        return builtin_tool_help(tool)
    if tool == GIT_TOOL:
        command = _git_command(parameters)
        executable = "git"
    elif tool == GH_TOOL:
        command = _gh_command(parameters)
        executable = "gh"
    else:
        raise RuntimeError(f"Unsupported intrinsic repository tool: {tool!r}.")
    result = subprocess.run(
        [executable, *command],
        cwd=worktree_root,
        capture_output=True,
        text=True,
        check=False,
    )
    return {
        "tool": tool,
        "command": [executable, *command],
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


def intrinsic_command(parameters: Mapping[str, Any], *, tool: str) -> list[str]:
    """Return the normalized command used for declared-invocation matching."""
    return _git_command(parameters) if tool == GIT_TOOL else _gh_command(parameters)


def _git_command(parameters: Mapping[str, Any]) -> list[str]:
    operation = parameters.get("operation")
    if operation == "status":
        command = ["status", "--short"]
    elif operation == "add":
        paths = _relative_paths(parameters.get("paths"), field="paths")
        command = ["add", *paths]
    elif operation in {"move", "rename"}:
        source = _relative_path(parameters.get("source"), field="source")
        destination = _relative_path(parameters.get("destination"), field="destination")
        command = ["mv", source, destination]
    else:
        raise RuntimeError(
            "git intrinsic tool requires structured operation status, add, or move."
        )
    if command[0] not in {"status", "add", "mv"}:
        raise RuntimeError("git intrinsic tool only supports status, add, and move.")
    if command[0] in {"add", "mv"}:
        for item in command[1:]:
            _relative_path(item, field="path")
    return command


def _gh_command(parameters: Mapping[str, Any]) -> list[str]:
    operation = parameters.get("operation")
    reference = parameters.get("pr_reference", parameters.get("number"))
    if operation in {"pr_view", "pr_diff", "pr_checks"}:
        reference = _required_text(reference, "pr_reference")
        command = ["pr", operation.removeprefix("pr_").replace("_", "-"), reference]
        if operation == "pr_view" and parameters.get("json_fields") is not None:
            fields = parameters.get("json_fields")
            if (
                not isinstance(fields, Sequence)
                or isinstance(fields, (str, bytes, bytearray))
                or not fields
                or any(
                    not isinstance(field, str) or not field.strip() for field in fields
                )
            ):
                raise RuntimeError(
                    "gh pr_view json_fields must be a non-empty array of strings."
                )
            command.extend(["--json", ",".join(field.strip() for field in fields)])
    elif operation == "pr_create":
        command = [
            "pr",
            "create",
            "--title",
            _required_text(parameters.get("title"), "title"),
            "--body",
            _required_text(parameters.get("body"), "body"),
        ]
        if parameters.get("draft", False):
            command.insert(2, "--draft")
    elif operation == "pr_edit":
        command = [
            "pr",
            "edit",
            _required_text(reference, "pr_reference"),
            "--title",
            _required_text(parameters.get("title"), "title"),
            "--body",
            _required_text(parameters.get("body"), "body"),
        ]
    elif operation == "pr_comments":
        command = [
            "pr",
            "view",
            _required_text(reference, "pr_reference"),
            "--comments",
        ]
    elif operation == "pr_review_comment":
        repository = _required_text(parameters.get("repository"), "repository")
        body = _required_text(parameters.get("body"), "body")
        commit_id = _required_text(parameters.get("commit_id"), "commit_id")
        path = _required_text(parameters.get("path"), "path")
        line = parameters.get("line")
        if isinstance(line, bool) or not isinstance(line, int) or line < 1:
            raise RuntimeError("gh pr_review_comment line must be a positive integer.")
        side = parameters.get("side", "RIGHT")
        if side not in {"LEFT", "RIGHT"}:
            raise RuntimeError("gh pr_review_comment side must be LEFT or RIGHT.")
        command = [
            "api",
            "repos/"
            f"{repository}/pulls/{_required_text(reference, 'pr_reference')}/comments",
            "--method",
            "POST",
            "-f",
            f"body={body}",
            "-f",
            f"commit_id={commit_id}",
            "-f",
            f"path={path}",
            "-F",
            f"line={line}",
            "-f",
            f"side={side}",
        ]
    else:
        raise RuntimeError(
            "gh intrinsic tool requires structured operation pr_view, pr_diff, "
            "pr_checks, pr_create, pr_edit, pr_comments, or pr_review_comment."
        )
    if (
        len(command) < 2
        or command[0] not in {"pr", "api"}
        or (
            command[0] == "pr"
            and command[1]
            not in {
                "view",
                "diff",
                "checks",
                "create",
                "edit",
            }
        )
    ):
        raise RuntimeError(
            "gh intrinsic tool only supports GitHub pull-request create and "
            "inspect operations."
        )
    return command


def _relative_paths(value: object, *, field: str) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise RuntimeError(f"git intrinsic {field} must be an array of paths.")
    return [_relative_path(item, field=field) for item in value]


def _relative_path(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"git intrinsic {field} must be a non-empty relative path.")
    path = value.strip()
    if Path(path).is_absolute() or ".." in Path(path).parts:
        raise RuntimeError(f"git intrinsic {field} must stay inside the worktree.")
    return path


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"gh intrinsic {field} must be a non-empty string.")
    return value
