from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Sequence
from typing import Any

from powdrr_agent.execution.github_actions import (
    GitHubActionsClient,
    execute_pinned_local_command,
)

HEAD = "6c85b1e5c334a113b3db274b2c1b7a8f350b9707"


def test_observation_fetches_annotations_even_when_runner_never_started() -> None:
    calls: list[list[str]] = []

    def run(
        arguments: Sequence[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        call = list(arguments)
        calls.append(call)
        endpoint = call[-1]
        payload: Any
        if endpoint == "repos/example/project/pulls/270":
            payload = {"head": {"sha": HEAD}}
        elif endpoint == "repos/example/project/pulls/270/files?per_page=100":
            payload = [
                [
                    {"filename": ".github/workflows/ci.yml"},
                    {"filename": "src/behavior.py"},
                ]
            ]
        elif "actions/runs?" in endpoint:
            payload = [
                {
                    "total_count": 1,
                    "workflow_runs": [
                        {
                            "id": 42107708170,
                            "run_attempt": 2,
                            "head_sha": HEAD,
                        }
                    ],
                }
            ]
        elif endpoint.endswith("/attempts/2/jobs?per_page=100"):
            payload = [
                {
                    "total_count": 1,
                    "jobs": [
                        {
                            "id": 127527360223,
                            "check_run_url": "https://api.github.com/repos/example/project/check-runs/127527360223",
                            "status": "completed",
                            "conclusion": "failure",
                            "runner_id": 0,
                            "steps": [],
                        }
                    ],
                }
            ]
        elif endpoint.endswith("/check-runs/127527360223/annotations?per_page=100"):
            payload = [[{"message": "The configuration 'macos-14' is not supported"}]]
        else:
            raise AssertionError(f"unexpected gh endpoint: {endpoint}")
        return subprocess.CompletedProcess(call, 0, json.dumps(payload), "")

    client = GitHubActionsClient("example/project", run=run)
    snapshot = client.observe_pull_request(270)

    assert snapshot.pull_request["head"]["sha"] == HEAD
    assert snapshot.changed_paths == (
        ".github/workflows/ci.yml",
        "src/behavior.py",
    )
    assert len(snapshot.runs) == 1
    assert snapshot.jobs_by_run["42107708170"][0]["steps"] == []
    assert snapshot.annotations_by_check_run["127527360223"][0]["message"].startswith(
        "The configuration"
    )
    assert any("annotations?per_page=100" in item[-1] for item in calls)


def test_pagination_merges_object_collection_fields() -> None:
    pages = [[{"id": 1}], [{"id": 2}]]

    client = GitHubActionsClient(
        "example/project",
        run=lambda arguments, **_kwargs: subprocess.CompletedProcess(
            arguments, 0, json.dumps(pages), ""
        ),
    )

    result = client._get("repos/example/project/actions/runs", paginated=True)

    assert result == [{"id": 1}, {"id": 2}]


def test_collection_pagination_merges_multiple_api_pages() -> None:
    pages = [
        {"total_count": 2, "workflow_runs": [{"id": 1}]},
        {"total_count": 2, "workflow_runs": [{"id": 2}]},
    ]
    client = GitHubActionsClient(
        "example/project",
        run=lambda arguments, **_kwargs: subprocess.CompletedProcess(
            arguments, 0, json.dumps(pages), ""
        ),
    )

    result = client._get(
        "repos/example/project/actions/runs",
        paginated=True,
        collection_key="workflow_runs",
    )

    assert result["workflow_runs"] == [{"id": 1}, {"id": 2}]


def test_issue_comment_lookup_is_bounded_and_most_recent_first() -> None:
    calls: list[list[str]] = []

    def run(
        arguments: Sequence[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        calls.append(list(arguments))
        return subprocess.CompletedProcess(arguments, 0, "[]", "")

    client = GitHubActionsClient("example/project", run=run)
    assert client.list_issue_comments(107) == ()

    invocation = calls[0]
    assert "--paginate" not in invocation
    assert invocation[-1].endswith("?per_page=20&sort=created&direction=desc")


def test_local_reproduction_uses_a_disposable_worktree_at_exact_commit(
    tmp_path: Any,
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    subprocess.run(["git", "init", str(repository)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.name", "Powdrr Test"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.email", "test@example.test"],
        check=True,
    )
    (repository / "tracked.txt").write_text("pinned baseline\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repository), "add", "tracked.txt"], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "commit", "-m", "baseline"],
        check=True,
        capture_output=True,
    )
    commit = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    result = execute_pinned_local_command(
        repository,
        commit,
        (
            sys.executable,
            "-c",
            "from pathlib import Path; print(Path('tracked.txt').read_text().strip())",
        ),
    )

    assert result.status == "completed"
    assert result.exit_code == 0
    assert result.stdout.strip() == "pinned baseline"
    assert result.commit_sha == commit
    assert (
        not (repository / "tracked.txt")
        .read_text(encoding="utf-8")
        .endswith("changed\n")
    )
    assert (
        subprocess.run(
            ["git", "-C", str(repository), "worktree", "list", "--porcelain"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.count("worktree ")
        == 1
    )
