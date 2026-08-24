"""Deterministic clustering for workflow LLM diagnostic records."""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from powdrr_lift.workflow_replay import (
    WorkflowReplayError,
    replay_bundle_from_error_record,
    save_workflow_replay_bundle,
)

_VOLATILE_VALUE = re.compile(
    r"(?:\b\d{2,}\b|[0-9a-f]{8,}|/[^\s'\"]+|\"[^\"]{20,}\")",
    flags=re.IGNORECASE,
)


class WorkflowErrorAnalysisError(ValueError):
    """Raised when workflow diagnostic records cannot be analyzed."""


@dataclass(frozen=True, slots=True)
class WorkflowErrorCluster:
    fingerprint: str
    definition: str | None
    skill_or_task: str | None
    step: str | None
    action: str | None
    error_type: str | None
    error_summary: str | None
    phase: str | None
    count: int
    blocked_count: int
    rank: int
    record_ids: tuple[str, ...]
    representative: Mapping[str, Any]

    def to_data(self) -> dict[str, Any]:
        return {
            "fingerprint": self.fingerprint,
            "definition": self.definition,
            "skill_or_task": self.skill_or_task,
            "step": self.step,
            "action": self.action,
            "error_type": self.error_type,
            "error_summary": self.error_summary,
            "phase": self.phase,
            "count": self.count,
            "blocked_count": self.blocked_count,
            "rank": self.rank,
            "record_ids": list(self.record_ids),
        }


def load_workflow_error_records(paths: Sequence[Path]) -> list[dict[str, Any]]:
    """Load structured workflow error JSONL records with source provenance."""
    records: list[dict[str, Any]] = []
    for path in paths:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise WorkflowErrorAnalysisError(f"Could not read {path}: {exc}") from exc
        for line_number, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise WorkflowErrorAnalysisError(
                    f"{path}:{line_number} is invalid JSON: {exc.msg}"
                ) from exc
            if not isinstance(record, Mapping):
                raise WorkflowErrorAnalysisError(
                    f"{path}:{line_number} must contain an object."
                )
            item = dict(record)
            item["_source_log"] = str(path)
            item["_source_line"] = line_number
            records.append(item)
    return records


def cluster_workflow_errors(
    records: Sequence[Mapping[str, Any]],
) -> tuple[WorkflowErrorCluster, ...]:
    """Group diagnostic records by a stable, value-insensitive failure identity."""
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    identities: dict[str, dict[str, str | None]] = {}
    for record in records:
        identity = _error_identity(record)
        fingerprint = _fingerprint(identity)
        grouped[fingerprint].append(record)
        identities[fingerprint] = identity
    clusters: list[WorkflowErrorCluster] = []
    for fingerprint, grouped_records in grouped.items():
        identity = identities[fingerprint]
        record_ids = tuple(
            str(record["record_id"])
            for record in grouped_records
            if isinstance(record.get("record_id"), str)
        )
        blocked_count = sum(_is_blocking(record) for record in grouped_records)
        rank = len(grouped_records) * 10 + blocked_count * 20
        clusters.append(
            WorkflowErrorCluster(
                fingerprint=fingerprint,
                definition=identity["definition"],
                skill_or_task=identity["skill_or_task"],
                step=identity["step"],
                action=identity["action"],
                error_type=identity["error_type"],
                error_summary=identity["error_summary"],
                phase=identity["phase"],
                count=len(grouped_records),
                blocked_count=blocked_count,
                rank=rank,
                record_ids=record_ids,
                representative=grouped_records[0],
            )
        )
    return tuple(
        sorted(
            clusters,
            key=lambda cluster: (-cluster.rank, -cluster.count, cluster.fingerprint),
        )
    )


def promote_replay_candidates(
    clusters: Sequence[WorkflowErrorCluster],
    *,
    repo_root: Path,
    output_dir: Path,
    limit: int | None = None,
) -> tuple[dict[str, Any], ...]:
    """Write one portable draft replay bundle for each eligible representative."""
    output_dir.mkdir(parents=True, exist_ok=True)
    promoted: list[dict[str, Any]] = []
    for cluster in clusters[:limit]:
        try:
            bundle = replay_bundle_from_error_record(
                cluster.representative, repo_root=repo_root
            )
        except WorkflowReplayError as exc:
            promoted.append(
                {
                    "fingerprint": cluster.fingerprint,
                    "record_id": cluster.representative.get("record_id"),
                    "status": "not_eligible",
                    "reason": str(exc),
                }
            )
            continue
        name = f"{cluster.rank:04d}-{cluster.fingerprint}.yaml"
        path = save_workflow_replay_bundle(output_dir / name, bundle)
        promoted.append(
            {
                "fingerprint": cluster.fingerprint,
                "record_id": cluster.representative.get("record_id"),
                "status": "promoted",
                "path": str(path),
            }
        )
    return tuple(promoted)


def workflow_error_analysis_data(
    clusters: Sequence[WorkflowErrorCluster],
    *,
    record_count: int,
    candidates: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "record_count": record_count,
        "cluster_count": len(clusters),
        "clusters": [cluster.to_data() for cluster in clusters],
        "replay_candidates": [dict(candidate) for candidate in candidates],
    }


def _error_identity(record: Mapping[str, Any]) -> dict[str, str | None]:
    context = record.get("context")
    context_mapping = context if isinstance(context, Mapping) else {}
    skill = context_mapping.get("skill")
    skill_mapping = skill if isinstance(skill, Mapping) else {}
    task = context_mapping.get("task")
    task_mapping = task if isinstance(task, Mapping) else {}
    action = _action_name(record.get("attempted_action", record.get("llm_output")))
    step_id = skill_mapping.get("step_id", task_mapping.get("id"))
    step_index = skill_mapping.get("step_index", task_mapping.get("index"))
    step = (
        str(step_id)
        if isinstance(step_id, str)
        else (f"step-{step_index}" if isinstance(step_index, int) else None)
    )
    definition = skill_mapping.get("path")
    if not isinstance(definition, str):
        definition = task_mapping.get("template_path")
    return {
        "definition": definition if isinstance(definition, str) else None,
        "skill_or_task": _first_text(skill_mapping.get("name"), task_mapping.get("id")),
        "step": step,
        "action": action,
        "error_type": _optional_text(record.get("error_type")),
        "error_summary": _normalize_error(_optional_text(record.get("error"))),
        "phase": _optional_text(record.get("phase")),
    }


def _action_name(value: Any) -> str | None:
    if not isinstance(value, Mapping):
        return None
    action = value.get("action", value.get("kind"))
    return action if isinstance(action, str) else None


def _normalize_error(value: str | None) -> str | None:
    if value is None:
        return None
    first_line = value.splitlines()[0]
    return _VOLATILE_VALUE.sub("<value>", " ".join(first_line.split()))


def _fingerprint(identity: Mapping[str, str | None]) -> str:
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


def _is_blocking(record: Mapping[str, Any]) -> bool:
    phase = _optional_text(record.get("phase")) or ""
    error = (_optional_text(record.get("error")) or "").casefold()
    return phase in {"action_validation_or_execution", "llm_output_parse"} or any(
        phrase in error for phrase in ("stopped", "blocked", "exhausted", "failed")
    )


def _first_text(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, str) and value:
            return value
    return None


def _optional_text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None
