from __future__ import annotations

import inspect
import json
import math
import os
import re
import select
import shlex
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any, Literal, TextIO, cast
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import yaml

try:
    import readline
    import termios
    import tty
except ImportError:  # pragma: no cover - only used on non-POSIX platforms
    readline = None  # type: ignore[assignment]
    termios = None  # type: ignore[assignment]
    tty = None  # type: ignore[assignment]

from powdrr_lift.basedpyright_tools import (
    BASEDPYRIGHT_STRUCTURE_TOOL,
    BASEDPYRIGHT_SYMBOL_TOOL,
    execute_basedpyright_tool,
    is_basedpyright_tool,
)
from powdrr_lift.core import (
    Skill,
    architecture_specification_default_output_path,
    build_skill_directory_validation_report,
    codebase_state_default_output_path,
    current_state_specification_default_output_path,
    feature_pr_specification_default_output_path,
    implementation_specification_default_output_path,
    load_skills,
    pr_specification_default_output_path,
    resolve_repo_root,
    system_map_specification_default_output_path,
    system_specification_default_output_path,
)
from powdrr_lift.core.spec_context import (
    gather_specification_context,
    normalize_context_type,
    render_gather_context_report,
)
from powdrr_lift.core.validation_messages import (
    ValidationError,
    validation_error_to_data,
)
from powdrr_lift.fuzzy_match import fuzzy_match_json
from powdrr_lift.workflow_llm import (
    ProgressDecision,
    WorkflowActionObservation,
    WorkflowActionOutcome,
    WorkflowActionProgressStrategy,
    WorkflowActionRequest,
    WorkflowExecutionStrategy,
    WorkflowLLMClient,
    WorkflowLLMExecutionAborted,
    WorkflowLLMExecutionDriver,
    prune_execution_events,
)
from powdrr_lift.workflow_llm import (
    WorkflowAction as SkillChatAction,
)
from powdrr_lift.workflow_llm import (
    WorkflowEdit as SkillChatEdit,
)
from powdrr_lift.workflow_llm import (
    WorkflowFileEdits as SkillChatFileEdits,
)
from powdrr_lift.workflow_llm import (
    WorkflowYamlOperation as SkillChatYamlOperation,
)
from powdrr_lift.workflow_llm import (
    complete_json as _request_json,
)
from powdrr_lift.workflow_llm import (
    workflow_action_signature as _shared_workflow_action_signature,
)

_DEFAULT_MODEL = "glm-5.2"
_DEFAULT_LLM_TYPE = "high_reasoning"
_MAX_COMPLETION_TOKENS = 32768
_MAX_EMPTY_QUESTION_REPROMPTS = 3
_QWEN_2_5_CODER_MODEL = "Qwen/Qwen2.5-Coder-14B-Instruct"
_LOCAL_MODEL_REPOSITORY = "Qwen/Qwen2.5-Coder-14B-Instruct-GGUF"
_LOCAL_MODEL_PATTERN = "qwen2.5-coder-14b-instruct-q5_k_m*.gguf"
_DEEPINFRA_CHEAP_MODEL = "deepseek-ai/DeepSeek-V4-Flash"
ALL_LLM_TYPES = (
    "high_reasoning",
    "standard_reasoning",
    "simple_task",
    "fast_iteration",
    "long_context",
    "vision",
)
_DEFAULT_LOCAL_MODEL_CONTEXT = 24576
_LOCAL_MODEL_CONTEXT_ENV = "POWDRR_LOCAL_MODEL_CONTEXT"
_TOKEN_ESTIMATE_CHARS_PER_TOKEN = 3
_CONTEXT_SAFETY_MARGIN_TOKENS = 1024
_MAX_DOCUMENT_CONTEXT_LINES = 2000
_MAX_PROMPT_TRANSCRIPT_ENTRIES = 32
_MAX_PROMPT_TRANSCRIPT_CHARS = 24000
_MAX_PROMPT_TRANSCRIPT_MESSAGE_CHARS = 8000
_WORKFLOW_CONTEXT_PATH = Path(".powdrr") / "workflow-context.json"
_INTERNAL_TOOL = "internal"
_INTERNAL_BINARY = "powdrr-lift"


@dataclass(frozen=True, slots=True)
class LLMModelLimits:
    context_window: int
    max_output_tokens: int


@dataclass(frozen=True, slots=True)
class LLMModelMapping:
    model: str
    provider: str
    backup_model: LLMModelMapping | None = None
    long_context_backup_model: LLMModelMapping | None = None


LLMProviderRole = Literal["normal", "adversarial"]


@dataclass(frozen=True, slots=True)
class LLMProviderRoles:
    normal: str
    adversarial: str | None = None

    def provider_for(self, role: LLMProviderRole) -> str:
        if role == "adversarial" and self.adversarial is not None:
            return self.adversarial
        return self.normal


_DEFAULT_MODEL_LIMITS = LLMModelLimits(
    context_window=128_000,
    max_output_tokens=_MAX_COMPLETION_TOKENS,
)

ZAI_MODEL_LIMITS: Mapping[str, LLMModelLimits] = {
    "glm-5.2": LLMModelLimits(context_window=200_000, max_output_tokens=131_072),
    "glm-4.7": LLMModelLimits(context_window=200_000, max_output_tokens=131_072),
    "glm-4.7-flashx": LLMModelLimits(
        context_window=200_000,
        max_output_tokens=131_072,
    ),
    "glm-4.7-flash": LLMModelLimits(
        context_window=200_000,
        max_output_tokens=131_072,
    ),
    "glm-4.6v": LLMModelLimits(context_window=200_000, max_output_tokens=32_768),
}

# DeepInfra exposes model-specific limits through its model metadata API. Keep
# conservative limits for the configured models so requests never claim more
# output than the documented 16K cap for most hosted models.
DEEPINFRA_MODEL_LIMITS: Mapping[str, LLMModelLimits] = {
    "deepseek-ai/deepseek-v4-pro": LLMModelLimits(
        context_window=1_000_000,
        max_output_tokens=16_384,
    ),
    "deepseek-ai/deepseek-v4-flash": LLMModelLimits(
        context_window=1_000_000,
        max_output_tokens=16_384,
    ),
    "qwen/qwen3-next-80b-a3b-instruct": LLMModelLimits(
        context_window=128_000,
        max_output_tokens=16_384,
    ),
    "qwen/qwen2.5-vl-32b-instruct": LLMModelLimits(
        context_window=128_000,
        max_output_tokens=16_384,
    ),
}


# These are semantic task classes, rather than model names. Each capability
# maps to its primary model and, when needed, its per-model backup.
ZAI_LLM_MAPPINGS: Mapping[str, LLMModelMapping] = {
    "high_reasoning": LLMModelMapping("glm-5.2", provider="zai"),
    "standard_reasoning": LLMModelMapping("glm-4.7", provider="zai"),
    "simple_task": LLMModelMapping(
        _QWEN_2_5_CODER_MODEL,
        provider="local",
        backup_model=LLMModelMapping("glm-4.7", provider="zai"),
        long_context_backup_model=LLMModelMapping("glm-5.2", provider="zai"),
    ),
    "fast_iteration": LLMModelMapping(
        _QWEN_2_5_CODER_MODEL,
        provider="local",
        long_context_backup_model=LLMModelMapping("glm-4.7-flash", provider="zai"),
    ),
    "long_context": LLMModelMapping("glm-5.2", provider="zai"),
    "vision": LLMModelMapping("glm-4.6v", provider="zai"),
}

DEEPINFRA_LLM_MAPPINGS: Mapping[str, LLMModelMapping] = {
    "high_reasoning": LLMModelMapping(
        "deepseek-ai/DeepSeek-V4-Pro", provider="deepinfra"
    ),
    "standard_reasoning": LLMModelMapping(
        "deepseek-ai/DeepSeek-V4-Flash", provider="deepinfra"
    ),
    "simple_task": LLMModelMapping(
        "Qwen/Qwen3-Next-80B-A3B-Instruct",
        provider="deepinfra",
        long_context_backup_model=LLMModelMapping(
            "deepseek-ai/DeepSeek-V4-Flash",
            provider="deepinfra",
        ),
    ),
    "fast_iteration": LLMModelMapping(
        "Qwen/Qwen3-Next-80B-A3B-Instruct",
        provider="deepinfra",
        long_context_backup_model=LLMModelMapping(
            "deepseek-ai/DeepSeek-V4-Flash",
            provider="deepinfra",
        ),
    ),
    "long_context": LLMModelMapping(
        "deepseek-ai/DeepSeek-V4-Flash", provider="deepinfra"
    ),
    "vision": LLMModelMapping("Qwen/Qwen2.5-VL-32B-Instruct", provider="deepinfra"),
}

DEEPINFRA_CHEAP_LLM_MAPPINGS: Mapping[str, LLMModelMapping] = {
    llm_type: LLMModelMapping(_DEEPINFRA_CHEAP_MODEL, provider="deepinfra-cheap")
    for llm_type in ALL_LLM_TYPES
}


@dataclass(frozen=True, slots=True)
class LLMProviderDefinition:
    name: str
    display_name: str
    llm_mappings: Mapping[str, LLMModelMapping]
    model_limits: Mapping[str, LLMModelLimits]
    api_key_env_names: tuple[str, ...] = ()
    base_url_env_names: tuple[str, ...] = ()
    default_base_url: str = "https://api.openai.com/v1"
    client_kind: str = "openai"
    forced_model: str | None = None
    auto_priority: int | None = None


LLM_PROVIDERS: Mapping[str, LLMProviderDefinition] = {
    "openai": LLMProviderDefinition(
        name="openai",
        display_name="OpenAI",
        llm_mappings={},
        model_limits={},
        api_key_env_names=("OPENAI_API_KEY", "CODEX_API_KEY"),
        base_url_env_names=("OPENAI_BASE_URL", "CODEX_BASE_URL"),
        auto_priority=40,
    ),
    "anthropic": LLMProviderDefinition(
        name="anthropic",
        display_name="Anthropic",
        llm_mappings={},
        model_limits={},
        api_key_env_names=("ANTHROPIC_API_KEY", "CLAUDE_API_KEY"),
        base_url_env_names=("ANTHROPIC_BASE_URL",),
        default_base_url="https://api.anthropic.com",
        client_kind="anthropic",
        auto_priority=20,
    ),
    "zai": LLMProviderDefinition(
        name="zai",
        display_name="z.ai",
        llm_mappings=ZAI_LLM_MAPPINGS,
        model_limits=ZAI_MODEL_LIMITS,
        api_key_env_names=("ZAI_API_KEY", "GLM_API_KEY"),
        base_url_env_names=("ZAI_BASE_URL",),
        default_base_url="https://api.z.ai/api/paas/v4/",
        auto_priority=30,
    ),
    "deepinfra": LLMProviderDefinition(
        name="deepinfra",
        display_name="DeepInfra",
        llm_mappings=DEEPINFRA_LLM_MAPPINGS,
        model_limits=DEEPINFRA_MODEL_LIMITS,
        api_key_env_names=("DEEPINFRA_API_TOKEN", "DEEPINFRA_API_KEY"),
        base_url_env_names=("DEEPINFRA_BASE_URL",),
        default_base_url="https://api.deepinfra.com/v1/openai",
        auto_priority=None,
    ),
    "deepinfra-cheap": LLMProviderDefinition(
        name="deepinfra-cheap",
        display_name="DeepInfra",
        llm_mappings=DEEPINFRA_CHEAP_LLM_MAPPINGS,
        model_limits=DEEPINFRA_MODEL_LIMITS,
        api_key_env_names=("DEEPINFRA_API_TOKEN", "DEEPINFRA_API_KEY"),
        base_url_env_names=("DEEPINFRA_BASE_URL",),
        default_base_url="https://api.deepinfra.com/v1/openai",
        forced_model=_DEEPINFRA_CHEAP_MODEL,
        auto_priority=10,
    ),
    "local": LLMProviderDefinition(
        name="local",
        display_name="local",
        llm_mappings=ZAI_LLM_MAPPINGS,
        model_limits={},
        default_base_url="local",
        client_kind="local",
    ),
}
ALL_PROVIDERS = tuple(LLM_PROVIDERS)


def _provider_definition(provider: str) -> LLMProviderDefinition:
    try:
        return LLM_PROVIDERS[provider]
    except KeyError as exc:
        raise RuntimeError(f"Unsupported LLM provider {provider!r}.") from exc


def _default_llm_mappings(provider: str) -> Mapping[str, LLMModelMapping]:
    return _provider_definition(provider).llm_mappings


def _provider_supports_llm_mappings(provider: str) -> bool:
    return bool(_provider_definition(provider).llm_mappings)


WorkflowActionParser = Callable[
    [dict[str, Any], str | None, str | None], "SkillChatAction"
]


@dataclass(frozen=True, slots=True)
class SkillCatalogEntry:
    path: Path
    skill: Skill


@dataclass(frozen=True, slots=True)
class SkillChatConfig:
    skills_dir: Path
    repo_root: Path | None = None
    output_dir: Path | None = None
    provider: str = "auto"
    normal_provider: str | None = None
    adversarial_provider: str | None = None
    model: str = _DEFAULT_MODEL
    llm_mappings: tuple[tuple[str, LLMModelMapping], ...] = ()
    api_key: str | None = None
    base_url: str | None = None
    max_turns: int = 8
    max_stalled_roundtrips: int = 3
    provider_retry_attempts: int = 3
    provider_retry_delay_seconds: float = 30.0
    verbose: bool = False

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


@dataclass(frozen=True, slots=True)
class WorkflowContext:
    worktree_root: Path
    branch_name: str | None = None
    pr_number: int | None = None
    pr_url: str | None = None
    skill_name: str | None = None
    request: str | None = None

    def to_data(self) -> dict[str, object]:
        return {
            "worktree_root": str(self.worktree_root),
            "branch_name": self.branch_name,
            "pr_number": self.pr_number,
            "pr_url": self.pr_url,
            "skill_name": self.skill_name,
            "request": self.request,
        }


WorkflowTemplateCatalogEntry = SkillCatalogEntry
WorkflowChatConfig = SkillChatConfig
WorkflowChatResult = SkillChatResult
WorkflowChatSelection = SkillChatSelection


@dataclass(slots=True)
class _WorkflowExecutionState:
    selected_skill: SkillCatalogEntry
    transcript: list[dict[str, str]]
    execution_events: list[dict[str, Any]]
    execution_context: list[str]
    step_index: int
    worktree_root: Path
    current_file_path: Path | None = None
    current_file_context_cache: dict[tuple[str, int, int], dict[str, Any]] = field(
        default_factory=dict
    )
    fuzzy_match_cache: dict[tuple[str, int, int | None], tuple[Path, ...]] = field(
        default_factory=dict
    )


@dataclass(slots=True)
class _ChatActionProgressStrategy(WorkflowActionProgressStrategy[SkillChatAction]):
    """Keep chat-only transcript and status context outside the shared engine."""

    state: _WorkflowExecutionState

    def material_state(self, action: SkillChatAction) -> object:
        return _workflow_action_material_state(action, self.state)

    def record_no_progress(
        self,
        action: SkillChatAction,
        observation: WorkflowActionObservation,
    ) -> None:
        _ = action
        assert observation.correction is not None
        self.state.transcript.append(
            {"role": "user", "content": observation.correction}
        )
        self.state.execution_context.append(observation.correction)


@dataclass(slots=True)
class _ChatWorkflowExecutionStrategy(WorkflowExecutionStrategy):
    """Interactive adapter around the shared action-roundtrip driver."""

    config: WorkflowChatConfig
    selection: SkillChatSelection
    catalog: tuple[SkillCatalogEntry, ...]
    workflow_context: WorkflowContext | None
    state: _WorkflowExecutionState
    progress: _WorkflowProgressDisplay
    input_func: Callable[[], str]
    stdout: TextIO
    stderr: TextIO
    client_for_model: Callable[[str, str], WorkflowLLMClient]
    provider_roles: LLMProviderRoles
    provider_role: LLMProviderRole
    current_model: str
    provider: str
    driver: WorkflowLLMExecutionDriver
    skill_stack: list[_SkillExecutionFrame] = field(default_factory=list)
    completed_dependencies: set[tuple[str, int, str]] = field(default_factory=set)
    last_failed_action: SkillChatAction | None = None
    last_validation_error: str | None = None
    failure_kind: str = "validation_error"
    current_step: Any = None
    current_step_index: int = 0
    step_roundtrips: int = 0

    @property
    def selected_skill(self) -> SkillCatalogEntry:
        return self.state.selected_skill

    def _parent_progress(self) -> tuple[SkillCatalogEntry | None, int | None]:
        if not self.skill_stack:
            return None, None
        frame = self.skill_stack[-1]
        return frame.parent_skill, frame.parent_step_index

    def _restore_parent(self) -> None:
        frame = self.skill_stack.pop()
        if frame.dependency_key is not None:
            self.completed_dependencies.add(frame.dependency_key)
        self.state.selected_skill = frame.parent_skill
        self.state.step_index = frame.resume_step_index
        if frame.clean_context:
            self.state.transcript = list(frame.parent_transcript or ())
            self.state.execution_events = list(frame.parent_execution_events or ())
            self.state.execution_context = list(frame.parent_execution_context or ())
            self.state.current_file_path = frame.parent_current_file_path
        self.current_model = frame.parent_model
        self.provider = frame.parent_provider
        self.provider_role = frame.parent_provider_role

    def _push_skill(
        self,
        nested_skill: SkillCatalogEntry,
        *,
        resume_step_index: int,
        dependency_key: tuple[str, int, str] | None = None,
        clean_context: bool = False,
    ) -> None:
        _push_nested_skill(
            self.skill_stack,
            current_skill=self.selected_skill,
            nested_skill=nested_skill,
            parent_step_index=self.current_step_index,
            resume_step_index=resume_step_index,
            dependency_key=dependency_key,
            parent_model=self.current_model,
            parent_provider=self.provider,
            parent_provider_role=self.provider_role,
            clean_context=clean_context,
            parent_transcript=tuple(self.state.transcript) if clean_context else None,
            parent_execution_events=(
                tuple(self.state.execution_events) if clean_context else None
            ),
            parent_execution_context=(
                tuple(self.state.execution_context) if clean_context else None
            ),
            parent_current_file_path=(
                self.state.current_file_path if clean_context else None
            ),
        )
        self.state.selected_skill = nested_skill
        self.state.step_index = 0

    def next_request(self) -> WorkflowActionRequest | None:
        while True:
            if self.state.step_index >= len(self.selected_skill.skill.steps):
                if not self.skill_stack:
                    return None
                self._restore_parent()
                continue
            self.provider = self.provider_roles.provider_for(self.provider_role)
            self.current_model = (
                _provider_definition(self.provider).forced_model or self.current_model
            )
            self.current_step_index = self.state.step_index
            self.current_step = self.selected_skill.skill.steps[self.current_step_index]
            dependency_name = _next_skill_dependency(
                self.selected_skill,
                self.current_step_index,
                self.completed_dependencies,
            )
            if dependency_name is not None:
                nested_skill = _find_skill_by_name(self.catalog, dependency_name)
                nested_role: LLMProviderRole = (
                    self.provider_role
                    if nested_skill.skill.adversarial is None
                    else ("adversarial" if nested_skill.skill.adversarial else "normal")
                )
                self._push_skill(
                    nested_skill,
                    resume_step_index=self.current_step_index,
                    dependency_key=(
                        str(self.selected_skill.path),
                        self.current_step_index,
                        dependency_name,
                    ),
                )
                self.provider_role = nested_role
                continue
            step_mapping = (
                _resolve_llm_mapping(
                    self.current_step.llm_type or self.selection.llm_type,
                    mappings=_active_llm_mappings(
                        self.config, self.provider_roles, self.provider_role
                    ),
                    provider=self.provider,
                )
                if _provider_supports_llm_mappings(self.provider)
                else None
            )
            if step_mapping is None:
                step_mapping = LLMModelMapping(
                    self.current_model, provider=self.provider
                )
            self.current_model = step_mapping.model
            self.provider = _resolve_provider(
                self.config.provider,
                self.current_model,
                mapping=step_mapping,
            )
            self.step_roundtrips += 1
            parent_skill, parent_step_index = self._parent_progress()
            self.progress.update(
                self.selected_skill,
                current_step_index=self.current_step_index,
                status=f"waiting for {self.current_model} LLM response...",
                parent_skill=parent_skill,
                parent_step_index=parent_step_index,
            )
            messages = _build_step_execution_messages(
                selected_skill=self.selected_skill,
                current_step=self.current_step,
                current_step_index=self.current_step_index,
                transcript=self.state.transcript,
                execution_events=self.state.execution_events,
                execution_context=self.state.execution_context,
                current_file_path=self.state.current_file_path,
                worktree_root=self.state.worktree_root,
                catalog=self.catalog,
                workflow_context=self.workflow_context,
                current_file_context_cache=self.state.current_file_context_cache,
            )
            return WorkflowActionRequest(
                client=self.client_for_model(self.current_model, self.provider),
                messages=messages,
                parser=_parse_action_response,
                model=self.current_model,
                stderr=self.stderr,
                max_timeout_retries=0,
                timeout_backoff_seconds=0,
                request_action=partial(self._request_action, messages),
            )

    def _request_action(self, messages: list[dict[str, str]]) -> SkillChatAction:
        action, self.current_model, self.provider = _complete_json_with_model_fallback(
            client_for=self.client_for_model,
            messages=messages,
            parser=_parse_action_response,
            context=(
                f"workflow execution for step {self.current_step_index + 1}/"
                f"{len(self.selected_skill.skill.steps)}"
            ),
            model=self.current_model,
            repair_instructions=_action_repair_prompt(
                self.selected_skill,
                failed_action=self.last_failed_action,
                validation_error=self.last_validation_error,
            ),
            config=self.config,
            input_func=self.input_func,
            stdout=self.stdout,
            stderr=self.stderr,
            provider=self.provider,
            model_mappings=tuple(ZAI_LLM_MAPPINGS.items())
            + tuple(
                _active_llm_mappings(
                    self.config, self.provider_roles, self.provider_role
                )
            ),
            empty_response_fallback_payload={"kind": "next_step"},
        )
        if action is None:
            raise WorkflowLLMExecutionAborted(1)
        return action

    def material_state(self, action: SkillChatAction) -> object:
        return _workflow_action_material_state(action, self.state)

    def record_no_progress(
        self,
        action: SkillChatAction,
        observation: WorkflowActionObservation,
    ) -> None:
        _ChatActionProgressStrategy(self.state).record_no_progress(action, observation)

    def execute_action(self, action: SkillChatAction) -> WorkflowActionOutcome:
        if action.llm_type is not None and _provider_supports_llm_mappings(
            self.provider
        ):
            mapping = _resolve_llm_mapping(
                action.llm_type,
                mappings=_active_llm_mappings(
                    self.config, self.provider_roles, self.provider_role
                ),
                provider=self.provider,
            )
            assert mapping is not None
            self.current_model = mapping.model
            self.provider = mapping.provider
        if action.kind == "invoke_skill":
            if action.skill_name is None:
                raise RuntimeError("invoke_skill action must include a skill name.")
            nested_skill = _find_skill_by_name(self.catalog, action.skill_name)
            context = list(action.context)
            if action.decisions_and_context is not None:
                context.append(action.decisions_and_context)
            nested_role: LLMProviderRole = (
                (
                    self.provider_role
                    if nested_skill.skill.adversarial is None
                    else ("adversarial" if nested_skill.skill.adversarial else "normal")
                )
                if action.provider_role is None
                else action.provider_role
            )
            self._push_skill(
                nested_skill,
                resume_step_index=self.current_step_index + 1,
                clean_context=action.clean,
            )
            self.state.execution_events.append(
                {
                    "kind": action.kind,
                    "skill": action.skill_name,
                    "step_index": self.current_step_index,
                }
            )
            if action.clean:
                self.state.transcript = []
                self.state.execution_events = []
                self.state.execution_context = context
                self.state.current_file_path = None
            else:
                self.state.execution_context.extend(context)
            self.provider_role = nested_role
            return WorkflowActionOutcome()
        if action.kind == "complete" and self.skill_stack:
            self._restore_parent()
            return WorkflowActionOutcome()
        status = _workflow_action_progress_status(action)
        if status is not None:
            parent_skill, parent_step_index = self._parent_progress()
            self.progress.update(
                self.selected_skill,
                current_step_index=self.state.step_index,
                status=status,
                parent_skill=parent_skill,
                parent_step_index=parent_step_index,
            )
        self.failure_kind = "validation_error"
        _validate_workflow_step_transition(
            action,
            self.current_step,
            self.state.execution_events,
            self.state.step_index,
        )
        _validate_workflow_action_for_step(action, self.current_step)
        self.failure_kind = "action_error"
        handler = _workflow_action_handlers().get(action.kind)
        if handler is None:
            raise RuntimeError(f"Unsupported workflow action kind: {action.kind!r}")
        should_continue = handler(
            action,
            self.state,
            self.stdout,
            self.stderr,
            self.input_func,
            self.config,
        )
        self.last_failed_action = None
        self.last_validation_error = None
        return WorkflowActionOutcome(continue_running=should_continue)

    def record_response_error(
        self,
        error: RuntimeError,
        payload: dict[str, Any] | None,
    ) -> None:
        _ = payload
        raise error

    def record_action_error(self, action: SkillChatAction, error: Exception) -> None:
        signature = _workflow_action_signature(action)
        feedback = _workflow_edit_failure_feedback(
            action,
            error,
            _current_file_context(
                self.state.worktree_root, self.state.current_file_path
            ),
        )
        print(feedback, file=self.stderr)
        _write_agent_error(
            self.state.worktree_root,
            feedback + _rejected_edit_guidance(action),
        )
        self.last_failed_action = action
        self.state.transcript.extend(
            [
                {"role": "assistant", "content": signature},
                {"role": "user", "content": feedback},
            ]
        )
        self.state.execution_context.append(feedback)
        validator_data = (
            validation_error_to_data(error.validation_error)
            if isinstance(error, _WorkflowToolValidationError)
            else None
        )
        validation_result = {
            self.failure_kind: {
                "action": json.loads(signature),
                "message": str(error),
                "corrective_instructions": feedback,
                **({"validator": validator_data} if validator_data else {}),
            }
        }
        if self.failure_kind == "validation_error":
            self.state.transcript.append(
                {"role": "user", "content": json.dumps(validation_result)}
            )
        self.state.execution_events.append(
            {
                "kind": self.failure_kind,
                "action_kind": action.kind,
                "error": str(error),
                "result": validation_result,
                "step_index": self.state.step_index,
            }
        )
        self.last_validation_error = (
            validator_data["message"] if validator_data is not None else None
        )

    def action_failure_exit_code(self, action: SkillChatAction) -> int:
        _ = action
        print("Workflow stopped after repeated action failures.", file=self.stderr)
        return 1

    def observe_outcome(
        self,
        action: SkillChatAction,
        observation: WorkflowActionObservation,
        outcome: WorkflowActionOutcome,
    ) -> WorkflowActionOutcome:
        if not observation.made_progress:
            if observation.decision == ProgressDecision.THRESHOLD:
                if action.kind != "invoke_tool":
                    print(
                        "Workflow stopped after repeated roundtrips without progress.",
                        file=self.stderr,
                    )
                    return WorkflowActionOutcome(exit_code=1)
                if _workflow_step_requires_pull_request(self.current_step):
                    warning = (
                        "Warning: a required pull-request creation step made no "
                        "progress; the workflow cannot skip PR creation."
                    )
                    print(warning, file=self.stderr)
                    return WorkflowActionOutcome(exit_code=1)
                warning = (
                    "Warning: repeated tool action made no progress; skipping to "
                    "the next workflow step."
                )
                parent_skill, parent_step_index = self._parent_progress()
                self.progress.update(
                    self.selected_skill,
                    current_step_index=self.state.step_index,
                    status=warning,
                    parent_skill=parent_skill,
                    parent_step_index=parent_step_index,
                )
                print(warning, file=self.stderr)
                self.state.step_index = self.current_step_index + 1
                self.driver.action_engine.reset_progress()
                if not self.skill_stack and self.state.step_index >= len(
                    self.selected_skill.skill.steps
                ):
                    outcome = WorkflowActionOutcome(continue_running=False)
        if self.state.step_index != self.current_step_index:
            self.step_roundtrips = 0
            self.driver.action_engine.reset_progress()
        if not outcome.continue_running:
            parent_skill, parent_step_index = self._parent_progress()
            self.progress.update(
                self.selected_skill,
                current_step_index=len(self.selected_skill.skill.steps),
                status=f"{self.selected_skill.skill.name} skill completed",
                parent_skill=parent_skill,
                parent_step_index=parent_step_index,
            )
        return outcome

    def exhausted_roundtrips_exit_code(self) -> int:
        return 2


@dataclass(frozen=True, slots=True)
class _SkillExecutionFrame:
    parent_skill: SkillCatalogEntry
    parent_step_index: int
    resume_step_index: int
    dependency_key: tuple[str, int, str] | None = None
    parent_model: str = ""
    parent_provider: str = ""
    parent_provider_role: LLMProviderRole = "normal"
    clean_context: bool = False
    parent_transcript: tuple[dict[str, str], ...] | None = None
    parent_execution_events: tuple[dict[str, Any], ...] | None = None
    parent_execution_context: tuple[str, ...] | None = None
    parent_current_file_path: Path | None = None


@dataclass(frozen=True, slots=True)
class WorkflowChatCredentials:
    provider: str
    api_key: str
    source: str
    base_url: str
    base_url_source: str


def _normalize_cache_usage(usage: Mapping[str, Any]) -> dict[str, int] | None:
    """Normalize cache counters returned by OpenAI-compatible providers."""
    prompt_tokens = usage.get("prompt_tokens")
    prompt_details = usage.get("prompt_tokens_details")
    if not isinstance(prompt_details, Mapping):
        prompt_details = {}

    cached_tokens = prompt_details.get("cached_tokens")
    if not isinstance(cached_tokens, int):
        cached_tokens = usage.get("prompt_cache_hit_tokens")
    if not isinstance(cached_tokens, int):
        cached_tokens = 0

    cache_miss_tokens = usage.get("prompt_cache_miss_tokens")
    if not isinstance(cache_miss_tokens, int):
        cache_miss_tokens = (
            prompt_tokens - cached_tokens if isinstance(prompt_tokens, int) else 0
        )

    cache_write_tokens = prompt_details.get("cache_write_tokens")
    if not isinstance(cache_write_tokens, int):
        cache_write_tokens = usage.get("cache_write_tokens")
    if not isinstance(cache_write_tokens, int):
        cache_write_tokens = 0

    if not any((cached_tokens, cache_miss_tokens, cache_write_tokens)):
        return None
    return {
        "prompt_tokens": prompt_tokens if isinstance(prompt_tokens, int) else 0,
        "cached_tokens": cached_tokens,
        "cache_miss_tokens": cache_miss_tokens,
        "cache_write_tokens": cache_write_tokens,
    }


class _LLMExchangeRecordingClient:
    """Record every LLM request and response in the active repository root."""

    def __init__(self, client: WorkflowLLMClient, repo_root: Path) -> None:
        self._client = client
        self._repo_root = repo_root.expanduser().resolve()

    def complete_json(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        try:
            response = _request_json(self._client, messages)
        except Exception as exc:
            serialized_messages = _client_serialized_messages(
                self._client,
                messages,
            )
            self._write_exchange(
                messages,
                {
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                },
                serialized_messages=serialized_messages,
            )
            raise
        self._write_exchange(
            messages,
            response,
            serialized_messages=_client_serialized_messages(self._client, messages),
        )
        return response

    def _write_exchange(
        self,
        messages: list[dict[str, str]],
        output: object,
        *,
        serialized_messages: str | None = None,
    ) -> None:
        timestamp = datetime.now(UTC)
        timestamp_text = timestamp.strftime("%Y%m%d-%H%M%S-%f")
        output_path = self._repo_root / f"llm-{timestamp_text}.json"
        suffix = 1
        while output_path.exists():
            output_path = self._repo_root / f"llm-{timestamp_text}-{suffix}.json"
            suffix += 1
        serialized_messages = serialized_messages or _serialize_messages(messages)
        exchange: dict[str, Any] = {
            "timestamp": timestamp.isoformat(),
            "output": output,
        }
        usage = getattr(self._client, "last_usage", None)
        if isinstance(usage, Mapping):
            cache_usage = _normalize_cache_usage(usage)
            if cache_usage is not None:
                exchange["usage"] = cache_usage
        output_path.write_text(
            _serialize_exchange(
                exchange,
                serialized_messages=serialized_messages,
            ),
            encoding="utf-8",
        )


def _serialize_exchange(
    exchange: Mapping[str, Any],
    *,
    serialized_messages: str,
) -> str:
    """Serialize an exchange without encoding the large input twice."""
    lines = [
        "{",
        f'  "timestamp": {json.dumps(exchange["timestamp"], ensure_ascii=False)},',
        f'  "input": {serialized_messages},',
        '  "output": ' + json.dumps(exchange["output"], indent=2, ensure_ascii=False),
    ]
    if "usage" in exchange:
        lines[-1] += ","
        lines.append(
            '  "usage": ' + json.dumps(exchange["usage"], indent=2, ensure_ascii=False)
        )
    lines.append("}")
    return "\n".join(lines) + "\n"


def _client_serialized_messages(
    client: object,
    messages: list[dict[str, str]],
) -> str:
    serialized_messages = getattr(client, "last_serialized_messages", None)
    if isinstance(serialized_messages, str):
        return serialized_messages
    return _serialize_messages(messages)


class OpenAIChatClient:
    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        base_url: str,
        timeout: float = 120.0,
        limits: LLMModelLimits | None = None,
        progress_stream: TextIO | None = None,
    ) -> None:
        self._model = model
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._limits = limits or _DEFAULT_MODEL_LIMITS
        self._progress_stream = progress_stream
        self.last_usage: dict[str, Any] = {}
        self.last_serialized_messages: str | None = None

    def complete_json(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        serialized_messages = _serialize_messages(messages)
        self.last_serialized_messages = serialized_messages
        max_tokens, estimated_input_tokens = _request_token_budget(
            messages,
            self._limits,
            serialized_messages=serialized_messages,
        )
        payload = {
            "model": self._model,
            "messages": messages,
            "temperature": 0,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
            "stream": True,
        }
        request = Request(
            f"{self._base_url}/chat/completions",
            data=_serialize_openai_payload(
                payload,
                serialized_messages=serialized_messages,
            ).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        request_started = time.monotonic()
        try:
            with urlopen(request, timeout=self._timeout) as response:
                raw_response = _read_openai_response(
                    response,
                    progress_stream=self._progress_stream,
                )
        except HTTPError as exc:
            raise RuntimeError(
                "OpenAI request failed with HTTP "
                f"{exc.code}: {exc.read().decode('utf-8', errors='replace')}"
            ) from exc
        except URLError as exc:
            raise RuntimeError(f"OpenAI request failed: {exc.reason}") from exc
        except TimeoutError as exc:
            raise RuntimeError(
                _provider_timeout_message(
                    provider="OpenAI-compatible",
                    model=self._model,
                    endpoint=request.full_url,
                    timeout=self._timeout,
                    elapsed=time.monotonic() - request_started,
                    message=str(exc),
                    message_count=len(messages),
                    max_tokens=max_tokens,
                    estimated_input_tokens=estimated_input_tokens,
                )
            ) from exc

        loaded_response = _parse_json_object(
            raw_response,
            "OpenAI response",
        )
        usage = loaded_response.get("usage")
        self.last_usage = dict(usage) if isinstance(usage, dict) else {}
        choices = loaded_response.get("choices")
        if not isinstance(choices, list) or not choices:
            raise RuntimeError("OpenAI response did not include any choices.")
        first_choice = choices[0]
        if not isinstance(first_choice, dict):
            raise RuntimeError("OpenAI response choice was not an object.")
        message = first_choice.get("message")
        if not isinstance(message, dict):
            raise RuntimeError("OpenAI response choice message was not an object.")
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            raise RuntimeError("OpenAI response message content was empty.")

        return _parse_json_object(content, "OpenAI response content")


def _read_openai_response(
    response: Any,
    *,
    progress_stream: TextIO | None,
) -> str:
    """Read a streamed OpenAI response and return its normal response shape.

    Providers sometimes ignore ``stream=true`` (and test doubles commonly do
    too), so a regular JSON response is still accepted. ``readline`` is used
    for SSE responses so each completed event is consumed as soon as it is
    available; the socket timeout therefore applies to inactivity between
    events rather than waiting for the entire generation to finish.
    """
    content_type = ""
    headers = getattr(response, "headers", None)
    if headers is not None:
        content_type = str(headers.get("Content-Type", "")).casefold()
    if "text/event-stream" not in content_type or not hasattr(response, "readline"):
        return response.read().decode("utf-8")

    content_parts: list[str] = []
    response_metadata: dict[str, Any] | None = None
    event_data: list[str] = []
    chunk_count = 0
    while True:
        line = response.readline()
        if not line:
            break
        decoded_line = line.decode("utf-8", errors="replace").rstrip("\r\n")
        if decoded_line:
            if decoded_line.startswith("data:"):
                event_data.append(decoded_line[5:].lstrip())
            continue
        if not event_data:
            continue
        event_payload = "\n".join(event_data)
        event_data.clear()
        if event_payload == "[DONE]":
            break
        try:
            event = json.loads(event_payload)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"OpenAI streaming response contained invalid JSON: {exc.msg}"
            ) from exc
        if not isinstance(event, dict):
            continue
        if response_metadata is None:
            response_metadata = event
        choices = event.get("choices")
        if not isinstance(choices, list) or not choices:
            continue
        first_choice = choices[0]
        if not isinstance(first_choice, dict):
            continue
        delta = first_choice.get("delta")
        if not isinstance(delta, dict):
            continue
        content = delta.get("content")
        if isinstance(content, str):
            content_parts.append(content)
            chunk_count += 1
            if progress_stream is not None:
                print(
                    f"received streamed LLM data ({chunk_count} chunks)...",
                    file=progress_stream,
                    flush=True,
                )

    if response_metadata is None:
        raise RuntimeError("OpenAI streaming response did not include any events.")
    if not content_parts:
        raise RuntimeError("OpenAI streaming response content was empty.")
    response_metadata["choices"] = [{"message": {"content": "".join(content_parts)}}]
    return json.dumps(response_metadata)


class LocalLlamaChatClient:
    def __init__(
        self,
        *,
        model_path: Path,
        n_ctx: int = _DEFAULT_LOCAL_MODEL_CONTEXT,
    ) -> None:
        try:
            from llama_cpp import (  # type: ignore[import-not-found]
                Llama,
                llama_supports_gpu_offload,
            )
        except ImportError as exc:
            raise RuntimeError(
                "Local provider requires llama-cpp-python. Install the local "
                "extra (with Metal support on macOS): "
                "CMAKE_ARGS='-DGGML_METAL=on' uv sync --extra local."
            ) from exc
        if not model_path.is_file():
            raise RuntimeError(f"Local GGUF model file does not exist: {model_path}")
        if "q5_k_m" not in model_path.name.casefold():
            raise RuntimeError(
                "Local Qwen model must be the Q5_K_M GGUF variant; expected a "
                "model filename containing 'q5_k_m'."
            )
        if not llama_supports_gpu_offload():
            raise RuntimeError(
                "Local model execution requires GPU offload support, but the "
                "installed llama-cpp-python build cannot use a GPU. Reinstall "
                "the local extra with Metal or CUDA support."
            )
        try:
            self._llama: Any = Llama(
                model_path=str(model_path),
                n_ctx=n_ctx,
                n_gpu_layers=-1,
                verbose=False,
            )
        except Exception as exc:
            raise LocalModelRuntimeError(
                "Local Qwen GPU model failed to initialize. The model was "
                "required to offload all layers to the GPU; no CPU fallback "
                f"is allowed. Model={model_path}, context={n_ctx}. "
                f"Underlying error: {exc}"
            ) from exc

    def complete_json(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        try:
            response = self._llama.create_chat_completion(
                messages=messages,
                temperature=0,
                max_tokens=_MAX_COMPLETION_TOKENS,
                response_format={"type": "json_object"},
            )
        except Exception as exc:
            raise LocalModelRuntimeError(
                "Local Qwen GPU inference failed. The workflow cannot continue "
                "with a CPU fallback. Check Metal/CUDA availability, GPU memory, "
                f"and POWDRR_LOCAL_MODEL_CONTEXT. Underlying error: {exc}"
            ) from exc
        choices = response.get("choices")
        if not isinstance(choices, list) or not choices:
            raise RuntimeError("Local LLM response did not include any choices.")
        first_choice = choices[0]
        if not isinstance(first_choice, dict):
            raise RuntimeError("Local LLM response choice was not an object.")
        message = first_choice.get("message")
        if not isinstance(message, dict):
            raise RuntimeError("Local LLM response message was not an object.")
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            raise RuntimeError("Local LLM response content was empty.")
        return _parse_json_object(content, "Local LLM response content")


class AnthropicChatClient:
    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        base_url: str,
        timeout: float = 120.0,
        api_version: str = "2023-06-01",
        limits: LLMModelLimits | None = None,
    ) -> None:
        self._model = model
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._api_version = api_version
        self._limits = limits or _DEFAULT_MODEL_LIMITS
        self.last_serialized_messages: str | None = None

    def complete_json(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        serialized_messages = _serialize_messages(messages)
        self.last_serialized_messages = serialized_messages
        system_prompt, conversation_messages = _split_system_message(messages)
        conversation_messages = [
            _anthropic_message(message) for message in conversation_messages
        ]
        serialized_conversation_messages = _serialize_messages(conversation_messages)
        max_tokens, estimated_input_tokens = _request_token_budget(
            messages,
            self._limits,
            serialized_messages=serialized_messages,
        )
        request = Request(
            f"{self._base_url}/v1/messages",
            data=_serialize_anthropic_payload(
                model=self._model,
                max_tokens=max_tokens,
                serialized_messages=serialized_conversation_messages,
                system_prompt=system_prompt,
            ).encode("utf-8"),
            headers={
                "x-api-key": self._api_key,
                "anthropic-version": self._api_version,
                "content-type": "application/json",
            },
            method="POST",
        )
        request_started = time.monotonic()
        try:
            with urlopen(request, timeout=self._timeout) as response:
                raw_response = response.read().decode("utf-8")
        except HTTPError as exc:
            raise RuntimeError(
                "Anthropic request failed with HTTP "
                f"{exc.code}: {exc.read().decode('utf-8', errors='replace')}"
            ) from exc
        except URLError as exc:
            raise RuntimeError(f"Anthropic request failed: {exc.reason}") from exc
        except TimeoutError as exc:
            raise RuntimeError(
                _provider_timeout_message(
                    provider="Anthropic",
                    model=self._model,
                    endpoint=request.full_url,
                    timeout=self._timeout,
                    elapsed=time.monotonic() - request_started,
                    message=str(exc),
                    message_count=len(conversation_messages),
                    max_tokens=max_tokens,
                    estimated_input_tokens=estimated_input_tokens,
                )
            ) from exc

        loaded_response = _parse_json_object(
            raw_response,
            "Anthropic response",
        )
        content = loaded_response.get("content")
        if not isinstance(content, list) or not content:
            raise RuntimeError("Anthropic response did not include any content.")

        text_parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") != "text":
                continue
            text = block.get("text")
            if isinstance(text, str) and text.strip():
                text_parts.append(text)

        response_text = "".join(text_parts).strip()
        if not response_text:
            raise RuntimeError("Anthropic response content was empty.")

        return _parse_json_object(response_text, "Anthropic response content")


def _provider_timeout_message(
    *,
    provider: str,
    model: str,
    endpoint: str,
    timeout: float,
    elapsed: float,
    message: str,
    message_count: int,
    max_tokens: int,
    estimated_input_tokens: int,
) -> str:
    return (
        f"{provider} request timed out for model {model!r}: {message}. "
        f"Elapsed {elapsed:.1f}s of configured {timeout:g}s timeout; "
        f"endpoint={endpoint!r}, messages={message_count}, "
        f"estimated_input_tokens={estimated_input_tokens}, "
        f"max_tokens={max_tokens}."
    )


def _serialize_messages(messages: Sequence[Mapping[str, str]]) -> str:
    return json.dumps(messages, ensure_ascii=False, separators=(",", ":"))


def _serialize_openai_payload(
    payload: Mapping[str, Any],
    *,
    serialized_messages: str,
) -> str:
    return (
        "{"
        + '"model":'
        + json.dumps(payload["model"], ensure_ascii=False)
        + ',"messages":'
        + serialized_messages
        + ',"temperature":'
        + json.dumps(payload["temperature"])
        + ',"max_tokens":'
        + json.dumps(payload["max_tokens"])
        + ',"response_format":'
        + json.dumps(payload["response_format"], separators=(",", ":"))
        + ',"stream":'
        + json.dumps(payload["stream"])
        + "}"
    )


def _serialize_anthropic_payload(
    *,
    model: str,
    max_tokens: int,
    serialized_messages: str,
    system_prompt: str | None,
) -> str:
    serialized = (
        "{"
        + '"model":'
        + json.dumps(model, ensure_ascii=False)
        + ',"max_tokens":'
        + json.dumps(max_tokens)
        + ',"messages":'
        + serialized_messages
    )
    if system_prompt is not None:
        serialized += ',"system":' + json.dumps(system_prompt, ensure_ascii=False)
    return serialized + "}"


def _request_token_budget(
    messages: list[dict[str, str]],
    limits: LLMModelLimits,
    *,
    serialized_messages: str | None = None,
) -> tuple[int, int]:
    estimated_input_tokens = _estimate_message_tokens(
        messages,
        serialized_messages=serialized_messages,
    )
    available_output_tokens = (
        limits.context_window - estimated_input_tokens - _CONTEXT_SAFETY_MARGIN_TOKENS
    )
    if available_output_tokens < 1:
        raise RuntimeError(
            "Model context window is exhausted: "
            f"estimated input is {estimated_input_tokens} tokens, "
            f"context window is {limits.context_window} tokens."
        )
    return (
        min(_MAX_COMPLETION_TOKENS, limits.max_output_tokens, available_output_tokens),
        estimated_input_tokens,
    )


def _estimate_message_tokens(
    messages: list[dict[str, str]],
    *,
    serialized_messages: str | None = None,
) -> int:
    serialized = serialized_messages or _serialize_messages(messages)
    return max(
        1,
        math.ceil(len(serialized) / _TOKEN_ESTIMATE_CHARS_PER_TOKEN),
    )


class _ModelUnavailableError(RuntimeError):
    pass


class _EmptyProviderResponseError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        messages: Sequence[dict[str, str]] | None = None,
    ) -> None:
        super().__init__(message)
        self.messages = messages


class LocalModelRuntimeError(RuntimeError):
    """Raised when the required local GPU model cannot run."""


class _WorkflowEditRangeError(RuntimeError):
    """Raised when a line-based edit falls outside the current file."""


class _WorkflowStructuredDocumentError(RuntimeError):
    """Raised when an edit produces invalid structured document text."""


class _WorkflowYamlEditError(RuntimeError):
    """Raised when a structural YAML edit cannot be applied safely."""


class _WorkflowToolValidationError(RuntimeError):
    def __init__(self, validation_error: ValidationError) -> None:
        self.validation_error = validation_error
        super().__init__(validation_error.message)


class _WorkflowProgressDisplay:
    def __init__(
        self,
        stream: TextIO,
        on_update: Callable[..., None] | None = None,
    ) -> None:
        self._stream = stream
        self._on_update = on_update
        self._dynamic = stream.isatty()
        self._rendered_line_count = 0
        self._last_step_index: int | None = None

    def update(
        self,
        skill: SkillCatalogEntry,
        *,
        current_step_index: int,
        status: str,
        parent_skill: SkillCatalogEntry | None = None,
        parent_step_index: int | None = None,
    ) -> None:
        if self._on_update is not None:
            if len(inspect.signature(self._on_update).parameters) >= 5:
                self._on_update(
                    skill,
                    current_step_index,
                    status,
                    parent_skill,
                    parent_step_index,
                )
            else:
                self._on_update(skill, current_step_index, status)
            self._last_step_index = current_step_index
            return
        if not self._dynamic and self._last_step_index == current_step_index:
            print(f"[workflow] {status}", file=self._stream, flush=True)
            return

        lines = ["Workflow progress:"]
        if parent_skill is not None and parent_step_index is not None:
            lines.append(
                f"  ▶ {parent_step_index + 1}. "
                f"{parent_skill.skill.steps[parent_step_index].description}"
            )
            lines.append("  -------")
        for step_index, step in enumerate(skill.skill.steps):
            if step_index < current_step_index:
                marker = "✓"
            elif step_index == current_step_index:
                marker = "▶"
            else:
                marker = "·"
            lines.append(f"  {marker} {step_index + 1}. {step.description}")
        lines.append(f"Status: {status}")

        if self._dynamic and self._rendered_line_count:
            self._stream.write(f"\033[{self._rendered_line_count}A")
        for line in lines:
            if self._dynamic:
                self._stream.write(f"\033[2K{line}\n")
            else:
                self._stream.write(f"{line}\n")
        self._stream.flush()
        self._rendered_line_count = len(lines)
        self._last_step_index = current_step_index


def run_workflow_chat(
    config: WorkflowChatConfig,
    *,
    input_func: Callable[[], str] = input,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
    progress_callback: Callable[
        [SkillCatalogEntry, int, str, SkillCatalogEntry | None, int | None], None
    ]
    | None = None,
) -> int:
    configured_repo_root = resolve_repo_root(config.repo_root)
    project_root = configured_repo_root
    workflow_context = _load_workflow_context(project_root)
    skills_dir = config.skills_dir
    if not skills_dir.is_absolute():
        skills_dir = configured_repo_root / skills_dir

    catalog = _load_skill_catalog(skills_dir, stderr=stderr)
    if not catalog:
        print(f"No skills found in {skills_dir}.", file=stderr)
        return 1

    provider_roles = _resolve_provider_roles(config)
    provider_role: LLMProviderRole = "normal"
    current_model = config.model
    provider = provider_roles.provider_for(provider_role)
    current_model = _provider_definition(provider).forced_model or current_model
    credentials = _resolve_credentials(provider, config.api_key, config.base_url)
    clients: dict[tuple[str, str], WorkflowLLMClient] = {}

    def client_for(
        selected_provider: str,
        selected_credentials: WorkflowChatCredentials,
        selected_model: str,
    ) -> WorkflowLLMClient:
        key = (selected_provider, selected_model)
        if key not in clients:
            clients[key] = _LLMExchangeRecordingClient(
                _build_chat_client(
                    selected_credentials,
                    model=selected_model,
                    model_cache_dir=project_root / ".powdrr" / "models",
                    progress_stream=stderr,
                ),
                project_root,
            )
        return clients[key]

    def client_for_model(
        selected_model: str, selected_provider: str
    ) -> WorkflowLLMClient:
        selected_credentials = _resolve_credentials(
            selected_provider,
            config.api_key,
            config.base_url,
        )
        return client_for(selected_provider, selected_credentials, selected_model)

    print(
        f"Using {credentials.provider} credentials from {credentials.source} "
        f"with base URL from {credentials.base_url_source}: {credentials.base_url}",
        file=stderr,
    )
    if config.provider == "auto" and provider_roles.adversarial is None:
        print(
            "WARNING: only one LLM provider is configured; reviews might be "
            "limited because adversarial work will use the normal provider.",
            file=stderr,
        )
    _verbose_print(
        stderr,
        config.verbose,
        f"Loaded {len(catalog)} skill(s) from {skills_dir}",
    )
    _verbose_print(stderr, config.verbose, f"Selected provider: {provider}")
    _verbose_print(stderr, config.verbose, f"Selected model: {config.model}")

    user_request = _prompt_user(
        "What do you want to do? ",
        input_func=input_func,
        stdout=stdout,
        status_stream=stderr,
    )
    transcript: list[dict[str, str]] = [{"role": "user", "content": user_request}]
    _verbose_print(stderr, config.verbose, f"Initial user request: {user_request}")
    selected_skill: SkillCatalogEntry | None = None
    selection: SkillChatSelection | None = None
    skill_announced = False

    for _turn in range(config.max_turns):
        _verbose_print(stderr, config.verbose, f"Starting selection turn {_turn + 1}")
        selection, current_model, provider = _complete_json_with_model_fallback(
            client_for=client_for_model,
            messages=_build_selection_messages(
                catalog,
                transcript,
                configured_repo_root,
                workflow_context,
            ),
            parser=lambda payload: _parse_selection_response(payload, catalog),
            context="skill selection",
            model=current_model,
            repair_instructions=_selection_repair_prompt(catalog),
            config=config,
            input_func=input_func,
            stdout=stdout,
            stderr=stderr,
            provider=provider,
            model_mappings=_active_llm_mappings(config, provider_roles, provider_role),
        )
        if selection is None:
            return 1
        _verbose_print(
            stderr,
            config.verbose,
            (
                "Selection result: "
                f"skill={selection.selected_skill_path}, "
                f"ready_to_execute={selection.ready_to_execute}"
            ),
        )
        selected_skill = _find_catalog_entry(catalog, selection.selected_skill_path)
        selection_mapping = (
            _resolve_llm_mapping(
                selection.llm_type,
                mappings=_active_llm_mappings(config, provider_roles, provider_role),
                provider=provider,
            )
            if _provider_supports_llm_mappings(provider)
            else None
        )
        if selection_mapping is not None:
            current_model = selection_mapping.model
            provider = _resolve_provider(
                config.provider,
                current_model,
                mapping=selection_mapping,
            )
        credentials = _resolve_credentials(provider, config.api_key, config.base_url)
        if not skill_announced:
            print(f"Matched skill: {selected_skill.skill.name}", file=stdout)
            skill_announced = True
        if selection.ready_to_execute and selection.next_question is None:
            break

        if selection.next_question is None:
            break

        print(selection.next_question, file=stdout)
        answer = _prompt_user(
            "> ",
            input_func=input_func,
            stdout=stdout,
            status_stream=stderr,
        )
        _verbose_print(stderr, config.verbose, f"Follow-up answer: {answer}")
        transcript.append({"role": "assistant", "content": selection.next_question})
        transcript.append({"role": "user", "content": answer})
    else:
        print(
            "Reached the maximum number of skill chat turns without selecting a skill.",
            file=stderr,
        )
        return 1

    if selected_skill is None or selection is None:
        print("Could not select a skill.", file=stderr)
        return 1

    provider_role = (
        "adversarial" if selected_skill.skill.adversarial is True else "normal"
    )

    worktree_root = _resolve_worktree_for_request(
        configured_repo_root,
        request=user_request,
        selected_skill=selected_skill,
        context=workflow_context,
        input_func=input_func,
        stdout=stdout,
        stderr=stderr,
        verbose=config.verbose,
    )
    repo_root = worktree_root
    project_root = _resolve_project_root(configured_repo_root, worktree_root)
    output_dir = config.output_dir
    if output_dir is not None and not output_dir.is_absolute():
        output_dir = repo_root / output_dir

    progress = _WorkflowProgressDisplay(stderr, on_update=progress_callback)
    execution_state = _WorkflowExecutionState(
        selected_skill=selected_skill,
        transcript=transcript,
        execution_events=[],
        execution_context=[],
        step_index=0,
        worktree_root=worktree_root,
    )
    root_skill = selected_skill
    driver = WorkflowLLMExecutionDriver(
        max_stalled_roundtrips=config.max_stalled_roundtrips
    )
    execution_strategy = _ChatWorkflowExecutionStrategy(
        config=config,
        selection=selection,
        catalog=catalog,
        workflow_context=workflow_context,
        state=execution_state,
        progress=progress,
        input_func=input_func,
        stdout=stdout,
        stderr=stderr,
        client_for_model=client_for_model,
        provider_roles=provider_roles,
        provider_role=provider_role,
        current_model=current_model,
        provider=provider,
        driver=driver,
    )
    exit_code = driver.run(
        execution_strategy,
        max_roundtrips=None,
        signature=_workflow_action_signature,
    )
    if exit_code != 0:
        return exit_code
    selected_skill = execution_strategy.selected_skill
    progress.update(
        root_skill,
        current_step_index=len(root_skill.skill.steps),
        status=f"{root_skill.skill.name} skill completed",
    )

    summary = _build_skill_execution_summary(
        root_skill,
        selection,
        execution_state.transcript,
        execution_state.execution_events,
    )
    _verbose_print(
        stderr,
        config.verbose,
        f"Prepared execution summary for {selected_skill.skill.name}",
    )

    output_dir = (
        output_dir
        if output_dir is not None
        else Path(tempfile.mkdtemp(prefix="powdrr-lift-skill-chat-"))
    )
    _verbose_print(stderr, config.verbose, f"Writing skill summary to {output_dir}")
    summary_path = _write_skill_summary(summary, output_dir)
    _verbose_print(
        stderr,
        config.verbose,
        f"Summary written to {summary_path}",
    )
    _persist_workflow_context(
        project_root,
        worktree_root,
        skill_name=root_skill.skill.name,
        request=user_request,
    )

    if config.output_dir is None:
        print(
            json.dumps(
                {
                    "selected_skill_file": str(selected_skill.path),
                    "summary_path": str(summary_path),
                    "summary": summary,
                },
                indent=2,
                ensure_ascii=False,
            ),
            file=stdout,
        )
    else:
        print(f"Wrote skill execution summary to {summary_path}", file=stdout)

    return 0


def _load_skill_catalog(
    skills_dir: Path,
    *,
    stderr: TextIO,
) -> tuple[SkillCatalogEntry, ...]:
    resolved_dir = skills_dir.expanduser().resolve()
    if not resolved_dir.exists():
        print(f"Skill directory does not exist: {resolved_dir}", file=stderr)
        return ()
    if not resolved_dir.is_dir():
        print(f"Skill path is not a directory: {resolved_dir}", file=stderr)
        return ()

    report = build_skill_directory_validation_report(resolved_dir)
    if not report.validation_successful:
        for issue in report.issues:
            print(f"{issue.path}: {issue.code}: {issue.message}", file=stderr)
        return ()

    skill_paths = tuple(
        skill_path
        for pattern in ("*.yaml", "*.yml", "*.json")
        for skill_path in sorted(resolved_dir.glob(pattern))
        if skill_path.is_file()
    )
    skills = load_skills(resolved_dir)
    entries = tuple(
        SkillCatalogEntry(path=skill_path, skill=skill)
        for skill_path, skill in zip(skill_paths, skills, strict=False)
    )

    return entries


def _load_workflow_template_catalog(
    templates_dir: Path,
    *,
    stderr: TextIO,
) -> tuple[SkillCatalogEntry, ...]:
    return _load_skill_catalog(templates_dir, stderr=stderr)


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
                indent=2,
                ensure_ascii=False,
            ),
        },
    ]


def _write_skill_summary(summary: dict[str, Any], output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    summary_path = output_dir / "skill-execution.json"
    summary_path.write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return summary_path


def _parse_selection_response(
    payload: dict[str, Any],
    catalog: Sequence[SkillCatalogEntry],
) -> SkillChatSelection:
    selected_skill_path_value = payload.get("selected_skill_path")
    if not isinstance(selected_skill_path_value, str) or not selected_skill_path_value:
        raise RuntimeError("Skill selection response must include selected_skill_path.")
    selected_skill_path = _resolve_skill_path(selected_skill_path_value, catalog)
    selected_skill_reason = payload.get("selected_skill_reason")
    if not isinstance(selected_skill_reason, str) or not selected_skill_reason:
        raise RuntimeError(
            "Skill selection response must include selected_skill_reason."
        )
    next_question = payload.get("next_question")
    if next_question is not None and not isinstance(next_question, str):
        raise RuntimeError("Skill selection response next_question must be a string.")
    if next_question is not None:
        next_question = _validate_user_question(
            next_question,
            field_name="Skill selection response next_question",
        )
    ready_to_execute_value = payload.get("ready_to_execute")
    if not isinstance(ready_to_execute_value, bool):
        raise RuntimeError(
            "Skill selection response ready_to_execute must be a boolean."
        )
    ready_to_execute = ready_to_execute_value
    if ready_to_execute and next_question is not None:
        raise RuntimeError(
            "Skill selection response must not include next_question when ready."
        )
    if not ready_to_execute and next_question is None:
        raise RuntimeError(
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
        raise RuntimeError(
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
    raise RuntimeError(
        f"Skill selection response referenced unknown skill {skill_path_value!r}."
    )


def _resolve_template_path(
    template_path_value: str,
    catalog: Sequence[SkillCatalogEntry],
) -> Path:
    return _resolve_skill_path(template_path_value, catalog)


def _available_work_item_names(worktree_root: Path) -> tuple[str, ...]:
    specifications_root = worktree_root / "docs" / "specs"
    if not specifications_root.is_dir():
        return ()
    return tuple(
        sorted(
            path.name
            for path in specifications_root.iterdir()
            if path.is_dir() and not path.name.startswith(".")
        )
    )


def _available_work_item_documents(
    worktree_root: Path,
    work_item_name: str,
) -> tuple[str, ...]:
    work_item_root = worktree_root / "docs" / "specs" / work_item_name
    if not work_item_root.is_dir():
        return ()
    return tuple(
        sorted(
            str(path.relative_to(worktree_root))
            for path in work_item_root.rglob("*")
            if path.is_file()
        )
    )


def _match_work_item_names(
    transcript: Sequence[dict[str, str]],
    work_item_names: Sequence[str],
) -> tuple[str, ...]:
    request_text = " ".join(
        message.get("content", "")
        for message in transcript
        if message.get("role") == "user"
    )
    request_tokens = _work_item_name_tokens(request_text)
    matches: list[str] = []
    for work_item_name in work_item_names:
        name_tokens = _work_item_name_tokens(work_item_name)
        if not name_tokens:
            continue
        token_count = len(name_tokens)
        contiguous_match = any(
            request_tokens[index : index + token_count] == name_tokens
            for index in range(len(request_tokens) - token_count + 1)
        )
        if contiguous_match or (
            token_count > 1 and all(token in request_tokens for token in name_tokens)
        ):
            matches.append(work_item_name)
    return tuple(matches)


def _work_item_name_tokens(value: str) -> tuple[str, ...]:
    return tuple(re.findall(r"[a-z0-9]+", value.casefold()))


def _normalize_skill_path_value(value: str) -> str:
    return value.strip().rstrip(".").rstrip()


def _path_without_suffix(value: str) -> str:
    return str(Path(value.rstrip(".")).with_suffix(""))


def _verbose_print(stderr: TextIO, verbose: bool, message: str) -> None:
    if verbose:
        print(f"[verbose] {message}", file=stderr)


def _write_agent_error(repo_root: Path, message: str) -> None:
    """Persist the latest failure context without hiding the original error."""
    try:
        (repo_root / "agent_error.txt").write_text(
            message.rstrip() + "\n", encoding="utf-8"
        )
    except OSError:
        return


def _verbose_json(
    stderr: TextIO,
    verbose: bool,
    label: str,
    value: object,
) -> None:
    if not verbose:
        return
    serialized = json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True)
    for line_number, line in enumerate(serialized.splitlines()):
        prefix = f"{label}: " if line_number == 0 else "  "
        _verbose_print(stderr, verbose, f"{prefix}{line}")


def _workflow_context_path(project_root: Path) -> Path:
    return project_root / _WORKFLOW_CONTEXT_PATH


def _load_workflow_context(project_root: Path) -> WorkflowContext | None:
    path = _workflow_context_path(project_root)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    worktree_value = payload.get("worktree_root")
    if not isinstance(worktree_value, str) or not worktree_value:
        return None
    pr_number = payload.get("pr_number")
    if not isinstance(pr_number, int):
        pr_number = None
    return WorkflowContext(
        worktree_root=Path(worktree_value).expanduser().resolve(),
        branch_name=payload.get("branch_name")
        if isinstance(payload.get("branch_name"), str)
        else None,
        pr_number=pr_number,
        pr_url=payload.get("pr_url")
        if isinstance(payload.get("pr_url"), str)
        else None,
        skill_name=payload.get("skill_name")
        if isinstance(payload.get("skill_name"), str)
        else None,
        request=payload.get("request")
        if isinstance(payload.get("request"), str)
        else None,
    )


def _persist_workflow_context(
    project_root: Path,
    worktree_root: Path,
    *,
    skill_name: str,
    request: str,
) -> None:
    if not (worktree_root / ".git").exists():
        return
    branch_name: str | None = None
    pr_number: int | None = None
    pr_url: str | None = None
    try:
        branch_result = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=worktree_root,
            check=False,
            capture_output=True,
            text=True,
        )
        branch_name = branch_result.stdout.strip() or None
        pr_result = subprocess.run(
            ["gh", "pr", "view", "--json", "number,url"],
            cwd=worktree_root,
            check=False,
            capture_output=True,
            text=True,
        )
        if pr_result.returncode == 0:
            pr_payload = json.loads(pr_result.stdout)
            if isinstance(pr_payload, dict):
                value = pr_payload.get("number")
                pr_number = value if isinstance(value, int) else None
                url = pr_payload.get("url")
                pr_url = url if isinstance(url, str) else None
    except (OSError, json.JSONDecodeError):
        pass
    context = WorkflowContext(
        worktree_root=worktree_root,
        branch_name=branch_name,
        pr_number=pr_number,
        pr_url=pr_url,
        skill_name=skill_name,
        request=request,
    )
    path = _workflow_context_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(context.to_data(), indent=2) + "\n", encoding="utf-8")


def _can_reuse_workflow_context(context: WorkflowContext | None) -> bool:
    return bool(
        context
        and context.worktree_root.exists()
        and _is_dedicated_worktree(context.worktree_root)
    )


def _workflow_context_pr_is_closed(context: WorkflowContext | None) -> bool:
    """Return whether the saved workflow PR has been closed or merged.

    A failed GitHub lookup is intentionally treated as unknown so users can
    still choose whether to reuse an otherwise valid worktree.
    """
    if (
        context is None
        or not _can_reuse_workflow_context(context)
        or context.pr_number is None
    ):
        return False
    try:
        result = subprocess.run(
            ["gh", "pr", "view", str(context.pr_number), "--json", "state"],
            cwd=context.worktree_root,
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return False
        payload = json.loads(result.stdout)
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict):
        return False
    return payload.get("state") in {"CLOSED", "MERGED"}


def _worktree_reuse_decision(
    request: str,
    selected_skill: SkillCatalogEntry,
    context: WorkflowContext | None,
) -> bool | None:
    if not _can_reuse_workflow_context(context):
        return False
    normalized = request.casefold()
    if any(
        phrase in normalized
        for phrase in ("new worktree", "new branch", "new feature", "start over")
    ):
        return False
    if selected_skill.skill.name in {"handle-ad-hoc", "address-review-comments"}:
        return True
    if any(
        phrase in normalized
        for phrase in (
            "reuse",
            "same worktree",
            "same branch",
            "previous skill",
            "the pr",
            "pull request",
            "review comment",
            "pr comment",
            "continue",
        )
    ):
        return True
    return None


def _resolve_worktree_for_request(
    configured_repo_root: Path,
    *,
    request: str,
    selected_skill: SkillCatalogEntry,
    context: WorkflowContext | None,
    input_func: Callable[[], str],
    stdout: TextIO,
    stderr: TextIO,
    verbose: bool,
) -> Path:
    if _is_dedicated_worktree(configured_repo_root):
        return configured_repo_root
    if _workflow_context_pr_is_closed(context):
        assert context is not None
        _verbose_print(
            stderr,
            verbose,
            f"Previous workflow pull request #{context.pr_number} is closed; "
            "creating a new worktree",
        )
        return _resolve_worktree_context(
            configured_repo_root,
            stderr=stderr,
            verbose=verbose,
        )
    decision = _worktree_reuse_decision(request, selected_skill, context)
    if decision is None:
        answer = _prompt_user(
            "A previous workflow worktree is available. Do you want to reuse it? ",
            input_func=input_func,
            stdout=stdout,
            status_stream=stderr,
        )
        decision = answer.casefold() in {"y", "yes", "reuse", "same", "continue"}
    if decision and context is not None:
        _verbose_print(
            stderr,
            verbose,
            f"Reusing previous workflow worktree at {context.worktree_root}",
        )
        return context.worktree_root
    return _resolve_worktree_context(
        configured_repo_root,
        stderr=stderr,
        verbose=verbose,
    )


def _resolve_worktree_context(
    repo_root: Path | None,
    *,
    stderr: TextIO,
    verbose: bool,
) -> Path:
    resolved_repo_root = resolve_repo_root(repo_root)
    if _is_dedicated_worktree(resolved_repo_root):
        _verbose_print(
            stderr,
            verbose,
            f"Using existing worktree context at {resolved_repo_root}",
        )
        return resolved_repo_root

    branch_name = _generate_worktree_branch_name()
    script_path = resolved_repo_root / "scripts" / "create-worktree.sh"
    if not script_path.is_file():
        raise RuntimeError(
            f"Could not find the worktree creation script at {script_path}."
        )

    _verbose_print(
        stderr,
        verbose,
        f"Creating dedicated worktree with branch {branch_name}",
    )
    process = subprocess.run(
        ["bash", str(script_path), branch_name],
        check=True,
        capture_output=True,
        text=True,
        cwd=resolved_repo_root,
    )
    worktree_path = Path(process.stdout.strip().splitlines()[-1]).expanduser().resolve()
    if not worktree_path.exists():
        raise RuntimeError(
            f"Worktree creation script did not return an existing path: {worktree_path}"
        )
    _verbose_print(stderr, verbose, f"Using dedicated worktree at {worktree_path}")
    return worktree_path


def _resolve_project_root(configured_repo_root: Path, worktree_root: Path) -> Path:
    """Return the primary checkout root used for shared local model storage."""
    if not _is_dedicated_worktree(configured_repo_root):
        return configured_repo_root
    worktree_parts = worktree_root.parts
    worktree_marker = ".worktrees"
    if worktree_marker not in worktree_parts:
        raise RuntimeError(
            f"Could not determine project root for worktree {worktree_root}."
        )
    marker_index = worktree_parts.index(worktree_marker)
    if marker_index == 0:
        raise RuntimeError(
            f"Could not determine project root for worktree {worktree_root}."
        )
    return Path(*worktree_parts[:marker_index])


def _is_dedicated_worktree(repo_root: Path) -> bool:
    return ".worktrees" in repo_root.parts


def _generate_worktree_branch_name() -> str:
    return f"workflow-chat-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S-%f')}"


def _find_catalog_entry(
    catalog: Sequence[SkillCatalogEntry],
    template_path: Path,
) -> SkillCatalogEntry:
    for entry in catalog:
        if entry.path == template_path:
            return entry
    raise RuntimeError(f"Could not find skill {template_path}.")


def _find_skill_by_name(
    catalog: Sequence[SkillCatalogEntry],
    skill_name: str,
) -> SkillCatalogEntry:
    normalized_name = skill_name.strip().casefold()
    for entry in catalog:
        if entry.skill.name.casefold() == normalized_name:
            return entry
    raise RuntimeError(f"Could not find referenced skill {skill_name!r}.")


def _next_skill_dependency(
    skill: SkillCatalogEntry,
    step_index: int,
    completed_dependencies: set[tuple[str, int, str]],
) -> str | None:
    step = skill.skill.steps[step_index]
    for dependency_name in step.uses_skills:
        dependency_key = (str(skill.path), step_index, dependency_name)
        if dependency_key not in completed_dependencies:
            return dependency_name
    return None


def _push_nested_skill(
    stack: list[_SkillExecutionFrame],
    *,
    current_skill: SkillCatalogEntry,
    nested_skill: SkillCatalogEntry,
    parent_step_index: int,
    resume_step_index: int,
    dependency_key: tuple[str, int, str] | None = None,
    parent_model: str = "",
    parent_provider: str = "",
    parent_provider_role: LLMProviderRole = "normal",
    clean_context: bool = False,
    parent_transcript: tuple[dict[str, str], ...] | None = None,
    parent_execution_events: tuple[dict[str, Any], ...] | None = None,
    parent_execution_context: tuple[str, ...] | None = None,
    parent_current_file_path: Path | None = None,
) -> None:
    active_skill_paths = {str(frame.parent_skill.path) for frame in stack}
    active_skill_paths.add(str(current_skill.path))
    if str(nested_skill.path) in active_skill_paths:
        raise RuntimeError(
            f"Recursive skill invocation is not allowed: {nested_skill.skill.name!r}."
        )
    stack.append(
        _SkillExecutionFrame(
            parent_skill=current_skill,
            parent_step_index=parent_step_index,
            resume_step_index=resume_step_index,
            dependency_key=dependency_key,
            parent_model=parent_model,
            parent_provider=parent_provider,
            parent_provider_role=parent_provider_role,
            clean_context=clean_context,
            parent_transcript=parent_transcript,
            parent_execution_events=parent_execution_events,
            parent_execution_context=parent_execution_context,
            parent_current_file_path=parent_current_file_path,
        )
    )


def _active_llm_mappings(
    config: SkillChatConfig,
    provider_roles: LLMProviderRoles,
    role: LLMProviderRole,
) -> tuple[tuple[str, LLMModelMapping], ...]:
    """Return mappings for a role without exposing provider details to callers."""
    provider = provider_roles.provider_for(role)
    mappings = tuple(_default_llm_mappings(provider).items())
    if role == "normal":
        mappings += config.llm_mappings
    return mappings


def _catalog_entry_to_data(entry: SkillCatalogEntry) -> dict[str, Any]:
    return {
        "file": str(entry.path),
        "name": entry.skill.name,
        "adversarial": entry.skill.adversarial,
        "when_to_use": list(entry.skill.when_to_use),
        "steps": [_skill_step_to_data(step) for step in entry.skill.steps],
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


def _build_skill_execution_summary(
    selected_skill: SkillCatalogEntry,
    selection: SkillChatSelection,
    transcript: Sequence[dict[str, str]],
    execution_events: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "selected_skill_file": str(selected_skill.path),
        "selected_skill_name": selected_skill.skill.name,
        "selected_skill_reason": selection.selected_skill_reason,
        "conversation": list(transcript),
        "execution_events": list(execution_events),
        "skill": selected_skill.skill.to_data(),
    }


def _execution_events_for_prompt(
    execution_events: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return the event metadata needed for the next action decision.

    Event results are retained in the full execution summary, but are also
    copied into the transcript or execution context as they are produced.
    Sending both copies on every roundtrip needlessly grows prompts and makes
    large tool results increasingly expensive to serialize. Keep the prompt
    event stream as metadata while leaving the complete event stream intact
    for persistence and diagnostics.
    """
    return prune_execution_events(execution_events, include_results=False)


def _prompt_transcript(
    transcript: Sequence[dict[str, str]],
) -> list[dict[str, str]]:
    """Keep recurring prompts bounded while retaining the complete transcript."""
    if len(transcript) <= _MAX_PROMPT_TRANSCRIPT_ENTRIES:
        return list(transcript)

    first = {
        **transcript[0],
        "content": _truncate_prompt_content(transcript[0].get("content", "")),
    }
    recent = [
        {
            **message,
            "content": _truncate_prompt_content(message.get("content", "")),
        }
        for message in transcript[-(_MAX_PROMPT_TRANSCRIPT_ENTRIES - 2) :]
    ]
    omitted = {
        "role": "user",
        "content": "[Earlier workflow transcript omitted from this prompt; "
        "full history remains in the execution summary.]",
    }
    compacted = [first, omitted, *recent]
    while (
        len(compacted) > 3
        and sum(len(message.get("content", "")) for message in compacted)
        > _MAX_PROMPT_TRANSCRIPT_CHARS
    ):
        compacted.pop(2)
    return compacted


def _truncate_prompt_content(content: str) -> str:
    if len(content) <= _MAX_PROMPT_TRANSCRIPT_MESSAGE_CHARS:
        return content
    half_limit = _MAX_PROMPT_TRANSCRIPT_MESSAGE_CHARS // 2
    return (
        content[:half_limit]
        + "\n... [prompt transcript message truncated] ...\n"
        + content[-half_limit:]
    )


def _build_step_execution_messages(
    *,
    selected_skill: SkillCatalogEntry,
    current_step: Any,
    current_step_index: int,
    transcript: Sequence[dict[str, str]],
    execution_events: Sequence[dict[str, Any]],
    execution_context: Sequence[str],
    current_file_path: Path | None,
    worktree_root: Path,
    catalog: Sequence[SkillCatalogEntry],
    workflow_context: WorkflowContext | None = None,
    current_file_context_cache: dict[tuple[str, int, int], dict[str, Any]]
    | None = None,
) -> list[dict[str, str]]:
    current_file_context = _current_file_context(
        worktree_root,
        current_file_path,
        cache=current_file_context_cache,
    )
    available_work_items = _available_work_item_names(worktree_root)
    available_tools = sorted(
        {
            invocation.tool
            for invocation in current_step.tool_invocations
            if invocation.tool != "ref"
        }
        | {_INTERNAL_TOOL}
    )
    tool_descriptions = {
        "shell": (
            "Execute a shell command in the current worktree. Commands run with "
            "the worktree as cwd; any explicit cwd must remain inside it."
        ),
        _INTERNAL_TOOL: (
            "Execute a powdrr-lift CLI command. This tool is always available, "
            "but its command must invoke only the powdrr-lift binary and runs "
            "with the current worktree as cwd."
        ),
        "fuzzy-match": (
            "Search worktree paths with find-like filters and fuzzy name matching."
        ),
        BASEDPYRIGHT_SYMBOL_TOOL: "Find Python symbols by name across the worktree.",
        BASEDPYRIGHT_STRUCTURE_TOOL: (
            "Discover the classes, functions, methods, and variables in a Python file."
        ),
    }
    return [
        {
            "role": "system",
            "content": _action_system_prompt(),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "execution_mode": "execute_selected_skill",
                    "current_step_index": current_step_index,
                    "current_step_count": len(selected_skill.skill.steps),
                    "current_step": _skill_step_to_data(current_step),
                    "step_context": list(execution_context),
                    "available_tools": [
                        {
                            "name": tool,
                            "description": tool_descriptions.get(tool, tool),
                        }
                        for tool in available_tools
                    ],
                    "available_context_types": [
                        {
                            "name": context_type,
                            "when_to_use": description,
                        }
                        for context_type, description in _context_type_catalog()
                    ],
                    "worktree_root": str(worktree_root),
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
                    },
                    "selected_skill": _catalog_entry_to_data(selected_skill),
                    "available_skills": [
                        {
                            "name": entry.skill.name,
                            "path": str(entry.path),
                            "adversarial": entry.skill.adversarial,
                            "when_to_use": list(entry.skill.when_to_use),
                        }
                        for entry in catalog
                    ],
                    "transcript": _prompt_transcript(transcript),
                    "execution_events": _execution_events_for_prompt(execution_events),
                    "current_file": current_file_context,
                },
                indent=2,
                ensure_ascii=False,
            ),
        },
    ]


def _action_system_prompt() -> str:
    context_type_lines = "\n".join(
        f"- {name}: {description}" for name, description in _context_type_catalog()
    )
    return (
        "Task: execute the current checked-in skill step using the current step, "
        "prior step context, transcript, execution events, available tools, and "
        "current file context in the user message. Choose the single next action "
        "that makes the most progress without asking for information already "
        "available.\n"
        "When current_step.uses_skills is non-empty, those skills run automatically "
        "in the same worktree before you continue the current step. Use invoke_skill "
        "only for an additional listed skill that the current step discovers it "
        "needs.\n"
        "Choose exactly one outcome and use it for the following reason:\n"
        "- gather_context: choose this when checked-in specifications or other "
        "repository context must be discovered before deciding or acting.\n"
        "- prompt_user: choose this only when a specific human decision or fact "
        "is genuinely required to continue; ask exactly one clear question.\n"
        "- edit: choose this when the current file context is sufficient and the "
        "next action is a line-based file change.\n"
        "- invoke_skill: choose this when a listed skill should run as a nested "
        "workflow before continuing. It inherits the current context and LLM "
        "provider role by default, including the current skill's adversarial "
        "role. Pass the current decisions and context to the nested skill; "
        'set provider_role="adversarial" to '
        "run this skill and its descendants with the adversarial provider, or "
        'provider_role="normal" to return to the normal provider. '
        "its descendants. Set clean=true only when the skill must receive only the "
        "explicit context list (and decisions_and_context) and must not return "
        "its gathered context to the caller.\n"
        "- invoke_tool: choose this only when the current step's explicitly "
        "listed tool_invocations support the tool needed for the next action.\n"
        "- read_document: choose this when you know the document path but need "
        "specific lines from that document before deciding the next action. "
        "Request only the smallest useful contiguous range.\n"
        "- next_step: choose this when the current step is complete and the next "
        "skill step should receive the accumulated context.\n"
        "- complete: choose this when the skill has finished and no more action "
        "is required.\n"
        "Response: return exactly one JSON object matching exactly one of these "
        "outcome shapes. Include decisions_and_context when there is information "
        "a later step needs. Include llm_type only when the next roundtrip needs "
        "a different capability; otherwise use null or omit it.\n"
        "Response field requirements by outcome: gather_context requires a non-"
        "empty types array and may include keywords and filters mappings; "
        "prompt_user requires "
        "text containing exactly one clear English question ending in '?'; edit "
        "requires either file_path plus a non-empty edits array or a non-empty "
        "file_edits array, with each edit using add, remove, or replace and valid "
        "line numbers; invoke_tool requires a tool listed in the current step's "
        "tool_invocations and "
        "parameters.command as a non-empty string or string array, except that "
        "basedpyright-symbol takes parameters.query and optional parameters.limit "
        "and basedpyright-structure takes parameters.path; yaml_edit requires "
        "a .yaml or .yml file_path and a non-empty operations array; invoke_skill "
        "takes "
        "a skill name from available_skills; next_step has "
        "no action-specific fields; read_document requires file_path, positive "
        "start_line and positive end_line for a range of at most 2000 lines; "
        "complete may include a human-readable text.\n"
        '{"kind":"gather_context","types":["requirements"],"keywords":["photo"],"filters":{"entity_type":["Service"]},'
        '"decisions_and_context":"...","llm_type":"simple_task"}\n'
        '{"kind":"prompt_user","text":"...","decisions_and_context":"...",'
        '"llm_type":"standard_reasoning"}\n'
        '{"kind":"edit","file_path":"docs/proposals/example/system-specification.yaml",'
        '"edits":[{"kind":"replace","start_line":1,"end_line":2,'
        '"text":"..."}],"decisions_and_context":"...",'
        '"llm_type":"standard_reasoning"}\n'
        "For edits across multiple files, use one edit action with "
        '"file_edits":[{"file_path":"...","edits":[...]}].\n'
        '{"kind":"yaml_edit","file_path":"docs/proposals/example/implementation-specification.yaml",'
        '"operations":[{"op":"upsert_item","section":"features",'
        '"id":"feature-capture","value":{"action":"added",'
        '"description":"Capture interactions",'
        '"functional_requirements":["Store input and output"]}}],'
        '"decisions_and_context":"...","llm_type":"standard_reasoning"}\n'
        '{"kind":"invoke_tool","tool":"shell","parameters":{"command":["..."],"cwd":"...","env":{...}},"decisions_and_context":"...",'
        '"llm_type":"simple_task"}\n'
        '{"kind":"invoke_skill","skill":"bootstrap-code-structure",'
        '"decisions_and_context":"...","llm_type":"standard_reasoning"}\n'
        '{"kind":"invoke_skill","skill":"adversarial-review",'
        '"provider_role":"adversarial","clean":true,'
        '"context":["Review only this diff."],"decisions_and_context":"..."}\n'
        '{"kind":"read_document","file_path":"docs/proposals/example/system-specification.yaml",'
        '"start_line":1,"end_line":80,"decisions_and_context":"...",'
        '"llm_type":"long_context"}\n'
        '{"kind":"next_step","decisions_and_context":"...",'
        '"llm_type":"standard_reasoning"}\n'
        '{"kind":"complete","text":"...","decisions_and_context":"...",'
        '"llm_type":"high_reasoning"}\n'
        "Use gather_context when you need to discover information already "
        "specified in checked-in specs before deciding the next action.\n"
        "Use gather_context to discover what requirements are already "
        "specified, find related entities, inspect approach notes, or gather "
        "current features, decisions, risks, or proposed PRs.\n"
        "The supported context types are:\n"
        f"{context_type_lines}\n"
        "Use keywords to narrow results to items that mention one or more "
        "words. Use filters for exact field matching, such as "
        '{"entity_type":["Tool"],"labels":["python"]}.\n'
        "Do not use filters.work_item_name. Work-item scope comes from the "
        "document path and the current work-item context; gather_context "
        "already searches the relevant local and checked-in documents. Use "
        "keywords or item fields to narrow results within that scope.\n"
        "Use prompt_user only when you need more information to continue "
        "executing the current step.\n"
        "When work_item_context contains matches, treat those names as the "
        "canonical existing work items. Normalize case, spaces, underscores, "
        "and hyphens when matching the user's wording, reuse the exact "
        "canonical name, and do not ask the user to repeat it. Only ask for "
        "a work-item name when no available work item is a reasonable match "
        "and a new item is genuinely required.\n"
        "Do not ask for information already present in the transcript or "
        "execution context. Every prompt_user action must include a concise, "
        "properly formed English question in text. The question must contain "
        "meaningful words, cannot be empty or only whitespace, and must end "
        "with a question mark; never return an instruction or placeholder.\n"
        "Use edit when you know the current file should be changed and you "
        "have enough context to describe line-based removals, additions, or "
        "replacements.\n"
        "Use yaml_edit for YAML specification files. It preserves section keys "
        "and edits list items structurally: upsert_item uses section, id, and a "
        "complete value mapping; remove_item uses section and id; set_value uses "
        "a mapping-key path and value. If yaml_edit reports a usage error, "
        "follow its corrective instructions and retry with the corrected shape.\n"
        "For YAML or JSON edits, preserve the surrounding document structure. "
        "When replacing a list item, start at the list item rather than its "
        "mapping key (for example, preserve `entities:` above `- id: ...`). "
        "For prose values containing embedded double quotes, colons, or other "
        "YAML-sensitive punctuation, use a single-quoted scalar or a `>-` "
        "block scalar; never place unescaped double quotes inside a double-"
        "quoted YAML value. "
        "After composing all line edits, ensure the complete resulting document "
        "remains valid before returning the action.\n"
        "When edit is available, current_file includes the file path and its "
        "current contents as context.\n"
        "Use invoke_skill for a listed nested skill; it runs in the same worktree "
        "and returns here when complete. Use invoke_tool for shell commands, "
        "fuzzy-match searches, or basedpyright "
        "symbol and structure queries.\n"
        "When a tool result reports validation failure, a non-zero validation "
        "status, or structured validation errors with corrective_action, do "
        "not invoke the same validation command again unchanged. First use the "
        "reported corrective_action to edit the affected document or gather the "
        "missing context; rerun validation only after a corrective action has "
        "changed or clarified the input.\n"
        "Use read_document instead of requesting or embedding an entire large "
        "document when only a section is needed. The returned line-numbered "
        "excerpt will be included in the next roundtrip context.\n"
        "The fuzzy-match tool executes in Python and returns structured JSON. "
        "Its command array starts with fuzzy-match followed by a search root and "
        "supports -name/-iname, -path/-ipath, -type f|d, -maxdepth, -mindepth, "
        "-threshold, and -print. Use -name for the natural-language query; it is "
        "fuzzy matched rather than treated as an exact glob.\n"
        "Before asking whether existing proposed PR specifications should be "
        "used, invoke fuzzy-match in the current feature specification directory "
        "with a query such as 'proposed PR specification'. Ask only after the "
        "tool result establishes whether matching files exist.\n"
        "For start-implementing-feature, the workflow template path is known and "
        "fixed: templates/execute-proposed-pr.yaml. Use it directly when invoking "
        "instantiate-workflow and never ask the user to supply or choose that path.\n"
        "A missing execute workflow is expected during start-implementing-feature: "
        "this skill creates it. If fuzzy-match finds no matching workflow, invoke "
        "instantiate-workflow immediately rather than asking the user for one.\n"
        "When the current step includes tool_invocations, choose one of those "
        "structured invocations and fill in its parameters unless the task "
        "description explicitly says otherwise. When it does not, "
        "do not return invoke_tool.\n"
        "Never return next_step or complete from a step with tool_invocations "
        "until you have invoked a declared tool for that step and received a "
        "successful result. A prose summary of the intended command is not a "
        "tool invocation; emit invoke_tool and wait for its result.\n"
        "Use next_step when the current step is complete and the next step "
        "should receive the accumulated context.\n"
        "When a step declares tool_invocations, next_step and complete are "
        "invalid until a declared tool has been invoked successfully for that "
        "step.\n"
        "Use complete when the skill is finished.\n"
        "Always include decisions_and_context with the concise information "
        "future steps will need.\n"
        "Always include llm_type to select the model for the next roundtrip. "
        "Use high_reasoning for architecture, difficult reasoning, and final "
        "review; standard_reasoning for normal implementation; simple_task "
        "for mechanical work; fast_iteration for quick feedback; long_context "
        "for large specifications; and vision for image-oriented tasks.\n"
        "Do not output markdown."
    )


def _context_type_catalog() -> tuple[tuple[str, str], ...]:
    return (
        ("requirements", "discover what requirements are already specified"),
        ("approach", "discover the existing approach or solution shape"),
        ("entities", "discover the domain entities already described"),
        (
            "entity-relationships",
            "discover how entities are already related",
        ),
        ("invariants", "discover the rules that must always remain true"),
        ("guidance", "discover implementation guidance or cautions"),
        ("features", "discover the features already recorded or in scope"),
        (
            "human-decisions",
            "discover human decisions that must be preserved",
        ),
        ("intent", "discover the problem, goal, or reasoning already stated"),
        ("intents", "discover current-state intent records"),
        (
            "acceptance_criteria",
            "discover acceptance criteria already written down",
        ),
        ("expected_tests", "discover expected tests already listed"),
        ("required_test_cases", "discover required test cases already listed"),
        ("expected_outcomes", "discover expected outcomes already stated"),
        ("non_goals", "discover what is explicitly out of scope"),
        ("risks", "discover open risks or concerns"),
        ("decisions", "discover recorded decisions or tradeoffs"),
        ("proposed_prs", "discover proposed PR records and their status"),
        ("modules", "discover project modules and their locations"),
        ("tools", "discover project tools and validation commands"),
    )


def _workflow_action_handlers() -> dict[
    str,
    Callable[
        [
            SkillChatAction,
            _WorkflowExecutionState,
            TextIO,
            TextIO,
            Callable[[], str],
            WorkflowChatConfig,
        ],
        bool,
    ],
]:
    return {
        "complete": _handle_workflow_action_complete,
        "edit": _handle_workflow_action_edit,
        "yaml_edit": _handle_workflow_action_yaml_edit,
        "read_document": _handle_workflow_action_read_document,
        "next_step": _handle_workflow_action_next_step,
        "prompt_user": _handle_workflow_action_prompt_user,
        "invoke_tool": _handle_workflow_action_invoke_tool,
        "gather_context": _handle_workflow_action_gather_context,
    }


def _current_file_contents(state: _WorkflowExecutionState) -> str | None:
    if state.current_file_path is None or not state.current_file_path.exists():
        return None
    return state.current_file_path.read_text(encoding="utf-8")


def _last_user_message(state: _WorkflowExecutionState) -> str | None:
    if not state.transcript or state.transcript[-1]["role"] != "user":
        return None
    return state.transcript[-1]["content"]


def _workflow_action_material_state(
    action: SkillChatAction,
    state: _WorkflowExecutionState,
) -> object:
    """Return only state that proves this action changed the workflow.

    Tool transcript entries are observations, not progress.  Counting them made
    an unchanged tool call appear productive forever and let the prompt grow
    without bound.  This mirrors durable task execution's edit-only material
    state snapshot while retaining the genuinely interactive user response.
    """
    if action.kind in {"edit", "yaml_edit"}:
        file_paths = (
            tuple(group.file_path for group in action.file_edits)
            if action.file_edits
            else ((action.file_path,) if action.file_path is not None else ())
        )
        return tuple(
            (
                file_path,
                (
                    target_path.read_text(encoding="utf-8")
                    if (
                        target_path := _resolve_worktree_file_path(
                            file_path,
                            state.worktree_root,
                        )
                    ).exists()
                    else None
                ),
            )
            for file_path in file_paths
        )
    if action.kind == "prompt_user":
        return _last_user_message(state)
    return None


def _workflow_action_signature(action: SkillChatAction) -> str:
    return _shared_workflow_action_signature(action)


def _workflow_action_progress_status(action: SkillChatAction) -> str | None:
    if action.kind in {"edit", "yaml_edit"}:
        return "Edited file"
    if action.kind == "read_document":
        return "Reading file"
    if action.kind == "gather_context":
        return "Gathering structured context"
    if action.kind == "invoke_tool":
        command = action.parameters.get("command")
        if isinstance(command, str):
            command_line = command
        elif isinstance(command, Sequence) and not isinstance(
            command, (str, bytes, bytearray)
        ):
            command_line = shlex.join(str(item) for item in command)
        else:
            command_line = action.tool or "tool"
        return f"Invoking {command_line}"
    return None


def _workflow_step_requires_pull_request(step: Any) -> bool:
    return any(
        tuple(invocation.command[:3]) == ("gh", "pr", "create")
        for invocation in step.tool_invocations
    )


def _handle_workflow_action_complete(
    action: SkillChatAction,
    state: _WorkflowExecutionState,
    stdout: TextIO,
    stderr: TextIO,
    input_func: Callable[[], str],
    config: WorkflowChatConfig,
) -> bool:
    _ = stderr
    _ = input_func
    _ = config
    if action.text:
        print(action.text, file=stdout)
    if action.decisions_and_context:
        state.execution_context.append(action.decisions_and_context)
    state.execution_events.append(
        {
            "kind": action.kind,
            "text": action.text,
            "decisions_and_context": action.decisions_and_context,
        }
    )
    return False


def _handle_workflow_action_edit(
    action: SkillChatAction,
    state: _WorkflowExecutionState,
    stdout: TextIO,
    stderr: TextIO,
    input_func: Callable[[], str],
    config: WorkflowChatConfig,
) -> bool:
    _ = input_func
    _ = config
    file_edits = action.file_edits
    if not file_edits:
        if action.file_path is None:
            raise RuntimeError("Workflow edit action must include file_path.")
        file_edits = (SkillChatFileEdits(action.file_path, action.edits),)

    pending_writes: list[tuple[Path, str]] = []
    results: list[dict[str, Any]] = []
    for file_edit in file_edits:
        target_path = _resolve_worktree_file_path(
            file_edit.file_path,
            state.worktree_root,
        )
        current_text = ""
        if target_path.exists():
            current_text = target_path.read_text(encoding="utf-8")
        state.current_file_path = target_path
        updated_text = _apply_file_edits(current_text, file_edit.edits)
        updated_text = _normalize_structured_document_text(target_path, updated_text)
        _validate_structured_document_text(target_path, updated_text)
        pending_writes.append((target_path, updated_text))
        results.append(
            {
                "file_path": str(target_path),
                "line_count": len(updated_text.splitlines()),
            }
        )

    for target_path, updated_text in pending_writes:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text(updated_text, encoding="utf-8")
    state.fuzzy_match_cache.clear()

    if action.decisions_and_context:
        state.execution_context.append(action.decisions_and_context)
    action_data = {
        "kind": action.kind,
        "file_edits": [_file_edits_to_data(group) for group in file_edits],
    }
    state.transcript.append(
        {
            "role": "assistant",
            "content": json.dumps(action_data, ensure_ascii=False),
        }
    )
    state.transcript.append(
        {
            "role": "user",
            "content": json.dumps({"edit_result": results}, ensure_ascii=False),
        }
    )
    state.execution_events.append(
        {
            "kind": action.kind,
            "file_edits": [_file_edits_to_data(group) for group in file_edits],
            "result": results,
            "decisions_and_context": action.decisions_and_context,
            "step_index": state.step_index,
        }
    )
    for result in results:
        print(f"Edited file: {result['file_path']}", file=stdout)
        _verbose_print(
            stderr,
            config.verbose,
            f"Applied edit to {result['file_path']}",
        )
    return True


def _handle_workflow_action_yaml_edit(
    action: SkillChatAction,
    state: _WorkflowExecutionState,
    stdout: TextIO,
    stderr: TextIO,
    input_func: Callable[[], str],
    config: WorkflowChatConfig,
) -> bool:
    _ = input_func
    if action.file_path is None:
        raise _WorkflowYamlEditError(
            "yaml_edit requires file_path. Use a repository-relative .yaml or "
            ".yml path."
        )
    target_path = _resolve_worktree_file_path(action.file_path, state.worktree_root)
    if not target_path.exists():
        raise _WorkflowYamlEditError(
            f"yaml_edit target {action.file_path!r} does not exist. Read or "
            "generate the YAML document before applying structural edits."
        )
    state.current_file_path = target_path
    current_text = target_path.read_text(encoding="utf-8")
    updated_text = _apply_yaml_operations(
        target_path,
        current_text,
        action.yaml_operations,
    )
    _validate_structured_document_text(target_path, updated_text)
    target_path.write_text(updated_text, encoding="utf-8")
    state.fuzzy_match_cache.clear()

    if action.decisions_and_context:
        state.execution_context.append(action.decisions_and_context)
    action_data = {
        "kind": action.kind,
        "file_path": action.file_path,
        "operations": [
            _yaml_operation_to_data(operation) for operation in action.yaml_operations
        ],
    }
    result = {
        "file_path": str(target_path),
        "line_count": len(updated_text.splitlines()),
    }
    state.transcript.extend(
        [
            {
                "role": "assistant",
                "content": json.dumps(action_data, ensure_ascii=False),
            },
            {
                "role": "user",
                "content": json.dumps({"yaml_edit_result": result}, ensure_ascii=False),
            },
        ]
    )
    state.execution_events.append(
        {
            **action_data,
            "result": result,
            "decisions_and_context": action.decisions_and_context,
            "step_index": state.step_index,
        }
    )
    print(f"Edited YAML file: {target_path}", file=stdout)
    _verbose_print(stderr, config.verbose, f"Applied YAML edit to {target_path}")
    return True


def _handle_workflow_action_read_document(
    action: SkillChatAction,
    state: _WorkflowExecutionState,
    stdout: TextIO,
    stderr: TextIO,
    input_func: Callable[[], str],
    config: WorkflowChatConfig,
) -> bool:
    _ = stdout
    _ = input_func
    if action.file_path is None:
        raise RuntimeError("Workflow read_document action must include file_path.")
    if action.start_line is None or action.end_line is None:
        raise RuntimeError(
            "Workflow read_document action must include start_line and end_line."
        )
    if action.end_line < action.start_line:
        raise RuntimeError(
            "Workflow read_document action end_line must be >= start_line."
        )
    requested_line_count = action.end_line - action.start_line + 1
    if requested_line_count > _MAX_DOCUMENT_CONTEXT_LINES:
        raise RuntimeError(
            "Workflow read_document action may request at most "
            f"{_MAX_DOCUMENT_CONTEXT_LINES} lines."
        )

    target_path = _resolve_worktree_file_path(action.file_path, state.worktree_root)
    if not target_path.exists() or not target_path.is_file():
        raise RuntimeError(
            f"Workflow read_document action file does not exist: {action.file_path}"
        )
    lines = target_path.read_text(encoding="utf-8").splitlines()
    if action.start_line > len(lines) or action.end_line > len(lines):
        raise RuntimeError(
            f"Workflow read_document action line range {action.start_line}-"
            f"{action.end_line} is outside the document, which has "
            f"{len(lines)} lines."
        )

    excerpt = {
        "path": str(target_path.relative_to(state.worktree_root)),
        "start_line": action.start_line,
        "end_line": action.end_line,
        "document_line_count": len(lines),
        "lines": [
            {
                "line_number": line_number,
                "text": lines[line_number - 1],
            }
            for line_number in range(action.start_line, action.end_line + 1)
        ],
    }
    excerpt_text = json.dumps(excerpt, ensure_ascii=False)
    action_data = {
        "kind": action.kind,
        "file_path": action.file_path,
        "start_line": action.start_line,
        "end_line": action.end_line,
    }
    state.transcript.append(
        {
            "role": "assistant",
            "content": json.dumps(action_data, ensure_ascii=False),
        }
    )
    state.transcript.append(
        {
            "role": "user",
            "content": json.dumps(
                {"document_context": excerpt},
                ensure_ascii=False,
            ),
        }
    )
    state.execution_context.append(f"Document context: {excerpt_text}")
    if action.decisions_and_context:
        state.execution_context.append(action.decisions_and_context)
    state.execution_events.append(
        {
            "kind": action.kind,
            "file_path": action.file_path,
            "start_line": action.start_line,
            "end_line": action.end_line,
            "result": excerpt,
            "decisions_and_context": action.decisions_and_context,
            "step_index": state.step_index,
        }
    )
    _verbose_print(
        stderr,
        config.verbose,
        f"Read document context {action.file_path}:{action.start_line}-"
        f"{action.end_line}",
    )
    return True


def _handle_workflow_action_next_step(
    action: SkillChatAction,
    state: _WorkflowExecutionState,
    stdout: TextIO,
    stderr: TextIO,
    input_func: Callable[[], str],
    config: WorkflowChatConfig,
) -> bool:
    _ = stdout
    _ = stderr
    _ = input_func
    _ = config
    if action.decisions_and_context:
        state.execution_context.append(action.decisions_and_context)
    state.execution_events.append(
        {
            "kind": action.kind,
            "decisions_and_context": action.decisions_and_context,
            "step_index": state.step_index,
        }
    )
    state.step_index += 1
    return True


def _handle_workflow_action_prompt_user(
    action: SkillChatAction,
    state: _WorkflowExecutionState,
    stdout: TextIO,
    stderr: TextIO,
    input_func: Callable[[], str],
    config: WorkflowChatConfig,
) -> bool:
    print(action.text or "", file=stdout)
    answer = _prompt_user(
        "> ",
        input_func=input_func,
        stdout=stdout,
        status_stream=stderr,
    )
    _verbose_print(stderr, config.verbose, f"Follow-up answer: {answer}")
    state.transcript.append(
        {
            "role": "assistant",
            "content": action.text or "",
        }
    )
    state.transcript.append({"role": "user", "content": answer})
    if action.decisions_and_context:
        state.execution_context.append(action.decisions_and_context)
    state.execution_events.append(
        {
            "kind": action.kind,
            "text": action.text,
            "answer": answer,
            "decisions_and_context": action.decisions_and_context,
            "step_index": state.step_index,
        }
    )
    return True


def _handle_workflow_action_invoke_tool(
    action: SkillChatAction,
    state: _WorkflowExecutionState,
    stdout: TextIO,
    stderr: TextIO,
    input_func: Callable[[], str],
    config: WorkflowChatConfig,
) -> bool:
    _ = input_func
    if action.tool == "fuzzy-match":
        tool_result = _execute_fuzzy_match_tool(
            action.parameters,
            worktree_root=state.worktree_root,
            path_cache=state.fuzzy_match_cache,
        )
    elif action.tool in {"shell", _INTERNAL_TOOL}:
        if action.tool == _INTERNAL_TOOL:
            _validate_internal_command(action.parameters.get("command"))
        command_items = _command_items_for_validation(action.parameters.get("command"))
        tool_result = _execute_shell_tool(
            action.parameters,
            worktree_root=state.worktree_root,
            stdout=stdout,
            stderr=stderr,
            verbose=config.verbose,
            announce=False,
            print_stdout=not (
                action.tool == _INTERNAL_TOOL
                and command_items[1:2] == ["pull-request-description"]
            ),
        )
    elif is_basedpyright_tool(action.tool or ""):
        assert action.tool is not None
        tool_result = execute_basedpyright_tool(
            action.tool,
            action.parameters,
            worktree_root=state.worktree_root,
        )
    else:
        raise RuntimeError(
            f"Unsupported workflow tool {action.tool!r}; supported tools are shell, "
            "internal, fuzzy-match, basedpyright-symbol, and basedpyright-structure."
        )
    inferred_path = _resolve_generated_file_path_from_command(
        action.parameters.get("command"),
        worktree_root=state.worktree_root,
    )
    if inferred_path is not None:
        state.current_file_path = inferred_path
    if action.tool == "shell":
        state.fuzzy_match_cache.clear()
    state.transcript.append(
        {
            "role": "assistant",
            "content": json.dumps(
                {
                    "kind": action.kind,
                    "parameters": action.parameters,
                },
                ensure_ascii=False,
            ),
        }
    )
    state.transcript.append(
        {
            "role": "user",
            "content": json.dumps(
                {"tool_result": tool_result},
                ensure_ascii=False,
            ),
        }
    )
    if action.decisions_and_context:
        state.execution_context.append(action.decisions_and_context)
    state.execution_events.append(
        {
            "kind": action.kind,
            "parameters": action.parameters,
            "result": tool_result,
            "decisions_and_context": action.decisions_and_context,
            "step_index": state.step_index,
        }
    )
    return True


def _validate_workflow_action_for_step(action: SkillChatAction, step: Any) -> None:
    """Reject tool actions that do not match a current-step invocation."""
    if action.kind != "invoke_tool":
        return
    try:
        _validate_workflow_action_for_step_unwrapped(action, step)
    except RuntimeError as exc:
        if isinstance(exc, _WorkflowToolValidationError):
            raise
        raise _WorkflowToolValidationError(
            ValidationError(
                code="workflow_tool_action_invalid",
                message=str(exc),
                path="parameters.command",
            )
        ) from exc


def _validate_workflow_step_transition(
    action: SkillChatAction,
    step: Any,
    execution_events: Sequence[Mapping[str, Any]],
    current_step_index: int,
) -> None:
    """Prevent the LLM from skipping a step's required tool invocation."""
    if action.kind not in {"next_step", "complete"}:
        return
    invocations = tuple(
        invocation for invocation in step.tool_invocations if invocation.tool != "ref"
    )
    if not invocations:
        return
    # Shell invocations are the externally visible commands that can mutate
    # the branch. Internal inspection and validator actions retain their
    # existing corrective-action behavior; a shell command in the same step
    # remains the transition gate.
    required_invocations = tuple(
        invocation for invocation in invocations if invocation.tool == "shell"
    )
    if not required_invocations:
        return

    def successful_event_matches(invocation: Any, event: Mapping[str, Any]) -> bool:
        if event.get("kind") != "invoke_tool":
            return False
        if event.get("tool") not in {None, invocation.tool}:
            return False
        if event.get("step_index") != current_step_index:
            return False
        result = event.get("result")
        if isinstance(result, Mapping) and result.get("returncode") not in (None, 0):
            return False
        parameters = event.get("parameters")
        if not isinstance(parameters, Mapping) or parameters.get("command") is None:
            return True
        try:
            command_items = _command_items_for_validation(parameters["command"])
        except RuntimeError:
            return False
        return _command_matches_invocation(command_items, invocation.command)

    if any(
        any(successful_event_matches(invocation, event) for event in execution_events)
        for invocation in required_invocations
    ):
        return
    expected = ", ".join(invocation.tool for invocation in required_invocations)
    raise _WorkflowToolValidationError(
        ValidationError(
            code="workflow_step_tool_required",
            message=(
                f"The current step requires a successful tool invocation before "
                f"{action.kind}: {expected}. Current step index: "
                f"{current_step_index}. Invoke the declared tool and wait for its "
                "result before advancing."
            ),
            path="kind",
        )
    )


def _validate_workflow_action_for_step_unwrapped(
    action: SkillChatAction, step: Any
) -> None:
    """Validate a tool action while preserving the original error wording."""
    supported_invocations = tuple(
        invocation for invocation in step.tool_invocations if invocation.tool != "ref"
    )
    if action.tool == _INTERNAL_TOOL:
        _validate_internal_command(action.parameters.get("command"))
        return
    matching_invocations = tuple(
        invocation
        for invocation in supported_invocations
        if invocation.tool == action.tool
    )
    if not matching_invocations:
        supported_tools = sorted(
            {invocation.tool for invocation in supported_invocations}
        )
        supported_tools_text = ", ".join(supported_tools) or "none"
        raise RuntimeError(
            f"Tool {action.tool!r} is not supported by the current workflow step. "
            f"The step explicitly supports: {supported_tools_text}."
        )
    command = action.parameters.get("command")
    command_items = _command_items_for_validation(command)
    if any(
        _command_matches_invocation(command_items, invocation.command)
        for invocation in matching_invocations
    ):
        return
    expected_commands = "; ".join(
        " ".join(invocation.command) for invocation in matching_invocations
    )
    raise RuntimeError(
        f"Tool {action.tool!r} command {' '.join(command_items)!r} does not match "
        f"the command shape explicitly supported by the current workflow step: "
        f"{expected_commands}."
    )


def _command_items_for_validation(command: object) -> list[str]:
    if isinstance(command, str):
        try:
            command_items = shlex.split(command)
        except ValueError as exc:
            raise RuntimeError(
                "Workflow tool command is not valid shell syntax."
            ) from exc
    elif isinstance(command, Sequence) and not isinstance(
        command, (str, bytes, bytearray)
    ):
        command_items = list(command)
    else:
        raise RuntimeError("Workflow tool action must include a command.")
    if not command_items or any(
        not isinstance(item, str) or not item for item in command_items
    ):
        raise RuntimeError(
            "Workflow tool action command must contain non-empty strings."
        )
    return command_items


def _command_matches_invocation(
    command: Sequence[str],
    expected_command: Sequence[str],
) -> bool:
    if len(command) != len(expected_command):
        return False
    return all(
        _command_token_matches(actual, expected)
        for actual, expected in zip(command, expected_command, strict=True)
    )


def _command_token_matches(actual: str, expected: str) -> bool:
    if actual == expected:
        return True
    placeholder_matches = list(re.finditer(r"<[^<>]+>", expected))
    if not placeholder_matches:
        return False
    if len(placeholder_matches) == 1 and placeholder_matches[0].span() == (
        0,
        len(expected),
    ):
        # A command-template argument such as <populated-pr-description> can
        # intentionally contain spaces and newlines.  The command parser keeps
        # that value as one argv item when the LLM returns an array (or quotes
        # it in a shell command), so validating it as a non-whitespace token
        # incorrectly rejects otherwise valid commands.
        return bool(actual)
    pattern_parts: list[str] = []
    previous_end = 0
    for placeholder_match in placeholder_matches:
        pattern_parts.append(
            re.escape(expected[previous_end : placeholder_match.start()])
        )
        pattern_parts.append(r"[^\s]+")
        previous_end = placeholder_match.end()
    pattern_parts.append(re.escape(expected[previous_end:]))
    return re.fullmatch("".join(pattern_parts), actual) is not None


def _execute_fuzzy_match_tool(
    parameters: dict[str, Any],
    *,
    worktree_root: Path,
    path_cache: dict[tuple[str, int, int | None], tuple[Path, ...]] | None = None,
) -> dict[str, Any]:
    command = parameters.get("command")
    if not isinstance(command, (str, list, tuple)):
        raise RuntimeError(
            "Workflow fuzzy-match tool parameters must include a command array."
        )
    return {
        "tool": "fuzzy-match",
        "command": list(command) if not isinstance(command, str) else command,
        "result": json.loads(
            fuzzy_match_json(
                command, worktree_root=worktree_root, path_cache=path_cache
            )
        ),
    }


def _handle_workflow_action_gather_context(
    action: SkillChatAction,
    state: _WorkflowExecutionState,
    stdout: TextIO,
    stderr: TextIO,
    input_func: Callable[[], str],
    config: WorkflowChatConfig,
) -> bool:
    _ = input_func
    gathered_context = gather_specification_context(
        state.worktree_root,
        types=list(action.types),
        keywords=list(action.keywords) if action.keywords else None,
        filters=action.filters,
    )
    gathered_context_text = render_gather_context_report(gathered_context)
    _verbose_print(
        stderr,
        config.verbose,
        (
            "Gathered context for "
            f"types={list(action.types)} keywords={list(action.keywords)}"
        ),
    )
    if action.decisions_and_context:
        state.execution_context.append(action.decisions_and_context)
    state.execution_context.append(f"Gathered context:\n{gathered_context_text}")
    state.execution_events.append(
        {
            "kind": action.kind,
            "types": list(action.types),
            "keywords": list(action.keywords),
            "filters": action.filters,
            "result": json.loads(gathered_context_text),
            "decisions_and_context": action.decisions_and_context,
            "step_index": state.step_index,
        }
    )
    _ = stdout
    return True


def _parse_action_response(payload: dict[str, Any]) -> SkillChatAction:
    kind = payload.get("kind")
    if not isinstance(kind, str):
        raise RuntimeError("Workflow action response must include kind.")
    normalized_kind = kind.strip()
    if not normalized_kind:
        raise RuntimeError("Workflow action response must include kind.")
    decisions_and_context = _optional_string(payload.get("decisions_and_context"))
    llm_type = _optional_llm_type(payload.get("llm_type"))
    parser = _workflow_action_parsers().get(normalized_kind)
    if parser is None:
        raise RuntimeError(f"Unknown workflow action kind: {normalized_kind!r}")
    return parser(payload, decisions_and_context, llm_type)


def _workflow_action_parsers() -> dict[str, WorkflowActionParser]:
    return {
        "complete": _parse_workflow_action_complete,
        "edit": _parse_workflow_action_edit,
        "yaml_edit": _parse_workflow_action_yaml_edit,
        "gather_context": _parse_workflow_action_gather_context,
        "invoke_tool": _parse_workflow_action_invoke_tool,
        "invoke_skill": _parse_workflow_action_invoke_skill,
        "read_document": _parse_workflow_action_read_document,
        "next_step": _parse_workflow_action_next_step,
        "prompt_user": _parse_workflow_action_prompt_user,
    }


def _parse_workflow_action_invoke_skill(
    payload: dict[str, Any],
    decisions_and_context: str | None,
    llm_type: str | None,
) -> SkillChatAction:
    skill_name = payload.get("skill")
    if not isinstance(skill_name, str) or not skill_name.strip():
        raise RuntimeError("Workflow invoke_skill action must include skill.")
    provider_role = payload.get("provider_role")
    if provider_role is not None and provider_role not in {"normal", "adversarial"}:
        raise RuntimeError(
            'provider_role must be "normal" or "adversarial" when provided.'
        )
    clean = payload.get("clean", False)
    if not isinstance(clean, bool):
        raise RuntimeError("clean must be a boolean when provided.")
    raw_context = payload.get("context", [])
    if not isinstance(raw_context, list) or not all(
        isinstance(value, str) and value.strip() for value in raw_context
    ):
        raise RuntimeError("context must be a list of non-empty strings.")
    return SkillChatAction(
        kind="invoke_skill",
        skill_name=skill_name.strip(),
        decisions_and_context=decisions_and_context,
        llm_type=llm_type,
        provider_role=provider_role,
        clean=clean,
        context=tuple(value.strip() for value in raw_context),
    )


def _parse_workflow_action_complete(
    payload: dict[str, Any],
    decisions_and_context: str | None,
    llm_type: str | None,
) -> SkillChatAction:
    text = payload.get("text")
    if text is not None and not isinstance(text, str):
        raise RuntimeError("Workflow complete action text must be a string.")
    return SkillChatAction(
        kind="complete",
        text=(text.strip() if text else None),
        output_state=payload.get("output_state"),
        decisions_and_context=decisions_and_context,
        llm_type=llm_type,
    )


def _parse_workflow_action_edit(
    payload: dict[str, Any],
    decisions_and_context: str | None,
    llm_type: str | None,
) -> SkillChatAction:
    file_edits_value = payload.get("file_edits")
    if file_edits_value is not None:
        return SkillChatAction(
            kind="edit",
            file_edits=_required_file_edits(file_edits_value),
            decisions_and_context=decisions_and_context,
            llm_type=llm_type,
        )
    file_path = payload.get("file_path")
    if not isinstance(file_path, str) or not file_path.strip():
        raise RuntimeError("Workflow edit action must include file_path.")
    edits = _required_edit_operations(payload.get("edits"))
    return SkillChatAction(
        kind="edit",
        file_path=file_path.strip(),
        edits=edits,
        decisions_and_context=decisions_and_context,
        llm_type=llm_type,
    )


def _parse_workflow_action_yaml_edit(
    payload: dict[str, Any],
    decisions_and_context: str | None,
    llm_type: str | None,
) -> SkillChatAction:
    file_path = payload.get("file_path")
    if not isinstance(file_path, str) or not file_path.strip():
        raise RuntimeError(
            "yaml_edit requires file_path. Use a repository-relative .yaml or "
            ".yml path."
        )
    if not file_path.strip().lower().endswith((".yaml", ".yml")):
        raise RuntimeError("yaml_edit file_path must end in .yaml or .yml.")
    raw_operations = payload.get("operations")
    if not isinstance(raw_operations, Sequence) or isinstance(
        raw_operations,
        (str, bytes, bytearray),
    ):
        raise RuntimeError(
            "yaml_edit requires a non-empty operations array. Supported "
            "operations are upsert_item, remove_item, and set_value."
        )
    operations = tuple(_parse_yaml_operation(item) for item in raw_operations)
    if not operations:
        raise RuntimeError(
            "yaml_edit operations must not be empty. Use upsert_item, "
            "remove_item, or set_value."
        )
    return SkillChatAction(
        kind="yaml_edit",
        file_path=file_path.strip(),
        yaml_operations=operations,
        decisions_and_context=decisions_and_context,
        llm_type=llm_type,
    )


def _parse_yaml_operation(value: object) -> SkillChatYamlOperation:
    if not isinstance(value, Mapping):
        raise RuntimeError(
            'yaml_edit operations must be objects. Example: {"op": '
            '"upsert_item", "section": "features", "id": '
            '"feature-id", "value": {...}}.'
        )
    operation = value.get("op")
    if operation not in {"upsert_item", "remove_item", "set_value"}:
        raise RuntimeError(
            f"yaml_edit operation op must be upsert_item, remove_item, or "
            f"set_value; received {operation!r}."
        )
    if operation == "set_value":
        raw_path = value.get("path")
        if (
            not isinstance(raw_path, Sequence)
            or isinstance(
                raw_path,
                (str, bytes, bytearray),
            )
            or not raw_path
            or not all(isinstance(item, str) and item.strip() for item in raw_path)
        ):
            raise RuntimeError(
                "yaml_edit set_value requires a non-empty path array of mapping "
                'keys, for example ["title"].'
            )
        if "value" not in value:
            raise RuntimeError("yaml_edit set_value requires value.")
        return SkillChatYamlOperation(
            operation=operation,
            path=tuple(item.strip() for item in raw_path),
            value=value["value"],
        )

    section = value.get("section")
    item_id = value.get("id")
    if not isinstance(section, str) or not section.strip():
        raise RuntimeError(
            f"yaml_edit {operation} requires a non-empty section, such as "
            "features or decisions."
        )
    if not isinstance(item_id, str) or not item_id.strip():
        raise RuntimeError(
            f"yaml_edit {operation} requires a non-empty id. Use read_document "
            "to discover the existing item id."
        )
    if operation == "upsert_item" and not isinstance(value.get("value"), Mapping):
        raise RuntimeError(
            "yaml_edit upsert_item requires value to be a mapping containing the "
            "complete item fields."
        )
    return SkillChatYamlOperation(
        operation=operation,
        section=section.strip(),
        item_id=item_id.strip(),
        value=value.get("value"),
    )


def _parse_workflow_action_gather_context(
    payload: dict[str, Any],
    decisions_and_context: str | None,
    llm_type: str | None,
) -> SkillChatAction:
    types = _required_action_string_sequence(
        payload.get("types"),
        field_name="types",
    )
    keywords = _optional_action_string_sequence(
        payload.get("keywords"),
        field_name="keywords",
    )
    filters = _optional_action_filters(payload.get("filters"))
    normalized_types = tuple(
        normalize_context_type(context_type) for context_type in types
    )
    return SkillChatAction(
        kind="gather_context",
        types=normalized_types,
        keywords=keywords,
        filters=filters,
        decisions_and_context=decisions_and_context,
        llm_type=llm_type,
    )


def _parse_workflow_action_invoke_tool(
    payload: dict[str, Any],
    decisions_and_context: str | None,
    llm_type: str | None,
) -> SkillChatAction:
    tool = payload.get("tool")
    if not isinstance(tool, str) or not tool.strip():
        raise RuntimeError("Workflow invoke_tool action must include tool.")
    parameters = payload.get("parameters")
    if not isinstance(parameters, dict):
        raise RuntimeError("Workflow invoke_tool action must include parameters.")
    normalized_tool = tool.strip()
    if is_basedpyright_tool(normalized_tool):
        return SkillChatAction(
            kind="invoke_tool",
            tool=normalized_tool,
            parameters=dict(parameters),
            decisions_and_context=decisions_and_context,
            llm_type=llm_type,
        )
    command = parameters.get("command")
    if isinstance(command, str):
        normalized_parameters = dict(parameters)
        normalized_command = command.strip()
        if not normalized_command:
            raise RuntimeError("Workflow invoke_tool action command must be non-empty.")
        normalized_parameters["command"] = normalized_command
        return SkillChatAction(
            kind="invoke_tool",
            tool=normalized_tool,
            parameters=normalized_parameters,
            decisions_and_context=decisions_and_context,
            llm_type=llm_type,
        )
    if isinstance(command, Sequence) and not isinstance(
        command,
        (str, bytes, bytearray),
    ):
        normalized_command_list = [
            _required_shell_command_item(item) for item in command
        ]
        if not normalized_command_list:
            raise RuntimeError("Workflow invoke_tool action command must not be empty.")
        normalized_parameters = dict(parameters)
        normalized_parameters["command"] = normalized_command_list
        return SkillChatAction(
            kind="invoke_tool",
            tool=normalized_tool,
            parameters=normalized_parameters,
            decisions_and_context=decisions_and_context,
            llm_type=llm_type,
        )
    raise RuntimeError("Workflow invoke_tool action command must be a string or array.")


def _parse_workflow_action_read_document(
    payload: dict[str, Any],
    decisions_and_context: str | None,
    llm_type: str | None,
) -> SkillChatAction:
    file_path = payload.get("file_path")
    if not isinstance(file_path, str) or not file_path.strip():
        raise RuntimeError("Workflow read_document action must include file_path.")
    start_line = _required_edit_line_number(
        payload.get("start_line"),
        field_name="start_line",
    )
    end_line = _required_edit_line_number(
        payload.get("end_line"),
        field_name="end_line",
    )
    if end_line < start_line:
        raise RuntimeError(
            "Workflow read_document action end_line must be >= start_line."
        )
    if end_line - start_line + 1 > _MAX_DOCUMENT_CONTEXT_LINES:
        raise RuntimeError(
            "Workflow read_document action may request at most "
            f"{_MAX_DOCUMENT_CONTEXT_LINES} lines."
        )
    return SkillChatAction(
        kind="read_document",
        file_path=file_path.strip(),
        start_line=start_line,
        end_line=end_line,
        decisions_and_context=decisions_and_context,
        llm_type=llm_type,
    )


def _parse_workflow_action_next_step(
    payload: dict[str, Any],
    decisions_and_context: str | None,
    llm_type: str | None,
) -> SkillChatAction:
    _ = payload
    return SkillChatAction(
        kind="next_step",
        decisions_and_context=decisions_and_context,
        llm_type=llm_type,
    )


def _parse_workflow_action_prompt_user(
    payload: dict[str, Any],
    decisions_and_context: str | None,
    llm_type: str | None,
) -> SkillChatAction:
    text = payload.get("text")
    if not isinstance(text, str):
        raise RuntimeError("Workflow prompt_user action text must be a string.")
    text = _validate_user_question(
        text,
        field_name="Workflow prompt_user action text",
    )
    return SkillChatAction(
        kind="prompt_user",
        text=(text.strip() if text else None),
        decisions_and_context=decisions_and_context,
        llm_type=llm_type,
    )


def _execute_shell_tool(
    parameters: dict[str, Any],
    *,
    worktree_root: Path,
    stdout: TextIO,
    stderr: TextIO,
    verbose: bool,
    announce: bool = True,
    print_stdout: bool = True,
) -> dict[str, Any]:
    command = parameters.get("command")
    if isinstance(command, str):
        command_display = _rtk_command_display(command)
        run_command: str | list[str] = _wrap_shell_command(command)
        use_shell = True
    elif isinstance(command, Sequence) and not isinstance(
        command,
        (str, bytes, bytearray),
    ):
        normalized_command = [_required_shell_command_item(item) for item in command]
        wrapped_command = _wrap_argument_command(normalized_command)
        command_display = " ".join(shlex.quote(item) for item in wrapped_command)
        run_command = wrapped_command
        use_shell = False
    else:
        raise RuntimeError(
            "Workflow invoke_tool action parameters must include a command."
        )

    cwd_value = parameters.get("cwd")
    if cwd_value is None:
        resolved_cwd = worktree_root
    elif isinstance(cwd_value, str) and cwd_value.strip():
        cwd_path = Path(cwd_value.strip())
        resolved_cwd = (
            cwd_path if cwd_path.is_absolute() else worktree_root / cwd_path
        ).resolve(strict=False)
        resolved_worktree_root = worktree_root.resolve(strict=False)
        if not resolved_cwd.is_relative_to(resolved_worktree_root):
            raise RuntimeError(
                "Workflow shell tool cwd must stay within the current worktree: "
                f"{resolved_worktree_root}"
            )
    else:
        raise RuntimeError("Workflow invoke_tool action cwd must be a string.")

    env_value = parameters.get("env")
    env = os.environ.copy()
    if env_value is not None:
        if not isinstance(env_value, dict):
            raise RuntimeError("Workflow invoke_tool action env must be an object.")
        for key, value in env_value.items():
            if not isinstance(key, str) or not key:
                raise RuntimeError(
                    "Workflow invoke_tool action env keys must be non-empty strings."
                )
            if not isinstance(value, str):
                raise RuntimeError(
                    "Workflow invoke_tool action env values must be strings."
                )
            env[key] = value

    if announce:
        print(f"Invoking shell tool: {command_display}", file=stdout)
        _verbose_print(stderr, verbose, f"Invoking shell tool: {command_display}")
    process = subprocess.run(
        run_command,
        shell=use_shell,
        check=False,
        capture_output=True,
        text=True,
        cwd=resolved_cwd,
        env=env,
    )
    if process.stdout:
        if print_stdout:
            print(process.stdout, end="", file=stdout)
            _verbose_print(
                stderr, verbose, f"Shell tool stdout:\n{process.stdout.rstrip()}"
            )
    if process.stderr:
        print(process.stderr, end="", file=stderr)
    _verbose_print(
        stderr,
        verbose,
        f"Shell tool exited with code {process.returncode}",
    )
    return {
        "command": command_display,
        "cwd": str(resolved_cwd),
        "returncode": process.returncode,
        "stdout": process.stdout,
        "stderr": process.stderr,
    }


def _validate_internal_command(command: object) -> None:
    command_items = _command_items_for_validation(command)
    if command_items[0] != _INTERNAL_BINARY:
        raise RuntimeError("The internal tool may invoke only the powdrr-lift binary.")


def _skill_step_to_data(step: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "description": step.description,
        "details": step.details,
        "uses_skills": list(step.uses_skills),
    }
    if step.tool_invocations:
        data["tool_invocations"] = [
            _tool_invocation_to_data(tool_invocation)
            for tool_invocation in step.tool_invocations
        ]
    return data


def _tool_invocation_to_data(tool_invocation: Any) -> dict[str, Any]:
    return tool_invocation.to_data()


def _required_shell_command_item(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(
            "Workflow invoke_tool action command items must be non-empty strings."
        )
    return value.strip()


def _wrap_shell_command(command: str) -> str:
    """Run a shell tool command through rtk without wrapping it twice."""
    if _shell_command_starts_with_rtk(command):
        return command
    return f"rtk {command}"


def _rtk_command_display(command: str) -> str:
    return _wrap_shell_command(command)


def _wrap_argument_command(command: list[str]) -> list[str]:
    if command and command[0] == "rtk":
        return command
    return ["rtk", *command]


def _shell_command_starts_with_rtk(command: str) -> bool:
    try:
        command_items = shlex.split(command)
    except ValueError:
        return False
    return bool(command_items) and command_items[0] == "rtk"


def _required_action_string_item(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(
            "Workflow gather_context action "
            f"{field_name} must contain non-empty strings."
        )
    return value.strip()


def _required_action_string_sequence(
    value: object,
    *,
    field_name: str,
) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(
        value,
        (str, bytes, bytearray),
    ):
        raise RuntimeError(
            f"Workflow gather_context action {field_name} must be an array."
        )

    normalized_values = tuple(
        _required_action_string_item(item, field_name=field_name) for item in value
    )
    if not normalized_values:
        raise RuntimeError(
            f"Workflow gather_context action {field_name} must not be empty."
        )
    return normalized_values


def _optional_action_string_sequence(
    value: object,
    *,
    field_name: str,
) -> tuple[str, ...]:
    if value is None:
        return tuple()
    if not isinstance(value, Sequence) or isinstance(
        value,
        (str, bytes, bytearray),
    ):
        raise RuntimeError(
            f"Workflow gather_context action {field_name} must be an array."
        )
    return tuple(
        _required_action_string_item(item, field_name=field_name) for item in value
    )


def _optional_action_filters(value: object) -> dict[str, object]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise RuntimeError("Workflow gather_context action filters must be an object.")
    filters: dict[str, object] = {}
    for raw_field, raw_values in value.items():
        field = _required_action_string_item(raw_field, field_name="filters")
        if isinstance(raw_values, (str, bytes, bytearray)):
            values: object = (raw_values,)
        elif isinstance(raw_values, Sequence):
            values = tuple(
                _required_action_string_item(item, field_name=f"filters.{field}")
                for item in raw_values
            )
        else:
            raise RuntimeError(
                f"Workflow gather_context action filters.{field} must be an array."
            )
        if not values:
            raise RuntimeError(
                f"Workflow gather_context action filters.{field} must not be empty."
            )
        filters[field] = values
    return filters


def _required_edit_operations(value: object) -> tuple[SkillChatEdit, ...]:
    if not isinstance(value, Sequence) or isinstance(
        value,
        (str, bytes, bytearray),
    ):
        raise RuntimeError("Workflow edit action edits must be an array.")

    edits = tuple(_required_edit_operation(item) for item in value)
    if not edits:
        raise RuntimeError("Workflow edit action edits must not be empty.")
    return edits


def _required_file_edits(value: object) -> tuple[SkillChatFileEdits, ...]:
    if not isinstance(value, Sequence) or isinstance(
        value,
        (str, bytes, bytearray),
    ):
        raise RuntimeError("Workflow edit action file_edits must be an array.")

    file_edits: list[SkillChatFileEdits] = []
    for item in value:
        if not isinstance(item, dict):
            raise RuntimeError("Workflow edit action file_edits must contain objects.")
        file_path = item.get("file_path")
        if not isinstance(file_path, str) or not file_path.strip():
            raise RuntimeError(
                "Workflow edit action file_edits entries must include file_path."
            )
        file_edits.append(
            SkillChatFileEdits(
                file_path=file_path.strip(),
                edits=_required_edit_operations(item.get("edits")),
            )
        )
    if not file_edits:
        raise RuntimeError("Workflow edit action file_edits must not be empty.")
    return tuple(file_edits)


def _required_edit_operation(value: object) -> SkillChatEdit:
    if not isinstance(value, dict):
        raise RuntimeError("Workflow edit action edits must be objects.")

    kind = value.get("kind")
    if not isinstance(kind, str) or not kind.strip():
        raise RuntimeError("Workflow edit action edit kind must be a string.")
    normalized_kind = kind.strip()
    if normalized_kind not in {"add", "remove", "replace"}:
        raise RuntimeError(
            "Workflow edit action edit kind must be add, remove, or replace."
        )

    start_line = _required_edit_line_number(
        value.get("start_line"),
        field_name="start_line",
    )
    end_line_value = value.get("end_line")
    end_line = None
    if end_line_value is not None:
        end_line = _required_edit_line_number(end_line_value, field_name="end_line")
        if end_line < start_line:
            raise RuntimeError("Workflow edit action end_line must be >= start_line.")

    text_value = value.get("text")
    if normalized_kind in {"add", "replace"}:
        if not isinstance(text_value, str):
            raise RuntimeError(
                "Workflow edit action add/replace edits must include text."
            )
        text = text_value
    else:
        if text_value is not None:
            raise RuntimeError(
                "Workflow edit action remove edits must not include text."
            )
        text = None
        if end_line is None:
            end_line = start_line

    return SkillChatEdit(
        kind=normalized_kind,
        start_line=start_line,
        end_line=end_line,
        text=text,
    )


def _required_edit_line_number(value: object, *, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise RuntimeError(
            f"Workflow edit action {field_name} must be a positive integer."
        )
    return value


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise RuntimeError("Workflow action decisions_and_context must be a string.")
    normalized_value = value.strip()
    return normalized_value or None


def _optional_llm_type(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError("Workflow llm_type must be a non-empty string.")
    return value.strip().lower().replace("-", "_")


def _resolve_llm_model(
    llm_type: str | None,
    *,
    fallback_model: str,
    mappings: Sequence[tuple[str, LLMModelMapping]],
    provider: str = "zai",
) -> str:
    if llm_type is None or not _provider_supports_llm_mappings(provider):
        return fallback_model
    resolved_mapping = _resolve_llm_mapping(
        llm_type,
        mappings=mappings,
        provider=provider,
    )
    return resolved_mapping.model if resolved_mapping is not None else fallback_model


def _resolve_llm_mapping(
    llm_type: str | None,
    *,
    mappings: Sequence[tuple[str, LLMModelMapping]],
    provider: str,
) -> LLMModelMapping | None:
    if llm_type is None:
        return None
    if not _provider_supports_llm_mappings(provider):
        raise RuntimeError(f"LLM mappings are not supported for provider {provider!r}.")
    normalized_llm_type = llm_type.strip().lower().replace("-", "_")
    mapping = dict(_default_llm_mappings(provider))
    mapping.update(
        {key.strip().lower().replace("-", "_"): value for key, value in mappings}
    )
    resolved_mapping = mapping.get(normalized_llm_type)
    if resolved_mapping is None:
        raise RuntimeError(
            f"No LLM mapping is configured for llm_type {llm_type!r} "
            f"with provider {provider!r}."
        )
    return resolved_mapping


def _complete_json_with_model_fallback(
    *,
    client_for: Callable[[str, str], WorkflowLLMClient],
    messages: list[dict[str, str]],
    context: str,
    model: str,
    parser: Callable[[dict[str, Any]], Any],
    repair_instructions: str,
    config: SkillChatConfig,
    input_func: Callable[[], str],
    stdout: TextIO,
    stderr: TextIO,
    model_mappings: Sequence[tuple[str, LLMModelMapping]],
    provider: str,
    empty_response_fallback_payload: dict[str, Any] | None = None,
) -> tuple[Any | None, str, str]:
    active_model = model
    active_provider = provider
    attempted_models = {model.casefold()}
    while True:
        long_context_backup = _long_context_backup_for(
            active_model,
            model_mappings,
        )
        estimated_input_tokens = _estimate_message_tokens(messages)
        active_limits = _model_limits_for(active_provider, active_model)
        if (
            long_context_backup is not None
            and estimated_input_tokens + _CONTEXT_SAFETY_MARGIN_TOKENS
            >= active_limits.context_window
            and long_context_backup.model.casefold() not in attempted_models
        ):
            print(
                f"{context} estimated context is too large for model "
                f"{active_model!r} ({estimated_input_tokens} input tokens; "
                f"limit {active_limits.context_window}). Switching to long-"
                f"context backup model {long_context_backup.model!r}.",
                file=stderr,
            )
            attempted_models.add(long_context_backup.model.casefold())
            active_model = long_context_backup.model
            active_provider = long_context_backup.provider
            continue
        try:
            result = _complete_json_with_repair(
                client_for(active_model, active_provider),
                messages,
                context=context,
                model=active_model,
                parser=parser,
                repair_instructions=repair_instructions,
                config=config,
                input_func=input_func,
                stdout=stdout,
                stderr=stderr,
                fallback_on_transient_exhaustion=(
                    _backup_model_for(active_model, model_mappings) is not None
                ),
                empty_response_fallback_payload=empty_response_fallback_payload,
            )
            return result, active_model, active_provider
        except _ModelUnavailableError as exc:
            backup_model = _backup_model_for(active_model, model_mappings)
            if (
                backup_model is None
                or backup_model.model.casefold() in attempted_models
            ):
                print(
                    f"{context} model {active_model!r} is unavailable and no "
                    "unused backup model is configured.",
                    file=stderr,
                )
                return None, active_model, active_provider
            print(
                f"{context} model {active_model!r} is unavailable: {exc}. "
                f"Switching to backup model {backup_model.model!r}.",
                file=stderr,
            )
            attempted_models.add(backup_model.model.casefold())
            active_model = backup_model.model
            active_provider = backup_model.provider


def _backup_model_for(
    model: str,
    model_mappings: Sequence[tuple[str, LLMModelMapping]],
) -> LLMModelMapping | None:
    normalized_model = model.casefold()
    for _, mapping in model_mappings:
        if mapping.model.casefold() == normalized_model:
            return mapping.backup_model
    return None


def _long_context_backup_for(
    model: str,
    model_mappings: Sequence[tuple[str, LLMModelMapping]],
) -> LLMModelMapping | None:
    normalized_model = model.casefold()
    for _, mapping in model_mappings:
        if mapping.model.casefold() == normalized_model:
            return mapping.long_context_backup_model
    return None


def _complete_json_with_repair(
    client: WorkflowLLMClient,
    messages: list[dict[str, str]],
    *,
    context: str,
    model: str,
    parser: Callable[[dict[str, Any]], Any],
    repair_instructions: str,
    config: WorkflowChatConfig,
    input_func: Callable[[], str],
    stdout: TextIO,
    stderr: TextIO,
    fallback_on_transient_exhaustion: bool = False,
    empty_response_fallback_payload: dict[str, Any] | None = None,
) -> Any | None:
    empty_question_reprompts = 0
    empty_response_reprompts = 0
    last_repair_fingerprint: tuple[str, str] | None = None
    while True:
        _verbose_json(
            stderr,
            config.verbose,
            f"{context} LLM input (model={model})",
            messages,
        )
        _print_waiting_for_model(stderr, model)
        try:
            payload = _request_json(client, messages)
            _verbose_json(
                stderr,
                config.verbose,
                f"{context} LLM output (model={model})",
                payload,
            )
        except RuntimeError as exc:
            if isinstance(exc, LocalModelRuntimeError):
                raise
            if _is_model_unavailable_error(exc):
                raise _ModelUnavailableError(
                    f"provider reported that model {model!r} is unavailable"
                ) from exc
            if _is_transient_provider_error(exc):
                retry_attempts = max(1, config.provider_retry_attempts)
                for retry_attempt in range(1, retry_attempts + 1):
                    delay_seconds = max(0.0, config.provider_retry_delay_seconds)
                    print(
                        f"{context} failed for model {model!r}: {exc}. "
                        f"Waiting {delay_seconds:g} seconds before automatic "
                        f"retry {retry_attempt}/{retry_attempts}.",
                        file=stderr,
                    )
                    time.sleep(delay_seconds)
                    try:
                        _print_waiting_for_model(stderr, model)
                        payload = _request_json(client, messages)
                        _verbose_json(
                            stderr,
                            config.verbose,
                            f"{context} LLM retry output (model={model})",
                            payload,
                        )
                        break
                    except RuntimeError as retry_exc:
                        if _is_model_unavailable_error(retry_exc):
                            raise _ModelUnavailableError(
                                f"provider reported that model {model!r} is unavailable"
                            ) from retry_exc
                        exc = retry_exc
                else:
                    payload = None

                if payload is not None:
                    try:
                        return parser(payload)
                    except RuntimeError as retry_parse_exc:
                        print(
                            f"{context} retry response needs repair: {retry_parse_exc}",
                            file=stderr,
                        )
                else:
                    print(
                        f"{context} automatic retries exhausted for model {model!r}.",
                        file=stderr,
                    )
                    if fallback_on_transient_exhaustion:
                        raise _ModelUnavailableError(
                            f"provider retries were exhausted for model {model!r}"
                        ) from exc
            elif _is_json_repairable_error(exc):
                _verbose_print(
                    stderr,
                    config.verbose,
                    f"Attempting automatic repair for {context} after provider failure",
                )
                repair_error_message = str(exc)
                if _is_empty_response_error(exc):
                    repair_error_message += (
                        " The response cannot be empty. Return a complete "
                        "corrected JSON object."
                    )
                try:
                    repaired_payload = _attempt_json_repair(
                        client,
                        messages,
                        context=context,
                        model=model,
                        error_message=repair_error_message,
                        repair_instructions=repair_instructions,
                        stderr=stderr,
                        verbose=config.verbose,
                    )
                except _EmptyProviderResponseError as empty_exc:
                    empty_response_reprompts += 1
                    _print_empty_response_exchange(
                        context=context,
                        model=model,
                        attempts=empty_response_reprompts,
                        provider_error=str(empty_exc),
                        messages=empty_exc.messages or messages,
                        stderr=stderr,
                    )
                    if empty_response_reprompts > 1:
                        if empty_response_fallback_payload is not None:
                            print(
                                f"{context} corrective response was empty; "
                                "interpreting it as next_step.",
                                file=stderr,
                            )
                            return parser(empty_response_fallback_payload)
                        if not _ask_to_retry_empty_response(
                            context=context,
                            model=model,
                            attempts=empty_response_reprompts,
                            provider_error=str(empty_exc),
                            messages=empty_exc.messages or messages,
                            input_func=input_func,
                            stdout=stdout,
                            stderr=stderr,
                        ):
                            return None
                        empty_response_reprompts = 0
                        continue
                    print(
                        f"{context} returned an empty response; requesting a "
                        "corrected response.",
                        file=stderr,
                    )
                    messages = _build_json_repair_messages(
                        messages,
                        context=context,
                        error_message=(
                            f"{empty_exc} The response cannot be empty. Return a "
                            "complete corrected JSON object."
                        ),
                        repair_instructions=repair_instructions,
                        previous_payload=None,
                    )
                    continue
                if repaired_payload is not None:
                    repair_fingerprint = _repair_response_fingerprint(
                        messages,
                        repaired_payload,
                    )
                    if repair_fingerprint == last_repair_fingerprint:
                        print(
                            f"{context} made no progress during response repair; "
                            "stopping.",
                            file=stderr,
                        )
                        return None
                    last_repair_fingerprint = repair_fingerprint
                    try:
                        return parser(repaired_payload)
                    except RuntimeError as repair_exc:
                        print(
                            "Repaired "
                            f"{context} response was still invalid: {repair_exc}",
                            file=stderr,
                        )
                        if _is_invalid_user_question_error(repair_exc):
                            empty_question_reprompts += 1
                            if empty_question_reprompts > _MAX_EMPTY_QUESTION_REPROMPTS:
                                raise RuntimeError(
                                    f"{context} LLM repeatedly returned an invalid "
                                    "user question."
                                ) from repair_exc
                            print(
                                f"{context} received an invalid user question. "
                                "Requesting a properly formed English question "
                                "from the LLM "
                                f"(attempt {empty_question_reprompts}/"
                                f"{_MAX_EMPTY_QUESTION_REPROMPTS}).",
                                file=stderr,
                            )
                            messages = _build_json_repair_messages(
                                messages,
                                context=context,
                                error_message=str(repair_exc),
                                repair_instructions=(
                                    repair_instructions
                                    + " Return a concise, specific, properly formed "
                                    "English question ending with a question mark in "
                                    "the user-question field."
                                ),
                                previous_payload=repaired_payload,
                            )
                            continue
                print(
                    f"{context} repair request failed; requesting the original "
                    "response again with an updated correction instruction.",
                    file=stderr,
                )
                messages = _build_json_repair_messages(
                    messages,
                    context=context,
                    error_message=(
                        f"{exc} The repair request itself failed. Correct the "
                        "original response directly."
                    ),
                    repair_instructions=repair_instructions,
                    previous_payload=None,
                )
                continue
            else:
                print(f"{context} failed: {exc}", file=stderr)
            retry = _prompt_user(
                "Type 'retry' to try again or 'abort' to stop: ",
                input_func=input_func,
                stdout=stdout,
                status_stream=stderr,
            )
            _verbose_print(
                stderr,
                config.verbose,
                f"User chose {retry!r} after {context} failure",
            )
            if retry.strip().lower() == "retry":
                continue
            print(f"Stopping after {context} failure.", file=stderr)
            return None
        assert payload is not None
        try:
            return parser(payload)
        except RuntimeError as exc:
            print(f"{context} response needs repair: {exc}", file=stderr)
            _verbose_print(
                stderr,
                config.verbose,
                f"Attempting automatic repair for {context} after validation failure",
            )
            repair_error_message = str(exc)
            if _is_empty_response_error(exc):
                repair_error_message += (
                    " The response cannot be empty. Return a complete corrected "
                    "JSON object."
                )
            try:
                repaired_payload = _attempt_json_repair(
                    client,
                    messages,
                    context=context,
                    model=model,
                    error_message=repair_error_message,
                    repair_instructions=repair_instructions,
                    previous_payload=payload,
                    stderr=stderr,
                    verbose=config.verbose,
                )
            except _EmptyProviderResponseError as empty_exc:
                empty_response_reprompts += 1
                _print_empty_response_exchange(
                    context=context,
                    model=model,
                    attempts=empty_response_reprompts,
                    provider_error=str(empty_exc),
                    messages=empty_exc.messages or messages,
                    stderr=stderr,
                )
                if empty_response_reprompts > 1:
                    if not _ask_to_retry_empty_response(
                        context=context,
                        model=model,
                        attempts=empty_response_reprompts,
                        provider_error=str(empty_exc),
                        messages=empty_exc.messages or messages,
                        input_func=input_func,
                        stdout=stdout,
                        stderr=stderr,
                    ):
                        return None
                    empty_response_reprompts = 0
                    continue
                print(
                    f"{context} returned an empty response; requesting a corrected "
                    "response.",
                    file=stderr,
                )
                messages = _build_json_repair_messages(
                    messages,
                    context=context,
                    error_message=(
                        f"{empty_exc} The response cannot be empty. Return a "
                        "complete corrected JSON object."
                    ),
                    repair_instructions=repair_instructions,
                    previous_payload=payload,
                )
                continue
            if repaired_payload is not None:
                repair_fingerprint = _repair_response_fingerprint(
                    messages,
                    repaired_payload,
                )
                if repair_fingerprint == last_repair_fingerprint:
                    print(
                        f"{context} made no progress during response repair; stopping.",
                        file=stderr,
                    )
                    return None
                last_repair_fingerprint = repair_fingerprint
                try:
                    return parser(repaired_payload)
                except RuntimeError as repair_exc:
                    print(
                        f"{context} repaired response was still invalid: {repair_exc}",
                        file=stderr,
                    )
                    if _is_invalid_user_question_error(repair_exc):
                        empty_question_reprompts += 1
                        if empty_question_reprompts > _MAX_EMPTY_QUESTION_REPROMPTS:
                            raise RuntimeError(
                                f"{context} LLM repeatedly returned an invalid user "
                                "question."
                            ) from repair_exc
                        print(
                            f"{context} received an invalid user question. "
                            "Requesting a properly formed English question from "
                            "the LLM "
                            f"(attempt {empty_question_reprompts}/"
                            f"{_MAX_EMPTY_QUESTION_REPROMPTS}).",
                            file=stderr,
                        )
                        messages = _build_json_repair_messages(
                            messages,
                            context=context,
                            error_message=str(repair_exc),
                            repair_instructions=(
                                repair_instructions
                                + " Return a concise, specific, properly formed "
                                "English question ending with a question mark in the "
                                "user-question field."
                            ),
                            previous_payload=repaired_payload,
                        )
                        continue
            print(
                f"{context} repair request failed; requesting the original "
                "response again with an updated correction instruction.",
                file=stderr,
            )
            messages = _build_json_repair_messages(
                messages,
                context=context,
                error_message=(
                    f"{exc} The repair request itself failed. Correct the original "
                    "response directly."
                ),
                repair_instructions=repair_instructions,
                previous_payload=payload,
            )
            continue
            retry = _prompt_user(
                "Type 'retry' to try again or 'abort' to stop: ",
                input_func=input_func,
                stdout=stdout,
                status_stream=stderr,
            )
            _verbose_print(
                stderr,
                config.verbose,
                f"User chose {retry!r} after {context} repair failure",
            )
            if retry.strip().lower() == "retry":
                continue
            print(f"Stopping after {context} failure.", file=stderr)
            return None


def _print_waiting_for_model(stderr: TextIO, model: str) -> None:
    print(f"waiting for {model} LLM response...", file=stderr, flush=True)


def _is_invalid_user_question_error(exc: RuntimeError) -> bool:
    return "must be a non-empty, properly formed English question" in str(exc)


def _ask_to_retry_empty_response(
    *,
    context: str,
    model: str,
    attempts: int,
    provider_error: str,
    messages: Sequence[dict[str, str]],
    input_func: Callable[[], str],
    stdout: TextIO,
    stderr: TextIO,
) -> bool:
    """Ask for an explicit recovery choice instead of completing silently."""
    _print_empty_response_exchange(
        context=context,
        model=model,
        attempts=attempts,
        provider_error=provider_error,
        messages=messages,
        stderr=stderr,
    )

    answer = _prompt_user(
        (
            f"{context} returned an empty response after {attempts} corrective "
            "reprompt attempts. Would you like me to retry this LLM request? "
            "Answer 'retry' or 'stop': "
        ),
        input_func=input_func,
        stdout=stdout,
        status_stream=stderr,
    )
    return answer.strip().lower() in {"retry", "yes", "y"}


def _print_empty_response_exchange(
    *,
    context: str,
    model: str,
    attempts: int,
    provider_error: str,
    messages: Sequence[dict[str, str]],
    stderr: TextIO,
) -> None:
    """Print every empty exchange, including ones recovered automatically."""
    diagnostic = (
        f"Empty-response context: {context}; model={model!r}; "
        f"corrective-reprompt-attempts={attempts}; provider_error={provider_error}\n"
        "LLM request messages:\n"
        f"{json.dumps(list(messages), indent=2, ensure_ascii=False)}\n"
        "LLM response: <empty>"
    )
    serialized_prompt = json.dumps(list(messages), ensure_ascii=False)
    print(diagnostic, file=stderr, flush=True)
    print(
        "[workflow] Empty-response exchange: "
        f"prompt={serialized_prompt} response=<empty>",
        file=stderr,
        flush=True,
    )


def _parse_json_object(content: str, context: str) -> dict[str, Any]:
    normalized_content = content.strip()
    try:
        parsed_content = json.loads(normalized_content)
    except json.JSONDecodeError as exc:
        parsed_content = _extract_embedded_json_object(normalized_content)
        if parsed_content is None:
            raise RuntimeError(
                f"{context} was not valid JSON: {exc.msg} at line "
                f"{exc.lineno}, column {exc.colno}.\nResponse content:\n{content}"
            ) from exc
    if not isinstance(parsed_content, dict):
        raise RuntimeError(f"{context} must be a JSON object.")
    return cast("dict[str, Any]", parsed_content)


def _extract_embedded_json_object(content: str) -> dict[str, Any] | None:
    """Accept JSON objects surrounded by common LLM presentation noise."""
    fenced_blocks = re.findall(
        r"```(?:json|JSON)?\s*\n?(.*?)```",
        content,
        flags=re.DOTALL,
    )
    candidates = [*fenced_blocks, content]
    decoder = json.JSONDecoder()
    for candidate in candidates:
        for index, character in enumerate(candidate):
            if character != "{":
                continue
            try:
                parsed, _ = decoder.raw_decode(candidate, index)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return cast("dict[str, Any]", parsed)
    return None


def _normalize_structured_document_text(path: Path, text: str) -> str:
    """Remove a single Markdown fence when an LLM wraps a structured file."""
    if path.suffix.lower() not in {".json", ".yaml", ".yml"}:
        return text
    match = re.fullmatch(
        r"\s*```(?:json|yaml|yml)?\s*\n(.*?)\n?```\s*",
        text,
        flags=re.DOTALL | re.IGNORECASE,
    )
    return match.group(1) + "\n" if match is not None else text


def _validate_structured_document_text(path: Path, text: str) -> None:
    """Reject malformed JSON/YAML before an edit is persisted."""
    suffix = path.suffix.lower()
    if suffix == ".json":
        try:
            json.loads(text)
        except json.JSONDecodeError as exc:
            raise _WorkflowStructuredDocumentError(
                f"Edited JSON file {path} is invalid at line {exc.lineno}, "
                f"column {exc.colno}: {exc.msg}. Correct the JSON before "
                "continuing."
            ) from exc
    elif suffix in {".yaml", ".yml"}:
        try:
            yaml.safe_load(text)
        except yaml.YAMLError as exc:
            problem_mark = getattr(exc, "problem_mark", None)
            if problem_mark is not None:
                location = (
                    f"line {problem_mark.line + 1}, column {problem_mark.column + 1}"
                )
            else:
                location = "an unknown location"
            problem = getattr(exc, "problem", None) or str(exc)
            raise _WorkflowStructuredDocumentError(
                f"Edited YAML file {path} is invalid at {location}: {problem}. "
                "Correct the YAML before continuing."
            ) from exc


def _edit_to_data(edit: SkillChatEdit) -> dict[str, Any]:
    data: dict[str, Any] = {
        "kind": edit.kind,
        "start_line": edit.start_line,
    }
    if edit.end_line is not None:
        data["end_line"] = edit.end_line
    if edit.text is not None:
        data["text"] = edit.text
    return data


def _yaml_operation_to_data(operation: SkillChatYamlOperation) -> dict[str, Any]:
    data: dict[str, Any] = {"op": operation.operation}
    if operation.section is not None:
        data["section"] = operation.section
    if operation.item_id is not None:
        data["id"] = operation.item_id
    if operation.path:
        data["path"] = list(operation.path)
    if operation.value is not None:
        data["value"] = operation.value
    return data


def _file_edits_to_data(file_edits: SkillChatFileEdits) -> dict[str, Any]:
    return {
        "file_path": file_edits.file_path,
        "edits": [_edit_to_data(edit) for edit in file_edits.edits],
    }


def _current_file_context(
    worktree_root: Path,
    current_file_path: Path | None,
    *,
    cache: dict[tuple[str, int, int], dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    if current_file_path is None:
        return None

    resolved_path = _resolve_worktree_file_path(
        str(current_file_path),
        worktree_root,
    )
    if not resolved_path.exists():
        return {
            "path": str(resolved_path.relative_to(worktree_root)),
            "exists": False,
        }
    if not resolved_path.is_file():
        return {
            "path": str(resolved_path.relative_to(worktree_root)),
            "exists": False,
        }

    stat = resolved_path.stat()
    cache_key = (str(resolved_path), stat.st_mtime_ns, stat.st_size)
    if cache is not None and cache_key in cache:
        return cache[cache_key]
    lines = resolved_path.read_text(encoding="utf-8").splitlines()
    context = {
        "path": str(resolved_path.relative_to(worktree_root)),
        "exists": True,
        "line_count": len(lines),
        "lines": [
            {
                "line_number": line_number,
                "text": line,
            }
            for line_number, line in enumerate(lines, start=1)
        ],
    }
    if cache is not None:
        cache.clear()
        cache[cache_key] = context
    return context


def _apply_file_edits(current_text: str, edits: Sequence[SkillChatEdit]) -> str:
    lines = current_text.splitlines()
    for edit in sorted(edits, key=_edit_sort_key, reverse=True):
        start_index = edit.start_line - 1
        if edit.kind == "add":
            if start_index > len(lines):
                raise _WorkflowEditRangeError(
                    "Workflow edit action add start_line "
                    f"{edit.start_line} is beyond the end of the file, which has "
                    f"{len(lines)} lines."
                )
            insert_lines = edit.text.splitlines() if edit.text is not None else []
            lines[start_index:start_index] = insert_lines
            continue

        end_line = edit.end_line if edit.end_line is not None else edit.start_line
        end_index = end_line
        if end_index > len(lines):
            raise _WorkflowEditRangeError(
                "Workflow edit action range ends at line "
                f"{end_line}, but the file has {len(lines)} lines."
            )

        if edit.kind == "remove":
            del lines[start_index:end_index]
            continue

        replacement_lines = edit.text.splitlines() if edit.text is not None else []
        lines[start_index:end_index] = replacement_lines

    if not lines:
        return ""
    return "\n".join(lines) + "\n"


def _apply_yaml_operations(
    path: Path,
    current_text: str,
    operations: Sequence[SkillChatYamlOperation],
) -> str:
    if path.suffix.lower() not in {".yaml", ".yml"}:
        raise _WorkflowYamlEditError(
            "yaml_edit only supports .yaml and .yml files. Use edit for other "
            "file types."
        )
    try:
        document = yaml.safe_load(current_text)
    except yaml.YAMLError as exc:
        raise _WorkflowYamlEditError(
            f"Cannot apply yaml_edit because {path} is already invalid YAML: "
            f"{exc}. Repair the YAML with edit first, then retry yaml_edit."
        ) from exc
    if document is None:
        document = {}
    if not isinstance(document, Mapping):
        raise _WorkflowYamlEditError(
            "yaml_edit requires the document root to be a YAML mapping."
        )
    updated: dict[str, Any] = dict(document)

    for operation in operations:
        if operation.operation == "set_value":
            if not operation.path:
                raise _WorkflowYamlEditError(
                    "set_value requires a non-empty path, for example "
                    '["title"] or ["features", "0", "description"].'
                )
            target: Any = updated
            for key in operation.path[:-1]:
                if not isinstance(target, dict) or key not in target:
                    raise _WorkflowYamlEditError(
                        f"set_value path {list(operation.path)!r} cannot find "
                        f"mapping key {key!r}. Re-read the YAML and use existing "
                        "mapping keys in the path."
                    )
                target = target[key]
            if not isinstance(target, dict):
                raise _WorkflowYamlEditError(
                    f"set_value path {list(operation.path)!r} does not resolve "
                    "to a YAML mapping."
                )
            target[operation.path[-1]] = operation.value
            continue

        if operation.section is None or operation.item_id is None:
            raise _WorkflowYamlEditError(
                f"{operation.operation} requires section and id. Use an operation "
                'such as {"op": "upsert_item", "section": "features", '
                '"id": "feature-id", "value": {...}}.'
            )
        raw_items = updated.get(operation.section)
        if raw_items is None and operation.operation == "upsert_item":
            raw_items = []
            updated[operation.section] = raw_items
        if not isinstance(raw_items, list):
            raise _WorkflowYamlEditError(
                f"YAML section {operation.section!r} must be a list for "
                f"{operation.operation}. Re-read the document and use the exact "
                "section name."
            )

        matching_indexes = [
            index
            for index, item in enumerate(raw_items)
            if isinstance(item, Mapping) and item.get("id") == operation.item_id
        ]
        if len(matching_indexes) > 1:
            raise _WorkflowYamlEditError(
                f"YAML section {operation.section!r} contains duplicate id "
                f"{operation.item_id!r}; repair the duplicates before yaml_edit."
            )
        if operation.operation == "remove_item":
            if not matching_indexes:
                raise _WorkflowYamlEditError(
                    f"No item with id {operation.item_id!r} exists in section "
                    f"{operation.section!r}. Use read_document to inspect ids "
                    "before retrying."
                )
            del raw_items[matching_indexes[0]]
            continue

        if operation.operation == "upsert_item":
            if not isinstance(operation.value, Mapping):
                raise _WorkflowYamlEditError(
                    "upsert_item requires value to be a mapping containing the "
                    "complete item fields."
                )
            item = dict(operation.value)
            item["id"] = operation.item_id
            if matching_indexes:
                raw_items[matching_indexes[0]] = item
            else:
                raw_items[:] = [
                    existing
                    for existing in raw_items
                    if not (
                        isinstance(existing, Mapping) and existing.get("id") is None
                    )
                ]
                raw_items.append(item)
            continue

        raise _WorkflowYamlEditError(
            f"Unsupported yaml_edit operation {operation.operation!r}. Use "
            "upsert_item, remove_item, or set_value."
        )

    return yaml.safe_dump(
        updated,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
    )


def _workflow_edit_failure_feedback(
    action: SkillChatAction,
    error: Exception,
    current_file_context: dict[str, Any] | None,
) -> str:
    feedback = (
        f"Workflow {action.kind} action failed: {error}. "
        "Re-read the current file context and return a corrected action."
    )
    if isinstance(error, _WorkflowEditRangeError):
        if current_file_context and current_file_context.get("exists"):
            feedback += (
                " The current file has "
                f"{current_file_context['line_count']} lines; every edit range "
                "must stay within that line count."
            )
    elif isinstance(error, _WorkflowStructuredDocumentError):
        feedback += (
            " The edit range was within the file, but the resulting structured "
            "document is invalid. Preserve surrounding mapping keys and YAML "
            "indentation, such as section headers like `entities:`. If a prose "
            "value contains embedded double quotes, colons, or YAML punctuation, "
            "use a single-quoted scalar or a `>-` block scalar; do not use "
            "unescaped double quotes inside a double-quoted value. Correct the "
            "document before retrying."
        )
    elif isinstance(error, _WorkflowYamlEditError):
        feedback += (
            " Use yaml_edit only for .yaml or .yml files. Its operations are "
            "structural: upsert_item replaces or appends a list item by section "
            "and id, remove_item deletes one by section and id, and set_value "
            "updates a mapping value by path. Do not use line numbers or replace "
            "section headers."
        )
    return feedback


def _rejected_edit_guidance(action: SkillChatAction) -> str:
    if action.kind not in {"edit", "yaml_edit"}:
        return ""
    return (
        "\n\nLast proposed edit (NOT APPLIED):\n"
        f"{_workflow_action_signature(action)}\n"
        "Do not repeat it unchanged. Re-read the current file and return a "
        "corrected action using the structural yaml_edit contract when editing "
        "YAML."
    )


def _edit_sort_key(edit: SkillChatEdit) -> tuple[int, int]:
    end_line = edit.end_line if edit.end_line is not None else edit.start_line
    return edit.start_line, end_line


def _resolve_worktree_file_path(file_path_value: str, worktree_root: Path) -> Path:
    resolved_path = Path(file_path_value.strip())
    if resolved_path.is_absolute():
        candidate_path = resolved_path.resolve(strict=False)
    else:
        candidate_path = (worktree_root / resolved_path).resolve(strict=False)

    resolved_worktree_root = worktree_root.resolve(strict=False)
    if not candidate_path.is_relative_to(resolved_worktree_root):
        raise RuntimeError(
            f"Workflow edit action file_path must stay within {resolved_worktree_root}."
        )
    return candidate_path


def _resolve_generated_file_path_from_command(
    command: object,
    *,
    worktree_root: Path,
) -> Path | None:
    command_items = _command_items(command)
    if not command_items or command_items[0] != "powdrr-lift" or len(command_items) < 2:
        return None

    output_path_value = _extract_command_option(command_items, "--output")
    if output_path_value is not None:
        return _resolve_worktree_file_path(output_path_value, worktree_root)

    work_item_name = _extract_command_option(command_items, "--work-item-name")
    if work_item_name is None:
        return None

    subcommand = command_items[1]
    if subcommand == "system-specification":
        return system_specification_default_output_path(work_item_name, worktree_root)
    if subcommand == "architecture-specification":
        return architecture_specification_default_output_path(
            work_item_name,
            worktree_root,
        )
    if subcommand == "implementation-specification":
        return implementation_specification_default_output_path(
            work_item_name,
            worktree_root,
        )
    if subcommand == "pr-specification":
        return pr_specification_default_output_path(work_item_name, worktree_root)
    if subcommand == "feature-pr-specification":
        return feature_pr_specification_default_output_path(
            work_item_name,
            worktree_root,
        )
    if subcommand == "system-map-specification":
        return system_map_specification_default_output_path(
            work_item_name,
            worktree_root,
        )
    if subcommand == "current-state":
        return current_state_specification_default_output_path(worktree_root)
    if subcommand == "codebase-state":
        return codebase_state_default_output_path(worktree_root)
    return None


def _command_items(command: object) -> list[str]:
    if isinstance(command, str):
        return [item for item in shlex.split(command) if item]
    if isinstance(command, Sequence) and not isinstance(
        command,
        (str, bytes, bytearray),
    ):
        items: list[str] = []
        for item in command:
            if not isinstance(item, str):
                raise RuntimeError(
                    "Workflow invoke_tool action command items must be strings."
                )
            normalized_item = item.strip()
            if normalized_item:
                items.append(normalized_item)
        return items
    return []


def _extract_command_option(
    command_items: Sequence[str],
    option_name: str,
) -> str | None:
    for index, item in enumerate(command_items):
        if item != option_name:
            continue
        if index + 1 >= len(command_items):
            return None
        return command_items[index + 1]
    return None


def _attempt_json_repair(
    client: WorkflowLLMClient,
    messages: Sequence[dict[str, str]],
    *,
    context: str,
    model: str,
    error_message: str,
    repair_instructions: str,
    stderr: TextIO,
    verbose: bool,
    previous_payload: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    repair_messages = _build_json_repair_messages(
        messages,
        context=context,
        error_message=error_message,
        repair_instructions=repair_instructions,
        previous_payload=previous_payload,
    )
    _verbose_json(
        stderr,
        verbose,
        f"{context} repair LLM input (model={model})",
        repair_messages,
    )
    try:
        _print_waiting_for_model(stderr, model)
        repaired_payload = _request_json(client, repair_messages)
        _verbose_json(
            stderr,
            verbose,
            f"{context} repair LLM output (model={model})",
            repaired_payload,
        )
        return repaired_payload
    except RuntimeError as exc:
        if isinstance(exc, LocalModelRuntimeError):
            raise
        if _is_empty_response_error(exc):
            raise _EmptyProviderResponseError(
                str(exc),
                messages=repair_messages,
            ) from exc
        print(f"{context} repair request failed: {exc}", file=stderr)
    return None


def _repair_response_fingerprint(
    messages: Sequence[dict[str, str]],
    payload: dict[str, Any],
) -> tuple[str, str]:
    return (
        json.dumps(messages, ensure_ascii=False, sort_keys=True),
        json.dumps(payload, ensure_ascii=False, sort_keys=True),
    )


def _build_json_repair_messages(
    messages: Sequence[dict[str, str]],
    *,
    context: str,
    error_message: str,
    repair_instructions: str,
    previous_payload: dict[str, Any] | None,
) -> list[dict[str, str]]:
    repair_message = (
        f"The previous {context} response was invalid because: {error_message}\n"
        f"{repair_instructions}\n"
        "Return only a corrected JSON object with no markdown or commentary."
    )
    if previous_payload is not None:
        repair_message += (
            "\nPrevious response:\n"
            f"{json.dumps(previous_payload, indent=2, ensure_ascii=False)}"
        )
    repaired_messages = list(messages)
    if previous_payload is not None:
        repaired_messages.append(
            {
                "role": "assistant",
                "content": json.dumps(previous_payload, indent=2, ensure_ascii=False),
            }
        )
    repaired_messages.append({"role": "user", "content": repair_message})
    return repaired_messages


def _is_json_repairable_error(exc: RuntimeError) -> bool:
    message = str(exc).lower()
    return any(
        phrase in message
        for phrase in (
            "was not valid json",
            "content was empty",
            "did not include any content",
            "did not include any choices",
            "choice was not an object",
            "message was not an object",
            "must be a json object",
        )
    )


def _is_empty_response_error(exc: RuntimeError) -> bool:
    return "response message content was empty" in str(exc).lower() or (
        "response content was empty" in str(exc).lower()
    )


def _is_transient_provider_error(exc: RuntimeError) -> bool:
    message = str(exc).lower()
    return (
        "http 429" in message
        or '"code":"1305"' in message
        or "temporarily overloaded" in message
        or _is_timeout_error(exc)
    )


def _is_timeout_error(exc: RuntimeError) -> bool:
    message = str(exc).lower()
    return "timed out" in message or "timeout" in message


def _is_model_unavailable_error(exc: RuntimeError) -> bool:
    message = str(exc).lower()
    return "model" in message and (
        "not available" in message
        or "unavailable" in message
        or "not found" in message
        or "does not exist" in message
        or "unsupported" in message
    )


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


def _action_repair_prompt(
    selected_skill: SkillCatalogEntry,
    *,
    failed_action: SkillChatAction | None = None,
    validation_error: str | None = None,
) -> str:
    prompt = (
        "Generate a JSON document selecting the best action based on this "
        "context. The available actions are: gather_context to discover "
        "repository specifications before deciding; prompt_user to ask one "
        "necessary human question; edit to make a known line-based file change; "
        "invoke_skill to run a listed nested skill; yaml_edit to make a "
        "structural YAML change; invoke_tool to run a shell, "
        "fuzzy-match, or basedpyright query; read_document to "
        "request a bounded line range from a known document; next_step when the "
        "current step is complete; and complete when the skill is finished.\n"
        "If the original action response was empty, choose next_step when the "
        "current step is complete instead of returning an empty response. If "
        "this corrective response is also empty, the system will interpret it "
        "as next_step.\n"
        "Return exactly one JSON object with a kind and the fields required by "
        "that action. Use file_path and edits or file_edits for edit, file_path "
        "and operations for yaml_edit, tool and "
        "parameters.command for invoke_tool, skill for invoke_skill, file_path "
        "with positive start_line "
        "and end_line for read_document, non-empty types for gather_context, "
        "and a clear English question ending in '?' for prompt_user. Do not "
        "combine actions or output markdown."
    )
    if validation_error is not None:
        prompt += (
            "\nThe previous action returned a validation_error and was not "
            "executed. Do not repeat it unchanged. Read the validation_error "
            "message and return an action that matches the current step's "
            "declared tool template exactly."
        )
    if failed_action is not None:
        prompt += (
            "\nThe previous edit action failed and was not applied. don't do this "
            "again; do not repeat it "
            "unchanged; reread the current file and return a corrected action. "
            "For YAML, use yaml_edit with upsert_item, remove_item, or set_value "
            "instead of line numbers. The "
            "rejected edit was:\n"
            f"{_workflow_action_signature(failed_action)}"
        )
    return prompt


def _prompt_user(
    prompt: str,
    *,
    input_func: Callable[[], str],
    stdout: TextIO,
    status_stream: TextIO | None = None,
) -> str:
    if input_func is input and _supports_readline_input(stdout):
        answer = input(prompt).strip()
    else:
        stdout.write(prompt)
        stdout.flush()
        if input_func is input and _supports_interactive_line_editing(stdout):
            answer = _read_interactive_line(prompt, stdout=stdout).strip()
        else:
            answer = input_func().strip()
        if input_func is not input:
            stdout.write("\n")
            stdout.flush()
    if status_stream is not None:
        print("[workflow] calling LLM...", file=status_stream, flush=True)
    return answer


def _supports_readline_input(stdout: TextIO) -> bool:
    """Return whether native readline can safely own this terminal prompt."""
    return sys.stdin.isatty() and stdout.isatty() and readline is not None


def _supports_interactive_line_editing(stdout: TextIO) -> bool:
    """Return whether the process has a terminal we can safely edit in place."""
    return sys.stdin.isatty() and stdout.isatty() and hasattr(termios, "tcgetattr")


def _read_interactive_line(prompt: str, *, stdout: TextIO) -> str:
    """Read a line with cursor movement support when readline is unavailable."""
    stdin = sys.stdin
    chars: list[str] = []
    cursor = 0
    original_attributes = termios.tcgetattr(stdin.fileno())

    def redraw() -> None:
        # Repaint the line and position the cursor after the edited prefix.
        stdout.write("\r" + prompt + "".join(chars) + "\x1b[K")
        distance_from_end = len(chars) - cursor
        if distance_from_end:
            stdout.write(f"\x1b[{distance_from_end}D")
        stdout.flush()

    try:
        tty.setraw(stdin.fileno())
        while True:
            character = stdin.read(1)
            if character in {"\r", "\n"}:
                stdout.write("\r\n")
                stdout.flush()
                return "".join(chars)
            if character == "\x03":
                raise KeyboardInterrupt
            if character == "\x04":
                if not chars:
                    raise EOFError
                continue
            if character in {"\x7f", "\b"}:
                if cursor:
                    del chars[cursor - 1]
                    cursor -= 1
                    redraw()
                continue
            if character != "\x1b":
                chars[cursor:cursor] = [character]
                cursor += 1
                stdout.write(character)
                stdout.flush()
                continue

            # Arrow and editing keys arrive as ANSI escape sequences. Read the
            # rest only when it is immediately available so a lone Escape can
            # still be entered as a normal control key without hanging.
            sequence = ""
            while select.select([stdin], [], [], 0.01)[0]:
                sequence += stdin.read(1)
                if sequence[-1:] in "~ABCDEFGH":
                    break
            if sequence in {"[D", "OD"}:
                cursor = max(0, cursor - 1)
            elif sequence in {"[C", "OC"}:
                cursor = min(len(chars), cursor + 1)
            elif sequence in {"[H", "OH", "[1~"}:
                cursor = 0
            elif sequence in {"[F", "OF", "[4~"}:
                cursor = len(chars)
            elif sequence == "[3~" and cursor < len(chars):
                del chars[cursor]
            if sequence:
                redraw()
    finally:
        termios.tcsetattr(stdin.fileno(), termios.TCSADRAIN, original_attributes)


def _build_chat_client(
    credentials: WorkflowChatCredentials,
    *,
    model: str,
    model_cache_dir: Path,
    progress_stream: TextIO | None = None,
) -> WorkflowLLMClient:
    provider = _provider_definition(credentials.provider)
    if provider.client_kind == "local":
        resolved_model_path = _resolve_local_model_path(model_cache_dir)
        return LocalLlamaChatClient(
            model_path=resolved_model_path,
            n_ctx=_resolve_local_model_context(),
        )
    limits = _model_limits_for(credentials.provider, model)
    if provider.client_kind == "anthropic":
        return AnthropicChatClient(
            model=model,
            api_key=credentials.api_key,
            base_url=credentials.base_url,
            limits=limits,
        )
    return OpenAIChatClient(
        model=model,
        api_key=credentials.api_key,
        base_url=credentials.base_url,
        limits=limits,
        progress_stream=progress_stream,
    )


def _model_limits_for(provider: str, model: str) -> LLMModelLimits:
    definition = _provider_definition(provider)
    if definition.client_kind == "local":
        return LLMModelLimits(
            context_window=_resolve_local_model_context(),
            max_output_tokens=_MAX_COMPLETION_TOKENS,
        )
    return definition.model_limits.get(model.casefold(), _DEFAULT_MODEL_LIMITS)


def _resolve_credentials(
    provider: str,
    api_key_override: str | None,
    base_url_override: str | None,
) -> WorkflowChatCredentials:
    api_key, source = _resolve_api_key(provider, api_key_override)
    base_url, base_url_source = _resolve_base_url(provider, base_url_override)
    return WorkflowChatCredentials(
        provider=provider,
        api_key=api_key,
        source=source,
        base_url=base_url,
        base_url_source=base_url_source,
    )


def _resolve_provider(
    provider_override: str,
    model: str,
    *,
    mapping: LLMModelMapping | None = None,
) -> str:
    if mapping is not None:
        _provider_definition(mapping.provider)
        return mapping.provider
    if provider_override != "auto":
        _provider_definition(provider_override)
        return provider_override
    candidates = _auto_provider_candidates()
    if candidates:
        return candidates[0]
    if model.startswith("claude-"):
        return "anthropic"
    return "openai"


def _resolve_provider_roles(config: SkillChatConfig) -> LLMProviderRoles:
    """Resolve the two opaque provider roles used by workflow execution."""
    if config.normal_provider is not None:
        normal = config.normal_provider
    elif config.provider == "auto":
        candidates = _auto_provider_candidates()
        normal = candidates[0] if candidates else "openai"
    else:
        normal = config.provider
    _provider_definition(normal)

    adversarial = config.adversarial_provider
    if adversarial is None and config.provider == "auto":
        candidates = _auto_provider_candidates()
        adversarial = next(
            (candidate for candidate in candidates if candidate != normal),
            None,
        )
    if adversarial is not None:
        _provider_definition(adversarial)
    return LLMProviderRoles(normal=normal, adversarial=adversarial)


def _auto_provider_candidates() -> tuple[str, ...]:
    """Return configured providers in their declared automatic priority order."""
    candidates: list[tuple[int, str]] = []
    for name, definition in LLM_PROVIDERS.items():
        if definition.auto_priority is None or not _provider_has_credentials(name):
            continue
        # DeepInfra Cheap is the automatic mode for a DeepInfra credential;
        # standard DeepInfra remains available through explicit selection.
        candidates.append((definition.auto_priority, name))
    return tuple(name for _, name in sorted(candidates))


def _provider_has_credentials(provider: str) -> bool:
    definition = _provider_definition(provider)
    if provider == "openai" and _resolve_codex_access_token() is not None:
        return True
    return any(
        os.environ.get(env_name)
        for env_name in definition.api_key_env_names + definition.base_url_env_names
    )


def _resolve_api_key(provider: str, override: str | None) -> tuple[str, str]:
    if override:
        return override, "--api-key"
    definition = _provider_definition(provider)
    if definition.client_kind == "local":
        return "local", "local"
    for env_name in definition.api_key_env_names:
        value = os.environ.get(env_name)
        if value:
            return value, env_name
    if provider == "openai":
        codex_token = _resolve_codex_access_token()
        if codex_token is not None:
            return codex_token, _codex_auth_path_description()
    if provider == "openai":
        raise RuntimeError(
            "No OpenAI credentials found. Set OPENAI_API_KEY, CODEX_API_KEY, or "
            "sign in with Codex so ~/.codex/auth.json is available."
        )
    credential_names = " or ".join(definition.api_key_env_names)
    raise RuntimeError(
        f"No {definition.display_name} credentials found. Set {credential_names}, "
        "or pass --api-key."
    )


def _resolve_local_model_path(model_cache_dir: Path) -> Path:
    cached_model_paths = sorted(model_cache_dir.glob(_LOCAL_MODEL_PATTERN))
    if _has_all_local_model_shards(cached_model_paths):
        return cached_model_paths[0]
    raise RuntimeError(
        "The local Qwen model is not fully cached. Run "
        "`powdrr-lift download-qwen-model` before starting workflow-chat. "
        f"Expected cache={model_cache_dir}."
    )


def download_local_qwen_model(model_cache_dir: Path) -> Path:
    """Download the local Qwen GGUF shards into the configured cache."""
    model_cache_dir.mkdir(parents=True, exist_ok=True)
    cached_model_paths = sorted(model_cache_dir.glob(_LOCAL_MODEL_PATTERN))
    if _has_all_local_model_shards(cached_model_paths):
        return cached_model_paths[0]
    try:
        from huggingface_hub import snapshot_download
    except ImportError as exc:
        raise RuntimeError(
            "Automatic local model downloads require huggingface-hub."
        ) from exc
    try:
        snapshot_directory = Path(
            snapshot_download(
                repo_id=_LOCAL_MODEL_REPOSITORY,
                allow_patterns=[_LOCAL_MODEL_PATTERN],
                local_dir=str(model_cache_dir),
            )
        )
    except Exception as exc:
        raise RuntimeError(
            "Could not download the Qwen Q5_K_M model from Hugging Face. "
            f"Repository={_LOCAL_MODEL_REPOSITORY}, cache={model_cache_dir}. "
            f"Underlying error: {type(exc).__name__}: {exc}"
        ) from exc
    model_paths = sorted(snapshot_directory.glob(_LOCAL_MODEL_PATTERN))
    if not _has_all_local_model_shards(model_paths):
        raise RuntimeError(
            "The Hugging Face Qwen repository did not provide all Q5_K_M GGUF shards."
        )
    return model_paths[0]


def _has_all_local_model_shards(model_paths: Sequence[Path]) -> bool:
    if not model_paths:
        return False
    match = re.search(r"-00001-of-(\d+)\.gguf$", model_paths[0].name)
    expected_shards = int(match.group(1)) if match else 1
    return len(model_paths) >= expected_shards


def _resolve_local_model_context() -> int:
    configured_context = os.environ.get(_LOCAL_MODEL_CONTEXT_ENV)
    if configured_context is None or not configured_context.strip():
        return _DEFAULT_LOCAL_MODEL_CONTEXT
    try:
        context = int(configured_context)
    except ValueError as exc:
        raise RuntimeError(
            f"{_LOCAL_MODEL_CONTEXT_ENV} must be a positive integer; got "
            f"{configured_context!r}."
        ) from exc
    if context <= 0:
        raise RuntimeError(
            f"{_LOCAL_MODEL_CONTEXT_ENV} must be a positive integer; got "
            f"{configured_context!r}."
        )
    return context


def _resolve_base_url(provider: str, override: str | None) -> tuple[str, str]:
    if override:
        return override, "--base-url"
    definition = _provider_definition(provider)
    if definition.client_kind == "local":
        return "local", "local"
    for env_name in definition.base_url_env_names:
        value = os.environ.get(env_name)
        if value:
            return value, env_name
    return definition.default_base_url, "default"


def _resolve_codex_access_token() -> str | None:
    auth_path = _resolve_codex_auth_path()
    if not auth_path.exists():
        return None

    try:
        raw_auth = json.loads(auth_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(raw_auth, dict):
        return None

    tokens = raw_auth.get("tokens")
    if not isinstance(tokens, dict):
        return None

    access_token = tokens.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        return None

    expiry = tokens.get("expiry")
    if isinstance(expiry, str):
        try:
            expiry_dt = datetime.fromisoformat(expiry)
        except ValueError:
            return access_token
        if expiry_dt.tzinfo is None:
            expiry_dt = expiry_dt.replace(tzinfo=UTC)
        if expiry_dt <= datetime.now(UTC):
            return None

    return access_token


def _resolve_codex_auth_path() -> Path:
    codex_home = os.environ.get("CODEX_HOME")
    if codex_home is not None:
        return Path(codex_home).expanduser() / "auth.json"

    return Path.home() / ".codex" / "auth.json"


def _codex_auth_path_description() -> str:
    return str(_resolve_codex_auth_path())


def _split_system_message(
    messages: list[dict[str, str]],
) -> tuple[str | None, list[dict[str, str]]]:
    if not messages:
        return None, []
    first_message = messages[0]
    if first_message.get("role") != "system":
        return None, list(messages)
    system_content = first_message.get("content")
    if not isinstance(system_content, str):
        return None, list(messages)
    return system_content, list(messages[1:])


def _anthropic_message(message: dict[str, str]) -> dict[str, Any]:
    role = message.get("role")
    content = message.get("content")
    if role not in {"user", "assistant"}:
        raise RuntimeError(
            "Anthropic messages must use user or assistant roles after splitting "
            "the system prompt."
        )
    if not isinstance(content, str):
        raise RuntimeError("Anthropic message content must be a string.")
    return {
        "role": role,
        "content": [{"type": "text", "text": content}],
    }
