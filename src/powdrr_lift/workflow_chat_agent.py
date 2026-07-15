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
    build_skill_directory_validation_report,
    load_skills,
    resolve_repo_root,
)

_DEFAULT_MODEL = "glm-5.2"


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
    text: str | None = None
    parameters: dict[str, Any] = field(default_factory=dict)
    decisions_and_context: str | None = None


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
        selection_payload = _complete_json_with_retry(
            client,
            _build_selection_messages(catalog, transcript),
            context="skill selection",
            config=config,
            input_func=input_func,
            stdout=stdout,
            stderr=stderr,
        )
        if selection_payload is None:
            return 1
        _verbose_print(
            stderr,
            config.verbose,
            "Selection payload: "
            f"{json.dumps(selection_payload, indent=2, ensure_ascii=False)}",
        )
        selection = _parse_selection_response(selection_payload, catalog)
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

    execution_events: list[dict[str, Any]] = []
    execution_context: list[str] = []
    step_index = 0
    for turn in range(config.max_turns):
        if step_index >= len(selected_skill.skill.steps):
            break
        current_step = selected_skill.skill.steps[step_index]
        _verbose_print(
            stderr,
            config.verbose,
            (
                f"Starting execution turn {turn + 1} "
                f"for step {step_index + 1}/{len(selected_skill.skill.steps)}"
            ),
        )
        action_payload = _complete_json_with_retry(
            client,
            _build_step_execution_messages(
                selected_skill=selected_skill,
                current_step=current_step,
                current_step_index=step_index,
                transcript=transcript,
                execution_events=execution_events,
                execution_context=execution_context,
                worktree_root=worktree_root,
            ),
            context=(
                f"workflow execution for step {step_index + 1}/"
                f"{len(selected_skill.skill.steps)}"
            ),
            config=config,
            input_func=input_func,
            stdout=stdout,
            stderr=stderr,
        )
        if action_payload is None:
            return 1
        _verbose_print(
            stderr,
            config.verbose,
            "Execution payload: "
            f"{json.dumps(action_payload, indent=2, ensure_ascii=False)}",
        )
        action = _parse_action_response(action_payload)
        _verbose_print(stderr, config.verbose, f"Execution action: {action.kind}")

        if action.decisions_and_context:
            execution_context.append(action.decisions_and_context)

        if action.kind == "complete":
            if action.text:
                print(action.text, file=stdout)
            execution_events.append(
                {
                    "kind": action.kind,
                    "text": action.text,
                    "decisions_and_context": action.decisions_and_context,
                }
            )
            break

        if action.kind == "next_step":
            execution_events.append(
                {
                    "kind": action.kind,
                    "decisions_and_context": action.decisions_and_context,
                    "step_index": step_index,
                }
            )
            step_index += 1
            continue

        if action.kind == "prompt_user":
            print(action.text or "", file=stdout)
            answer = _prompt_user("> ", input_func=input_func, stdout=stdout)
            _verbose_print(stderr, config.verbose, f"Follow-up answer: {answer}")
            transcript.append(
                {
                    "role": "assistant",
                    "content": action.text or "",
                }
            )
            transcript.append({"role": "user", "content": answer})
            execution_events.append(
                {
                    "kind": action.kind,
                    "text": action.text,
                    "answer": answer,
                    "decisions_and_context": action.decisions_and_context,
                    "step_index": step_index,
                }
            )
            continue

        if action.kind == "invoke_tool":
            if action.tool != "shell":
                raise RuntimeError(
                    "Unsupported workflow tool "
                    f"{action.tool!r}; only shell is supported."
                )
            tool_result = _execute_shell_tool(
                action.parameters,
                worktree_root=worktree_root,
                stdout=stdout,
                stderr=stderr,
                verbose=config.verbose,
            )
            transcript.append(
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
            transcript.append(
                {
                    "role": "user",
                    "content": json.dumps(
                        {"tool_result": tool_result},
                        ensure_ascii=False,
                    ),
                }
            )
            execution_events.append(
                {
                    "kind": action.kind,
                    "parameters": action.parameters,
                    "result": tool_result,
                    "decisions_and_context": action.decisions_and_context,
                    "step_index": step_index,
                }
            )
            continue

        raise RuntimeError(f"Unsupported workflow action kind: {action.kind!r}")
    else:
        print(
            "Reached the maximum number of workflow turns without completion.",
            file=stderr,
        )
        return 1

    summary = _build_skill_execution_summary(
        selected_skill,
        selection,
        transcript,
        execution_events,
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
    worktree_root: Path,
) -> list[dict[str, str]]:
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
                    "worktree_root": str(worktree_root),
                    "selected_skill": _catalog_entry_to_data(selected_skill),
                    "transcript": list(transcript),
                    "execution_events": list(execution_events),
                },
                indent=2,
                ensure_ascii=False,
            ),
        },
    ]


def _action_system_prompt() -> str:
    return (
        "You are executing a checked-in skill in a terminal workflow.\n"
        "Use the current step, prior step context, transcript, and prior "
        "execution events to determine the next action.\n"
        "Return exactly one JSON object with one of these forms:\n"
        '{"kind":"prompt_user","text":"...","decisions_and_context":"..."}\n'
        '{"kind":"invoke_tool","tool":"shell","parameters":{"command":["..."],"cwd":"...","env":{...}},"decisions_and_context":"..."}\n'
        '{"kind":"next_step","decisions_and_context":"..."}\n'
        '{"kind":"complete","text":"...","decisions_and_context":"..."}\n'
        "Use prompt_user only when you need more information to continue "
        "executing the current step.\n"
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


def _parse_action_response(payload: dict[str, Any]) -> SkillChatAction:
    kind = payload.get("kind")
    if not isinstance(kind, str) or not kind:
        raise RuntimeError("Workflow action response must include kind.")
    decisions_and_context = _optional_string(payload.get("decisions_and_context"))

    if kind == "prompt_user":
        text = payload.get("text")
        if not isinstance(text, str) or not text.strip():
            raise RuntimeError(
                "Workflow prompt_user action must include non-empty text."
            )
        return SkillChatAction(
            kind=kind,
            text=text.strip(),
            decisions_and_context=decisions_and_context,
        )

    if kind == "invoke_tool":
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
                raise RuntimeError(
                    "Workflow invoke_tool action command must be non-empty."
                )
            normalized_parameters["command"] = normalized_command
            return SkillChatAction(
                kind=kind,
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
                raise RuntimeError(
                    "Workflow invoke_tool action command must not be empty."
                )
            normalized_parameters = dict(parameters)
            normalized_parameters["command"] = normalized_command_list
            return SkillChatAction(
                kind=kind,
                tool=tool.strip(),
                parameters=normalized_parameters,
                decisions_and_context=decisions_and_context,
            )
        raise RuntimeError(
            "Workflow invoke_tool action command must be a string or array."
        )

    if kind == "next_step":
        return SkillChatAction(kind=kind, decisions_and_context=decisions_and_context)

    if kind == "complete":
        text = payload.get("text")
        if text is not None and not isinstance(text, str):
            raise RuntimeError("Workflow complete action text must be a string.")
        return SkillChatAction(
            kind=kind,
            text=(text.strip() if text else None),
            decisions_and_context=decisions_and_context,
        )

    raise RuntimeError(f"Unknown workflow action kind: {kind!r}")


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


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise RuntimeError("Workflow action decisions_and_context must be a string.")
    normalized_value = value.strip()
    return normalized_value or None


def _complete_json_with_retry(
    client: WorkflowChatClient,
    messages: list[dict[str, str]],
    *,
    context: str,
    config: WorkflowChatConfig,
    input_func: Callable[[], str],
    stdout: TextIO,
    stderr: TextIO,
) -> dict[str, Any] | None:
    while True:
        try:
            return client.complete_json(messages)
        except RuntimeError as exc:
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


def _parse_json_object(content: str, context: str) -> dict[str, Any]:
    try:
        parsed_content = json.loads(content)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{context} was not valid JSON: {exc.msg}") from exc
    if not isinstance(parsed_content, dict):
        raise RuntimeError(f"{context} must be a JSON object.")
    return cast("dict[str, Any]", parsed_content)


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
