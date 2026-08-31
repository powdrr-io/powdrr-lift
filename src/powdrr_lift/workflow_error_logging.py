"""Structured records for LLM output and workflow-action failures."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

WORKFLOW_LLM_ERROR_LOG = "workflow-llm-errors.jsonl"
WORKFLOW_OBSERVER_LOG = "workflow-observer-events.jsonl"


def _bounded_log_value(value: Any, *, depth: int = 0) -> Any:
    """Keep diagnostic logging from becoming a second unbounded transcript."""
    if depth > 5:
        return "<nested value omitted>"
    if isinstance(value, str):
        return value if len(value) <= 4_000 else value[:3_960] + "… <truncated>"
    if isinstance(value, Mapping):
        return {
            str(key): _bounded_log_value(item, depth=depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [
            _bounded_log_value(item, depth=depth + 1)
            for item in (value[-32:] if len(value) > 32 else value)
        ]
    return value


def record_workflow_llm_error(
    repo_root: Path,
    *,
    execution_mode: str,
    phase: str,
    error: BaseException,
    context: Mapping[str, Any],
    llm_output: Any = None,
    attempted_action: Any = None,
    guidance: str | None = None,
) -> Path | None:
    """Append one analysis-ready LLM failure record.

    JSON Lines keeps each failure independently readable and appendable when
    multiple workflow agents share a repository. ``context`` should identify
    the skill/task and active step; callers retain the complete prompt and
    execution history separately.
    """
    record: dict[str, Any] = {
        "schema_version": 1,
        "record_id": str(uuid4()),
        "recorded_at": datetime.now(UTC).isoformat(),
        "execution_mode": execution_mode,
        "phase": phase,
        "error_type": type(error).__name__,
        "error": str(error),
        "context": _bounded_log_value(context),
    }
    if llm_output is not None:
        record["llm_output"] = _bounded_log_value(llm_output)
    if attempted_action is not None:
        record["attempted_action"] = _bounded_log_value(attempted_action)
    if guidance:
        record["guidance"] = guidance

    path = repo_root / WORKFLOW_LLM_ERROR_LOG
    try:
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except OSError:
        return None
    return path


def record_workflow_observer_event(
    repo_root: Path,
    *,
    execution_mode: str,
    trigger: str,
    fingerprint: str,
    context: Mapping[str, Any],
    packet: Mapping[str, Any] | None = None,
    decision: Mapping[str, Any] | None = None,
    error: BaseException | None = None,
) -> Path | None:
    """Append one repository-root shadow-observer event."""
    record: dict[str, Any] = {
        "schema_version": 1,
        "record_id": str(uuid4()),
        "recorded_at": datetime.now(UTC).isoformat(),
        "execution_mode": execution_mode,
        "phase": "observer_shadow",
        "trigger": trigger,
        "fingerprint": fingerprint,
        "context": _bounded_log_value(context),
    }
    if packet is not None:
        record["observer_packet"] = _bounded_log_value(packet)
    if decision is not None:
        record["observer_decision"] = _bounded_log_value(decision)
    if error is not None:
        record["error_type"] = type(error).__name__
        record["error"] = str(error)
    path = repo_root / WORKFLOW_OBSERVER_LOG
    try:
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except OSError:
        return None
    return path
