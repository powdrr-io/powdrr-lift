from __future__ import annotations

from pathlib import Path

from powdrr_lift.workflow_replay import (
    WORKFLOW_REPLAY_BUNDLE_SCHEMA_VERSION,
    build_workflow_replay_state,
    load_workflow_replay_bundle,
    render_skill_replay,
    replay_bundle_from_error_record,
    save_workflow_replay_bundle,
)


def test_error_record_becomes_portable_replay_and_uses_production_prompt(
    tmp_path: Path,
) -> None:
    skill_path = tmp_path / "skill-definitions" / "inspect.yaml"
    skill_path.parent.mkdir()
    skill_path.write_text(
        """\
name: inspect
when_to_use:
  - Inspect the repository.
steps:
  - id: inspect-files
    description: Inspect files.
    tool_invocations:
      - tool: shell
        command: [rg, --files]
""",
        encoding="utf-8",
    )
    replay_state = build_workflow_replay_state(
        transcript=[{"role": "user", "content": "Inspect the repository."}],
        execution_events=[],
        execution_context=[],
        handoff_records={},
        durable_facts={},
        current_file_path=None,
        worktree_root=tmp_path,
        validation_gate=None,
        stalled_step_context=[],
    )
    record = {
        "record_id": "error-001",
        "recorded_at": "2026-08-24T00:00:00+00:00",
        "execution_mode": "execute_selected_skill",
        "phase": "action_validation_or_execution",
        "error_type": "RuntimeError",
        "error": "simulated failure",
        "context": {
            "skill": {
                "name": "inspect",
                "path": str(skill_path),
                "step_index": 0,
                "step_id": "inspect-files",
                "description": "Inspect files.",
            },
            "replay_state": replay_state,
        },
        "attempted_action": {
            "action": "invoke_tool",
            "tool": "shell",
            "parameters": {"command": ["rg", "--files"]},
        },
    }

    bundle = replay_bundle_from_error_record(record, repo_root=tmp_path)
    bundle_path = tmp_path / "replays" / "inspect.yaml"
    save_workflow_replay_bundle(bundle_path, bundle)

    loaded = load_workflow_replay_bundle(bundle_path)
    rendered = render_skill_replay(loaded, repo_root=tmp_path)

    assert loaded["schema_version"] == WORKFLOW_REPLAY_BUNDLE_SCHEMA_VERSION
    assert loaded["definition"]["path"] == "skill-definitions/inspect.yaml"
    assert rendered["response_validation"] == {
        "valid": True,
        "action": "invoke_tool",
    }
    assert rendered["prompt_messages"][1]["role"] == "user"
    assert "inspect-files" in rendered["prompt_messages"][1]["content"]


def test_replay_reports_invalid_recorded_action_without_executing_it(
    tmp_path: Path,
) -> None:
    skill_path = tmp_path / "skill.yaml"
    skill_path.write_text(
        """\
name: inspect
when_to_use:
  - Inspect the repository.
steps:
  - id: inspect-files
    description: Inspect files.
    tool_invocations:
      - tool: shell
        command: [rg, --files]
""",
        encoding="utf-8",
    )
    bundle = {
        "schema_version": 1,
        "id": "invalid-action",
        "execution_mode": "execute_selected_skill",
        "definition": {"kind": "skill", "path": "skill.yaml", "name": "inspect"},
        "step": {"index": 0, "id": "inspect-files"},
        "prompt_builder_version": 1,
        "prompt_state": {},
        "failed_response": {"action": "next_step"},
        "expected": {"error": "missing invocation"},
        "redactions": [],
    }

    rendered = render_skill_replay(bundle, repo_root=tmp_path)

    assert rendered["response_validation"]["valid"] is False
    assert (
        "requires a successful tool invocation"
        in rendered["response_validation"]["error"]
    )
