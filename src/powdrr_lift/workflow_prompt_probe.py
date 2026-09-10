"""Probe production workflow prompts against a live LLM without executing actions."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

import yaml

from powdrr_lift.core import resolve_repo_root
from powdrr_lift.core.skill_specification import load_skill
from powdrr_lift.core.workflow_task_specification import (
    WorkflowInstance,
    WorkflowTask,
    workflow_task_from_data,
)
from powdrr_lift.core.workflow_template_specification import load_workflow_template
from powdrr_lift.workflow_chat_agent import (
    SkillCatalogEntry,
    _build_chat_client,
    _build_step_execution_messages,
    _parse_action_response_with_schema,
    _resolve_credentials,
    _step_action_response_schema,
    _workflow_action_data,
)
from powdrr_lift.workflow_llm import (
    WorkflowLLMClient,
    complete_json_with_timeout_retry,
)
from powdrr_lift.workflow_task_agent import _build_task_messages


class WorkflowPromptProbeError(ValueError):
    """Raised when a definition or step cannot be selected for probing."""


@dataclass(frozen=True, slots=True)
class WorkflowPromptProbe:
    """The production prompt and contract prepared for one definition step."""

    definition: Path
    definition_kind: str
    step_index: int
    step_id: str | None
    messages: list[dict[str, str]]
    response_schema: Mapping[str, Any]
    step: Any

    @property
    def prompt_sha256(self) -> str:
        content = json.dumps(self.messages, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(content.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class WorkflowPromptProbeResult:
    """One non-executing model probe result."""

    probe: WorkflowPromptProbe
    sample: int
    response: Mapping[str, Any] | None
    valid: bool
    error: str | None = None
    error_code: str | None = None
    action: Mapping[str, Any] | None = None

    def to_data(self, *, include_prompt: bool = False) -> dict[str, Any]:
        data: dict[str, Any] = {
            "definition": str(self.probe.definition),
            "definition_kind": self.probe.definition_kind,
            "step_index": self.probe.step_index,
            "step_id": self.probe.step_id,
            "sample": self.sample,
            "prompt_sha256": self.probe.prompt_sha256,
            "response_schema": self.probe.response_schema,
            "response": self.response,
            "valid": self.valid,
            "error": self.error,
            "error_code": self.error_code,
            "action": self.action,
        }
        if include_prompt:
            data["messages"] = self.probe.messages
        return data


def build_workflow_prompt_probe(
    definition_path: Path,
    *,
    repo_root: Path | None = None,
    root_intent: str = "Probe this workflow step.",
    step_id: str | None = None,
    step_index: int | None = None,
) -> WorkflowPromptProbe:
    """Build the exact normal execution prompt for one selected step."""
    if step_id is not None and step_index is not None:
        raise WorkflowPromptProbeError(
            "Specify either step_id or step_index, not both."
        )
    root = resolve_repo_root(repo_root)
    definition = definition_path.expanduser().resolve()
    try:
        raw = _load_raw_definition(definition)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        raise WorkflowPromptProbeError(f"Could not load {definition}: {exc}") from exc

    if isinstance(raw.get("steps"), list):
        step: Any
        skill = load_skill(definition)
        index = _select_step(skill.steps, step_id=step_id, step_index=step_index)
        step = skill.steps[index]
        entry = SkillCatalogEntry(definition, skill)
        messages = _build_step_execution_messages(
            selected_skill=entry,
            current_step=step,
            current_step_index=index,
            transcript=[{"role": "user", "content": root_intent}],
            execution_events=[],
            execution_context=[],
            handoff_records={},
            durable_facts={},
            current_file_path=None,
            worktree_root=root,
            catalog=(entry,),
        )
        kind = "skill"
        step_id_value = step.id
    elif isinstance(raw.get("task_templates"), list):
        template = load_workflow_template(definition)
        index = _select_step(
            template.task_templates, step_id=step_id, step_index=step_index
        )
        template_step = template.task_templates[index]
        task = _task_from_template(template_step, index=index, definition=definition)
        workflow = WorkflowInstance(directory=root, _tasks={task.task_id: task})
        messages = _build_task_messages(
            workflow,
            task,
            [],
            repo_root=root,
            skill_catalog=(),
        )
        step = task
        kind = "workflow_template"
        step_id_value = task.task_id
    elif "task_id" in raw:
        task = workflow_task_from_data(raw)
        index = 0 if step_index is None else step_index
        if step_id is not None and step_id != task.task_id:
            raise WorkflowPromptProbeError(
                f"No task with id {step_id!r} exists in {definition}."
            )
        if index != 0:
            raise WorkflowPromptProbeError("A durable task file contains only step 0.")
        workflow = WorkflowInstance(directory=root, _tasks={task.task_id: task})
        messages = _build_task_messages(
            workflow,
            task,
            [],
            repo_root=root,
            skill_catalog=(),
        )
        step = task
        kind = "workflow_task"
        step_id_value = task.task_id
    else:
        raise WorkflowPromptProbeError(
            "Definition must contain steps, task_templates, or task_id."
        )

    return WorkflowPromptProbe(
        definition=definition,
        definition_kind=kind,
        step_index=index,
        step_id=step_id_value,
        messages=messages,
        response_schema=_step_action_response_schema(step),
        step=step,
    )


def probe_workflow_step(
    client: WorkflowLLMClient,
    probe: WorkflowPromptProbe,
    *,
    samples: int = 1,
    model: str = "",
    stderr: TextIO | None = None,
    max_timeout_retries: int = 0,
    timeout_backoff_seconds: float = 0,
    include_prompt: bool = False,
) -> tuple[WorkflowPromptProbeResult, ...]:
    """Call a prepared prompt and validate every response without executing it."""
    if samples < 1:
        raise WorkflowPromptProbeError("samples must be at least 1.")
    results: list[WorkflowPromptProbeResult] = []
    for sample in range(1, samples + 1):
        response: Mapping[str, Any] | None = None
        try:
            response = complete_json_with_timeout_retry(
                client,
                probe.messages,
                model=model,
                stderr=stderr,
                max_timeout_retries=max_timeout_retries,
                timeout_backoff_seconds=timeout_backoff_seconds,
                response_schema=probe.response_schema,
            )
            action = _parse_action_response_with_schema(
                dict(response), schema=probe.response_schema
            )
            _validate_probe_action(action, probe.step)
        except Exception as exc:
            results.append(
                WorkflowPromptProbeResult(
                    probe=probe,
                    sample=sample,
                    response=response,
                    valid=False,
                    error=str(exc),
                    error_code=getattr(exc, "error_code", type(exc).__name__),
                )
            )
        else:
            results.append(
                WorkflowPromptProbeResult(
                    probe=probe,
                    sample=sample,
                    response=response,
                    valid=True,
                    action=_workflow_action_data(action),
                )
            )
    return tuple(results)


def build_probe_client(
    *,
    provider: str,
    model: str,
    api_key: str | None,
    base_url: str | None,
    repo_root: Path,
    progress_stream: TextIO | None = None,
) -> WorkflowLLMClient:
    """Construct the same provider client used by workflow-chat."""
    credentials = _resolve_credentials(provider, api_key, base_url)
    return _build_chat_client(
        credentials,
        model=model,
        model_cache_dir=repo_root / ".powdrr" / "models",
        progress_stream=progress_stream,
    )


def _load_raw_definition(path: Path) -> Mapping[str, Any]:
    content = path.read_text(encoding="utf-8")
    data = (
        json.loads(content)
        if path.suffix.lower() == ".json"
        else yaml.safe_load(content)
    )
    if not isinstance(data, Mapping):
        raise ValueError("definition must decode to an object")
    return data


def _select_step(
    steps: Any,
    *,
    step_id: str | None,
    step_index: int | None,
) -> int:
    if step_index is None and step_id is None:
        raise WorkflowPromptProbeError("Specify --step-id or --step-index.")
    if step_index is not None:
        if not 0 <= step_index < len(steps):
            raise WorkflowPromptProbeError(
                f"step_index {step_index} is outside the definition."
            )
        return step_index
    matches = [
        index
        for index, step in enumerate(steps)
        if getattr(step, "id", None) == step_id
    ]
    if len(matches) != 1:
        raise WorkflowPromptProbeError(f"No unique step with id {step_id!r} exists.")
    return matches[0]


def _task_from_template(
    template_step: Any, *, index: int, definition: Path
) -> WorkflowTask:
    data = template_step.to_data()
    data.update(
        {
            "task_id": f"probe-task-{index + 1:03d}",
            "status": "open",
            "upstream_task_ids": [],
            "dependent_state": data.get("dependent_state", []),
        }
    )
    data["workflow_template"] = definition.stem
    return workflow_task_from_data(data)


def _validate_probe_action(action: Any, step: Any) -> None:
    from powdrr_lift.workflow_chat_agent import _validate_workflow_action_for_step

    _validate_workflow_action_for_step(action, step)
