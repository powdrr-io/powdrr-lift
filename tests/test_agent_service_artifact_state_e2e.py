from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from powdrr_agent.artifact_state import (
    RepositorySnapshotError,
    collect_workflow_snapshot,
)
from powdrr_local import JsonServiceBridge, LocalPowdrrService

from powdrr_lift.cli import main as cli_main


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _repository(tmp_path: Path, *, with_workflow: bool = True) -> tuple[Path, str]:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.name", "Powdrr tests")
    _git(root, "config", "user.email", "tests@example.invalid")
    if with_workflow:
        workflow = root / "docs/workflows/PR-17-task-1.yaml"
        workflow.parent.mkdir(parents=True)
        workflow.write_text(
            "workflow_id: PR-17\n"
            "task_id: PR-17-task-1\n"
            "status: succeeded\n"
            "inputs:\n"
            "  proposed_pr_context:\n"
            "    plan_id: plan-17\n",
            encoding="utf-8",
        )
    _git(root, "add", "--all")
    _git(root, "commit", "--allow-empty", "-m", "fixture")
    return root, _git(root, "rev-parse", "HEAD")


def test_agent_json_bridge_and_service_inspect_pinned_workflows(tmp_path: Path) -> None:
    root, sha = _repository(tmp_path)
    request = collect_workflow_snapshot(
        root,
        branch="main",
        sha=sha,
        repository_id="owner/repo",
        request_id="request-17",
    )

    update = JsonServiceBridge(LocalPowdrrService()).start(request)

    assert update.status == "succeeded"
    assert update.operation_id == "request-17"
    assert len(update.output_artifacts) == 1
    projection = json.loads(update.output_artifacts[0].data)
    assert projection["repository"] == "owner/repo"
    assert projection["branch"] == "main"
    assert projection["sha"] == sha
    assert projection["summary"] == {"published_workflow_count": 1, "workflow_count": 1}
    assert projection["workflows"][0]["id"] == "PR-17"
    assert projection["workflows"][0]["tasks"] == [
        {
            "id": "PR-17-task-1",
            "path": "docs/workflows/PR-17-task-1.yaml",
            "status": "succeeded",
        }
    ]


def test_artifact_state_workflows_cli_uses_the_agent_service_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root, sha = _repository(tmp_path)

    exit_code = cli_main(
        [
            "artifact-state",
            "--branch",
            "main",
            "--sha",
            sha,
            "--repo-root",
            str(root),
            "--repository",
            "owner/repo",
            "--workflows",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    result = json.loads(captured.out)
    assert result["sha"] == sha
    assert result["workflows"][0]["id"] == "PR-17"
    assert captured.err == ""


def test_empty_workflow_snapshot_is_successful_empty_state(tmp_path: Path) -> None:
    root, sha = _repository(tmp_path, with_workflow=False)
    request = collect_workflow_snapshot(
        root, branch="main", sha=sha, repository_id="owner/empty", request_id="empty-1"
    )

    update = JsonServiceBridge(LocalPowdrrService()).start(request)

    assert update.status == "succeeded"
    projection = json.loads(update.output_artifacts[0].data)
    assert projection["workflows"] == []
    assert projection["summary"] == {"published_workflow_count": 0, "workflow_count": 0}


def test_service_uses_collected_sha_after_branch_advances(tmp_path: Path) -> None:
    root, pinned_sha = _repository(tmp_path)
    request = collect_workflow_snapshot(
        root,
        branch="main",
        sha=pinned_sha,
        repository_id="owner/repo",
        request_id="pinned-1",
    )
    workflow = root / "docs/workflows/PR-17-task-1.yaml"
    workflow.write_text(
        workflow.read_text(encoding="utf-8").replace("succeeded", "failed"),
        encoding="utf-8",
    )
    _git(root, "add", "--all")
    _git(root, "commit", "-m", "advance workflow state")

    update = JsonServiceBridge(LocalPowdrrService()).start(request)
    projection = json.loads(update.output_artifacts[0].data)

    assert projection["sha"] == pinned_sha
    assert projection["workflows"][0]["tasks"][0]["status"] == "succeeded"


def test_agent_rejects_sha_not_reachable_from_requested_branch(tmp_path: Path) -> None:
    root, sha = _repository(tmp_path)
    _git(root, "checkout", "--orphan", "other")
    _git(root, "rm", "-rf", ".")
    _git(root, "commit", "--allow-empty", "-m", "unrelated")

    with pytest.raises(RepositorySnapshotError, match="not reachable"):
        collect_workflow_snapshot(
            root,
            branch="other",
            sha=sha,
            repository_id="owner/repo",
            request_id="invalid-1",
        )


def test_service_rejects_unregistered_operation_at_bridge(tmp_path: Path) -> None:
    root, sha = _repository(tmp_path)
    request = collect_workflow_snapshot(
        root, branch="main", sha=sha, repository_id="owner/repo", request_id="bad-kind"
    )
    from dataclasses import replace

    with pytest.raises(ValueError, match="unregistered operation kind"):
        JsonServiceBridge(LocalPowdrrService()).start(
            replace(request, operation_kind="run_python")
        )


def test_service_rejects_snapshot_manifest_that_does_not_match_payload(
    tmp_path: Path,
) -> None:
    from dataclasses import replace

    root, sha = _repository(tmp_path)
    request = collect_workflow_snapshot(
        root, branch="main", sha=sha, repository_id="owner/repo", request_id="tampered"
    )
    assert request.repository is not None
    tampered = replace(
        request,
        repository=replace(request.repository, manifest_digest="0" * 64),
    )

    update = JsonServiceBridge(LocalPowdrrService()).start(tampered)

    assert update.status == "failed"
    assert update.failure is not None
    assert update.failure.category == "artifact_inspection_failed"
    assert "manifest digest" in update.failure.message
