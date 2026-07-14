from __future__ import annotations

import io
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from powdrr_lift.cli import main
from powdrr_lift.core import (
    TaskComplexity,
    WorkflowTaskTemplate,
    WorkflowTemplate,
    save_workflow_template,
)
from powdrr_lift.workflow_chat_agent import (
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

    responses = iter(
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
    assert stderr.getvalue() == ""


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

    assert _resolve_api_key(None) == "env-token"


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

    assert _resolve_api_key(None) == "codex-token"


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
