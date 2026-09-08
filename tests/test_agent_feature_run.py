from __future__ import annotations

import json
import subprocess
from pathlib import Path

from powdrr_lift.agent_feature_run import (
    AgentFeatureRunConfig,
    run_agent_feature_e2e,
)


def test_agent_feature_run_prompts_agent_then_executes_generated_workflows(
    tmp_path: Path,
) -> None:
    workflow_root = tmp_path / "docs/workflows/interaction-file-log"
    workflow_root.mkdir(parents=True)
    (workflow_root / "interaction-file-log-core-task-001.yaml").write_text(
        "status: open\n", encoding="utf-8"
    )
    calls: list[list[str]] = []
    inputs: list[str] = []

    def runner(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        inputs.append(str(kwargs.get("input", "")))
        return subprocess.CompletedProcess(command, 0, "ok\n", "")

    result = run_agent_feature_e2e(
        AgentFeatureRunConfig(repo_root=tmp_path),
        runner=runner,
    )

    assert result.status == "passed"
    assert calls[0][3] == "workflow-chat"
    assert "I want to specify a feature" in inputs[0]
    assert any("start-implementing-feature-harness.py" in part for part in calls[1])
    assert "process-workflow-task" in calls[2]
    report = json.loads(
        (tmp_path / ".powdrr/agent-feature-run/report.json").read_text()
    )
    assert report["status"] == "passed"
    assert report["task_files_observed"]


def test_agent_feature_run_stops_after_specification_failure(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def failing_runner(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 1, "", "agent failed")

    result = run_agent_feature_e2e(
        AgentFeatureRunConfig(repo_root=tmp_path),
        runner=failing_runner,
    )

    assert result.status == "failed"
    assert len(calls) == 1
    assert result.phases[0]["name"] == "specify-a-feature"
