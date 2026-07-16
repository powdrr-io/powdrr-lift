from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, TextIO, cast
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

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

WorkflowActionParser = Callable[[dict[str, Any], str | None], "SkillChatAction"]


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
    api_key: str | None = None
    base_url: str | None = None
    max_turns: int = 8
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
    types: tuple[str, ...] = field(default_factory=tuple)
    keywords: tuple[str, ...] = field(default_factory=tuple)
    decisions_and_context: str | None = None


@dataclass(frozen=True, slots=True)
class SkillChatEdit:
    kind: str
    start_line: int
    end_line: int | None = None
    text: str | None = None


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
    ) -> None:
        self._model = model
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    def complete_json(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        payload = {
            "model": self._model,
            "messages": messages,
            "temperature": 0,
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


class AnthropicChatClient:
    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        base_url: str,
        timeout: float = 120.0,
        api_version: str = "2023-06-01",
    ) -> None:
        self._model = model
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._api_version = api_version

    def complete_json(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        system_prompt, conversation_messages = _split_system_message(messages)
        payload: dict[str, Any] = {
            "model": self._model,
            "max_tokens": 4096,
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


class WorkflowChatClient(Protocol):
    def complete_json(self, messages: list[dict[str, str]]) -> dict[str, Any]: ...


def run_workflow_chat(
    config: WorkflowChatConfig,
    *,
    input_func: Callable[[], str] = input,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
) -> int:
    worktree_root = _resolve_worktree_context(
        config.repo_root,
        stderr=stderr,
        verbose=config.verbose,
    )
    repo_root = worktree_root
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

    provider = _resolve_provider(config.provider, config.model)
    credentials = _resolve_credentials(provider, config.api_key, config.base_url)
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
    client = _build_chat_client(
        credentials,
        model=config.model,
    )

    user_request = _prompt_user(
        "What do you want to do? ",
        input_func=input_func,
        stdout=stdout,
    )
    transcript: list[dict[str, str]] = [{"role": "user", "content": user_request}]
    _verbose_print(stderr, config.verbose, f"Initial user request: {user_request}")
    selected_skill: SkillCatalogEntry | None = None
    selection: SkillChatSelection | None = None

    for _turn in range(config.max_turns):
        _verbose_print(stderr, config.verbose, f"Starting selection turn {_turn + 1}")
        selection = _complete_json_with_repair(
            client,
            _build_selection_messages(catalog, transcript),
            parser=lambda payload: _parse_selection_response(payload, catalog),
            context="skill selection",
            repair_instructions=_selection_repair_prompt(catalog),
            config=config,
            input_func=input_func,
            stdout=stdout,
            stderr=stderr,
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
        print(f"Matched skill: {selected_skill.path}", file=stdout)
        print(selection.selected_skill_reason, file=stdout)
        if selection.ready_to_execute and selection.next_question is None:
            break

        if selection.next_question is None:
            break

        print(selection.next_question, file=stdout)
        answer = _prompt_user("> ", input_func=input_func, stdout=stdout)
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

    execution_state = _WorkflowExecutionState(
        selected_skill=selected_skill,
        transcript=transcript,
        execution_events=[],
        execution_context=[],
        step_index=0,
        worktree_root=worktree_root,
    )
    action_handlers = _workflow_action_handlers()
    for turn in range(config.max_turns):
        if execution_state.step_index >= len(selected_skill.skill.steps):
            break
        current_step = selected_skill.skill.steps[execution_state.step_index]
        _verbose_print(
            stderr,
            config.verbose,
            (
                f"Starting execution turn {turn + 1} "
                f"for step {execution_state.step_index + 1}/"
                f"{len(selected_skill.skill.steps)}"
            ),
        )
        action = _complete_json_with_repair(
            client,
            _build_step_execution_messages(
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
            repair_instructions=_action_repair_prompt(selected_skill),
            config=config,
            input_func=input_func,
            stdout=stdout,
            stderr=stderr,
        )
        if action is None:
            return 1
        _verbose_print(
            stderr,
            config.verbose,
            f"Execution result: kind={action.kind}",
        )
        _verbose_print(stderr, config.verbose, f"Execution action: {action.kind}")

        handler = action_handlers.get(action.kind)
        if handler is None:
            raise RuntimeError(f"Unsupported workflow action kind: {action.kind!r}")
        if not handler(
            action,
            execution_state,
            stdout,
            stderr,
            input_func,
            config,
        ):
            break
    else:
        print(
            "Reached the maximum number of workflow turns without completion.",
            file=stderr,
        )
        return 1

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
        for skill_path in sorted(resolved_dir.glob("*.json"))
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
    ready_to_execute = bool(payload.get("ready_to_execute"))
    return SkillChatSelection(
        selected_skill_path=selected_skill_path,
        selected_skill_reason=selected_skill_reason,
        next_question=next_question,
        ready_to_execute=ready_to_execute,
    )


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
        "Return JSON with keys: selected_skill_path, selected_skill_reason, "
        "next_question, ready_to_execute.\n"
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
        '"decisions_and_context":"..."}\n'
        '{"kind":"prompt_user","text":"...","decisions_and_context":"..."}\n'
        '{"kind":"edit","file_path":"docs/specs/example/system-specification.yaml",'
        '"edits":[{"kind":"replace","start_line":1,"end_line":2,'
        '"text":"..."}],"decisions_and_context":"..."}\n'
        '{"kind":"invoke_tool","tool":"shell","parameters":{"command":["..."],"cwd":"...","env":{...}},"decisions_and_context":"..."}\n'
        '{"kind":"next_step","decisions_and_context":"..."}\n'
        '{"kind":"complete","text":"...","decisions_and_context":"..."}\n'
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
    if action.file_path is None:
        raise RuntimeError("Workflow edit action must include file_path.")
    target_path = _resolve_worktree_file_path(action.file_path, state.worktree_root)
    current_text = ""
    if target_path.exists():
        current_text = target_path.read_text(encoding="utf-8")
    updated_text = _apply_file_edits(current_text, action.edits)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text(updated_text, encoding="utf-8")
    state.current_file_path = target_path
    if action.decisions_and_context:
        state.execution_context.append(action.decisions_and_context)
    state.transcript.append(
        {
            "role": "assistant",
            "content": json.dumps(
                {
                    "kind": action.kind,
                    "file_path": action.file_path,
                    "edits": [_edit_to_data(edit) for edit in action.edits],
                },
                ensure_ascii=False,
            ),
        }
    )
    state.transcript.append(
        {
            "role": "user",
            "content": json.dumps(
                {
                    "edit_result": {
                        "file_path": str(target_path),
                        "line_count": len(updated_text.splitlines()),
                    }
                },
                ensure_ascii=False,
            ),
        }
    )
    state.execution_events.append(
        {
            "kind": action.kind,
            "file_path": action.file_path,
            "edits": [_edit_to_data(edit) for edit in action.edits],
            "result": {
                "file_path": str(target_path),
                "line_count": len(updated_text.splitlines()),
            },
            "decisions_and_context": action.decisions_and_context,
            "step_index": state.step_index,
        }
    )
    print(f"Edited file: {target_path}", file=stdout)
    _verbose_print(stderr, config.verbose, f"Applied edit to {target_path}")
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
    _ = stderr
    print(action.text or "", file=stdout)
    answer = _prompt_user("> ", input_func=input_func, stdout=stdout)
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
    parser = _workflow_action_parsers().get(normalized_kind)
    if parser is None:
        raise RuntimeError(f"Unknown workflow action kind: {normalized_kind!r}")
    return parser(payload, decisions_and_context)


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
) -> SkillChatAction:
    text = payload.get("text")
    if text is not None and not isinstance(text, str):
        raise RuntimeError("Workflow complete action text must be a string.")
    return SkillChatAction(
        kind="complete",
        text=(text.strip() if text else None),
        decisions_and_context=decisions_and_context,
    )


def _parse_workflow_action_edit(
    payload: dict[str, Any],
    decisions_and_context: str | None,
) -> SkillChatAction:
    file_path = payload.get("file_path")
    if not isinstance(file_path, str) or not file_path.strip():
        raise RuntimeError("Workflow edit action must include file_path.")
    edits = _required_edit_operations(payload.get("edits"))
    return SkillChatAction(
        kind="edit",
        file_path=file_path.strip(),
        edits=edits,
        decisions_and_context=decisions_and_context,
    )


def _parse_workflow_action_gather_context(
    payload: dict[str, Any],
    decisions_and_context: str | None,
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
    )


def _parse_workflow_action_invoke_tool(
    payload: dict[str, Any],
    decisions_and_context: str | None,
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
        )
    raise RuntimeError("Workflow invoke_tool action command must be a string or array.")


def _parse_workflow_action_next_step(
    payload: dict[str, Any],
    decisions_and_context: str | None,
) -> SkillChatAction:
    _ = payload
    return SkillChatAction(
        kind="next_step", decisions_and_context=decisions_and_context
    )


def _parse_workflow_action_prompt_user(
    payload: dict[str, Any],
    decisions_and_context: str | None,
) -> SkillChatAction:
    text = payload.get("text")
    if text is not None and not isinstance(text, str):
        raise RuntimeError("Workflow prompt_user action text must be a string.")
    return SkillChatAction(
        kind="prompt_user",
        text=(text.strip() if text else None),
        decisions_and_context=decisions_and_context,
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
        command_display = command
        run_command: str | list[str] = command
        use_shell = True
    elif isinstance(command, Sequence) and not isinstance(
        command,
        (str, bytes, bytearray),
    ):
        normalized_command = [_required_shell_command_item(item) for item in command]
        command_display = " ".join(shlex.quote(item) for item in normalized_command)
        run_command = normalized_command
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
        if not isinstance(text_value, str) or not text_value.strip():
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


def _complete_json_with_repair(
    client: WorkflowChatClient,
    messages: list[dict[str, str]],
    *,
    context: str,
    parser: Callable[[dict[str, Any]], Any],
    repair_instructions: str,
    config: WorkflowChatConfig,
    input_func: Callable[[], str],
    stdout: TextIO,
    stderr: TextIO,
) -> Any | None:
    while True:
        try:
            payload = client.complete_json(messages)
        except RuntimeError as exc:
            if _is_json_repairable_error(exc):
                _verbose_print(
                    stderr,
                    config.verbose,
                    f"Attempting automatic repair for {context} after provider failure",
                )
                repaired_payload = _attempt_json_repair(
                    client,
                    messages,
                    context=context,
                    error_message=str(exc),
                    repair_instructions=repair_instructions,
                    stderr=stderr,
                    verbose=config.verbose,
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
            else:
                print(f"{context} failed: {exc}", file=stderr)
            retry = _prompt_user(
                "Type 'retry' to try again or 'abort' to stop: ",
                input_func=input_func,
                stdout=stdout,
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
                error_message=str(exc),
                repair_instructions=repair_instructions,
                previous_payload=payload,
                stderr=stderr,
                verbose=config.verbose,
            )
            if repaired_payload is not None:
                try:
                    return parser(repaired_payload)
                except RuntimeError as repair_exc:
                    print(
                        f"{context} repaired response was still invalid: {repair_exc}",
                        file=stderr,
                    )
            retry = _prompt_user(
                "Type 'retry' to try again or 'abort' to stop: ",
                input_func=input_func,
                stdout=stdout,
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
                    "Workflow edit action add start_line is beyond the end of the file."
                )
            insert_lines = edit.text.splitlines() if edit.text is not None else []
            lines[start_index:start_index] = insert_lines
            continue

        end_line = edit.end_line if edit.end_line is not None else edit.start_line
        end_index = end_line
        if end_index > len(lines):
            raise RuntimeError(
                "Workflow edit action range extends beyond the end of the file."
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
    _verbose_print(
        stderr,
        verbose,
        "Repair prompt for "
        f"{context}: {json.dumps(repair_messages, indent=2, ensure_ascii=False)}",
    )
    try:
        return client.complete_json(repair_messages)
    except RuntimeError as exc:
        print(f"{context} repair request failed: {exc}", file=stderr)
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


def _selection_repair_prompt(catalog: Sequence[SkillCatalogEntry]) -> str:
    catalog_entries = ", ".join(str(entry.path) for entry in catalog)
    return (
        "Fix the response so it matches the selection schema with keys "
        "selected_skill_path, selected_skill_reason, next_question, and "
        f"ready_to_execute. The selected_skill_path must be one of: {catalog_entries}."
    )


def _action_repair_prompt(selected_skill: SkillCatalogEntry) -> str:
    step_kinds = ", ".join([step.description for step in selected_skill.skill.steps])
    return (
        "Fix the response so it matches the workflow action schema with keys "
        "kind, tool, file_path, text, parameters, edits, types, keywords, and "
        "decisions_and_context. "
        "Allowed kinds are gather-context, prompt_user, edit, invoke_tool, "
        "next_step, and complete. "
        f"The skill steps are: {step_kinds}."
    )


def _prompt_user(
    prompt: str,
    *,
    input_func: Callable[[], str],
    stdout: TextIO,
) -> str:
    stdout.write(prompt)
    stdout.flush()
    return input_func().strip()


def _build_chat_client(
    credentials: WorkflowChatCredentials,
    *,
    model: str,
) -> OpenAIChatClient | AnthropicChatClient:
    if credentials.provider == "anthropic":
        return AnthropicChatClient(
            model=model,
            api_key=credentials.api_key,
            base_url=credentials.base_url,
        )
    if credentials.provider == "zai":
        return OpenAIChatClient(
            model=model,
            api_key=credentials.api_key,
            base_url=credentials.base_url,
        )
    return OpenAIChatClient(
        model=model,
        api_key=credentials.api_key,
        base_url=credentials.base_url,
    )


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


def _resolve_provider(provider_override: str, model: str) -> str:
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
    return "openai"


def _resolve_api_key(provider: str, override: str | None) -> tuple[str, str]:
    if override:
        return override, "--api-key"
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


def _resolve_base_url(provider: str, override: str | None) -> tuple[str, str]:
    if override:
        return override, "--base-url"
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
