from __future__ import annotations

import json
from pathlib import Path

from powdrr_lift.workflow_error_analysis import (
    cluster_workflow_errors,
    load_workflow_error_records,
    promote_replay_candidates,
    workflow_error_analysis_data,
)


def _record(
    record_id: str, *, error: str = "Action failed at /tmp/run-123"
) -> dict[str, object]:
    return {
        "record_id": record_id,
        "execution_mode": "execute_selected_skill",
        "phase": "action_validation_or_execution",
        "error_type": "RuntimeError",
        "error": error,
        "context": {
            "skill": {
                "name": "inspect",
                "path": "skill-definitions/inspect.yaml",
                "step_index": 0,
                "step_id": "inspect-files",
            },
            "replay_state": {},
        },
        "attempted_action": {
            "action": "invoke_tool",
            "tool": "shell",
            "parameters": {"command": ["rg", "--files"]},
        },
    }


def test_error_analysis_clusters_value_variant_failures_and_ranks_blocks(
    tmp_path: Path,
) -> None:
    log = tmp_path / "errors.jsonl"
    log.write_text(
        "\n".join(
            json.dumps(record)
            for record in (
                _record("one", error="Action failed at /tmp/run-123"),
                _record("two", error="Action failed at /tmp/run-456"),
                _record("three", error="Different error"),
            )
        )
        + "\n",
        encoding="utf-8",
    )

    clusters = cluster_workflow_errors(load_workflow_error_records([log]))

    assert len(clusters) == 2
    assert clusters[0].count == 2
    assert clusters[0].blocked_count == 2
    assert clusters[0].skill_or_task == "inspect"
    assert clusters[0].step == "inspect-files"
    assert workflow_error_analysis_data(clusters, record_count=3)["cluster_count"] == 2


def test_error_analysis_promotes_eligible_representative_to_replay_bundle(
    tmp_path: Path,
) -> None:
    skill_path = tmp_path / "skill-definitions" / "inspect.yaml"
    skill_path.parent.mkdir()
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
    record = _record("one")
    record["context"] = {
        "skill": {
            "name": "inspect",
            "path": str(skill_path),
            "step_index": 0,
            "step_id": "inspect-files",
        },
        "replay_state": {},
    }

    candidates = promote_replay_candidates(
        cluster_workflow_errors([record]),
        repo_root=tmp_path,
        output_dir=tmp_path / "replays",
    )

    assert candidates[0]["status"] == "promoted"
    assert Path(str(candidates[0]["path"])).is_file()
