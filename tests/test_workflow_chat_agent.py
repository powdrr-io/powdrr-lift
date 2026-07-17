from __future__ import annotations

import io
import json
import os
import subprocess
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TextIO, cast
from urllib.request import Request

import pytest
import yaml

from powdrr_lift.cli import main
from powdrr_lift.core import (
    Skill,
    SkillStep,
    SkillToolInvocation,
    load_skill,
    save_skill,
)
from powdrr_lift.core.architecture_specification import (
    validate_architecture_specification_yaml,
)
from powdrr_lift.core.implementation_specification import (
    validate_implementation_specification_yaml,
)
from powdrr_lift.core.pr_specification import validate_pr_specification_yaml
from powdrr_lift.core.system_specification import validate_system_specification_yaml
from powdrr_lift.workflow_chat_agent import (
    AnthropicChatClient,
    OpenAIChatClient,
    SkillCatalogEntry,
    SkillChatConfig,
    SkillChatEdit,
    _action_system_prompt,
    _apply_file_edits,
    _catalog_entry_to_data,
    _resolve_api_key,
    _resolve_skill_path,
    _resolve_worktree_context,
    run_workflow_chat,
)

# ruff: noqa: E501


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


def test_workflow_chat_action_prompt_mentions_gather_context() -> None:
    prompt = _action_system_prompt()

    assert "gather-context" in prompt
    assert "edit" in prompt
    assert "file_path" in prompt
    assert "requirements" in prompt
    assert "entity-relationships" in prompt
    assert "proposed PRs" in prompt


def test_run_workflow_chat_gathers_context_into_follow_up_step(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    worktree_root = repo_root / ".worktrees" / "skill-chat-test"
    skills_dir = worktree_root / "skill-definitions"
    skills_dir.mkdir(parents=True)
    save_skill(
        Skill(
            name="specify-a-feature",
            when_to_use=("When the user needs a simple synchronous workflow.",),
            steps=(
                SkillStep(
                    description="Discover what requirements are already specified.",
                    details=(
                        "Use gather-context to retrieve existing requirement notes."
                    ),
                ),
                SkillStep(
                    description="Summarize the gathered context.",
                    details="Describe the requirements that were found.",
                ),
            ),
        ),
        skills_dir / "specify-a-feature.json",
    )

    system_spec_path = (
        worktree_root
        / "docs"
        / "specs"
        / "display-related-photos"
        / "system-specification.yaml"
    )
    system_spec_path.parent.mkdir(parents=True, exist_ok=True)
    system_spec_path.write_text(
        "\n".join(
            [
                "schema: https://powdrr.io/schemas/specification-v1",
                "id: system-display-related-photos",
                "requirements:",
                "  - id: req-1",
                "    description: Show related photos in the UI.",
                "approach:",
                "  - id: app-1",
                "    description: Reuse the existing photo grid.",
            ]
        ),
        encoding="utf-8",
    )

    responses: Iterator[dict[str, object]] = iter(
        [
            {
                "selected_skill_path": str(skills_dir / "specify-a-feature.json"),
                "selected_skill_reason": "The request is to inspect existing context.",
                "next_question": None,
                "ready_to_execute": True,
            },
            {
                "kind": "gather-context",
                "types": ["requirements"],
                "keywords": ["related photos"],
                "decisions_and_context": (
                    "Need the existing requirements before summarizing."
                ),
            },
            {
                "kind": "next_step",
                "decisions_and_context": "Requirements gathered.",
            },
            {
                "kind": "complete",
                "text": "Context gathered.",
                "decisions_and_context": "Ready to summarize the requirements.",
            },
        ]
    )

    captured: dict[str, object] = {"messages": []}

    class _FakeOpenAIClient:
        def __init__(self, *, model: str, api_key: str, base_url: str) -> None:
            captured["model"] = model
            captured["api_key"] = api_key
            captured["base_url"] = base_url
            self._call_index = 0

        def complete_json(self, messages: list[dict[str, str]]) -> dict[str, object]:
            cast(list[list[dict[str, str]]], captured["messages"]).append(messages)
            prompt = json.loads(messages[1]["content"])
            if self._call_index == 0:
                assert (
                    prompt["conversation"][0]["content"]
                    == "Find the existing requirements"
                )
            elif self._call_index == 1:
                assert prompt["current_step"]["description"] == (
                    "Discover what requirements are already specified."
                )
                assert prompt["step_context"] == []
            elif self._call_index == 2:
                assert prompt["current_step"]["description"] == (
                    "Discover what requirements are already specified."
                )
                assert "Gathered context:" in prompt["step_context"][-1]
                assert "Show related photos in the UI." in prompt["step_context"][-1]
                assert prompt["execution_events"][-1]["kind"] == "gather-context"
                assert prompt["execution_events"][-1]["types"] == ["requirements"]
                assert (
                    prompt["execution_events"][-1]["result"]["matches"][0]["item"][
                        "description"
                    ]
                    == "Show related photos in the UI."
                )
            elif self._call_index == 3:
                assert prompt["current_step"]["description"] == (
                    "Summarize the gathered context."
                )
                assert prompt["step_context"][-1] == "Requirements gathered."
                assert prompt["execution_events"][-1]["kind"] == "next_step"
            else:
                raise AssertionError(f"Unexpected LLM call index: {self._call_index}")

            response = next(responses)
            self._call_index += 1
            return response

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
            max_turns=10,
        ),
        input_func=lambda: "Find the existing requirements",
        stdout=stdout,
        stderr=stderr,
    )

    summary_path = worktree_root / "generated" / "skill-execution.json"
    assert exit_code == 0
    assert summary_path.exists()
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert [event["kind"] for event in summary["execution_events"]] == [
        "gather-context",
        "next_step",
        "complete",
    ]
    assert "Context gathered." in stdout.getvalue()


def test_run_workflow_chat_surfaces_current_file_context_for_edit_actions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    worktree_root = repo_root / ".worktrees" / "skill-chat-test"
    skills_dir = worktree_root / "skill-definitions"
    skills_dir.mkdir(parents=True)
    save_skill(
        Skill(
            name="specify-a-feature",
            when_to_use=("When the user needs a simple synchronous workflow.",),
            steps=(
                SkillStep(
                    description="Generate the system template.",
                    details="Create the system specification file first.",
                    tool_invocations=(
                        SkillToolInvocation(
                            tool="shell",
                            command=(
                                "powdrr-lift",
                                "system-specification",
                                "--work-item-name",
                                "display-related-photos",
                            ),
                        ),
                    ),
                ),
                SkillStep(
                    description="Edit the system template.",
                    details="Update the generated file in place.",
                ),
                SkillStep(
                    description="Finish the flow.",
                    details="Report completion after the edit lands.",
                ),
            ),
        ),
        skills_dir / "specify-a-feature.json",
    )

    system_spec_path = (
        worktree_root
        / "docs"
        / "specs"
        / "display-related-photos"
        / "system-specification.yaml"
    )

    responses: Iterator[dict[str, object]] = iter(
        [
            {
                "selected_skill_path": str(skills_dir / "specify-a-feature.json"),
                "selected_skill_reason": "The request is to inspect existing context.",
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
                        "display-related-photos",
                    ],
                },
                "decisions_and_context": "Create the system spec template.",
            },
            {
                "kind": "edit",
                "file_path": (
                    "docs/specs/display-related-photos/system-specification.yaml"
                ),
                "edits": [
                    {
                        "kind": "replace",
                        "start_line": 3,
                        "end_line": 3,
                        "text": "id: display-related-photos",
                    }
                ],
                "decisions_and_context": "Set the system spec id.",
            },
            {
                "kind": "next_step",
                "decisions_and_context": "System template updated.",
            },
            {
                "kind": "complete",
                "text": "Done.",
                "decisions_and_context": "Edit complete.",
            },
        ]
    )

    captured: dict[str, object] = {"messages": []}

    class _FakeOpenAIClient:
        def __init__(self, *, model: str, api_key: str, base_url: str) -> None:
            captured["model"] = model
            captured["api_key"] = api_key
            captured["base_url"] = base_url
            self._call_index = 0

        def complete_json(self, messages: list[dict[str, str]]) -> dict[str, object]:
            cast(list[list[dict[str, str]]], captured["messages"]).append(messages)
            prompt = json.loads(messages[1]["content"])
            if self._call_index == 0:
                assert "current_file" not in prompt
            elif self._call_index == 1:
                assert prompt["current_file"] is None
            elif self._call_index == 2:
                assert prompt["current_file"]["path"] == str(
                    system_spec_path.relative_to(worktree_root)
                )
                assert prompt["current_file"]["lines"][0]["text"] == (
                    "# System specification template."
                )
                assert prompt["current_file"]["lines"][2]["text"] == "id: null"
            elif self._call_index == 3:
                assert prompt["current_file"]["path"] == str(
                    system_spec_path.relative_to(worktree_root)
                )
                assert prompt["execution_events"][-1]["kind"] == "edit"
            elif self._call_index == 4:
                assert prompt["current_file"]["path"] == str(
                    system_spec_path.relative_to(worktree_root)
                )
                assert prompt["execution_events"][-1]["kind"] == "next_step"
            else:
                raise AssertionError(f"Unexpected LLM call index: {self._call_index}")

            response = next(responses)
            self._call_index += 1
            return response

    class _FakeProcess:
        def __init__(self) -> None:
            self.returncode = 0
            self.stdout = f"{system_spec_path}\n"
            self.stderr = ""

    def _fake_run(*args: object, **kwargs: object) -> _FakeProcess:
        del args, kwargs
        system_spec_path.parent.mkdir(parents=True, exist_ok=True)
        system_spec_path.write_text(
            "\n".join(
                [
                    "# System specification template.",
                    "schema: https://powdrr.io/schemas/specification-v1",
                    "id: null",
                    "requirements:",
                    "  - id: null",
                    "    description: null",
                    "    state: null",
                    "approach:",
                    "  - id: null",
                    "    description: null",
                    "    state: null",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        return _FakeProcess()

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(
        "powdrr_lift.workflow_chat_agent.OpenAIChatClient",
        _FakeOpenAIClient,
    )
    monkeypatch.setattr(
        "powdrr_lift.workflow_chat_agent.subprocess.run",
        _fake_run,
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
            max_turns=10,
        ),
        input_func=lambda: "Find the existing requirements",
        stdout=stdout,
        stderr=stderr,
    )

    assert exit_code == 0
    assert system_spec_path.read_text(encoding="utf-8").splitlines()[2] == (
        "id: display-related-photos"
    )
    summary = json.loads(
        (worktree_root / "generated" / "skill-execution.json").read_text(
            encoding="utf-8"
        )
    )
    assert [event["kind"] for event in summary["execution_events"]] == [
        "invoke_tool",
        "edit",
        "next_step",
        "complete",
    ]


@pytest.mark.parametrize(
    ("current_text", "edits", "expected_text"),
    [
        (
            "\n".join(["line-1", "line-2", "line-3", "line-4", "line-5"]) + "\n",
            (
                SkillChatEdit(kind="add", start_line=2, text="insert-a"),
                SkillChatEdit(kind="remove", start_line=4, end_line=4),
                SkillChatEdit(
                    kind="replace",
                    start_line=5,
                    end_line=5,
                    text="line-5-updated",
                ),
            ),
            "\n".join(
                [
                    "line-1",
                    "insert-a",
                    "line-2",
                    "line-3",
                    "line-5-updated",
                ]
            )
            + "\n",
        ),
        (
            "\n".join(["line-1", "line-2", "line-3", "line-4", "line-5", "line-6"])
            + "\n",
            (
                SkillChatEdit(kind="add", start_line=2, text="insert-a"),
                SkillChatEdit(kind="remove", start_line=4, end_line=5),
                SkillChatEdit(kind="add", start_line=6, text="insert-b"),
            ),
            "\n".join(
                [
                    "line-1",
                    "insert-a",
                    "line-2",
                    "line-3",
                    "insert-b",
                    "line-6",
                ]
            )
            + "\n",
        ),
        (
            "\n".join(["line-1", "line-2", "line-3"]) + "\n",
            (
                SkillChatEdit(kind="remove", start_line=2, end_line=3),
                SkillChatEdit(kind="add", start_line=4, text="tail"),
            ),
            "\n".join(["line-1", "tail"]) + "\n",
        ),
    ],
)
def test_apply_file_edits_uses_original_line_numbers_for_interleaved_edits(
    current_text: str,
    edits: tuple[SkillChatEdit, ...],
    expected_text: str,
) -> None:
    assert _apply_file_edits(current_text, edits) == expected_text


def test_cli_workflow_chat_end_to_end_specify_feature_with_mocked_llm_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_repo_root = next(
        parent
        for parent in Path(__file__).resolve().parents
        if (parent / "pyproject.toml").exists()
    )
    repo_root = tmp_path / "repo"
    subprocess.run(
        ["git", "clone", str(source_repo_root), str(repo_root)],
        check=True,
        capture_output=True,
        text=True,
    )
    tool_bin = tmp_path / "bin"
    tool_bin.mkdir()
    powdrr_lift_wrapper = tool_bin / "powdrr-lift"
    powdrr_lift_wrapper.write_text(
        '#!/bin/sh\nexec uv run powdrr-lift "$@"\n',
        encoding="utf-8",
    )
    powdrr_lift_wrapper.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tool_bin}:{os.environ['PATH']}")
    skills_dir = repo_root / "skill-definitions"
    worktree_root_holder: dict[str, Path] = {}
    system_spec_dir = "docs/specs/display-related-photos"
    system_spec_filename = "system-specification.yaml"
    architecture_spec_filename = "architecture-specification.yaml"
    implementation_spec_filename = "implementation-specification.yaml"
    pr_spec_filename = "proposed-pr-specification.yaml"
    system_goal_description = "Show related photos in the feature view."
    system_grid_description = "Reuse the existing grid layout for related photos."
    system_grid_approach_description = "Render related photos in the existing grid."
    system_empty_state_description = (
        "Provide a helpful empty state when there are no related photos."
    )
    architecture_related_photo_rationale = (
        'Align with "req-related-photos" and "app-related-photos-grid".'
    )
    architecture_gallery_photo_rationale = (
        'Align with "req-gallery-grid" and "app-related-photos-grid".'
    )
    architecture_relationship_rationale = (
        'Keep the grouping aligned with "app-related-photos-grid".'
    )
    architecture_invariant_rationale = (
        'Preserve "req-related-photos" and "app-related-photos-grid".'
    )
    architecture_guidance_rationale = (
        'Preserve "req-gallery-grid" and "app-related-photos-grid".'
    )
    implementation_functional_requirement = "Show related photos in the UI."
    implementation_responsive_requirement = (
        "Keep the layout responsive on mobile and desktop."
    )

    system_spec_yaml = yaml.safe_dump(
        {
            "schema": "https://powdrr.io/schemas/specification-v1",
            "id": "display-related-photos-system",
            "title": "Display related photos",
            "requirements": [
                {
                    "id": "req-related-photos",
                    "description": system_goal_description,
                    "state": "added",
                },
                {
                    "id": "req-gallery-grid",
                    "description": system_grid_description,
                    "state": "added",
                },
            ],
            "approach": [
                {
                    "id": "app-related-photos-grid",
                    "description": system_grid_approach_description,
                    "state": "added",
                },
                {
                    "id": "app-related-photos-empty",
                    "description": system_empty_state_description,
                    "state": "added",
                },
            ],
        },
        sort_keys=False,
    )
    architecture_spec_yaml = yaml.safe_dump(
        {
            "schema": "https://powdrr.io/schemas/specification-v1",
            "id": "2026-07-16-display-related-photos-architecture",
            "title": "Display related photos architecture",
            "entities": [
                {
                    "id": "related-photo",
                    "type": "photo",
                    "summary": "A photo related to the current feature.",
                    "rationale": architecture_related_photo_rationale,
                },
                {
                    "id": "gallery-photo",
                    "type": "photo",
                    "summary": "A photo shown in the feature gallery.",
                    "rationale": architecture_gallery_photo_rationale,
                },
            ],
            "entity_relationships": [
                {
                    "id": "related-photo-groups-with-gallery-photo",
                    "source": "related-photo",
                    "target": "gallery-photo",
                    "relationship": "groups_with",
                    "description": "Related photos are grouped in the gallery.",
                    "rationale": architecture_relationship_rationale,
                }
            ],
            "invariants": [
                {
                    "id": "related-photo-invariant",
                    "description": "Related photos stay within the gallery flow.",
                    "rationale": architecture_invariant_rationale,
                    "related": {
                        "entities": ["related-photo", "gallery-photo"],
                        "entity_relationships": [
                            "related-photo-groups-with-gallery-photo"
                        ],
                    },
                }
            ],
            "guidance": [
                {
                    "id": "related-photo-guidance",
                    "description": (
                        "Prefer the existing grid layout for related photos."
                    ),
                    "rationale": architecture_guidance_rationale,
                    "related": {
                        "entities": ["related-photo", "gallery-photo"],
                        "entity_relationships": [
                            "related-photo-groups-with-gallery-photo"
                        ],
                    },
                }
            ],
        },
        sort_keys=False,
    )
    implementation_spec_yaml = yaml.safe_dump(
        {
            "schema": "https://powdrr.io/schemas/specification-v1",
            "title": "Display related photos implementation",
            "architecture_id": "2026-07-16-display-related-photos-architecture",
            "entities": [
                {
                    "id": "related-photo",
                    "action": "added",
                    "rationale": "Add the related-photo entity from the architecture.",
                },
                {
                    "id": "gallery-photo",
                    "action": "added",
                    "rationale": "Add the gallery-photo entity from the architecture.",
                },
            ],
            "entity_relationships": [
                {
                    "id": "related-photo-groups-with-gallery-photo",
                    "action": "added",
                    "rationale": "Add the grouping relationship from the architecture.",
                }
            ],
            "features": [
                {
                    "id": "display-related-photos",
                    "action": "added",
                    "description": "Display related photos in the feature view.",
                    "functional_requirements": [
                        implementation_functional_requirement,
                        implementation_responsive_requirement,
                    ],
                }
            ],
            "decisions": [
                {
                    "id": "display-related-photos-grid",
                    "action": "added",
                    "description": "Reuse the existing photo grid layout.",
                }
            ],
        },
        sort_keys=False,
    )
    pr_spec_yaml = yaml.safe_dump(
        {
            "schema": "https://powdrr.io/schemas/specification-v1",
            "id": "pr-display-related-photos",
            "feature_ids": [
                "specify-system",
                "specify-architecture",
            ],
            "intent": {
                "problem": (
                    "Users need a structured workflow for specifying related photos."
                ),
                "goal": (
                    "Produce validated system, architecture, implementation, and PR "
                    "specifications."
                ),
                "reasoning": (
                    "The feature-specification flow should leave a durable record "
                    "for follow-up work."
                ),
            },
            "acceptance_criteria": [
                {
                    "id": "ac-display-related-photos",
                    "description": (
                        "The proposed PR captures the feature scope and validation "
                        "trail."
                    ),
                }
            ],
            "expected_tests": [
                {
                    "id": "test-display-related-photos",
                    "description": (
                        "The workflow produces a validated set of specification files."
                    ),
                }
            ],
            "required_test_cases": [
                {
                    "id": "rtc-display-related-photos",
                    "description": (
                        "Verify the workflow creates and validates the system, "
                        "architecture, implementation, and PR specs."
                    ),
                }
            ],
            "expected_outcomes": [
                {
                    "id": "outcome-display-related-photos",
                    "description": (
                        "The feature plan is ready for asynchronous "
                        "implementation work."
                    ),
                }
            ],
            "non_goals": [
                {
                    "id": "ng-display-related-photos",
                    "description": (
                        "Do not execute the async implementation work in this test."
                    ),
                }
            ],
            "risks": [
                {
                    "id": "risk-display-related-photos",
                    "description": (
                        "The current feature catalog may need refreshing if ids change."
                    ),
                }
            ],
        },
        sort_keys=False,
    )

    def _full_replace_edit(
        prompt: dict[str, object],
        *,
        yaml_text: str,
    ) -> dict[str, object]:
        current_file = cast(dict[str, object], prompt["current_file"])
        current_file_lines = cast(
            list[dict[str, object]],
            current_file.get("lines", []),
        )
        line_count = current_file.get("line_count")
        if isinstance(line_count, int) and line_count > 0:
            return {
                "kind": "edit",
                "file_path": current_file["path"],
                "edits": [
                    {
                        "kind": "replace",
                        "start_line": 1,
                        "end_line": line_count,
                        "text": yaml_text,
                    }
                ],
            }
        if current_file_lines:
            return {
                "kind": "edit",
                "file_path": current_file["path"],
                "edits": [
                    {
                        "kind": "replace",
                        "start_line": 1,
                        "end_line": len(current_file_lines),
                        "text": yaml_text,
                    }
                ],
            }
        return {
            "kind": "edit",
            "file_path": current_file["path"],
            "edits": [
                {
                    "kind": "add",
                    "start_line": 1,
                    "text": yaml_text,
                }
            ],
        }

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

    captured: dict[str, object] = {"messages": []}

    real_resolve_worktree_context = _resolve_worktree_context

    def _capture_worktree_context(
        repo_root_value: Path,
        *,
        stderr: TextIO,
        verbose: bool,
    ) -> Path:
        resolved = real_resolve_worktree_context(
            repo_root_value,
            stderr=stderr,
            verbose=verbose,
        )
        worktree_root_holder["path"] = resolved
        return resolved

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
                response: dict[str, object] = {
                    "selected_skill_path": str(skills_dir / "specify-a-feature.json"),
                    "selected_skill_reason": (
                        "The user wants a synchronous feature-specification flow."
                    ),
                    "next_question": None,
                    "ready_to_execute": True,
                }
            elif self._call_index == 1:
                self._assert_execution_prompt(
                    messages,
                    expected_step_index=0,
                    expected_step_description=step_descriptions[0],
                    expected_context_suffix=None,
                    expected_event_count=0,
                )
                response = {
                    "kind": "prompt_user",
                    "text": "What feature are you specifying?",
                    "decisions_and_context": (
                        "Need the feature goal and success criteria."
                    ),
                }
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
                response = {
                    "kind": "next_step",
                    "decisions_and_context": (
                        "Goal captured: display related photos; success criteria: "
                        "show related photos in the UI."
                    ),
                }
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
                response = {
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
                }
            elif self._call_index == 4:
                prompt = self._assert_execution_prompt(
                    messages,
                    expected_step_index=1,
                    expected_step_description=step_descriptions[1],
                    expected_context_suffix=(
                        "Start system spec generation for display-related-photos."
                    ),
                    expected_event_count=3,
                    expected_last_event_kind="invoke_tool",
                )
                current_file = cast(dict[str, object], prompt["current_file"])
                assert (
                    current_file["path"] == f"{system_spec_dir}/{system_spec_filename}"
                )
                response = _full_replace_edit(
                    prompt,
                    yaml_text=system_spec_yaml,
                )
                response["decisions_and_context"] = (
                    "System template filled with the captured goal and success criteria."
                )
            elif self._call_index == 5:
                self._assert_execution_prompt(
                    messages,
                    expected_step_index=1,
                    expected_step_description=step_descriptions[1],
                    expected_context_suffix=(
                        "System template filled with the captured goal and success criteria."
                    ),
                    expected_event_count=4,
                    expected_last_event_kind="edit",
                )
                response = {
                    "kind": "next_step",
                    "decisions_and_context": (
                        "System template filled; move to system review."
                    ),
                }
            elif self._call_index == 6:
                prompt = self._assert_execution_prompt(
                    messages,
                    expected_step_index=2,
                    expected_step_description=step_descriptions[2],
                    expected_context_suffix=(
                        "System template filled; move to system review."
                    ),
                    expected_event_count=5,
                    expected_last_event_kind="next_step",
                )
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
                response = {
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
                }
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
                response = {
                    "kind": "next_step",
                    "decisions_and_context": (
                        "System review complete: keep changes in the current worktree and use shell tools."
                    ),
                }
            elif self._call_index == 8:
                prompt = self._assert_execution_prompt(
                    messages,
                    expected_step_index=3,
                    expected_step_description=step_descriptions[3],
                    expected_context_suffix=(
                        "System review complete: keep changes in the current worktree and use shell tools."
                    ),
                    expected_event_count=7,
                    expected_last_event_kind="next_step",
                )
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
                response = {
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
                }
            elif self._call_index == 9:
                prompt = self._assert_execution_prompt(
                    messages,
                    expected_step_index=3,
                    expected_step_description=step_descriptions[3],
                    expected_context_suffix=(
                        "Start architecture spec generation for display-related-photos."
                    ),
                    expected_event_count=8,
                    expected_last_event_kind="invoke_tool",
                )
                current_file = cast(dict[str, object], prompt["current_file"])
                assert current_file["path"] == (
                    f"{system_spec_dir}/{architecture_spec_filename}"
                )
                assert (
                    cast(list[dict[str, object]], current_file["lines"])[0]["text"]
                    == "# Architecture specification template."
                )
                response = _full_replace_edit(
                    prompt,
                    yaml_text=architecture_spec_yaml,
                )
                response["decisions_and_context"] = (
                    "Architecture template filled with the chosen entity model and relationships."
                )
            elif self._call_index == 10:
                self._assert_execution_prompt(
                    messages,
                    expected_step_index=3,
                    expected_step_description=step_descriptions[3],
                    expected_context_suffix=(
                        "Architecture template filled with the chosen entity model and relationships."
                    ),
                    expected_event_count=9,
                    expected_last_event_kind="edit",
                )
                response = {
                    "kind": "next_step",
                    "decisions_and_context": (
                        "Architecture template filled; move to architecture review."
                    ),
                }
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
                response = {
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
                }
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
                response = {
                    "kind": "next_step",
                    "decisions_and_context": (
                        "Architecture review complete: align with existing entities and invariants."
                    ),
                }
            elif self._call_index == 13:
                prompt = self._assert_execution_prompt(
                    messages,
                    expected_step_index=5,
                    expected_step_description=step_descriptions[5],
                    expected_context_suffix=(
                        "Architecture review complete: align with existing entities and invariants."
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
                response = {
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
                }
            elif self._call_index == 14:
                prompt = self._assert_execution_prompt(
                    messages,
                    expected_step_index=5,
                    expected_step_description=step_descriptions[5],
                    expected_context_suffix=(
                        "Start implementation spec generation for display-related-photos."
                    ),
                    expected_event_count=13,
                    expected_last_event_kind="invoke_tool",
                )
                current_file = cast(dict[str, object], prompt["current_file"])
                assert current_file["path"] == (
                    f"{system_spec_dir}/{implementation_spec_filename}"
                )
                assert (
                    cast(list[dict[str, object]], current_file["lines"])[0]["text"]
                    == "# Implementation specification template."
                )
                response = _full_replace_edit(
                    prompt,
                    yaml_text=implementation_spec_yaml,
                )
                response["decisions_and_context"] = (
                    "Implementation template filled with the chosen layout and requirements."
                )
            elif self._call_index == 15:
                self._assert_execution_prompt(
                    messages,
                    expected_step_index=5,
                    expected_step_description=step_descriptions[5],
                    expected_context_suffix=(
                        "Implementation template filled with the chosen layout and requirements."
                    ),
                    expected_event_count=14,
                    expected_last_event_kind="edit",
                )
                response = {
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
                }
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
                response = {
                    "kind": "next_step",
                    "decisions_and_context": (
                        "Implementation step complete; use this spec for PR scope."
                    ),
                }
            elif self._call_index == 17:
                prompt = self._assert_execution_prompt(
                    messages,
                    expected_step_index=6,
                    expected_step_description=step_descriptions[6],
                    expected_context_suffix=(
                        "Implementation step complete; use this spec for PR scope."
                    ),
                    expected_event_count=16,
                    expected_last_event_kind="next_step",
                )
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
                response = {
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
                }
            elif self._call_index == 18:
                prompt = self._assert_execution_prompt(
                    messages,
                    expected_step_index=6,
                    expected_step_description=step_descriptions[6],
                    expected_context_suffix=(
                        "Start PR template generation for the feature."
                    ),
                    expected_event_count=17,
                    expected_last_event_kind="invoke_tool",
                )
                current_file = cast(dict[str, object], prompt["current_file"])
                assert current_file["path"] == (f"{system_spec_dir}/{pr_spec_filename}")
                assert (
                    cast(list[dict[str, object]], current_file["lines"])[0]["text"]
                    == "# PR specification template."
                )
                response = _full_replace_edit(
                    prompt,
                    yaml_text=pr_spec_yaml,
                )
                response["decisions_and_context"] = (
                    "PR template filled with acceptance criteria and risks."
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
                    expected_last_event_kind="edit",
                )
                response = {
                    "kind": "next_step",
                    "decisions_and_context": "PR step complete; handoff is ready.",
                }
            elif self._call_index == 20:
                self._assert_execution_prompt(
                    messages,
                    expected_step_index=7,
                    expected_step_description=step_descriptions[7],
                    expected_context_suffix="PR step complete; handoff is ready.",
                    expected_event_count=19,
                    expected_last_event_kind="next_step",
                )
                response = {
                    "kind": "prompt_user",
                    "text": "Please review the draft result.",
                    "decisions_and_context": "Ask the user to review the draft.",
                }
            elif self._call_index == 21:
                self._assert_execution_prompt(
                    messages,
                    expected_step_index=7,
                    expected_step_description=step_descriptions[7],
                    expected_context_suffix="Ask the user to review the draft.",
                    expected_event_count=20,
                    expected_last_event_kind="prompt_user",
                )
                response = {
                    "kind": "complete",
                    "text": "Feature specification complete.",
                    "decisions_and_context": "User review requested.",
                }
            else:
                raise AssertionError(f"Unexpected LLM call index: {self._call_index}")

            self._call_index += 1
            return response

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("UV_CACHE_DIR", "/private/tmp/uv-cache-4")
    monkeypatch.setattr(
        "powdrr_lift.workflow_chat_agent.OpenAIChatClient",
        _FakeOpenAIClient,
    )
    monkeypatch.setattr(
        "powdrr_lift.workflow_chat_agent._resolve_worktree_context",
        _capture_worktree_context,
    )

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

    assert exit_code == 0
    assert "path" in worktree_root_holder
    worktree_root = worktree_root_holder["path"]
    summary_path = worktree_root / "generated" / "skill-execution.json"
    system_path = worktree_root / system_spec_dir / system_spec_filename
    architecture_path = worktree_root / system_spec_dir / architecture_spec_filename
    implementation_path = worktree_root / system_spec_dir / implementation_spec_filename
    pr_path = worktree_root / system_spec_dir / pr_spec_filename

    assert summary_path.exists()
    assert system_path.exists()
    assert architecture_path.exists()
    assert implementation_path.exists()
    assert pr_path.exists()

    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert summary["selected_skill_name"] == "specify-a-feature"
    assert [event["kind"] for event in summary["execution_events"]] == [
        "prompt_user",
        "next_step",
        "invoke_tool",
        "edit",
        "next_step",
        "invoke_tool",
        "next_step",
        "invoke_tool",
        "edit",
        "next_step",
        "invoke_tool",
        "next_step",
        "invoke_tool",
        "edit",
        "invoke_tool",
        "next_step",
        "invoke_tool",
        "edit",
        "next_step",
        "prompt_user",
        "complete",
    ]

    system_report = yaml.safe_load(
        validate_system_specification_yaml(
            system_path.read_text(encoding="utf-8"),
            work_item_name="display-related-photos",
            repo_root=worktree_root,
        )
    )
    architecture_report = yaml.safe_load(
        validate_architecture_specification_yaml(
            architecture_path.read_text(encoding="utf-8"),
            entity_types=["photo"],
            work_item_name="display-related-photos",
            repo_root=worktree_root,
        )
    )
    implementation_report = yaml.safe_load(
        validate_implementation_specification_yaml(
            implementation_path.read_text(encoding="utf-8"),
            work_item_name="display-related-photos",
            architecture_specification_path=architecture_path,
            repo_root=worktree_root,
        )
    )
    pr_report = yaml.safe_load(
        validate_pr_specification_yaml(
            pr_path.read_text(encoding="utf-8"),
            work_item_name="display-related-photos",
            repo_root=repo_root,
        )
    )

    assert system_report["validation_successful"] is True
    assert architecture_report["validation_successful"] is True
    assert implementation_report["validation_successful"] is True
    assert pr_report["validation_successful"] is True
    assert "Wrote skill execution summary to" in stdout.getvalue()
    assert "Please review the draft result." in stdout.getvalue()


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
                "system-specification",
                "--work-item-name",
                "<work-item-name>",
            ],
        },
        {
            "tool": "shell",
            "command": [
                "powdrr-lift",
                "evaluate-system-specification",
                "--work-item-name",
                "<work-item-name>",
            ],
        },
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
