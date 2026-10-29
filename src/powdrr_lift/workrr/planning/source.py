"""Compatibility imports for service-owned planning source semantics."""

from __future__ import annotations

from powdrr_agent.repository import LocalActionError, read_pinned_file
from powdrr_service.planning import source as _source
from powdrr_service.planning.source import *  # noqa: F403
from powdrr_service.planning.source import (
    PinnedSourceDocument,
    PlanningSourceError,
    pinned_source_document,
)

__all__ = [*_source.__all__, "read_pinned_source"]


def read_pinned_source(
    repository_root: str,
    commit_sha: str,
    path: str,
    *,
    repository_id: str | None = None,
) -> PinnedSourceDocument:
    """Compatibility adapter: capture pinned Git bytes locally, then parse."""
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
