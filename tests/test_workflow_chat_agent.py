from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import types
from collections.abc import Iterator
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, TextIO, cast
from unittest.mock import patch
from urllib.request import Request

import pytest
import yaml

from powdrr_lift.cli import main
from powdrr_lift.core import (
    Skill,
    SkillStep,
    SkillStepGate,
    SkillStepInput,
    SkillStepOutput,
    SkillStepPreStep,
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
)
from powdrr_lift.core.system_specification import validate_system_specification_yaml
from powdrr_lift.core.workflow_task_specification import (
    TaskStatus,
    load_ready_workflow_tasks,
    load_workflow_tasks,
    save_workflow_task,
    select_ready_workflow_tasks,
)
from powdrr_lift.errors import PowdrrExecutionError
from powdrr_lift.file_management import FileManagementError, manage_worktree_file
from powdrr_lift.fuzzy_match import execute_fuzzy_match
from powdrr_lift.test_failure_packet import build_test_failure_packet
from powdrr_lift.workflow_chat_agent import (
    ALL_LLM_TYPES,
    ALL_PROVIDERS,
    DEEPINFRA_CHEAP_LLM_MAPPINGS,
    DEEPINFRA_LLM_MAPPINGS,
    OPENROUTER_LLM_MAPPINGS,
    ZAI_LLM_MAPPINGS,
    AnthropicChatClient,
    LLMModelLimits,
    LLMModelMapping,
    LLMProviderRoles,
    LocalLlamaChatClient,
    LocalModelRuntimeError,
    OpenAIChatClient,
    SkillCatalogEntry,
    SkillChatConfig,
    SkillChatEdit,
    WorkflowContext,
    _action_repair_prompt,
    _action_system_prompt,
    _apply_file_edits,
    _apply_yaml_operations,
    _available_work_item_documents,
    _available_work_item_names,
    _backup_model_for,
    _build_selection_messages,
    _build_step_execution_messages,
    _catalog_entry_to_data,
    _command_matches_invocation,
    _complete_json_with_model_fallback,
    _current_file_context,
    _discover_validation_obligations,
    _empty_pull_request_error,
    _execute_shell_tool,
    _execution_events_for_prompt,
    _handle_workflow_action_edit,
    _handle_workflow_action_file_management,
    _handle_workflow_action_read_document,
    _initial_model_for_provider,
    _latest_deterministic_pre_step,
    _latest_execution_event_for_prompt,
    _LLMExchangeRecordingClient,
    _load_workflow_context,
    _long_context_backup_for,
    _match_work_item_names,
    _modular_action_system_prompt,
    _normalize_cache_usage,
    _parse_action_response,
    _parse_json_object,
    _parse_workflow_action_gather_context,
    _prompt_durable_facts,
    _prompt_step_context,
    _prompt_transcript,
    _prompt_user,
    _record_durable_fact,
    _record_dynamic_validation_result,
    _request_token_budget,
    _resolve_api_key,
    _resolve_base_url,
    _resolve_llm_mapping,
    _resolve_llm_model,
    _resolve_local_model_context,
    _resolve_local_model_path,
    _resolve_project_root,
    _resolve_provider,
    _resolve_provider_roles,
    _resolve_skill_path,
    _resolve_worktree_context,
    _resolve_worktree_for_request,
    _run_deterministic_pre_step,
    _run_gate,
    _serialize_messages,
    _step_actions,
    _validate_dynamic_validation_gate_action,
    _validate_internal_command,
    _validate_user_question,
    _validate_workflow_action_for_step,
    _validate_workflow_handoff,
    _validate_workflow_step_transition,
    _validation_actions_match,
    _validation_issue_fingerprint,
    _ValidationGateState,
    _ValidationObligation,
    _workflow_action_material_state,
    _workflow_action_progress_status,
    _workflow_edit_failure_feedback,
    _workflow_handoff_inputs,
    _WorkflowEditRangeError,
    _WorkflowExecutionState,
    _WorkflowProgressDisplay,
    _WorkflowStructuredDocumentError,
    _WorkflowToolValidationError,
    _WorkflowYamlEditError,
    _worktree_reuse_decision,
    available_workflow_providers,
    choose_workflow_provider,
    dependency_backed_command_variants,
    download_local_qwen_model,
    missing_executable_output,
    run_workflow_chat,
)
from powdrr_lift.workflow_llm import WorkflowAction, workflow_action_summary

# ruff: noqa: E501


def test_goto_step_action_requires_a_step_id() -> None:
    action = _parse_action_response(
        {
            "action": "goto_step",
            "step_id": "process-next-item",
            "decisions_and_context": "More items remain.",
        }
    )

    assert action.kind == "goto_step"
    assert action.step_id == "process-next-item"
    with pytest.raises(RuntimeError, match="must include step_id"):
        _parse_action_response({"action": "goto_step"})


def test_edit_action_repair_accepts_nested_parameters_and_single_edit() -> None:
    action = _parse_action_response(
        {
            "action": "edit",
            "parameters": {
                "file_path": "docs/proposals/example/input.json",
                "edits": {
                    "kind": "replace",
                    "start_line": 1,
                    "end_line": 1,
                    "text": "{}\n",
                },
            },
        }
    )

    assert action.kind == "edit"
    assert action.file_path == "docs/proposals/example/input.json"
    assert len(action.edits) == 1


def test_execution_events_for_prompt_compacts_results_without_mutating_summary_data() -> (
    None
):
    events = [
        {
            "kind": "invoke_tool",
            "parameters": {"command": "git diff"},
            "result": {"stdout": "a large result"},
            "step_index": 2,
        }
    ]

    prompt_events = _execution_events_for_prompt(events)

    assert prompt_events == [
        {
            "kind": "invoke_tool",
            "parameters": {"command": "git diff"},
            "step_index": 2,
        }
    ]
    assert events[0]["result"] == {"stdout": "a large result"}


def test_latest_execution_event_keeps_only_the_latest_result_for_prompt() -> None:
    events = [
        {"kind": "edit", "result": {"file_path": "old.py"}},
        {"kind": "invoke_tool", "result": {"stdout": "latest output"}},
    ]

    assert _latest_execution_event_for_prompt(events) == events[-1]


def test_prompt_transcript_omits_action_observations_represented_by_events() -> None:
    transcript = [
        {"role": "user", "content": "Implement the change."},
        {"role": "assistant", "content": '{"action":"read_document"}'},
        {"role": "user", "content": '{"document_context":{"lines":[]}}'},
        {"role": "assistant", "content": "I need one decision."},
        {"role": "user", "content": "Use the existing interface."},
    ]

    assert _prompt_transcript(transcript) == [
        {"role": "user", "content": "Implement the change."},
        {"role": "assistant", "content": "I need one decision."},
        {"role": "user", "content": "Use the existing interface."},
    ]


def test_workflow_action_summary_explains_action_and_reason() -> None:
    summary = workflow_action_summary(
        WorkflowAction(
            kind="invoke_tool",
            tool="shell",
            decisions_and_context="The file must be inspected before editing.",
        )
    )

    assert summary == (
        "invoke_tool (shell) — The file must be inspected before editing."
    )


def test_prompt_transcript_keeps_request_and_bounds_history() -> None:
    transcript = [
        {"role": "user", "content": "Initial request"},
        *({"role": "user", "content": "x" * 9000} for _ in range(40)),
    ]

    prompt_transcript = _prompt_transcript(transcript)

    assert prompt_transcript[0]["content"] == "Initial request"
    assert len(prompt_transcript) <= 32
    assert sum(len(message["content"]) for message in prompt_transcript) <= 24000
    assert "Earlier workflow transcript omitted" in prompt_transcript[1]["content"]


def test_current_file_context_cache_reuses_unchanged_file(tmp_path: Path) -> None:
    path = tmp_path / "example.txt"
    path.write_text("one\ntwo\n", encoding="utf-8")
    cache: dict[tuple[str, int, int], dict[str, object]] = {}

    first = _current_file_context(tmp_path, path, cache=cache)
    second = _current_file_context(tmp_path, path, cache=cache)

    assert first is second
    assert len(cache) == 1

    path.write_text("one\ntwo\nthree\n", encoding="utf-8")
    updated = _current_file_context(tmp_path, path, cache=cache)
    assert updated is not None
    assert updated is not first
    assert updated["line_count"] == 3


def test_current_file_context_omits_large_files_without_truncating(
    tmp_path: Path,
) -> None:
    path = tmp_path / "large.txt"
    path.write_text(
        "\n".join(f"line-{index}" for index in range(1, 501)), encoding="utf-8"
    )

    context = _current_file_context(tmp_path, path)

    assert context is not None
    assert context["line_count"] == 500
    assert context["content_omitted"] is True
    assert context["lines"] == []


def test_prompt_step_context_keeps_newest_entries_and_bounds_size() -> None:
    context = [f"entry-{index}-" + "x" * 2000 for index in range(40)]

    prompt_context = _prompt_step_context(context)

    assert len(prompt_context) <= 24
    assert sum(len(value) for value in prompt_context) <= 16000 + 80
    assert prompt_context[-1].startswith("entry-39")
    assert not prompt_context[0].startswith("[Earlier step context omitted")


def test_durable_facts_deduplicate_decisions_and_are_prompt_ready(
    tmp_path: Path,
) -> None:
    state = _WorkflowExecutionState(
        selected_skill=SkillCatalogEntry(
            tmp_path / "skill.json",
            Skill(name="test", when_to_use=(), steps=(SkillStep(description="Run"),)),
        ),
        transcript=[],
        execution_events=[],
        execution_context=[],
        step_index=0,
        worktree_root=tmp_path,
    )

    _record_durable_fact(
        state,
        "Use the exact specification path.",
        kind="decision",
        source="gather_context",
    )
    _record_durable_fact(
        state,
        "  Use the exact specification path. ",
        kind="decision",
        source="next_step",
    )

    assert len(state.durable_facts) == 1
    assert state.execution_context == ["Use the exact specification path."]
    assert _prompt_durable_facts(state.durable_facts) == [
        {
            "value": "Use the exact specification path.",
            "kind": "decision",
            "source": "gather_context",
            "step_index": 0,
        }
    ]


def test_step_execution_prompt_includes_capability_catalogs_only_when_needed(
    tmp_path: Path,
) -> None:
    ordinary_step = SkillStep(
        description="Inspect the current implementation.",
        details="Use the declared shell command and report what it finds.",
        tool_invocations=(SkillToolInvocation(tool="shell", command=("rg", "TODO")),),
    )
    gather_step = SkillStep(
        description="Gather context and invoke the nested review skill.",
        details="Use gather_context before continuing.",
        uses_skills=("review-system",),
        prompt_catalogs=("context_types", "skills"),
    )
    ordinary_skill = SkillCatalogEntry(
        tmp_path / "ordinary.json",
        Skill(name="ordinary", when_to_use=(), steps=(ordinary_step,)),
    )
    catalog = (
        ordinary_skill,
        SkillCatalogEntry(
            tmp_path / "review.json",
            Skill(name="review-system", when_to_use=(), steps=(gather_step,)),
        ),
    )

    ordinary_prompt = json.loads(
        _build_step_execution_messages(
            selected_skill=ordinary_skill,
            current_step=ordinary_step,
            current_step_index=0,
            transcript=[],
            execution_events=[],
            execution_context=[],
            current_file_path=None,
            worktree_root=tmp_path,
            catalog=catalog,
            workflow_context=WorkflowContext(
                worktree_root=tmp_path,
                branch_name="feature/test",
                request="Inspect the implementation",
            ),
        )[1]["content"]
    )
    assert "available_context_types" not in ordinary_prompt
    assert "available_skills" not in ordinary_prompt
    assert ordinary_prompt["worktree_root"] == "."
    assert "worktree_root" not in ordinary_prompt["previous_workflow_context"]
    ordinary_system_prompt = _action_system_prompt(current_step=ordinary_step)
    assert "Use next_step when the current step is complete" in ordinary_system_prompt
    assert "Use complete when the skill is finished" in ordinary_system_prompt
    assert "apply to every step" in ordinary_system_prompt

    output_step = SkillStep(
        description="Capture the feature name.",
        outputs=(
            SkillStepOutput(
                name="work_item_name",
                type="string",
                required_for_next_step=True,
            ),
        ),
    )
    output_prompt = _modular_action_system_prompt(output_step)
    assert '"outputs"' in output_prompt
    assert '"work_item_name":"interaction-file-log"' in output_prompt

    ordinary_system_prompt = _build_step_execution_messages(
        selected_skill=ordinary_skill,
        current_step=ordinary_step,
        current_step_index=0,
        transcript=[],
        execution_events=[],
        execution_context=[],
        current_file_path=None,
        worktree_root=tmp_path,
        catalog=catalog,
    )[0]["content"]
    assert "Context guidance:" not in ordinary_system_prompt
    assert "Nested-skill guidance:" not in ordinary_system_prompt
    assert "entity-relationships" not in ordinary_system_prompt
    assert (
        "prompt_user requires the question in the text field" in ordinary_system_prompt
    )

    gather_prompt = json.loads(
        _build_step_execution_messages(
            selected_skill=ordinary_skill,
            current_step=gather_step,
            current_step_index=0,
            transcript=[],
            execution_events=[],
            execution_context=[],
            current_file_path=None,
            worktree_root=tmp_path,
            catalog=catalog,
        )[1]["content"]
    )
    assert "available_context_types" in gather_prompt
    assert "available_skills" in gather_prompt

    gather_system_prompt = _build_step_execution_messages(
        selected_skill=ordinary_skill,
        current_step=gather_step,
        current_step_index=0,
        transcript=[],
        execution_events=[],
        execution_context=[],
        current_file_path=None,
        worktree_root=tmp_path,
        catalog=catalog,
    )[0]["content"]
    assert "Context guidance:" in gather_system_prompt
    assert "Nested-skill guidance:" in gather_system_prompt


def test_execution_event_prompt_uses_only_current_step_events() -> None:
    events = [
        {
            "kind": "invoke_tool",
            "tool": "internal",
            "parameters": {"command": ["powdrr-lift", "system-specification"]},
            "step_index": 0,
        },
        {
            "kind": "invoke_tool",
            "tool": "internal",
            "parameters": {"command": ["powdrr-lift", "evaluate"]},
            "step_index": 1,
        },
    ]

    current = _execution_events_for_prompt(events, 1)
    latest = _latest_execution_event_for_prompt(events, 1)

    assert len(current) == 2
    assert "parameters" not in current[0]
    assert current[1]["parameters"]["command"] == ["powdrr-lift", "evaluate"]
    assert latest is not None
    assert latest["parameters"]["command"] == ["powdrr-lift", "evaluate"]


def test_modular_action_prompt_requires_invoke_skill_for_nested_steps() -> None:
    prompt = _modular_action_system_prompt(
        SkillStep(
            description="Run the preparation skill.", uses_skills=("finish-pr-prep",)
        )
    )

    assert "use invoke_skill, not invoke_tool or an internal CLI command" in prompt
    assert '"skill":"finish-pr-prep"' in prompt


def test_explicit_empty_step_does_not_infer_legacy_actions() -> None:
    actions = _step_actions(
        SkillStep(
            description="Wait for the engine-owned result.",
            details="Legacy inference must not reopen this contract.",
            uses_skills=("unrelated-skill",),
            actions_declared=True,
        )
    )

    assert actions == (
        ("prompt_user", "Ask one necessary human question."),
        ("next_step", "Advance only after this step is complete."),
    )


def test_next_step_is_prompted_without_required_outputs() -> None:
    actions = _step_actions(SkillStep(description="Finish the step."))

    assert any(name == "next_step" for name, _ in actions)


def test_explicit_step_contract_rejects_undeclared_complete() -> None:
    with pytest.raises(_WorkflowToolValidationError, match="not allowed"):
        _validate_workflow_action_for_step(
            _parse_action_response({"action": "complete"}),
            SkillStep(
                description="Use the declared handoff.",
                actions=("read_document",),
            ),
        )


def test_explicit_step_contract_rejects_undeclared_internal_tool() -> None:
    with pytest.raises(_WorkflowToolValidationError, match="not allowed"):
        _validate_workflow_action_for_step(
            _parse_action_response(
                {
                    "action": "invoke_tool",
                    "tool": "internal",
                    "parameters": {"command": ["powdrr-lift", "help"]},
                }
            ),
            SkillStep(description="Only read documents.", actions=("read_document",)),
        )


def test_deterministic_shell_pre_step_accepts_empty_successful_result(
    tmp_path: Path,
) -> None:
    subprocess.run(
        ["git", "init", "--quiet"],
        cwd=tmp_path,
        check=True,
    )
    step = SkillStep(
        description="Inspect staged files.",
        step_type="invoke_tool",
        pre_step=SkillStepPreStep(
            action="invoke_tool",
            template={
                "tool": "shell",
                "command": ["git", "diff", "--cached", "--name-only"],
            },
        ),
    )
    events: list[dict[str, Any]] = []

    _run_deterministic_pre_step(
        step,
        skill_name="finish-pr-prep",
        worktree_root=tmp_path,
        execution_events=events,
        execution_context=[],
        handoff_records={},
        step_index=0,
        workflow_context=None,
        stdout=io.StringIO(),
        stderr=io.StringIO(),
    )

    assert events[0]["result"]["returncode"] == 0
    assert events[0]["result"]["stdout"].strip() == ""


def test_gate_reports_fresh_result_separately_from_llm_commentary(
    tmp_path: Path,
) -> None:
    step = SkillStep(
        id="evaluate-specification",
        description="Evaluate the specification.",
        step_type="gate",
        pre_step=SkillStepPreStep(
            action="invoke_tool",
            template={
                "tool": "shell",
                "command": ["printf", "issues remain"],
            },
        ),
        gate=SkillStepGate(
            outcome={"path": "returncode", "equals": 0},
            goto_step="repair-specification",
            retry_context="Repair the reported issues.",
        ),
    )
    stderr = io.StringIO()

    passed = _run_gate(
        step,
        skill_name="specify-a-feature",
        worktree_root=tmp_path,
        execution_events=[],
        execution_context=[],
        handoff_records={},
        step_index=3,
        workflow_context=None,
        stdout=io.StringIO(),
        stderr=stderr,
        verbose=False,
    )

    assert passed is True
    output = stderr.getvalue()
    assert (
        "Workflow gate evaluation (specify-a-feature/evaluate-specification): passed"
        in output
    )
    assert '"returncode": 0' in output
    assert "issues remain" in output


def test_invoke_tool_runs_gather_context_pre_step_once(
    tmp_path: Path,
) -> None:
    current_root = tmp_path / "docs" / "current"
    current_root.mkdir(parents=True)
    (current_root / "requirements.yaml").write_text(
        "requirements:\n"
        "- id: requirement-1\n"
        "  description: Keep the selected requirement.\n",
        encoding="utf-8",
    )
    step = SkillStep(
        description="Filter the gathered requirements.",
        step_type="invoke_tool",
        pre_step=SkillStepPreStep(
            action="gather_context",
            template={
                "feature_id": "<feature-id>",
                "types": ["requirements"],
                "keywords": ["selected"],
            },
        ),
        outputs=(
            SkillStepOutput(
                name="filtered_requirements",
                type="any",
                required_for_next_step=True,
            ),
        ),
    )
    state = _WorkflowExecutionState(
        selected_skill=SkillCatalogEntry(
            tmp_path / "skill.json",
            Skill(name="filter", when_to_use=(), steps=(step,)),
        ),
        transcript=[],
        execution_events=[],
        execution_context=[],
        step_index=0,
        worktree_root=tmp_path,
        handoff_records={
            "feature_id": {
                "name": "feature_id",
                "value": "display-related-photos",
            }
        },
    )

    _run_deterministic_pre_step(
        step,
        skill_name="filter",
        worktree_root=state.worktree_root,
        execution_events=state.execution_events,
        execution_context=state.execution_context,
        handoff_records=state.handoff_records,
        step_index=state.step_index,
        workflow_context=WorkflowContext(worktree_root=tmp_path),
    )
    _run_deterministic_pre_step(
        step,
        skill_name="filter",
        worktree_root=state.worktree_root,
        execution_events=state.execution_events,
        execution_context=state.execution_context,
        handoff_records=state.handoff_records,
        step_index=state.step_index,
        workflow_context=WorkflowContext(worktree_root=tmp_path),
    )

    assert len(state.execution_events) == 1
    event = state.execution_events[0]
    assert event["kind"] == "deterministic_pre_step"
    assert event["skill_name"] == "filter"
    assert (
        _latest_deterministic_pre_step(
            state.execution_events,
            skill_name="different-skill",
            step_index=0,
        )
        is None
    )
    assert event["template"]["feature_id"] == "display-related-photos"
    assert event["result"]["matches"]

    prompt = json.loads(
        _build_step_execution_messages(
            selected_skill=state.selected_skill,
            current_step=step,
            current_step_index=0,
            transcript=[],
            execution_events=state.execution_events,
            execution_context=state.execution_context,
            handoff_records=state.handoff_records,
            current_file_path=None,
            worktree_root=tmp_path,
            catalog=(state.selected_skill,),
        )[1]["content"]
    )
    assert prompt["deterministic_context"]["source"] == "gather_context"
    assert prompt["deterministic_context"]["scope"]["feature_id"] == (
        "display-related-photos"
    )
    assert "details" in prompt["current_step"]
    assert prompt["deterministic_context"]["result"]["matches"]


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


def test_workflow_tool_action_must_be_declared_by_current_step() -> None:
    step = SkillStep(
        description="Inspect the repository.",
        tool_invocations=(
            SkillToolInvocation(tool="shell", command=("rg", "--files")),
        ),
    )

    _validate_workflow_action_for_step(
        _parse_action_response(
            {
                "action": "invoke_tool",
                "tool": "shell",
                "parameters": {"command": ["rg", "--files"]},
            }
        ),
        step,
    )

    with pytest.raises(RuntimeError, match="command.*does not match"):
        _validate_workflow_action_for_step(
            _parse_action_response(
                {
                    "action": "invoke_tool",
                    "tool": "shell",
                    "parameters": {"command": ["rg", "--files", "extra"]},
                }
            ),
            step,
        )

    with pytest.raises(RuntimeError, match="requires a successful tool invocation"):
        _validate_workflow_step_transition(
            _parse_action_response({"action": "next_step"}),
            step,
            [],
            0,
        )

    _validate_workflow_step_transition(
        _parse_action_response({"action": "next_step"}),
        step,
        [
            {
                "kind": "invoke_tool",
                "tool": "shell",
                "parameters": {"command": ["rg", "--files"]},
                "result": {"returncode": 0},
                "step_index": 0,
            }
        ],
        0,
    )

    with pytest.raises(RuntimeError, match="requires a successful tool invocation"):
        _validate_workflow_step_transition(
            _parse_action_response({"action": "next_step"}),
            step,
            [
                {
                    "kind": "invoke_tool",
                    "tool": "shell",
                    "parameters": {"command": ["rg", "--files"]},
                    "result": {"returncode": 1},
                    "step_index": 0,
                }
            ],
            0,
        )

    commit_step = replace(
        step,
        tool_invocations=(
            SkillToolInvocation(
                tool="internal", command=("powdrr-lift", "repository-state")
            ),
            SkillToolInvocation(
                tool="shell", command=("git", "commit", "-m", "<message>")
            ),
        ),
    )
    with pytest.raises(RuntimeError, match="requires a successful tool invocation"):
        _validate_workflow_step_transition(
            _parse_action_response({"action": "next_step"}),
            commit_step,
            [
                {
                    "kind": "invoke_tool",
                    "tool": "internal",
                    "parameters": {"command": ["powdrr-lift", "repository-state"]},
                    "result": {},
                    "step_index": 0,
                }
            ],
            0,
        )

    wildcard_step = replace(
        step,
        tool_invocations=(
            SkillToolInvocation(tool="shell", command=("rg", "--path=<path>")),
        ),
    )
    _validate_workflow_action_for_step(
        _parse_action_response(
            {
                "action": "invoke_tool",
                "tool": "shell",
                "parameters": {"command": ["rg", "--path=src"]},
            }
        ),
        wildcard_step,
    )

    pull_request_step = replace(
        step,
        tool_invocations=(
            SkillToolInvocation(
                tool="shell",
                command=(
                    "gh",
                    "pr",
                    "create",
                    "--draft",
                    "--body",
                    "<populated-pr-description>",
                ),
            ),
        ),
    )
    _validate_workflow_action_for_step(
        _parse_action_response(
            {
                "action": "invoke_tool",
                "tool": "shell",
                "parameters": {
                    "command": [
                        "gh",
                        "pr",
                        "create",
                        "--draft",
                        "--body",
                        "## Summary\n\nA detailed pull request description.",
                    ]
                },
            }
        ),
        pull_request_step,
    )

    with pytest.raises(RuntimeError, match="fuzzy-match.*not supported"):
        _validate_workflow_action_for_step(
            _parse_action_response(
                {
                    "action": "invoke_tool",
                    "tool": "fuzzy-match",
                    "parameters": {"command": ["fuzzy-match", ".", "-name", "src"]},
                }
            ),
            step,
        )

    with pytest.raises(RuntimeError, match="explicitly supports: none"):
        _validate_workflow_action_for_step(
            _parse_action_response(
                {
                    "action": "invoke_tool",
                    "tool": "shell",
                    "parameters": {"command": ["rg", "--files"]},
                }
            ),
            SkillStep(description="Report the result."),
        )


def test_step_allowed_actions_reject_direct_edit() -> None:
    step = SkillStep(
        description="Produce a deferred edit.",
        actions=("read_document",),
    )

    with pytest.raises(RuntimeError, match="edit action is not allowed"):
        _validate_workflow_action_for_step(
            _parse_action_response(
                {
                    "action": "edit",
                    "file_path": "src/example.py",
                    "edits": [
                        {
                            "kind": "replace",
                            "start_line": 1,
                            "end_line": 1,
                            "text": "replacement",
                        }
                    ],
                }
            ),
            step,
        )


def test_workflow_can_advance_after_empty_gather_context_result() -> None:
    validated = False
    try:
        _validate_workflow_step_transition(
            _parse_action_response({"action": "next_step"}),
            SkillStep(description="Discover optional PR-preparation tools."),
            [{"kind": "gather_context", "result": {"matches": []}}],
            0,
        )
    except RuntimeError:
        pass
    else:
        validated = True

    assert validated is True


def test_goto_step_can_only_target_a_prior_step() -> None:
    prior = SkillStep(id="prior", description="Repeat this step.")
    current = SkillStep(id="current", description="Current step.")
    later = SkillStep(id="later", description="Later step.")
    selected_skill = SkillCatalogEntry(
        Path("skill.yaml"),
        Skill(name="transitions", when_to_use=(), steps=(prior, current, later)),
    )
    state = _WorkflowExecutionState(
        selected_skill=selected_skill,
        transcript=[],
        execution_events=[],
        execution_context=[],
        step_index=1,
        worktree_root=Path("."),
    )

    _validate_workflow_step_transition(
        _parse_action_response({"action": "goto_step", "step_id": "prior"}),
        current,
        [],
        1,
        state=state,
    )
    for target in ("current", "later"):
        with pytest.raises(RuntimeError, match="only a prior step"):
            _validate_workflow_step_transition(
                _parse_action_response({"action": "goto_step", "step_id": target}),
                current,
                [],
                1,
                state=state,
            )


def test_complete_cannot_bypass_a_later_gate() -> None:
    current = SkillStep(id="current", description="Current step.")
    later_gate = SkillStep(
        id="validate",
        description="Validate the result.",
        step_type="gate",
        gate=SkillStepGate(
            outcome={"path": "returncode", "equals": 0},
            goto_step="current",
            retry_context="Repair the result.",
        ),
    )
    selected_skill = SkillCatalogEntry(
        Path("skill.yaml"),
        Skill(name="gated", when_to_use=(), steps=(current, later_gate)),
    )
    state = _WorkflowExecutionState(
        selected_skill=selected_skill,
        transcript=[],
        execution_events=[],
        execution_context=[],
        step_index=0,
        worktree_root=Path("."),
    )

    with pytest.raises(RuntimeError, match="later gate steps remain"):
        _validate_workflow_step_transition(
            _parse_action_response({"action": "complete"}),
            current,
            [],
            0,
            state=state,
        )

    state.step_index = 1
    _validate_workflow_step_transition(
        _parse_action_response({"action": "complete"}),
        later_gate,
        [],
        1,
        state=state,
    )


def test_dynamic_validation_gate_cannot_be_bypassed() -> None:
    step = SkillStep(
        description="Run discovered checks.",
        validation_gate={
            "id": "checks",
            "discovery": {"action": {"action": "gather_context"}},
            "obligations": {
                "source": "matches",
                "filter": {"section": "tools"},
                "id": "item.id",
                "action": "item.validation_action",
            },
        },
    )
    selected_skill = SkillCatalogEntry(
        Path("skill.yaml"),
        Skill(name="validation", when_to_use=(), steps=(step,)),
    )
    state = _WorkflowExecutionState(
        selected_skill=selected_skill,
        transcript=[],
        execution_events=[],
        execution_context=[],
        step_index=0,
        worktree_root=Path("."),
    )
    next_step = _parse_action_response({"action": "next_step"})

    with pytest.raises(RuntimeError, match="cannot be skipped"):
        _validate_workflow_step_transition(
            next_step,
            step,
            [],
            0,
            state=state,
        )

    state.validation_gates["checks"] = _ValidationGateState(
        step_index=0,
        discovered=True,
        epoch=1,
        obligations={
            "pytest": _ValidationObligation(
                obligation_id="pytest",
                expected_action={
                    "kind": "invoke_tool",
                    "tool": "shell",
                    "parameters": {"command": ["uv", "run", "pytest"]},
                },
                source={"id": "pytest", "template": "uv run pytest"},
            )
        },
    )
    with pytest.raises(RuntimeError, match="Still pending: pytest"):
        _validate_workflow_step_transition(
            next_step,
            step,
            [],
            0,
            state=state,
        )

    state.validation_gates["checks"].correction_required = True
    with pytest.raises(RuntimeError, match="Apply corrective steps"):
        _validate_workflow_step_transition(
            next_step,
            step,
            [],
            0,
            state=state,
        )

    _validate_dynamic_validation_gate_action(
        _parse_action_response(
            {
                "action": "gather_context",
                "types": ["requirements"],
            }
        ),
        state,
        step,
    )

    state.validation_gates["checks"].correction_required = False
    obligation = state.validation_gates["checks"].obligations["pytest"]
    obligation.status = "passed"
    _validate_workflow_step_transition(
        next_step,
        step,
        [],
        0,
        state=state,
    )

    with pytest.raises(RuntimeError, match="not one of the discovered obligations"):
        _validate_dynamic_validation_gate_action(
            _parse_action_response(
                {
                    "action": "invoke_tool",
                    "tool": "shell",
                    "parameters": {"command": ["uv", "run", "ruff"]},
                }
            ),
            state,
            step,
        )


def test_dynamic_validation_gate_rejects_commands_before_discovery() -> None:
    step = SkillStep(
        description="Run discovered checks.",
        validation_gate={
            "id": "checks",
            "discovery": {"action": {"action": "gather_context"}},
            "obligations": {
                "source": "matches",
                "filter": {"section": "tools"},
                "id": "item.id",
                "action": "item.validation_action",
            },
        },
    )
    state = _WorkflowExecutionState(
        selected_skill=SkillCatalogEntry(
            Path("skill.yaml"), Skill(name="validation", when_to_use=(), steps=(step,))
        ),
        transcript=[],
        execution_events=[],
        execution_context=[],
        step_index=0,
        worktree_root=Path("."),
    )

    with pytest.raises(RuntimeError, match="Do not invoke a validation command"):
        _validate_dynamic_validation_gate_action(
            _parse_action_response(
                {
                    "action": "invoke_tool",
                    "tool": "internal",
                    "parameters": {
                        "command": [
                            "powdrr-lift",
                            "discover-validation-obligations",
                        ]
                    },
                }
            ),
            state,
            step,
        )


def test_dynamic_validation_gate_registers_valid_handoff_before_invoking() -> None:
    step = SkillStep(
        description="Run discovered checks.",
        validation_gate={
            "id": "checks",
            "discovery": {"input_ref": "validation_tool_obligations"},
            "obligations": {
                "source": "matches",
                "filter": {"section": "tools"},
                "id": "item.id",
                "action": "item.validation_action",
            },
        },
    )
    selected_skill = SkillCatalogEntry(
        Path("skill.yaml"), Skill(name="validation", when_to_use=(), steps=(step,))
    )
    state = _WorkflowExecutionState(
        selected_skill=selected_skill,
        transcript=[],
        execution_events=[],
        execution_context=[],
        step_index=0,
        worktree_root=Path("."),
        handoff_records={
            "validation_tool_obligations": {
                "name": "validation_tool_obligations",
                "value": [
                    {
                        "id": "pytest",
                        "validation_action": {
                            "kind": "invoke_tool",
                            "tool": "shell",
                            "parameters": {"command": ["uv", "run", "pytest"]},
                        },
                    }
                ],
            }
        },
    )

    _validate_dynamic_validation_gate_action(
        _parse_action_response(
            {
                "action": "invoke_tool",
                "tool": "shell",
                "parameters": {"command": ["uv", "run", "pytest"]},
            }
        ),
        state,
        step,
    )

    assert state.validation_gates["checks"].discovered is True
    assert state.validation_gates["checks"].discovery_action == {
        "kind": "handoff",
        "name": "validation_tool_obligations",
    }


def test_dynamic_validation_gate_matches_shell_command_string_and_argument_list() -> (
    None
):
    expected = {
        "action": "invoke_tool",
        "tool": "shell",
        "parameters": {"command": "uv run --extra dev ruff format ."},
    }
    actual = {
        "action": "invoke_tool",
        "tool": "shell",
        "parameters": {
            "command": ["uv", "run", "--extra", "dev", "ruff", "format", "."]
        },
    }

    assert _validation_actions_match(expected, actual)
    assert _validation_actions_match(expected, expected)


def test_dynamic_validation_gates_are_multiple_and_action_generic() -> None:
    first_step = SkillStep(
        description="Run repository checks.",
        validation_gate={
            "id": "repository-checks",
            "discovery": {
                "action": {"action": "gather_context"},
            },
            "obligations": {
                "source": "matches",
                "filter": {"section": "tools"},
                "id": "item.id",
                "action": "item.validation_action",
            },
        },
    )
    second_step = SkillStep(
        description="Run deployment checks.",
        validation_gate={
            "id": "deployment-checks",
            "discovery": {
                "action": {"action": "gather_context"},
            },
            "obligations": {
                "source": "matches",
                "filter": {"section": "tools"},
                "id": "item.id",
                "action": "item.validation_action",
            },
        },
    )
    state = _WorkflowExecutionState(
        selected_skill=SkillCatalogEntry(
            Path("skill.yaml"),
            Skill(name="validation", when_to_use=(), steps=(first_step, second_step)),
        ),
        transcript=[],
        execution_events=[],
        execution_context=[],
        step_index=0,
        worktree_root=Path("."),
    )
    result = {
        "matches": [
            {
                "section": "tools",
                "item": {
                    "id": "check-one",
                    "validation_action": {
                        "action": "invoke_tool",
                        "tool": "internal",
                        "parameters": {"command": ["repository-state"]},
                    },
                },
            }
        ]
    }

    _discover_validation_obligations(
        result, state=state, gate=first_step, gate_step_index=0
    )
    _discover_validation_obligations(
        result, state=state, gate=second_step, gate_step_index=1
    )

    assert set(state.validation_gates) == {"repository-checks", "deployment-checks"}
    assert (
        state.validation_gates["deployment-checks"]
        .obligations["check-one"]
        .expected_action["tool"]
        == "internal"
    )


def test_validation_failure_context_contains_exact_tool_result() -> None:
    step = SkillStep(
        description="Run discovered checks.",
        validation_gate={"id": "checks"},
    )
    selected_skill = SkillCatalogEntry(
        Path("skill.yaml"),
        Skill(name="validation", when_to_use=(), steps=(step,)),
    )
    state = _WorkflowExecutionState(
        selected_skill=selected_skill,
        transcript=[],
        execution_events=[
            {
                "kind": "invoke_tool",
                "tool": "shell",
                "parameters": {"command": ["uv", "run", "mypy"]},
                "result": {
                    "command": "uv run mypy",
                    "returncode": 1,
                    "stdout": "src/example.py:1: error",
                    "stderr": "Found 1 error",
                },
            }
        ],
        execution_context=[],
        step_index=0,
        worktree_root=Path("."),
    )
    state.validation_gates["checks"] = _ValidationGateState(
        step_index=0,
        discovered=True,
        epoch=1,
        obligations={
            "mypy": _ValidationObligation(
                obligation_id="mypy",
                expected_action={
                    "kind": "invoke_tool",
                    "tool": "shell",
                    "parameters": {"command": ["uv", "run", "mypy"]},
                },
                source={"id": "mypy"},
            )
        },
    )

    _record_dynamic_validation_result(
        _parse_action_response(
            {
                "action": "invoke_tool",
                "tool": "shell",
                "parameters": {"command": ["uv", "run", "mypy"]},
            }
        ),
        state,
    )

    assert '"returncode": 1' in state.execution_context[-1]
    assert "src/example.py:1: error" in state.execution_context[-1]
    assert "Found 1 error" in state.execution_context[-1]


def test_validation_fingerprint_detects_repeated_and_regressed_issues() -> None:
    result = {
        "returncode": 1,
        "stdout": (
            "validation_successful: false\n"
            "issues:\n"
            "- code: missing_id\n"
            "  path: entities[0].id\n"
        ),
    }
    fingerprint = _validation_issue_fingerprint(result)
    assert len(fingerprint) == 1

    step = SkillStep(
        description="Run validation.",
        validation_gate={"id": "checks"},
    )
    state = _WorkflowExecutionState(
        selected_skill=SkillCatalogEntry(
            Path("skill.yaml"), Skill(name="validation", when_to_use=(), steps=(step,))
        ),
        transcript=[],
        execution_events=[],
        execution_context=[],
        step_index=0,
        worktree_root=Path("."),
    )
    action = _parse_action_response(
        {
            "action": "invoke_tool",
            "tool": "shell",
            "parameters": {"command": ["uv", "run", "evaluate"]},
        }
    )
    obligation = _ValidationObligation(
        obligation_id="evaluate",
        expected_action={
            "kind": "invoke_tool",
            "tool": "shell",
            "parameters": {"command": ["uv", "run", "evaluate"]},
        },
        source={"id": "evaluate"},
    )
    state.validation_gates["checks"] = _ValidationGateState(
        step_index=0,
        discovered=True,
        epoch=1,
        obligations={"evaluate": obligation},
    )

    for _ in range(2):
        state.execution_events.append({"result": result})
        _record_dynamic_validation_result(action, state)

    assert obligation.semantic_stalls == 1
    assert len(obligation.issue_history) == 1


def test_action_schema_uses_action_and_internal_tool_must_be_declared() -> None:
    action = _parse_action_response(
        {
            "action": "invoke_tool",
            "tool": "internal",
            "parameters": {"command": ["powdrr-lift", "repository-state"]},
        }
    )
    with pytest.raises(RuntimeError, match="internal tool is not declared"):
        _validate_workflow_action_for_step(action, SkillStep(description="Report."))

    _validate_workflow_action_for_step(
        action,
        SkillStep(
            description="Inspect the repository.",
            tool_invocations=(
                SkillToolInvocation(
                    tool="internal", command=("powdrr-lift", "repository-state")
                ),
            ),
        ),
    )

    with pytest.raises(RuntimeError, match="only the powdrr-lift binary"):
        _validate_internal_command(["git", "status"])


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
        status="Edited file",
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
    assert "Edited file" in output
    assert "performing local action..." not in output


def test_workflow_action_progress_status_uses_action_specific_messages() -> None:
    assert (
        _workflow_action_progress_status(
            _parse_action_response(
                {
                    "action": "edit",
                    "file_path": "README.md",
                    "edits": [{"kind": "replace", "start_line": 1, "text": "Updated"}],
                }
            )
        )
        == "Attempting file edit"
    )
    assert (
        _workflow_action_progress_status(
            _parse_action_response(
                {
                    "action": "read_document",
                    "file_path": "README.md",
                    "start_line": 1,
                    "end_line": 2,
                }
            )
        )
        == "Reading file"
    )
    assert (
        _workflow_action_progress_status(
            _parse_action_response(
                {
                    "action": "gather_context",
                    "types": ["requirements"],
                }
            )
        )
        == "Gathering structured context"
    )
    assert (
        _workflow_action_progress_status(
            _parse_action_response(
                {
                    "action": "invoke_tool",
                    "tool": "shell",
                    "parameters": {"command": ["git", "status", "--short"]},
                }
            )
        )
        == "Invoking git status --short"
    )
    assert (
        _workflow_action_progress_status(
            _parse_action_response(
                {
                    "action": "next_step",
                }
            )
        )
        is None
    )


def test_repeated_document_reads_do_not_count_as_material_progress(
    tmp_path: Path,
) -> None:
    document = tmp_path / "README.md"
    document.write_text("line one\nline two\n", encoding="utf-8")
    state = _WorkflowExecutionState(
        selected_skill=SkillCatalogEntry(tmp_path / "skill.yaml", _build_skill()),
        transcript=[],
        execution_events=[],
        execution_context=[],
        step_index=0,
        worktree_root=tmp_path,
        current_file_path=document,
    )
    action = _parse_action_response(
        {
            "action": "read_document",
            "file_path": "README.md",
            "start_line": 1,
            "end_line": 2,
        }
    )

    assert _workflow_action_material_state(action, state) is None


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
    long_context_mapping = _long_context_backup_for(
        "Qwen/Qwen2.5-Coder-14B-Instruct",
        tuple(ZAI_LLM_MAPPINGS.items()),
    )
    assert long_context_mapping is not None
    assert long_context_mapping.model == "glm-5.2"


def test_fast_iteration_uses_flash_model_for_long_context_fallback() -> None:
    mapping = _resolve_llm_mapping(
        "fast_iteration",
        mappings=(),
        provider="zai",
    )

    assert mapping is not None
    assert mapping.model == "Qwen/Qwen2.5-Coder-14B-Instruct"
    assert mapping.long_context_backup_model is not None
    assert mapping.long_context_backup_model.model == "glm-4.7-flash"


def test_oversized_context_uses_long_context_backup_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FakeClient:
        def complete_json(self, messages: list[dict[str, str]]) -> dict[str, Any]:
            return {"ok": True}

    clients: list[str] = []

    def client_for(model: str, provider: str) -> _FakeClient:
        _ = provider
        clients.append(model)
        return _FakeClient()

    monkeypatch.setattr(
        "powdrr_lift.workflow_chat_agent._model_limits_for",
        lambda provider, model: LLMModelLimits(
            context_window=100,
            max_output_tokens=50,
        ),
    )
    result, model, provider = _complete_json_with_model_fallback(
        client_for=client_for,
        messages=[{"role": "user", "content": "x" * 1_000}],
        context="large request",
        model="normal-model",
        provider="zai",
        parser=lambda payload: payload,
        repair_instructions="",
        config=SkillChatConfig(skills_dir=Path("skills")),
        input_func=lambda: "abort",
        stdout=io.StringIO(),
        stderr=io.StringIO(),
        model_mappings=(
            (
                "standard_reasoning",
                LLMModelMapping(
                    "normal-model",
                    provider="zai",
                    long_context_backup_model=LLMModelMapping(
                        "long-context-model",
                        provider="zai",
                    ),
                ),
            ),
        ),
    )

    assert result == {"ok": True}
    assert model == "long-context-model"
    assert provider == "zai"
    assert clients == ["long-context-model"]


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


def test_llm_exchange_recorder_writes_input_and_output_json(
    tmp_path: Path,
) -> None:
    class _FakeClient:
        def complete_json(self, messages: list[dict[str, str]]) -> dict[str, Any]:
            assert messages == [{"role": "user", "content": "request"}]
            return {"action": "complete", "text": "done"}

    recorder = _LLMExchangeRecordingClient(_FakeClient(), tmp_path)

    assert recorder.complete_json([{"role": "user", "content": "request"}]) == {
        "action": "complete",
        "text": "done",
    }

    dump_paths = sorted(tmp_path.glob("llm-*.json"))
    assert len(dump_paths) == 1
    exchange = json.loads(dump_paths[0].read_text(encoding="utf-8"))
    assert exchange["input"] == [{"role": "user", "content": "request"}]
    assert exchange["output"] == {"action": "complete", "text": "done"}
    assert exchange["timestamp"]


def test_llm_exchange_recorder_reuses_client_serialized_messages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    serialization_calls = 0

    def _track_serialization(messages: object) -> str:
        nonlocal serialization_calls
        serialization_calls += 1
        return _serialize_messages(cast(list[dict[str, str]], messages))

    monkeypatch.setattr(
        "powdrr_lift.workflow_chat_agent._serialize_messages",
        _track_serialization,
    )

    class _FakeClient:
        last_serialized_messages = '[{"role":"user","content":"request"}]'

        def complete_json(self, messages: list[dict[str, str]]) -> dict[str, Any]:
            assert messages == [{"role": "user", "content": "request"}]
            return {"action": "complete"}

    recorder = _LLMExchangeRecordingClient(_FakeClient(), tmp_path)
    recorder.complete_json([{"role": "user", "content": "request"}])

    exchange = json.loads(next(tmp_path.glob("llm-*.json")).read_text())
    assert exchange["input"] == [{"role": "user", "content": "request"}]
    assert serialization_calls == 0


def test_openai_client_serializes_messages_once_for_budget_and_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    serialization_calls = 0

    def _track_serialization(messages: object) -> str:
        nonlocal serialization_calls
        serialization_calls += 1
        return _serialize_messages(cast(list[dict[str, str]], messages))

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
                        {"message": {"content": '{"action":"complete"}'}},
                    ]
                }
            ).encode("utf-8")

    request_bodies: list[dict[str, Any]] = []

    def _fake_urlopen(request: Request, timeout: float) -> _FakeResponse:
        request_bodies.append(json.loads(cast(bytes, request.data).decode("utf-8")))
        return _FakeResponse()

    monkeypatch.setattr(
        "powdrr_lift.workflow_chat_agent._serialize_messages",
        _track_serialization,
    )
    monkeypatch.setattr("powdrr_lift.workflow_chat_agent.urlopen", _fake_urlopen)

    client = OpenAIChatClient(
        model="test-model",
        api_key="test-key",
        base_url="https://api.openai.com/v1",
    )

    assert client.complete_json([{"role": "user", "content": "request"}]) == {
        "action": "complete"
    }
    assert serialization_calls == 1
    assert request_bodies[0]["messages"] == [{"role": "user", "content": "request"}]


def test_normalize_cache_usage_supports_provider_formats() -> None:
    assert _normalize_cache_usage(
        {
            "prompt_tokens": 2000,
            "prompt_tokens_details": {
                "cached_tokens": 1500,
                "cache_write_tokens": 0,
            },
        }
    ) == {
        "prompt_tokens": 2000,
        "cached_tokens": 1500,
        "cache_miss_tokens": 500,
        "cache_write_tokens": 0,
    }
    assert _normalize_cache_usage(
        {
            "prompt_tokens": 2000,
            "prompt_cache_hit_tokens": 1800,
            "prompt_cache_miss_tokens": 200,
        }
    ) == {
        "prompt_tokens": 2000,
        "cached_tokens": 1800,
        "cache_miss_tokens": 200,
        "cache_write_tokens": 0,
    }


def test_llm_exchange_recorder_includes_normalized_cache_usage(
    tmp_path: Path,
) -> None:
    class _FakeClient:
        last_usage = {
            "prompt_tokens": 2000,
            "prompt_tokens_details": {"cached_tokens": 1500},
        }

        def complete_json(self, messages: list[dict[str, str]]) -> dict[str, Any]:
            return {"action": "complete"}

    recorder = _LLMExchangeRecordingClient(_FakeClient(), tmp_path)
    recorder.complete_json([{"role": "user", "content": "request"}])

    exchange = json.loads(next(tmp_path.glob("llm-*.json")).read_text())
    assert exchange["usage"] == {
        "prompt_tokens": 2000,
        "cached_tokens": 1500,
        "cache_miss_tokens": 500,
        "cache_write_tokens": 0,
    }


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


def test_openai_remote_disconnect_is_reported_as_retryable_provider_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _disconnected(request: Request, timeout: float) -> object:
        raise ConnectionResetError("Remote end closed connection without response")

    monkeypatch.setattr("powdrr_lift.workflow_chat_agent.urlopen", _disconnected)
    client = OpenAIChatClient(
        model="test-model",
        api_key="test-key",
        base_url="https://api.openai.com/v1",
    )

    with pytest.raises(
        RuntimeError,
        match="OpenAI request connection dropped: Remote end closed connection",
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


def test_openrouter_credentials_and_default_base_url_are_supported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "openrouter-key")
    monkeypatch.delenv("OPENROUTER_BASE_URL", raising=False)

    assert _resolve_api_key("openrouter", None) == (
        "openrouter-key",
        "OPENROUTER_API_KEY",
    )
    assert _resolve_base_url("openrouter", None) == (
        "https://openrouter.ai/api/v1",
        "default",
    )


def test_openrouter_maps_every_llm_type_to_ox_alpha() -> None:
    for llm_type in ALL_LLM_TYPES:
        mapping = OPENROUTER_LLM_MAPPINGS[llm_type]
        assert mapping.model == "stealth/ox-alpha"
        assert mapping.provider == "openrouter"


def test_auto_provider_selects_openrouter_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for env_name in (
        "OPENAI_API_KEY",
        "CODEX_API_KEY",
        "ANTHROPIC_API_KEY",
        "CLAUDE_API_KEY",
        "ZAI_API_KEY",
        "ZAI_BASE_URL",
        "DEEPINFRA_API_TOKEN",
        "DEEPINFRA_API_KEY",
        "DEEPINFRA_BASE_URL",
    ):
        monkeypatch.delenv(env_name, raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "openrouter-key")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")

    assert _resolve_provider("auto", "glm-5.2") == "openrouter"


def test_auto_provider_prefers_deepinfra_cheap_over_openrouter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for env_name in (
        "OPENAI_API_KEY",
        "CODEX_API_KEY",
        "ANTHROPIC_API_KEY",
        "CLAUDE_API_KEY",
        "ZAI_API_KEY",
        "ZAI_BASE_URL",
        "DEEPINFRA_API_KEY",
        "DEEPINFRA_BASE_URL",
    ):
        monkeypatch.delenv(env_name, raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "openrouter-key")
    monkeypatch.setenv("DEEPINFRA_API_TOKEN", "deepinfra-token")

    assert _resolve_provider("auto", "glm-5.2") == "deepinfra-cheap"


def test_available_workflow_providers_requires_api_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for env_name in (
        "OPENAI_API_KEY",
        "CODEX_API_KEY",
        "ANTHROPIC_API_KEY",
        "CLAUDE_API_KEY",
        "ZAI_API_KEY",
        "GLM_API_KEY",
        "OPENROUTER_API_KEY",
        "DEEPINFRA_API_TOKEN",
        "DEEPINFRA_API_KEY",
    ):
        monkeypatch.delenv(env_name, raising=False)
    monkeypatch.setattr(
        "powdrr_lift.workflow_chat_agent._resolve_codex_access_token",
        lambda: None,
    )
    monkeypatch.setenv("OPENROUTER_BASE_URL", "https://example.test")
    monkeypatch.setenv("DEEPINFRA_API_TOKEN", "deepinfra-token")

    assert available_workflow_providers() == ("deepinfra-cheap", "deepinfra")


def test_choose_workflow_provider_presents_configured_provider_pick_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for env_name in (
        "OPENAI_API_KEY",
        "CODEX_API_KEY",
        "ANTHROPIC_API_KEY",
        "CLAUDE_API_KEY",
        "ZAI_API_KEY",
        "GLM_API_KEY",
        "OPENROUTER_API_KEY",
        "DEEPINFRA_API_TOKEN",
        "DEEPINFRA_API_KEY",
    ):
        monkeypatch.delenv(env_name, raising=False)
    monkeypatch.setattr(
        "powdrr_lift.workflow_chat_agent._resolve_codex_access_token",
        lambda: None,
    )
    monkeypatch.setenv("ZAI_API_KEY", "zai-token")
    output = io.StringIO()

    selected = choose_workflow_provider(
        input_func=lambda: "1",
        stdout=output,
    )

    assert selected == "zai"
    assert "z.ai (zai)" in output.getvalue()
    assert "OPENAI_API_KEY" not in output.getvalue()


def test_initial_model_uses_openrouter_mapping_for_default_model() -> None:
    assert _initial_model_for_provider("openrouter", "glm-5.2") == ("stealth/ox-alpha")
    assert _initial_model_for_provider("openai", "glm-5.2") == "glm-5.2"
    assert _initial_model_for_provider("openrouter", "custom-model") == ("custom-model")


def test_invalid_gather_context_type_is_repairable() -> None:
    with pytest.raises(
        RuntimeError,
        match="Unsupported context type 'implementation-specifications'",
    ):
        _parse_workflow_action_gather_context(
            {"types": ["implementation-specifications"]},
            None,
            None,
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


def test_auto_provider_prefers_deepinfra_cheap_when_credentials_are_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for env_name in (
        "OPENAI_API_KEY",
        "CODEX_API_KEY",
        "ANTHROPIC_API_KEY",
        "CLAUDE_API_KEY",
        "ZAI_API_KEY",
        "ZAI_BASE_URL",
        "OPENROUTER_API_KEY",
    ):
        monkeypatch.delenv(env_name, raising=False)
    monkeypatch.setenv("DEEPINFRA_API_TOKEN", "deepinfra-token")

    assert _resolve_provider("auto", "glm-5.2") == "deepinfra-cheap"


def test_auto_provider_roles_use_the_top_two_configured_providers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for env_name in (
        "OPENAI_API_KEY",
        "CODEX_API_KEY",
        "ANTHROPIC_API_KEY",
        "CLAUDE_API_KEY",
        "ZAI_API_KEY",
        "ZAI_BASE_URL",
        "DEEPINFRA_API_TOKEN",
        "DEEPINFRA_API_KEY",
        "DEEPINFRA_BASE_URL",
        "OPENROUTER_API_KEY",
    ):
        monkeypatch.delenv(env_name, raising=False)
    monkeypatch.setenv("DEEPINFRA_API_TOKEN", "deepinfra-token")
    monkeypatch.setenv("ZAI_API_KEY", "zai-token")

    roles = _resolve_provider_roles(SkillChatConfig(skills_dir=Path("skills")))

    assert isinstance(roles, LLMProviderRoles)
    assert roles.normal == "deepinfra-cheap"
    assert roles.adversarial == "zai"


def test_auto_provider_roles_return_no_adversarial_provider_when_only_one_is_configured(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    for env_name in (
        "OPENAI_API_KEY",
        "CODEX_API_KEY",
        "ZAI_API_KEY",
        "OPENROUTER_API_KEY",
    ):
        monkeypatch.delenv(env_name, raising=False)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    monkeypatch.setenv("DEEPINFRA_API_TOKEN", "deepinfra-token")

    roles = _resolve_provider_roles(SkillChatConfig(skills_dir=Path("skills")))

    assert roles.normal == "deepinfra-cheap"
    assert roles.adversarial is None


def test_explicit_provider_selection_is_not_overridden() -> None:
    assert _resolve_provider("local", "glm-5.2") == "local"
    assert "deepinfra-cheap" in ALL_PROVIDERS
    with pytest.raises(RuntimeError, match="Unsupported LLM provider 'unknown'"):
        _resolve_provider("unknown", "test-model")


def test_deepinfra_cheap_maps_every_llm_type_to_flash() -> None:
    expected_model = "deepseek-ai/DeepSeek-V4-Flash-0731"
    expected_backup_model = "deepseek-ai/DeepSeek-V4-Flash"
    for llm_type in DEEPINFRA_CHEAP_LLM_MAPPINGS:
        mapping = DEEPINFRA_CHEAP_LLM_MAPPINGS[llm_type]
        assert mapping.model == expected_model
        assert mapping.backup_model is not None
        assert mapping.backup_model.model == expected_backup_model
        assert (
            _resolve_llm_model(
                llm_type,
                fallback_model="fallback-model",
                mappings=tuple(DEEPINFRA_CHEAP_LLM_MAPPINGS.items()),
                provider="deepinfra-cheap",
            )
            == expected_model
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
                    "action": "invoke_tool",
                    "tool": "shell",
                    "parameters": {"command": f"printf progress-{call_index}"},
                }
            if call_index == 7:
                return {"action": "next_step"}
            if call_index == 8:
                return {"action": "complete", "text": "Done."}
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
    monkeypatch.setattr(
        "powdrr_lift.workflow_chat_agent._resolve_worktree_context",
        lambda repo_root, stderr, verbose: repo_root,
    )

    stdout = io.StringIO()
    stderr = io.StringIO()
    progress_statuses: list[str] = []
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
        progress_callback=lambda _skill, _step, status, _parent, _parent_step: (
            progress_statuses.append(status)
        ),
    )

    assert exit_code == 0
    assert "Done." in stdout.getvalue()
    assert any(
        status.startswith("roundtrip 7: next_step") for status in progress_statuses
    )
    assert any(
        status.startswith("roundtrip 8: complete") for status in progress_statuses
    )


def test_workflow_execution_retries_stalled_step_with_clean_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    subprocess.run(["git", "init", "-q", str(repo_root)], check=True)
    (repo_root / "step-change.txt").write_text("before\n", encoding="utf-8")
    skills_dir = repo_root / "skill-definitions"
    skills_dir.mkdir()
    skill_path = skills_dir / "one-step.json"
    save_skill(
        Skill(
            name="one-step",
            when_to_use=("For testing stalled workflow execution.",),
            steps=(
                SkillStep(
                    description="Do the work.",
                    details="Do it.",
                ),
            ),
        ),
        skill_path,
    )

    captured_messages: list[list[dict[str, str]]] = []
    progress_statuses: list[str] = []

    class _FakeOpenAIClient:
        def __init__(self, **_: object) -> None:
            self.call_index = 0

        def complete_json(self, messages: list[dict[str, str]]) -> dict[str, object]:
            captured_messages.append(messages)
            self.call_index += 1
            if self.call_index == 1:
                return {
                    "selected_skill_path": str(skill_path),
                    "selected_skill_reason": "The skill matches the request.",
                    "next_question": None,
                    "ready_to_execute": True,
                }
            if self.call_index <= 5:
                return {
                    "action": "edit",
                    "file_path": "step-change.txt",
                    "edits": [
                        {
                            "kind": "replace",
                            "start_line": 1,
                            "end_line": 1,
                            "text": "same\n",
                        }
                    ],
                    **(
                        {"decisions_and_context": "Different explanation."}
                        if self.call_index == 5
                        else {}
                    ),
                }
            return {"action": "complete", "text": "Recovered."}

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
            max_stalled_roundtrips=2,
        ),
        input_func=lambda: "Build the feature",
        stdout=stdout,
        stderr=stderr,
        progress_callback=lambda _skill, _step, status, _parent, _parent_step: (
            progress_statuses.append(status)
        ),
    )

    assert exit_code == 0
    assert "Retrying step after stall" in stderr.getvalue()
    assert "already identified as stalled" in stderr.getvalue()
    assert (repo_root / "step-change.txt").read_text(encoding="utf-8") == "before\n"
    assert "Recovered." in stdout.getvalue()
    assert any(status.startswith("roundtrip 1: edit") for status in progress_statuses)
    assert "made no progress" in "\n".join(
        message["content"]
        for exchange in captured_messages
        for message in exchange
        if message["role"] == "user"
    )
    assert any(
        '"stalled_step_context":[{' in message["content"]
        for exchange in captured_messages[2:]
        for message in exchange
        if message["role"] == "user"
    )


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
                "action": "complete",
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
            provider="openai",
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


def test_workflow_chat_runs_declared_nested_skill_in_same_worktree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    skills_dir = repo_root / "skill-definitions"
    skills_dir.mkdir()
    save_skill(
        Skill(
            name="parent",
            when_to_use=("Run the parent skill.",),
            steps=(
                SkillStep(
                    description="Run the child first.",
                    uses_skills=("child",),
                ),
            ),
        ),
        skills_dir / "parent.yaml",
    )
    save_skill(
        Skill(
            name="child",
            when_to_use=("Run the child skill.",),
            steps=(SkillStep(description="Finish the child."),),
        ),
        skills_dir / "child.yaml",
    )

    responses: Iterator[dict[str, object]] = iter(
        [
            {
                "selected_skill_path": str(skills_dir / "parent.yaml"),
                "selected_skill_reason": "The parent skill matches.",
                "next_question": None,
                "ready_to_execute": True,
            },
            {"action": "complete", "text": "Child complete."},
            {"action": "complete", "text": "Parent complete."},
        ]
    )

    class _FakeOpenAIClient:
        def __init__(self, **_: object) -> None:
            pass

        def complete_json(self, messages: list[dict[str, str]]) -> dict[str, object]:
            return next(responses)

    monkeypatch.setattr(
        "powdrr_lift.workflow_chat_agent.OpenAIChatClient",
        _FakeOpenAIClient,
    )
    monkeypatch.setattr(
        "powdrr_lift.workflow_chat_agent._resolve_worktree_context",
        lambda repo_root, stderr, verbose: repo_root,
    )
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    exit_code = run_workflow_chat(
        SkillChatConfig(
            skills_dir=skills_dir,
            repo_root=repo_root,
            output_dir=Path("generated"),
            api_key="test-key",
            model="test-model",
        ),
        input_func=lambda: "run parent",
        stdout=io.StringIO(),
        stderr=io.StringIO(),
    )

    assert exit_code == 0
    assert (repo_root / "generated" / "skill-execution.json").exists()


def test_workflow_chat_action_prompt_mentions_gather_context() -> None:
    prompt = _action_system_prompt()

    assert "gather_context" in prompt
    assert "edit" in prompt
    assert "file_path" in prompt
    assert "requirements" in prompt
    assert "entity-relationships" in prompt
    assert "proposed PRs" in prompt
    assert "Do not use filters.work_item_name" in prompt
    assert "do not invoke the same validation command again unchanged" in prompt
    assert "reported corrective_action" in prompt
    assert "read_document" in prompt
    assert "start_line" in prompt
    assert "end_line" in prompt
    assert "top-level action field" in prompt
    assert "multiple independent edits" in prompt
    assert 'action":"yaml_edit"' in prompt


def test_step_prompt_includes_interaction_style_guidance(tmp_path: Path) -> None:
    step = SkillStep(description="Review the implementation.")
    skill_entry = SkillCatalogEntry(
        tmp_path / "review.yaml",
        Skill(
            name="review",
            when_to_use=("Review changes.",),
            steps=(step,),
            interaction_style="observational_review",
        ),
    )

    system_prompt = _build_step_execution_messages(
        selected_skill=skill_entry,
        current_step=step,
        current_step_index=0,
        transcript=[],
        execution_events=[],
        execution_context=[],
        current_file_path=None,
        worktree_root=tmp_path,
        catalog=(skill_entry,),
    )[0]["content"]

    assert "Interaction style: observational_review." in system_prompt
    assert "Separate observations, inferences, risks, and recommendations." in (
        system_prompt
    )
    assert "current step contract" in system_prompt


def test_invoke_skill_supports_adversarial_provider_and_clean_context() -> None:
    action = _parse_action_response(
        {
            "action": "invoke_skill",
            "skill": "adversarial-review",
            "provider_role": "adversarial",
            "clean": True,
            "context": ["Review only this diff."],
            "decisions_and_context": "The change is intentionally narrow.",
        }
    )

    assert action.skill_name == "adversarial-review"
    assert action.provider_role == "adversarial"
    assert action.clean is True
    assert action.context == ("Review only this diff.",)
    assert action.decisions_and_context == "The change is intentionally narrow."


def test_workflow_handoff_requires_declared_outputs_and_matching_inputs() -> None:
    current_step = SkillStep(
        description="Produce a validation result.",
        outputs=(
            SkillStepOutput(
                name="validation_result",
                type="validation_result",
                required_for_next_step=True,
            ),
        ),
    )
    next_step = SkillStep(
        description="Consume the validation result.",
        inputs=(
            SkillStepInput(
                name="validation_result",
                type="validation_result",
            ),
        ),
    )

    with pytest.raises(RuntimeError, match="required outputs"):
        _validate_workflow_handoff(current_step, next_step, {}, current_step_index=0)

    _validate_workflow_handoff(
        current_step,
        next_step,
        {
            "validation_result": {
                "type": "validation_result",
                "value": {},
                "produced_by": {"step_index": 0},
            }
        },
        current_step_index=0,
    )


def test_workflow_handoff_prompt_excludes_unrelated_records() -> None:
    step = SkillStep(
        description="Consume the validation result.",
        inputs=(SkillStepInput(name="validation_result", type="validation_result"),),
    )
    records: dict[str, dict[str, Any]] = {
        "validation_result": {
            "name": "validation_result",
            "type": "validation_result",
            "value": {"ok": True},
        },
        "unrelated": {
            "name": "unrelated",
            "type": "string",
            "value": "must not leak",
        },
    }

    handoff = _workflow_handoff_inputs(step, records)

    assert list(handoff["resolved"]) == ["validation_result"]
    assert "must not leak" not in json.dumps(handoff)


def test_action_outputs_are_preserved_in_the_parsed_action() -> None:
    action = _parse_action_response(
        {
            "action": "next_step",
            "outputs": {"validation_result": {"ok": True}},
        }
    )

    assert action.outputs == {"validation_result": {"ok": True}}


def test_read_document_action_returns_requested_lines_as_next_context(
    tmp_path: Path,
) -> None:
    document = tmp_path / "docs" / "specification.yaml"
    document.parent.mkdir(parents=True)
    document.write_text("one\ntwo\nthree\nfour\n", encoding="utf-8")
    skill_path = tmp_path / "skill.yaml"
    skill = SkillCatalogEntry(skill_path, _build_skill())
    state = _WorkflowExecutionState(
        selected_skill=skill,
        transcript=[],
        execution_events=[],
        execution_context=[],
        step_index=0,
        worktree_root=tmp_path,
    )
    action = _parse_action_response(
        {
            "action": "read_document",
            "file_path": "docs/specification.yaml",
            "start_line": 2,
            "end_line": 3,
        }
    )

    assert (
        _handle_workflow_action_read_document(
            action,
            state,
            io.StringIO(),
            io.StringIO(),
            lambda: "",
            SkillChatConfig(skills_dir=Path("skill-definitions")),
        )
        is True
    )
    context = json.loads(state.transcript[-1]["content"])
    assert context["document_context"]["path"] == "docs/specification.yaml"
    assert context["document_context"]["start_line"] == 2
    assert context["document_context"]["end_line"] == 3
    assert [line["text"] for line in context["document_context"]["lines"]] == [
        "two",
        "three",
    ]
    assert state.execution_events[-1]["kind"] == "read_document"


def test_read_document_action_clamps_range_past_end_of_short_document(
    tmp_path: Path,
) -> None:
    document = tmp_path / "short.md"
    document.write_text("one\ntwo\nthree\n", encoding="utf-8")
    state = _WorkflowExecutionState(
        selected_skill=SkillCatalogEntry(tmp_path / "skill.yaml", _build_skill()),
        transcript=[],
        execution_events=[],
        execution_context=[],
        step_index=0,
        worktree_root=tmp_path,
    )
    action = _parse_action_response(
        {
            "action": "read_document",
            "file_path": "short.md",
            "start_line": 0,
            "end_line": 200,
        }
    )

    assert (
        _handle_workflow_action_read_document(
            action,
            state,
            io.StringIO(),
            io.StringIO(),
            lambda: "",
            SkillChatConfig(skills_dir=Path("skill-definitions")),
        )
        is True
    )
    context = json.loads(state.transcript[-1]["content"])["document_context"]
    assert context["start_line"] == 1
    assert context["end_line"] == 3
    assert [line["text"] for line in context["lines"]] == ["one", "two", "three"]


def test_read_document_action_accepts_zero_to_max_lines_range(
    tmp_path: Path,
) -> None:
    document = tmp_path / "long.md"
    document.write_text(
        "\n".join(f"line-{index}" for index in range(1, 2001)),
        encoding="utf-8",
    )
    state = _WorkflowExecutionState(
        selected_skill=SkillCatalogEntry(tmp_path / "skill.yaml", _build_skill()),
        transcript=[],
        execution_events=[],
        execution_context=[],
        step_index=0,
        worktree_root=tmp_path,
    )
    action = _parse_action_response(
        {
            "action": "read_document",
            "file_path": "long.md",
            "start_line": 0,
            "end_line": 2000,
        }
    )

    assert (
        _handle_workflow_action_read_document(
            action,
            state,
            io.StringIO(),
            io.StringIO(),
            lambda: "",
            SkillChatConfig(skills_dir=Path("skill-definitions")),
        )
        is True
    )
    context = json.loads(state.transcript[-1]["content"])["document_context"]
    assert context["start_line"] == 1
    assert context["end_line"] == 2000
    assert len(context["lines"]) == 2000
    assert context["lines"][0]["line_number"] == 1
    assert context["lines"][-1]["line_number"] == 2000


def test_read_document_missing_file_lists_directory_files(
    tmp_path: Path,
) -> None:
    workflow_directory = tmp_path / "docs" / "workflows" / "feature"
    workflow_directory.mkdir(parents=True)
    (workflow_directory / "feature-core-workflow.yaml").write_text(
        "id: feature-core\n", encoding="utf-8"
    )
    state = _WorkflowExecutionState(
        selected_skill=SkillCatalogEntry(tmp_path / "skill.yaml", _build_skill()),
        transcript=[],
        execution_events=[],
        execution_context=[],
        step_index=0,
        worktree_root=tmp_path,
    )
    action = _parse_action_response(
        {
            "action": "read_document",
            "file_path": "docs/workflows/feature/wrong-workflow.yaml",
            "start_line": 1,
            "end_line": 10,
        }
    )

    with pytest.raises(RuntimeError, match="feature-core-workflow.yaml"):
        _handle_workflow_action_read_document(
            action,
            state,
            io.StringIO(),
            io.StringIO(),
            lambda: "",
            SkillChatConfig(skills_dir=Path("skill-definitions")),
        )


def test_file_management_action_renames_and_records_result(tmp_path: Path) -> None:
    source = tmp_path / "old.txt"
    source.write_text("content", encoding="utf-8")
    state = _WorkflowExecutionState(
        selected_skill=SkillCatalogEntry(tmp_path / "skill.yaml", _build_skill()),
        transcript=[],
        execution_events=[],
        execution_context=[],
        step_index=0,
        worktree_root=tmp_path,
    )
    action = _parse_action_response(
        {
            "action": "file_management",
            "operation": "rename",
            "file_path": "old.txt",
            "destination_path": "new.txt",
        }
    )

    assert (
        _handle_workflow_action_file_management(
            action,
            state,
            io.StringIO(),
            io.StringIO(),
            lambda: "",
            SkillChatConfig(skills_dir=Path("skill-definitions")),
        )
        is True
    )
    assert not source.exists()
    assert (tmp_path / "new.txt").read_text(encoding="utf-8") == "content"
    assert state.execution_events[-1]["result"]["destination_path"] == "new.txt"


@pytest.mark.parametrize("path", ["../outside.txt", "nested/../../outside.txt"])
def test_file_management_rejects_parent_traversal(tmp_path: Path, path: str) -> None:
    source = tmp_path / "source.txt"
    source.write_text("content", encoding="utf-8")

    with pytest.raises(FileManagementError, match="must not contain '..'"):
        manage_worktree_file(
            tmp_path,
            operation="delete",
            file_path=path,
        )
    assert issubclass(FileManagementError, PowdrrExecutionError)
    assert source.exists()


def test_file_management_rejects_symlinked_directory(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    link = tmp_path / "linked"
    link.symlink_to(outside, target_is_directory=True)

    with pytest.raises(FileManagementError, match="symlink"):
        manage_worktree_file(
            tmp_path,
            operation="delete",
            file_path="linked/secret.txt",
        )
    assert (outside / "secret.txt").exists()


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
                        "Use gather_context to retrieve existing requirement notes."
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
        / "proposals"
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
                "action": "gather_context",
                "feature_id": "display-related-photos",
                "types": ["requirements"],
                "keywords": ["related photos"],
                "decisions_and_context": (
                    "Need the existing requirements before summarizing."
                ),
            },
            {
                "action": "next_step",
                "decisions_and_context": "Requirements gathered.",
            },
            {
                "action": "complete",
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
                assert prompt["execution_events"][-1]["kind"] == "gather_context"
                assert prompt["execution_events"][-1]["types"] == ["requirements"]
                assert "result" not in prompt["execution_events"][-1]
            elif self._call_index == 3:
                assert prompt["current_step"]["description"] == (
                    "Summarize the gathered context."
                )
                assert prompt["durable_facts"][-1]["value"] == (
                    "Requirements gathered."
                )
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
        "gather_context",
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
        / "proposals"
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
                "action": "invoke_tool",
                "tool": "internal",
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
                "action": "yaml_edit",
                "file_path": (
                    "docs/proposals/display-related-photos/system-specification.yaml"
                ),
                "operations": [
                    {
                        "op": "set_value",
                        "path": ["id"],
                        "value": "display-related-photos",
                    }
                ],
                "decisions_and_context": "Set the system spec id.",
            },
            {
                "action": "next_step",
                "decisions_and_context": "System template updated.",
            },
            {
                "action": "complete",
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
                assert prompt["execution_events"][-1]["kind"] == "yaml_edit"
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
    assert (
        "id: display-related-photos"
        in system_spec_path.read_text(encoding="utf-8").splitlines()
    )
    summary = json.loads(
        (worktree_root / "generated" / "skill-execution.json").read_text(
            encoding="utf-8"
        )
    )
    assert [event["kind"] for event in summary["execution_events"]] == [
        "invoke_tool",
        "yaml_edit",
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
            "action": "edit",
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


def test_yaml_edit_preserves_section_keys_and_updates_items_structurally() -> None:
    action = _parse_action_response(
        {
            "action": "yaml_edit",
            "file_path": "docs/specification.yaml",
            "operations": [
                {
                    "op": "upsert_item",
                    "section": "entities",
                    "id": "interaction-log-entry",
                    "value": {
                        "action": "added",
                        "rationale": "Capture one interaction.",
                    },
                },
                {
                    "op": "set_value",
                    "path": ["title"],
                    "value": "Interaction File Logging",
                },
            ],
        }
    )

    updated = _apply_yaml_operations(
        Path("docs/specification.yaml"),
        "title: null\nentities:\n  - id: null\n    action: null\n",
        action.yaml_operations,
    )

    assert yaml.safe_load(updated) == {
        "title": "Interaction File Logging",
        "entities": [
            {
                "action": "added",
                "rationale": "Capture one interaction.",
                "id": "interaction-log-entry",
            }
        ],
    }
    assert "entities:" in updated


def test_yaml_edit_invalid_shape_returns_progressive_usage_guidance() -> None:
    with pytest.raises(RuntimeError, match="non-empty operations array"):
        _parse_action_response(
            {"action": "yaml_edit", "file_path": "docs/specification.yaml"}
        )

    with pytest.raises(
        RuntimeError, match="upsert_item, remove_item, remove_key, or set_value"
    ):
        _parse_action_response(
            {
                "action": "yaml_edit",
                "file_path": "docs/specification.yaml",
                "operations": [{"op": "replace_lines"}],
            }
        )

    action = _parse_action_response(
        {
            "action": "yaml_edit",
            "file_path": "docs/specification.yaml",
            "operations": [
                {
                    "op": "remove_item",
                    "section": "entities",
                    "id": "missing",
                }
            ],
        }
    )
    with pytest.raises(_WorkflowYamlEditError, match="Use read_document"):
        _apply_yaml_operations(
            Path("docs/specification.yaml"),
            "entities:\n  - id: present\n",
            action.yaml_operations,
        )

    feedback = _workflow_edit_failure_feedback(
        action,
        _WorkflowYamlEditError("No item with id 'missing' exists."),
        {"exists": True, "line_count": 2},
    )
    assert "upsert_item" in feedback
    assert "multiple independent operations" in feedback
    assert "exact line ranges" in feedback


def test_yaml_edit_set_value_supports_validator_list_indices() -> None:
    action = _parse_action_response(
        {
            "action": "yaml_edit",
            "file_path": "docs/architecture.yaml",
            "operations": [
                {
                    "op": "set_value",
                    "path": ["entities", "0", "type"],
                    "value": "Log",
                }
            ],
        }
    )

    updated = _apply_yaml_operations(
        Path("docs/architecture.yaml"),
        "entities:\n  - id: log-entry\n    type: log\n",
        action.yaml_operations,
    )

    assert yaml.safe_load(updated)["entities"][0]["type"] == "Log"


def test_yaml_edit_remove_item_supports_validator_list_indices() -> None:
    action = _parse_action_response(
        {
            "action": "yaml_edit",
            "file_path": "docs/system-specification.yaml",
            "operations": [
                {
                    "op": "remove_item",
                    "section": "requirements",
                    "index": 0,
                }
            ],
        }
    )

    updated = _apply_yaml_operations(
        Path("docs/system-specification.yaml"),
        "requirements:\n  - id: null\n    description: null\n  - id: req-1\n",
        action.yaml_operations,
    )

    assert yaml.safe_load(updated) == {"requirements": [{"id": "req-1"}]}


def test_yaml_edit_remove_key_deletes_top_level_key() -> None:
    action = _parse_action_response(
        {
            "action": "yaml_edit",
            "file_path": "docs/system-specification.yaml",
            "operations": [{"op": "remove_key", "path": ["0"]}],
        }
    )

    updated = _apply_yaml_operations(
        Path("docs/system-specification.yaml"),
        "schema: v1\n'0': null\nrequirements: []\n",
        action.yaml_operations,
    )

    assert yaml.safe_load(updated) == {"schema": "v1", "requirements": []}


def test_yaml_edit_index_errors_include_repair_guidance() -> None:
    with pytest.raises(RuntimeError, match="Corrective action"):
        _parse_action_response(
            {
                "action": "yaml_edit",
                "file_path": "docs/system-specification.yaml",
                "operations": [
                    {
                        "op": "remove_item",
                        "section": "requirements",
                        "index": -1,
                    }
                ],
            }
        )

    with pytest.raises(RuntimeError, match="exactly one of id or index"):
        _parse_action_response(
            {
                "action": "yaml_edit",
                "file_path": "docs/system-specification.yaml",
                "operations": [
                    {
                        "op": "remove_item",
                        "section": "requirements",
                        "id": "req-1",
                        "index": 0,
                    }
                ],
            }
        )


@pytest.mark.parametrize("tool", ["internal", "shell"])
@pytest.mark.parametrize(
    ("command", "expected_action"),
    [
        (["powdrr-lift", "yaml-edit"], "yaml_edit"),
        (["powdrr-lift", "gather-context"], "gather_context"),
        (["powdrr-lift", "next-step"], "next_step"),
    ],
)
def test_first_class_command_must_use_first_class_action(
    tool: str, command: list[str], expected_action: str
) -> None:
    with pytest.raises(RuntimeError, match=f"first-class action `{expected_action}`"):
        _parse_action_response(
            {
                "action": "invoke_tool",
                "tool": tool,
                "parameters": {"command": command},
            }
        )


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
            "action": "edit",
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


def test_edit_failure_feedback_distinguishes_yaml_from_range_errors() -> None:
    action = _parse_action_response(
        {
            "action": "edit",
            "file_path": "implementation-specification.txt",
            "edits": [
                {
                    "kind": "replace",
                    "start_line": 40,
                    "end_line": 43,
                    "text": "  - id: corrected",
                }
            ],
        }
    )
    file_context = {"exists": True, "line_count": 70}

    yaml_feedback = _workflow_edit_failure_feedback(
        action,
        _WorkflowStructuredDocumentError(
            "Edited YAML file is invalid at line 14, column 3"
        ),
        file_context,
    )
    assert "Preserve surrounding mapping keys" in yaml_feedback
    assert "unescaped double quotes" in yaml_feedback
    assert "current file has 70 lines" not in yaml_feedback

    range_feedback = _workflow_edit_failure_feedback(
        action,
        _WorkflowEditRangeError("range ends at line 74, but the file has 70 lines"),
        file_context,
    )
    assert "current file has 70 lines" in range_feedback


def test_action_repair_prompt_includes_the_rejected_edit() -> None:
    action = _parse_action_response(
        {
            "action": "edit",
            "file_path": "implementation-specification.txt",
            "edits": [
                {
                    "kind": "replace",
                    "start_line": 21,
                    "end_line": 24,
                    "text": "entities:\n - id: invalid",
                }
            ],
        }
    )

    prompt = _action_repair_prompt(
        SkillCatalogEntry(Path("skill.yaml"), _build_skill()),
        failed_action=action,
    )

    assert "previous edit action failed and was not applied" in prompt
    assert '"file_path": "implementation-specification.txt"' in prompt
    assert "Do not repeat it unchanged" in prompt


def test_action_repair_prompt_explains_validation_errors() -> None:
    prompt = _action_repair_prompt(
        SkillCatalogEntry(Path("skill.yaml"), _build_skill()),
        validation_error="command does not match the declared template",
    )

    assert "returned a validation_error" in prompt
    assert "matches the current step's declared tool template exactly" in prompt


def test_prompt_user_repair_guidance_uses_text_and_current_step_shapes() -> None:
    step = SkillStep(
        description="Ask for the missing success criteria.",
        details="Collect the answer before continuing.",
        actions=("prompt_user",),
    )
    prompt = _action_repair_prompt(
        SkillCatalogEntry(Path("skill.yaml"), _build_skill()),
        current_step=step,
        validation_error=(
            'Workflow prompt_user action requires a string text field; use "text".'
        ),
    )

    assert '"action":"prompt_user"' in prompt
    assert '"text":"One clear English question?"' in prompt
    assert '"action":"next_step"' in prompt
    assert 'rename "prompt" to "text"' in prompt
    assert (
        "This step declares no tool invocations; do not return invoke_tool"
        not in prompt
    )
    assert "file_path and edits or file_edits for edit" not in prompt
    assert '"action":"yaml_edit"' not in prompt


def test_modular_action_prompt_has_canonical_prompt_user_shape() -> None:
    prompt = _modular_action_system_prompt(
        SkillStep(
            description="Ask a question.",
            actions=("prompt_user",),
        )
    )

    assert '"action":"prompt_user"' in prompt
    assert '"text":"What specific success criteria should this feature meet?"' in prompt
    assert "gather_context" not in prompt
    assert "invoke_tool" not in prompt
    assert "prompt_user" in prompt
    assert "next_step" in prompt
    assert "powdrr-lift yaml-edit" not in prompt


def test_validation_gate_prompt_requires_a_different_repair_strategy() -> None:
    step = SkillStep(
        description="Repair the generated YAML and pass all checks.",
        validation_gate={"id": "yaml-checks"},
    )

    prompt = _modular_action_system_prompt(step)

    assert (
        "failed result is a diagnosis, not permission to repeat the same edit" in prompt
    )
    assert "Never repeat an operation or semantically equivalent operation" in prompt
    assert "change the target or repair strategy" in prompt
    assert "wait for the deterministic obligation rerun" in prompt

    repair_prompt = _action_repair_prompt(
        SkillCatalogEntry(Path("skill.yaml"), _build_skill()),
        current_step=step,
    )
    assert "choose a materially different target or strategy" in repair_prompt


def test_edit_action_normalizes_fenced_json_before_validation(tmp_path: Path) -> None:
    json_path = tmp_path / "settings.json"
    json_path.write_text('{"name": "original"}\n', encoding="utf-8")
    action = _parse_action_response(
        {
            "action": "edit",
            "file_path": json_path.name,
            "edits": [
                {
                    "kind": "replace",
                    "start_line": 1,
                    "text": '```json\n{"name": "corrected"}\n```',
                }
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

    assert json.loads(json_path.read_text(encoding="utf-8")) == {"name": "corrected"}


def test_edit_action_rejects_invalid_json_before_writing(tmp_path: Path) -> None:
    json_path = tmp_path / "settings.json"
    json_path.write_text('{"name": "original"}\n', encoding="utf-8")
    action = _parse_action_response(
        {
            "action": "edit",
            "file_path": json_path.name,
            "edits": [
                {
                    "kind": "replace",
                    "start_line": 1,
                    "text": '{"name": }',
                }
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

    with pytest.raises(RuntimeError, match="Edited JSON file .*column"):
        _handle_workflow_action_edit(
            action,
            state,
            io.StringIO(),
            io.StringIO(),
            lambda: "",
            SkillChatConfig(skills_dir=tmp_path),
        )

    assert json_path.read_text(encoding="utf-8") == '{"name": "original"}\n'


def test_parse_json_object_accepts_fenced_json_and_surrounding_prose() -> None:
    assert _parse_json_object(
        'Here is the response:\n```json\n{"action": "next_step"}\n```',
        "workflow response",
    ) == {"action": "next_step"}
    assert _parse_json_object(
        'I chose this action: {"action": "complete", "text": "done"}',
        "workflow response",
    ) == {"action": "complete", "text": "done"}


def test_prompt_user_action_requires_nonempty_text() -> None:
    with pytest.raises(
        RuntimeError,
        match="properly formed English question",
    ):
        _parse_action_response({"action": "prompt_user", "text": "  "})


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
                    "action": "edit",
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
                    "action": "edit",
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
                return {"action": "next_step"}
            if call_index == 4:
                return {"action": "complete", "text": "Done."}
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


def test_workflow_fuzzy_match_failure_is_sent_back_to_llm_for_correction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    worktree_root = repo_root / ".worktrees" / "skill-chat-test"
    skills_dir = worktree_root / "skill-definitions"
    skills_dir.mkdir(parents=True)
    skill = _build_skill()
    save_skill(
        replace(
            skill,
            steps=(
                replace(
                    skill.steps[0],
                    tool_invocations=(
                        SkillToolInvocation(
                            tool="fuzzy-match",
                            command=(
                                "fuzzy-match",
                                ".",
                                "-name",
                                "<name>",
                                "-type",
                                "f",
                            ),
                        ),
                    ),
                ),
                *skill.steps[1:],
            ),
        ),
        skills_dir / "specify-a-feature.json",
    )
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
                    "selected_skill_reason": "The request needs the guided workflow.",
                    "next_question": None,
                    "ready_to_execute": True,
                }
            if call_index == 1:
                return {
                    "action": "invoke_tool",
                    "tool": "fuzzy-match",
                    "parameters": {"command": ["fuzzy-match", "."]},
                }
            if call_index == 2:
                assert "command" in messages[1]["content"]
                assert "does not match" in messages[1]["content"]
                prompt = json.loads(messages[1]["content"])
                assert '"validation_error"' in prompt["transcript"][-1]["content"]
                assert (
                    "workflow_tool_action_invalid"
                    in prompt["transcript"][-1]["content"]
                )
                assert (
                    "Return a corrected invoke_tool action"
                    in (prompt["transcript"][-1]["content"])
                )
                return {
                    "action": "invoke_tool",
                    "tool": "fuzzy-match",
                    "parameters": {
                        "command": [
                            "fuzzy-match",
                            ".",
                            "-name",
                            "project-structure.yaml",
                            "-type",
                            "f",
                        ]
                    },
                }
            if call_index == 3:
                return {"action": "complete", "text": "Done."}
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

    stderr = io.StringIO()
    exit_code = run_workflow_chat(
        SkillChatConfig(
            skills_dir=skills_dir,
            repo_root=repo_root,
            output_dir=Path("generated"),
            api_key="test-key",
            model="test-model",
        ),
        input_func=iter(["Find the project structure"]).__next__,
        stdout=io.StringIO(),
        stderr=stderr,
    )

    assert exit_code == 0
    assert "Workflow invoke_tool action failed" in stderr.getvalue()
    assert len(captured_messages) == 4
    error_records = [
        json.loads(line)
        for line in (repo_root / "workflow-llm-errors.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert any(
        record["phase"] == "action_validation_or_execution"
        and record["attempted_action"]["kind"] == "invoke_tool"
        and record["context"]["replay_state"]["transcript"]
        and record["context"]["prompt_builder_version"] == 1
        for record in error_records
    )


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
    shutil.copytree(
        source_repo_root,
        repo_root,
        ignore=shutil.ignore_patterns(
            ".git",
            ".worktrees",
            ".venv",
            ".pytest_cache",
            ".ruff_cache",
            ".mypy_cache",
            ".powdrr",
            "__pycache__",
            "*.pyc",
        ),
    )
    subprocess.run(
        ["git", "init", "-b", "main"],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=repo_root,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=repo_root,
        check=True,
    )
    subprocess.run(["git", "add", "-A"], cwd=repo_root, check=True)
    subprocess.run(
        ["git", "commit", "-m", "Create isolated test base"],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    )
    remote_repo = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "--bare", str(remote_repo)],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "remote", "add", "origin", str(remote_repo)],
        cwd=repo_root,
        check=True,
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
    system_spec_dir = "docs/proposals/display-related-photos"
    system_spec_filename = "system-specification.yaml"
    architecture_spec_filename = "architecture-specification.yaml"
    implementation_spec_filename = "implementation-specification.yaml"
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
            "modules": [
                {
                    "id": "related-photos-module",
                    "action": "added",
                    "relative_location": "src/related_photos",
                    "purpose": "Render related photos in the feature view.",
                },
            ],
            "tools": [
                {
                    "id": "related-photos-check",
                    "action": "added",
                    "related_modules": ["related-photos-module"],
                    "when_to_use": "Validate the related photos implementation.",
                    "template": "powdrr-lift evaluate",
                    "how_to_use": "Run the evaluator before review.",
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
            "modules": [
                {"id": "related-photos-module", "action": "added"},
            ],
            "tools": [
                {"id": "related-photos-check", "action": "added"},
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
    pr_spec_entry = {
        "id": "display-related-photos-pr-001",
        "intent": "Produce the related photos feature.",
        "justification": "The feature plan requires a durable proposed PR.",
        "acceptance_criteria": [
            {
                "id": "ac-display-related-photos",
                "description": (
                    "The proposed PR captures the feature scope and validation trail."
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
                    "architecture, and implementation specs."
                ),
            }
        ],
        "expected_outcomes": [
            {
                "id": "outcome-display-related-photos",
                "description": (
                    "The feature plan is ready for asynchronous implementation work."
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
    }
    pr_spec_yaml = yaml.safe_dump(
        {
            "schema": "https://powdrr.io/schemas/proposed-pr-specification-v1",
            "id": "display-related-photos",
            "feature_ids": _repo_feature_ids(repo_root),
            "proposed_prs": [
                {
                    **pr_spec_entry,
                    "id": "display-related-photos-pr-001",
                    "dependent_prs": [],
                    "entities": [
                        {
                            "id": "related-photo",
                            "action": "added",
                            "proposed_pr_id": "display-related-photos-pr-001",
                        },
                        {
                            "id": "gallery-photo",
                            "action": "added",
                            "proposed_pr_id": "display-related-photos-pr-001",
                        },
                    ],
                    "modules": [
                        {
                            "id": "related-photos-module",
                            "action": "added",
                            "proposed_pr_id": "display-related-photos-pr-001",
                        },
                        {
                            "id": "related-photos-module",
                            "action": "added",
                            "proposed_pr_id": "display-related-photos-pr-001",
                        },
                    ],
                    "tools": [
                        {
                            "id": "related-photos-check",
                            "action": "added",
                            "proposed_pr_id": "display-related-photos-pr-001",
                        },
                        {
                            "id": "related-photos-check",
                            "action": "added",
                            "proposed_pr_id": "display-related-photos-pr-001",
                        },
                    ],
                    "entity_relationships": [
                        {
                            "id": "related-photo-groups-with-gallery-photo",
                            "action": "added",
                            "proposed_pr_id": "display-related-photos-pr-001",
                        }
                    ],
                    "features": [
                        {
                            "id": "display-related-photos",
                            "action": "added",
                            "proposed_pr_id": "display-related-photos-pr-001",
                        }
                    ],
                    "decisions": [
                        {
                            "id": "display-related-photos-grid",
                            "action": "added",
                            "proposed_pr_id": "display-related-photos-pr-001",
                        }
                    ],
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
        parsed = yaml.safe_load(yaml_text)
        assert isinstance(parsed, dict)
        return {
            "action": "yaml_edit",
            "file_path": current_file["path"],
            "operations": [
                {"op": "set_value", "path": [key], "value": value}
                for key, value in parsed.items()
            ],
        }

    step_descriptions = [None] * 17

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
            Path("templates") / "execute-proposed-pr.yaml",
            Path("src") / "powdrr_lift" / "core" / "workflow_template_specification.py",
            Path("src") / "powdrr_lift" / "core" / "workflow_task_specification.py",
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
            self._nested_event_count = 0
            self._nested_validation_index = 0
            self._nested_invoked_steps: set[tuple[str, int, str]] = set()

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
            expected_step_description: str | None,
            expected_context_suffix: str | None,
            expected_event_count: int,
            expected_last_event_kind: str | None = None,
        ) -> dict[str, object]:
            prompt = json.loads(messages[1]["content"])
            execution_events = prompt["execution_events"]
            assert len(execution_events) >= self._nested_event_count
            assert prompt["execution_mode"] == "execute_selected_skill"
            assert prompt["selected_skill"]["name"] == "specify-a-feature"
            assert prompt["current_step_index"] == expected_step_index
            assert prompt["transcript"][0]["content"] == "Build exports"
            return prompt

        def complete_json(self, messages: list[dict[str, str]]) -> dict[str, object]:
            cast(list[list[dict[str, str]]], captured["messages"]).append(messages)
            prompt = json.loads(messages[1]["content"])
            if (
                self._call_index > 0
                and prompt["selected_skill"]["name"] != "specify-a-feature"
            ):
                assert prompt["selected_skill"]["name"] in {
                    "review-system",
                    "review-architecture",
                    "finish-pr-prep",
                    "create-pull-request",
                }
                self._nested_event_count += 1
                current_step = cast(dict[str, object], prompt["current_step"])
                step_index = int(prompt["current_step_index"])
                available_actions = prompt.get("available_actions", [])
                current_step_events = [
                    event
                    for event in prompt.get("execution_events", [])
                    if event.get("step_index") == step_index
                ]
                has_gather_event = any(
                    event.get("kind") == "gather_context"
                    for event in current_step_events
                )
                if (
                    prompt["selected_skill"]["name"] == "finish-pr-prep"
                    and step_index == 3
                ):
                    validation_gate = cast(
                        dict[str, object], prompt.get("validation_gate", {})
                    )
                    validation_commands = [
                        cast(
                            list[str],
                            cast(
                                dict[str, object],
                                cast(dict[str, object], obligation["expected_action"])[
                                    "parameters"
                                ],
                            )["command"],
                        )
                        for obligation in cast(
                            list[dict[str, object]],
                            validation_gate.get("obligations", []),
                        )
                    ]
                    if self._nested_validation_index < len(validation_commands):
                        command = validation_commands[self._nested_validation_index]
                        self._nested_validation_index += 1
                        self._call_index += 1
                        return {
                            "action": "invoke_tool",
                            "tool": "shell",
                            "parameters": {"command": command},
                        }
                if (
                    prompt["selected_skill"]["name"] == "finish-pr-prep"
                    and step_index == 2
                    and isinstance(available_actions, list)
                    and "gather_context" in available_actions
                    and not has_gather_event
                ):
                    self._call_index += 1
                    return {
                        "action": "gather_context",
                        "types": ["tools"],
                        "filters": {"labels": ["pr-prep"]},
                    }
                if (
                    prompt["selected_skill"]["name"] == "finish-pr-prep"
                    and step_index == 2
                    and has_gather_event
                ):
                    self._call_index += 1
                    return {
                        "action": "next_step",
                        "outputs": {"validation_tool_obligations": []},
                    }
                raw_tool_invocations = current_step.get("tool_invocations", [])
                tool_invocations = (
                    cast(list[dict[str, object]], raw_tool_invocations)
                    if isinstance(raw_tool_invocations, list)
                    else []
                )
                invocation = next(
                    (
                        item
                        for item in tool_invocations
                        if isinstance(item, dict) and item.get("tool") == "shell"
                    ),
                    tool_invocations[0] if tool_invocations else None,
                )
                invocation_tool = (
                    str(invocation.get("tool")) if isinstance(invocation, dict) else ""
                )
                nested_key = (
                    str(prompt["selected_skill"]["name"]),
                    int(prompt["current_step_index"]),
                    invocation_tool,
                )
                if (
                    isinstance(tool_invocations, list)
                    and tool_invocations
                    and nested_key not in self._nested_invoked_steps
                ):
                    self._nested_invoked_steps.add(nested_key)
                    invocation = cast(dict[str, object], invocation)
                    parameters = (
                        {"operation": invocation["operation"]}
                        if "operation" in invocation
                        else {"command": invocation["command"]}
                    )
                    return {
                        "action": "invoke_tool",
                        "tool": invocation["tool"],
                        "parameters": parameters,
                    }
                return {
                    "action": "next_step",
                    "decisions_and_context": (
                        "The existing specification already satisfies this review."
                    ),
                }
            elif 3 <= self._call_index <= 17:
                current_step = cast(dict[str, object], prompt["current_step"])
                assert prompt["selected_skill"]["name"] == "specify-a-feature"
                step_id = current_step.get("id")
                latest_action = prompt.get("latest_action")
                if step_id in {
                    "fill-system-specification",
                    "fill-architecture-specification",
                    "fill-implementation-specification",
                } and not (
                    isinstance(latest_action, dict)
                    and latest_action.get("kind") == "yaml_edit"
                ):
                    current_file = cast(dict[str, object], prompt["current_file"])
                    file_path = str(current_file["path"])
                    yaml_text = {
                        system_spec_filename: system_spec_yaml,
                        architecture_spec_filename: architecture_spec_yaml,
                        implementation_spec_filename: implementation_spec_yaml,
                    }[Path(file_path).name]
                    generic_response = _full_replace_edit(prompt, yaml_text=yaml_text)
                elif step_id == "stage-specification-artifacts" and not (
                    isinstance(latest_action, dict)
                    and latest_action.get("kind") == "invoke_tool"
                ):
                    generic_response = {
                        "action": "invoke_tool",
                        "tool": "git",
                        "parameters": {
                            "operation": "add",
                            "paths": ["docs/proposals/display-related-photos"],
                        },
                    }
                elif step_id == "stage-specification-artifacts":
                    generic_response = {
                        "action": "complete",
                        "text": "Feature specification complete.",
                    }
                else:
                    generic_response = {
                        "action": "next_step",
                        "decisions_and_context": "Completed the current step.",
                    }
                self._call_index += 1
                return generic_response
            elif 20 <= self._call_index <= 21:
                current_step = cast(dict[str, object], prompt["current_step"])
                nested_skills = current_step.get("uses_skills", [])
                if isinstance(nested_skills, list) and nested_skills:
                    nested_response = {
                        "action": "invoke_skill",
                        "skill": nested_skills[0],
                    }
                else:
                    nested_response = {"action": "next_step"}
                self._call_index += 1
                return nested_response
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
                    "action": "prompt_user",
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
                    "action": "next_step",
                    "outputs": {"work_item_name": "display-related-photos"},
                    "decisions_and_context": (
                        "Feature name captured: display-related-photos."
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
                    "action": "invoke_tool",
                    "tool": "internal",
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
                        "Goal captured: display related photos; success criteria: "
                        "show related photos in the UI."
                    ),
                    expected_event_count=4,
                    expected_last_event_kind="yaml_edit",
                )
                response = {
                    "action": "next_step",
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
                    expected_event_count=9,
                    expected_last_event_kind="gate",
                )
                current_step = cast(dict[str, object], prompt["current_step"])
                tool_invocations = cast(
                    list[dict[str, object]], current_step["tool_invocations"]
                )
                assert tool_invocations[0]["command"] == [
                    "powdrr-lift",
                    "evaluate",
                    "docs/proposals/<work-item-name>",
                ]
                response = {
                    "action": "invoke_tool",
                    "tool": "internal",
                    "parameters": {
                        "command": [
                            "powdrr-lift",
                            "evaluate",
                            "docs/proposals/display-related-photos",
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
                    expected_event_count=10,
                    expected_last_event_kind="invoke_tool",
                )
                response = {
                    "action": "next_step",
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
                    expected_event_count=11,
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
                    "action": "invoke_tool",
                    "tool": "internal",
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
                    expected_event_count=12,
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
                    expected_event_count=13,
                    expected_last_event_kind="yaml_edit",
                )
                response = {
                    "action": "next_step",
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
                    expected_event_count=18,
                    expected_last_event_kind="gate",
                )
                current_step = cast(dict[str, object], prompt["current_step"])
                tool_invocations = cast(
                    list[dict[str, object]], current_step["tool_invocations"]
                )
                assert tool_invocations[0]["command"] == [
                    "powdrr-lift",
                    "evaluate",
                    "docs/proposals/<work-item-name>",
                ]
                response = {
                    "action": "invoke_tool",
                    "tool": "internal",
                    "parameters": {
                        "command": [
                            "powdrr-lift",
                            "evaluate",
                            "docs/proposals/display-related-photos",
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
                    expected_event_count=19,
                    expected_last_event_kind="invoke_tool",
                )
                response = {
                    "action": "next_step",
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
                    expected_event_count=20,
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
                    "action": "invoke_tool",
                    "tool": "internal",
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
                    expected_event_count=20,
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
                    expected_event_count=20,
                    expected_last_event_kind="yaml_edit",
                )
                response = {
                    "action": "invoke_tool",
                    "tool": "internal",
                    "parameters": {
                        "command": [
                            "powdrr-lift",
                            "evaluate",
                            "docs/proposals/display-related-photos",
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
                    expected_event_count=20,
                    expected_last_event_kind="invoke_tool",
                )
                response = {
                    "action": "next_step",
                    "decisions_and_context": (
                        "Implementation step complete; use this spec for PR scope."
                    ),
                }
            elif self._call_index == 18:
                self._assert_execution_prompt(
                    messages,
                    expected_step_index=16,
                    expected_step_description=step_descriptions[16],
                    expected_context_suffix="Implementation step complete; use this spec for PR scope.",
                    expected_event_count=20,
                    expected_last_event_kind="gate",
                )
                response = {
                    "action": "invoke_tool",
                    "tool": "git",
                    "parameters": {
                        "operation": "add",
                        "paths": ["docs/proposals/display-related-photos"],
                    },
                    "decisions_and_context": "Specification artifacts are staged.",
                }
            elif self._call_index == 19:
                self._assert_execution_prompt(
                    messages,
                    expected_step_index=16,
                    expected_step_description=step_descriptions[16],
                    expected_context_suffix="Specification artifacts are staged.",
                    expected_event_count=21,
                    expected_last_event_kind="invoke_tool",
                )
                response = {
                    "action": "complete",
                    "text": "Feature specification complete.",
                    "decisions_and_context": "Specification validation is complete.",
                }
            elif self._call_index == 22:
                self._assert_execution_prompt(
                    messages,
                    expected_step_index=18,
                    expected_step_description=(
                        "Stage the validated specification artifacts for pull request preparation."
                    ),
                    expected_context_suffix="All specification issues are fixed.",
                    expected_event_count=32,
                    expected_last_event_kind="gate",
                )
                response = {
                    "action": "invoke_tool",
                    "tool": "git",
                    "parameters": {
                        "operation": "add",
                        "paths": ["docs/proposals/display-related-photos"],
                    },
                    "decisions_and_context": "Specification artifacts are staged.",
                }
            elif self._call_index == 23:
                self._assert_execution_prompt(
                    messages,
                    expected_step_index=18,
                    expected_step_description=(
                        "Stage the validated specification artifacts for pull request preparation."
                    ),
                    expected_context_suffix="Specification artifacts are staged.",
                    expected_event_count=32,
                    expected_last_event_kind="invoke_tool",
                )
                response = {
                    "action": "complete",
                    "text": "Feature specification complete.",
                    "decisions_and_context": "Specification validation is complete.",
                }
            elif self._call_index == 24:
                self._assert_execution_prompt(
                    messages,
                    expected_step_index=18,
                    expected_step_description=(
                        "Stage the validated specification artifacts for pull request preparation."
                    ),
                    expected_context_suffix="Specification artifacts are staged.",
                    expected_event_count=32,
                    expected_last_event_kind="invoke_tool",
                )
                response = {
                    "action": "complete",
                    "text": "Feature specification complete.",
                    "decisions_and_context": "Specification validation is complete.",
                }
            else:
                current_step = cast(dict[str, object], prompt["current_step"])
                latest_action = prompt.get("latest_action")
                if current_step.get("id") == "stage-specification-artifacts":
                    if (
                        isinstance(latest_action, dict)
                        and latest_action.get("kind") == "invoke_tool"
                    ):
                        response = {"action": "complete"}
                    else:
                        response = {
                            "action": "invoke_tool",
                            "tool": "git",
                            "parameters": {
                                "operation": "add",
                                "paths": ["docs/proposals/display-related-photos"],
                            },
                        }
                else:
                    response = {"action": "next_step"}

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

    def _fake_shell_tool(
        parameters: dict[str, object], *, worktree_root: Path, **_: object
    ) -> dict[str, object]:
        command = parameters.get("command")
        if isinstance(command, (list, tuple)) and len(command) >= 2:
            generated_name = {
                "system-specification": system_spec_filename,
                "architecture-specification": architecture_spec_filename,
                "implementation-specification": implementation_spec_filename,
            }.get(str(command[1]))
            if generated_name is not None:
                generated_path = worktree_root / system_spec_dir / generated_name
                generated_path.parent.mkdir(parents=True, exist_ok=True)
                generated_path.write_text(
                    {
                        system_spec_filename: system_spec_yaml,
                        architecture_spec_filename: architecture_spec_yaml,
                        implementation_spec_filename: implementation_spec_yaml,
                    }[generated_name],
                    encoding="utf-8",
                )
        return {
            "command": command,
            "cwd": str(worktree_root),
            "returncode": 0,
            "stdout": "",
            "stderr": "",
            "status": "success",
            "validation_successful": True,
        }

    monkeypatch.setattr(
        "powdrr_lift.workflow_chat_agent._execute_shell_tool", _fake_shell_tool
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

    assert summary_path.exists()
    assert system_path.exists()
    assert architecture_path.exists()
    assert implementation_path.exists()

    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert summary["selected_skill_name"] == "specify-a-feature"
    event_kinds = [event["kind"] for event in summary["execution_events"]]
    assert event_kinds[0:2] == ["prompt_user", "next_step"]
    assert event_kinds[-1] in {"complete", "next_step"}
    assert event_kinds.count("yaml_edit") == 3
    assert event_kinds.count("deterministic_pre_step") >= 7

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

    _assert_validation_success(system_report, label="system")
    _assert_validation_success(architecture_report, label="architecture")
    _assert_validation_success(implementation_report, label="implementation")
    assert "Wrote skill execution summary to" in stdout.getvalue()
    assert "Would you please review the draft result?" not in stdout.getvalue()

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
                "action": "next_step",
                "decisions_and_context": "The feature and work item are confirmed.",
            },
            {
                "action": "next_step",
                "decisions_and_context": "The project structure bootstrap is complete.",
            },
            {
                "action": "next_step",
                "decisions_and_context": "The workflow candidates are discovered.",
            },
            {
                "action": "next_step",
                "outputs": {"feature_name": "display-related-photos"},
                "decisions_and_context": "The canonical feature context is selected.",
            },
            {
                "action": "read_document",
                "file_path": "templates/execute-proposed-pr.yaml",
                "start_line": 1,
                "end_line": 260,
                "decisions_and_context": "The execute-proposed-pr template is selected.",
            },
            {
                "action": "next_step",
                "decisions_and_context": "The generated task graph is appropriate.",
            },
            {
                "action": "invoke_tool",
                "tool": "internal",
                "parameters": {
                    "command": [
                        "powdrr-lift",
                        "instantiate-workflow",
                        "--work-item-name",
                        "display-related-photos",
                        "--template-value",
                        "proposed-pr-id=display-related-photos-pr-001",
                        "--template",
                        "templates/execute-proposed-pr.yaml",
                    ]
                },
                "decisions_and_context": "The durable implementation workflow is created.",
            },
            {
                "action": "next_step",
                "decisions_and_context": "The first task is ready for review.",
            },
            {
                "action": "next_step",
                "decisions_and_context": "The generated workflow looks correct.",
            },
            {
                "action": "next_step",
                "decisions_and_context": "The validated planning artifacts are staged.",
            },
            {
                "action": "next_step",
                "decisions_and_context": "Pull request preparation checks are complete.",
            },
            {
                "action": "next_step",
                "decisions_and_context": "The pull request description template is generated.",
            },
            {
                "action": "next_step",
                "decisions_and_context": "The pull request description is filled in.",
            },
            {
                "action": "next_step",
                "decisions_and_context": "The draft pull request is created.",
            },
            {
                "action": "next_step",
                "decisions_and_context": "The draft pull request is ready.",
            },
            {
                "action": "complete",
                "text": "Implementation workflow ready for review.",
                "decisions_and_context": "Stop until the user approves implementation.",
            },
        ]
    )

    start_captured: dict[str, object] = {"messages": []}

    class _FakeStartOpenAIClient:
        def __init__(self, **_: object) -> None:
            self._call_index = 0
            self._pr_template_invoked = False
            self._pr_stage_invoked = False
            self._pr_commit_invoked = False
            self._pr_push_invoked = False
            self._pr_create_invoked = False
            self._pr_update_invoked = False
            self._start_invoked_steps: set[int] = set()
            self._bootstrap_invoked_steps: set[int] = set()
            self._finish_invoked_steps: set[int] = set()
            self._finish_validation_commands: list[list[str]] | None = None
            self._finish_validation_index = 0
            self._finish_scope_invoked = False
            self._finish_context_gathered = False

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
                assert prompt["execution_mode"] == "execute_selected_skill"
                if prompt["selected_skill"]["name"] == "bootstrap-code-structure":
                    step_index = int(prompt["current_step_index"])
                    assert step_index < 8
                    shell_invocations = [
                        invocation
                        for invocation in prompt["current_step"].get(
                            "tool_invocations", []
                        )
                        if invocation.get("tool") == "shell"
                    ]
                    if (
                        shell_invocations
                        and step_index not in self._bootstrap_invoked_steps
                    ):
                        self._bootstrap_invoked_steps.add(step_index)
                        self._call_index += 1
                        return {
                            "action": "invoke_tool",
                            "tool": "shell",
                            "parameters": {"command": shell_invocations[0]["command"]},
                        }
                    self._call_index += 1
                    return {"action": "next_step"}
                if prompt["selected_skill"]["name"] == "finish-pr-prep":
                    step_index = int(prompt["current_step_index"])
                    assert step_index < 5
                    if step_index == 1 and not self._finish_scope_invoked:
                        self._finish_scope_invoked = True
                        self._call_index += 1
                        return {
                            "action": "invoke_tool",
                            "tool": "shell",
                            "parameters": {
                                "command": ["git", "diff", "--cached", "--stat"]
                            },
                        }
                    if step_index == 2 and not self._finish_context_gathered:
                        self._finish_context_gathered = True
                        self._call_index += 1
                        return {
                            "action": "gather_context",
                            "types": ["tools"],
                            "filters": {
                                "labels": ["pr-prep", "python"],
                            },
                        }
                    if step_index == 3:
                        validation_gate = cast(
                            dict[str, object], prompt.get("validation_gate", {})
                        )
                        obligations = cast(
                            list[dict[str, object]],
                            validation_gate.get("obligations", []),
                        )
                        if self._finish_validation_commands is None:
                            self._finish_validation_commands = [
                                cast(
                                    list[str],
                                    cast(
                                        dict[str, object],
                                        cast(
                                            dict[str, object],
                                            obligation["expected_action"],
                                        )["parameters"],
                                    )["command"],
                                )
                                for obligation in obligations
                            ]
                        if self._finish_validation_index < len(
                            self._finish_validation_commands
                        ):
                            command = self._finish_validation_commands[
                                self._finish_validation_index
                            ]
                            self._finish_validation_index += 1
                            self._call_index += 1
                            return {
                                "action": "invoke_tool",
                                "tool": "shell",
                                "parameters": {"command": command},
                            }
                    shell_invocations = [
                        invocation
                        for invocation in prompt["current_step"].get(
                            "tool_invocations", []
                        )
                        if invocation.get("tool") == "shell"
                    ]
                    if (
                        shell_invocations
                        and step_index not in self._finish_invoked_steps
                    ):
                        self._finish_invoked_steps.add(step_index)
                        self._call_index += 1
                        return {
                            "action": "invoke_tool",
                            "tool": "shell",
                            "parameters": {"command": shell_invocations[0]["command"]},
                        }
                    self._call_index += 1
                    return {"action": "next_step"}
                if prompt["selected_skill"]["name"] == "create-pull-request":
                    assert prompt["current_step_index"] < 7
                    if (
                        prompt["current_step_index"] == 0
                        and not self._pr_template_invoked
                    ):
                        self._pr_template_invoked = True
                        self._call_index += 1
                        return {"action": "next_step"}
                    if prompt["current_step_index"] == 2 and not self._pr_stage_invoked:
                        self._pr_stage_invoked = True
                        self._call_index += 1
                        return {
                            "action": "invoke_tool",
                            "tool": "shell",
                            "parameters": {
                                "command": [
                                    "git",
                                    "add",
                                    "docs/proposals/display-related-photos",
                                ]
                            },
                        }
                    if (
                        prompt["current_step_index"] == 3
                        and not self._pr_commit_invoked
                    ):
                        self._pr_commit_invoked = True
                        self._call_index += 1
                        return {
                            "action": "invoke_tool",
                            "tool": "shell",
                            "parameters": {"command": ["git", "commit", "-m", "test"]},
                        }
                    if prompt["current_step_index"] == 4 and not self._pr_push_invoked:
                        self._pr_push_invoked = True
                        self._call_index += 1
                        return {
                            "action": "invoke_tool",
                            "tool": "shell",
                            "parameters": {
                                "command": [
                                    "git",
                                    "push",
                                    "-u",
                                    "origin",
                                    "HEAD",
                                ]
                            },
                        }
                    if (
                        prompt["current_step_index"] == 5
                        and not self._pr_create_invoked
                    ):
                        self._pr_create_invoked = True
                        self._call_index += 1
                        return {
                            "action": "invoke_tool",
                            "tool": "gh",
                            "parameters": {
                                "operation": "pr_create",
                                "draft": True,
                                "title": "test",
                                "body": "test",
                            },
                        }
                    if (
                        prompt["current_step_index"] == 6
                        and not self._pr_update_invoked
                    ):
                        self._pr_update_invoked = True
                        self._call_index += 1
                        return {
                            "action": "invoke_tool",
                            "tool": "gh",
                            "parameters": {
                                "operation": "pr_edit",
                                "pr_reference": "123",
                                "title": "test",
                                "body": "test",
                            },
                        }
                    self._call_index += 1
                    return {"action": "next_step"}
                assert prompt["selected_skill"]["name"] == (
                    "start-implementing-feature"
                )
                step_index = int(prompt["current_step_index"])
                tool_invocations = prompt["current_step"].get("tool_invocations", [])
                if tool_invocations and step_index not in self._start_invoked_steps:
                    invocation = next(
                        (
                            item
                            for item in tool_invocations
                            if item.get("tool") == "shell"
                        ),
                        tool_invocations[0],
                    )
                    self._start_invoked_steps.add(step_index)
                    self._call_index += 1
                    command = [
                        str(part)
                        .replace("<feature-name>", "display-related-photos")
                        .replace("<proposed-pr-name>", "display-related-photos-pr-001")
                        .replace("<feature-id>", "display-related-photos")
                        for part in invocation["command"]
                    ]
                    return {
                        "action": "invoke_tool",
                        "tool": invocation["tool"],
                        "parameters": {"command": command},
                    }
                if prompt["current_step"].get("id") == "select-feature-context":
                    self._call_index += 1
                    return {
                        "action": "next_step",
                        "outputs": {"feature_name": "display-related-photos"},
                    }
                if prompt["current_step"].get("id") == "plan-proposed-prs":
                    self._call_index += 1
                    return {
                        "action": "next_step",
                        "outputs": {
                            "proposed_pr_names": ["display-related-photos-pr-001"]
                        },
                    }
                if (
                    prompt["current_step"].get("id")
                    == "generate-implementation-specifications"
                    and step_index in self._start_invoked_steps
                ):
                    self._call_index += 1
                    return {
                        "action": "next_step",
                        "outputs": {
                            "implementation_specification_paths": [
                                "docs/proposals/display-related-photos/"
                                "display-related-photos-pr-001-implementation-specification.yaml"
                            ]
                        },
                    }
                self._call_index += 1
                return {"action": "next_step"}
            response = next(start_responses)
            self._call_index += 1
            return response

    real_subprocess_run = subprocess.run

    def _fake_start_subprocess_run(*args: Any, **kwargs: Any) -> Any:
        command = args[0] if args else kwargs.get("args")
        if isinstance(command, list) and (
            command[:3] == ["rtk", "powdrr-lift", "instantiate-workflow"]
            or command[:4] == ["uv", "run", "powdrr-lift", "instantiate-workflow"]
        ):
            generated_pr_path = (
                worktree_root / system_spec_dir / "proposed-pr-specification.yaml"
            )
            if not generated_pr_path.exists():
                generated_pr_path.parent.mkdir(parents=True, exist_ok=True)
                generated_pr_path.write_text(pr_spec_yaml, encoding="utf-8")
            captured_stdout = io.StringIO()
            captured_stderr = io.StringIO()
            with redirect_stdout(captured_stdout), redirect_stderr(captured_stderr):
                returncode = main(command[2:] if command[0] == "rtk" else command[3:])
            return subprocess.CompletedProcess(
                command,
                returncode,
                stdout=captured_stdout.getvalue(),
                stderr=captured_stderr.getvalue(),
            )
        if isinstance(command, list) and "pr-specification" in command:
            generated_pr_path = (
                worktree_root / system_spec_dir / "proposed-pr-specification.yaml"
            )
            generated_pr_path.parent.mkdir(parents=True, exist_ok=True)
            generated_pr_path.write_text(pr_spec_yaml, encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        if isinstance(command, list) and (
            command[:2] == ["rtk", "powdrr-lift"]
            or command[:3] == ["uv", "run", "powdrr-lift"]
        ):
            return subprocess.CompletedProcess(
                command,
                0,
                stdout="Implementation specifications are valid.\n",
                stderr="",
            )
        if isinstance(command, list) and (
            command[:2] == ["rtk", "gh"] or command[:2] == ["gh", "pr"]
        ):
            if "--json" in command:
                return subprocess.CompletedProcess(
                    command,
                    0,
                    stdout='{"url":"https://github.com/example/repo/pull/123","body":""}\n',
                    stderr="",
                )
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

    real_execute_shell_tool = _execute_shell_tool

    def _fake_pr_shell_tool(
        parameters: dict[str, object],
        *,
        worktree_root: Path,
        stdout: TextIO,
        stderr: TextIO,
        verbose: bool,
        announce: bool = True,
        print_stdout: bool = True,
    ) -> dict[str, object]:
        command = parameters.get("command")
        command_items = list(command) if isinstance(command, list) else []
        if command_items[:1] == ["rtk"]:
            command_items = command_items[1:]
        if command_items[:3] == ["uv", "run", "powdrr-lift"]:
            command_items = command_items[2:]
        if command_items and command_items[0] == "powdrr-lift":
            if len(command_items) > 1 and command_items[1] == "pr-specification":
                generated_pr_path = (
                    worktree_root / system_spec_dir / "proposed-pr-specification.yaml"
                )
                generated_pr_path.parent.mkdir(parents=True, exist_ok=True)
                generated_pr_path.write_text(pr_spec_yaml, encoding="utf-8")
                return {
                    "command": " ".join(str(item) for item in command_items),
                    "cwd": str(worktree_root),
                    "returncode": 0,
                    "stdout": "",
                    "stderr": "",
                }
            if len(command_items) <= 1 or command_items[1] != "instantiate-workflow":
                return {
                    "command": " ".join(str(item) for item in command_items),
                    "cwd": str(worktree_root),
                    "returncode": 0,
                    "stdout": "{}",
                    "stderr": "",
                }
            generated_pr_path = (
                worktree_root / system_spec_dir / "proposed-pr-specification.yaml"
            )
            if not generated_pr_path.exists():
                generated_pr_path.parent.mkdir(parents=True, exist_ok=True)
                generated_pr_path.write_text(pr_spec_yaml, encoding="utf-8")
            captured_stdout = io.StringIO()
            captured_stderr = io.StringIO()
            with redirect_stdout(captured_stdout), redirect_stderr(captured_stderr):
                returncode = main(
                    command_items[1:] + ["--repo-root", str(worktree_root)]
                )
            return {
                "command": " ".join(str(item) for item in command_items),
                "cwd": str(worktree_root),
                "returncode": returncode,
                "stdout": captured_stdout.getvalue(),
                "stderr": captured_stderr.getvalue(),
            }
        if command_items and command_items[0] != "powdrr-lift":
            if command_items[:3] == ["gh", "pr", "create"]:
                stdout.write("https://github.com/example/repo/pull/123\n")
            return {
                "command": " ".join(str(item) for item in command_items),
                "cwd": str(worktree_root),
                "returncode": 0,
                "stdout": "https://github.com/example/repo/pull/123\n"
                if command_items[:3] == ["gh", "pr", "create"]
                else "",
                "stderr": "",
            }
        return real_execute_shell_tool(
            parameters,
            worktree_root=worktree_root,
            stdout=stdout,
            stderr=stderr,
            verbose=verbose,
            announce=announce,
            print_stdout=print_stdout,
        )

    monkeypatch.setattr(
        "powdrr_lift.workflow_chat_agent._execute_shell_tool",
        _fake_pr_shell_tool,
    )
    monkeypatch.setattr(
        "powdrr_lift.execution.runtime.ExecutionRuntime.publish_readiness",
        lambda _runtime: types.SimpleNamespace(ready=True, reasons=()),
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
            max_turns=40,
        ),
        input_func=lambda: "Start implementing display related photos",
        stdout=start_stdout,
        stderr=start_stderr,
    )

    assert start_exit_code == 0, start_stderr.getvalue()
    start_summary_path = worktree_root / start_output_dir / "skill-execution.json"
    assert start_summary_path.exists()
    start_summary = json.loads(start_summary_path.read_text(encoding="utf-8"))
    assert start_summary["selected_skill_name"] == "start-implementing-feature"
    start_event_kinds = [event["kind"] for event in start_summary["execution_events"]]
    assert start_event_kinds[0] == "invoke_tool"
    assert start_event_kinds.count("next_step") >= 6
    assert start_event_kinds[-1] in {"complete", "next_step"}
    assert any(
        event["kind"] == "invoke_tool"
        and event.get("tool") == "gh"
        and event.get("parameters", {}).get("operation") == "pr_create"
        for event in start_summary["execution_events"]
    )

    instantiate_event = next(
        event
        for event in start_summary["execution_events"]
        if event["kind"] == "invoke_tool"
        and event.get("parameters", {}).get("command")
        and event["parameters"]["command"][1] == "instantiate-workflow"
    )
    instantiate_result = cast(dict[str, object], instantiate_event["result"])
    assert instantiate_result["returncode"] == 0, instantiate_result.get("stderr", "")
    instantiate_output = json.loads(cast(str, instantiate_result["stdout"]))
    integration_worktree = Path(instantiate_output["integration_worktree"])
    workflow_directory = Path(instantiate_output["workflow_directory"])
    workflow_state_files = sorted(workflow_directory.glob("*-workflow.yaml"))
    assert len(workflow_state_files) == 1
    tasks = load_workflow_tasks(workflow_directory)
    assert [task.task_id for task in tasks] == [
        f"display-related-photos-pr-001-task-{index:03d}" for index in range(1, 16)
    ], start_stdout.getvalue() + start_stderr.getvalue()
    assert [task.description for task in tasks] == [
        "Gather context about the proposed PR",
        "Create the execution plan",
        "Generate the planned tests",
        "Implement the planned product code",
        "Run all tests and fix failures",
        "Review specification completeness",
        "Repair specification completeness gaps",
        "Run formatting checks",
        "Run lint checks",
        "Run type checks",
        "Review the final diff for scope",
        "Repair final diff scope issues",
        "Promote the feature documents",
        "Verify the pull request file set",
        "Finish pull request preparation",
    ]
    assert [
        tuple(task_id[task_id.rfind("task-") :] for task_id in task.upstream_task_ids)
        for task in tasks
    ] == [
        (),
        ("task-001",),
        ("task-001", "task-002"),
        ("task-001", "task-002", "task-003"),
        ("task-004", "task-003"),
        ("task-001", "task-002", "task-004", "task-005"),
        ("task-006",),
        ("task-007",),
        ("task-008",),
        ("task-009",),
        ("task-008", "task-009", "task-010"),
        ("task-011",),
        ("task-001", "task-006", "task-012"),
        ("task-004", "task-013", "task-010"),
        (
            "task-001",
            "task-004",
            "task-005",
            "task-006",
            "task-008",
            "task-009",
            "task-010",
            "task-013",
            "task-014",
        ),
    ]
    assert all(task.status.value == "open" for task in tasks)
    assert select_ready_workflow_tasks(tasks) == (tasks[0],)
    assert "https://github.com/example/repo/pull/123" in start_stdout.getvalue()

    workflow_root = integration_worktree / "docs" / "workflows"
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
                if invocation.tool == "fuzzy-match":
                    result = execute_fuzzy_match(
                        command, worktree_root=integration_worktree
                    )
                    assert result["matches"], f"command={command!r} result={result!r}"
                elif command[:1] == ["pytest"]:
                    # This integration test uses a clone whose committed tests
                    # predate the template under test. The workflow command is
                    # asserted here; pytest execution is covered by the suite
                    # running in this worktree.
                    result = {"command": command, "returncode": 0}
                elif command[:2] == ["git", "mv"]:
                    # The generated execution worktree does not contain the
                    # separately committed proposal artifacts in this test.
                    result = {"command": command, "returncode": 0}
                else:
                    result = _execute_shell_tool(
                        {"command": command},
                        worktree_root=integration_worktree,
                        stdout=start_stdout,
                        stderr=start_stderr,
                        verbose=False,
                    )
                    assert result["returncode"] == 0, (
                        f"command={command!r} result={result!r}"
                    )
            save_workflow_task(
                replace(task, status=TaskStatus.COMPLETED),
                workflow_root / ready_task.work_item_name / f"{task.task_id}.yaml",
            )

    assert assignment_batches
    assert all(
        assignment
        in {
            ("agent", "architect"),
            ("agent", "coder"),
            ("agent", "reviewer"),
        }
        for assignment in assignment_batches
    )
    assert load_ready_workflow_tasks(workflow_root) == ()

    execute_tasks = load_workflow_tasks(workflow_root / "display-related-photos")
    assert len(execute_tasks) == 15
    assert execute_tasks[0].input_state["proposed_pr"] == (
        "display-related-photos-pr-001"
    )
    assert execute_tasks[0].llm_type == "long_context"
    assert [task.description for task in execute_tasks] == [
        "Gather context about the proposed PR",
        "Create the execution plan",
        "Generate the planned tests",
        "Implement the planned product code",
        "Run all tests and fix failures",
        "Review specification completeness",
        "Repair specification completeness gaps",
        "Run formatting checks",
        "Run lint checks",
        "Run type checks",
        "Review the final diff for scope",
        "Repair final diff scope issues",
        "Promote the feature documents",
        "Verify the pull request file set",
        "Finish pull request preparation",
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
                "action": "complete",
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
            provider="openai",
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
    assert '"action": "complete"' in stderr_value
    assert "Workflow chat LLM action:" in stderr_value
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
            {"action": "complete", "text": "Skill execution complete."},
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
    error_log = repo_root / "workflow-llm-errors.jsonl"
    assert error_log.exists()
    selection_error = json.loads(error_log.read_text(encoding="utf-8").splitlines()[0])
    assert selection_error["execution_mode"] == "select_skill"
    assert selection_error["context"]["request"] == "Build exports"
    assert selection_error["llm_output"]["next_question"] == "   "


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
                "action": "complete",
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
                "action": "complete",
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
            provider="zai",
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
                    "action": "next_step",
                    "decisions_and_context": "Step 1 complete.",
                }
            if call_index in (2, 3, 4):
                raise RuntimeError(
                    "OpenAI request failed with HTTP 429: "
                    '{"error":{"code":"1305","message":"temporarily overloaded"}}'
                )
            if call_index == 5:
                return {
                    "action": "complete",
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
            provider="openai",
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
                assert "Generate a JSON document selecting the best action" in (
                    repair_request
                )
                assert "The current step's action declarations below are the only" in (
                    repair_request
                )
                assert "kind" in repair_request
                return {
                    "action": "complete",
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
    error_records = [
        json.loads(line)
        for line in (repo_root / "workflow-llm-errors.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert any(
        record["phase"] == "llm_output_repair"
        and record["context"]["skill"]["step_index"] == 0
        for record in error_records
    )


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
                return {"action": "prompt_user", "text": "   "}
            if calls == 4:
                assert "non-empty" in messages[-1]["content"]
                return {
                    "action": "prompt_user",
                    "text": "What should this feature accomplish?",
                }
            if calls == 5:
                return {"action": "complete", "text": "Skill execution complete."}
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
                return {"action": "invoke_tool"}
            if calls == 3:
                raise RuntimeError("OpenAI response message content was empty.")
            if calls == 4:
                assert "The response cannot be empty" in messages[-1]["content"]
                raise RuntimeError("OpenAI response message content was empty.")
            if calls == 5:
                raise RuntimeError("OpenAI response message content was empty.")
            if calls == 6:
                return {"action": "complete", "text": "Skill execution complete."}
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
    assert stderr.getvalue().count("[workflow] Empty-response exchange: prompt=") >= 2
    assert "Would you like me to retry this LLM request?" not in stdout.getvalue()
    assert "corrective response was empty; interpreting it as next_step" in (
        stderr.getvalue()
    )
    assert "treating the step as complete" not in stderr.getvalue()
    assert "automatic repair retry" not in stderr.getvalue()
    assert "Waiting 30 seconds" not in stderr.getvalue()
    assert "Type 'retry' to try again or 'abort' to stop:" not in stdout.getvalue()


def test_work_item_names_match_natural_language_feature_requests(
    tmp_path: Path,
) -> None:
    specifications_root = tmp_path / "docs" / "proposals"
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
    specifications_root = tmp_path / "docs" / "proposals" / "interaction-file-log"
    specifications_root.mkdir(parents=True)
    (specifications_root / "proposed-pr-specification.yaml").touch()

    path_cache: dict[tuple[str, int, int | None], tuple[Path, ...]] = {}
    result = execute_fuzzy_match(
        [
            "fuzzy-match",
            "docs/proposals/interaction-file-log",
            "-name",
            "proposed PR specification",
            "-type",
            "f",
            "-maxdepth",
            "1",
            "-print",
        ],
        worktree_root=tmp_path,
        path_cache=path_cache,
    )
    cached_result = execute_fuzzy_match(
        [
            "fuzzy-match",
            "docs/proposals/interaction-file-log",
            "-name",
            "proposed PR specification",
            "-type",
            "f",
            "-maxdepth",
            "1",
            "-print",
        ],
        worktree_root=tmp_path,
        path_cache=path_cache,
    )

    assert [match["path"] for match in result["matches"]] == [
        "docs/proposals/interaction-file-log/proposed-pr-specification.yaml"
    ]
    assert result["matches"][0]["score"] == 1.0
    assert cached_result == result
    assert len(path_cache) == 1


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
    specifications_root = tmp_path / "docs" / "proposals" / "interaction-file-log"
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
        "docs/proposals/interaction-file-log/implementation-specification.yaml",
        "docs/proposals/interaction-file-log/system-specification.yaml",
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

    assert tool_invocations == []


def test_run_workflow_chat_executes_shell_tool_actions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    worktree_root = repo_root / ".worktrees" / "skill-chat-test"
    skills_dir = worktree_root / "skill-definitions"
    skills_dir.mkdir(parents=True)
    skill = _build_skill()
    save_skill(
        replace(
            skill,
            steps=(
                replace(
                    skill.steps[0],
                    tool_invocations=(
                        SkillToolInvocation(
                            tool="internal",
                            command=(
                                "powdrr-lift",
                                "system-specification",
                                "--work-item-name",
                                "<work-item-name>",
                            ),
                        ),
                    ),
                ),
                *skill.steps[1:],
            ),
        ),
        skills_dir / "specify-a-feature.json",
    )

    responses: Iterator[dict[str, object]] = iter(
        [
            {
                "selected_skill_path": str(skills_dir / "specify-a-feature.json"),
                "selected_skill_reason": "The request is to specify a feature.",
                "next_question": None,
                "ready_to_execute": True,
            },
            {
                "action": "invoke_tool",
                "tool": "internal",
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
                "action": "complete",
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
            announce=False,
        )

    assert run.call_args.args[0] == "rtk git status"
    assert run.call_args.kwargs["cwd"] == tmp_path.resolve()
    assert "Invoking" not in stdout.getvalue()
    assert "Invoking" not in stderr.getvalue()


def test_python_tool_variants_follow_declared_dependency_groups(
    tmp_path: Path,
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        """
[project]
name = "example"
[project.optional-dependencies]
dev = ["pytest", "ruff>=0.8"]
[dependency-groups]
quality = ["mypy"]
""",
        encoding="utf-8",
    )

    assert dependency_backed_command_variants(
        ["uv", "run", "ruff", "format", "--check", "."],
        project_root=tmp_path,
    )[0].command == ("uv", "run", "--extra", "dev", "ruff", "format", "--check", ".")
    assert dependency_backed_command_variants(
        ["uv", "run", "mypy", "src"],
        project_root=tmp_path,
    )[0].command == ("uv", "run", "--group", "quality", "mypy", "src")
    assert (
        dependency_backed_command_variants(
            ["uv", "run", "ruff", "format", "--check", ".", "--extra", "dev"],
            project_root=tmp_path,
        )
        == ()
    )


def test_execute_shell_tool_retries_missing_uv_tool_with_declared_extra(
    tmp_path: Path,
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        """
[project]
name = "example"
[project.optional-dependencies]
dev = ["ruff>=0.8"]
""",
        encoding="utf-8",
    )
    missing = subprocess.CompletedProcess(
        ["rtk", "uv", "run", "ruff"],
        1,
        stdout="",
        stderr="error: Failed to spawn: ruff\n",
    )
    success = subprocess.CompletedProcess(
        ["rtk", "uv", "run", "--extra", "dev", "ruff"],
        0,
        stdout="ruff passed\n",
        stderr="",
    )

    with patch(
        "powdrr_lift.workflow_chat_agent.subprocess.run",
        side_effect=[missing, success],
    ) as run:
        result = _execute_shell_tool(
            {"command": ["uv", "run", "ruff", "format", "--check", "."]},
            worktree_root=tmp_path,
            stdout=io.StringIO(),
            stderr=io.StringIO(),
            verbose=False,
            announce=False,
        )

    assert result["returncode"] == 0
    assert result["command"] == "rtk uv run --extra dev ruff format --check ."
    assert result["attempted_commands"] == [
        "rtk uv run ruff format --check .",
        "rtk uv run --extra dev ruff format --check .",
    ]
    assert missing_executable_output(stdout="", stderr="Failed to spawn: ruff")
    assert run.call_args_list[1].args[0] == [
        "rtk",
        "uv",
        "run",
        "--extra",
        "dev",
        "ruff",
        "format",
        "--check",
        ".",
    ]


def test_execute_shell_tool_accepts_empty_commit_on_clean_worktree(
    tmp_path: Path,
) -> None:
    no_commit = subprocess.CompletedProcess(
        ["git", "commit", "-m", "message"],
        1,
        stdout="",
        stderr="nothing to commit, working tree clean\n",
    )
    clean_status = subprocess.CompletedProcess(
        ["git", "status", "--short"], 0, stdout="", stderr=""
    )
    with patch(
        "powdrr_lift.workflow_chat_agent.subprocess.run",
        side_effect=[no_commit, clean_status],
    ):
        result = _execute_shell_tool(
            {"command": ["git", "commit", "-m", "message"]},
            worktree_root=tmp_path,
            stdout=io.StringIO(),
            stderr=io.StringIO(),
            verbose=False,
            announce=False,
        )

    assert result["returncode"] == 0
    assert result["no_op"] is True
    assert "existing HEAD retained" in result["stdout"]


def test_execute_shell_tool_reports_corrective_action_for_dirty_empty_commit(
    tmp_path: Path,
) -> None:
    no_commit = subprocess.CompletedProcess(
        ["git", "commit", "-m", "message"],
        1,
        stdout="",
        stderr="nothing to commit\n",
    )
    dirty_status = subprocess.CompletedProcess(
        ["git", "status", "--short"], 0, stdout="?? agent_error.txt\n", stderr=""
    )
    with patch(
        "powdrr_lift.workflow_chat_agent.subprocess.run",
        side_effect=[no_commit, dirty_status],
    ):
        result = _execute_shell_tool(
            {"command": ["git", "commit", "-m", "message"]},
            worktree_root=tmp_path,
            stdout=io.StringIO(),
            stderr=io.StringIO(),
            verbose=False,
            announce=False,
        )

    assert result["returncode"] == 1
    assert "agent_error.txt" in result["stderr"]
    assert '"operation":"add"' in result["stderr"]
    assert "file-management action" in result["stderr"]


def test_command_invocation_accepts_multiple_publish_files() -> None:
    expected = ("git", "add", "<files-to-publish>")

    assert _command_matches_invocation(
        ["git", "add", "docs/a.yaml", "docs/b.yaml"],
        expected,
    )
    assert not _command_matches_invocation(["git", "add"], expected)
    assert not _command_matches_invocation(
        ["git", "add", "docs/a.yaml", "--all"],
        ("git", "add", "<files-to-publish>", "--all"),
    )


def test_empty_pull_request_error_rejects_uncommitted_only_branch(
    tmp_path: Path,
) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    git_config = ["-c", "user.name=Test", "-c", "user.email=test@example.com"]
    subprocess.run(
        ["git", *git_config, "commit", "--allow-empty", "-m", "base"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "branch", "-M", "main"], cwd=tmp_path, check=True)
    (tmp_path / "new.txt").write_text("new\n", encoding="utf-8")

    error = _empty_pull_request_error(
        ["gh", "pr", "create", "--base", "main"],
        tmp_path,
    )

    assert error is not None
    assert "no commits exist between main and HEAD" in error
    assert "files_to_publish" in error


def test_execute_shell_tool_rejects_cwd_outside_worktree(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="must stay within the current worktree"):
        _execute_shell_tool(
            {"command": ["pwd"], "cwd": str(tmp_path.parent)},
            worktree_root=tmp_path,
            stdout=io.StringIO(),
            stderr=io.StringIO(),
            verbose=False,
        )


def test_empty_pull_request_error_reports_uncommitted_changes(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    git_config = ["-c", "user.name=Test", "-c", "user.email=test@example.com"]
    subprocess.run(
        ["git", *git_config, "commit", "--allow-empty", "-m", "base"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "branch", "-M", "main"], cwd=tmp_path, check=True)
    subprocess.run(["git", "switch", "-c", "feature"], cwd=tmp_path, check=True)
    (tmp_path / "change.txt").write_text("change\n", encoding="utf-8")

    error = _empty_pull_request_error(
        ["gh", "pr", "create", "--base", "main"],
        tmp_path,
    )

    assert error is not None
    assert "no commits exist between main and HEAD" in error
    assert "Uncommitted changes are present" in error


def test_empty_pull_request_error_ignores_branch_with_commits(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    git_config = ["-c", "user.name=Test", "-c", "user.email=test@example.com"]
    subprocess.run(
        ["git", *git_config, "commit", "--allow-empty", "-m", "base"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "branch", "-M", "main"], cwd=tmp_path, check=True)
    subprocess.run(["git", "switch", "-c", "feature"], cwd=tmp_path, check=True)
    (tmp_path / "change.txt").write_text("change\n", encoding="utf-8")
    subprocess.run(["git", "add", "change.txt"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", *git_config, "commit", "-m", "change"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )

    assert (
        _empty_pull_request_error(
            ["gh", "pr", "create", "--base", "main"],
            tmp_path,
        )
        is None
    )


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


def test_pytest_result_includes_deterministic_failure_packet() -> None:
    result = build_test_failure_packet(
        command=["uv", "run", "pytest", "-q"],
        returncode=1,
        stdout=(
            "FAILED tests/test_log_writer.py::test_json_format - TypeError: bad\n"
            "E   TypeError: bad\n"
            "E   src/log.py:12: in write\n"
        ),
        stderr="",
        cwd="/repo",
    )

    assert result == {
        "status": "failed",
        "failures": [
            {
                "node_id": "tests/test_log_writer.py::test_json_format",
                "exception": "TypeError",
                "message": "bad",
                "traceback_file": "src/log.py",
                "traceback_line": 12,
                "source_files": ["tests/test_log_writer.py", "src/log.py"],
            }
        ],
    }


def test_passing_pytest_result_includes_empty_failure_packet() -> None:
    assert build_test_failure_packet(
        command="uv run pytest -q",
        returncode=0,
        stdout="5 passed in 0.2s\n",
        stderr="",
        cwd="/repo",
    ) == {"status": "passed", "failures": []}


def test_shell_tool_returns_raw_result_without_format_specific_enrichment(
    tmp_path: Path,
) -> None:
    with patch("powdrr_lift.workflow_chat_agent.subprocess.run") as run:
        run.return_value.returncode = 0
        run.return_value.stdout = "10 passed\n"
        run.return_value.stderr = ""

        result = _execute_shell_tool(
            {"command": ["uv", "run", "pytest", "-q"]},
            worktree_root=tmp_path,
            stdout=io.StringIO(),
            stderr=io.StringIO(),
            verbose=False,
        )

    assert result["command"] == "rtk uv run pytest -q"
    assert result["returncode"] == 0
    assert "test_failure_packet" not in result


def test_execute_shell_tool_can_suppress_stdout_without_losing_result(
    tmp_path: Path,
) -> None:
    stdout = io.StringIO()
    stderr = io.StringIO()

    with patch("powdrr_lift.workflow_chat_agent.subprocess.run") as run:
        run.return_value.returncode = 0
        run.return_value.stdout = "generated PR template\n"
        run.return_value.stderr = ""

        result = _execute_shell_tool(
            {
                "command": [
                    "powdrr-lift",
                    "pull-request-description",
                    "--kind",
                    "feature",
                ]
            },
            worktree_root=tmp_path,
            stdout=stdout,
            stderr=stderr,
            verbose=True,
            announce=False,
            print_stdout=False,
        )

    assert stdout.getvalue() == ""
    assert "generated PR template" not in stderr.getvalue()
    assert result["stdout"] == "generated PR template\n"


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


def test_workflow_context_is_loaded_and_ad_hoc_reuses_previous_worktree(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    worktree_root = repo_root / ".worktrees" / "previous"
    worktree_root.mkdir(parents=True)
    context_path = repo_root / ".powdrr" / "workflow-context.json"
    context_path.parent.mkdir()
    context_path.write_text(
        json.dumps(
            {
                "worktree_root": str(worktree_root),
                "branch_name": "feature/previous",
                "pr_number": 215,
                "pr_url": "https://github.com/example/repo/pull/215",
                "skill_name": "start-implementing-feature",
                "request": "Build the feature",
            }
        ),
        encoding="utf-8",
    )
    context = _load_workflow_context(repo_root)
    skill = SkillCatalogEntry(
        repo_root / "handle-ad-hoc.yaml",
        Skill(
            name="handle-ad-hoc",
            when_to_use=("Handle follow-ups.",),
            steps=(),
        ),
    )

    assert context is not None
    assert context.pr_number == 215
    assert _worktree_reuse_decision("Check the typo", skill, context) is True


@pytest.mark.parametrize("state", ["CLOSED", "MERGED"])
def test_closed_workflow_pr_creates_a_new_worktree_without_prompting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    state: str,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    previous_worktree = repo_root / ".worktrees" / "previous"
    previous_worktree.mkdir(parents=True)
    new_worktree = repo_root / ".worktrees" / "new"
    new_worktree.mkdir(parents=True)
    context = WorkflowContext(worktree_root=previous_worktree, pr_number=215)
    skill = SkillCatalogEntry(
        repo_root / "handle-ad-hoc.yaml",
        Skill(
            name="handle-ad-hoc",
            when_to_use=("Handle follow-ups.",),
            steps=(),
        ),
    )
    captured: dict[str, object] = {}

    def _fake_run(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        captured["command"] = command
        captured["cwd"] = kwargs["cwd"]
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps({"state": state}),
            stderr="",
        )

    monkeypatch.setattr("powdrr_lift.workflow_chat_agent.subprocess.run", _fake_run)
    monkeypatch.setattr(
        "powdrr_lift.workflow_chat_agent._resolve_worktree_context",
        lambda repo_root, stderr, verbose: new_worktree,
    )

    stderr = io.StringIO()
    resolved = _resolve_worktree_for_request(
        repo_root,
        request="Check the typo",
        selected_skill=skill,
        context=context,
        input_func=lambda: pytest.fail("closed PRs should not prompt for reuse"),
        stdout=io.StringIO(),
        stderr=stderr,
        verbose=True,
    )

    assert resolved == new_worktree
    assert captured["command"] == [
        "gh",
        "pr",
        "view",
        "215",
        "--json",
        "state",
    ]
    assert captured["cwd"] == previous_worktree
    assert "pull request #215 is closed" in stderr.getvalue()


def test_worktree_reuse_decision_is_ambiguous_for_unrelated_new_skill(
    tmp_path: Path,
) -> None:
    worktree_root = tmp_path / ".worktrees" / "previous"
    worktree_root.mkdir(parents=True)
    context = WorkflowContext(worktree_root=worktree_root)
    skill = SkillCatalogEntry(
        tmp_path / "new.yaml",
        Skill(
            name="specify-a-feature",
            when_to_use=("Specify a feature.",),
            steps=(),
        ),
    )

    assert _worktree_reuse_decision("Build a different feature", skill, context) is None


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


def test_openai_chat_client_consumes_sse_content_and_reports_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FakeResponse:
        headers = {"Content-Type": "text/event-stream"}

        def __enter__(self) -> _FakeResponse:
            return self

        def __exit__(
            self,
            exc_type: object,
            exc: object,
            tb: object,
        ) -> None:
            return None

        def readline(self) -> bytes:
            return next(self._lines)

        _lines = iter(
            [
                b'data: {"choices":[{"delta":{"role":"assistant"}}]}\n',
                b"\n",
                b'data: {"choices":[{"delta":{"content":"{\\"ok\\": "}}]}\n',
                b"\n",
                b'data: {"choices":[{"delta":{"content":"true}"}}]}\n',
                b"\n",
                b"data: [DONE]\n",
                b"\n",
            ]
        )

    def _fake_urlopen(request: Request, timeout: float) -> _FakeResponse:
        body = json.loads(cast(bytes, request.data).decode("utf-8"))
        assert body["stream"] is True
        return _FakeResponse()

    monkeypatch.setattr("powdrr_lift.workflow_chat_agent.urlopen", _fake_urlopen)
    progress = io.StringIO()
    client = OpenAIChatClient(
        model="test-model",
        api_key="test-key",
        base_url="https://api.openai.com/v1",
        progress_stream=progress,
    )

    assert client.complete_json([{"role": "user", "content": "hello"}]) == {"ok": True}
    assert "received streamed LLM data" in progress.getvalue()


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
