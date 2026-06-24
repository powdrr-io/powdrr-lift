from __future__ import annotations

from pathlib import Path

SPECIFICATION_SCHEMA_URL = "https://powdrr.io/schemas/specification-v1"
SPECIFICATIONS_ROOT = Path("docs") / "specs"

ARCHITECTURE_SPECIFICATION_FILENAME = "architecture-specification.yaml"
SYSTEM_SPECIFICATION_FILENAME = "system-specification.yaml"
IMPLEMENTATION_SPECIFICATION_FILENAME = "implementation-specification.yaml"
PROPOSED_PR_SPECIFICATION_FILENAME = "proposed-pr-specification.yaml"
SYSTEM_MAP_SPECIFICATION_FILENAME = "system-map-specification.yaml"
FEATURE_PR_SPECIFICATION_FILENAME = "feature-pr-specification.yaml"


def normalize_work_item_name(work_item_name: str) -> str:
    normalized_work_item_name = work_item_name.strip()
    if not normalized_work_item_name:
        raise ValueError("work_item_name must not be empty.")

    if normalized_work_item_name.startswith(".") or "/" in normalized_work_item_name:
        raise ValueError(
            "work_item_name must be a simple directory name without path separators."
        )

    return normalized_work_item_name


def work_item_specification_root(
    repo_root: str | Path,
    work_item_name: str,
) -> Path:
    return (
        Path(repo_root) / SPECIFICATIONS_ROOT / normalize_work_item_name(work_item_name)
    )


def architecture_specification_path(
    repo_root: str | Path,
    work_item_name: str,
) -> Path:
    return work_item_specification_root(repo_root, work_item_name) / (
        ARCHITECTURE_SPECIFICATION_FILENAME
    )


def system_specification_path(
    repo_root: str | Path,
    work_item_name: str,
) -> Path:
    return work_item_specification_root(repo_root, work_item_name) / (
        SYSTEM_SPECIFICATION_FILENAME
    )


def implementation_specification_path(
    repo_root: str | Path,
    work_item_name: str,
) -> Path:
    return work_item_specification_root(repo_root, work_item_name) / (
        IMPLEMENTATION_SPECIFICATION_FILENAME
    )


def proposed_pr_specification_path(
    repo_root: str | Path,
    work_item_name: str,
) -> Path:
    return work_item_specification_root(repo_root, work_item_name) / (
        PROPOSED_PR_SPECIFICATION_FILENAME
    )


def system_map_specification_path(
    repo_root: str | Path,
    work_item_name: str,
) -> Path:
    return work_item_specification_root(repo_root, work_item_name) / (
        SYSTEM_MAP_SPECIFICATION_FILENAME
    )


def feature_pr_specification_path(
    repo_root: str | Path,
    work_item_name: str,
) -> Path:
    return work_item_specification_root(repo_root, work_item_name) / (
        FEATURE_PR_SPECIFICATION_FILENAME
    )


def is_specification_path(path: str) -> bool:
    normalized_path = path.replace("\\", "/")
    return normalized_path.startswith("docs/specs/") and normalized_path.endswith(
        ".yaml"
    )
