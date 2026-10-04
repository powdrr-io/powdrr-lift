"""Durable records for normative uncertainty decisions."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "uncertainty-decisions-v1"


def records_for_scenario(
    clause: Mapping[str, Any],
    source_text: str,
    scenario: Mapping[str, Any],
    *,
    phase: str,
    repository_location: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Bind recorded assumptions to their exact instruction source span."""
    clause_id = clause.get("clause_id")
    span = clause.get("source_span")
    if not isinstance(clause_id, str) or not clause_id.strip():
        raise ValueError("uncertainty decision has no source clause ID")
    if not isinstance(span, Mapping):
        raise ValueError("uncertainty decision has no source span")
    start, end = span.get("start"), span.get("end")
    if (
        not isinstance(start, int)
        or isinstance(start, bool)
        or not isinstance(end, int)
        or isinstance(end, bool)
        or start < 0
        or end <= start
        or end > len(source_text)
    ):
        raise ValueError("uncertainty decision source span is invalid")
    quote = source_text[start:end]
    if not quote.strip():
        raise ValueError("uncertainty decision source quote is empty")

    assumptions = scenario.get("assumptions", [])
    if not isinstance(assumptions, list):
        raise ValueError("uncertainty decision assumptions are malformed")
    scenario_context = {
        key: scenario[key]
        for key in ("scenario_id", "subject", "given", "when", "then")
        if isinstance(scenario.get(key), str)
    }
    records: list[dict[str, Any]] = []
    for item in assumptions:
        if not isinstance(item, Mapping):
            raise ValueError("uncertainty decision assumption is malformed")
        dimension = item.get("dimension")
        resolution = item.get("resolution")
        rationale = item.get("rationale")
        basis = item.get("basis")
        basis_reference = item.get("basis_reference")
        confidence = item.get("confidence")
        if not isinstance(dimension, str) or not dimension.strip():
            raise ValueError("uncertainty decision dimension is missing")
        if not isinstance(resolution, str) or not resolution.strip():
            raise ValueError("uncertainty decision resolution is missing")
        if not isinstance(rationale, str) or not rationale.strip():
            raise ValueError("uncertainty decision rationale is missing")
        if not isinstance(basis, str) or not basis.strip():
            raise ValueError("uncertainty decision basis is missing")
        if not isinstance(basis_reference, str) or not basis_reference.strip():
            raise ValueError("uncertainty decision basis reference is missing")
        if not isinstance(confidence, str) or not confidence.strip():
            raise ValueError("uncertainty decision fields are incomplete")
        record: dict[str, Any] = {
            "id": f"uncertainty:{clause_id}:{dimension}",
            "originating_phase": phase,
            "last_updated_phase": phase,
            "source_ref": clause_id,
            "source_fingerprint": clause.get("fingerprint"),
            "source_quote": quote,
            "source_location": {
                "kind": "request_text",
                "start_offset": start,
                "end_offset": end,
            },
            "uncertainty": (
                f"The source leaves {dimension.replace('_', ' ')} undefined "
                "for this behavior."
            ),
            "dimension": dimension,
            "selected_default": resolution,
            "rationale": rationale,
            "basis": basis,
            "basis_reference": basis_reference,
            "confidence": confidence,
            "scenario": scenario_context,
            "revision_history": [],
        }
        if repository_location is not None:
            record["repository_location"] = dict(repository_location)
        records.append(record)
    return records


def update_decision_artifact(
    path: Path,
    incoming_records: Sequence[Mapping[str, Any]],
    *,
    uncertainty_policy: str,
    instruction_ledger_fingerprint: str | None = None,
) -> Path:
    """Atomically upsert decisions and retain every superseded decision."""
    existing_records: dict[str, dict[str, Any]] = {}
    if path.is_file():
        try:
            current = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError(
                f"uncertainty decision artifact is unreadable: {error}"
            ) from error
        if (
            not isinstance(current, Mapping)
            or current.get("schema_version") != SCHEMA_VERSION
        ):
            raise ValueError("uncertainty decision artifact has an unsupported schema")
        raw_records = current.get("decisions")
        if not isinstance(raw_records, list):
            raise ValueError("uncertainty decision artifact has no decisions list")
        for item in raw_records:
            if not isinstance(item, Mapping) or not isinstance(item.get("id"), str):
                raise ValueError(
                    "uncertainty decision artifact contains a malformed record"
                )
            existing_records[str(item["id"])] = dict(item)

    for raw in incoming_records:
        if not isinstance(raw, Mapping) or not isinstance(raw.get("id"), str):
            raise ValueError("uncertainty decision record has no stable ID")
        record = dict(raw)
        decision_id = str(record["id"])
        previous = existing_records.get(decision_id)
        if previous is not None:
            history = previous.get("revision_history", [])
            if not isinstance(history, list):
                raise ValueError("uncertainty decision revision history is malformed")
            changed_fields = (
                "selected_default",
                "rationale",
                "basis",
                "basis_reference",
                "confidence",
            )
            changed = any(
                previous.get(key) != record.get(key) for key in changed_fields
            )
            if changed:
                snapshot = {
                    key: previous.get(key)
                    for key in (
                        "selected_default",
                        "rationale",
                        "basis",
                        "basis_reference",
                        "confidence",
                        "last_updated_phase",
                    )
                }
                snapshot["superseded_in_phase"] = record.get("last_updated_phase")
                record["revision_history"] = [*history, snapshot]
            else:
                record["revision_history"] = history
                record["last_updated_phase"] = previous.get(
                    "last_updated_phase", record.get("last_updated_phase")
                )
            record["originating_phase"] = previous.get(
                "originating_phase", record.get("originating_phase")
            )
        existing_records[decision_id] = record

    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "uncertainty_policy": uncertainty_policy,
        "decisions": [existing_records[key] for key in sorted(existing_records)],
    }
    if instruction_ledger_fingerprint is not None:
        payload["instruction_ledger_fingerprint"] = instruction_ledger_fingerprint
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, delete=False
        ) as stream:
            json.dump(payload, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
            temporary_path = stream.name
        os.replace(temporary_path, path)
    except (OSError, TypeError, ValueError):
        if temporary_path is not None:
            Path(temporary_path).unlink(missing_ok=True)
        raise
    return path


__all__ = ["SCHEMA_VERSION", "records_for_scenario", "update_decision_artifact"]
