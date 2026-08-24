"""Portable, no-tool replay bundles for workflow LLM decisions."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml

from powdrr_lift.core import load_skill
from powdrr_lift.workflow_error_logging import WORKFLOW_LLM_ERROR_LOG

WORKFLOW_REPLAY_BUNDLE_SCHEMA_VERSION = 1
WORKFLOW_REPLAY_PROMPT_BUILDER_VERSION = 1


class WorkflowReplayError(ValueError):
    """Raised when a replay bundle cannot be loaded or evaluated."""


def build_workflow_replay_state(
    *,
    transcript: Sequence[Mapping[str, str]],
    execution_events: Sequence[Mapping[str, Any]],
    execution_context: Sequence[str],
    handoff_records: Mapping[str, Mapping[str, Any]],
    durable_facts: Mapping[str, Mapping[str, Any]],
    current_file_path: Path | None,
    worktree_root: Path,
    validation_gate: Mapping[str, Any] | None,
    stalled_step_context: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Capture the state consumed by the production skill-action prompt builder."""
    relative_file_path: str | None = None
    if current_file_path is not None:
        try:
            relative_file_path = str(
                current_file_path.resolve().relative_to(worktree_root.resolve())
            )
        except ValueError:
            relative_file_path = str(current_file_path)
    return {
        "transcript": [dict(message) for message in transcript],
        "execution_events": [dict(event) for event in execution_events],
        "execution_context": list(execution_context),
        "handoff_records": {
            name: dict(record) for name, record in handoff_records.items()
        },
        "durable_facts": {name: dict(record) for name, record in durable_facts.items()},
        "current_file_path": relative_file_path,
        "validation_gate": dict(validation_gate)
        if validation_gate is not None
        else None,
        "stalled_step_context": [dict(item) for item in stalled_step_context],
    }


def replay_bundle_from_error_record(
    record: Mapping[str, Any],
    *,
    repo_root: Path,
) -> dict[str, Any]:
    """Convert one workflow LLM error record into a portable replay bundle."""
    context = _mapping(record.get("context"), "record context")
    skill = _mapping(context.get("skill"), "record context.skill")
    definition_path = _string(skill.get("path"), "record context.skill.path")
    step_index = _integer(skill.get("step_index"), "record context.skill.step_index")
    step_id = skill.get("step_id")
    if step_id is not None and not isinstance(step_id, str):
        raise WorkflowReplayError("record context.skill.step_id must be a string.")

    failed_response = record.get("llm_output", record.get("attempted_action"))
    if failed_response is None:
        raise WorkflowReplayError(
            "record must include llm_output or attempted_action to create a "
            "replay bundle."
        )
    replay_state = context.get("replay_state", {})
    if not isinstance(replay_state, Mapping):
        raise WorkflowReplayError("record context.replay_state must be an object.")

    bundle: dict[str, Any] = {
        "schema_version": WORKFLOW_REPLAY_BUNDLE_SCHEMA_VERSION,
        "id": f"replay-{record.get('record_id') or uuid4()}",
        "source": {
            "record_id": record.get("record_id"),
            "recorded_at": record.get("recorded_at"),
            "phase": record.get("phase"),
        },
        "execution_mode": _string(
            record.get("execution_mode"), "record execution_mode"
        ),
        "definition": {
            "kind": "skill",
            "path": _portable_path(definition_path, repo_root),
            "name": _string(skill.get("name"), "record context.skill.name"),
            "content_sha256": skill.get("content_sha256"),
        },
        "step": {
            "index": step_index,
            "id": step_id,
            "description": skill.get("description"),
        },
        "prompt_builder_version": context.get(
            "prompt_builder_version", WORKFLOW_REPLAY_PROMPT_BUILDER_VERSION
        ),
        "prompt_state": dict(replay_state),
        "failed_response": failed_response,
        "expected": {
            "error_type": record.get("error_type"),
            "error": record.get("error"),
        },
        "guidance": record.get("guidance"),
        "redactions": [],
    }
    return bundle


def load_workflow_replay_bundle(path: Path) -> dict[str, Any]:
    """Load and validate one YAML or JSON replay bundle."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise WorkflowReplayError(
            f"Could not read replay bundle {path}: {exc}"
        ) from exc
    try:
        loaded = json.loads(raw) if path.suffix == ".json" else yaml.safe_load(raw)
    except (json.JSONDecodeError, yaml.YAMLError) as exc:
        raise WorkflowReplayError(
            f"Could not parse replay bundle {path}: {exc}"
        ) from exc
    if not isinstance(loaded, Mapping):
        raise WorkflowReplayError("replay bundle must decode to an object.")
    bundle = dict(loaded)
    _validate_replay_bundle(bundle)
    return bundle


def save_workflow_replay_bundle(path: Path, bundle: Mapping[str, Any]) -> Path:
    """Validate and save a replay bundle in YAML or JSON format."""
    normalized = dict(bundle)
    _validate_replay_bundle(normalized)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix == ".json":
        text = json.dumps(normalized, indent=2, ensure_ascii=False) + "\n"
    else:
        text = yaml.safe_dump(normalized, sort_keys=False, allow_unicode=True)
    path.write_text(text, encoding="utf-8")
    return path


def load_error_record(path: Path, record_id: str) -> dict[str, Any]:
    """Load a single error record by stable record id from a JSONL log."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise WorkflowReplayError(f"Could not read error log {path}: {exc}") from exc
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise WorkflowReplayError(
                f"Error log {path} line {line_number} is invalid JSON: {exc.msg}"
            ) from exc
        if isinstance(record, Mapping) and record.get("record_id") == record_id:
            return dict(record)
    raise WorkflowReplayError(
        f"Could not find record id {record_id!r} in {path or WORKFLOW_LLM_ERROR_LOG}."
    )


def render_skill_replay(
    bundle: Mapping[str, Any],
    *,
    repo_root: Path,
    definition_path: Path | None = None,
) -> dict[str, Any]:
    """Render and validate a skill replay without invoking tools or an LLM."""
    _validate_replay_bundle(bundle)
    definition = _mapping(bundle.get("definition"), "bundle definition")
    resolved_definition = _resolve_definition_path(
        definition_path or Path(_string(definition.get("path"), "definition path")),
        repo_root,
    )
    skill = load_skill(resolved_definition)
    step_data = _mapping(bundle.get("step"), "bundle step")
    step_index = _integer(step_data.get("index"), "bundle step.index")
    if not 0 <= step_index < len(skill.steps):
        raise WorkflowReplayError(
            f"bundle step index {step_index} is outside {resolved_definition}"
        )
    expected_step_id = step_data.get("id")
    current_step = skill.steps[step_index]
    if expected_step_id is not None and expected_step_id != current_step.id:
        raise WorkflowReplayError(
            f"bundle targets step id {expected_step_id!r}, but candidate definition "
            f"has {current_step.id!r} at index {step_index}."
        )

    from powdrr_lift.workflow_chat_agent import (
        SkillCatalogEntry,
        _build_step_execution_messages,
        _parse_action_response,
        _validate_workflow_action_for_step,
        _validate_workflow_step_transition,
        _WorkflowExecutionState,
    )

    state = _mapping(bundle.get("prompt_state"), "bundle prompt_state")
    execution_events = [
        dict(event)
        for event in _mapping_sequence(
            state.get("execution_events"), "execution_events"
        )
    ]
    transcript = _string_mapping_sequence(state.get("transcript"), "transcript")
    execution_context = _string_sequence(
        state.get("execution_context"), "execution_context"
    )
    handoff_records = _mapping_dict(state.get("handoff_records"), "handoff_records")
    durable_facts = _mapping_dict(state.get("durable_facts"), "durable_facts")
    current_file_path = state.get("current_file_path")
    if current_file_path is not None and not isinstance(current_file_path, str):
        raise WorkflowReplayError("prompt_state.current_file_path must be a string.")
    catalog_entry = SkillCatalogEntry(resolved_definition, skill)
    prompt_messages = _build_step_execution_messages(
        selected_skill=catalog_entry,
        current_step=current_step,
        current_step_index=step_index,
        transcript=transcript,
        execution_events=execution_events,
        execution_context=execution_context,
        handoff_records=handoff_records,
        durable_facts=durable_facts,
        current_file_path=(
            repo_root / current_file_path if current_file_path else None
        ),
        worktree_root=repo_root,
        catalog=(catalog_entry,),
        validation_gate=(
            _mapping(state["validation_gate"], "validation_gate")
            if state.get("validation_gate") is not None
            else None
        ),
        stalled_step_context=_mapping_sequence(
            state.get("stalled_step_context"), "stalled_step_context"
        ),
    )
    result: dict[str, Any] = {
        "schema_version": WORKFLOW_REPLAY_BUNDLE_SCHEMA_VERSION,
        "bundle_id": bundle["id"],
        "definition": str(resolved_definition),
        "step": {
            "index": step_index,
            "id": current_step.id,
            "description": current_step.description,
        },
        "prompt_messages": prompt_messages,
    }
    failed_response = bundle.get("failed_response")
    if not isinstance(failed_response, Mapping):
        raise WorkflowReplayError("bundle failed_response must be an object.")
    try:
        action = _parse_action_response(dict(failed_response))
        execution_state = _WorkflowExecutionState(
            selected_skill=catalog_entry,
            transcript=list(transcript),
            execution_events=[dict(event) for event in execution_events],
            execution_context=list(execution_context),
            step_index=step_index,
            worktree_root=repo_root,
            handoff_records={
                name: dict(value) for name, value in handoff_records.items()
            },
            durable_facts={name: dict(value) for name, value in durable_facts.items()},
        )
        _validate_workflow_step_transition(
            action,
            current_step,
            execution_events,
            step_index,
            state=execution_state,
        )
        _validate_workflow_action_for_step(action, current_step)
    except RuntimeError as exc:
        result["response_validation"] = {
            "valid": False,
            "error_type": type(exc).__name__,
            "error": str(exc),
        }
    else:
        result["response_validation"] = {
            "valid": True,
            "action": action.kind,
        }
    return result


def _validate_replay_bundle(bundle: Mapping[str, Any]) -> None:
    if bundle.get("schema_version") != WORKFLOW_REPLAY_BUNDLE_SCHEMA_VERSION:
        raise WorkflowReplayError(
            "replay bundle schema_version must be "
            f"{WORKFLOW_REPLAY_BUNDLE_SCHEMA_VERSION}."
        )
    _string(bundle.get("id"), "bundle id")
    definition = _mapping(bundle.get("definition"), "bundle definition")
    if definition.get("kind") != "skill":
        raise WorkflowReplayError("only skill replay bundles are supported initially.")
    _string(definition.get("path"), "bundle definition.path")
    _string(definition.get("name"), "bundle definition.name")
    step = _mapping(bundle.get("step"), "bundle step")
    _integer(step.get("index"), "bundle step.index")
    _mapping(bundle.get("prompt_state"), "bundle prompt_state")
    if not isinstance(bundle.get("failed_response"), Mapping):
        raise WorkflowReplayError("bundle failed_response must be an object.")


def _portable_path(path: str, repo_root: Path) -> str:
    candidate = Path(path)
    try:
        return candidate.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return path


def _resolve_definition_path(path: Path, repo_root: Path) -> Path:
    resolved = path if path.is_absolute() else repo_root / path
    if not resolved.is_file():
        raise WorkflowReplayError(f"Replay definition does not exist: {resolved}")
    return resolved


def definition_content_sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise WorkflowReplayError(f"{label} must be an object.")
    return value


def _mapping_dict(value: Any, label: str) -> dict[str, Mapping[str, Any]]:
    if value is None:
        return {}
    mapping = _mapping(value, label)
    result: dict[str, Mapping[str, Any]] = {}
    for name, item in mapping.items():
        result[str(name)] = _mapping(item, f"{label}.{name}")
    return result


def _mapping_sequence(value: Any, label: str) -> list[Mapping[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(
        isinstance(item, Mapping) for item in value
    ):
        raise WorkflowReplayError(f"{label} must be a list of objects.")
    return list(value)


def _string_mapping_sequence(value: Any, label: str) -> list[dict[str, str]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise WorkflowReplayError(f"{label} must be a list of objects.")
    result: list[dict[str, str]] = []
    for index, item in enumerate(value):
        mapping = _mapping(item, f"{label}[{index}]")
        role = _string(mapping.get("role"), f"{label}[{index}].role")
        content = _string(mapping.get("content"), f"{label}[{index}].content")
        result.append({"role": role, "content": content})
    return result


def _string_sequence(value: Any, label: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise WorkflowReplayError(f"{label} must be a list of strings.")
    return list(value)


def _string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise WorkflowReplayError(f"{label} must be a non-empty string.")
    return value


def _integer(value: Any, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise WorkflowReplayError(f"{label} must be a non-negative integer.")
    return value
