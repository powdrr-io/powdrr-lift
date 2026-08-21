"""Structured records for LLM output and workflow-action failures."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

WORKFLOW_LLM_ERROR_LOG = "workflow-llm-errors.jsonl"


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
        "context": dict(context),
    }
    if llm_output is not None:
        record["llm_output"] = llm_output
    if attempted_action is not None:
        record["attempted_action"] = attempted_action
    if guidance:
        record["guidance"] = guidance

    path = repo_root / WORKFLOW_LLM_ERROR_LOG
    try:
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except OSError:
        return None
    return path
