"""Safe intrinsic tools for the small set of Git and GitHub operations agents need."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from powdrr_lift.builtin_tool_help import builtin_tool_help
from powdrr_lift.errors import PowdrrExecutionError

GIT_TOOL = "git"
GH_TOOL = "gh"

_REVIEW_THREADS_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $after: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      id number title body state baseRefName headRefName headRefOid
      reviewThreads(first: 100, after: $after) {
        nodes {
          id isResolved isOutdated path line originalLine startLine
          originalStartLine diffSide startDiffSide
          comments(first: 100) {
            nodes {
              id databaseId body createdAt updatedAt path line originalLine
              outdated url author { login } replyTo { id } commit { oid }
            }
            pageInfo { hasNextPage endCursor }
          }
        }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
""".strip()
_REVIEW_THREAD_COMMENTS_QUERY = """
query($threadId: ID!, $after: String) {
  node(id: $threadId) {
    ... on PullRequestReviewThread {
      comments(first: 100, after: $after) {
        nodes {
          id databaseId body createdAt updatedAt path line originalLine
          outdated url author { login } replyTo { id } commit { oid }
        }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
""".strip()
_REVIEW_REPLY_MUTATION = """
mutation($threadId: ID!, $body: String!) {
  addPullRequestReviewThreadReply(input: {
    pullRequestReviewThreadId: $threadId,
    body: $body
  }) {
    comment { id body url }
  }
}
""".strip()
_REVIEW_RESOLVE_MUTATION = """
mutation($threadId: ID!) {
  resolveReviewThread(input: {threadId: $threadId}) {
    thread { id isResolved }
  }
}
""".strip()


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
        if parameters.get("operation") == "verify_pr_scope":
            from powdrr_lift.clean_pr_publisher import verify_publication_scope

            return verify_publication_scope(parameters, worktree_root=worktree_root)
        command = _git_command(parameters)
        executable = "git"
    elif tool == GH_TOOL:
        effective_parameters = dict(parameters)
        operation = effective_parameters.get("operation")
        if operation == "pr_create":
            _reject_pr_identity_overrides(effective_parameters, ("base", "head"))
        elif operation == "pr_edit":
            _reject_pr_identity_overrides(effective_parameters, ("pr_reference",))
            # gh accepts a branch name as the PR selector, so the current branch
            # is sufficient and avoids a separate number lookup race.
            effective_parameters["pr_reference"] = _current_branch(worktree_root)
        command = _gh_command(effective_parameters)
        executable = "gh"
    else:
        raise PowdrrExecutionError(
            f"Unsupported intrinsic repository tool: {tool!r}.",
            error_code="unsupported_tool",
        )
    if tool == GH_TOOL and parameters.get("operation") == "pr_create":
        existing = _existing_pull_request(worktree_root)
        if existing is not None:
            return {
                "tool": tool,
                "command": [executable, *command],
                "returncode": 0,
                "stdout": existing,
                "stderr": "",
                "no_op": True,
            }
    if tool == GH_TOOL and parameters.get("operation") == "pr_publish_clean":
        from powdrr_lift.clean_pr_publisher import publish_clean_pull_request

        pull_request_url = publish_clean_pull_request(
            parameters, worktree_root=worktree_root
        )
        return {
            "tool": tool,
            "command": ["gh", "pr", "publish-clean"],
            "returncode": 0,
            "stdout": pull_request_url,
            "stderr": "",
            "pull_request_url": pull_request_url,
            "no_op": False,
        }
    if tool == GH_TOOL and parameters.get("operation") == "pr_review_threads":
        return _execute_review_threads(command, worktree_root=worktree_root)
    result = subprocess.run(
        [executable, *command],
        cwd=worktree_root,
        capture_output=True,
        text=True,
        check=False,
    )
    normalized_returncode = result.returncode
    normalized_stdout = result.stdout
    normalized_stderr = result.stderr
    if (
        tool == GH_TOOL
        and parameters.get("operation") in {"pr_review_reply", "pr_review_resolve"}
        and result.returncode == 0
    ):
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            payload = None
        data = payload.get("data") if isinstance(payload, Mapping) else None
        operation = parameters.get("operation")
        mutation_result = (
            data.get(
                "addPullRequestReviewThreadReply"
                if operation == "pr_review_reply"
                else "resolveReviewThread"
            )
            if isinstance(data, Mapping)
            else None
        )
        if operation == "pr_review_reply":
            mutation_succeeded = (
                isinstance(mutation_result, Mapping)
                and isinstance(mutation_result.get("comment"), Mapping)
                and isinstance(mutation_result["comment"].get("id"), str)
            )
        else:
            resolved_thread = (
                mutation_result.get("thread")
                if isinstance(mutation_result, Mapping)
                else None
            )
            mutation_succeeded = (
                isinstance(resolved_thread, Mapping)
                and resolved_thread.get("isResolved") is True
            )
        if (
            not isinstance(payload, Mapping)
            or payload.get("errors")
            or not mutation_succeeded
        ):
            normalized_returncode = 1
            normalized_stderr = json.dumps(
                payload.get("errors", "GitHub returned an invalid GraphQL response")
                if isinstance(payload, Mapping)
                else "GitHub returned an invalid GraphQL response"
            )
    no_op = False
    if tool == GIT_TOOL and parameters.get("operation") == "commit":
        normalized_returncode, normalized_stdout, normalized_stderr, no_op = (
            _normalize_empty_commit_result(
                result,
                worktree_root=worktree_root,
            )
        )
    return {
        "tool": tool,
        "command": [executable, *command],
        "returncode": normalized_returncode,
        "stdout": normalized_stdout,
        "stderr": normalized_stderr,
        "no_op": no_op,
    }


def _execute_review_threads(
    command: list[str], *, worktree_root: Path
) -> dict[str, Any]:
    """Fetch every review-thread page and preserve PR identity with the result."""
    threads: list[dict[str, Any]] = []
    pull_request: Mapping[str, Any] | None = None
    cursor: str | None = None
    while True:
        page_command = [*command]
        if cursor is not None:
            page_command.extend(["-f", f"after={cursor}"])
        result = subprocess.run(
            ["gh", *page_command],
            cwd=worktree_root,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            return {
                "tool": GH_TOOL,
                "command": ["gh", *page_command],
                "returncode": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "no_op": False,
            }
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            payload = None
        if not isinstance(payload, Mapping) or payload.get("errors"):
            detail = (
                json.dumps(payload.get("errors"))
                if isinstance(payload, Mapping)
                else "GitHub returned invalid review-thread JSON"
            )
            return {
                "tool": GH_TOOL,
                "command": ["gh", *page_command],
                "returncode": 1,
                "stdout": result.stdout,
                "stderr": detail,
                "no_op": False,
            }
        data = payload.get("data")
        repository = data.get("repository") if isinstance(data, Mapping) else None
        page_pr = (
            repository.get("pullRequest") if isinstance(repository, Mapping) else None
        )
        connection = (
            page_pr.get("reviewThreads") if isinstance(page_pr, Mapping) else None
        )
        if not isinstance(page_pr, Mapping) or not isinstance(connection, Mapping):
            return {
                "tool": GH_TOOL,
                "command": ["gh", *page_command],
                "returncode": 1,
                "stdout": result.stdout,
                "stderr": "GitHub returned no pull-request review-thread data",
                "no_op": False,
            }
        if pull_request is None:
            pull_request = {
                key: page_pr.get(key)
                for key in (
                    "id",
                    "number",
                    "title",
                    "body",
                    "state",
                    "baseRefName",
                    "headRefName",
                    "headRefOid",
                )
            }
        nodes = connection.get("nodes")
        page_info = connection.get("pageInfo")
        if not isinstance(nodes, list):
            return {
                "tool": GH_TOOL,
                "command": ["gh", *page_command],
                "returncode": 1,
                "stdout": result.stdout,
                "stderr": "GitHub returned an invalid review-thread list",
                "no_op": False,
            }
        if not isinstance(page_info, Mapping) or not isinstance(
            page_info.get("hasNextPage"), bool
        ):
            return {
                "tool": GH_TOOL,
                "command": ["gh", *page_command],
                "returncode": 1,
                "stdout": result.stdout,
                "stderr": "GitHub returned invalid review-thread pagination data",
                "no_op": False,
            }
        threads.extend(node for node in nodes if isinstance(node, dict))
        if page_info.get("hasNextPage") is False:
            break
        next_cursor = page_info.get("endCursor")
        if not isinstance(next_cursor, str) or not next_cursor or next_cursor == cursor:
            return {
                "tool": GH_TOOL,
                "command": ["gh", *page_command],
                "returncode": 1,
                "stdout": result.stdout,
                "stderr": "GitHub review-thread pagination returned no new cursor",
                "no_op": False,
            }
        cursor = next_cursor
    for thread in threads:
        comment_connection = thread.get("comments")
        if not isinstance(comment_connection, dict):
            return {
                "tool": GH_TOOL,
                "command": ["gh", *command],
                "returncode": 1,
                "stdout": "",
                "stderr": "GitHub returned invalid review-thread comments",
                "no_op": False,
            }
        if not isinstance(comment_connection.get("nodes"), list):
            return {
                "tool": GH_TOOL,
                "command": ["gh", *command],
                "returncode": 1,
                "stdout": "",
                "stderr": "GitHub returned an invalid review-comment list",
                "no_op": False,
            }
        page_info = comment_connection.get("pageInfo")
        if not isinstance(page_info, Mapping) or not isinstance(
            page_info.get("hasNextPage"), bool
        ):
            return {
                "tool": GH_TOOL,
                "command": ["gh", *command],
                "returncode": 1,
                "stdout": "",
                "stderr": "GitHub returned invalid review-comment pagination data",
                "no_op": False,
            }
        cursor = page_info.get("endCursor")
        while page_info.get("hasNextPage") is True:
            if not isinstance(cursor, str) or not cursor:
                return {
                    "tool": GH_TOOL,
                    "command": ["gh", *command],
                    "returncode": 1,
                    "stdout": "",
                    "stderr": "GitHub review-comment pagination returned no cursor",
                    "no_op": False,
                }
            comments_command = [
                "api",
                "graphql",
                "-f",
                f"query={_REVIEW_THREAD_COMMENTS_QUERY}",
                "-f",
                f"threadId={thread.get('id', '')}",
                "-f",
                f"after={cursor}",
            ]
            comment_result = subprocess.run(
                ["gh", *comments_command],
                cwd=worktree_root,
                capture_output=True,
                text=True,
                check=False,
            )
            if comment_result.returncode != 0:
                return {
                    "tool": GH_TOOL,
                    "command": ["gh", *comments_command],
                    "returncode": comment_result.returncode,
                    "stdout": comment_result.stdout,
                    "stderr": comment_result.stderr,
                    "no_op": False,
                }
            try:
                comment_payload = json.loads(comment_result.stdout)
            except json.JSONDecodeError:
                comment_payload = None
            comment_data = (
                comment_payload.get("data")
                if isinstance(comment_payload, Mapping)
                else None
            )
            node = (
                comment_data.get("node") if isinstance(comment_data, Mapping) else None
            )
            page_connection = (
                node.get("comments") if isinstance(node, Mapping) else None
            )
            if (
                not isinstance(page_connection, Mapping)
                or not isinstance(comment_payload, Mapping)
                or comment_payload.get("errors")
                or not isinstance(page_connection.get("nodes"), list)
                or not isinstance(page_connection.get("pageInfo"), Mapping)
                or not isinstance(page_connection["pageInfo"].get("hasNextPage"), bool)
            ):
                errors = (
                    json.dumps(comment_payload.get("errors"))
                    if isinstance(comment_payload, Mapping)
                    and comment_payload.get("errors")
                    else "GitHub returned invalid review-comment pagination data"
                )
                return {
                    "tool": GH_TOOL,
                    "command": ["gh", *comments_command],
                    "returncode": 1,
                    "stdout": comment_result.stdout,
                    "stderr": errors,
                    "no_op": False,
                }
            previous_cursor = cursor
            comment_connection["nodes"].extend(
                item for item in page_connection["nodes"] if isinstance(item, dict)
            )
            page_info = page_connection["pageInfo"]
            cursor = page_info.get("endCursor")
            comment_connection["pageInfo"] = page_info
            if page_info.get("hasNextPage") is True and (
                not isinstance(cursor, str) or not cursor or cursor == previous_cursor
            ):
                return {
                    "tool": GH_TOOL,
                    "command": ["gh", *comments_command],
                    "returncode": 1,
                    "stdout": comment_result.stdout,
                    "stderr": "GitHub review-comment pagination returned no new cursor",
                    "no_op": False,
                }
    return {
        "tool": GH_TOOL,
        "command": ["gh", *command],
        "returncode": 0,
        "stdout": json.dumps({"pull_request": pull_request, "review_threads": threads}),
        "stderr": "",
        "no_op": False,
    }


def _normalize_empty_commit_result(
    result: subprocess.CompletedProcess[str],
    *,
    worktree_root: Path,
) -> tuple[int, str, str, bool]:
    """Make a clean worktree's empty commit a successful idempotent no-op."""
    if result.returncode == 0:
        return result.returncode, result.stdout, result.stderr, False
    output = f"{result.stdout}\n{result.stderr}".casefold()
    if not any(
        marker in output
        for marker in (
            "nothing to commit",
            "nothing added to commit",
            "no changes added to commit",
        )
    ):
        return result.returncode, result.stdout, result.stderr, False
    status = subprocess.run(
        ["git", "status", "--short"],
        cwd=worktree_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if status.returncode != 0 or status.stdout.strip():
        return result.returncode, result.stdout, result.stderr, False
    return (
        0,
        f"{result.stdout}No changes to commit; existing HEAD retained.\n",
        result.stderr,
        True,
    )


def _existing_pull_request(worktree_root: Path) -> str | None:
    """Return the current branch's PR URL when one already exists."""
    try:
        branch = _current_branch(worktree_root)
    except PowdrrExecutionError:
        return None
    result = subprocess.run(
        ["gh", "pr", "view", branch, "--json", "url,state"],
        cwd=worktree_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    url = payload.get("url") if isinstance(payload, Mapping) else None
    state = payload.get("state") if isinstance(payload, Mapping) else None
    if isinstance(url, str) and state in {"OPEN", "DRAFT"}:
        return url.strip() + "\n"
    return None


def _reject_pr_identity_overrides(
    parameters: Mapping[str, Any], fields: tuple[str, ...]
) -> None:
    provided = tuple(field for field in fields if field in parameters)
    if provided:
        raise PowdrrExecutionError(
            "Pull-request identity is runtime-owned; do not provide: "
            + ", ".join(provided)
        )


def _current_branch(worktree_root: Path) -> str:
    result = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=worktree_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise PowdrrExecutionError(
            "Cannot determine the current branch for the pull-request operation."
        )
    return result.stdout.strip()


def intrinsic_command(parameters: Mapping[str, Any], *, tool: str) -> list[str]:
    """Return the normalized command used for declared-invocation matching."""
    return _git_command(parameters) if tool == GIT_TOOL else _gh_command(parameters)


def _git_command(parameters: Mapping[str, Any]) -> list[str]:
    operation = parameters.get("operation")
    if operation == "status":
        command = ["status", "--short"]
    elif operation == "remote":
        command = ["remote"]
    elif operation == "branch_current":
        command = ["branch", "--show-current"]
    elif operation == "head":
        command = ["rev-parse", "HEAD"]
    elif operation == "default_branch":
        command = ["symbolic-ref", "--short", "refs/remotes/origin/HEAD"]
    elif operation == "show_ref":
        branch = _required_text(parameters.get("branch"), "branch")
        command = ["show-ref", "--verify", f"refs/heads/{branch}"]
    elif operation == "verify_pr_scope":
        _relative_paths(parameters.get("paths"), field="paths")
        command = ["status", "--short"]
    elif operation == "switch":
        branch = _branch_name(parameters.get("branch"))
        command = ["switch", branch]
    elif operation == "switch_create":
        branch = _branch_name(parameters.get("branch"))
        command = ["switch", "-c", branch]
    elif operation == "commit":
        command = ["commit", "-m", _required_text(parameters.get("message"), "message")]
    elif operation == "push":
        branch = _branch_name(parameters.get("branch"))
        command = ["push", "--set-upstream", "origin", branch]
    elif operation == "add":
        paths = _relative_paths(parameters.get("paths"), field="paths")
        command = ["add", *paths]
    elif operation in {"move", "rename"}:
        source = _relative_path(parameters.get("source"), field="source")
        destination = _relative_path(parameters.get("destination"), field="destination")
        command = ["mv", source, destination]
    else:
        raise PowdrrExecutionError(
            "git intrinsic tool requires a supported structured operation."
        )
    if command[0] not in {
        "status",
        "remote",
        "branch",
        "rev-parse",
        "symbolic-ref",
        "show-ref",
        "switch",
        "commit",
        "push",
        "add",
        "mv",
    }:
        raise PowdrrExecutionError(
            "git intrinsic tool only supports bounded repository operations."
        )
    if command[0] in {"add", "mv"}:
        for item in command[1:]:
            _relative_path(item, field="path")
    return command


def _gh_command(parameters: Mapping[str, Any]) -> list[str]:
    operation = parameters.get("operation")
    reference = parameters.get("pr_reference", parameters.get("number"))
    if operation in {"pr_view", "pr_diff", "pr_checks"}:
        subcommand = operation.removeprefix("pr_").replace("_", "-")
        if operation == "pr_view" and reference is None:
            command = ["pr", "view"]
        else:
            reference = _required_text(reference, "pr_reference")
            command = ["pr", subcommand, reference]
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
                raise PowdrrExecutionError(
                    "gh pr_view json_fields must be a non-empty array of strings."
                )
            command.extend(["--json", ",".join(field.strip() for field in fields)])
            if parameters.get("jq") == ".url":
                command.extend(["--jq", ".url"])
    elif operation in {"pr_create", "pr_publish_clean"}:
        command = [
            "pr",
            "create",
            "--draft",
            "--title",
            _required_text(parameters.get("title"), "title"),
            "--body",
            _required_text(parameters.get("body"), "body"),
        ]
        if parameters.get("base") is not None:
            command.extend(["--base", _branch_name(parameters.get("base"))])
        head = parameters.get(
            "publication_branch" if operation == "pr_publish_clean" else "head"
        )
        if head is not None:
            command.extend(["--head", _branch_name(head)])
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
            raise PowdrrExecutionError(
                "gh pr_review_comment line must be a positive integer."
            )
        side = parameters.get("side", "RIGHT")
        if side not in {"LEFT", "RIGHT"}:
            raise PowdrrExecutionError(
                "gh pr_review_comment side must be LEFT or RIGHT."
            )
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
    elif operation == "pr_review_threads":
        owner, name = _repository_owner_name(parameters.get("repository"))
        number = _pull_request_number(reference)
        command = [
            "api",
            "graphql",
            "-f",
            f"query={_REVIEW_THREADS_QUERY}",
            "-F",
            f"owner={owner}",
            "-F",
            f"name={name}",
            "-F",
            f"number={number}",
        ]
    elif operation == "pr_review_reply":
        thread_id = _required_text(parameters.get("thread_id"), "thread_id")
        body = _required_text(parameters.get("body"), "body")
        command = [
            "api",
            "graphql",
            "-f",
            f"query={_REVIEW_REPLY_MUTATION}",
            "-f",
            f"threadId={thread_id}",
            "-f",
            f"body={body}",
        ]
    elif operation == "pr_review_resolve":
        thread_id = _required_text(parameters.get("thread_id"), "thread_id")
        command = [
            "api",
            "graphql",
            "-f",
            f"query={_REVIEW_RESOLVE_MUTATION}",
            "-f",
            f"threadId={thread_id}",
        ]
    else:
        raise PowdrrExecutionError(
            "gh intrinsic tool requires structured operation pr_view, pr_diff, "
            "pr_checks, pr_create, pr_edit, pr_comments, pr_review_comment, "
            "pr_review_threads, pr_review_reply, or pr_review_resolve."
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
        raise PowdrrExecutionError(
            "gh intrinsic tool only supports GitHub pull-request create and "
            "inspect operations."
        )
    return command


def _repository_owner_name(value: object) -> tuple[str, str]:
    repository = _required_text(value, "repository")
    parts = repository.split("/")
    if len(parts) != 2 or any(not part for part in parts):
        raise PowdrrExecutionError("gh intrinsic repository must use owner/name form.")
    return parts[0], parts[1]


def _pull_request_number(value: object) -> int:
    if isinstance(value, bool):
        raise PowdrrExecutionError("gh intrinsic pr_reference must be a PR number.")
    if isinstance(value, int):
        number = value
    elif isinstance(value, str) and value.isdecimal():
        number = int(value)
    else:
        raise PowdrrExecutionError("gh intrinsic pr_reference must be a PR number.")
    if number < 1:
        raise PowdrrExecutionError("gh intrinsic pr_reference must be a PR number.")
    return number


def _relative_paths(value: object, *, field: str) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise PowdrrExecutionError(f"git intrinsic {field} must be an array of paths.")
    return [_relative_path(item, field=field) for item in value]


def _relative_path(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PowdrrExecutionError(
            f"git intrinsic {field} must be a non-empty relative path."
        )
    path = value.strip()
    if Path(path).is_absolute() or ".." in Path(path).parts:
        raise PowdrrExecutionError(
            f"git intrinsic {field} must stay inside the worktree."
        )
    return path


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PowdrrExecutionError(f"gh intrinsic {field} must be a non-empty string.")
    return value


def _branch_name(value: object) -> str:
    branch = _required_text(value, "branch")
    if any(character in branch for character in "..~^:?*[\\ "):
        raise PowdrrExecutionError(
            "git intrinsic branch must be a safe branch name without shell syntax."
        )
    return branch
