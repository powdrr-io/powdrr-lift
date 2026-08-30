from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path
from threading import Thread
from typing import Any
from unittest.mock import patch

import pytest
from test_workflow_chat_agent import SkillChatConfig, _build_skill
from textual.containers import ScrollableContainer
from textual.events import Key
from textual.widgets import Label, ListItem, ListView, Static, TextArea

from powdrr_lift.core import Skill, SkillStep, load_skill
from powdrr_lift.workflow_chat_agent import SkillCatalogEntry
from powdrr_lift.workflow_chat_tui import (
    WorkflowChatApp,
    _TextualStdoutOutput,
    _visible_step_indices,
)


def test_textual_response_grows_and_submits_on_return(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    received: list[str] = []

    def fake_run_workflow_chat(config: Any, **kwargs: Any) -> int:
        kwargs["stdout"].write("What do you want to do? ")
        kwargs["stdout"].flush()
        received.append(kwargs["input_func"]())
        if len(received) == 1:
            skill_path = Path("skill-definitions/bootstrap-code-structure.yaml")
            skill = SkillCatalogEntry(skill_path, load_skill(skill_path))
            kwargs["progress_callback"](
                skill,
                len(skill.skill.steps),
                "bootstrap-code-structure skill completed",
                None,
                None,
            )
        return 0 if len(received) == 1 else 1

    monkeypatch.setattr(
        "powdrr_lift.workflow_chat_tui.run_workflow_chat",
        fake_run_workflow_chat,
    )

    async def exercise() -> int:
        app = WorkflowChatApp(SkillChatConfig(skills_dir=Path("skill-definitions")))
        async with app.run_test() as pilot:
            await pilot.pause()
            assert "What do you want to do?" in str(
                app.query_one("#status", Static).render()
            )
            response = app.query_one("#response", TextArea)
            response.text = "line one\nline two"
            await pilot.pause()
            height_style = response.styles.height
            assert height_style is not None
            height = int(height_style.value)
            await pilot.press("enter")
            for _ in range(20):
                await pilot.pause(0.05)
                if received:
                    break
            assert "skill completed" in str(app.query_one("#status", Static).render())
            response.text = "follow-up request"
            await pilot.press("enter")
            await pilot.pause(0.1)
            return height

    height = asyncio.run(exercise())

    assert height >= 4
    assert received == ["line one\nline two", "follow-up request"]


def test_textual_response_grows_for_trailing_newline() -> None:
    async def exercise() -> tuple[int, float]:
        app = WorkflowChatApp(SkillChatConfig(skills_dir=Path("skill-definitions")))
        app._stop_requested.set()
        async with app.run_test() as pilot:
            response = app.query_one("#response", TextArea)
            response.text = "line one\n"
            await pilot.pause()
            height_style = response.styles.height
            assert height_style is not None
            return int(height_style.value), response.scroll_y

    height, scroll_y = asyncio.run(exercise())
    assert height >= 4
    assert scroll_y == 0


def test_textual_response_grows_for_wrapped_text() -> None:
    async def exercise() -> tuple[int, float]:
        app = WorkflowChatApp(SkillChatConfig(skills_dir=Path("skill-definitions")))
        app._stop_requested.set()
        async with app.run_test() as pilot:
            response = app.query_one("#response", TextArea)
            response.text = "x" * 200
            await pilot.pause()
            height_style = response.styles.height
            assert height_style is not None
            height = int(height_style.value)
            return height, response.scroll_y

    height, scroll_y = asyncio.run(exercise())
    assert height > 3
    assert scroll_y == 0


def test_textual_response_grows_beyond_previous_thirty_row_cap() -> None:
    async def exercise() -> tuple[int, float]:
        app = WorkflowChatApp(SkillChatConfig(skills_dir=Path("skill-definitions")))
        app._stop_requested.set()
        async with app.run_test(size=(80, 50)) as pilot:
            response = app.query_one("#response", TextArea)
            response.text = "\n".join(f"line {number}" for number in range(35))
            await pilot.pause()
            height_style = response.styles.height
            assert height_style is not None
            return int(height_style.value), response.scroll_y

    height, scroll_y = asyncio.run(exercise())
    assert height >= 37
    assert scroll_y == 0


def test_textual_status_textarea_does_not_reserve_hidden_label_row() -> None:
    async def exercise() -> tuple[int, int]:
        app = WorkflowChatApp(SkillChatConfig(skills_dir=Path("skill-definitions")))
        app._stop_requested.set()
        async with app.run_test() as pilot:
            await pilot.pause()
            status_container = app.query_one("#status-container", ScrollableContainer)
            status = app.query_one("#status-text", TextArea)
            return status.region.y, status_container.region.y

    status_y, container_y = asyncio.run(exercise())
    assert status_y == container_y + 1


def test_textual_startup_shows_initial_question(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run_workflow_chat(config: Any, **kwargs: Any) -> int:
        kwargs["stdout"].write("What do you want to do? ")
        kwargs["stdout"].flush()
        kwargs["input_func"]()
        return 1

    monkeypatch.setattr(
        "powdrr_lift.workflow_chat_tui.run_workflow_chat",
        fake_run_workflow_chat,
    )

    async def exercise() -> tuple[str, bool]:
        app = WorkflowChatApp(SkillChatConfig(skills_dir=Path("skill-definitions")))
        async with app.run_test() as pilot:
            await pilot.pause()
            response = app.query_one("#response", TextArea)
            return str(app.query_one("#status", Static).render()), response.disabled

    assert asyncio.run(exercise()) == (
        "Powdrr Agent v0.0.1\n\nWhat do you want to do?",
        False,
    )


def test_textual_quit_unblocks_workflow_input() -> None:
    app = WorkflowChatApp(SkillChatConfig(skills_dir=Path("skill-definitions")))

    app.action_quit_workflow()

    assert app._answers.get_nowait() == ""


def test_textual_input_marker_preserves_follow_up_question() -> None:
    async def exercise() -> str:
        app = WorkflowChatApp(SkillChatConfig(skills_dir=Path("skill-definitions")))
        app._stop_requested.set()
        app._workflow_active = True
        async with app.run_test() as pilot:
            app._set_message("Which requirements should this feature satisfy?")
            app._show_prompt("> ")
            await pilot.pause()
            return str(app.query_one("#status", Static).render())

    assert asyncio.run(exercise()) == (
        "Powdrr Agent v0.0.1\n\nWhich requirements should this feature satisfy?"
    )


def test_textual_submit_shows_user_response_before_calling_llm() -> None:
    async def exercise() -> str:
        app = WorkflowChatApp(SkillChatConfig(skills_dir=Path("skill-definitions")))
        app._stop_requested.set()
        async with app.run_test() as pilot:
            response = app.query_one("#response", TextArea)
            response.text = "Build the feature"
            app._submit_response()
            await pilot.pause()
            return str(app.query_one("#status", Static).render())

    assert asyncio.run(exercise()) == (
        "Powdrr Agent v0.0.1\n\n"
        "----------------------------------------\n"
        "> Build the feature\n"
        "----------------------------------------\n\n"
        "calling LLM..."
    )


def test_textual_submit_ignores_empty_response() -> None:
    async def exercise() -> tuple[str, str, bool, bool]:
        app = WorkflowChatApp(SkillChatConfig(skills_dir=Path("skill-definitions")))
        app._stop_requested.set()
        async with app.run_test() as pilot:
            response = app.query_one("#response", TextArea)
            response.text = "  \n  "
            app._submit_response()
            await pilot.pause()
            return (
                str(app.query_one("#status", Static).render()),
                response.text,
                response.disabled,
                app._request_submitted.is_set(),
            )

    assert asyncio.run(exercise()) == (
        "Powdrr Agent v0.0.1",
        "  \n  ",
        False,
        False,
    )


def test_textual_submit_retains_initial_prompt_and_echoes_user_response() -> None:
    async def exercise() -> str:
        app = WorkflowChatApp(SkillChatConfig(skills_dir=Path("skill-definitions")))
        app._stop_requested.set()
        app._workflow_active = True
        async with app.run_test() as pilot:
            app._show_initial_prompt("What do you want to do?")
            response = app.query_one("#response", TextArea)
            response.text = "Build the feature"
            app._submit_response()
            await pilot.pause()
            return str(app.query_one("#status", Static).render())

    assert asyncio.run(exercise()) == (
        "Powdrr Agent v0.0.1\n\n"
        "What do you want to do?\n\n"
        "----------------------------------------\n"
        "> Build the feature\n"
        "----------------------------------------\n\n"
        "calling LLM..."
    )


def test_textual_status_retains_multiple_user_responses() -> None:
    async def exercise() -> str:
        app = WorkflowChatApp(SkillChatConfig(skills_dir=Path("skill-definitions")))
        app._stop_requested.set()
        app._workflow_active = True
        async with app.run_test() as pilot:
            app._show_initial_prompt("What do you want to do?")
            response = app.query_one("#response", TextArea)
            response.text = "First answer"
            app._submit_response()
            await pilot.pause()
            response.disabled = False
            response.text = "Second answer"
            app._submit_response()
            await pilot.pause()
            return str(app.query_one("#status", Static).render())

    assert asyncio.run(exercise()) == (
        "Powdrr Agent v0.0.1\n\n"
        "What do you want to do?\n\n"
        "----------------------------------------\n"
        "> First answer\n"
        "----------------------------------------\n\n"
        "calling LLM...\n\n"
        "----------------------------------------\n"
        "> Second answer\n"
        "----------------------------------------\n\n"
        "calling LLM..."
    )


def test_textual_status_is_visible_and_not_collapsed() -> None:
    async def exercise() -> tuple[str, int, int]:
        app = WorkflowChatApp(SkillChatConfig(skills_dir=Path("skill-definitions")))
        app._stop_requested.set()
        async with app.run_test() as pilot:
            status = app.query_one("#status", Static)
            status_container = app.query_one("#status-container")
            await pilot.pause()
            app._set_status("x" * 200)
            await pilot.pause()
            return (
                str(status.render()),
                status_container.region.height,
                status_container.region.width,
            )

    rendered, height, width = asyncio.run(exercise())
    assert rendered.endswith("x" * 200)
    assert height > 4
    assert width == 80


def test_textual_panels_have_the_same_width() -> None:
    async def exercise() -> tuple[int, int, int]:
        app = WorkflowChatApp(SkillChatConfig(skills_dir=Path("skill-definitions")))
        app._stop_requested.set()
        async with app.run_test() as pilot:
            await pilot.pause()
            return (
                app.query_one("#steps", ListView).region.width,
                app.query_one("#status-container").region.width,
                app.query_one("#response", TextArea).region.width,
            )

    assert asyncio.run(exercise()) == (0, 80, 80)


def test_textual_files_panel_preserves_add_order_without_duplicates() -> None:
    async def exercise() -> list[str]:
        app = WorkflowChatApp(SkillChatConfig(skills_dir=Path("skill-definitions")))
        app._stop_requested.set()
        async with app.run_test() as pilot:
            app._record_added_files(("docs/first.yaml", "src/first.py"))
            app._record_added_files(("docs/first.yaml", "tests/first.py"))
            app._record_added_files(("./docs/first.yaml",))
            await pilot.pause()
            return [
                str(label.render())
                for label in app.query_one("#files", ListView).query(Label)
            ]

    assert asyncio.run(exercise()) == [
        "docs/first.yaml",
        "src/first.py",
        "tests/first.py",
    ]


def test_textual_files_panel_limits_retained_history() -> None:
    async def exercise() -> list[str]:
        app = WorkflowChatApp(SkillChatConfig(skills_dir=Path("skill-definitions")))
        app._stop_requested.set()
        async with app.run_test() as pilot:
            app._record_added_files(tuple(f"file-{index}.py" for index in range(81)))
            await pilot.pause()
            return [
                str(label.render())
                for label in app.query_one("#files", ListView).query(Label)
            ]

    files = asyncio.run(exercise())
    assert len(files) == 80
    assert files[0] == "file-1.py"
    assert files[-1] == "file-80.py"


def test_textual_orange_panels_share_the_width() -> None:
    async def exercise() -> tuple[int, int, int]:
        app = WorkflowChatApp(SkillChatConfig(skills_dir=Path("skill-definitions")))
        app._stop_requested.set()
        skill = SkillCatalogEntry(Path("skill.yaml"), _build_skill())
        async with app.run_test() as pilot:
            app._apply_progress(skill, current_step_index=0, status="running")
            await pilot.pause()
            return (
                app.query_one("#steps", ListView).region.width,
                app.query_one("#files", ListView).region.width,
                app.query_one("#workflow-panels").region.height,
            )

    assert asyncio.run(exercise()) == (40, 40, 12)


def test_textual_panels_place_green_output_above_orange_steps() -> None:
    async def exercise() -> tuple[int, int, int, int]:
        app = WorkflowChatApp(SkillChatConfig(skills_dir=Path("skill-definitions")))
        app._stop_requested.set()
        async with app.run_test() as pilot:
            await pilot.pause()
            skill = SkillCatalogEntry(Path("skill.yaml"), _build_skill())
            steps = app.query_one("#steps", ListView)
            status = app.query_one("#status", Static)
            empty_height = steps.region.height
            app._apply_progress(skill, current_step_index=0, status="running")
            await pilot.pause()
            return (
                status.region.y,
                steps.region.y,
                empty_height,
                steps.region.height,
            )

    status_y, steps_y, empty_height, populated_height = asyncio.run(exercise())
    assert status_y < steps_y
    assert empty_height == 0
    assert populated_height > 0


def test_visible_step_window_is_limited_to_ten_steps() -> None:
    assert _visible_step_indices(10, 5) == tuple(range(10))
    assert _visible_step_indices(20, 5) == (4, 5, 6, 7, 8, 9, 10, 11, 12, 19)
    assert _visible_step_indices(20, 0) == (0, 1, 2, 3, 4, 5, 6, 7, 19)
    assert _visible_step_indices(20, 19) == (18, 19)


def test_textual_long_step_list_shows_requested_window() -> None:
    async def exercise() -> tuple[list[str], float]:
        app = WorkflowChatApp(SkillChatConfig(skills_dir=Path("skill-definitions")))
        app._stop_requested.set()
        skill = SkillCatalogEntry(
            Path("long-skill.yaml"),
            Skill(
                name="long-skill",
                when_to_use=(),
                steps=tuple(
                    SkillStep(description=f"Step {index}") for index in range(1, 21)
                ),
            ),
        )
        async with app.run_test() as pilot:
            app._apply_progress(skill, current_step_index=5, status="running")
            await pilot.pause()
            steps = app.query_one("#steps", ListView)
            return (
                [str(label.render()) for label in steps.query(Label)],
                steps.scroll_y,
            )

    labels, scroll_y = asyncio.run(exercise())
    assert labels == [
        "5. Step 5",
        "6. Step 6",
        "7. Step 7",
        "8. Step 8",
        "9. Step 9",
        "10. Step 10",
        "11. Step 11",
        "12. Step 12",
        "13. Step 13",
        "20. Step 20",
    ]
    assert scroll_y == 0


def test_textual_nested_steps_fit_without_scrolling() -> None:
    async def exercise() -> tuple[int, float]:
        app = WorkflowChatApp(SkillChatConfig(skills_dir=Path("skill-definitions")))
        app._stop_requested.set()
        parent = SkillCatalogEntry(Path("parent.yaml"), _build_skill())
        nested = SkillCatalogEntry(Path("nested.yaml"), _build_skill())
        async with app.run_test() as pilot:
            app._apply_progress(
                nested,
                current_step_index=0,
                status="running",
                parent_skill=parent,
                parent_step_index=0,
            )
            await pilot.pause()
            steps = app.query_one("#steps", ListView)
            return steps.region.height, steps.scroll_y

    height, scroll_y = asyncio.run(exercise())
    assert height == 12
    assert scroll_y == 0


def test_textual_completed_skill_removes_orange_step_list() -> None:
    async def exercise() -> tuple[int, int, str]:
        app = WorkflowChatApp(SkillChatConfig(skills_dir=Path("skill-definitions")))
        app._stop_requested.set()
        async with app.run_test() as pilot:
            skill = SkillCatalogEntry(Path("skill.yaml"), _build_skill())
            steps = app.query_one("#steps", ListView)
            app._apply_progress(skill, current_step_index=0, status="running")
            await pilot.pause()
            app._apply_progress(
                skill,
                current_step_index=len(skill.skill.steps),
                status="skill complete",
            )
            await pilot.pause()
            return (
                len(steps.children),
                steps.region.height,
                str(app.query_one("#status", Static).render()),
            )

    child_count, height, status = asyncio.run(exercise())
    assert child_count == 0
    assert height == 0
    assert status.endswith("skill complete")


def test_textual_nested_progress_shows_parent_separator_and_nested_steps() -> None:
    async def exercise() -> list[str]:
        app = WorkflowChatApp(SkillChatConfig(skills_dir=Path("skill-definitions")))
        app._stop_requested.set()
        async with app.run_test() as pilot:
            parent_skill = SkillCatalogEntry(Path("parent.yaml"), _build_skill())
            nested_skill = SkillCatalogEntry(
                Path("nested.yaml"),
                Skill(
                    name="nested",
                    when_to_use=("Run nested work.",),
                    steps=(SkillStep(description="Nested step."),),
                ),
            )
            app._apply_progress(
                nested_skill,
                current_step_index=0,
                status="running nested skill",
                parent_skill=parent_skill,
                parent_step_index=0,
            )
            await pilot.pause()
            steps = app.query_one("#steps", ListView)
            border_title = steps.border_title
            assert border_title is not None
            return [str(label.render()) for label in steps.query(Label)] + [
                border_title
            ]

    assert asyncio.run(exercise()) == [
        "1. Capture the feature goal.",
        "-------",
        "1. Nested step.",
        "specify-a-feature > nested",
    ]


def test_textual_parent_progress_is_colored_after_nested_completion() -> None:
    async def exercise() -> list[tuple[str, bool, bool]]:
        app = WorkflowChatApp(SkillChatConfig(skills_dir=Path("skill-definitions")))
        app._stop_requested.set()
        async with app.run_test() as pilot:
            parent_skill = SkillCatalogEntry(Path("parent.yaml"), _build_skill())
            nested_skill = SkillCatalogEntry(
                Path("nested.yaml"),
                Skill(
                    name="nested",
                    when_to_use=("Run nested work.",),
                    steps=(SkillStep(description="Nested step."),),
                ),
            )
            app._apply_progress(
                nested_skill,
                current_step_index=0,
                status="running nested skill",
                parent_skill=parent_skill,
                parent_step_index=0,
            )
            app._apply_progress(parent_skill, current_step_index=1, status="resuming")
            await pilot.pause()
            return [
                (
                    str(item.query_one(Label).render()),
                    item.has_class("completed"),
                    item.has_class("current"),
                )
                for item in app.query_one("#steps", ListView).query(ListItem)
            ]

    assert asyncio.run(exercise()) == [
        ("1. Capture the feature goal.", True, False),
        ("2. Summarize the result.", False, True),
    ]


def test_textual_skill_path_tracks_deep_nesting_and_parent_resume() -> None:
    app = WorkflowChatApp(SkillChatConfig(skills_dir=Path("skill-definitions")))
    parent = SkillCatalogEntry(Path("parent.yaml"), _build_skill())
    child = SkillCatalogEntry(
        Path("child.yaml"),
        Skill(
            name="child",
            when_to_use=("Run child work.",),
            steps=(SkillStep(description="Child step."),),
        ),
    )
    grandchild = SkillCatalogEntry(
        Path("grandchild.yaml"),
        Skill(
            name="grandchild",
            when_to_use=("Run grandchild work.",),
            steps=(SkillStep(description="Grandchild step."),),
        ),
    )

    assert app._resolve_skill_path(parent, None) == ("specify-a-feature",)
    assert app._resolve_skill_path(child, parent) == (
        "specify-a-feature",
        "child",
    )
    assert app._resolve_skill_path(grandchild, child) == (
        "specify-a-feature",
        "child",
        "grandchild",
    )
    assert app._resolve_skill_path(child, parent) == (
        "specify-a-feature",
        "child",
    )


def test_textual_status_scrolls_to_new_output_and_retains_history() -> None:
    async def exercise() -> tuple[str, float, int, int]:
        app = WorkflowChatApp(SkillChatConfig(skills_dir=Path("skill-definitions")))
        app._stop_requested.set()
        async with app.run_test(size=(80, 12)) as pilot:
            status = app.query_one("#status", Static)
            for number in range(20):
                app._set_message(f"output-{number}")
            await pilot.pause()
            return (
                str(status.render()),
                app.query_one("#status-container").scroll_y,
                app.query_one("#status-container").region.height,
                app.query_one("#status-container").virtual_size.height,
            )

    rendered, scroll_y, region_height, virtual_height = asyncio.run(exercise())
    assert "output-19" in rendered
    assert virtual_height > region_height
    assert scroll_y > 0


def test_textual_status_history_is_bounded() -> None:
    async def exercise() -> tuple[int, int, str]:
        app = WorkflowChatApp(SkillChatConfig(skills_dir=Path("skill-definitions")))
        app._stop_requested.set()
        async with app.run_test() as pilot:
            for number in range(200):
                app._set_message(f"output-{number}-" + ("x" * 500))
            await pilot.pause()
            return (
                len(app._message_history),
                app._message_history_chars,
                str(app.query_one("#status", Static).render()),
            )

    history_count, history_chars, rendered = asyncio.run(exercise())
    assert history_count <= 80
    assert history_chars <= 24_000
    assert "output-199" in rendered
    assert "output-0" not in rendered


def test_textual_status_truncates_large_messages() -> None:
    async def exercise() -> str:
        app = WorkflowChatApp(SkillChatConfig(skills_dir=Path("skill-definitions")))
        app._stop_requested.set()
        async with app.run_test() as pilot:
            app._set_message("start-" + ("x" * 20_000) + "-end")
            await pilot.pause()
            return str(app.query_one("#status", Static).render())

    rendered = asyncio.run(exercise())
    assert "status message truncated" in rendered
    assert len(rendered) < 9_000


def test_textual_failure_retains_diagnostics_instead_of_unknown_error(
    tmp_path: Path,
) -> None:
    async def exercise(repo_root: Path) -> str:
        app = WorkflowChatApp(
            SkillChatConfig(skills_dir=Path("skill-definitions"), repo_root=repo_root)
        )
        app._stop_requested.set()
        async with app.run_test() as pilot:
            app._record_output("stderr", "yaml.parser.ParserError: invalid syntax")
            app._record_output("stderr", "  line 12, column 7")
            app._record_output("progress", "Editing project-structure.yaml")
            app._exit_code = 1
            app._finish()
            await pilot.pause()
            return str(app.query_one("#status", Static).render())

    rendered = asyncio.run(exercise(tmp_path))
    assert "unknown error" not in rendered
    assert "workflow exited with status 1" in rendered
    assert "yaml.parser.ParserError: invalid syntax" in rendered
    assert "line 12, column 7" in rendered
    assert "Editing project-structure.yaml" in rendered
    assert "Press Ctrl+Q to exit." in rendered
    assert "Press Ctrl+C to exit." not in rendered
    error_log = (tmp_path / "agent_error.txt").read_text(encoding="utf-8")
    assert "Workflow error:" in error_log
    assert "Editing project-structure.yaml" in error_log


def test_textual_status_shows_latest_output() -> None:
    async def exercise() -> str:
        app = WorkflowChatApp(SkillChatConfig(skills_dir=Path("skill-definitions")))
        app._stop_requested.set()
        async with app.run_test() as pilot:
            for message in ("first", "second", "third", "fourth"):
                app._set_message(message)
            await pilot.pause()
            return str(app.query_one("#status", Static).render())

    assert asyncio.run(exercise()) == (
        "Powdrr Agent v0.0.1\n\nfirst\n\nsecond\n\nthird\n\nfourth"
    )


def test_textual_status_surfaces_provider_wait_after_local_tool() -> None:
    async def exercise() -> str:
        app = WorkflowChatApp(SkillChatConfig(skills_dir=Path("skill-definitions")))
        app._stop_requested.set()
        async with app.run_test() as pilot:
            app._set_status("calling LLM...")
            writer = Thread(
                target=app._output_line,
                args=("stderr", "waiting for test-model LLM response..."),
            )
            writer.start()
            await pilot.pause()
            writer.join()
            return str(app.query_one("#status", Static).render())

    assert asyncio.run(exercise()) == (
        "Powdrr Agent v0.0.1\n\n"
        "calling LLM...\n\nwaiting for test-model LLM response..."
    )


def test_textual_status_retains_full_empty_response_prompt() -> None:
    async def exercise() -> str:
        app = WorkflowChatApp(SkillChatConfig(skills_dir=Path("skill-definitions")))
        app._stop_requested.set()
        app._workflow_active = True
        exchange = (
            "[workflow] Empty-response exchange: "
            'prompt=[{"role":"user","content":"Proceed with instantiating '
            'the workflow?"}] '
            "response=<empty>"
        )

        def write_output() -> None:
            app._output_line("stderr", exchange)
            app._output_line("stderr", "[workflow] calling LLM...")

        async with app.run_test() as pilot:
            writer = Thread(target=write_output)
            writer.start()
            await pilot.pause()
            writer.join()
            await pilot.pause()
            return str(app.query_one("#status", Static).render())

    rendered = asyncio.run(exercise())
    assert "Empty-response exchange: prompt=" in rendered
    assert '"content":"Proceed with instantiating the workflow?"' in rendered
    assert "response=<empty>" in rendered
    assert "calling LLM..." in rendered


def test_textual_status_keeps_all_questions_visible() -> None:
    async def exercise() -> tuple[str, int]:
        app = WorkflowChatApp(SkillChatConfig(skills_dir=Path("skill-definitions")))
        app._stop_requested.set()
        async with app.run_test() as pilot:
            status = app.query_one("#status-text", TextArea)
            for question in (
                "1. What should be logged?",
                "2. Which file format should be used?",
                "3. Which platforms should be supported?",
                "4. What should be redacted?",
                "5. What performance constraints apply?",
            ):
                app._set_message(question)
            await pilot.pause()
            return status.text, status.region.height

    rendered, height = asyncio.run(exercise())
    assert all(f"{number}." in rendered for number in range(1, 6))
    assert height > 4


def test_textual_output_hides_debug_and_promotes_question() -> None:
    async def exercise() -> tuple[str, str]:
        app = WorkflowChatApp(SkillChatConfig(skills_dir=Path("skill-definitions")))
        app._stop_requested.set()
        app._workflow_active = True
        async with app.run_test() as pilot:
            output = _TextualStdoutOutput(app)

            def write_output() -> None:
                output.write("Matched skill: internal-debug\n")
                output.write("Which requirements should this feature satisfy?\n")
                output.write("> ")

            writer = Thread(target=write_output)
            writer.start()
            await pilot.pause()
            writer.join()
            status = app.query_one("#status", Static)
            return str(status.render()), str(status.render())

    assert asyncio.run(exercise()) == (
        "Powdrr Agent v0.0.1\n\n"
        "Matched skill: internal-debug\n\nWhich requirements should this "
        "feature satisfy?",
        "Powdrr Agent v0.0.1\n\n"
        "Matched skill: internal-debug\n\nWhich requirements should this "
        "feature satisfy?",
    )


def test_textual_output_keeps_multiline_question_complete() -> None:
    async def exercise() -> str:
        app = WorkflowChatApp(SkillChatConfig(skills_dir=Path("skill-definitions")))
        app._stop_requested.set()
        app._workflow_active = True
        async with app.run_test() as pilot:
            output = _TextualStdoutOutput(app)

            def write_output() -> None:
                output.write("What do you want to do? ")
                output.flush()
                output.write("Matched skill: specify-a-feature\n")
                output.write(
                    "1. What is the feature goal?\n"
                    "2. Which requirements matter?\n"
                    "Please answer whichever of these you can.\n"
                )
                output.flush()
                output.write("Matched skill: next-skill\n")
                output.write("Next question?\n")
                output.flush()

            writer = Thread(target=write_output)
            writer.start()
            await pilot.pause()
            writer.join()
            return str(app.query_one("#status", Static).render())

    assert asyncio.run(exercise()) == (
        "Powdrr Agent v0.0.1\n\n"
        "What do you want to do?\n\n"
        "Matched skill: specify-a-feature\n\n"
        "1. What is the feature goal?\n"
        "2. Which requirements matter?\n"
        "Please answer whichever of these you can.\n\n"
        "Matched skill: next-skill\n\n"
        "Next question?"
    )


def test_textual_output_buffers_partial_writes_until_the_line_is_complete() -> None:
    async def exercise() -> str:
        app = WorkflowChatApp(SkillChatConfig(skills_dir=Path("skill-definitions")))
        app._stop_requested.set()
        app._workflow_active = True
        async with app.run_test() as pilot:
            output = _TextualStdoutOutput(app)

            def write_output() -> None:
                output.write("ok workf")
                output.write("low chat\n")
                output.flush()

            writer = Thread(target=write_output)
            writer.start()
            await pilot.pause()
            writer.join()
            return str(app.query_one("#status", Static).render())

    assert asyncio.run(exercise()) == ("Powdrr Agent v0.0.1\n\nok workflow chat")


def test_textual_execution_transition_retains_output_history() -> None:
    async def exercise() -> str:
        app = WorkflowChatApp(SkillChatConfig(skills_dir=Path("skill-definitions")))
        app._stop_requested.set()
        app._workflow_active = True
        async with app.run_test() as pilot:
            skill = SkillCatalogEntry(Path("skill.yaml"), _build_skill())
            app._set_message("Matched skill: specify-a-feature")
            app._apply_progress(
                skill,
                current_step_index=0,
                status="waiting on LLM response...",
            )
            app._show_prompt("What is the feature goal?")
            await pilot.pause()
            return str(app.query_one("#status", Static).render())

    rendered = asyncio.run(exercise())
    assert rendered == (
        "Powdrr Agent v0.0.1\n\n"
        "Matched skill: specify-a-feature\n\n"
        "waiting on LLM response...\n\n"
        "What is the feature goal?"
    )


def test_textual_status_surfaces_provider_warning() -> None:
    async def exercise() -> str:
        app = WorkflowChatApp(SkillChatConfig(skills_dir=Path("skill-definitions")))
        app._stop_requested.set()
        async with app.run_test() as pilot:
            writer = Thread(
                target=app._output_line,
                args=("stderr", "WARNING: reviews might be limited"),
            )
            writer.start()
            await pilot.pause()
            writer.join()
            return str(app.query_one("#status", Static).render())

    assert asyncio.run(exercise()) == (
        "Powdrr Agent v0.0.1\n\nWARNING: reviews might be limited"
    )


def test_textual_empty_human_prompt_replaces_llm_wait_status_with_warning() -> None:
    async def exercise() -> str:
        app = WorkflowChatApp(SkillChatConfig(skills_dir=Path("skill-definitions")))
        app._stop_requested.set()
        app._workflow_active = True
        async with app.run_test() as pilot:
            app._set_status("waiting for model LLM response...")
            app._show_prompt("")
            await pilot.pause()
            return str(app.query_one("#status", Static).render())

    assert asyncio.run(exercise()) == (
        "Powdrr Agent v0.0.1\n\n"
        "waiting for model LLM response...\n\n"
        "WARNING: received empty response but need human input"
    )


def test_textual_bare_prompt_marker_does_not_create_empty_response_warning() -> None:
    async def exercise() -> str:
        app = WorkflowChatApp(SkillChatConfig(skills_dir=Path("skill-definitions")))
        app._stop_requested.set()
        app._workflow_active = True
        async with app.run_test() as pilot:
            app._set_status("waiting for model LLM response...")
            output = _TextualStdoutOutput(app)
            writer = Thread(target=output.write, args=("> ",))
            writer.start()
            await pilot.pause()
            writer.join()
            return str(app.query_one("#status", Static).render())

    assert asyncio.run(exercise()) == (
        "Powdrr Agent v0.0.1\n\nwaiting for model LLM response..."
    )


def test_textual_answer_echo_before_prompt_marker_does_not_create_warning() -> None:
    async def exercise() -> str:
        app = WorkflowChatApp(SkillChatConfig(skills_dir=Path("skill-definitions")))
        app._stop_requested.set()
        app._workflow_active = True
        async with app.run_test() as pilot:
            app._set_status("calling LLM...")
            output = _TextualStdoutOutput(app)
            writer = Thread(
                target=lambda: (
                    output.write("What do you want to do? "),
                    output.write("\n"),
                    output.write("> "),
                ),
            )
            writer.start()
            await pilot.pause()
            writer.join()
            return str(app.query_one("#status", Static).render())

    assert asyncio.run(exercise()) == (
        "Powdrr Agent v0.0.1\n\ncalling LLM...\n\nWhat do you want to do?"
    )


def test_textual_flush_displays_nonstandard_human_prompt_before_next_output() -> None:
    async def exercise() -> str:
        app = WorkflowChatApp(SkillChatConfig(skills_dir=Path("skill-definitions")))
        app._stop_requested.set()
        app._workflow_active = True
        async with app.run_test() as pilot:
            output = _TextualStdoutOutput(app)

            def write_prompt() -> None:
                output.write("The LLM returned an empty response. Retry this request? ")
                output.flush()

            writer = Thread(target=write_prompt)
            writer.start()
            await pilot.pause()
            writer.join()
            return str(app.query_one("#status", Static).render())

    assert asyncio.run(exercise()) == (
        "Powdrr Agent v0.0.1\n\nThe LLM returned an empty response. Retry this request?"
    )


def test_textual_each_execution_step_retains_status_history() -> None:
    async def exercise() -> str:
        app = WorkflowChatApp(SkillChatConfig(skills_dir=Path("skill-definitions")))
        app._stop_requested.set()
        app._workflow_active = True
        async with app.run_test() as pilot:
            skill = SkillCatalogEntry(Path("skill.yaml"), _build_skill())
            app._apply_progress(
                skill,
                current_step_index=0,
                status="first step is running",
            )
            app._set_message("first step output")
            app._apply_progress(
                skill,
                current_step_index=1,
                status="second step is running",
            )
            await pilot.pause()
            return str(app.query_one("#status", Static).render())

    assert asyncio.run(exercise()) == (
        "Powdrr Agent v0.0.1\n\n"
        "first step is running\n\n"
        "first step output\n\n"
        "second step is running"
    )


def test_textual_initial_prompt_and_response_remain_before_matched_skill() -> None:
    async def exercise() -> str:
        app = WorkflowChatApp(SkillChatConfig(skills_dir=Path("skill-definitions")))
        app._stop_requested.set()
        app._workflow_active = True
        async with app.run_test() as pilot:
            output = _TextualStdoutOutput(app)

            def write_initial_prompt() -> None:
                output.write("What do you want to do? ")
                output.flush()

            writer = Thread(target=write_initial_prompt)
            writer.start()
            await pilot.pause()
            writer.join()
            response = app.query_one("#response", TextArea)
            response.text = "Specify the feature"
            app._submit_response()
            await pilot.pause()

            def write_matched_skill() -> None:
                output.write("Matched skill: specify-a-feature\n")
                output.flush()

            writer = Thread(target=write_matched_skill)
            writer.start()
            await pilot.pause()
            writer.join()
            await pilot.pause()
            return str(app.query_one("#status", Static).render())

    rendered = asyncio.run(exercise())
    assert rendered == (
        "Powdrr Agent v0.0.1\n\n"
        "What do you want to do?\n\n"
        "----------------------------------------\n"
        "> Specify the feature\n"
        "----------------------------------------\n\n"
        "calling LLM...\n\n"
        "Matched skill: specify-a-feature"
    )
    assert "thinking..." not in rendered


def test_textual_response_supports_copy() -> None:
    async def exercise() -> tuple[bool, str]:
        app = WorkflowChatApp(SkillChatConfig(skills_dir=Path("skill-definitions")))
        app._stop_requested.set()
        async with app.run_test() as pilot:
            response = app.query_one("#response", TextArea)
            response.text = "copy this output"
            response.select_all()
            response.focus()
            app.action_copy_selection()
            await pilot.pause()
            return response.read_only, app.clipboard

    assert asyncio.run(exercise()) == (False, "copy this output")


def test_textual_copy_uses_native_macos_clipboard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = WorkflowChatApp(SkillChatConfig(skills_dir=Path("skill-definitions")))
    monkeypatch.setattr(sys, "platform", "darwin")
    with patch("powdrr_lift.workflow_chat_tui.subprocess.run") as run:
        app.copy_to_clipboard("copy this output")

    assert app.clipboard == "copy this output"
    run.assert_called_once_with(
        ["pbcopy"],
        input="copy this output",
        text=True,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def test_textual_response_supports_cut_through_app_action() -> None:
    async def exercise() -> tuple[str, str]:
        app = WorkflowChatApp(SkillChatConfig(skills_dir=Path("skill-definitions")))
        app._stop_requested.set()
        async with app.run_test() as pilot:
            response = app.query_one("#response", TextArea)
            response.text = "cut this response"
            response.select_all()
            response.focus()
            app.action_cut_selection()
            await pilot.pause()
            return response.text, app.clipboard

    assert asyncio.run(exercise()) == ("", "cut this response")


def test_textual_response_supports_cut_key_without_beeping() -> None:
    async def exercise() -> tuple[str, str]:
        app = WorkflowChatApp(SkillChatConfig(skills_dir=Path("skill-definitions")))
        app._stop_requested.set()
        async with app.run_test() as pilot:
            response = app.query_one("#response", TextArea)
            response.text = "cut this response"
            response.select_all()
            response.focus()
            await pilot.press("ctrl+x")
            await pilot.pause()
            return response.text, app.clipboard

    assert asyncio.run(exercise()) == ("", "cut this response")


def test_textual_response_supports_command_copy_and_cut_keys() -> None:
    async def exercise() -> tuple[str, str]:
        app = WorkflowChatApp(SkillChatConfig(skills_dir=Path("skill-definitions")))
        app._stop_requested.set()
        async with app.run_test() as pilot:
            response = app.query_one("#response", TextArea)
            response.text = "copy and cut this response"
            response.select_all()
            response.focus()
            await pilot.press("super+c")
            await pilot.pause()
            copied = app.clipboard
            response.select_all()
            await pilot.press("super+x")
            await pilot.pause()
            return response.text, copied

    assert asyncio.run(exercise()) == ("", "copy and cut this response")


def test_textual_kitty_protocol_decodes_command_key_sequence() -> None:
    """Verify the terminal sequence emitted for Cmd+C reaches Textual."""
    from textual._xterm_parser import XTermParser

    events = list(XTermParser().feed("\x1b[99;9u"))

    assert len(events) == 1
    assert isinstance(events[0], Key)
    assert events[0].key == "super+c"
    assert events[0].character is None


def test_textual_response_supports_command_paste_from_native_clipboard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def exercise() -> str:
        app = WorkflowChatApp(SkillChatConfig(skills_dir=Path("skill-definitions")))
        app._stop_requested.set()
        async with app.run_test() as pilot:
            response = app.query_one("#response", TextArea)
            response.focus()
            await pilot.press("super+v")
            await pilot.pause()
            return response.text

    monkeypatch.setattr(sys, "platform", "darwin")
    with patch("powdrr_lift.workflow_chat_tui.subprocess.run") as run:
        run.return_value.stdout = "pasted from macOS"
        run.return_value.returncode = 0
        assert asyncio.run(exercise()) == "pasted from macOS"
    run.assert_called_once_with(
        ["pbpaste"],
        check=True,
        capture_output=True,
        text=True,
    )


def test_textual_read_only_panels_support_copy_and_cut() -> None:
    async def exercise() -> tuple[str, str]:
        app = WorkflowChatApp(SkillChatConfig(skills_dir=Path("skill-definitions")))
        app._stop_requested.set()
        app._workflow_active = True
        async with app.run_test() as pilot:
            skill = SkillCatalogEntry(Path("skill.yaml"), _build_skill())
            app._apply_progress(skill, current_step_index=0, status="running")
            app._set_message("green output")
            status_container = app.query_one("#status-container", ScrollableContainer)
            status_container.focus()
            await pilot.pause()
            await pilot.press("super+c")
            green_clipboard = app.clipboard
            steps = app.query_one("#steps", ListView)
            steps.focus()
            await pilot.pause()
            await pilot.press("super+x")
            await pilot.pause()
            return green_clipboard, app.clipboard

    assert asyncio.run(exercise()) == (
        "Powdrr Agent v0.0.1\n\nrunning\n\ngreen output",
        "1. Capture the feature goal.\n2. Summarize the result.",
    )


def test_textual_status_textarea_supports_range_copy_and_cut() -> None:
    async def exercise() -> tuple[str, str]:
        app = WorkflowChatApp(SkillChatConfig(skills_dir=Path("skill-definitions")))
        app._stop_requested.set()
        async with app.run_test() as pilot:
            status = app.query_one("#status-text", TextArea)
            status.text = "copy only this range"
            status.select_all()
            status.focus()
            await pilot.press("super+c")
            await pilot.pause()
            copied = app.clipboard
            await pilot.press("super+x")
            await pilot.pause()
            return status.text, copied

    assert asyncio.run(exercise()) == ("copy only this range", "copy only this range")
