"""Collect commit-pinned source files for service-owned artifact inspection."""

from __future__ import annotations

import hashlib
import subprocess
from collections.abc import Sequence
from pathlib import Path

from powdrr_contracts.models import (
    Artifact,
    OperationRequest,
    RepositorySnapshot,
    artifact_manifest_digest,
)


class RepositorySnapshotError(ValueError):
    """The requested branch and commit cannot be collected safely."""


def collect_artifact_state_snapshot(
    repo_root: str | Path,
    *,
    branch: str,
    sha: str,
    repository_id: str = "",
    request_id: str,
    areas: Sequence[str] | None = None,
    include_github: bool = True,
) -> OperationRequest:
    """Collect the selected artifact sources at a commit reachable from branch."""
    root = Path(repo_root).resolve()
    repository_id = repository_id or _repository_from_origin(root) or "local"
    commit = _git(root, "rev-parse", "--verify", f"{sha}^{{commit}}").strip()
    branch_commit = _git(root, "rev-parse", "--verify", f"{branch}^{{commit}}").strip()
    ancestor = subprocess.run(
        ["git", "-C", str(root), "merge-base", "--is-ancestor", commit, branch_commit],
        capture_output=True,
        check=False,
    )
    if ancestor.returncode:
        raise RepositorySnapshotError(
            f"{commit} is not reachable from branch {branch!r}"
        )

    available_areas = {
        "bootstrap",
        "entities",
        "relationships",
        "workflows",
        "plans",
        "changelogs",
        "pull_requests",
        "story_evidence",
    }
    selected_areas = set(areas) if areas else available_areas
    unknown_areas = selected_areas - available_areas
    if unknown_areas:
        raise RepositorySnapshotError(
            f"unknown artifact-state area(s): {', '.join(sorted(unknown_areas))}"
        )
    all_paths = _git(root, "ls-tree", "-r", "--name-only", commit).splitlines()
    paths = sorted(
        path for path in all_paths if _is_selected_source(path, selected_areas)
    )
    artifacts = tuple(
        Artifact(
            artifact_id=path,
            media_type="application/vnd.powdrr.source-file",
            data=_git_bytes(root, "show", f"{commit}:{path}"),
            scope="repository_snapshot",
            attributes={
                "git_mode": "100644",
                **_commit_metadata(root, commit, path),
            },
        )
        for path in paths
    )
    manifest_digest = artifact_manifest_digest(artifacts)
    tree_id = _git(root, "rev-parse", f"{commit}^{{tree}}").strip()
    snapshot = RepositorySnapshot(
        repository_id=repository_id,
        branch=branch,
        base_sha=None,
        head_sha=commit,
        manifest_digest=manifest_digest,
        worktree_digest=hashlib.sha256(tree_id.encode()).hexdigest(),
        source_artifact_refs=tuple(item.artifact_id for item in artifacts),
        includes_dirty_and_untracked=False,
    )
    return OperationRequest(
        request_id=request_id,
        operation_kind="inspect_artifacts",
        inputs={
            "areas": sorted(selected_areas),
            "include_github": include_github,
        },
        repository=snapshot,
        artifacts=artifacts,
        capabilities=({"name": "repository.pinned_file_snapshot", "version": "1"},),
    )


def _is_selected_source(path: str, areas: set[str]) -> bool:
    if path.startswith("docs/structrr/diffs/") and path.endswith(
        "/story-execution.json"
    ):
        return "story_evidence" in areas
    if path.startswith("docs/structrr/current/baseline-") and path.endswith(".yaml"):
        return bool(areas & {"bootstrap", "entities", "relationships"})
    if path.startswith("docs/changelogs/") and path.endswith(".yaml"):
        return bool(areas & {"changelogs", "entities", "relationships"})
    if path.startswith("docs/workflows/") and path.endswith((".yaml", ".yml")):
        return bool(areas & {"workflows", "plans"})
    if path.startswith("docs/plans/") and path.endswith(
        (".yaml", ".yml", ".md", ".json")
    ):
        return "plans" in areas
    return False


def _commit_metadata(root: Path, commit: str, path: str) -> dict[str, str]:
    result = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "log",
            "-1",
            "--format=%H%x1f%ct%x1f%s",
            commit,
            "--",
            path,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    parts = result.stdout.strip().split("\x1f", maxsplit=2)
    if result.returncode or len(parts) != 3:
        return {}
    return {
        "source_commit_sha": parts[0],
        "source_commit_timestamp": parts[1],
        "source_commit_subject": parts[2],
    }


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise RepositorySnapshotError(result.stderr.strip() or "git command failed")
    return result.stdout


def _git_bytes(root: Path, *args: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise RepositorySnapshotError(result.stderr.decode(errors="replace").strip())
    return result.stdout


def _repository_from_origin(root: Path) -> str | None:
    result = subprocess.run(
        ["git", "-C", str(root), "config", "--get", "remote.origin.url"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        return None
    remote = result.stdout.strip().removesuffix(".git")
    if ":" in remote and "@" in remote:
        remote = remote.split(":", maxsplit=1)[1]
    parts = remote.rstrip("/").split("/")
    return "/".join(parts[-2:]) if len(parts) >= 2 else None
