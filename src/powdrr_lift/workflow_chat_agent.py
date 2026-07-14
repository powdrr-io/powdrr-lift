from __future__ import annotations

import json
import os
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
    WorkflowTask,
    WorkflowTaskValidationReport,
    WorkflowTemplate,
    build_workflow_task_directory_validation_report,
    build_workflow_template_validation_report,
    load_workflow_template,
    save_workflow_task,
    validate_workflow_task_directory,
)

_DEFAULT_MODEL = "gpt-4.1-mini"


@dataclass(frozen=True, slots=True)
class WorkflowTemplateCatalogEntry:
    path: Path
    template: WorkflowTemplate


@dataclass(frozen=True, slots=True)
class WorkflowChatConfig:
    templates_dir: Path
    output_dir: Path | None = None
    provider: str = "auto"
    model: str = _DEFAULT_MODEL
    api_key: str | None = None
    base_url: str | None = None
    max_turns: int = 8
    verbose: bool = False


@dataclass(frozen=True, slots=True)
class WorkflowChatResult:
    selected_template_path: Path
    task_paths: tuple[Path, ...]
    validation_report: WorkflowTaskValidationReport


@dataclass(frozen=True, slots=True)
class WorkflowChatSelection:
    selected_template_path: Path
    selected_template_reason: str
    next_question: str | None = None
    ready_to_generate: bool = False


@dataclass(frozen=True, slots=True)
class WorkflowChatTaskBundle:
    tasks: tuple[WorkflowTask, ...]

    def to_data(self) -> dict[str, Any]:
        return {"tasks": [task.to_data() for task in self.tasks]}


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
    catalog = _load_workflow_template_catalog(config.templates_dir, stderr=stderr)
    if not catalog:
        print(
            f"No workflow templates found in {config.templates_dir}.",
            file=stderr,
        )
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
        f"Loaded {len(catalog)} workflow template(s) from {config.templates_dir}",
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
    selected_template: WorkflowTemplateCatalogEntry | None = None
    selection: WorkflowChatSelection | None = None

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
        selected_template = _find_catalog_entry(
            catalog,
            selection.selected_template_path,
        )
        print(
            f"Matched workflow template: {selected_template.path}",
            file=stdout,
        )
        print(selection.selected_template_reason, file=stdout)
        if selection.ready_to_generate and selection.next_question is None:
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
            "Reached the maximum number of workflow chat turns without "
            "generating output.",
            file=stderr,
        )
        return 1

    if selected_template is None or selection is None:
        print("Could not select a workflow template.", file=stderr)
        return 1

    task_bundle = _generate_workflow_tasks(
        client,
        selected_template,
        transcript,
    )
    _verbose_print(
        stderr,
        config.verbose,
        f"Generated {len(task_bundle.tasks)} workflow task(s)",
    )

    output_dir = (
        config.output_dir
        if config.output_dir is not None
        else Path(tempfile.mkdtemp(prefix="powdrr-lift-workflow-chat-"))
    )
    _verbose_print(stderr, config.verbose, f"Writing tasks to {output_dir}")
    task_paths = _write_task_bundle(task_bundle, output_dir)
    validation_report = _validate_task_directory(output_dir)
    _verbose_print(
        stderr,
        config.verbose,
        f"Validation successful: {validation_report.validation_successful}",
    )

    if config.output_dir is None:
        print(
            json.dumps(
                {
                    "selected_template_file": str(selected_template.path),
                    "task_directory": str(output_dir),
                    "task_paths": [str(path) for path in task_paths],
                    "validation_successful": validation_report.validation_successful,
                    "issues": [
                        {
                            "code": issue.code,
                            "message": issue.message,
                            "path": issue.path,
                        }
                        for issue in validation_report.issues
                    ],
                },
                indent=2,
                ensure_ascii=False,
            ),
            file=stdout,
        )
    else:
        print(f"Wrote workflow tasks to {output_dir}", file=stdout)
        print(validation_report.validation_successful, file=stdout)

    if not validation_report.validation_successful:
        print(validate_workflow_task_directory(output_dir), file=stderr, end="")
        return 1

    return 0


def _load_workflow_template_catalog(
    templates_dir: Path,
    *,
    stderr: TextIO,
) -> tuple[WorkflowTemplateCatalogEntry, ...]:
    resolved_dir = templates_dir.expanduser().resolve()
    if not resolved_dir.exists():
        print(
            f"Workflow template directory does not exist: {resolved_dir}",
            file=stderr,
        )
        return ()
    if not resolved_dir.is_dir():
        print(f"Workflow template path is not a directory: {resolved_dir}", file=stderr)
        return ()

    entries: list[WorkflowTemplateCatalogEntry] = []
    for template_path in sorted(resolved_dir.glob("*.json")):
        if not template_path.is_file():
            continue
        raw_template = template_path.read_text(encoding="utf-8")
        report = build_workflow_template_validation_report(
            raw_template,
            source_path=template_path,
        )
        if not report.validation_successful:
            for issue in report.issues:
                print(
                    f"{template_path}: {issue.code}: {issue.message}",
                    file=stderr,
                )
            return ()
        template = load_workflow_template(template_path)
        entries.append(
            WorkflowTemplateCatalogEntry(
                path=template_path,
                template=template,
            )
        )

    return tuple(entries)


def _build_selection_messages(
    catalog: Sequence[WorkflowTemplateCatalogEntry],
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
                    "workflow_templates": [
                        _catalog_entry_to_data(entry) for entry in catalog
                    ],
                    "conversation": list(transcript),
                },
                indent=2,
                ensure_ascii=False,
            ),
        },
    ]


def _build_generation_messages(
    template: WorkflowTemplateCatalogEntry,
    transcript: Sequence[dict[str, str]],
) -> list[dict[str, str]]:
    return [
        {
            "role": "system",
            "content": _generation_system_prompt(),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "workflow_template_file": str(template.path),
                    "workflow_template": template.template.to_data(),
                    "conversation": list(transcript),
                },
                indent=2,
                ensure_ascii=False,
            ),
        },
    ]


def _generate_workflow_tasks(
    client: WorkflowChatClient,
    template: WorkflowTemplateCatalogEntry,
    transcript: Sequence[dict[str, str]],
) -> WorkflowChatTaskBundle:
    payload = client.complete_json(_build_generation_messages(template, transcript))
    raw_tasks = payload.get("tasks")
    if not isinstance(raw_tasks, list):
        raise RuntimeError("Workflow task generation response did not include tasks.")

    tasks = tuple(
        WorkflowTask.from_data(task_data)
        for task_data in raw_tasks
        if isinstance(task_data, dict)
    )
    if not tasks:
        raise RuntimeError("Workflow task generation response did not produce tasks.")
    return WorkflowChatTaskBundle(tasks=tasks)


def _write_task_bundle(
    task_bundle: WorkflowChatTaskBundle,
    output_dir: Path,
) -> tuple[Path, ...]:
    output_dir.mkdir(parents=True, exist_ok=True)
    task_paths: list[Path] = []
    for task in task_bundle.tasks:
        task_path = output_dir / f"{task.task_id}.json"
        save_workflow_task(task, task_path)
        task_paths.append(task_path)
    return tuple(task_paths)


def _validate_task_directory(directory: Path) -> WorkflowTaskValidationReport:
    report = build_workflow_task_directory_validation_report(directory)
    return report


def _parse_selection_response(
    payload: dict[str, Any],
    catalog: Sequence[WorkflowTemplateCatalogEntry],
) -> WorkflowChatSelection:
    selected_template_path_value = payload.get("selected_template_path")
    if (
        not isinstance(selected_template_path_value, str)
        or not selected_template_path_value
    ):
        raise RuntimeError(
            "Workflow selection response must include selected_template_path."
        )
    selected_template_path = _resolve_template_path(
        selected_template_path_value,
        catalog,
    )
    selected_template_reason = payload.get("selected_template_reason")
    if not isinstance(selected_template_reason, str) or not selected_template_reason:
        raise RuntimeError(
            "Workflow selection response must include selected_template_reason."
        )
    next_question = payload.get("next_question")
    if next_question is not None and not isinstance(next_question, str):
        raise RuntimeError(
            "Workflow selection response next_question must be a string."
        )
    ready_to_generate = bool(payload.get("ready_to_generate"))
    return WorkflowChatSelection(
        selected_template_path=selected_template_path,
        selected_template_reason=selected_template_reason,
        next_question=next_question,
        ready_to_generate=ready_to_generate,
    )


def _resolve_template_path(
    template_path_value: str,
    catalog: Sequence[WorkflowTemplateCatalogEntry],
) -> Path:
    normalized_value = _normalize_template_path_value(template_path_value)
    path_without_suffix = _path_without_suffix(template_path_value)
    for entry in catalog:
        if (
            template_path_value == str(entry.path)
            or template_path_value == entry.path.name
            or template_path_value == entry.path.stem
            or normalized_value == _normalize_template_path_value(str(entry.path))
            or normalized_value == _normalize_template_path_value(entry.path.name)
            or normalized_value == _normalize_template_path_value(entry.path.stem)
            or path_without_suffix == _path_without_suffix(str(entry.path))
        ):
            return entry.path
    raise RuntimeError(
        "Workflow selection response referenced unknown template "
        f"{template_path_value!r}."
    )


def _normalize_template_path_value(value: str) -> str:
    return value.strip().rstrip("./").rstrip()


def _path_without_suffix(value: str) -> str:
    return str(Path(value).with_suffix(""))


def _verbose_print(stderr: TextIO, verbose: bool, message: str) -> None:
    if verbose:
        print(f"[verbose] {message}", file=stderr)


def _find_catalog_entry(
    catalog: Sequence[WorkflowTemplateCatalogEntry],
    template_path: Path,
) -> WorkflowTemplateCatalogEntry:
    for entry in catalog:
        if entry.path == template_path:
            return entry
    raise RuntimeError(f"Could not find workflow template {template_path}.")


def _catalog_entry_to_data(entry: WorkflowTemplateCatalogEntry) -> dict[str, Any]:
    return {
        "file": str(entry.path),
        "when_to_use": list(entry.template.when_to_use),
        "how_to_fill_this_out": list(entry.template.how_to_fill_this_out),
        "task_templates": [
            {
                "index": index,
                "description": task_template.description,
                "complexity": task_template.complexity.value,
                "input_state": task_template.input_state,
                "output_state_type": task_template.output_state_type,
                "upstream_task_template_indexes": list(
                    task_template.upstream_task_template_indexes
                ),
                "dependent_state": list(task_template.dependent_state),
                "generation": (
                    task_template.generation.to_data()
                    if task_template.generation is not None
                    else None
                ),
            }
            for index, task_template in enumerate(entry.template.task_templates)
        ],
    }


def _selection_system_prompt() -> str:
    return (
        "You are an interactive workflow template router.\n"
        "Choose the best workflow template for the user's request.\n"
        "If the request is not fully specified, ask exactly one concise "
        "follow-up question.\n"
        "Return JSON with keys: selected_template_path, selected_template_reason, "
        "next_question, ready_to_generate.\n"
        "selected_template_path must match one of the catalog entries.\n"
        "Use the template when_to_use and task descriptions to decide.\n"
        "Do not output markdown."
    )


def _generation_system_prompt() -> str:
    return (
        "You are a workflow task generator.\n"
        "Create concrete workflow tasks that satisfy the selected workflow template.\n"
        "Return JSON with a top-level tasks array.\n"
        "Every task must include task_id, status, description, complexity, "
        "input_state, output_state_type, upstream_task_ids, and dependent_state.\n"
        "Use status open for every generated task.\n"
        "Make task IDs unique and filesystem-friendly.\n"
        "Preserve the task ordering implied by the workflow template.\n"
        "If the template includes a generation block, expand it when the conversation "
        "provides multiple items that should become separate tasks.\n"
        "Do not output markdown."
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
