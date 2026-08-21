from __future__ import annotations

import json
from pathlib import Path

from powdrr_lift.workflow_error_logging import (
    WORKFLOW_LLM_ERROR_LOG,
    record_workflow_llm_error,
)


def test_record_workflow_llm_error_appends_analysis_ready_jsonl(tmp_path: Path) -> None:
    first_path = record_workflow_llm_error(
        tmp_path,
        execution_mode="execute_selected_skill",
        phase="llm_output_parse",
        error=ValueError("feature is not a valid option"),
        context={
            "skill": {
                "name": "start-implementing-feature",
                "step_index": 2,
                "description": "Create the implementation PR.",
            }
        },
        llm_output={"kind": "invoke_tool", "parameters": {}},
        guidance="Use the declared tool invocation and include all required arguments.",
    )
    record_workflow_llm_error(
        tmp_path,
        execution_mode="process_workflow_task",
        phase="action_validation_or_execution",
        error=RuntimeError("missing required argument"),
        context={"task": {"task_id": "task-001"}},
        attempted_action={"kind": "invoke_tool"},
    )

    assert first_path == tmp_path / WORKFLOW_LLM_ERROR_LOG
    records = [
        json.loads(line) for line in first_path.read_text(encoding="utf-8").splitlines()
    ]
    assert len(records) == 2
    assert records[0]["schema_version"] == 1
    assert records[0]["execution_mode"] == "execute_selected_skill"
    assert records[0]["context"]["skill"]["name"] == ("start-implementing-feature")
    assert records[0]["llm_output"]["parameters"] == {}
    assert records[0]["guidance"].startswith("Use the declared")
    assert records[1]["phase"] == "action_validation_or_execution"
