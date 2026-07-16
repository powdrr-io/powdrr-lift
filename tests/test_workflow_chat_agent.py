from __future__ import annotations

import ast
import io
import json
import re
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from urllib.request import Request

import pytest

from powdrr_lift.cli import main
from powdrr_lift.core import Skill, SkillStep, load_skill, save_skill
from powdrr_lift.workflow_chat_agent import (
    AnthropicChatClient,
    OpenAIChatClient,
    SkillCatalogEntry,
    SkillChatConfig,
    _catalog_entry_to_data,
    _resolve_api_key,
    _resolve_skill_path,
    _resolve_worktree_context,
    run_workflow_chat,
)


def test_cli_workflow_chat_wires_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    skills_dir = repo_root / "skill-definitions"
    skills_dir.mkdir()
    save_skill(_build_skill(), skills_dir / "specify-a-feature.json")

    captured: dict[str, object] = {"messages": []}

    def _fake_run_workflow_chat(config: SkillChatConfig, **kwargs: object) -> int:
        captured["config"] = config
        return 0

    monkeypatch.setattr("powdrr_lift.cli.run_workflow_chat", _fake_run_workflow_chat)

    exit_code = main(
        [
            "workflow-chat",
            "--repo-root",
            str(repo_root),
            "--skills-dir",
            "skill-definitions",
            "--output-dir",
            "generated",
            "--model",
            "test-model",
        ]
    )

    assert exit_code == 0
    config = captured["config"]
    assert isinstance(config, SkillChatConfig)
    assert config.repo_root == repo_root
    assert config.skills_dir == Path("skill-definitions")
    assert config.templates_dir == Path("skill-definitions")
    assert config.output_dir == Path("generated")
    assert config.model == "test-model"
    assert config.verbose is False


def test_cli_workflow_chat_defaults_to_glm_5_2(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    skills_dir = repo_root / "skill-definitions"
    skills_dir.mkdir()
    save_skill(_build_skill(), skills_dir / "specify-a-feature.json")

    captured: dict[str, object] = {"messages": []}

    def _fake_run_workflow_chat(config: SkillChatConfig, **kwargs: object) -> int:
        captured["config"] = config
        return 0

    monkeypatch.setattr("powdrr_lift.cli.run_workflow_chat", _fake_run_workflow_chat)

    exit_code = main(
        [
            "workflow-chat",
            "--repo-root",
            str(repo_root),
            "--skills-dir",
            "skill-definitions",
        ]
    )

    assert exit_code == 0
    config = captured["config"]
    assert isinstance(config, SkillChatConfig)
    assert config.model == "glm-5.2"


def test_cli_workflow_chat_wires_verbose_flag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    skills_dir = repo_root / "skill-definitions"
    skills_dir.mkdir()
    save_skill(_build_skill(), skills_dir / "specify-a-feature.json")

    captured: dict[str, object] = {"messages": []}

    def _fake_run_workflow_chat(config: SkillChatConfig, **kwargs: object) -> int:
        captured["config"] = config
        return 0

    monkeypatch.setattr("powdrr_lift.cli.run_workflow_chat", _fake_run_workflow_chat)

    exit_code = main(
        [
            "workflow-chat",
            "--repo-root",
            str(repo_root),
            "--skills-dir",
            "skill-definitions",
            "--verbose",
        ]
    )

    assert exit_code == 0
    config = captured["config"]
    assert isinstance(config, SkillChatConfig)
    assert config.repo_root == repo_root
    assert config.verbose is True


def test_run_workflow_chat_generates_skill_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    worktree_root = repo_root / ".worktrees" / "skill-chat-test"
    skills_dir = worktree_root / "skill-definitions"
    skills_dir.mkdir(parents=True)
    save_skill(_build_skill(), skills_dir / "specify-a-feature.json")

    responses: Iterator[dict[str, object]] = iter(
        [
            {
                "selected_skill_path": str(skills_dir / "specify-a-feature.json"),
                "selected_skill_reason": "The request is to specify a feature.",
                "next_question": "What feature are you specifying?",
                "ready_to_execute": False,
            },
            {
                "selected_skill_path": str(skills_dir / "specify-a-feature.json"),
                "selected_skill_reason": "The request is to specify a feature.",
                "next_question": None,
                "ready_to_execute": True,
            },
            {
                "kind": "complete",
                "text": "Skill execution complete.",
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
        SkillChatConfig(
            skills_dir=skills_dir,
            repo_root=repo_root,
            output_dir=output_dir,
            api_key="test-key",
            model="test-model",
        ),
        input_func=lambda: next(answers),
        stdout=stdout,
        stderr=stderr,
    )

    summary_path = worktree_root / output_dir / "skill-execution.json"
    assert exit_code == 0
    assert summary_path.exists()
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert summary["selected_skill_name"] == "specify-a-feature"
    assert summary["skill"]["name"] == "specify-a-feature"
    assert "What feature are you specifying?" in stdout.getvalue()
    assert "Wrote skill execution summary to" in stdout.getvalue()
    assert "Using openai credentials from --api-key" in stderr.getvalue()


def test_cli_workflow_chat_end_to_end_specify_feature_with_mocked_llm_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    skills_dir = repo_root / "skill-definitions"
    skills_dir.mkdir()
    source_skills_dir = Path(__file__).resolve().parents[1] / "skill-definitions"
    for skill_path in sorted(source_skills_dir.glob("*.json")):
        save_skill(load_skill(skill_path), skills_dir / skill_path.name)

    worktree_root = repo_root / ".worktrees" / "skill-chat-test"
    worktree_root.mkdir(parents=True)

    system_spec_path = (
        worktree_root
        / "docs"
        / "specs"
        / "display-related-photos"
        / "system-specification.yaml"
    )
    architecture_spec_path = (
        worktree_root
        / "docs"
        / "specs"
        / "display-related-photos"
        / "architecture-specification.yaml"
    )
    implementation_spec_path = (
        worktree_root
        / "docs"
        / "specs"
        / "display-related-photos"
        / "implementation-specification.yaml"
    )
    pr_spec_path = (
        worktree_root
        / "docs"
        / "specs"
        / "display-related-photos"
        / "proposed-pr-specification.yaml"
    )

    def _write_python_command(target: Path, content: str) -> list[str]:
        return [
            "python3",
            "-c",
            (
                "from pathlib import Path; "
                f"Path({str(target)!r}).write_text({content!r}, encoding='utf-8')"
            ),
        ]

    step_descriptions = [
        "Capture the feature goal and success criteria.",
        "Generate the system template and fill it out.",
        "Review the system context before deciding the feature shape.",
        "Generate the architecture template and fill it out.",
        "Review architecture before implementation.",
        "Generate the implementation template and fill it out.",
        "Decide on proposed PRs and fill each template.",
        "Prompt the user to review the result.",
    ]

    responses: Iterator[dict[str, object]] = iter(
        [
            {
                "selected_skill_path": str(skills_dir / "specify-a-feature.json"),
                "selected_skill_reason": (
                    "The user wants a synchronous feature-specification flow."
                ),
                "next_question": None,
                "ready_to_execute": True,
            },
            {
                "kind": "prompt_user",
                "text": "What feature are you specifying?",
                "decisions_and_context": (
                    "Need the feature goal and success criteria."
                ),
            },
            {
                "kind": "next_step",
                "decisions_and_context": (
                    "Goal captured: display related photos; success criteria: "
                    "show related photos in the UI."
                ),
            },
            {
                "kind": "invoke_tool",
                "tool": "shell",
                "parameters": {
                    "command": [
                        "powdrr-lift",
                        "system-specification",
                        "--work-item-name",
                        "display-related-photos",
                    ],
                },
                "decisions_and_context": (
                    "Start system spec generation for display-related-photos."
                ),
            },
            {
                "kind": "invoke_tool",
                "tool": "shell",
                "parameters": {
                    "command": _write_python_command(
                        system_spec_path,
                        "\n".join(
                            [
                                "schema: https://powdrr.io/schemas/specification-v1",
                                "work_item_name: display-related-photos",
                                "feature_goal: Display related photos.",
                                "success_criteria:",
                                "  - Show related photos in the UI.",
                                "constraints:",
                                "  - Keep the worktree changes local.",
                            ]
                        ),
                    ),
                },
                "decisions_and_context": (
                    "System template filled with the captured goal and "
                    "success criteria."
                ),
            },
            {
                "kind": "next_step",
                "decisions_and_context": (
                    "System template filled; move to system review."
                ),
            },
            {
                "kind": "invoke_tool",
                "tool": "shell",
                "parameters": {
                    "command": [
                        "powdrr-lift",
                        "evaluate-system-specification",
                        "--work-item-name",
                        "display-related-photos",
                    ],
                },
                "decisions_and_context": (
                    "Start system review for display-related-photos."
                ),
            },
            {
                "kind": "next_step",
                "decisions_and_context": (
                    "System review complete: keep changes in the current "
                    "worktree and use shell tools."
                ),
            },
            {
                "kind": "invoke_tool",
                "tool": "shell",
                "parameters": {
                    "command": [
                        "powdrr-lift",
                        "architecture-specification",
                        "--work-item-name",
                        "display-related-photos",
                        "--entity-type",
                        "photo",
                    ],
                },
                "decisions_and_context": (
                    "Start architecture spec generation for display-related-photos."
                ),
            },
            {
                "kind": "invoke_tool",
                "tool": "shell",
                "parameters": {
                    "command": _write_python_command(
                        architecture_spec_path,
                        "\n".join(
                            [
                                "schema: https://powdrr.io/schemas/specification-v1",
                                "work_item_name: display-related-photos",
                                "entity_type: photo",
                                "entities:",
                                "  - id: related-photo",
                                "    description: A photo related to the feature.",
                                "relationships:",
                                "  - from: related-photo",
                                "    to: feature-view",
                                "invariants:",
                                "  - Related photos belong to the current feature.",
                            ]
                        ),
                    ),
                },
                "decisions_and_context": (
                    "Architecture template filled with the chosen entity "
                    "model and relationships."
                ),
            },
            {
                "kind": "next_step",
                "decisions_and_context": (
                    "Architecture template filled; move to architecture review."
                ),
            },
            {
                "kind": "invoke_tool",
                "tool": "shell",
                "parameters": {
                    "command": [
                        "powdrr-lift",
                        "evaluate-architecture-specification",
                        "--work-item-name",
                        "display-related-photos",
                        "--entity-type",
                        "photo",
                    ],
                },
                "decisions_and_context": (
                    "Start architecture review for display-related-photos."
                ),
            },
            {
                "kind": "next_step",
                "decisions_and_context": (
                    "Architecture review complete: align with existing "
                    "entities and invariants."
                ),
            },
            {
                "kind": "invoke_tool",
                "tool": "shell",
                "parameters": {
                    "command": [
                        "powdrr-lift",
                        "implementation-specification",
                        "--work-item-name",
                        "display-related-photos",
                    ],
                },
                "decisions_and_context": (
                    "Start implementation spec generation for display-related-photos."
                ),
            },
            {
                "kind": "invoke_tool",
                "tool": "shell",
                "parameters": {
                    "command": _write_python_command(
                        implementation_spec_path,
                        "\n".join(
                            [
                                "schema: https://powdrr.io/schemas/specification-v1",
                                "architecture_id: arch-2026-07-15",
                                "features:",
                                "  - id: display-related-photos",
                                "    action: added",
                                "    description: Display related photos.",
                                "    functional_requirements:",
                                "      - Show related photos in the UI.",
                                "decisions:",
                                "  - id: display-related-photos-layout",
                                "    action: added",
                                "    description: Use a responsive photo grid.",
                            ]
                        ),
                    ),
                },
                "decisions_and_context": (
                    "Implementation template filled with the chosen layout and "
                    "requirements."
                ),
            },
            {
                "kind": "invoke_tool",
                "tool": "shell",
                "parameters": {
                    "command": [
                        "powdrr-lift",
                        "evaluate-implementation-specification",
                        "--work-item-name",
                        "display-related-photos",
                    ],
                },
                "decisions_and_context": (
                    "Implementation spec validated; move to PR planning."
                ),
            },
            {
                "kind": "next_step",
                "decisions_and_context": (
                    "Implementation step complete; use this spec for PR scope."
                ),
            },
            {
                "kind": "invoke_tool",
                "tool": "shell",
                "parameters": {
                    "command": [
                        "powdrr-lift",
                        "pr-specification",
                        "--work-item-name",
                        "display-related-photos",
                    ],
                },
                "decisions_and_context": (
                    "Start PR template generation for the feature."
                ),
            },
            {
                "kind": "invoke_tool",
                "tool": "shell",
                "parameters": {
                    "command": _write_python_command(
                        pr_spec_path,
                        "\n".join(
                            [
                                "schema: https://powdrr.io/schemas/specification-v1",
                                "id: pr-display-related-photos",
                                "feature_ids:",
                                "  - display-related-photos",
                                "intent:",
                                "  problem: Users need related photos surfaced.",
                                "  goal: Add related photos to the feature view.",
                                "  reasoning: The implementation spec defines the",
                                "    behavior and the PR spec captures the intended",
                                "    scope for the async follow-up.",
                                "acceptance_criteria:",
                                "  - id: ac-1",
                                "    description: Related photos are visible.",
                                "expected_tests:",
                                "  - id: test-1",
                                "    description: The related photo list renders.",
                                "required_test_cases:",
                                "  - id: rtc-1",
                                "    description: Verify empty and populated states.",
                                "expected_outcomes:",
                                "  - id: outcome-1",
                                "    description: The workflow identifies scope.",
                                "non_goals:",
                                "  - id: ng-1",
                                "    description: No image upload changes.",
                                "risks:",
                                "  - id: risk-1",
                                "    description: The layout may need extra space.",
                            ]
                        ),
                    ),
                },
                "decisions_and_context": (
                    "PR template filled with acceptance criteria and risks."
                ),
            },
            {
                "kind": "next_step",
                "decisions_and_context": "PR step complete; handoff is ready.",
            },
            {
                "kind": "prompt_user",
                "text": "Please review the draft result.",
                "decisions_and_context": "Ask the user to review the draft.",
            },
            {
                "kind": "complete",
                "text": "Feature specification complete.",
                "decisions_and_context": "User review requested.",
            },
        ]
    )

    captured: dict[str, object] = {"messages": [], "run_history": []}

    class _FakeOpenAIClient:
        def __init__(self, *, model: str, api_key: str, base_url: str) -> None:
            captured["model"] = model
            captured["api_key"] = api_key
            captured["base_url"] = base_url
            self._call_index = 0

        def _assert_selection_prompt(self, messages: list[dict[str, str]]) -> None:
            prompt = json.loads(messages[1]["content"])
            assert prompt["conversation"][0]["content"] == "Build exports"
            assert any(
                skill["name"] == "specify-a-feature" for skill in prompt["skills"]
            )
            assert any(skill["name"] == "review-system" for skill in prompt["skills"])

        def _assert_execution_prompt(
            self,
            messages: list[dict[str, str]],
            *,
            expected_step_index: int,
            expected_step_description: str,
            expected_context_suffix: str | None,
            expected_event_count: int,
            expected_last_event_kind: str | None = None,
        ) -> dict[str, object]:
            prompt = json.loads(messages[1]["content"])
            execution_events = prompt["execution_events"]
            assert len(execution_events) == expected_event_count
            assert prompt["execution_mode"] == "execute_selected_skill"
            assert prompt["selected_skill"]["name"] == "specify-a-feature"
            assert prompt["current_step_index"] == expected_step_index
            assert prompt["current_step"]["description"] == expected_step_description
            assert prompt["transcript"][0]["content"] == "Build exports"
            if expected_context_suffix is None:
                assert prompt["step_context"] == []
            else:
                assert prompt["step_context"][-1] == expected_context_suffix
            if expected_last_event_kind is not None:
                assert execution_events[-1]["kind"] == expected_last_event_kind
            return prompt

        def complete_json(self, messages: list[dict[str, str]]) -> dict[str, object]:
            cast(list[list[dict[str, str]]], captured["messages"]).append(messages)
            if self._call_index == 0:
                self._assert_selection_prompt(messages)
            elif self._call_index == 1:
                self._assert_execution_prompt(
                    messages,
                    expected_step_index=0,
                    expected_step_description=step_descriptions[0],
                    expected_context_suffix=None,
                    expected_event_count=0,
                )
            elif self._call_index == 2:
                self._assert_execution_prompt(
                    messages,
                    expected_step_index=0,
                    expected_step_description=step_descriptions[0],
                    expected_context_suffix=(
                        "Need the feature goal and success criteria."
                    ),
                    expected_event_count=1,
                    expected_last_event_kind="prompt_user",
                )
            elif self._call_index == 3:
                prompt = self._assert_execution_prompt(
                    messages,
                    expected_step_index=1,
                    expected_step_description=step_descriptions[1],
                    expected_context_suffix=(
                        "Goal captured: display related photos; success criteria: "
                        "show related photos in the UI."
                    ),
                    expected_event_count=2,
                    expected_last_event_kind="next_step",
                )
                current_step = cast(dict[str, object], prompt["current_step"])
                tool_invocations = cast(
                    list[dict[str, object]], current_step["tool_invocations"]
                )
                assert tool_invocations[0]["command"] == [
                    "powdrr-lift",
                    "system-specification",
                    "--work-item-name",
                    "<work-item-name>",
                ]
            elif self._call_index == 4:
                self._assert_execution_prompt(
                    messages,
                    expected_step_index=1,
                    expected_step_description=step_descriptions[1],
                    expected_context_suffix=(
                        "Start system spec generation for display-related-photos."
                    ),
                    expected_event_count=3,
                    expected_last_event_kind="invoke_tool",
                )
            elif self._call_index == 5:
                prompt = self._assert_execution_prompt(
                    messages,
                    expected_step_index=1,
                    expected_step_description=step_descriptions[1],
                    expected_context_suffix=(
                        "System template filled with the captured goal and "
                        "success criteria."
                    ),
                    expected_event_count=4,
                    expected_last_event_kind="invoke_tool",
                )
                current_step = cast(dict[str, object], prompt["current_step"])
                tool_invocations = cast(
                    list[dict[str, object]], current_step["tool_invocations"]
                )
                assert tool_invocations[0]["command"] == [
                    "powdrr-lift",
                    "system-specification",
                    "--work-item-name",
                    "<work-item-name>",
                ]
            elif self._call_index == 6:
                self._assert_execution_prompt(
                    messages,
                    expected_step_index=2,
                    expected_step_description=step_descriptions[2],
                    expected_context_suffix=(
                        "System template filled; move to system review."
                    ),
                    expected_event_count=5,
                    expected_last_event_kind="next_step",
                )
                prompt = json.loads(messages[1]["content"])
                current_step = cast(dict[str, object], prompt["current_step"])
                tool_invocations = cast(
                    list[dict[str, object]], current_step["tool_invocations"]
                )
                assert tool_invocations[0]["command"] == [
                    "powdrr-lift",
                    "evaluate-system-specification",
                    "--work-item-name",
                    "<work-item-name>",
                ]
            elif self._call_index == 7:
                self._assert_execution_prompt(
                    messages,
                    expected_step_index=2,
                    expected_step_description=step_descriptions[2],
                    expected_context_suffix=(
                        "Start system review for display-related-photos."
                    ),
                    expected_event_count=6,
                    expected_last_event_kind="invoke_tool",
                )
            elif self._call_index == 8:
                self._assert_execution_prompt(
                    messages,
                    expected_step_index=3,
                    expected_step_description=step_descriptions[3],
                    expected_context_suffix=(
                        "System review complete: keep changes in the current "
                        "worktree and use shell tools."
                    ),
                    expected_event_count=7,
                    expected_last_event_kind="next_step",
                )
                prompt = json.loads(messages[1]["content"])
                current_step = cast(dict[str, object], prompt["current_step"])
                tool_invocations = cast(
                    list[dict[str, object]], current_step["tool_invocations"]
                )
                assert tool_invocations[0]["command"] == [
                    "powdrr-lift",
                    "architecture-specification",
                    "--work-item-name",
                    "<work-item-name>",
                    "--entity-type",
                    "<type>",
                ]
            elif self._call_index == 9:
                self._assert_execution_prompt(
                    messages,
                    expected_step_index=3,
                    expected_step_description=step_descriptions[3],
                    expected_context_suffix=(
                        "Start architecture spec generation for display-related-photos."
                    ),
                    expected_event_count=8,
                    expected_last_event_kind="invoke_tool",
                )
            elif self._call_index == 10:
                self._assert_execution_prompt(
                    messages,
                    expected_step_index=3,
                    expected_step_description=step_descriptions[3],
                    expected_context_suffix=(
                        "Architecture template filled with the chosen entity "
                        "model and relationships."
                    ),
                    expected_event_count=9,
                    expected_last_event_kind="invoke_tool",
                )
            elif self._call_index == 11:
                prompt = self._assert_execution_prompt(
                    messages,
                    expected_step_index=4,
                    expected_step_description=step_descriptions[4],
                    expected_context_suffix=(
                        "Architecture template filled; move to architecture review."
                    ),
                    expected_event_count=10,
                    expected_last_event_kind="next_step",
                )
                current_step = cast(dict[str, object], prompt["current_step"])
                tool_invocations = cast(
                    list[dict[str, object]], current_step["tool_invocations"]
                )
                assert tool_invocations[0]["command"] == [
                    "powdrr-lift",
                    "evaluate-architecture-specification",
                    "--work-item-name",
                    "<work-item-name>",
                    "--entity-type",
                    "<type>",
                ]
            elif self._call_index == 12:
                self._assert_execution_prompt(
                    messages,
                    expected_step_index=4,
                    expected_step_description=step_descriptions[4],
                    expected_context_suffix=(
                        "Start architecture review for display-related-photos."
                    ),
                    expected_event_count=11,
                    expected_last_event_kind="invoke_tool",
                )
            elif self._call_index == 13:
                prompt = self._assert_execution_prompt(
                    messages,
                    expected_step_index=5,
                    expected_step_description=step_descriptions[5],
                    expected_context_suffix=(
                        "Architecture review complete: align with existing "
                        "entities and invariants."
                    ),
                    expected_event_count=12,
                    expected_last_event_kind="next_step",
                )
                current_step = cast(dict[str, object], prompt["current_step"])
                tool_invocations = cast(
                    list[dict[str, object]], current_step["tool_invocations"]
                )
                assert tool_invocations[0]["command"] == [
                    "powdrr-lift",
                    "implementation-specification",
                    "--work-item-name",
                    "<work-item-name>",
                ]
            elif self._call_index == 14:
                self._assert_execution_prompt(
                    messages,
                    expected_step_index=5,
                    expected_step_description=step_descriptions[5],
                    expected_context_suffix=(
                        "Start implementation spec generation for "
                        "display-related-photos."
                    ),
                    expected_event_count=13,
                    expected_last_event_kind="invoke_tool",
                )
            elif self._call_index == 15:
                self._assert_execution_prompt(
                    messages,
                    expected_step_index=5,
                    expected_step_description=step_descriptions[5],
                    expected_context_suffix=(
                        "Implementation template filled with the chosen layout "
                        "and requirements."
                    ),
                    expected_event_count=14,
                    expected_last_event_kind="invoke_tool",
                )
            elif self._call_index == 16:
                self._assert_execution_prompt(
                    messages,
                    expected_step_index=5,
                    expected_step_description=step_descriptions[5],
                    expected_context_suffix=(
                        "Implementation spec validated; move to PR planning."
                    ),
                    expected_event_count=15,
                    expected_last_event_kind="invoke_tool",
                )
            elif self._call_index == 17:
                self._assert_execution_prompt(
                    messages,
                    expected_step_index=6,
                    expected_step_description=step_descriptions[6],
                    expected_context_suffix=(
                        "Implementation step complete; use this spec for PR scope."
                    ),
                    expected_event_count=16,
                    expected_last_event_kind="next_step",
                )
                prompt = json.loads(messages[1]["content"])
                current_step = cast(dict[str, object], prompt["current_step"])
                tool_invocations = cast(
                    list[dict[str, object]], current_step["tool_invocations"]
                )
                assert tool_invocations[0]["command"] == [
                    "powdrr-lift",
                    "pr-specification",
                    "--work-item-name",
                    "<work-item-name>",
                ]
            elif self._call_index == 18:
                self._assert_execution_prompt(
                    messages,
                    expected_step_index=6,
                    expected_step_description=step_descriptions[6],
                    expected_context_suffix=(
                        "Start PR template generation for the feature."
                    ),
                    expected_event_count=17,
                    expected_last_event_kind="invoke_tool",
                )
            elif self._call_index == 19:
                self._assert_execution_prompt(
                    messages,
                    expected_step_index=6,
                    expected_step_description=step_descriptions[6],
                    expected_context_suffix=(
                        "PR template filled with acceptance criteria and risks."
                    ),
                    expected_event_count=18,
                    expected_last_event_kind="invoke_tool",
                )
            elif self._call_index == 20:
                self._assert_execution_prompt(
                    messages,
                    expected_step_index=7,
                    expected_step_description=step_descriptions[7],
                    expected_context_suffix="PR step complete; handoff is ready.",
                    expected_event_count=19,
                    expected_last_event_kind="next_step",
                )
            elif self._call_index == 21:
                self._assert_execution_prompt(
                    messages,
                    expected_step_index=7,
                    expected_step_description=step_descriptions[7],
                    expected_context_suffix="Ask the user to review the draft.",
                    expected_event_count=20,
                    expected_last_event_kind="prompt_user",
                )
            else:
                raise AssertionError(f"Unexpected LLM call index: {self._call_index}")

            response = next(responses)
            self._call_index += 1
            return response

    class _FakeProcess:
        def __init__(self, *, stdout: str = "", stderr: str = "") -> None:
            self.returncode = 0
            self.stdout = stdout
            self.stderr = stderr

    def _fake_run(*args: object, **kwargs: object) -> _FakeProcess:
        command = cast(list[str] | str, args[0])
        shell = bool(kwargs.get("shell"))
        cwd = cast(Path, kwargs.get("cwd", worktree_root))
        run_history = cast(list[dict[str, object]], captured["run_history"])
        run_history.append({"command": command, "shell": shell, "cwd": cwd})

        if command == [
            "powdrr-lift",
            "system-specification",
            "--work-item-name",
            "display-related-photos",
        ]:
            system_spec_path.parent.mkdir(parents=True, exist_ok=True)
            system_spec_path.write_text(
                "\n".join(
                    [
                        "# System specification template.",
                        "schema: https://powdrr.io/schemas/specification-v1",
                        "work_item_name: display-related-photos",
                        "feature_goal: null",
                        "success_criteria: []",
                        "constraints: []",
                    ]
                ),
                encoding="utf-8",
            )
            return _FakeProcess(stdout=f"generated {system_spec_path}\n")

        if command == [
            "powdrr-lift",
            "evaluate-system-specification",
            "--work-item-name",
            "display-related-photos",
        ]:
            return _FakeProcess(stdout="system specification valid\n")

        if command == [
            "powdrr-lift",
            "architecture-specification",
            "--work-item-name",
            "display-related-photos",
            "--entity-type",
            "photo",
        ]:
            architecture_spec_path.parent.mkdir(parents=True, exist_ok=True)
            architecture_spec_path.write_text(
                "\n".join(
                    [
                        "# Architecture specification template.",
                        "schema: https://powdrr.io/schemas/specification-v1",
                        "work_item_name: display-related-photos",
                        "entity_type: photo",
                        "entities: []",
                        "relationships: []",
                        "invariants: []",
                    ]
                ),
                encoding="utf-8",
            )
            return _FakeProcess(stdout=f"generated {architecture_spec_path}\n")

        if command == [
            "powdrr-lift",
            "evaluate-architecture-specification",
            "--work-item-name",
            "display-related-photos",
            "--entity-type",
            "photo",
        ]:
            return _FakeProcess(stdout="architecture specification valid\n")

        if command == [
            "powdrr-lift",
            "implementation-specification",
            "--work-item-name",
            "display-related-photos",
        ]:
            implementation_spec_path.parent.mkdir(parents=True, exist_ok=True)
            implementation_spec_path.write_text(
                "\n".join(
                    [
                        "# Implementation specification template.",
                        "schema: https://powdrr.io/schemas/specification-v1",
                        "architecture_id: arch-2026-07-15",
                        "features: []",
                        "decisions: []",
                    ]
                ),
                encoding="utf-8",
            )
            return _FakeProcess(stdout=f"generated {implementation_spec_path}\n")

        if command == [
            "powdrr-lift",
            "evaluate-implementation-specification",
            "--work-item-name",
            "display-related-photos",
        ]:
            return _FakeProcess(stdout="implementation specification valid\n")

        if command == [
            "powdrr-lift",
            "pr-specification",
            "--work-item-name",
            "display-related-photos",
        ]:
            pr_spec_path.parent.mkdir(parents=True, exist_ok=True)
            pr_spec_path.write_text(
                "\n".join(
                    [
                        "# PR specification template.",
                        "schema: https://powdrr.io/schemas/specification-v1",
                        "id: pr-display-related-photos",
                        "feature_ids: []",
                        "intent:",
                        "  problem: null",
                        "  goal: null",
                        "  reasoning: null",
                    ]
                ),
                encoding="utf-8",
            )
            return _FakeProcess(stdout=f"generated {pr_spec_path}\n")

        if (
            isinstance(command, list)
            and len(command) == 3
            and command[0] == "python3"
            and command[1] == "-c"
        ):
            script = command[2]
            match = re.fullmatch(
                (
                    r"from pathlib import Path; Path\((?P<path>.+)\)"
                    r"\.write_text\((?P<content>.+), encoding='utf-8'\)"
                ),
                script,
                flags=re.DOTALL,
            )
            if match is None:
                raise AssertionError(f"Unexpected python command: {script}")
            target = Path(ast.literal_eval(match.group("path")))
            content = ast.literal_eval(match.group("content"))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            return _FakeProcess(stdout=f"wrote {target}\n")

        raise AssertionError(f"Unexpected shell command: {command!r}")

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(
        "powdrr_lift.workflow_chat_agent.OpenAIChatClient",
        _FakeOpenAIClient,
    )
    monkeypatch.setattr(
        "powdrr_lift.workflow_chat_agent._resolve_worktree_context",
        lambda repo_root, stderr, verbose: worktree_root,
    )
    monkeypatch.setattr("powdrr_lift.workflow_chat_agent.subprocess.run", _fake_run)

    stdout = io.StringIO()
    stderr = io.StringIO()

    answers = iter(["Build exports", "Display related photos", "Looks good"])

    exit_code = run_workflow_chat(
        SkillChatConfig(
            skills_dir=skills_dir,
            repo_root=repo_root,
            output_dir=Path("generated"),
            provider="openai",
            model="test-model",
            api_key="test-key",
            max_turns=30,
        ),
        input_func=lambda: next(answers),
        stdout=stdout,
        stderr=stderr,
    )

    summary_path = worktree_root / "generated" / "skill-execution.json"
    assert exit_code == 0
    assert summary_path.exists()

    messages = cast(list[list[dict[str, str]]], captured["messages"])
    assert len(messages) == 22
    action_prompt = json.loads(messages[1][1]["content"])
    assert action_prompt["execution_mode"] == "execute_selected_skill"
    assert action_prompt["selected_skill"]["name"] == "specify-a-feature"
    assert action_prompt["current_step_index"] == 0
    assert action_prompt["current_step"]["description"] == step_descriptions[0]
    assert action_prompt["step_context"] == []

    run_history = cast(list[dict[str, object]], captured["run_history"])
    assert len(run_history) == 11
    assert run_history[0]["command"] == [
        "powdrr-lift",
        "system-specification",
        "--work-item-name",
        "display-related-photos",
    ]
    system_write_command = cast(list[str], run_history[1]["command"])
    assert system_write_command[:2] == ["python3", "-c"]
    assert "system-specification.yaml" in system_write_command[2]
    assert "Show related photos in the UI." in system_write_command[2]
    assert run_history[2]["command"] == [
        "powdrr-lift",
        "evaluate-system-specification",
        "--work-item-name",
        "display-related-photos",
    ]
    assert run_history[3]["command"] == [
        "powdrr-lift",
        "architecture-specification",
        "--work-item-name",
        "display-related-photos",
        "--entity-type",
        "photo",
    ]
    architecture_write_command = cast(list[str], run_history[4]["command"])
    assert architecture_write_command[:2] == ["python3", "-c"]
    assert "architecture-specification.yaml" in architecture_write_command[2]
    assert "entity_type: photo" in architecture_write_command[2]
    assert run_history[5]["command"] == [
        "powdrr-lift",
        "evaluate-architecture-specification",
        "--work-item-name",
        "display-related-photos",
        "--entity-type",
        "photo",
    ]
    assert run_history[6]["command"] == [
        "powdrr-lift",
        "implementation-specification",
        "--work-item-name",
        "display-related-photos",
    ]
    implementation_write_command = cast(list[str], run_history[7]["command"])
    assert implementation_write_command[:2] == ["python3", "-c"]
    assert "implementation-specification.yaml" in implementation_write_command[2]
    assert "Show related photos in the UI." in implementation_write_command[2]
    assert run_history[8]["command"] == [
        "powdrr-lift",
        "evaluate-implementation-specification",
        "--work-item-name",
        "display-related-photos",
    ]
    assert run_history[9]["command"] == [
        "powdrr-lift",
        "pr-specification",
        "--work-item-name",
        "display-related-photos",
    ]
    pr_write_command = cast(list[str], run_history[10]["command"])
    assert pr_write_command[:2] == ["python3", "-c"]
    assert "proposed-pr-specification.yaml" in pr_write_command[2]
    assert "Related photos are visible." in pr_write_command[2]
    assert system_spec_path.exists()
    assert architecture_spec_path.exists()
    assert implementation_spec_path.exists()
    assert pr_spec_path.exists()
    system_text = system_spec_path.read_text(encoding="utf-8")
    assert "display-related-photos" in system_text
    assert "Show related photos in the UI." in system_text
    architecture_text = architecture_spec_path.read_text(encoding="utf-8")
    assert "display-related-photos" in architecture_text
    assert "entity_type: photo" in architecture_text
    implementation_text = implementation_spec_path.read_text(encoding="utf-8")
    assert "display-related-photos" in implementation_text
    assert "Show related photos in the UI." in implementation_text
    pr_text = pr_spec_path.read_text(encoding="utf-8")
    assert "pr-display-related-photos" in pr_text
    assert "Related photos are visible." in pr_text
    assert "generated" in stdout.getvalue()
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert summary["selected_skill_name"] == "specify-a-feature"
    assert [event["kind"] for event in summary["execution_events"]] == [
        "prompt_user",
        "next_step",
        "invoke_tool",
        "invoke_tool",
        "next_step",
        "invoke_tool",
        "next_step",
        "invoke_tool",
        "invoke_tool",
        "next_step",
        "invoke_tool",
        "next_step",
        "invoke_tool",
        "invoke_tool",
        "invoke_tool",
        "next_step",
        "invoke_tool",
        "invoke_tool",
        "next_step",
        "prompt_user",
        "complete",
    ]


def test_run_workflow_chat_verbose_prints_progress(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    worktree_root = repo_root / ".worktrees" / "skill-chat-test"
    skills_dir = worktree_root / "skill-definitions"
    skills_dir.mkdir(parents=True)
    save_skill(_build_skill(), skills_dir / "specify-a-feature.json")

    responses: Iterator[dict[str, object]] = iter(
        [
            {
                "selected_skill_path": str(skills_dir / "specify-a-feature.json"),
                "selected_skill_reason": "The request is to specify a feature.",
                "next_question": None,
                "ready_to_execute": True,
            },
            {
                "kind": "complete",
                "text": "Skill execution complete.",
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
        SkillChatConfig(
            skills_dir=skills_dir,
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
    assert "[verbose] Loaded 1 skill(s)" in stderr_value
    assert "[verbose] Selected provider: openai" in stderr_value
    assert "[verbose] Selected model: test-model" in stderr_value
    assert "[verbose] Initial user request: Build exports" in stderr_value
    assert "[verbose] Prepared execution summary for specify-a-feature" in stderr_value
    assert (worktree_root / output_dir / "skill-execution.json").exists()


def test_run_workflow_chat_uses_anthropic_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    worktree_root = repo_root / ".worktrees" / "skill-chat-test"
    skills_dir = worktree_root / "skill-definitions"
    skills_dir.mkdir(parents=True)
    save_skill(_build_skill(), skills_dir / "specify-a-feature.json")

    responses: Iterator[dict[str, object]] = iter(
        [
            {
                "selected_skill_path": str(skills_dir / "specify-a-feature.json"),
                "selected_skill_reason": "The request is to specify a feature.",
                "next_question": None,
                "ready_to_execute": True,
            },
            {
                "kind": "complete",
                "text": "Skill execution complete.",
            },
        ]
    )

    captured: dict[str, object] = {"messages": []}

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
        SkillChatConfig(
            skills_dir=skills_dir,
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
    assert (worktree_root / output_dir / "skill-execution.json").exists()


def test_run_workflow_chat_uses_zai_provider_for_glm_models(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    worktree_root = repo_root / ".worktrees" / "skill-chat-test"
    skills_dir = worktree_root / "skill-definitions"
    skills_dir.mkdir(parents=True)
    save_skill(_build_skill(), skills_dir / "specify-a-feature.json")

    responses: Iterator[dict[str, object]] = iter(
        [
            {
                "selected_skill_path": str(skills_dir / "specify-a-feature.json"),
                "selected_skill_reason": "The request is to specify a feature.",
                "next_question": None,
                "ready_to_execute": True,
            },
            {
                "kind": "complete",
                "text": "Skill execution complete.",
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
        SkillChatConfig(
            skills_dir=skills_dir,
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
    assert (worktree_root / output_dir / "skill-execution.json").exists()


def test_run_workflow_chat_prompts_for_retry_on_provider_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    worktree_root = repo_root / ".worktrees" / "skill-chat-test"
    skills_dir = worktree_root / "skill-definitions"
    skills_dir.mkdir(parents=True)
    save_skill(_build_skill(), skills_dir / "specify-a-feature.json")

    captured: dict[str, object] = {"calls": 0}

    class _FakeOpenAIClient:
        def __init__(self, *, model: str, api_key: str, base_url: str) -> None:
            self.model = model
            self.api_key = api_key
            self.base_url = base_url

        def complete_json(self, messages: list[dict[str, str]]) -> dict[str, object]:
            call_index = cast(int, captured["calls"])
            captured["calls"] = call_index + 1
            if call_index == 0:
                return {
                    "selected_skill_path": str(skills_dir / "specify-a-feature.json"),
                    "selected_skill_reason": "The request is to specify a feature.",
                    "next_question": None,
                    "ready_to_execute": True,
                }
            if call_index == 1:
                return {
                    "kind": "next_step",
                    "decisions_and_context": "Step 1 complete.",
                }
            if call_index == 2:
                raise RuntimeError("OpenAI request failed with HTTP 429: rate limit")
            if call_index == 3:
                return {
                    "kind": "complete",
                    "text": "Skill execution complete.",
                }
            raise AssertionError(f"Unexpected call index: {call_index}")

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(
        "powdrr_lift.workflow_chat_agent.OpenAIChatClient",
        _FakeOpenAIClient,
    )
    monkeypatch.setattr(
        "powdrr_lift.workflow_chat_agent._resolve_worktree_context",
        lambda repo_root, stderr, verbose: worktree_root,
    )

    stdout = io.StringIO()
    stderr = io.StringIO()

    exit_code = run_workflow_chat(
        SkillChatConfig(
            skills_dir=skills_dir,
            repo_root=repo_root,
            output_dir=Path("generated"),
            api_key="test-key",
            model="test-model",
        ),
        input_func=iter(["Build exports", "retry"]).__next__,
        stdout=stdout,
        stderr=stderr,
    )

    assert exit_code == 0
    assert cast(int, captured["calls"]) == 4
    assert "workflow execution for step 2/2 failed" in stderr.getvalue()
    assert "Type 'retry' to try again or 'abort' to stop:" in stdout.getvalue()
    assert (worktree_root / "generated" / "skill-execution.json").exists()


def test_run_workflow_chat_repairs_missing_action_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    worktree_root = repo_root / ".worktrees" / "skill-chat-test"
    skills_dir = worktree_root / "skill-definitions"
    skills_dir.mkdir(parents=True)
    save_skill(_build_skill(), skills_dir / "specify-a-feature.json")

    captured: dict[str, object] = {"calls": 0, "messages": []}

    class _FakeOpenAIClient:
        def __init__(self, *, model: str, api_key: str, base_url: str) -> None:
            self.model = model
            self.api_key = api_key
            self.base_url = base_url

        def complete_json(self, messages: list[dict[str, str]]) -> dict[str, object]:
            cast(list[list[dict[str, str]]], captured["messages"]).append(messages)
            call_index = cast(int, captured["calls"])
            captured["calls"] = call_index + 1
            if call_index == 0:
                return {
                    "selected_skill_path": str(skills_dir / "specify-a-feature.json"),
                    "selected_skill_reason": "The request is to specify a feature.",
                    "next_question": None,
                    "ready_to_execute": True,
                }
            if call_index == 1:
                return {
                    "decisions_and_context": (
                        "The step is ready to complete, but the schema is missing kind."
                    ),
                }
            if call_index == 2:
                repair_request = messages[-1]["content"]
                assert "workflow action schema" in repair_request
                assert "kind" in repair_request
                return {
                    "kind": "complete",
                    "text": "Skill execution complete.",
                }
            raise AssertionError(f"Unexpected call index: {call_index}")

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(
        "powdrr_lift.workflow_chat_agent.OpenAIChatClient",
        _FakeOpenAIClient,
    )
    monkeypatch.setattr(
        "powdrr_lift.workflow_chat_agent._resolve_worktree_context",
        lambda repo_root, stderr, verbose: worktree_root,
    )

    stdout = io.StringIO()
    stderr = io.StringIO()

    exit_code = run_workflow_chat(
        SkillChatConfig(
            skills_dir=skills_dir,
            repo_root=repo_root,
            output_dir=Path("generated"),
            api_key="test-key",
            model="test-model",
        ),
        input_func=lambda: "Build exports",
        stdout=stdout,
        stderr=stderr,
    )

    assert exit_code == 0
    assert cast(int, captured["calls"]) == 3
    assert "response needs repair" in stderr.getvalue()
    assert (worktree_root / "generated" / "skill-execution.json").exists()


def test_catalog_entry_to_data_includes_structured_tool_invocations() -> None:
    skill_path = (
        Path(__file__).resolve().parents[1] / "skill-definitions" / "review-system.json"
    )
    skill = load_skill(skill_path)
    data = _catalog_entry_to_data(
        SkillCatalogEntry(path=skill_path, skill=skill),
    )

    tool_invocations = [
        tool_invocation
        for step in data["steps"]
        for tool_invocation in step.get("tool_invocations", [])
    ]

    assert tool_invocations == [
        {
            "tool": "shell",
            "command": [
                "powdrr-lift",
                "evaluate-system-specification",
                "--work-item-name",
                "<work-item-name>",
            ],
        }
    ]


def test_run_workflow_chat_executes_shell_tool_actions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    worktree_root = repo_root / ".worktrees" / "skill-chat-test"
    skills_dir = worktree_root / "skill-definitions"
    skills_dir.mkdir(parents=True)
    save_skill(_build_skill(), skills_dir / "specify-a-feature.json")

    responses: Iterator[dict[str, object]] = iter(
        [
            {
                "selected_skill_path": str(skills_dir / "specify-a-feature.json"),
                "selected_skill_reason": "The request is to specify a feature.",
                "next_question": None,
                "ready_to_execute": True,
            },
            {
                "kind": "invoke_tool",
                "tool": "shell",
                "parameters": {
                    "command": [
                        "powdrr-lift",
                        "system-specification",
                        "--work-item-name",
                        "demo",
                    ],
                },
            },
            {
                "kind": "complete",
                "text": "Skill execution complete.",
            },
        ]
    )

    captured: dict[str, object] = {"messages": []}

    class _FakeOpenAIClient:
        def __init__(self, *, model: str, api_key: str, base_url: str) -> None:
            captured["model"] = model
            captured["api_key"] = api_key
            captured["base_url"] = base_url

        def complete_json(self, messages: list[dict[str, str]]) -> dict[str, object]:
            cast(list[list[dict[str, str]]], captured["messages"]).append(messages)
            return next(responses)

    class _FakeProcess:
        def __init__(self) -> None:
            self.returncode = 0
            self.stdout = "tool stdout\n"
            self.stderr = "tool stderr\n"

    def _fake_run(*args: object, **kwargs: object) -> _FakeProcess:
        captured["run_args"] = args
        captured["run_kwargs"] = kwargs
        return _FakeProcess()

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(
        "powdrr_lift.workflow_chat_agent.OpenAIChatClient",
        _FakeOpenAIClient,
    )
    monkeypatch.setattr(
        "powdrr_lift.workflow_chat_agent._resolve_worktree_context",
        lambda repo_root, stderr, verbose: worktree_root,
    )
    monkeypatch.setattr("powdrr_lift.workflow_chat_agent.subprocess.run", _fake_run)

    output_dir = Path("generated")
    stdout = io.StringIO()
    stderr = io.StringIO()

    exit_code = run_workflow_chat(
        SkillChatConfig(
            skills_dir=skills_dir,
            repo_root=repo_root,
            output_dir=output_dir,
            api_key="test-key",
            model="test-model",
        ),
        input_func=lambda: "Build exports",
        stdout=stdout,
        stderr=stderr,
    )

    summary_path = worktree_root / output_dir / "skill-execution.json"
    assert exit_code == 0
    assert summary_path.exists()
    run_args = cast(tuple[object, ...], captured["run_args"])
    run_kwargs = cast(dict[str, object], captured["run_kwargs"])
    assert run_args[0] == [
        "powdrr-lift",
        "system-specification",
        "--work-item-name",
        "demo",
    ]
    assert run_kwargs["shell"] is False
    assert "tool stdout" in stdout.getvalue()
    assert "tool stderr" in stderr.getvalue()
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert summary["execution_events"][0]["kind"] == "invoke_tool"
    assert summary["execution_events"][0]["result"]["returncode"] == 0


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


def test_resolve_skill_path_accepts_missing_extension(
    tmp_path: Path,
) -> None:
    skills_dir = tmp_path / "skill-definitions"
    skills_dir.mkdir()
    skill_path = skills_dir / "specify-a-feature.json"
    save_skill(_build_skill(), skill_path)
    from powdrr_lift.workflow_chat_agent import SkillCatalogEntry

    catalog = (
        SkillCatalogEntry(
            path=skill_path,
            skill=_build_skill(),
        ),
    )

    assert _resolve_skill_path(str(skill_path.with_suffix("")), catalog) == skill_path


def test_resolve_skill_path_accepts_trailing_dot(
    tmp_path: Path,
) -> None:
    skills_dir = tmp_path / "skill-definitions"
    skills_dir.mkdir()
    skill_path = skills_dir / "specify-a-feature.json"
    save_skill(_build_skill(), skill_path)
    from powdrr_lift.workflow_chat_agent import SkillCatalogEntry

    catalog = (
        SkillCatalogEntry(
            path=skill_path,
            skill=_build_skill(),
        ),
    )

    assert (
        _resolve_skill_path(
            f"{skill_path.with_suffix('').as_posix()}.",
            catalog,
        )
        == skill_path
    )


def test_resolve_worktree_context_uses_existing_dedicated_worktree(
    tmp_path: Path,
) -> None:
    worktree_root = tmp_path / "repo" / ".worktrees" / "skill-chat"
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
    worktree_root = repo_root / ".worktrees" / "workflow-chat-20260714"
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
    assert cast(list[str], captured["cmd"])[2].startswith("workflow-chat-")
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
                                    "selected_skill_path": (
                                        "skill-definitions/specify-a-feature.json"
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
    assert response == {
        "selected_skill_path": "skill-definitions/specify-a-feature.json"
    }


def test_openai_chat_client_reports_malformed_json_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
                    "choices": [
                        {
                            "message": {
                                "content": "not-json",
                            }
                        }
                    ]
                }
            ).encode("utf-8")

    def _fake_urlopen(request: Request, timeout: float) -> _FakeResponse:
        return _FakeResponse()

    monkeypatch.setattr("powdrr_lift.workflow_chat_agent.urlopen", _fake_urlopen)

    client = OpenAIChatClient(
        model="test-model",
        api_key="test-key",
        base_url="https://api.openai.com/v1",
    )

    with pytest.raises(
        RuntimeError, match="OpenAI response content was not valid JSON"
    ):
        client.complete_json([{"role": "user", "content": "hello"}])


def _build_skill() -> Skill:
    return Skill(
        name="specify-a-feature",
        when_to_use=(
            "When the user wants a guided synchronous flow for a new feature.",
            "When the flow should match the user's intent to a checked-in skill.",
        ),
        steps=(
            SkillStep(
                description="Capture the feature goal.",
                details="Record the user-visible outcome first.",
            ),
            SkillStep(
                description="Summarize the result.",
                details="Leave the user with a concise handoff.",
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
