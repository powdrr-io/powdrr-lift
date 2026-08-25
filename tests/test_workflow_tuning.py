from __future__ import annotations

import json
import subprocess
from pathlib import Path

from powdrr_lift.workflow_tuning import save_workflow_tuning_report, tune_workflow


def _git(root: Path, *arguments: str) -> None:
    result = subprocess.run(
        ["git", *arguments], cwd=root, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr


def test_tuning_writes_a_portable_passing_report(tmp_path: Path) -> None:
    root = tmp_path / "repository"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "user.email", "test@example.invalid")
    (root / "skills").mkdir()
    definition = root / "skills" / "inspect.yaml"
    definition.write_text(
        """\
name: inspect
when_to_use: [Inspect files.]
steps:
  - id: inspect
    description: Inspect files.
    tool_invocations:
      - tool: shell
        command: [rg, --files]
""",
        encoding="utf-8",
    )
    replay = root / "replay.yaml"
    replay.write_text(
        """\
schema_version: 1
id: inspect
execution_mode: execute_selected_skill
definition: {kind: skill, path: skills/inspect.yaml, name: inspect}
step: {index: 0, id: inspect}
prompt_builder_version: 1
prompt_state: {}
failed_response:
  action: invoke_tool
  tool: shell
  parameters: {command: [rg, --files]}
expected: {}
redactions: []
""",
        encoding="utf-8",
    )
    _git(root, "add", ".")
    _git(root, "commit", "-m", "baseline")

    report = tune_workflow(
        definition=definition,
        repo_root=root,
        baseline_ref="HEAD",
        replay_paths=[replay],
    )
    report_path = root / "report.json"
    save_workflow_tuning_report(report_path, report)

    assert report["status"] == "passed"
    assert report["summary"]["cases"] == 1
    assert json.loads(report_path.read_text(encoding="utf-8"))["definition"] == (
        "skills/inspect.yaml"
    )
