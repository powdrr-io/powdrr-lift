from __future__ import annotations

import asyncio
import io
import json
import os
import shutil
import subprocess
import sys
import types
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Thread
from typing import Any, TextIO, cast
from unittest.mock import patch
from urllib.request import Request

import pytest
import yaml
from textual.containers import ScrollableContainer
from textual.events import Key
from textual.widgets import ListView, Static, TextArea

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
from powdrr_lift.core.pr_specification import (
    _load_feature_catalog,
    validate_pr_specification_yaml,
)
from powdrr_lift.core.system_specification import validate_system_specification_yaml
from powdrr_lift.core.workflow_task_specification import (
    TaskStatus,
    load_ready_workflow_tasks,
    load_workflow_tasks,
    save_workflow_task,
    select_ready_workflow_tasks,
)
from powdrr_lift.fuzzy_match import execute_fuzzy_match
from powdrr_lift.workflow_chat_agent import (
    DEEPINFRA_LLM_MAPPINGS,
    ZAI_LLM_MAPPINGS,
    AnthropicChatClient,
    LLMModelLimits,
    LLMModelMapping,
    LocalLlamaChatClient,
    LocalModelRuntimeError,
    OpenAIChatClient,
    SkillCatalogEntry,
    SkillChatConfig,
    SkillChatEdit,
    _action_system_prompt,
    _apply_file_edits,
    _available_work_item_documents,
    _available_work_item_names,
    _backup_model_for,
    _build_selection_messages,
    _catalog_entry_to_data,
    _complete_json_with_model_fallback,
    _execute_shell_tool,
    _handle_workflow_action_edit,
    _match_work_item_names,
    _parse_action_response,
    _prompt_user,
    _request_token_budget,
    _resolve_api_key,
    _resolve_base_url,
    _resolve_llm_mapping,
    _resolve_llm_model,
    _resolve_local_model_context,
    _resolve_local_model_path,
    _resolve_project_root,
    _resolve_provider,
    _resolve_skill_path,
    _resolve_worktree_context,
    _validate_user_question,
    _WorkflowExecutionState,
    _WorkflowProgressDisplay,
    download_local_qwen_model,
    run_workflow_chat,
)
from powdrr_lift.workflow_chat_tui import (
    WorkflowChatApp,
    _TextualStdoutOutput,
)

# ruff: noqa: E501


def test_local_llama_client_errors_without_gpu_offload_support(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model_path = tmp_path / "qwen2.5-coder-q5_k_m.gguf"
    model_path.touch()
    llama_module = types.SimpleNamespace(
        Llama=lambda **_: pytest.fail("Llama must not load without GPU support"),
        llama_supports_gpu_offload=lambda: False,
    )
    monkeypatch.setitem(sys.modules, "llama_cpp", llama_module)

    with pytest.raises(RuntimeError, match="requires GPU offload support"):
        LocalLlamaChatClient(model_path=model_path)


def test_local_llama_client_requests_full_gpu_offload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model_path = tmp_path / "qwen2.5-coder-q5_k_m.gguf"
    model_path.touch()
    captured: dict[str, object] = {}

    class FakeLlama:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    llama_module = types.SimpleNamespace(
        Llama=FakeLlama,
        llama_supports_gpu_offload=lambda: True,
    )
    monkeypatch.setitem(sys.modules, "llama_cpp", llama_module)

    LocalLlamaChatClient(model_path=model_path)

    assert captured["n_gpu_layers"] == -1
    assert captured["n_ctx"] == 24576


def test_local_model_context_is_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POWDRR_LOCAL_MODEL_CONTEXT", "8192")

    assert _resolve_local_model_context() == 8192


def test_local_model_context_rejects_invalid_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("POWDRR_LOCAL_MODEL_CONTEXT", "not-a-number")

    with pytest.raises(RuntimeError, match="POWDRR_LOCAL_MODEL_CONTEXT"):
        _resolve_local_model_context()


def test_local_llama_client_reports_gpu_initialization_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model_path = tmp_path / "qwen2.5-coder-q5_k_m.gguf"

    def fail_to_load(**_: object) -> object:
        raise ValueError("failed to create Metal command queue")

    monkeypatch.setitem(
        sys.modules,
        "llama_cpp",
        types.SimpleNamespace(
            Llama=fail_to_load,
            llama_supports_gpu_offload=lambda: True,
        ),
    )
    model_path.touch()

    with pytest.raises(LocalModelRuntimeError, match="no CPU fallback"):
        LocalLlamaChatClient(model_path=model_path)


def test_local_llama_client_reports_gpu_inference_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model_path = tmp_path / "qwen2.5-coder-q5_k_m.gguf"

    class FailingLlama:
        def __init__(self, **_: object) -> None:
            pass

        def create_chat_completion(self, **_: object) -> object:
            raise RuntimeError("llama_decode returned -3")

    monkeypatch.setitem(
        sys.modules,
        "llama_cpp",
        types.SimpleNamespace(
            Llama=FailingLlama,
            llama_supports_gpu_offload=lambda: True,
        ),
    )
    model_path.touch()
    client = LocalLlamaChatClient(model_path=model_path)

    with pytest.raises(LocalModelRuntimeError, match="inference failed"):
        client.complete_json([{"role": "user", "content": "test"}])


def test_llm_type_mapping_selects_zai_model_for_next_roundtrip() -> None:
    assert (
        _resolve_llm_model(
            "high_reasoning",
            fallback_model="test-model",
            mappings=(),
        )
        == "glm-5.2"
    )
    assert (
        _resolve_llm_model(
            "simple-task",
            fallback_model="test-model",
            mappings=(
                (
                    "simple_task",
                    LLMModelMapping("custom-fast-model", provider="zai"),
                ),
            ),
            provider="zai",
        )
        == "custom-fast-model"
    )
    assert (
        _resolve_llm_model(
            None,
            fallback_model="test-model",
            mappings=(),
        )
        == "test-model"
    )
    assert (
        _resolve_llm_model(
            "high_reasoning",
            fallback_model="gpt-test-model",
            mappings=(),
            provider="openai",
        )
        == "gpt-test-model"
    )
    simple_mapping = _resolve_llm_mapping(
        "simple_task",
        mappings=(),
        provider="zai",
    )
    assert simple_mapping is not None
    assert simple_mapping.provider == "local"
    assert _resolve_provider("auto", simple_mapping.model, mapping=simple_mapping) == (
        "local"
    )
    deepinfra_mapping = _resolve_llm_mapping(
        "high_reasoning",
        mappings=(),
        provider="deepinfra",
    )
    assert deepinfra_mapping is not None
    assert deepinfra_mapping.provider == "deepinfra"


def test_prompt_user_reports_llm_call_after_input() -> None:
    stdout = io.StringIO()
    status_stream = io.StringIO()

    answer = _prompt_user(
        "Question: ",
        input_func=lambda: "answer",
        stdout=stdout,
        status_stream=status_stream,
    )

    assert answer == "answer"
    assert stdout.getvalue() == "Question: \n"
    assert status_stream.getvalue() == "[workflow] calling LLM...\n"


def test_textual_response_grows_and_submits_on_return(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    received: list[str] = []

    def fake_run_workflow_chat(config: Any, **kwargs: Any) -> int:
        kwargs["stdout"].write("What do you want to do? ")
        received.append(kwargs["input_func"]())
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
            assert "Workflow complete" in str(app.query_one("#status", Static).render())
            response.text = "follow-up request"
            await pilot.press("enter")
            await pilot.pause(0.1)
            return height

    height = asyncio.run(exercise())

    assert height >= 4
    assert received == ["line one\nline two", "follow-up request"]


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


def test_textual_startup_shows_initial_question(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run_workflow_chat(config: Any, **kwargs: Any) -> int:
        kwargs["stdout"].write("What do you want to do? ")
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

    assert asyncio.run(exercise()) == ("What do you want to do?", False)


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

    assert asyncio.run(exercise()) == "Which requirements should this feature satisfy?"


def test_textual_submit_shows_thinking_before_releasing_workflow() -> None:
    async def exercise() -> str:
        app = WorkflowChatApp(SkillChatConfig(skills_dir=Path("skill-definitions")))
        app._stop_requested.set()
        async with app.run_test() as pilot:
            response = app.query_one("#response", TextArea)
            response.text = "Build the feature"
            app._submit_response()
            await pilot.pause()
            return str(app.query_one("#status", Static).render())

    assert asyncio.run(exercise()) == "calling LLM..."


def test_textual_submit_removes_initial_prompt_from_status() -> None:
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

    assert asyncio.run(exercise()) == "calling LLM..."


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
    assert rendered.startswith("x")
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


def test_textual_status_shows_latest_output() -> None:
    async def exercise() -> str:
        app = WorkflowChatApp(SkillChatConfig(skills_dir=Path("skill-definitions")))
        app._stop_requested.set()
        async with app.run_test() as pilot:
            for message in ("first", "second", "third", "fourth"):
                app._set_message(message)
            await pilot.pause()
            return str(app.query_one("#status", Static).render())

    assert asyncio.run(exercise()) == "first\n\nsecond\n\nthird\n\nfourth"


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

    assert asyncio.run(exercise()) == "waiting for test-model LLM response..."


def test_textual_status_keeps_all_questions_visible() -> None:
    async def exercise() -> tuple[str, int]:
        app = WorkflowChatApp(SkillChatConfig(skills_dir=Path("skill-definitions")))
        app._stop_requested.set()
        async with app.run_test() as pilot:
            status = app.query_one("#status", Static)
            for question in (
                "1. What should be logged?",
                "2. Which file format should be used?",
                "3. Which platforms should be supported?",
                "4. What should be redacted?",
                "5. What performance constraints apply?",
            ):
                app._set_message(question)
            await pilot.pause()
            return str(status.render()), status.region.height

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
        "Matched skill: internal-debug\n\nWhich requirements should this feature satisfy?",
        "Matched skill: internal-debug\n\nWhich requirements should this feature satisfy?",
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
        "Matched skill: specify-a-feature\n\n"
        "1. What is the feature goal?\n"
        "2. Which requirements matter?\n"
        "Please answer whichever of these you can.\n\n"
        "Matched skill: next-skill\n\n"
        "Next question?"
    )


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
    assert rendered == ("Matched skill: specify-a-feature\n\nWhat is the feature goal?")


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

    assert asyncio.run(exercise()) == "waiting for model LLM response..."


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

    assert asyncio.run(exercise()) == "What do you want to do?"


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
        "The LLM returned an empty response. Retry this request?"
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

    assert asyncio.run(exercise()) == "first step output\n\nsecond step is running"


def test_textual_initial_prompt_is_gone_before_matched_skill() -> None:
    async def exercise() -> str:
        app = WorkflowChatApp(SkillChatConfig(skills_dir=Path("skill-definitions")))
        app._stop_requested.set()
        app._workflow_active = True
        async with app.run_test() as pilot:
            output = _TextualStdoutOutput(app)

            def write_initial_prompt() -> None:
                output.write("What do you want to do? ")

            writer = Thread(target=write_initial_prompt)
            writer.start()
            await pilot.pause()
            writer.join()
            response = app.query_one("#response", TextArea)
            response.text = "Specify the feature"
            app._submit_response()

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
    assert rendered == "Matched skill: specify-a-feature"
    assert "What do you want to do?" not in rendered
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
        "green output",
        "1. Capture the feature goal.\n2. Summarize the result.",
    )


def test_workflow_progress_lists_steps_and_updates_status() -> None:
    stream = io.StringIO()
    progress = _WorkflowProgressDisplay(stream)
    skill = SkillCatalogEntry(Path("skill.yaml"), _build_skill())

    progress.update(
        skill,
        current_step_index=0,
        status="waiting on LLM response...",
    )
    progress.update(
        skill,
        current_step_index=0,
        status="performing local action...",
    )
    progress.update(
        skill,
        current_step_index=1,
        status="waiting on LLM response...",
    )

    output = stream.getvalue()
    assert "1. Capture the feature goal." in output
    assert "2. Summarize the result." in output
    assert "waiting on LLM response..." in output
    assert "performing local action..." in output


def test_default_simple_task_model_uses_qwen_coder_with_glm_backup() -> None:
    assert (
        _resolve_llm_model(
            "simple_task",
            fallback_model="test-model",
            mappings=(),
            provider="zai",
        )
        == "Qwen/Qwen2.5-Coder-14B-Instruct"
    )
    backup_mapping = _backup_model_for(
        "Qwen/Qwen2.5-Coder-14B-Instruct",
        tuple(ZAI_LLM_MAPPINGS.items()),
    )
    assert backup_mapping is not None
    assert backup_mapping.model == "glm-4.7"


def test_llm_mapping_rejects_unsupported_provider() -> None:
    with pytest.raises(RuntimeError, match="not supported for provider 'openai'"):
        _resolve_llm_mapping(
            "simple_task",
            mappings=(),
            provider="openai",
        )


@pytest.mark.parametrize("value", ["   ", "Please provide the requirements.", "???"])
def test_user_question_validation_rejects_empty_or_malformed_questions(
    value: str,
) -> None:
    with pytest.raises(RuntimeError, match="properly formed English question"):
        _validate_user_question(value, field_name="test question")


def test_user_question_validation_normalizes_valid_question() -> None:
    assert (
        _validate_user_question("  What should this feature do?  ", field_name="test")
        == "What should this feature do?"
    )


def test_local_model_path_requires_pre_downloaded_q5_k_m_shards(
    tmp_path: Path,
) -> None:
    with pytest.raises(RuntimeError, match="download-qwen-model"):
        _resolve_local_model_path(tmp_path)


def test_download_local_model_caches_q5_k_m_shards(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_shard = tmp_path / "qwen2.5-coder-14b-instruct-q5_k_m-00001-of-00002.gguf"
    second_shard = tmp_path / "qwen2.5-coder-14b-instruct-q5_k_m-00002-of-00002.gguf"
    download_calls = 0

    class _FakeHuggingFaceHub:
        @staticmethod
        def snapshot_download(**kwargs: object) -> str:
            nonlocal download_calls
            download_calls += 1
            assert kwargs["repo_id"] == "Qwen/Qwen2.5-Coder-14B-Instruct-GGUF"
            assert kwargs["allow_patterns"] == [
                "qwen2.5-coder-14b-instruct-q5_k_m*.gguf"
            ]
            assert kwargs["local_dir"] == str(tmp_path)
            first_shard.touch()
            second_shard.touch()
            return str(tmp_path)

    monkeypatch.setitem(sys.modules, "huggingface_hub", _FakeHuggingFaceHub)

    assert download_local_qwen_model(tmp_path) == first_shard
    assert download_calls == 1


def test_local_model_download_reports_underlying_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FailingHuggingFaceHub:
        @staticmethod
        def snapshot_download(**_: object) -> str:
            raise OSError("TLS handshake failed")

    monkeypatch.setitem(
        sys.modules,
        "huggingface_hub",
        _FailingHuggingFaceHub,
    )

    with pytest.raises(RuntimeError, match="OSError: TLS handshake failed"):
        download_local_qwen_model(tmp_path)


def test_request_token_budget_reserves_input_context_and_model_limit() -> None:
    max_tokens, estimated_input_tokens = _request_token_budget(
        [{"role": "user", "content": "x" * 3_000}],
        LLMModelLimits(context_window=2_500, max_output_tokens=2_000),
    )

    assert estimated_input_tokens == 1_010
    assert max_tokens == 466


def test_request_token_budget_rejects_exhausted_context() -> None:
    with pytest.raises(RuntimeError, match="context window is exhausted"):
        _request_token_budget(
            [{"role": "user", "content": "x" * 3_000}],
            LLMModelLimits(context_window=1_000, max_output_tokens=2_000),
        )


def test_model_unavailable_uses_backup_model_without_prompting() -> None:
    class _FakeClient:
        def __init__(self, model: str) -> None:
            self.model = model

        def complete_json(self, messages: list[dict[str, str]]) -> dict[str, Any]:
            if self.model == "glm-4.7-flashx":
                raise RuntimeError(
                    "OpenAI request failed with HTTP 404: model is not available"
                )
            return {"ok": True}

    clients: list[str] = []

    def client_for(model: str, provider: str) -> _FakeClient:
        clients.append(model)
        return _FakeClient(model)

    result, model, provider = _complete_json_with_model_fallback(
        client_for=client_for,
        messages=[],
        context="test request",
        model="glm-4.7-flashx",
        provider="zai",
        parser=lambda payload: payload,
        repair_instructions="",
        config=SkillChatConfig(skills_dir=Path("skills")),
        input_func=lambda: "abort",
        stdout=io.StringIO(),
        stderr=io.StringIO(),
        model_mappings=(
            (
                "simple_task",
                LLMModelMapping(
                    "glm-4.7-flashx",
                    provider="zai",
                    backup_model=LLMModelMapping("glm-4.7", provider="zai"),
                ),
            ),
        ),
    )

    assert result == {"ok": True}
    assert model == "glm-4.7"
    assert clients == ["glm-4.7-flashx", "glm-4.7"]


def test_repeated_timeouts_use_backup_model_after_retries() -> None:
    class _FakeClient:
        def __init__(self, model: str) -> None:
            self.model = model

        def complete_json(self, messages: list[dict[str, str]]) -> dict[str, Any]:
            if self.model == "glm-4.7-flashx":
                raise RuntimeError(
                    "OpenAI request timed out: The read operation timed out"
                )
            return {"ok": True}

    clients: list[str] = []

    def client_for(model: str, provider: str) -> _FakeClient:
        clients.append(model)
        return _FakeClient(model)

    result, model, provider = _complete_json_with_model_fallback(
        client_for=client_for,
        messages=[],
        context="test request",
        model="glm-4.7-flashx",
        provider="zai",
        parser=lambda payload: payload,
        repair_instructions="",
        config=SkillChatConfig(
            skills_dir=Path("skills"),
            provider_retry_attempts=2,
            provider_retry_delay_seconds=0,
        ),
        input_func=lambda: "abort",
        stdout=io.StringIO(),
        stderr=io.StringIO(),
        model_mappings=(
            (
                "simple_task",
                LLMModelMapping(
                    "glm-4.7-flashx",
                    provider="zai",
                    backup_model=LLMModelMapping("glm-4.7", provider="zai"),
                ),
            ),
        ),
    )

    assert result == {"ok": True}
    assert model == "glm-4.7"
    assert clients == ["glm-4.7-flashx", "glm-4.7"]


def test_timeout_followed_by_other_transient_errors_uses_backup_model() -> None:
    class _FakeClient:
        def __init__(self, model: str) -> None:
            self.model = model
            self.calls = 0

        def complete_json(self, messages: list[dict[str, str]]) -> dict[str, Any]:
            self.calls += 1
            if self.model == "glm-4.7-flashx":
                if self.calls == 1:
                    raise RuntimeError("OpenAI request timed out")
                raise RuntimeError(
                    'OpenAI request failed with HTTP 429: {"error":{"code":"1305"}}'
                )
            return {"ok": True}

    clients: list[str] = []

    def client_for(model: str, provider: str) -> _FakeClient:
        clients.append(model)
        return _FakeClient(model)

    result, model, provider = _complete_json_with_model_fallback(
        client_for=client_for,
        messages=[],
        context="test request",
        model="glm-4.7-flashx",
        provider="zai",
        parser=lambda payload: payload,
        repair_instructions="",
        config=SkillChatConfig(
            skills_dir=Path("skills"),
            provider_retry_attempts=2,
            provider_retry_delay_seconds=0,
        ),
        input_func=lambda: "abort",
        stdout=io.StringIO(),
        stderr=io.StringIO(),
        model_mappings=(
            (
                "simple_task",
                LLMModelMapping(
                    "glm-4.7-flashx",
                    provider="zai",
                    backup_model=LLMModelMapping("glm-4.7", provider="zai"),
                ),
            ),
        ),
    )

    assert result == {"ok": True}
    assert model == "glm-4.7"
    assert clients == ["glm-4.7-flashx", "glm-4.7"]


def test_repeated_rate_limits_use_backup_model_after_retries() -> None:
    class _FakeClient:
        def __init__(self, model: str) -> None:
            self.model = model

        def complete_json(self, messages: list[dict[str, str]]) -> dict[str, Any]:
            if self.model == "glm-4.7-flashx":
                raise RuntimeError(
                    "OpenAI request failed with HTTP 429: "
                    '{"error":{"code":"1302","message":"Rate limit reached for requests"}}'
                )
            return {"ok": True}

    clients: list[str] = []

    def client_for(model: str, provider: str) -> _FakeClient:
        clients.append(model)
        return _FakeClient(model)

    result, model, provider = _complete_json_with_model_fallback(
        client_for=client_for,
        messages=[],
        context="test request",
        model="glm-4.7-flashx",
        provider="zai",
        parser=lambda payload: payload,
        repair_instructions="",
        config=SkillChatConfig(
            skills_dir=Path("skills"),
            provider_retry_attempts=3,
            provider_retry_delay_seconds=0,
        ),
        input_func=lambda: "abort",
        stdout=io.StringIO(),
        stderr=io.StringIO(),
        model_mappings=(
            (
                "simple_task",
                LLMModelMapping(
                    "glm-4.7-flashx",
                    provider="zai",
                    backup_model=LLMModelMapping("glm-4.7", provider="zai"),
                ),
            ),
        ),
    )

    assert result == {"ok": True}
    assert model == "glm-4.7"
    assert clients == ["glm-4.7-flashx", "glm-4.7"]


def test_openai_read_timeout_is_reported_as_provider_runtime_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _timed_out(request: Request, timeout: float) -> object:
        raise TimeoutError("The read operation timed out")

    monkeypatch.setattr("powdrr_lift.workflow_chat_agent.urlopen", _timed_out)
    client = OpenAIChatClient(
        model="test-model",
        api_key="test-key",
        base_url="https://api.openai.com/v1",
    )

    with pytest.raises(
        RuntimeError,
        match=(
            "OpenAI-compatible request timed out for model 'test-model'.*"
            "configured 120s timeout.*messages=1.*max_tokens=32768"
        ),
    ):
        client.complete_json([{"role": "user", "content": "hello"}])


def test_deepinfra_credentials_and_base_url_are_supported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPINFRA_API_TOKEN", "deepinfra-token")
    monkeypatch.setenv("DEEPINFRA_BASE_URL", "https://deepinfra.example/v1/openai")

    assert _resolve_api_key("deepinfra", None) == (
        "deepinfra-token",
        "DEEPINFRA_API_TOKEN",
    )
    assert _resolve_base_url("deepinfra", None) == (
        "https://deepinfra.example/v1/openai",
        "DEEPINFRA_BASE_URL",
    )


def test_llm_type_mapping_selects_deepinfra_model() -> None:
    assert (
        _resolve_llm_model(
            "high_reasoning",
            fallback_model="fallback-model",
            mappings=tuple(DEEPINFRA_LLM_MAPPINGS.items()),
            provider="deepinfra",
        )
        == "deepseek-ai/DeepSeek-V4-Pro"
    )


def _assert_validation_success(
    report: dict[str, object],
    *,
    label: str,
) -> None:
    assert report["validation_successful"] is True, (
        f"{label} validation failed:\n{yaml.safe_dump(report, sort_keys=False)}"
    )


def _repo_feature_ids(repo_root: Path, *, count: int = 2) -> list[str]:
    feature_ids = [entry.feature_id for entry in _load_feature_catalog(repo_root)]
    assert len(feature_ids) >= count, "Expected at least two current feature ids."
    return feature_ids[:count]


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
            "--max-stalled-roundtrips",
            "5",
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
    assert config.max_stalled_roundtrips == 5
    assert config.verbose is False


def test_cli_download_qwen_model_uses_repository_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    captured: dict[str, Path] = {}
    model_path = repo_root / ".powdrr" / "models" / "model.gguf"

    def _fake_download(cache_dir: Path) -> Path:
        captured["cache_dir"] = cache_dir
        return model_path

    monkeypatch.setattr("powdrr_lift.cli.download_local_qwen_model", _fake_download)

    assert main(["download-qwen-model", "--repo-root", str(repo_root)]) == 0
    assert captured["cache_dir"] == repo_root / ".powdrr" / "models"
    assert capsys.readouterr().out == f"Qwen model cached at {model_path}\n"


def test_workflow_execution_allows_more_roundtrips_than_max_turns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    skills_dir = repo_root / "skill-definitions"
    skills_dir.mkdir()
    skill_path = skills_dir / "specify-a-feature.json"
    save_skill(_build_skill(), skill_path)

    class _FakeOpenAIClient:
        def __init__(self, **_: object) -> None:
            self.call_index = 0

        def complete_json(self, _: list[dict[str, str]]) -> dict[str, object]:
            call_index = self.call_index
            self.call_index += 1
            if call_index == 0:
                return {
                    "selected_skill_path": str(skill_path),
                    "selected_skill_reason": "The skill matches the request.",
                    "next_question": None,
                    "ready_to_execute": True,
                }
            if 1 <= call_index <= 6:
                return {
                    "kind": "invoke_tool",
                    "tool": "shell",
                    "parameters": {"command": f"printf progress-{call_index}"},
                }
            if call_index == 7:
                return {"kind": "next_step"}
            if call_index == 8:
                return {"kind": "complete", "text": "Done."}
            raise AssertionError(f"Unexpected LLM call index: {call_index}")

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(
        "powdrr_lift.workflow_chat_agent.OpenAIChatClient",
        _FakeOpenAIClient,
    )
    monkeypatch.setattr(
        "powdrr_lift.workflow_chat_agent._resolve_worktree_context",
        lambda repo_root, stderr, verbose: repo_root,
    )

    stdout = io.StringIO()
    stderr = io.StringIO()
    exit_code = run_workflow_chat(
        SkillChatConfig(
            skills_dir=skills_dir,
            repo_root=repo_root,
            output_dir=tmp_path / "generated",
            provider="openai",
            api_key="test-key",
            max_turns=1,
        ),
        input_func=lambda: "Build the feature",
        stdout=stdout,
        stderr=stderr,
    )

    assert exit_code == 0
    assert "Done." in stdout.getvalue()


def test_workflow_execution_stops_after_repeated_no_progress(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    skills_dir = repo_root / "skill-definitions"
    skills_dir.mkdir()
    skill_path = skills_dir / "one-step.json"
    save_skill(
        Skill(
            name="one-step",
            when_to_use=("For testing stalled workflow execution.",),
            steps=(SkillStep(description="Do the work.", details="Do it."),),
        ),
        skill_path,
    )

    class _FakeOpenAIClient:
        def __init__(self, **_: object) -> None:
            self.call_index = 0

        def complete_json(self, _: list[dict[str, str]]) -> dict[str, object]:
            self.call_index += 1
            if self.call_index == 1:
                return {
                    "selected_skill_path": str(skill_path),
                    "selected_skill_reason": "The skill matches the request.",
                    "next_question": None,
                    "ready_to_execute": True,
                }
            return {
                "kind": "invoke_tool",
                "tool": "shell",
                "parameters": {"command": "printf same"},
            }

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(
        "powdrr_lift.workflow_chat_agent.OpenAIChatClient",
        _FakeOpenAIClient,
    )
    monkeypatch.setattr(
        "powdrr_lift.workflow_chat_agent._resolve_worktree_context",
        lambda repo_root, stderr, verbose: repo_root,
    )

    stderr = io.StringIO()
    exit_code = run_workflow_chat(
        SkillChatConfig(
            skills_dir=skills_dir,
            repo_root=repo_root,
            output_dir=tmp_path / "generated",
            provider="openai",
            api_key="test-key",
            max_stalled_roundtrips=2,
        ),
        input_func=lambda: "Build the feature",
        stdout=io.StringIO(),
        stderr=stderr,
    )

    assert exit_code == 1
    assert "without progress" in stderr.getvalue()


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
        def __init__(
            self,
            *,
            model: str,
            api_key: str,
            base_url: str,
            **_: object,
        ) -> None:
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
        def __init__(
            self,
            *,
            model: str,
            api_key: str,
            base_url: str,
            **_: object,
        ) -> None:
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
        def __init__(
            self,
            *,
            model: str,
            api_key: str,
            base_url: str,
            **_: object,
        ) -> None:
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


def test_empty_replace_text_removes_the_selected_lines() -> None:
    action = _parse_action_response(
        {
            "kind": "edit",
            "file_path": "notes.txt",
            "edits": [
                {
                    "kind": "replace",
                    "start_line": 2,
                    "end_line": 3,
                    "text": "",
                }
            ],
        }
    )

    assert _apply_file_edits("one\ntwo\nthree\nfour\n", action.edits) == ("one\nfour\n")


def test_edit_action_can_update_multiple_files_in_one_response(
    tmp_path: Path,
) -> None:
    first_path = tmp_path / "first.txt"
    second_path = tmp_path / "nested" / "second.txt"
    first_path.write_text("first\n", encoding="utf-8")
    second_path.parent.mkdir()
    second_path.write_text("second\n", encoding="utf-8")
    action = _parse_action_response(
        {
            "kind": "edit",
            "file_edits": [
                {
                    "file_path": "first.txt",
                    "edits": [
                        {
                            "kind": "replace",
                            "start_line": 1,
                            "text": "updated first",
                        }
                    ],
                },
                {
                    "file_path": "nested/second.txt",
                    "edits": [
                        {
                            "kind": "replace",
                            "start_line": 1,
                            "text": "updated second",
                        }
                    ],
                },
            ],
        }
    )
    state = _WorkflowExecutionState(
        selected_skill=SkillCatalogEntry(tmp_path / "skill.json", _build_skill()),
        transcript=[],
        execution_events=[],
        execution_context=[],
        step_index=0,
        worktree_root=tmp_path,
    )

    _handle_workflow_action_edit(
        action,
        state,
        io.StringIO(),
        io.StringIO(),
        lambda: "",
        SkillChatConfig(skills_dir=tmp_path),
    )

    assert first_path.read_text(encoding="utf-8") == "updated first\n"
    assert second_path.read_text(encoding="utf-8") == "updated second\n"
    assert len(state.execution_events[0]["result"]) == 2


def test_prompt_user_action_requires_nonempty_text() -> None:
    with pytest.raises(
        RuntimeError,
        match="properly formed English question",
    ):
        _parse_action_response({"kind": "prompt_user", "text": "  "})


def test_workflow_edit_failure_is_sent_back_to_llm_for_correction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    worktree_root = repo_root / ".worktrees" / "skill-chat-test"
    skills_dir = worktree_root / "skill-definitions"
    skills_dir.mkdir(parents=True)
    save_skill(_build_skill(), skills_dir / "specify-a-feature.json")
    notes_path = worktree_root / "notes.txt"
    notes_path.write_text("original\n", encoding="utf-8")
    captured_messages: list[dict[str, str]] = []

    class _FakeOpenAIClient:
        def __init__(self, **_: object) -> None:
            self.call_index = 0

        def complete_json(self, messages: list[dict[str, str]]) -> dict[str, object]:
            captured_messages.append(messages[1])
            call_index = self.call_index
            self.call_index += 1
            if call_index == 0:
                return {
                    "selected_skill_path": str(skills_dir / "specify-a-feature.json"),
                    "selected_skill_reason": "The request is a feature task.",
                    "next_question": None,
                    "ready_to_execute": True,
                }
            if call_index == 1:
                return {
                    "kind": "edit",
                    "file_path": "notes.txt",
                    "edits": [
                        {
                            "kind": "replace",
                            "start_line": 5,
                            "end_line": 5,
                            "text": "incorrect range",
                        }
                    ],
                }
            if call_index == 2:
                prompt = json.loads(messages[1]["content"])
                assert prompt["current_file"] == {
                    "path": "notes.txt",
                    "exists": True,
                    "line_count": 1,
                    "lines": [{"line_number": 1, "text": "original"}],
                }
                assert "line 5" in prompt["transcript"][-1]["content"]
                return {
                    "kind": "edit",
                    "file_path": "notes.txt",
                    "edits": [
                        {
                            "kind": "replace",
                            "start_line": 1,
                            "end_line": 1,
                            "text": "corrected",
                        }
                    ],
                }
            if call_index == 3:
                return {"kind": "next_step"}
            if call_index == 4:
                return {"kind": "complete", "text": "Done."}
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
        input_func=iter(["Fix notes"]).__next__,
        stdout=stdout,
        stderr=stderr,
    )

    assert exit_code == 0
    assert notes_path.read_text(encoding="utf-8") == "corrected\n"
    assert "Workflow edit action failed" in stderr.getvalue()
    assert "current file has 1 lines" in stderr.getvalue()
    assert len(captured_messages) == 5


def test_cli_workflow_chat_end_to_end_specify_and_start_feature_with_mocked_llm_calls(
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
    for old_skill_path in skills_dir.glob("*.json"):
        old_skill_path.unlink()
    for source_skill_path in sorted(
        (source_repo_root / "skill-definitions").glob("*.yaml")
    ):
        shutil.copy2(source_skill_path, skills_dir / source_skill_path.name)
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
            "id": "display-related-photos",
            "feature_ids": _repo_feature_ids(repo_root),
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
        "Capture the feature name, goal, and success criteria.",
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
        for old_skill_path in (resolved / "skill-definitions").glob("*.json"):
            old_skill_path.unlink()
        relative_paths = (
            Path("templates") / "implement-a-feature.yaml",
            Path("templates") / "execute-proposed-pr.yaml",
            Path("src") / "powdrr_lift" / "core" / "workflow_template_specification.py",
            Path("src") / "powdrr_lift" / "core" / "skill_specification.py",
            Path("src") / "powdrr_lift" / "core" / "spec_paths.py",
            Path("src") / "powdrr_lift" / "core" / "__init__.py",
            Path("src") / "powdrr_lift" / "__init__.py",
        ) + tuple(
            path.relative_to(source_repo_root)
            for path in sorted((source_repo_root / "skill-definitions").glob("*.yaml"))
        )
        for relative_path in relative_paths:
            target_path = resolved / relative_path
            target_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_repo_root / relative_path, target_path)
        return resolved

    class _FakeOpenAIClient:
        def __init__(
            self,
            *,
            model: str,
            api_key: str,
            base_url: str,
            **_: object,
        ) -> None:
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
                    "selected_skill_path": str(skills_dir / "specify-a-feature.yaml"),
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
                    "text": "Would you please review the draft result?",
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
    monkeypatch.setenv("UV_CACHE_DIR", str(tmp_path / "uv-cache"))
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

    assert exit_code == 0, stderr.getvalue()
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

    _assert_validation_success(system_report, label="system")
    _assert_validation_success(architecture_report, label="architecture")
    _assert_validation_success(implementation_report, label="implementation")
    _assert_validation_success(pr_report, label="proposed PR")
    assert "Wrote skill execution summary to" in stdout.getvalue()
    assert "Would you please review the draft result?" in stdout.getvalue()

    start_skills_dir = worktree_root / "skill-definitions"
    start_output_dir = Path("start-generated")
    start_responses: Iterator[dict[str, object]] = iter(
        [
            {
                "selected_skill_path": str(
                    start_skills_dir / "start-implementing-feature.yaml"
                ),
                "selected_skill_reason": (
                    "The feature has validated specifications and is ready for "
                    "implementation planning."
                ),
                "next_question": None,
                "ready_to_execute": True,
            },
            {
                "kind": "next_step",
                "decisions_and_context": "The feature and work item are confirmed.",
            },
            {
                "kind": "invoke_tool",
                "tool": "shell",
                "parameters": {
                    "command": [
                        "sed",
                        "-n",
                        "1,260p",
                        "templates/implement-a-feature.yaml",
                    ]
                },
                "decisions_and_context": "The implement-a-feature template is selected.",
            },
            {
                "kind": "next_step",
                "decisions_and_context": "The generated task graph is appropriate.",
            },
            {
                "kind": "invoke_tool",
                "tool": "shell",
                "parameters": {
                    "command": [
                        "powdrr-lift",
                        "instantiate-workflow",
                        "--work-item-name",
                        "display-related-photos",
                        "--template",
                        "templates/implement-a-feature.yaml",
                    ]
                },
                "decisions_and_context": "The durable implementation workflow is created.",
            },
            {
                "kind": "next_step",
                "decisions_and_context": "The first task is ready for review.",
            },
            {
                "kind": "next_step",
                "decisions_and_context": "The generated workflow looks correct.",
            },
            {
                "kind": "invoke_tool",
                "tool": "shell",
                "parameters": {
                    "command": [
                        "gh",
                        "pr",
                        "create",
                        "--draft",
                        "--fill",
                    ]
                },
                "decisions_and_context": "The generated workflow is handed off for review.",
            },
            {
                "kind": "next_step",
                "decisions_and_context": "The draft pull request is ready.",
            },
            {
                "kind": "complete",
                "text": "Implementation workflow ready for review.",
                "decisions_and_context": "Stop until the user approves implementation.",
            },
        ]
    )

    start_captured: dict[str, object] = {"messages": []}

    def _expected_start_step(call_index: int) -> int:
        return {
            1: 0,
            2: 1,
            3: 1,
            4: 2,
            5: 2,
            6: 3,
            7: 4,
            8: 4,
            9: 5,
        }[call_index]

    class _FakeStartOpenAIClient:
        def __init__(self, **_: object) -> None:
            self._call_index = 0

        def complete_json(self, messages: list[dict[str, str]]) -> dict[str, object]:
            cast(list[list[dict[str, str]]], start_captured["messages"]).append(
                messages
            )
            prompt = json.loads(messages[1]["content"])
            if self._call_index == 0:
                assert prompt["conversation"][0]["content"] == (
                    "Start implementing display related photos"
                )
                assert any(
                    skill["name"] == "start-implementing-feature"
                    for skill in prompt["skills"]
                )
            else:
                assert prompt["selected_skill"]["name"] == (
                    "start-implementing-feature"
                )
                assert prompt["execution_mode"] == "execute_selected_skill"
                assert prompt["current_step_index"] == _expected_start_step(
                    self._call_index
                )
            response = next(start_responses)
            self._call_index += 1
            return response

    real_subprocess_run = subprocess.run

    def _fake_start_subprocess_run(*args: Any, **kwargs: Any) -> Any:
        command = args[0] if args else kwargs.get("args")
        if isinstance(command, list) and command[:2] == ["rtk", "gh"]:
            return subprocess.CompletedProcess(
                command,
                0,
                stdout="https://github.com/example/repo/pull/123\n",
                stderr="",
            )
        return real_subprocess_run(*args, **kwargs)

    monkeypatch.setattr(
        "powdrr_lift.workflow_chat_agent.OpenAIChatClient",
        _FakeStartOpenAIClient,
    )
    monkeypatch.setattr(
        "powdrr_lift.workflow_chat_agent.subprocess.run",
        _fake_start_subprocess_run,
    )

    start_stdout = io.StringIO()
    start_stderr = io.StringIO()
    start_exit_code = run_workflow_chat(
        SkillChatConfig(
            skills_dir=start_skills_dir,
            repo_root=worktree_root,
            output_dir=start_output_dir,
            provider="openai",
            model="test-model",
            api_key="test-key",
            max_turns=10,
        ),
        input_func=lambda: "Start implementing display related photos",
        stdout=start_stdout,
        stderr=start_stderr,
    )

    assert start_exit_code == 0
    start_summary_path = worktree_root / start_output_dir / "skill-execution.json"
    assert start_summary_path.exists()
    start_summary = json.loads(start_summary_path.read_text(encoding="utf-8"))
    assert start_summary["selected_skill_name"] == "start-implementing-feature"
    assert [event["kind"] for event in start_summary["execution_events"]] == [
        "next_step",
        "invoke_tool",
        "next_step",
        "invoke_tool",
        "next_step",
        "next_step",
        "invoke_tool",
        "next_step",
        "complete",
    ]

    workflow_directory = worktree_root / "docs" / "workflows" / "display-related-photos"
    tasks = load_workflow_tasks(workflow_directory)
    assert [task.task_id for task in tasks] == [
        "task-001",
        "task-002",
        "task-003",
    ], start_stdout.getvalue() + start_stderr.getvalue()
    assert [task.description for task in tasks] == [
        "Review plan documents",
        "Confirm requirements",
        "Generate proposed PRs",
    ]
    assert [task.upstream_task_ids for task in tasks] == [
        (),
        ("task-001",),
        ("task-002",),
    ]
    assert all(task.status.value == "open" for task in tasks)
    assert select_ready_workflow_tasks(tasks) == (tasks[0],)
    assert "Implementation workflow ready for review." in start_stdout.getvalue()

    workflow_root = worktree_root / "docs" / "workflows"
    assignment_batches: list[tuple[str, str]] = []
    while ready_tasks := load_ready_workflow_tasks(workflow_root):
        current_assignment = (
            ready_tasks[0].task.assignee_type.value,
            ready_tasks[0].task.assignee_role.value,
        )
        assignment_batches.append(current_assignment)
        assigned_ready_tasks = load_ready_workflow_tasks(
            workflow_root,
            assignee_type=current_assignment[0],
            assignee_role=current_assignment[1],
        )
        assert assigned_ready_tasks
        for ready_task in assigned_ready_tasks:
            task = ready_task.task
            for invocation in task.tool_invocations:
                command = [
                    item.replace(
                        "<execute-work-item-name>",
                        "display-related-photos-pr-001",
                    )
                    .replace("<feature-name>", "display-related-photos")
                    .replace("<work-item-name>", "display-related-photos")
                    .replace(
                        "<workflow-instance-name>", "display-related-photos-pr-001"
                    )
                    for item in invocation.command
                ]
                result = _execute_shell_tool(
                    {"command": command},
                    worktree_root=worktree_root,
                    stdout=start_stdout,
                    stderr=start_stderr,
                    verbose=False,
                )
                assert result["returncode"] == 0, (
                    f"command={command!r} result={result!r}"
                )
            save_workflow_task(
                replace(task, status=TaskStatus.COMPLETED),
                workflow_root / ready_task.work_item_name / f"{task.task_id}.json",
            )

    assert assignment_batches == [
        ("agent", "architect"),
        ("human", "decider"),
        ("agent", "architect"),
        ("agent", "architect"),
        ("agent", "architect"),
        ("human", "decider"),
        ("agent", "coder"),
        ("agent", "coder"),
        ("human", "reviewer"),
        ("human", "decider"),
        ("agent", "reviewer"),
        ("human", "reviewer"),
    ]
    assert load_ready_workflow_tasks(workflow_root) == ()

    execute_tasks = [
        task
        for task in load_workflow_tasks(workflow_root / "display-related-photos")
        if task.task_id.startswith("display-related-photos-pr-001-")
    ]
    assert len(execute_tasks) == 9
    assert [task.description for task in execute_tasks] == [
        "Review proposed PR plan",
        "Review overall plan",
        "Confirm requirements",
        "Generate test diffs",
        "Generate functionality diffs",
        "Confirm completeness",
        "Confirm goals",
        "Lint and cleanup",
        "Create PR",
    ]
    assert all(task.status is TaskStatus.COMPLETED for task in execute_tasks)


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
        def __init__(
            self,
            *,
            model: str,
            api_key: str,
            base_url: str,
            **_: object,
        ) -> None:
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
    assert "[verbose] skill selection LLM input (model=test-model):" in stderr_value
    assert stderr_value.count("Build exports") >= 2
    assert "[verbose] skill selection LLM output (model=test-model):" in stderr_value
    assert '"selected_skill_path":' in stderr_value
    assert "[verbose] workflow execution for step 1/2 LLM input" in stderr_value
    assert '"kind": "complete"' in stderr_value
    assert "test-key" not in stderr_value
    assert "[verbose] Prepared execution summary for specify-a-feature" in stderr_value
    assert (worktree_root / output_dir / "skill-execution.json").exists()


def test_run_workflow_chat_prints_selection_follow_up_question(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    worktree_root = repo_root / ".worktrees" / "skill-chat-test"
    skills_dir = worktree_root / "skill-definitions"
    skills_dir.mkdir(parents=True)
    save_skill(_build_skill(), skills_dir / "follow-up-skill.yaml")

    responses: Iterator[dict[str, object]] = iter(
        [
            {
                "selected_skill_path": str(skills_dir / "follow-up-skill.yaml"),
                "selected_skill_reason": "Internal reason not shown to users.",
                "next_question": "   ",
                "ready_to_execute": False,
            },
            {
                "selected_skill_path": str(skills_dir / "follow-up-skill.yaml"),
                "selected_skill_reason": "Internal reason not shown to users.",
                "next_question": "Which requirements should this feature satisfy?",
                "ready_to_execute": False,
            },
            {
                "selected_skill_path": str(skills_dir / "follow-up-skill.yaml"),
                "selected_skill_reason": "Internal reason not shown to users.",
                "next_question": None,
                "ready_to_execute": True,
            },
            {"kind": "complete", "text": "Skill execution complete."},
        ]
    )

    class _FakeOpenAIClient:
        def __init__(self, **_: object) -> None:
            pass

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

    answers = iter(["Build exports", "Requirement details"])
    stdout = io.StringIO()
    stderr = io.StringIO()
    exit_code = run_workflow_chat(
        SkillChatConfig(
            skills_dir=skills_dir,
            repo_root=repo_root,
            output_dir=tmp_path / "generated",
            api_key="test-key",
            model="test-model",
        ),
        input_func=lambda: next(answers),
        stdout=stdout,
        stderr=stderr,
    )

    assert exit_code == 0
    output = stdout.getvalue()
    assert output.count("Matched skill: specify-a-feature") == 1
    assert "Which requirements should this feature satisfy?" in output
    assert "skill selection response needs repair" in stderr.getvalue()
    assert "Internal reason not shown to users." not in output
    assert str(skills_dir) not in output


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
        def __init__(
            self,
            *,
            model: str,
            api_key: str,
            base_url: str,
            **_: object,
        ) -> None:
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
        def __init__(
            self,
            *,
            model: str,
            api_key: str,
            base_url: str,
            **_: object,
        ) -> None:
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
        def __init__(
            self,
            *,
            model: str,
            api_key: str,
            base_url: str,
            **_: object,
        ) -> None:
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
            if call_index in (2, 3, 4):
                raise RuntimeError(
                    "OpenAI request failed with HTTP 429: "
                    '{"error":{"code":"1305","message":"temporarily overloaded"}}'
                )
            if call_index == 5:
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
    sleep_calls: list[float] = []
    monkeypatch.setattr(
        "powdrr_lift.workflow_chat_agent.time.sleep",
        sleep_calls.append,
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
            provider_retry_delay_seconds=0,
        ),
        input_func=iter(["Build exports"]).__next__,
        stdout=stdout,
        stderr=stderr,
    )

    assert exit_code == 0
    assert cast(int, captured["calls"]) == 6
    assert sleep_calls == [0, 0, 0]
    assert "workflow execution for step 2/2 failed for model 'test-model'" in (
        stderr.getvalue()
    )
    assert "automatic retry 3/3" in stderr.getvalue()
    assert "Type 'retry' to try again or 'abort' to stop:" not in stdout.getvalue()
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
        def __init__(
            self,
            *,
            model: str,
            api_key: str,
            base_url: str,
            **_: object,
        ) -> None:
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


def test_empty_prompt_user_action_is_reprompted_until_question_is_present(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    worktree_root = repo_root / ".worktrees" / "skill-chat-test"
    skills_dir = worktree_root / "skill-definitions"
    skills_dir.mkdir(parents=True)
    save_skill(_build_skill(), skills_dir / "specify-a-feature.json")

    calls = 0

    class _FakeOpenAIClient:
        def __init__(self, **_: object) -> None:
            pass

        def complete_json(self, messages: list[dict[str, str]]) -> dict[str, object]:
            nonlocal calls
            calls += 1
            if calls == 1:
                return {
                    "selected_skill_path": str(skills_dir / "specify-a-feature.json"),
                    "selected_skill_reason": "The request is to specify a feature.",
                    "next_question": None,
                    "ready_to_execute": True,
                }
            if calls in {2, 3}:
                return {"kind": "prompt_user", "text": "   "}
            if calls == 4:
                assert "non-empty" in messages[-1]["content"]
                return {
                    "kind": "prompt_user",
                    "text": "What should this feature accomplish?",
                }
            if calls == 5:
                return {"kind": "complete", "text": "Skill execution complete."}
            raise AssertionError(f"Unexpected call count: {calls}")

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
            provider_retry_delay_seconds=0,
        ),
        input_func=iter(["Build exports", "The feature should accomplish X"]).__next__,
        stdout=stdout,
        stderr=stderr,
    )

    assert exit_code == 0
    assert calls == 5
    assert "What should this feature accomplish?" in stdout.getvalue()
    assert "Type 'retry' to try again or 'abort' to stop:" not in stdout.getvalue()
    assert "received an invalid user question" in stderr.getvalue()


def test_workflow_action_repair_retries_empty_provider_response_automatically(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    worktree_root = repo_root / ".worktrees" / "skill-chat-test"
    skills_dir = worktree_root / "skill-definitions"
    skills_dir.mkdir(parents=True)
    save_skill(_build_skill(), skills_dir / "specify-a-feature.json")

    calls = 0

    class _FakeOpenAIClient:
        def __init__(self, **_: object) -> None:
            pass

        def complete_json(self, messages: list[dict[str, str]]) -> dict[str, object]:
            nonlocal calls
            calls += 1
            if calls == 1:
                return {
                    "selected_skill_path": str(skills_dir / "specify-a-feature.json"),
                    "selected_skill_reason": "The request is to specify a feature.",
                    "next_question": None,
                    "ready_to_execute": True,
                }
            if calls == 2:
                return {"kind": "invoke_tool"}
            if calls == 3:
                raise RuntimeError("OpenAI response message content was empty.")
            if calls == 4:
                assert "The response cannot be empty" in messages[-1]["content"]
                raise RuntimeError("OpenAI response message content was empty.")
            if calls == 5:
                raise RuntimeError("OpenAI response message content was empty.")
            if calls == 6:
                return {"kind": "complete", "text": "Skill execution complete."}
            raise AssertionError(f"Unexpected call count: {calls}")

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
            provider_retry_delay_seconds=0,
            provider_retry_attempts=1,
        ),
        input_func=lambda: "retry",
        stdout=stdout,
        stderr=stderr,
    )

    assert exit_code == 0
    assert calls == 6
    assert "returned an empty response" in stderr.getvalue()
    assert "Empty-response context:" in stderr.getvalue()
    assert "corrective-reprompt-attempts=2" in stderr.getvalue()
    assert "LLM request messages:" in stderr.getvalue()
    assert '"role": "user"' in stderr.getvalue()
    assert "LLM response: <empty>" in stderr.getvalue()
    assert "[workflow] Empty-response exchange: prompt=" in stderr.getvalue()
    assert "response=<empty>" in stderr.getvalue()
    assert "Would you like me to retry this LLM request?" in stdout.getvalue()
    assert "treating the step as complete" not in stderr.getvalue()
    assert "automatic repair retry" not in stderr.getvalue()
    assert "Waiting 30 seconds" not in stderr.getvalue()
    assert "Type 'retry' to try again or 'abort' to stop:" not in stdout.getvalue()


def test_work_item_names_match_natural_language_feature_requests(
    tmp_path: Path,
) -> None:
    specifications_root = tmp_path / "docs" / "specs"
    (specifications_root / "interaction-file-log").mkdir(parents=True)
    (specifications_root / "other-feature").mkdir()

    available = _available_work_item_names(tmp_path)

    assert available == ("interaction-file-log", "other-feature")
    assert _match_work_item_names(
        [{"role": "user", "content": "Implement the interaction file log feature."}],
        available,
    ) == ("interaction-file-log",)
    assert _match_work_item_names(
        [{"role": "user", "content": "Implement interaction_file_log."}],
        available,
    ) == ("interaction-file-log",)


def test_fuzzy_match_finds_existing_work_item_and_proposed_pr_specification(
    tmp_path: Path,
) -> None:
    specifications_root = tmp_path / "docs" / "specs" / "interaction-file-log"
    specifications_root.mkdir(parents=True)
    (specifications_root / "proposed-pr-specification.yaml").touch()

    result = execute_fuzzy_match(
        [
            "fuzzy-match",
            "docs/specs/interaction-file-log",
            "-name",
            "proposed PR specification",
            "-type",
            "f",
            "-maxdepth",
            "1",
            "-print",
        ],
        worktree_root=tmp_path,
    )

    assert [match["path"] for match in result["matches"]] == [
        "docs/specs/interaction-file-log/proposed-pr-specification.yaml"
    ]
    assert result["matches"][0]["score"] == 1.0


def test_fuzzy_match_missing_root_is_a_successful_empty_result(
    tmp_path: Path,
) -> None:
    result = execute_fuzzy_match(
        [
            "fuzzy-match",
            "docs/workflows/interaction-file-log",
            "-name",
            "proposed PR",
            "-type",
            "d",
            "-print",
        ],
        worktree_root=tmp_path,
    )

    assert result == {
        "query": "proposed PR",
        "root": str(tmp_path / "docs" / "workflows" / "interaction-file-log"),
        "matches": [],
    }


def test_selection_context_lists_matched_existing_specification_documents(
    tmp_path: Path,
) -> None:
    specifications_root = tmp_path / "docs" / "specs" / "interaction-file-log"
    specifications_root.mkdir(parents=True)
    (specifications_root / "system-specification.yaml").touch()
    (specifications_root / "implementation-specification.yaml").touch()

    messages = _build_selection_messages(
        (),
        [{"role": "user", "content": "Start implementing interaction file log."}],
        tmp_path,
    )
    payload = json.loads(messages[1]["content"])

    assert payload["work_item_context"]["matches"] == ["interaction-file-log"]
    assert payload["work_item_context"]["documents"]["interaction-file-log"] == [
        "docs/specs/interaction-file-log/implementation-specification.yaml",
        "docs/specs/interaction-file-log/system-specification.yaml",
    ]
    assert _available_work_item_documents(
        tmp_path,
        "interaction-file-log",
    ) == tuple(payload["work_item_context"]["documents"]["interaction-file-log"])


def test_catalog_entry_to_data_includes_structured_tool_invocations() -> None:
    skill_path = (
        Path(__file__).resolve().parents[1] / "skill-definitions" / "review-system.yaml"
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
        def __init__(
            self,
            *,
            model: str,
            api_key: str,
            base_url: str,
            **_: object,
        ) -> None:
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
        "rtk",
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


def test_execute_shell_tool_does_not_double_wrap_rtk(
    tmp_path: Path,
) -> None:
    stdout = io.StringIO()
    stderr = io.StringIO()

    with patch("powdrr_lift.workflow_chat_agent.subprocess.run") as run:
        run.return_value.returncode = 0
        run.return_value.stdout = ""
        run.return_value.stderr = ""

        _execute_shell_tool(
            {"command": "rtk git status"},
            worktree_root=tmp_path,
            stdout=stdout,
            stderr=stderr,
            verbose=False,
        )

    assert run.call_args.args[0] == "rtk git status"


def test_execute_shell_tool_verbose_prints_stdout(
    tmp_path: Path,
) -> None:
    stdout = io.StringIO()
    stderr = io.StringIO()

    with patch("powdrr_lift.workflow_chat_agent.subprocess.run") as run:
        run.return_value.returncode = 0
        run.return_value.stdout = "tool stdout\n"
        run.return_value.stderr = ""

        _execute_shell_tool(
            {"command": ["echo", "tool stdout"]},
            worktree_root=tmp_path,
            stdout=stdout,
            stderr=stderr,
            verbose=True,
        )

    assert "[verbose] Shell tool stdout:\ntool stdout" in stderr.getvalue()


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


def test_local_model_cache_uses_primary_project_root_for_worktree() -> None:
    project_root = Path("/Users/test/project")
    worktree_root = project_root / ".worktrees" / "skill-chat"

    assert (
        _resolve_project_root(project_root / ".worktrees" / "other", worktree_root)
        == project_root
    )


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
                                        "skill-definitions/specify-a-feature.yaml"
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
        "max_tokens": 32768,
        "system": "system prompt",
        "messages": [
            {
                "role": "user",
                "content": [{"type": "text", "text": "hello"}],
            }
        ],
    }
    assert response == {
        "selected_skill_path": "skill-definitions/specify-a-feature.yaml"
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
