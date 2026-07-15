from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
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

_DEFAULT_MODEL = "gpt-4.1-mini"


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

        loaded_response = json.loads(raw_response)
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

        parsed_content = json.loads(content)
        if not isinstance(parsed_content, dict):
            raise RuntimeError("OpenAI response content must be a JSON object.")
        return cast("dict[str, Any]", parsed_content)


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

        loaded_response = json.loads(raw_response)
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

        parsed_content = json.loads(response_text)
        if not isinstance(parsed_content, dict):
            raise RuntimeError("Anthropic response content must be a JSON object.")
        return cast("dict[str, Any]", parsed_content)


class WorkflowChatClient(Protocol):
    def complete_json(self, messages: list[dict[str, str]]) -> dict[str, Any]: ...


def run_workflow_chat(
    config: WorkflowChatConfig,
    *,
    input_func: Callable[[], str] = input,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
) -> int:
    repo_root = _resolve_worktree_context(
        config.repo_root,
        stderr=stderr,
        verbose=config.verbose,
    )
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
        selection_payload = client.complete_json(
            _build_selection_messages(catalog, transcript)
        )
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

    summary = _build_skill_execution_summary(selected_skill, selection, transcript)
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
        "steps": [
            {
                "description": step.description,
                "details": step.details,
                "uses_skills": list(step.uses_skills),
            }
            for step in entry.skill.steps
        ],
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
) -> dict[str, Any]:
    return {
        "selected_skill_file": str(selected_skill.path),
        "selected_skill_name": selected_skill.skill.name,
        "selected_skill_reason": selection.selected_skill_reason,
        "conversation": list(transcript),
        "skill": selected_skill.skill.to_data(),
    }


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
