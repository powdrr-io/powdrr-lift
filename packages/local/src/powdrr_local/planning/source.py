"""Compose local pinned-file reads with service-owned planning semantics."""

from __future__ import annotations

from powdrr_agent.repository import LocalActionError, read_pinned_file
from powdrr_service.planning.source import (
    PinnedSourceDocument,
    PlanningSourceError,
    pinned_source_document,
)


def read_pinned_source(
    repository_root: str,
    commit_sha: str,
    path: str,
    *,
    repository_id: str | None = None,
) -> PinnedSourceDocument:
    """Read a pinned Git blob locally and construct the service source record."""
    if (
        not path
        or path.startswith("/")
        or "\\" in path
        or any(part in {"", ".", ".."} for part in path.split("/"))
    ):
        raise PlanningSourceError("source path must be a normalized repository path")
    try:
        artifact = read_pinned_file(repository_root, commit_sha, path)
    except LocalActionError as error:
        raise PlanningSourceError(str(error)) from error
    blob_sha = artifact.attributes.get("git_blob_sha")
    if not isinstance(blob_sha, str):
        raise PlanningSourceError("agent did not return the pinned Git blob ID")
    repository = repository_id or repository_root
    return pinned_source_document(
        repository,
        commit_sha,
        artifact.artifact_id,
        blob_sha,
        artifact.data,
    )
