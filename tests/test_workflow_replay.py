from __future__ import annotations

from pathlib import Path

import pytest

from powdrr_lift.workflow_replay import (
    WORKFLOW_REPLAY_BUNDLE_SCHEMA_VERSION,
    WorkflowReplayError,
    build_workflow_replay_state,
    load_workflow_replay_bundle,
    redact_replay_bundle,
    render_skill_replay,
    render_skill_replay_corpus,
    render_skill_replay_trajectory,
    replay_bundle_from_error_record,
    save_workflow_replay_bundle,
    validate_replay_fixture_safety,
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


def test_replay_corpus_reports_all_bundles_and_malformed_files(tmp_path: Path) -> None:
    skill_path = tmp_path / "skill.yaml"
    skill_path.write_text(
        """\
name: inspect
when_to_use: [Inspect files.]
steps:
  - id: inspect-files
    description: Inspect files.
    tool_invocations:
      - tool: shell
        command: [rg, --files]
""",
        encoding="utf-8",
    )
    corpus_dir = tmp_path / "replays"
    bundle = {
        "schema_version": 1,
        "id": "valid-bundle",
        "execution_mode": "execute_selected_skill",
        "definition": {"kind": "skill", "path": "skill.yaml", "name": "inspect"},
        "step": {"index": 0, "id": "inspect-files"},
        "prompt_builder_version": 1,
        "prompt_state": {},
        "failed_response": {
            "action": "invoke_tool",
            "tool": "shell",
            "parameters": {"command": ["rg", "--files"]},
        },
        "expected": {"response_valid": True, "action": "invoke_tool"},
        "redactions": [],
    }
    save_workflow_replay_bundle(corpus_dir / "valid.yaml", bundle)
    mismatch = dict(bundle)
    mismatch["id"] = "mismatch-bundle"
    mismatch["expected"] = {"response_valid": False}
    save_workflow_replay_bundle(corpus_dir / "mismatch.yaml", mismatch)
    (corpus_dir / "broken.yaml").write_text("not: [valid", encoding="utf-8")

    report = render_skill_replay_corpus(corpus_dir, repo_root=tmp_path)

    assert report["bundle_count"] == 3
    assert report["passed"] == 1
    assert report["failed"] == 2
    assert [item["valid"] for item in report["bundles"]] == [False, False, True]
    assert "expected response_valid=False" in report["bundles"][1]["error"]


def test_replay_trajectory_asserts_actions_and_roundtrip_bound(tmp_path: Path) -> None:
    (tmp_path / "skill.yaml").write_text(
        """\
name: inspect
when_to_use: [Inspect files.]
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
        "id": "trajectory",
        "execution_mode": "execute_selected_skill",
        "definition": {"kind": "skill", "path": "skill.yaml", "name": "inspect"},
        "step": {"index": 0, "id": "inspect-files"},
        "prompt_builder_version": 1,
        "prompt_state": {},
        "trajectory": [
            {
                "response": {
                    "action": "invoke_tool",
                    "tool": "shell",
                    "parameters": {"command": ["rg", "--files"]},
                }
            },
            {
                "response": {
                    "action": "invoke_tool",
                    "tool": "shell",
                    "parameters": {"command": ["rg", "--files"]},
                }
            },
        ],
        "expected": {
            "required_actions": ["invoke_tool"],
            "max_repeated_action_count": 1,
            "max_roundtrips": 2,
        },
        "redactions": [],
    }

    rendered = render_skill_replay_trajectory(bundle, repo_root=tmp_path)

    assert rendered["trajectory_validation"] == {
        "trajectory_valid": True,
        "required_actions": True,
        "max_repeated_action_count": True,
        "max_roundtrips": True,
        "valid": True,
    }


def test_replay_fixture_redaction_removes_credentials_and_absolute_paths() -> None:
    bundle = {
        "id": "unsafe",
        "prompt_state": {
            "message": "Read /private/tmp/worktree/file.py",
            "headers": {"authorization": "Bearer abcdefghijklmnopqrstuvwxyz"},
            "custom": "customer-secret-123",
        },
        "redactions": [],
    }

    redacted = redact_replay_bundle(
        bundle,
        secret_patterns=(r"customer-secret-\d+",),
    )

    assert redacted["prompt_state"]["message"] == "Read <repo-root>"
    assert redacted["prompt_state"]["headers"]["authorization"] == "<redacted>"
    assert redacted["prompt_state"]["custom"] == "<redacted>"
    assert len(redacted["redactions"]) == 3
    validate_replay_fixture_safety(redacted, secret_patterns=(r"customer-secret-\d+",))


def test_replay_fixture_safety_rejects_unredacted_sensitive_data() -> None:
    with pytest.raises(WorkflowReplayError, match="sensitive data"):
        validate_replay_fixture_safety(
            {"prompt_state": {"path": "/Users/example/project"}}
        )


def test_coding_loop_replay_corpus_exercises_completion_guards(tmp_path: Path) -> None:
    skill_path = tmp_path / "skill.yaml"
    skill_path.write_text(
        """\
name: coding-loop-replay
when_to_use:
  - Exercise coding-loop replay guards.
steps:
  - id: implement
    description: Implement and verify.
    step_type: coding_loop
    actions: [read_document, edit]
    coding_loop:
      goal: Make the check pass.
      verification:
        - id: check
          command: "true"
      stopping_conditions: [The check passes.]
      max_iterations: 4
""",
        encoding="utf-8",
    )
    corpus = (
        {
            "id": "failed-verification-read",
            "events": [
                {
                    "kind": "coding_loop_verification",
                    "step_index": 0,
                    "all_passed": False,
                }
            ],
            "response": {
                "action": "read_document",
                "file_path": "skill.yaml",
                "start_line": 1,
                "end_line": 10,
            },
            "valid": True,
        },
        {
            "id": "repeated-inspection-after-failure",
            "events": [
                {"kind": "read_document"},
                {
                    "kind": "coding_loop_verification",
                    "step_index": 0,
                    "all_passed": False,
                },
            ],
            "response": {
                "action": "read_document",
                "file_path": "skill.yaml",
                "start_line": 1,
                "end_line": 10,
            },
            "valid": True,
        },
        {
            "id": "redundant-action-after-pass",
            "events": [
                {
                    "kind": "coding_loop_verification",
                    "step_index": 0,
                    "all_passed": True,
                }
            ],
            "response": {
                "action": "read_document",
                "file_path": "skill.yaml",
                "start_line": 1,
                "end_line": 10,
            },
            "valid": False,
            "error": "already passed",
        },
        {
            "id": "stale-pass-completion",
            "events": [
                {
                    "kind": "coding_loop_verification",
                    "step_index": 0,
                    "all_passed": True,
                    "worktree_fingerprint": "stale-fingerprint",
                }
            ],
            "response": {"action": "next_step"},
            "valid": False,
            "error": "verification is stale",
        },
    )

    for case in corpus:
        bundle = {
            "schema_version": WORKFLOW_REPLAY_BUNDLE_SCHEMA_VERSION,
            "id": case["id"],
            "execution_mode": "execute_selected_skill",
            "definition": {
                "kind": "skill",
                "path": "skill.yaml",
                "name": "coding-loop-replay",
            },
            "step": {"index": 0, "id": "implement"},
            "prompt_builder_version": 1,
            "prompt_state": {"execution_events": case["events"]},
            "failed_response": case["response"],
            "expected": {},
            "redactions": [],
        }
        rendered = render_skill_replay(bundle, repo_root=tmp_path)
        validation = rendered["response_validation"]
        assert validation["valid"] is case["valid"]
        if not case["valid"]:
            assert case["error"] in validation["error"]


def test_committed_coding_loop_replay_fixtures_are_portable() -> None:
    corpus_root = Path(__file__).parents[1] / "workflow-evals/replays/coding-loop"
    expected = {
        "failed-verification-read.yaml": (True, None),
        "repeated-inspection-after-failure.yaml": (True, None),
        "redundant-action-after-pass.yaml": (False, "already passed"),
        "stale-pass-completion.yaml": (False, "verification is stale"),
    }

    for filename, (valid, error_text) in expected.items():
        bundle = load_workflow_replay_bundle(corpus_root / filename)
        rendered = render_skill_replay(bundle, repo_root=corpus_root)
        validation = rendered["response_validation"]
        assert validation["valid"] is valid
        if error_text is not None:
            assert error_text in validation["error"]
