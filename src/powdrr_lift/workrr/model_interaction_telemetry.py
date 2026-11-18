"""Privacy-conscious model attempt telemetry for durable workflow tasks."""

from __future__ import annotations

import json
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any


class TaskModelInteractionTelemetry:
    """Persist provider attempts and summarize their linkage to judge events."""

    def __init__(
        self,
        *,
        attempts_path: Path,
        events_path: Path,
        manifest: dict[str, Any],
        task_id: str,
        workflow_id: str,
        process_id: str,
        process_fingerprint: str,
    ) -> None:
        self._attempts_path = attempts_path
        self._events_path = events_path
        self._manifest = manifest
        self._task_id = task_id
        self._workflow_id = workflow_id
        self._process_id = process_id
        self._process_fingerprint = process_fingerprint
        self._lock = threading.Lock()

    def record_attempt(self, record: Mapping[str, Any]) -> None:
        """Write metadata only; prompt/response and free-form errors stay private."""
        safe_record = {
            key: value
            for key, value in record.items()
            if key not in {"error_message", "error_module"}
        }
        safe_record.update(
            {
                "task_id": self._task_id,
                "workflow_id": self._workflow_id,
                "process_id": self._process_id,
                "process_fingerprint": self._process_fingerprint,
            }
        )
        try:
            with self._lock, self._attempts_path.open("a", encoding="utf-8") as stream:
                stream.write(
                    json.dumps(safe_record, sort_keys=True, default=str) + "\n"
                )
        except OSError as error:
            telemetry = self._manifest["model_interaction_telemetry"]
            if isinstance(telemetry, dict):
                telemetry["status"] = "partial"
                telemetry.setdefault("errors", []).append(
                    f"persist provider attempt: {type(error).__name__}"
                )

    def refresh_summary(self) -> None:
        """Refresh manifest totals without treating missing routes as zero."""
        attempts = _read_jsonl_mappings(self._attempts_path)
        event_trace_ids = {
            str(trace_id)
            for event in _read_jsonl_mappings(self._events_path)
            if isinstance((data := event.get("data")), Mapping)
            if isinstance((trace_id := data.get("trace_id")), str)
        }
        interactions = {
            str(item["interaction_id"])
            for item in attempts
            if isinstance(item.get("interaction_id"), str)
        }
        by_caller = _summarize_by_caller(attempts)
        attempt_trace_ids: set[str] = set()
        for item in attempts:
            batch_trace_ids = item.get("batch_member_trace_ids")
            trace_ids = (
                batch_trace_ids
                if isinstance(batch_trace_ids, list)
                else [item.get("trace_id")]
            )
            attempt_trace_ids.update(
                trace_id for trace_id in trace_ids if isinstance(trace_id, str)
            )
        linked = attempt_trace_ids.intersection(event_trace_ids)
        unmatched = attempt_trace_ids.difference(event_trace_ids)
        prompt_tokens = _integer_values(attempts, "prompt_tokens")
        completion_tokens = _integer_values(attempts, "completion_tokens")
        costs = _numeric_values(attempts, "cost_amount")
        currencies = {
            value
            for item in attempts
            if isinstance((value := item.get("cost_currency")), str)
        }
        telemetry = self._manifest["model_interaction_telemetry"]
        if not isinstance(telemetry, dict):
            return
        telemetry.update(
            {
                "status": "partial",
                "logical_interactions": len(interactions),
                "logical_question_members": sum(
                    summary["logical_question_members"]
                    for summary in by_caller.values()
                ),
                "wire_attempts": len(attempts),
                "retry_attempts": sum(
                    item.get("outcome") == "retrying" for item in attempts
                ),
                "failed_attempts": sum(
                    item.get("outcome") == "failed" for item in attempts
                ),
                "by_caller_path": by_caller,
                "trace_linkage": {
                    "linked_interactions": len(linked),
                    "unmatched_interactions": len(unmatched),
                    "status": "observed" if not unmatched else "partial",
                },
                "tokens": {
                    "prompt": sum(prompt_tokens) if prompt_tokens else None,
                    "completion": (
                        sum(completion_tokens) if completion_tokens else None
                    ),
                    "status": (
                        "observed"
                        if prompt_tokens and completion_tokens
                        else "partial"
                        if prompt_tokens or completion_tokens
                        else "unknown"
                    ),
                },
                "cost": {
                    "amount": sum(costs) if costs and len(currencies) == 1 else None,
                    "currency": (
                        next(iter(currencies))
                        if costs and len(currencies) == 1
                        else None
                    ),
                    "status": (
                        "observed" if costs and len(currencies) == 1 else "unknown"
                    ),
                },
            }
        )


def _read_jsonl_mappings(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    records: list[dict[str, Any]] = []
    for line in lines:
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            records.append(value)
    return records


def _summarize_by_caller(
    attempts: list[dict[str, Any]],
) -> dict[str, dict[str, int]]:
    result: dict[str, dict[str, int]] = {}
    for item in attempts:
        caller_paths = item.get("caller_paths")
        callers = (
            list(dict.fromkeys(path for path in caller_paths if isinstance(path, str)))
            if isinstance(caller_paths, list)
            else [str(item.get("caller_path") or "unknown")]
        )
        if not callers:
            callers = ["unknown"]
        for caller in callers:
            summary = result.setdefault(
                caller,
                {
                    "logical_interactions": 0,
                    "logical_question_members": 0,
                    "wire_attempts": 0,
                    "batch_wire_attempts": 0,
                    "retries": 0,
                    "failures": 0,
                    "duration_ms": 0,
                },
            )
            is_batch = item.get("transport_kind") == "batch"
            summary["wire_attempts"] += int(not is_batch)
            summary["batch_wire_attempts"] += int(is_batch)
            summary["retries"] += int(item.get("outcome") == "retrying")
            summary["failures"] += int(item.get("outcome") == "failed")
            duration_ms = item.get("duration_ms")
            summary["duration_ms"] += (
                int(duration_ms)
                if isinstance(duration_ms, (int, float))
                and not isinstance(duration_ms, bool)
                else 0
            )
    for caller, summary in result.items():
        caller_attempts: list[dict[str, Any]] = []
        for item in attempts:
            caller_paths = item.get("caller_paths")
            paths = (
                caller_paths
                if isinstance(caller_paths, list)
                else [str(item.get("caller_path") or "unknown")]
            )
            if caller in paths:
                caller_attempts.append(item)
        summary["logical_interactions"] = len(
            {
                str(item["interaction_id"])
                for item in caller_attempts
                if isinstance(item.get("interaction_id"), str)
            }
        )
        summary["logical_question_members"] = sum(
            _question_members_for_caller(item, caller) for item in caller_attempts
        )
    return result


def _question_members_for_caller(item: Mapping[str, Any], caller: str) -> int:
    if item.get("transport_kind") != "batch":
        return int(item.get("outcome") in {"succeeded", "failed"})
    if item.get("outcome") != "succeeded":
        return 0
    caller_paths = item.get("caller_paths")
    if isinstance(caller_paths, list):
        return sum(path == caller for path in caller_paths)
    batch_members = item.get("batch_member_count")
    return (
        batch_members
        if isinstance(batch_members, int) and not isinstance(batch_members, bool)
        else 0
    )


def _integer_values(attempts: list[dict[str, Any]], field: str) -> list[int]:
    return [
        value
        for item in attempts
        if isinstance((value := item.get(field)), int) and not isinstance(value, bool)
    ]


def _numeric_values(attempts: list[dict[str, Any]], field: str) -> list[float]:
    return [
        float(value)
        for item in attempts
        if isinstance((value := item.get(field)), (int, float))
        and not isinstance(value, bool)
    ]
