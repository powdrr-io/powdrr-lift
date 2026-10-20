"""Deterministically resolve a feature name to its repository context."""

from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path
from typing import Any

from powdrr_lift.fuzzy_match import execute_fuzzy_match

_SPECIFICATION_FILES = (
    "system-specification.yaml",
    "architecture-specification.yaml",
    "implementation-specification.yaml",
    "proposed-pr-specification.yaml",
)


def resolve_feature_context(feature_query: str, repo_root: Path) -> dict[str, Any]:
    """Select the unique best normalized proposal/current feature match.

    Matching and tie handling are deterministic. A workflow path is derived from
    the selected canonical name; workflow candidates never compete with features.
    """
    query = feature_query.strip()
    if not query:
        raise ValueError("feature query must not be empty")

    candidates: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"name": "", "score": 0.0, "proposal": None, "current": None}
    )
    for category, search_root, maxdepth in (
        ("proposal", "docs/proposals", 2),
        ("current", "docs/current", 2),
    ):
        result = execute_fuzzy_match(
            [
                "fuzzy-match",
                search_root,
                "-name",
                query,
                "-type",
                "d",
                "-maxdepth",
                str(maxdepth),
                "-print",
            ],
            worktree_root=repo_root,
        )
        for match in result["matches"]:
            name = str(match["name"])
            key = _normalize(name)
            candidate = candidates[key]
            candidate["score"] = max(float(candidate["score"]), float(match["score"]))
            candidate[category] = str(match["path"])
            if category == "proposal" or not candidate["name"]:
                candidate["name"] = name

    normalized_query = _normalize(query)
    exact = [item for key, item in candidates.items() if key == normalized_query]
    if exact:
        best = exact
    elif candidates:
        top_score = max(float(item["score"]) for item in candidates.values())
        best = [item for item in candidates.values() if item["score"] == top_score]
    else:
        best = []

    if not best:
        return _unselected("not_found", [])
    if len(best) != 1:
        return _unselected("ambiguous", sorted(str(item["name"]) for item in best))

    selected = best[0]
    feature_name = str(selected["name"])
    proposal_dir = selected["proposal"]
    current_dir = selected["current"]
    specification_documents: list[str] = []
    missing_documents: list[str] = []
    for directory in (proposal_dir, current_dir):
        if not isinstance(directory, str):
            continue
        for filename in _SPECIFICATION_FILES:
            path = f"{directory}/{filename}"
            if (repo_root / path).is_file():
                specification_documents.append(path)
            else:
                missing_documents.append(path)

    workflow_dir = repo_root / "docs" / "workflows" / feature_name
    workflow_documents = (
        sorted(
            path.relative_to(repo_root).as_posix()
            for path in workflow_dir.rglob("*")
            if path.is_file() and not any(part.startswith(".") for part in path.parts)
        )
        if workflow_dir.is_dir()
        else []
    )
    return {
        "status": "selected",
        "feature_name": feature_name,
        "proposal_directory": proposal_dir,
        "current_directory": current_dir,
        "specification_documents": sorted(set(specification_documents)),
        "workflow_documents": workflow_documents,
        "missing_documents": sorted(set(missing_documents)),
        "candidate_names": [feature_name],
    }


def _normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold())


def _unselected(status: str, candidate_names: list[str]) -> dict[str, Any]:
    return {
        "status": status,
        "feature_name": None,
        "proposal_directory": None,
        "current_directory": None,
        "specification_documents": [],
        "workflow_documents": [],
        "missing_documents": [],
        "candidate_names": candidate_names,
    }
