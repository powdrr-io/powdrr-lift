from __future__ import annotations

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
from pathlib import Path
from typing import Any, Protocol, TextIO, cast
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

try:
    import readline
    import termios
    import tty
except ImportError:  # pragma: no cover - only used on non-POSIX platforms
    readline = None  # type: ignore[assignment]
    termios = None  # type: ignore[assignment]
    tty = None  # type: ignore[assignment]

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

_DEFAULT_MODEL = "glm-5.2"
_DEFAULT_LLM_TYPE = "high_reasoning"
_MAX_COMPLETION_TOKENS = 32768
_MAX_EMPTY_QUESTION_REPROMPTS = 3
_QWEN_2_5_CODER_MODEL = "Qwen/Qwen2.5-Coder-14B-Instruct"
_LOCAL_MODEL_REPOSITORY = "Qwen/Qwen2.5-Coder-14B-Instruct-GGUF"
_LOCAL_MODEL_PATTERN = "qwen2.5-coder-14b-instruct-q5_k_m*.gguf"
_DEFAULT_LOCAL_MODEL_CONTEXT = 24576
_LOCAL_MODEL_CONTEXT_ENV = "POWDRR_LOCAL_MODEL_CONTEXT"
_TOKEN_ESTIMATE_CHARS_PER_TOKEN = 3
_CONTEXT_SAFETY_MARGIN_TOKENS = 1024


@dataclass(frozen=True, slots=True)
class LLMModelLimits:
    context_window: int
    max_output_tokens: int


@dataclass(frozen=True, slots=True)
class LLMModelMapping:
    model: str
    provider: str
    backup_model: LLMModelMapping | None = None


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
    ),
    "fast_iteration": LLMModelMapping(_QWEN_2_5_CODER_MODEL, provider="local"),
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
        "Qwen/Qwen3-Next-80B-A3B-Instruct", provider="deepinfra"
    ),
    "fast_iteration": LLMModelMapping(
        "Qwen/Qwen3-Next-80B-A3B-Instruct", provider="deepinfra"
    ),
    "long_context": LLMModelMapping(
        "deepseek-ai/DeepSeek-V4-Flash", provider="deepinfra"
    ),
    "vision": LLMModelMapping("Qwen/Qwen2.5-VL-32B-Instruct", provider="deepinfra"),
}

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


WorkflowTemplateCatalogEntry = SkillCatalogEntry
WorkflowChatConfig = SkillChatConfig
WorkflowChatResult = SkillChatResult
WorkflowChatSelection = SkillChatSelection


@dataclass(frozen=True, slots=True)
class SkillChatAction:
    kind: str
    tool: str | None = None
    file_path: str | None = None
    text: str | None = None
    parameters: dict[str, Any] = field(default_factory=dict)
    edits: tuple[SkillChatEdit, ...] = field(default_factory=tuple)
    file_edits: tuple[SkillChatFileEdits, ...] = field(default_factory=tuple)
    types: tuple[str, ...] = field(default_factory=tuple)
    keywords: tuple[str, ...] = field(default_factory=tuple)
    decisions_and_context: str | None = None
    llm_type: str | None = None


@dataclass(frozen=True, slots=True)
class SkillChatEdit:
    kind: str
    start_line: int
    end_line: int | None = None
    text: str | None = None


@dataclass(frozen=True, slots=True)
class SkillChatFileEdits:
    file_path: str
    edits: tuple[SkillChatEdit, ...]


@dataclass(slots=True)
class _WorkflowExecutionState:
    selected_skill: SkillCatalogEntry
    transcript: list[dict[str, str]]
    execution_events: list[dict[str, Any]]
    execution_context: list[str]
    step_index: int
    worktree_root: Path
    current_file_path: Path | None = None


@dataclass(frozen=True, slots=True)
class WorkflowChatCredentials:
    provider: str
    api_key: str
    source: str
    base_url: str
    base_url_source: str


class OpenAIChatClient:
    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        base_url: str,
        timeout: float = 120.0,
        limits: LLMModelLimits | None = None,
    ) -> None:
        self._model = model
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._limits = limits or _DEFAULT_MODEL_LIMITS

    def complete_json(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        max_tokens, estimated_input_tokens = _request_token_budget(
            messages,
            self._limits,
        )
        payload = {
            "model": self._model,
            "messages": messages,
            "temperature": 0,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
        }
        request = Request(
            f"{self._base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        request_started = time.monotonic()
        try:
            with urlopen(request, timeout=self._timeout) as response:
                raw_response = response.read().decode("utf-8")
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

    def complete_json(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        max_tokens, estimated_input_tokens = _request_token_budget(
            messages,
            self._limits,
        )
        system_prompt, conversation_messages = _split_system_message(messages)
        payload: dict[str, Any] = {
            "model": self._model,
            "max_tokens": max_tokens,
            "messages": [
                _anthropic_message(message) for message in conversation_messages
            ],
        }
        if system_prompt is not None:
            payload["system"] = system_prompt

        request = Request(
            f"{self._base_url}/v1/messages",
            data=json.dumps(payload).encode("utf-8"),
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


def _request_token_budget(
    messages: list[dict[str, str]],
    limits: LLMModelLimits,
) -> tuple[int, int]:
    estimated_input_tokens = _estimate_message_tokens(messages)
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


def _estimate_message_tokens(messages: list[dict[str, str]]) -> int:
    serialized = json.dumps(messages, ensure_ascii=False, separators=(",", ":"))
    return max(
        1,
        math.ceil(len(serialized) / _TOKEN_ESTIMATE_CHARS_PER_TOKEN),
    )


class WorkflowChatClient(Protocol):
    def complete_json(self, messages: list[dict[str, str]]) -> dict[str, Any]: ...


class _ModelUnavailableError(RuntimeError):
    pass


class LocalModelRuntimeError(RuntimeError):
    """Raised when the required local GPU model cannot run."""


class _WorkflowProgressDisplay:
    def __init__(
        self,
        stream: TextIO,
        on_update: Callable[[SkillCatalogEntry, int, str], None] | None = None,
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
    ) -> None:
        if self._on_update is not None:
            self._on_update(skill, current_step_index, status)
            self._last_step_index = current_step_index
            return
        if not self._dynamic and self._last_step_index == current_step_index:
            print(f"[workflow] {status}", file=self._stream, flush=True)
            return

        lines = ["Workflow progress:"]
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
    progress_callback: Callable[[SkillCatalogEntry, int, str], None] | None = None,
) -> int:
    configured_repo_root = resolve_repo_root(config.repo_root)
    worktree_root = _resolve_worktree_context(
        config.repo_root,
        stderr=stderr,
        verbose=config.verbose,
    )
    repo_root = worktree_root
    project_root = _resolve_project_root(configured_repo_root, worktree_root)
    skills_dir = config.skills_dir
    if not skills_dir.is_absolute():
        skills_dir = repo_root / skills_dir
    output_dir = config.output_dir
    if output_dir is not None and not output_dir.is_absolute():
        output_dir = repo_root / output_dir

    catalog = _load_skill_catalog(skills_dir, stderr=stderr)
    if not catalog:
        print(f"No skills found in {skills_dir}.", file=stderr)
        return 1

    current_model = config.model
    provider = _resolve_provider(config.provider, current_model)
    credentials = _resolve_credentials(provider, config.api_key, config.base_url)
    clients: dict[tuple[str, str], WorkflowChatClient] = {}

    def client_for(
        selected_provider: str,
        selected_credentials: WorkflowChatCredentials,
        selected_model: str,
    ) -> WorkflowChatClient:
        key = (selected_provider, selected_model)
        if key not in clients:
            clients[key] = _build_chat_client(
                selected_credentials,
                model=selected_model,
                model_cache_dir=project_root / ".powdrr" / "models",
            )
        return clients[key]

    def client_for_model(
        selected_model: str, selected_provider: str
    ) -> WorkflowChatClient:
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
            messages=_build_selection_messages(catalog, transcript),
            parser=lambda payload: _parse_selection_response(payload, catalog),
            context="skill selection",
            model=current_model,
            repair_instructions=_selection_repair_prompt(catalog),
            config=config,
            input_func=input_func,
            stdout=stdout,
            stderr=stderr,
            provider=provider,
            model_mappings=tuple(ZAI_LLM_MAPPINGS.items())
            + tuple((key, value) for key, value in config.llm_mappings),
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
                mappings=config.llm_mappings,
                provider=provider,
            )
            if provider in {"zai", "deepinfra", "local"}
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

    progress = _WorkflowProgressDisplay(stderr, on_update=progress_callback)
    execution_state = _WorkflowExecutionState(
        selected_skill=selected_skill,
        transcript=transcript,
        execution_events=[],
        execution_context=[],
        step_index=0,
        worktree_root=worktree_root,
    )
    action_handlers = _workflow_action_handlers()
    step_roundtrips = 0
    stalled_roundtrips = 0
    previous_action_signature: str | None = None
    failed_action_signature: str | None = None
    while execution_state.step_index < len(selected_skill.skill.steps):
        current_step_index = execution_state.step_index
        current_step = selected_skill.skill.steps[execution_state.step_index]
        step_mapping = (
            _resolve_llm_mapping(
                current_step.llm_type or selection.llm_type,
                mappings=config.llm_mappings,
                provider=provider,
            )
            if provider in {"zai", "deepinfra", "local"}
            else None
        )
        if step_mapping is None:
            if provider in {"zai", "deepinfra", "local"} and (
                current_step.llm_type is not None or selection.llm_type is not None
            ):
                raise RuntimeError(
                    "The workflow step llm_type did not resolve to an LLM mapping."
                )
            step_mapping = LLMModelMapping(current_model, provider=provider)
        assert step_mapping is not None
        current_model = step_mapping.model
        provider = _resolve_provider(
            config.provider,
            current_model,
            mapping=step_mapping,
        )
        credentials = _resolve_credentials(provider, config.api_key, config.base_url)
        step_roundtrips += 1
        before_file_contents = _current_file_contents(execution_state)
        before_last_user_message = _last_user_message(execution_state)
        _verbose_print(
            stderr,
            config.verbose,
            (
                f"Starting execution roundtrip {step_roundtrips} for "
                f"step {execution_state.step_index + 1}/"
                f"{len(selected_skill.skill.steps)}"
            ),
        )
        progress.update(
            selected_skill,
            current_step_index=execution_state.step_index,
            status=f"waiting for {current_model} LLM response...",
        )
        action, current_model, provider = _complete_json_with_model_fallback(
            client_for=client_for_model,
            messages=_build_step_execution_messages(
                selected_skill=selected_skill,
                current_step=current_step,
                current_step_index=execution_state.step_index,
                transcript=execution_state.transcript,
                execution_events=execution_state.execution_events,
                execution_context=execution_state.execution_context,
                current_file_path=execution_state.current_file_path,
                worktree_root=worktree_root,
            ),
            parser=_parse_action_response,
            context=(
                f"workflow execution for step {execution_state.step_index + 1}/"
                f"{len(selected_skill.skill.steps)}"
            ),
            model=current_model,
            repair_instructions=_action_repair_prompt(selected_skill),
            config=config,
            input_func=input_func,
            stdout=stdout,
            stderr=stderr,
            provider=provider,
            model_mappings=tuple(ZAI_LLM_MAPPINGS.items())
            + tuple((key, value) for key, value in config.llm_mappings),
        )
        if action is None:
            return 1
        if action.llm_type is not None:
            action_mapping = (
                _resolve_llm_mapping(
                    action.llm_type,
                    mappings=config.llm_mappings,
                    provider=provider,
                )
                if provider in {"zai", "deepinfra", "local"}
                else None
            )
            assert action_mapping is not None
            current_model = action_mapping.model
            provider = action_mapping.provider
        _verbose_print(
            stderr,
            config.verbose,
            f"Execution result: kind={action.kind}",
        )
        _verbose_print(stderr, config.verbose, f"Execution action: {action.kind}")
        progress.update(
            selected_skill,
            current_step_index=execution_state.step_index,
            status="performing local action...",
        )

        handler = action_handlers.get(action.kind)
        if handler is None:
            raise RuntimeError(f"Unsupported workflow action kind: {action.kind!r}")
        try:
            should_continue = handler(
                action,
                execution_state,
                stdout,
                stderr,
                input_func,
                config,
            )
        except RuntimeError as exc:
            action_signature = _workflow_action_signature(action)
            current_file_context = _current_file_context(
                worktree_root,
                execution_state.current_file_path,
            )
            line_count_feedback = ""
            if current_file_context and current_file_context.get("exists"):
                line_count_feedback = (
                    " The current file has "
                    f"{current_file_context['line_count']} lines; every edit "
                    "range must stay within that line count."
                )
            feedback = (
                f"Workflow {action.kind} action failed: {exc}. "
                "Re-read the current file context and return a corrected action."
                f"{line_count_feedback}"
            )
            print(feedback, file=stderr)
            execution_state.transcript.append(
                {
                    "role": "assistant",
                    "content": _workflow_action_signature(action),
                }
            )
            execution_state.transcript.append(
                {
                    "role": "user",
                    "content": feedback,
                }
            )
            execution_state.execution_context.append(feedback)
            execution_state.execution_events.append(
                {
                    "kind": "action_error",
                    "action_kind": action.kind,
                    "error": str(exc),
                    "step_index": execution_state.step_index,
                }
            )
            if action_signature == failed_action_signature:
                stalled_roundtrips += 1
            else:
                stalled_roundtrips = 1
                failed_action_signature = action_signature
            if stalled_roundtrips >= max(1, config.max_stalled_roundtrips):
                print(
                    "Workflow stopped after repeated action failures.",
                    file=stderr,
                )
                return 1
            previous_action_signature = None
            continue
        action_signature = _workflow_action_signature(action)
        made_progress = _workflow_action_made_progress(
            action,
            previous_action_signature=previous_action_signature,
            before_file_contents=before_file_contents,
            before_last_user_message=before_last_user_message,
            state=execution_state,
        )
        previous_action_signature = action_signature
        if made_progress:
            stalled_roundtrips = 0
            failed_action_signature = None
        else:
            stalled_roundtrips += 1
            _verbose_print(
                stderr,
                config.verbose,
                (
                    f"No progress detected for workflow step "
                    f"({stalled_roundtrips}/{config.max_stalled_roundtrips})"
                ),
            )
            if stalled_roundtrips >= max(1, config.max_stalled_roundtrips):
                print(
                    "Workflow stopped after repeated roundtrips without progress.",
                    file=stderr,
                )
                return 1
        if execution_state.step_index != current_step_index:
            step_roundtrips = 0
            stalled_roundtrips = 0
            previous_action_signature = None
        if not should_continue:
            progress.update(
                selected_skill,
                current_step_index=len(selected_skill.skill.steps),
                status="workflow complete",
            )
            break

    summary = _build_skill_execution_summary(
        selected_skill,
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
) -> list[dict[str, str]]:
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
    ready_to_execute = bool(payload.get("ready_to_execute"))
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


def _normalize_skill_path_value(value: str) -> str:
    return value.strip().rstrip(".").rstrip()


def _path_without_suffix(value: str) -> str:
    return str(Path(value.rstrip(".")).with_suffix(""))


def _verbose_print(stderr: TextIO, verbose: bool, message: str) -> None:
    if verbose:
        print(f"[verbose] {message}", file=stderr)


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


def _catalog_entry_to_data(entry: SkillCatalogEntry) -> dict[str, Any]:
    return {
        "file": str(entry.path),
        "name": entry.skill.name,
        "when_to_use": list(entry.skill.when_to_use),
        "steps": [_skill_step_to_data(step) for step in entry.skill.steps],
    }


def _selection_system_prompt() -> str:
    return (
        "You are an interactive skill router.\n"
        "Choose the best skill for the user's request.\n"
        "If the request is not fully specified, ask exactly one concise "
        "follow-up question.\n"
        "A user question must be a properly formed English question: it must "
        "contain meaningful words, cannot be empty or only whitespace, and "
        "must end with a question mark. Never return whitespace or an "
        "instruction as next_question.\n"
        "Return JSON with keys: selected_skill_path, selected_skill_reason, "
        "next_question, ready_to_execute, llm_type.\n"
        "llm_type describes the capability needed for the next roundtrip; use "
        "high_reasoning, standard_reasoning, simple_task, fast_iteration, "
        "long_context, or vision.\n"
        "selected_skill_path must match one of the catalog entries.\n"
        "Use the skill when_to_use and step descriptions to decide.\n"
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
) -> list[dict[str, str]]:
    current_file_context = _current_file_context(worktree_root, current_file_path)
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
                            "name": "shell",
                            "description": (
                                "Execute a shell command in the current worktree."
                            ),
                        }
                    ],
                    "available_context_types": [
                        {
                            "name": context_type,
                            "when_to_use": description,
                        }
                        for context_type, description in _context_type_catalog()
                    ],
                    "worktree_root": str(worktree_root),
                    "selected_skill": _catalog_entry_to_data(selected_skill),
                    "transcript": list(transcript),
                    "execution_events": list(execution_events),
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
        "You are executing a checked-in skill in a terminal workflow.\n"
        "Use the current step, prior step context, transcript, and prior "
        "execution events to determine the next action.\n"
        "Return exactly one JSON object with one of these forms:\n"
        '{"kind":"gather-context","types":["requirements"],"keywords":["photo"],'
        '"decisions_and_context":"...","llm_type":"simple_task"}\n'
        '{"kind":"prompt_user","text":"...","decisions_and_context":"...",'
        '"llm_type":"standard_reasoning"}\n'
        '{"kind":"edit","file_path":"docs/specs/example/system-specification.yaml",'
        '"edits":[{"kind":"replace","start_line":1,"end_line":2,'
        '"text":"..."}],"decisions_and_context":"...",'
        '"llm_type":"standard_reasoning"}\n'
        "For edits across multiple files, use one edit action with "
        '"file_edits":[{"file_path":"...","edits":[...]}].\n'
        '{"kind":"invoke_tool","tool":"shell","parameters":{"command":["..."],"cwd":"...","env":{...}},"decisions_and_context":"...",'
        '"llm_type":"simple_task"}\n'
        '{"kind":"next_step","decisions_and_context":"...",'
        '"llm_type":"standard_reasoning"}\n'
        '{"kind":"complete","text":"...","decisions_and_context":"...",'
        '"llm_type":"high_reasoning"}\n'
        "Use gather-context when you need to discover information already "
        "specified in checked-in specs before deciding the next action.\n"
        "Use gather-context to discover what requirements are already "
        "specified, find related entities, inspect approach notes, or gather "
        "current features, decisions, risks, or proposed PRs.\n"
        "The supported context types are:\n"
        f"{context_type_lines}\n"
        "Use keywords to narrow results to items that mention one or more "
        "words.\n"
        "Use prompt_user only when you need more information to continue "
        "executing the current step.\n"
        "Do not ask for information already present in the transcript or "
        "execution context. Every prompt_user action must include a concise, "
        "properly formed English question in text. The question must contain "
        "meaningful words, cannot be empty or only whitespace, and must end "
        "with a question mark; never return an instruction or placeholder.\n"
        "Use edit when you know the current file should be changed and you "
        "have enough context to describe line-based removals, additions, or "
        "replacements.\n"
        "When edit is available, current_file includes the file path and its "
        "current contents as context.\n"
        "Use invoke_tool for shell commands.\n"
        "When the current step includes tool_invocations, choose one of those "
        "structured invocations and fill in its parameters.\n"
        "Use next_step when the current step is complete and the next step "
        "should receive the accumulated context.\n"
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
        "next_step": _handle_workflow_action_next_step,
        "prompt_user": _handle_workflow_action_prompt_user,
        "invoke_tool": _handle_workflow_action_invoke_tool,
        "gather-context": _handle_workflow_action_gather_context,
    }


def _current_file_contents(state: _WorkflowExecutionState) -> str | None:
    if state.current_file_path is None or not state.current_file_path.exists():
        return None
    return state.current_file_path.read_text(encoding="utf-8")


def _last_user_message(state: _WorkflowExecutionState) -> str | None:
    if not state.transcript or state.transcript[-1]["role"] != "user":
        return None
    return state.transcript[-1]["content"]


def _workflow_action_signature(action: SkillChatAction) -> str:
    return json.dumps(
        {
            "kind": action.kind,
            "tool": action.tool,
            "file_path": action.file_path,
            "text": action.text,
            "parameters": action.parameters,
            "edits": [_edit_to_data(edit) for edit in action.edits],
            "file_edits": [_file_edits_to_data(group) for group in action.file_edits],
            "types": action.types,
            "keywords": action.keywords,
            "decisions_and_context": action.decisions_and_context,
            "llm_type": action.llm_type,
        },
        sort_keys=True,
        ensure_ascii=False,
    )


def _workflow_action_made_progress(
    action: SkillChatAction,
    *,
    previous_action_signature: str | None,
    before_file_contents: str | None,
    before_last_user_message: str | None,
    state: _WorkflowExecutionState,
) -> bool:
    if action.kind in {"complete", "next_step"}:
        return True
    if previous_action_signature is None:
        return True
    if _workflow_action_signature(action) != previous_action_signature:
        return True
    if action.kind == "edit":
        return _current_file_contents(state) != before_file_contents
    if action.kind == "prompt_user":
        return _last_user_message(state) != before_last_user_message
    return False


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
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text(updated_text, encoding="utf-8")
        results.append(
            {
                "file_path": str(target_path),
                "line_count": len(updated_text.splitlines()),
            }
        )

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
    if action.tool != "shell":
        raise RuntimeError(
            f"Unsupported workflow tool {action.tool!r}; only shell is supported."
        )
    tool_result = _execute_shell_tool(
        action.parameters,
        worktree_root=state.worktree_root,
        stdout=stdout,
        stderr=stderr,
        verbose=config.verbose,
    )
    inferred_path = _resolve_generated_file_path_from_command(
        action.parameters.get("command"),
        worktree_root=state.worktree_root,
    )
    if inferred_path is not None:
        state.current_file_path = inferred_path
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
        "gather-context": _parse_workflow_action_gather_context,
        "invoke_tool": _parse_workflow_action_invoke_tool,
        "next_step": _parse_workflow_action_next_step,
        "prompt_user": _parse_workflow_action_prompt_user,
    }


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
    normalized_types = tuple(
        normalize_context_type(context_type) for context_type in types
    )
    return SkillChatAction(
        kind="gather-context",
        types=normalized_types,
        keywords=keywords,
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
    command = parameters.get("command")
    if isinstance(command, str):
        normalized_parameters = dict(parameters)
        normalized_command = command.strip()
        if not normalized_command:
            raise RuntimeError("Workflow invoke_tool action command must be non-empty.")
        normalized_parameters["command"] = normalized_command
        return SkillChatAction(
            kind="invoke_tool",
            tool=tool.strip(),
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
            tool=tool.strip(),
            parameters=normalized_parameters,
            decisions_and_context=decisions_and_context,
            llm_type=llm_type,
        )
    raise RuntimeError("Workflow invoke_tool action command must be a string or array.")


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
        resolved_cwd = cwd_path if cwd_path.is_absolute() else worktree_root / cwd_path
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
            "Workflow gather-context action "
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
            f"Workflow gather-context action {field_name} must be an array."
        )

    normalized_values = tuple(
        _required_action_string_item(item, field_name=field_name) for item in value
    )
    if not normalized_values:
        raise RuntimeError(
            f"Workflow gather-context action {field_name} must not be empty."
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
            f"Workflow gather-context action {field_name} must be an array."
        )
    return tuple(
        _required_action_string_item(item, field_name=field_name) for item in value
    )


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
    if llm_type is None or provider not in {"zai", "deepinfra", "local"}:
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
    if provider not in {"zai", "deepinfra", "local"}:
        raise RuntimeError(f"LLM mappings are not supported for provider {provider!r}.")
    normalized_llm_type = llm_type.strip().lower().replace("-", "_")
    mapping = dict(
        ZAI_LLM_MAPPINGS if provider in {"zai", "local"} else DEEPINFRA_LLM_MAPPINGS
    )
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
    client_for: Callable[[str, str], WorkflowChatClient],
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
) -> tuple[Any | None, str, str]:
    active_model = model
    active_provider = provider
    attempted_models = {model.casefold()}
    while True:
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


def _complete_json_with_repair(
    client: WorkflowChatClient,
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
) -> Any | None:
    empty_question_reprompts = 0
    while True:
        _verbose_json(
            stderr,
            config.verbose,
            f"{context} LLM input (model={model})",
            messages,
        )
        _print_waiting_for_model(stderr, model)
        try:
            payload = client.complete_json(messages)
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
                        payload = client.complete_json(messages)
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
                repaired_payload = _attempt_json_repair(
                    client,
                    messages,
                    context=context,
                    model=model,
                    error_message=str(exc),
                    repair_instructions=repair_instructions,
                    stderr=stderr,
                    verbose=config.verbose,
                    retry_attempts=config.provider_retry_attempts,
                    retry_delay_seconds=config.provider_retry_delay_seconds,
                )
                if repaired_payload is not None:
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
            repaired_payload = _attempt_json_repair(
                client,
                messages,
                context=context,
                model=model,
                error_message=str(exc),
                repair_instructions=repair_instructions,
                previous_payload=payload,
                stderr=stderr,
                verbose=config.verbose,
                retry_attempts=config.provider_retry_attempts,
                retry_delay_seconds=config.provider_retry_delay_seconds,
            )
            if repaired_payload is not None:
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


def _parse_json_object(content: str, context: str) -> dict[str, Any]:
    try:
        parsed_content = json.loads(content)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{context} was not valid JSON: {exc.msg}") from exc
    if not isinstance(parsed_content, dict):
        raise RuntimeError(f"{context} must be a JSON object.")
    return cast("dict[str, Any]", parsed_content)


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


def _file_edits_to_data(file_edits: SkillChatFileEdits) -> dict[str, Any]:
    return {
        "file_path": file_edits.file_path,
        "edits": [_edit_to_data(edit) for edit in file_edits.edits],
    }


def _current_file_context(
    worktree_root: Path,
    current_file_path: Path | None,
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

    lines = resolved_path.read_text(encoding="utf-8").splitlines()
    return {
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


def _apply_file_edits(current_text: str, edits: Sequence[SkillChatEdit]) -> str:
    lines = current_text.splitlines()
    for edit in sorted(edits, key=_edit_sort_key, reverse=True):
        start_index = edit.start_line - 1
        if edit.kind == "add":
            if start_index > len(lines):
                raise RuntimeError(
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
            raise RuntimeError(
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
    client: WorkflowChatClient,
    messages: Sequence[dict[str, str]],
    *,
    context: str,
    model: str,
    error_message: str,
    repair_instructions: str,
    stderr: TextIO,
    verbose: bool,
    previous_payload: dict[str, Any] | None = None,
    retry_attempts: int = 0,
    retry_delay_seconds: float = 0.0,
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
    attempts = max(1, retry_attempts + 1)
    for attempt in range(1, attempts + 1):
        try:
            _print_waiting_for_model(stderr, model)
            repaired_payload = client.complete_json(repair_messages)
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
            if attempt == attempts:
                print(f"{context} repair request failed: {exc}", file=stderr)
                return None
            delay_seconds = max(0.0, retry_delay_seconds)
            print(
                f"{context} repair request failed for model {model!r}: {exc}. "
                f"Waiting {delay_seconds:g} seconds before automatic repair "
                f"retry {attempt}/{attempts - 1}.",
                file=stderr,
            )
            time.sleep(delay_seconds)
    return None


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
        "Fix the response so it matches the selection schema with keys "
        "selected_skill_path, selected_skill_reason, next_question, and "
        f"ready_to_execute. The selected_skill_path must be one of: {catalog_entries}. "
        "If next_question is present, it must be a concise, properly formed "
        "English question with meaningful words and a trailing question mark; "
        "it cannot be empty or only whitespace."
    )


def _action_repair_prompt(selected_skill: SkillCatalogEntry) -> str:
    step_kinds = ", ".join([step.description for step in selected_skill.skill.steps])
    return (
        "Fix the response so it matches the workflow action schema with keys "
        "kind, tool, file_path, text, parameters, edits, file_edits, types, "
        "keywords, and "
        "decisions_and_context, and llm_type. "
        "Allowed kinds are gather-context, prompt_user, edit, invoke_tool, "
        "next_step, and complete. "
        f"The skill steps are: {step_kinds}. For prompt_user, text must be a "
        "concise, properly formed English question with meaningful words and "
        "a trailing question mark; it cannot be empty or only whitespace."
    )


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
        print("[workflow] thinking...", file=status_stream, flush=True)
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
) -> WorkflowChatClient:
    if credentials.provider == "local":
        resolved_model_path = _resolve_local_model_path(model_cache_dir)
        return LocalLlamaChatClient(
            model_path=resolved_model_path,
            n_ctx=_resolve_local_model_context(),
        )
    limits = _model_limits_for(credentials.provider, model)
    if credentials.provider == "anthropic":
        return AnthropicChatClient(
            model=model,
            api_key=credentials.api_key,
            base_url=credentials.base_url,
            limits=limits,
        )
    if credentials.provider == "zai":
        return OpenAIChatClient(
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
    )


def _model_limits_for(provider: str, model: str) -> LLMModelLimits:
    if provider == "zai":
        limits = ZAI_MODEL_LIMITS
    elif provider == "deepinfra":
        limits = DEEPINFRA_MODEL_LIMITS
    else:
        return _DEFAULT_MODEL_LIMITS
    return limits.get(model.casefold(), _DEFAULT_MODEL_LIMITS)


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
        return mapping.provider
    if provider_override == "local":
        provider_override = "auto"
    if provider_override != "auto":
        return provider_override
    if model.startswith("claude-"):
        return "anthropic"
    if model.startswith("glm-"):
        return "zai"
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("CLAUDE_API_KEY"):
        if not (
            os.environ.get("OPENAI_API_KEY")
            or os.environ.get("CODEX_API_KEY")
            or _resolve_codex_access_token() is not None
        ):
            return "anthropic"
    if os.environ.get("ZAI_API_KEY") or os.environ.get("ZAI_BASE_URL"):
        if not (
            os.environ.get("OPENAI_API_KEY")
            or os.environ.get("CODEX_API_KEY")
            or os.environ.get("ANTHROPIC_API_KEY")
            or os.environ.get("CLAUDE_API_KEY")
            or _resolve_codex_access_token() is not None
        ):
            return "zai"
    if (
        os.environ.get("DEEPINFRA_API_TOKEN")
        or os.environ.get("DEEPINFRA_API_KEY")
        or os.environ.get("DEEPINFRA_BASE_URL")
    ):
        if not (
            os.environ.get("OPENAI_API_KEY")
            or os.environ.get("CODEX_API_KEY")
            or os.environ.get("ANTHROPIC_API_KEY")
            or os.environ.get("CLAUDE_API_KEY")
            or os.environ.get("ZAI_API_KEY")
            or os.environ.get("ZAI_BASE_URL")
            or _resolve_codex_access_token() is not None
        ):
            return "deepinfra"
    return "openai"


def _resolve_api_key(provider: str, override: str | None) -> tuple[str, str]:
    if override:
        return override, "--api-key"
    if provider == "local":
        return "local", "local"
    if provider == "anthropic":
        for env_name in ("ANTHROPIC_API_KEY", "CLAUDE_API_KEY"):
            value = os.environ.get(env_name)
            if value:
                return value, env_name
        raise RuntimeError(
            "No Anthropic credentials found. Set ANTHROPIC_API_KEY or "
            "CLAUDE_API_KEY, or pass --api-key."
        )
    if provider == "zai":
        for env_name in ("ZAI_API_KEY", "GLM_API_KEY"):
            value = os.environ.get(env_name)
            if value:
                return value, env_name
        raise RuntimeError(
            "No z.ai credentials found. Set ZAI_API_KEY or GLM_API_KEY, or "
            "pass --api-key."
        )
    if provider == "deepinfra":
        for env_name in ("DEEPINFRA_API_TOKEN", "DEEPINFRA_API_KEY"):
            value = os.environ.get(env_name)
            if value:
                return value, env_name
        raise RuntimeError(
            "No DeepInfra credentials found. Set DEEPINFRA_API_TOKEN or "
            "DEEPINFRA_API_KEY, or pass --api-key."
        )
    for env_name in ("OPENAI_API_KEY", "CODEX_API_KEY"):
        value = os.environ.get(env_name)
        if value:
            return value, env_name
    codex_token = _resolve_codex_access_token()
    if codex_token is not None:
        return codex_token, _codex_auth_path_description()
    raise RuntimeError(
        "No OpenAI credentials found. Set OPENAI_API_KEY, CODEX_API_KEY, or "
        "sign in with Codex so ~/.codex/auth.json is available."
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
    if provider == "local":
        return "local", "local"
    if provider == "anthropic":
        for env_name in ("ANTHROPIC_BASE_URL",):
            value = os.environ.get(env_name)
            if value:
                return value, env_name
        return "https://api.anthropic.com", "default"
    if provider == "zai":
        for env_name in ("ZAI_BASE_URL",):
            value = os.environ.get(env_name)
            if value:
                return value, env_name
        return "https://api.z.ai/api/paas/v4/", "default"
    if provider == "deepinfra":
        value = os.environ.get("DEEPINFRA_BASE_URL")
        if value:
            return value, "DEEPINFRA_BASE_URL"
        return "https://api.deepinfra.com/v1/openai", "default"
    for env_name in ("OPENAI_BASE_URL", "CODEX_BASE_URL"):
        value = os.environ.get(env_name)
        if value:
            return value, env_name
    return "https://api.openai.com/v1", "default"


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
