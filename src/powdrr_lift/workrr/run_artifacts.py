"""Stable metadata and failure artifacts for feature endpoint runs."""

from __future__ import annotations

import importlib.metadata
import json
import os
import subprocess
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any


class RunFailureStage(StrEnum):
    INITIALIZATION = "initialization"
    PLANNING = "planning"
    IMPLEMENTATION = "implementation"
    VALIDATION = "validation"
    REVIEW = "review"
    PUBLICATION = "publication"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class RunFailure:
    stage: RunFailureStage
    error_type: str
    message: str
    clause_id: str | None = None
    obligation_id: str | None = None
    contract_id: str | None = None
    model_response_path: str | None = None
    validation_command: tuple[str, ...] = ()
    artifact_paths: tuple[str, ...] = ()

    def to_data(self) -> dict[str, Any]:
        return {
            "schema_version": "powdrr-run-failure-v1",
            "stage": self.stage.value,
            "error_type": self.error_type,
            "message": self.message,
            "clause_id": self.clause_id,
            "obligation_id": self.obligation_id,
            "contract_id": self.contract_id,
            "model_response_path": self.model_response_path,
            "validation_command": list(self.validation_command),
            "artifact_paths": list(self.artifact_paths),
        }


def collect_run_metadata(
    *, task_id: str, repo_root: Path, output_root: Path
) -> dict[str, Any]:
    """Collect reproducibility metadata without making the run depend on Git."""
    try:
        distribution = importlib.metadata.distribution("powdrr-lift")
        powdrr_version = distribution.version
        direct_url = distribution.read_text("direct_url.json")
    except importlib.metadata.PackageNotFoundError:
        powdrr_version = "editable"
        direct_url = None
    return {
        "schema_version": "powdrr-run-metadata-v1",
        "task_id": task_id,
        "powdrr_version": powdrr_version,
        "powdrr_install_spec": os.environ.get("POWDRR_INSTALL_SPEC"),
        "powdrr_revision": os.environ.get("POWDRR_REVISION"),
        "powdrr_direct_url": direct_url,
        "opencode_version": _command_version("opencode", repo_root),
        "repo_root": str(repo_root),
        "output_root": str(output_root),
    }


def write_json_artifact(output_root: Path, name: str, value: Any) -> Path:
    path = output_root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return path


def _command_version(command: str, cwd: Path) -> str | None:
    try:
        result = subprocess.run(
            [command, "--version"],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    version = (result.stdout or result.stderr).strip()
    return version or None


__all__ = [
    "RunFailure",
    "RunFailureStage",
    "collect_run_metadata",
    "write_json_artifact",
]
