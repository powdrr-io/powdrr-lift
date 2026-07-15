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
    _resolve_template_path,
    _resolve_worktree_context,
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
    assert config.repo_root == repo_root
    assert config.templates_dir == Path("templates")
    assert config.output_dir == Path("generated")
    assert config.model == "test-model"
    assert config.verbose is False


def test_cli_workflow_chat_wires_verbose_flag(
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
            "--verbose",
        ]
    )

    assert exit_code == 0
    config = captured["config"]
    assert isinstance(config, WorkflowChatConfig)
    assert config.repo_root == repo_root
    assert config.verbose is True


def test_run_workflow_chat_generates_and_validates_tasks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    worktree_root = repo_root / ".worktrees" / "codex" / "workflow-chat-test"
    templates_dir = worktree_root / "templates"
    templates_dir.mkdir(parents=True)
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
    monkeypatch.setattr(
        "powdrr_lift.workflow_chat_agent._resolve_worktree_context",
        lambda repo_root, stderr, verbose: worktree_root,
    )

    output_dir = Path("generated")
    stdout = io.StringIO()
    stderr = io.StringIO()
    answers = iter(["Build exports", "Add API exports for the package"])

    exit_code = run_workflow_chat(
        WorkflowChatConfig(
            templates_dir=templates_dir,
            repo_root=repo_root,
            output_dir=output_dir,
            api_key="test-key",
            model="test-model",
        ),
        input_func=lambda: next(answers),
        stdout=stdout,
        stderr=stderr,
    )

    assert exit_code == 0
    assert (worktree_root / output_dir / "gather-requirements.json").exists()
    assert (worktree_root / output_dir / "specify-prs.json").exists()
    assert "What feature are you specifying?" in stdout.getvalue()
    assert "Wrote workflow tasks to" in stdout.getvalue()
    assert "Using openai credentials from --api-key" in stderr.getvalue()


def test_run_workflow_chat_verbose_prints_progress(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    worktree_root = repo_root / ".worktrees" / "codex" / "workflow-chat-test"
    templates_dir = worktree_root / "templates"
    templates_dir.mkdir(parents=True)
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
    monkeypatch.setattr(
        "powdrr_lift.workflow_chat_agent._resolve_worktree_context",
        lambda repo_root, stderr, verbose: worktree_root,
    )

    output_dir = Path("generated")
    stdout = io.StringIO()
    stderr = io.StringIO()

    exit_code = run_workflow_chat(
        WorkflowChatConfig(
            templates_dir=templates_dir,
            repo_root=repo_root,
            output_dir=output_dir,
            api_key="test-key",
            model="test-model",
            verbose=True,
        ),
        input_func=lambda: "Build exports",
        stdout=stdout,
        stderr=stderr,
    )

    assert exit_code == 0
    stderr_value = stderr.getvalue()
    assert "[verbose] Loaded 1 workflow template(s)" in stderr_value
    assert "[verbose] Selected provider: openai" in stderr_value
    assert "[verbose] Selected model: test-model" in stderr_value
    assert "[verbose] Initial user request: Build exports" in stderr_value
    assert "[verbose] Generated 1 workflow task(s)" in stderr_value
    assert (worktree_root / output_dir / "gather-requirements.json").exists()


def test_run_workflow_chat_uses_anthropic_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    worktree_root = repo_root / ".worktrees" / "codex" / "workflow-chat-test"
    templates_dir = worktree_root / "templates"
    templates_dir.mkdir(parents=True)
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
    monkeypatch.setattr(
        "powdrr_lift.workflow_chat_agent._resolve_worktree_context",
        lambda repo_root, stderr, verbose: worktree_root,
    )

    output_dir = Path("generated")
    stdout = io.StringIO()
    stderr = io.StringIO()

    exit_code = run_workflow_chat(
        WorkflowChatConfig(
            templates_dir=templates_dir,
            repo_root=repo_root,
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
    assert (worktree_root / output_dir / "gather-requirements.json").exists()


def test_run_workflow_chat_uses_zai_provider_for_glm_models(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    worktree_root = repo_root / ".worktrees" / "codex" / "workflow-chat-test"
    templates_dir = worktree_root / "templates"
    templates_dir.mkdir(parents=True)
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
    monkeypatch.setattr(
        "powdrr_lift.workflow_chat_agent._resolve_worktree_context",
        lambda repo_root, stderr, verbose: worktree_root,
    )

    output_dir = Path("generated")
    stdout = io.StringIO()
    stderr = io.StringIO()

    exit_code = run_workflow_chat(
        WorkflowChatConfig(
            templates_dir=templates_dir,
            repo_root=repo_root,
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
    assert (worktree_root / output_dir / "gather-requirements.json").exists()


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


def test_resolve_template_path_accepts_missing_extension(
    tmp_path: Path,
) -> None:
    templates_dir = tmp_path / "templates"
    templates_dir.mkdir()
    template_path = templates_dir / "specify-a-feature.json"
    save_workflow_template(_build_template(), template_path)
    from powdrr_lift.workflow_chat_agent import WorkflowTemplateCatalogEntry

    catalog = (
        WorkflowTemplateCatalogEntry(
            path=template_path,
            template=_build_template(),
        ),
    )

    assert (
        _resolve_template_path(
            str(template_path.with_suffix("")),
            catalog,
        )
        == template_path
    )


def test_resolve_template_path_accepts_trailing_dot(
    tmp_path: Path,
) -> None:
    templates_dir = tmp_path / "templates"
    templates_dir.mkdir()
    template_path = templates_dir / "specify-a-feature.json"
    save_workflow_template(_build_template(), template_path)
    from powdrr_lift.workflow_chat_agent import WorkflowTemplateCatalogEntry

    catalog = (
        WorkflowTemplateCatalogEntry(
            path=template_path,
            template=_build_template(),
        ),
    )

    assert (
        _resolve_template_path(
            f"{template_path.with_suffix('').as_posix()}.",
            catalog,
        )
        == template_path
    )


def test_resolve_worktree_context_uses_existing_dedicated_worktree(
    tmp_path: Path,
) -> None:
    worktree_root = tmp_path / "repo" / ".worktrees" / "codex" / "workflow-chat"
    worktree_root.mkdir(parents=True)

    stderr = io.StringIO()
    resolved = _resolve_worktree_context(worktree_root, stderr=stderr, verbose=True)

    assert resolved == worktree_root.resolve()
    assert "Using existing worktree context" in stderr.getvalue()


def test_resolve_worktree_context_creates_dedicated_worktree_from_primary_checkout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    script_path = repo_root / "scripts" / "create-worktree.sh"
    script_path.parent.mkdir(parents=True)
    script_path.touch()
    worktree_root = repo_root / ".worktrees" / "codex" / "workflow-chat-20260714"
    worktree_root.mkdir(parents=True)

    captured: dict[str, object] = {}

    def _fake_run(
        cmd: list[str],
        *,
        check: bool,
        capture_output: bool,
        text: bool,
        cwd: Path,
    ) -> object:
        captured["cmd"] = cmd
        captured["cwd"] = cwd
        return type("Result", (), {"stdout": f"{worktree_root}\n"})()

    monkeypatch.setattr("powdrr_lift.workflow_chat_agent.subprocess.run", _fake_run)

    stderr = io.StringIO()
    resolved = _resolve_worktree_context(repo_root, stderr=stderr, verbose=True)

    assert resolved == worktree_root.resolve()
    assert cast(list[str], captured["cmd"])[0] == "bash"
    assert cast(list[str], captured["cmd"])[1] == str(script_path)
    assert cast(list[str], captured["cmd"])[2].startswith("codex/workflow-chat-")
    assert captured["cwd"] == repo_root.resolve()
    assert "Creating dedicated worktree" in stderr.getvalue()


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
