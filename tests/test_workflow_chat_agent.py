from __future__ import annotations

import io
import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from urllib.request import Request

import pytest

from powdrr_lift.cli import main
from powdrr_lift.core import (
    TaskComplexity,
    WorkflowTaskTemplate,
    WorkflowTemplate,
    save_workflow_template,
)
from powdrr_lift.workflow_chat_agent import (
    AnthropicChatClient,
    WorkflowChatConfig,
    _resolve_api_key,
    run_workflow_chat,
)


def test_cli_workflow_chat_wires_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    templates_dir = repo_root / "templates"
    templates_dir.mkdir()
    save_workflow_template(_build_template(), templates_dir / "specify-a-feature.json")

    captured: dict[str, object] = {}

    def _fake_run_workflow_chat(config: WorkflowChatConfig, **kwargs: object) -> int:
        captured["config"] = config
        return 0

    monkeypatch.setattr("powdrr_lift.cli.run_workflow_chat", _fake_run_workflow_chat)

    exit_code = main(
        [
            "workflow-chat",
            "--repo-root",
            str(repo_root),
            "--templates-dir",
            "templates",
            "--output-dir",
            "generated",
            "--model",
            "test-model",
        ]
    )

    assert exit_code == 0
    config = captured["config"]
    assert isinstance(config, WorkflowChatConfig)
    assert config.templates_dir == templates_dir
    assert config.output_dir == repo_root / "generated"
    assert config.model == "test-model"


def test_run_workflow_chat_generates_and_validates_tasks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    templates_dir = tmp_path / "templates"
    templates_dir.mkdir()
    save_workflow_template(_build_template(), templates_dir / "specify-a-feature.json")

    responses: Iterator[dict[str, object]] = iter(
        [
            {
                "selected_template_path": str(templates_dir / "specify-a-feature.json"),
                "selected_template_reason": "The request is to specify a feature.",
                "next_question": "What feature are you specifying?",
                "ready_to_generate": False,
            },
            {
                "selected_template_path": str(templates_dir / "specify-a-feature.json"),
                "selected_template_reason": "The request is to specify a feature.",
                "next_question": None,
                "ready_to_generate": True,
            },
            {
                "tasks": [
                    {
                        "task_id": "gather-requirements",
                        "status": "open",
                        "description": "Gather the requirements and approach.",
                        "complexity": "medium",
                        "input_state": {
                            "feature": "Add exports",
                            "requirements": ["Expose package symbols"],
                            "approach": ["Add re-exports"],
                        },
                        "output_state_type": "requirements-and-approach-state",
                        "upstream_task_ids": [],
                        "dependent_state": [
                            "requirements-captured",
                            "approach-defined",
                        ],
                    },
                    {
                        "task_id": "specify-prs",
                        "status": "open",
                        "description": "Specify proposed PRs.",
                        "complexity": "high",
                        "input_state": {"proposed_prs": ["Add exports"]},
                        "output_state_type": "proposed-prs-state",
                        "upstream_task_ids": ["gather-requirements"],
                        "dependent_state": ["proposed-prs-specified"],
                    },
                ]
            },
        ]
    )

    class _FakeOpenAIClient:
        def __init__(self, *, model: str, api_key: str, base_url: str) -> None:
            self.model = model
            self.api_key = api_key
            self.base_url = base_url

        def complete_json(self, messages: list[dict[str, str]]) -> dict[str, object]:
            return next(responses)

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(
        "powdrr_lift.workflow_chat_agent.OpenAIChatClient",
        _FakeOpenAIClient,
    )

    output_dir = tmp_path / "generated"
    stdout = io.StringIO()
    stderr = io.StringIO()
    answers = iter(["Build exports", "Add API exports for the package"])

    exit_code = run_workflow_chat(
        WorkflowChatConfig(
            templates_dir=templates_dir,
            output_dir=output_dir,
            api_key="test-key",
            model="test-model",
        ),
        input_func=lambda: next(answers),
        stdout=stdout,
        stderr=stderr,
    )

    assert exit_code == 0
    assert (output_dir / "gather-requirements.json").exists()
    assert (output_dir / "specify-prs.json").exists()
    assert "What feature are you specifying?" in stdout.getvalue()
    assert "Wrote workflow tasks to" in stdout.getvalue()
    assert "Using openai credentials from --api-key" in stderr.getvalue()


def test_run_workflow_chat_uses_anthropic_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    templates_dir = tmp_path / "templates"
    templates_dir.mkdir()
    save_workflow_template(_build_template(), templates_dir / "specify-a-feature.json")

    responses: Iterator[dict[str, object]] = iter(
        [
            {
                "selected_template_path": str(templates_dir / "specify-a-feature.json"),
                "selected_template_reason": "The request is to specify a feature.",
                "next_question": None,
                "ready_to_generate": True,
            },
            {
                "tasks": [
                    {
                        "task_id": "gather-requirements",
                        "status": "open",
                        "description": "Gather the requirements and approach.",
                        "complexity": "medium",
                        "input_state": {
                            "feature": "Add exports",
                            "requirements": ["Expose package symbols"],
                            "approach": ["Add re-exports"],
                        },
                        "output_state_type": "requirements-and-approach-state",
                        "upstream_task_ids": [],
                        "dependent_state": [
                            "requirements-captured",
                            "approach-defined",
                        ],
                    },
                ]
            },
        ]
    )

    captured: dict[str, object] = {}

    class _FakeAnthropicClient:
        def __init__(self, *, model: str, api_key: str, base_url: str) -> None:
            captured["model"] = model
            captured["api_key"] = api_key
            captured["base_url"] = base_url

        def complete_json(self, messages: list[dict[str, str]]) -> dict[str, object]:
            captured["messages"] = messages
            return next(responses)

    monkeypatch.setattr(
        "powdrr_lift.workflow_chat_agent.AnthropicChatClient",
        _FakeAnthropicClient,
    )

    output_dir = tmp_path / "generated"
    stdout = io.StringIO()
    stderr = io.StringIO()

    exit_code = run_workflow_chat(
        WorkflowChatConfig(
            templates_dir=templates_dir,
            output_dir=output_dir,
            provider="anthropic",
            api_key="anth-key",
            base_url="https://api.anthropic.com",
            model="claude-sonnet-4.5",
        ),
        input_func=lambda: "Build exports",
        stdout=stdout,
        stderr=stderr,
    )

    assert exit_code == 0
    assert captured["model"] == "claude-sonnet-4.5"
    assert captured["api_key"] == "anth-key"
    assert captured["base_url"] == "https://api.anthropic.com"
    assert "Using anthropic credentials from --api-key" in stderr.getvalue()
    assert (output_dir / "gather-requirements.json").exists()


def test_run_workflow_chat_uses_zai_provider_for_glm_models(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    templates_dir = tmp_path / "templates"
    templates_dir.mkdir()
    save_workflow_template(_build_template(), templates_dir / "specify-a-feature.json")

    responses: Iterator[dict[str, object]] = iter(
        [
            {
                "selected_template_path": str(templates_dir / "specify-a-feature.json"),
                "selected_template_reason": "The request is to specify a feature.",
                "next_question": None,
                "ready_to_generate": True,
            },
            {
                "tasks": [
                    {
                        "task_id": "gather-requirements",
                        "status": "open",
                        "description": "Gather the requirements and approach.",
                        "complexity": "medium",
                        "input_state": {
                            "feature": "Add exports",
                            "requirements": ["Expose package symbols"],
                            "approach": ["Add re-exports"],
                        },
                        "output_state_type": "requirements-and-approach-state",
                        "upstream_task_ids": [],
                        "dependent_state": [
                            "requirements-captured",
                            "approach-defined",
                        ],
                    },
                ]
            },
        ]
    )

    captured: dict[str, object] = {}

    class _FakeOpenAIClient:
        def __init__(self, *, model: str, api_key: str, base_url: str) -> None:
            captured["model"] = model
            captured["api_key"] = api_key
            captured["base_url"] = base_url

        def complete_json(self, messages: list[dict[str, str]]) -> dict[str, object]:
            captured["messages"] = messages
            return next(responses)

    monkeypatch.setattr(
        "powdrr_lift.workflow_chat_agent.OpenAIChatClient",
        _FakeOpenAIClient,
    )
    monkeypatch.setenv("ZAI_API_KEY", "zai-key")

    output_dir = tmp_path / "generated"
    stdout = io.StringIO()
    stderr = io.StringIO()

    exit_code = run_workflow_chat(
        WorkflowChatConfig(
            templates_dir=templates_dir,
            output_dir=output_dir,
            model="glm-5.2",
        ),
        input_func=lambda: "Build exports",
        stdout=stdout,
        stderr=stderr,
    )

    assert exit_code == 0
    assert captured["model"] == "glm-5.2"
    assert captured["base_url"] == "https://api.z.ai/api/paas/v4/"
    assert "Using zai credentials from ZAI_API_KEY" in stderr.getvalue()
    assert (output_dir / "gather-requirements.json").exists()


def test_resolve_api_key_prefers_env_over_codex_auth(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    _write_codex_auth(
        codex_home / "auth.json",
        access_token="codex-token",
        expiry=datetime.now(UTC) + timedelta(hours=1),
    )
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("OPENAI_API_KEY", "env-token")

    assert _resolve_api_key("openai", None) == ("env-token", "OPENAI_API_KEY")


def test_resolve_api_key_uses_codex_auth_when_env_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    _write_codex_auth(
        codex_home / "auth.json",
        access_token="codex-token",
        expiry=datetime.now(UTC) + timedelta(hours=1),
    )
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("CODEX_API_KEY", raising=False)

    assert _resolve_api_key("openai", None) == (
        "codex-token",
        str(codex_home / "auth.json"),
    )


def test_resolve_api_key_uses_anthropic_env_when_requested(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anth-key")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("CODEX_API_KEY", raising=False)

    assert _resolve_api_key("anthropic", None) == ("anth-key", "ANTHROPIC_API_KEY")


def test_resolve_api_key_uses_zai_env_when_requested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ZAI_API_KEY", "zai-key")
    monkeypatch.delenv("GLM_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("CODEX_API_KEY", raising=False)

    assert _resolve_api_key("zai", None) == ("zai-key", "ZAI_API_KEY")


def test_anthropic_chat_client_sends_messages_api_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class _FakeResponse:
        def __enter__(self) -> _FakeResponse:
            return self

        def __exit__(
            self,
            exc_type: object,
            exc: object,
            tb: object,
        ) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps(
                {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(
                                {
                                    "selected_template_path": (
                                        "templates/specify-a-feature.json"
                                    )
                                }
                            ),
                        }
                    ]
                }
            ).encode("utf-8")

    def _fake_urlopen(request: Request, timeout: float) -> _FakeResponse:
        captured["url"] = request.full_url
        captured["headers"] = dict(request.header_items())
        captured["body"] = json.loads(cast(bytes, request.data).decode("utf-8"))
        captured["timeout"] = timeout
        return _FakeResponse()

    monkeypatch.setattr("powdrr_lift.workflow_chat_agent.urlopen", _fake_urlopen)

    client = AnthropicChatClient(
        model="claude-sonnet-4.5",
        api_key="anth-key",
        base_url="https://api.anthropic.com",
    )
    response = client.complete_json(
        [
            {"role": "system", "content": "system prompt"},
            {"role": "user", "content": "hello"},
        ]
    )

    assert captured["url"] == "https://api.anthropic.com/v1/messages"
    assert captured["body"] == {
        "model": "claude-sonnet-4.5",
        "max_tokens": 4096,
        "system": "system prompt",
        "messages": [
            {
                "role": "user",
                "content": [{"type": "text", "text": "hello"}],
            }
        ],
    }
    assert response == {"selected_template_path": "templates/specify-a-feature.json"}


def _build_template() -> WorkflowTemplate:
    return WorkflowTemplate(
        when_to_use=(
            "When a feature needs to move from an idea to implementation-ready work.",
        ),
        how_to_fill_this_out=("Fill the steps in order.",),
        task_templates=(
            WorkflowTaskTemplate(
                description="Gather the requirements and approach.",
                complexity=TaskComplexity.MEDIUM,
                input_state={
                    "feature": None,
                    "requirements": [],
                    "approach": [],
                },
                output_state_type="requirements-and-approach-state",
            ),
            WorkflowTaskTemplate(
                description="Specify proposed PRs.",
                complexity=TaskComplexity.HIGH,
                input_state={"proposed_prs": []},
                output_state_type="proposed-prs-state",
                upstream_task_template_indexes=(0,),
                dependent_state=("proposed-prs-specified",),
            ),
        ),
    )


def _write_codex_auth(
    auth_path: Path,
    *,
    access_token: str,
    expiry: datetime,
) -> None:
    auth_path.write_text(
        json.dumps(
            {
                "auth_mode": "chatgpt",
                "tokens": {
                    "access_token": access_token,
                    "expiry": expiry.isoformat(),
                },
            }
        ),
        encoding="utf-8",
    )
