"""Agent-side collection of commit-pinned workflow source artifacts."""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

from powdrr_contracts.models import (
    Artifact,
    OperationRequest,
    RepositorySnapshot,
    artifact_manifest_digest,
)


class RepositorySnapshotError(ValueError):
    """The requested branch and commit cannot be collected safely."""


def collect_workflow_snapshot(
    repo_root: str | Path,
    *,
    branch: str,
    sha: str,
    repository_id: str = "",
    request_id: str,
) -> OperationRequest:
    """Read workflow YAML at a branch-reachable SHA and serialize file bytes."""
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

    paths = sorted(
        path
        for path in _git(root, "ls-tree", "-r", "--name-only", commit).splitlines()
        if path.startswith("docs/workflows/") and path.endswith((".yaml", ".yml"))
    )
    artifacts = tuple(
        Artifact(
            artifact_id=path,
            media_type="application/vnd.powdrr.source-file",
            data=_git_bytes(root, "show", f"{commit}:{path}"),
            scope="repository_snapshot",
        )
        for path in paths
    )
    manifest_digest = artifact_manifest_digest(artifacts)
    tree_id = _git(root, "rev-parse", f"{commit}^{{tree}}").strip()
    worktree_digest = hashlib.sha256(tree_id.encode()).hexdigest()
    snapshot = RepositorySnapshot(
        repository_id=repository_id,
        branch=branch,
        base_sha=None,
        head_sha=commit,
        manifest_digest=manifest_digest,
        worktree_digest=worktree_digest,
        source_artifact_refs=tuple(item.artifact_id for item in artifacts),
        includes_dirty_and_untracked=False,
    )
    return OperationRequest(
        request_id=request_id,
        operation_kind="inspect_artifacts",
        inputs={"areas": ["workflows"]},
        repository=snapshot,
        artifacts=artifacts,
        capabilities=({"name": "repository.pinned_file_snapshot", "version": "1"},),
    )


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
