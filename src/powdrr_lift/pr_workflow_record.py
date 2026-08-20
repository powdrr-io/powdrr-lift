"""Generate the auditable workflow record committed with each agent PR."""

from __future__ import annotations

import re
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml

_PULL_REQUEST_URL = re.compile(r"/pull/(\d+)(?:\b|/|$)")


def pull_request_number(value: str) -> int | None:
    """Extract a GitHub pull-request number from command output."""
    match = _PULL_REQUEST_URL.search(value)
    return int(match.group(1)) if match is not None else None


def is_pull_request_create_command(command: Sequence[str]) -> bool:
    """Return whether a command invokes GitHub's pull-request creation."""
    normalized = list(command)
    for offset in range(max(0, len(normalized) - 2)):
        if normalized[offset : offset + 3] == ["gh", "pr", "create"]:
            return True
    return False


def record_pull_request_workflow(
    repo_root: str | Path,
    pull_request_number: int,
    *,
    branch: str,
    base_branch: str,
    title: str,
    workflow_name: str | None,
    workflow_path: str | None,
    steps: Sequence[Mapping[str, Any]],
    events: Sequence[Mapping[str, Any]],
    explanation: str,
) -> Path:
    """Write, commit, and push the structured record for an agent PR."""
    repo_root_path = Path(repo_root).resolve()
    relative_path = Path("docs") / "prs" / f"{pull_request_number}.yaml"
    record_path = repo_root_path / relative_path
    record = _record_data(
        pull_request_number,
        branch=branch,
        base_branch=base_branch,
        title=title,
        workflow_name=workflow_name,
        workflow_path=workflow_path,
        steps=steps,
        events=events,
        explanation=explanation,
    )
    if record_path.exists():
        previous = yaml.safe_load(record_path.read_text(encoding="utf-8"))
        if isinstance(previous, Mapping):
            previous_calls = previous.get("tool_calls")
            if isinstance(previous_calls, list):
                record["tool_calls"] = _unique_mappings(
                    [*previous_calls, *record["tool_calls"]]
                )
    rendered = yaml.safe_dump(record, sort_keys=False, allow_unicode=True)
    record_path.parent.mkdir(parents=True, exist_ok=True)
    if not record_path.exists() or record_path.read_text(encoding="utf-8") != rendered:
        record_path.write_text(rendered, encoding="utf-8")

    _run_git(repo_root_path, ["add", "--", str(relative_path)])
    staged_check = subprocess.run(
        ["git", "diff", "--cached", "--quiet", "--", str(relative_path)],
        cwd=repo_root_path,
        capture_output=True,
        text=True,
        check=False,
    )
    if staged_check.returncode != 0:
        _run_git(
            repo_root_path,
            ["commit", "-m", f"Add workflow record for PR {pull_request_number}"],
        )
        _run_git(repo_root_path, ["push", "origin", branch])
    return record_path


def _record_data(
    pull_request_number: int,
    *,
    branch: str,
    base_branch: str,
    title: str,
    workflow_name: str | None,
    workflow_path: str | None,
    steps: Sequence[Mapping[str, Any]],
    events: Sequence[Mapping[str, Any]],
    explanation: str,
) -> dict[str, Any]:
    return {
        "schema": "https://powdrr.io/schemas/pr-workflow-record-v1",
        "pull_request": pull_request_number,
        "title": title,
        "branch": branch,
        "base_branch": base_branch,
        "workflow": {
            "name": workflow_name,
            "path": workflow_path,
            "skills": _skills(workflow_name, events),
            "steps": [dict(step) for step in steps],
        },
        "tool_calls": _tool_calls(events),
        "explanation": explanation,
    }


def _tool_calls(events: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    for event in events:
        if event.get("kind") != "invoke_tool":
            continue
        parameters = event.get("parameters")
        call: dict[str, Any] = {
            "tool": event.get("tool"),
            "parameters": _tool_parameters(parameters),
        }
        rationale = event.get("decisions_and_context")
        if isinstance(rationale, str) and rationale.strip():
            call["why"] = rationale.strip()
        result = event.get("result")
        if isinstance(result, Mapping) and isinstance(result.get("returncode"), int):
            call["returncode"] = result["returncode"]
        calls.append(call)
    return calls


def _skills(
    workflow_name: str | None,
    events: Sequence[Mapping[str, Any]],
) -> list[str]:
    names = [workflow_name] if workflow_name else []
    names.extend(
        str(event["skill"])
        for event in events
        if event.get("kind") == "invoke_skill" and event.get("skill")
    )
    return list(dict.fromkeys(names))


def _unique_mappings(values: Sequence[object]) -> list[dict[str, Any]]:
    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, Mapping):
            continue
        item = dict(value)
        signature = repr(sorted(item.items()))
        if signature in seen:
            continue
        seen.add(signature)
        unique.append(item)
    return unique


def _tool_parameters(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    parameters: dict[str, Any] = {}
    for key in ("command", "cwd", "tool"):
        item = value.get(key)
        if isinstance(item, (str, list, tuple)):
            parameters[key] = list(item) if isinstance(item, (list, tuple)) else item
    return parameters


def _run_git(repo_root: Path, arguments: list[str]) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Git command failed ({' '.join(arguments)}): {result.stderr.strip()}"
        )
    return result
