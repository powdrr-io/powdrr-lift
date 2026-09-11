"""Skill selection contracts and routing prompts for the workflow agent.

This module owns the data and prompt contract used to choose a skill. Runtime
execution remains in the chat agent.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from powdrr_lift.agent.provider_config import (
    DEFAULT_MODEL,
    LLMModelMapping,
    LLMProviderRole,
    LLMProviderRoles,
    provider_definition,
)
from powdrr_lift.errors import PowdrrExecutionError
from powdrr_lift.workflow_action_protocol import _optional_llm_type
from powdrr_lift.workflow_models import SkillCatalogEntry, WorkflowContext
from powdrr_lift.workflow_prompting import (
    _available_work_item_documents,
    _available_work_item_names,
    _match_work_item_names,
    _skill_step_to_data,
)


@dataclass(frozen=True, slots=True)
class SkillChatConfig:
    skills_dir: Path
    repo_root: Path | None = None
    output_dir: Path | None = None
    provider: str = "auto"
    normal_provider: str | None = None
    adversarial_provider: str | None = None
    model: str = DEFAULT_MODEL
    llm_mappings: tuple[tuple[str, LLMModelMapping], ...] = ()
    api_key: str | None = None
    base_url: str | None = None
    max_turns: int = 8
    max_stalled_roundtrips: int = 3
    provider_retry_attempts: int = 3
    provider_retry_delay_seconds: float = 30.0
    verbose: bool = False
    execution_id: str | None = None

    @property
    def templates_dir(self) -> Path:
        return self.skills_dir


@dataclass(frozen=True, slots=True)
class SkillChatResult:
    selected_skill_path: Path
    summary_path: Path


@dataclass(frozen=True, slots=True)
class SkillChatSelection:
    selected_skill_path: Path
    selected_skill_reason: str
    next_question: str | None = None
    ready_to_execute: bool = False
    llm_type: str | None = None

    @property
    def selected_template_path(self) -> Path:
        return self.selected_skill_path

    @property
    def selected_template_reason(self) -> str:
        return self.selected_skill_reason

    @property
    def ready_to_generate(self) -> bool:
        return self.ready_to_execute


WorkflowChatConfig = SkillChatConfig
WorkflowChatResult = SkillChatResult
WorkflowChatSelection = SkillChatSelection


def _build_selection_messages(
    catalog: Sequence[SkillCatalogEntry],
    transcript: Sequence[dict[str, str]],
    worktree_root: Path,
    workflow_context: WorkflowContext | None = None,
) -> list[dict[str, str]]:
    available_work_items = _available_work_item_names(worktree_root)
    return [
        {
            "role": "system",
            "content": _selection_system_prompt(),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "skills": [_catalog_entry_to_data(entry) for entry in catalog],
                    "conversation": list(transcript),
                    "previous_workflow_context": (
                        workflow_context.to_data() if workflow_context else None
                    ),
                    "work_item_context": {
                        "available": list(available_work_items),
                        "matches": list(
                            _match_work_item_names(
                                transcript,
                                available_work_items,
                            )
                        ),
                        "documents": {
                            work_item_name: list(
                                _available_work_item_documents(
                                    worktree_root,
                                    work_item_name,
                                )
                            )
                            for work_item_name in available_work_items
                        },
                    },
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        },
    ]


def _parse_selection_response(
    payload: dict[str, Any],
    catalog: Sequence[SkillCatalogEntry],
) -> SkillChatSelection:
    selected_skill_path_value = payload.get("selected_skill_path")
    if not isinstance(selected_skill_path_value, str) or not selected_skill_path_value:
        raise PowdrrExecutionError(
            "Skill selection response must include selected_skill_path."
        )
    selected_skill_path = _resolve_skill_path(selected_skill_path_value, catalog)
    selected_skill_reason = payload.get("selected_skill_reason")
    if not isinstance(selected_skill_reason, str) or not selected_skill_reason:
        raise PowdrrExecutionError(
            "Skill selection response must include selected_skill_reason."
        )
    next_question = payload.get("next_question")
    if next_question is not None and not isinstance(next_question, str):
        raise PowdrrExecutionError(
            "Skill selection response next_question must be a string."
        )
    if next_question is not None:
        next_question = _validate_user_question(
            next_question,
            field_name="Skill selection response next_question",
        )
    ready_to_execute_value = payload.get("ready_to_execute")
    if not isinstance(ready_to_execute_value, bool):
        raise PowdrrExecutionError(
            "Skill selection response ready_to_execute must be a boolean."
        )
    ready_to_execute = ready_to_execute_value
    if ready_to_execute and next_question is not None:
        raise PowdrrExecutionError(
            "Skill selection response must not include next_question when ready."
        )
    if not ready_to_execute and next_question is None:
        raise PowdrrExecutionError(
            "Skill selection response must include next_question when not ready."
        )
    llm_type = _optional_llm_type(payload.get("llm_type"))
    return SkillChatSelection(
        selected_skill_path=selected_skill_path,
        selected_skill_reason=selected_skill_reason,
        next_question=next_question,
        ready_to_execute=ready_to_execute,
        llm_type=llm_type,
    )


def _validate_user_question(value: str, *, field_name: str) -> str:
    normalized_value = value.strip()
    if (
        not normalized_value
        or not re.search(r"[A-Za-z]", normalized_value)
        or not normalized_value.endswith("?")
    ):
        raise PowdrrExecutionError(
            f"{field_name} must be a non-empty, properly formed English question."
        )
    return normalized_value


def _resolve_skill_path(
    skill_path_value: str,
    catalog: Sequence[SkillCatalogEntry],
) -> Path:
    normalized_value = _normalize_skill_path_value(skill_path_value)
    for entry in catalog:
        entry_value = str(entry.path)
        entry_value_no_suffix = _path_without_suffix(entry_value)
        if (
            skill_path_value == entry_value
            or skill_path_value == entry.path.name
            or skill_path_value == entry.path.stem
            or normalized_value == _normalize_skill_path_value(entry_value)
            or normalized_value == _normalize_skill_path_value(entry.path.name)
            or normalized_value == _normalize_skill_path_value(entry.path.stem)
            or _path_without_suffix(skill_path_value) == entry_value_no_suffix
        ):
            return entry.path
    raise PowdrrExecutionError(
        f"Skill selection response referenced unknown skill {skill_path_value!r}."
    )


def _resolve_template_path(
    template_path_value: str,
    catalog: Sequence[SkillCatalogEntry],
) -> Path:
    return _resolve_skill_path(template_path_value, catalog)


def _normalize_skill_path_value(value: str) -> str:
    return value.strip().rstrip(".").rstrip()


def _path_without_suffix(value: str) -> str:
    return str(Path(value.rstrip(".")).with_suffix(""))


def _catalog_entry_to_data(entry: SkillCatalogEntry) -> dict[str, Any]:
    return {
        "file": str(entry.path),
        "name": entry.skill.name,
        "adversarial": entry.skill.adversarial,
        "interaction_style": entry.skill.interaction_style,
        "when_to_use": list(entry.skill.when_to_use),
        "steps": [_skill_step_to_data(step) for step in entry.skill.steps],
    }


def _selected_skill_prompt_data(entry: SkillCatalogEntry) -> dict[str, Any]:
    """Return only skill identity; the active step carries execution details."""
    return {
        "file": entry.path.name,
        "name": entry.skill.name,
        "adversarial": entry.skill.adversarial,
        "interaction_style": entry.skill.interaction_style,
    }


def _selection_system_prompt() -> str:
    return (
        "Task: route the user's request to the best available skill. Read the "
        "catalog, conversation, and work-item context in the user message. "
        "Decide whether the request is sufficiently specified to begin that "
        "skill.\n"
        "Choose exactly one outcome:\n"
        "1. Ready: use this when one skill clearly matches and the available "
        "context is sufficient to start it. Set ready_to_execute to true and "
        "next_question to null.\n"
        "2. Need-information: use this when the skill is identifiable but a "
        "specific missing user decision or fact prevents starting. Set "
        "ready_to_execute to false and put exactly one concise question in "
        "next_question. Ask only for information not already present in the "
        "conversation or work-item context.\n"
        "3. Continue-clarification: use this only when the request is still "
        "ambiguous enough that the best skill cannot be selected. Set "
        "ready_to_execute to false and put exactly one concise question in "
        "next_question.\n"
        "Response: return exactly one JSON object with the keys "
        "selected_skill_path, selected_skill_reason, next_question, and "
        "ready_to_execute; llm_type is optional. For a ready response, "
        "next_question must be null and ready_to_execute must be true. For "
        "either clarification outcome, next_question must be a question and "
        "ready_to_execute must be false.\n"
        "A user question must be a properly formed English question: it must "
        "contain meaningful words, cannot be empty or only whitespace, and "
        "must end with a question mark. Never return whitespace or an "
        "instruction as next_question.\n"
        "llm_type describes the capability needed for the next roundtrip; use "
        "high_reasoning, standard_reasoning, simple_task, fast_iteration, "
        "long_context, or vision.\n"
        "selected_skill_path must match one of the catalog entries.\n"
        "Use the skill when_to_use and step descriptions to decide.\n"
        "When previous_workflow_context is present, treat it as the last skill's "
        "worktree and pull-request context. Select handle-ad-hoc for a small "
        "follow-up that does not match a more specific skill. Select "
        "address-review-comments for requests to check or fix pull-request "
        "comments. Follow-up requests about that worktree, branch, or PR should "
        "continue there. If the request could reasonably be either a continuation "
        "or a new task, ask exactly whether the user wants to reuse the previous "
        "worktree or start a new one.\n"
        "The user may refer to an existing work item using natural language. "
        "Before asking whether approved specification documents exist, inspect "
        "work_item_context. When matches contains a reasonable canonical name "
        "and its documents list is non-empty, reuse that exact name and existing "
        "documents; do not ask the user to confirm that they exist.\n"
        "Do not output markdown."
    )


def _active_llm_mappings(
    config: SkillChatConfig,
    provider_roles: LLMProviderRoles,
    role: LLMProviderRole,
) -> tuple[tuple[str, LLMModelMapping], ...]:
    """Return mappings for a role without exposing provider details to callers."""
    provider = provider_roles.provider_for(role)
    mappings = tuple(provider_definition(provider).llm_mappings.items())
    if role == "normal":
        mappings += config.llm_mappings
    return mappings


def _selection_repair_prompt(catalog: Sequence[SkillCatalogEntry]) -> str:
    catalog_entries = ", ".join(str(entry.path) for entry in catalog)
    return (
        "Task: repair the previous skill-routing response so it answers the "
        "routing task and obeys the response contract. Choose ready when one "
        "skill is sufficiently specified, or clarification when one specific "
        "missing fact or decision must be asked of the user.\n"
        "Response: return exactly one JSON object with keys "
        "selected_skill_path, selected_skill_reason, next_question, "
        "ready_to_execute, and llm_type. Set ready_to_execute=true and "
        "next_question=null for ready; set ready_to_execute=false and provide "
        "exactly one English question ending in '?' for clarification. The "
        f"selected_skill_path must be one of: {catalog_entries}. "
        "If next_question is present, it must be a concise, properly formed "
        "English question with meaningful words and a trailing question mark; "
        "it cannot be empty or only whitespace."
    )
