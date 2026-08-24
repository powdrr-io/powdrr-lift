from __future__ import annotations

import json
import subprocess
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

from powdrr_lift.workflow_definition_comparison import (
    compare_workflow_definitions,
)


def _git(repo_root: Path, *arguments: str) -> None:
    result = subprocess.run(
        ["git", *arguments], cwd=repo_root, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr


def _write_skill_and_replay(repo_root: Path, command: str) -> Path:
    (repo_root / "skills").mkdir(exist_ok=True)
    (repo_root / "skills" / "inspect.yaml").write_text(
        f"""\
name: inspect
when_to_use: [Inspect files.]
steps:
  - id: inspect
    description: Inspect files.
    tool_invocations:
      - tool: shell
        command: [{command}, --files]
""",
        encoding="utf-8",
    )
    replay = repo_root / "inspect-replay.yaml"
    replay.write_text(
        """\
schema_version: 1
id: inspect-replay
execution_mode: execute_selected_skill
definition:
  kind: skill
  path: skills/inspect.yaml
  name: inspect
step:
  index: 0
  id: inspect
prompt_builder_version: 1
prompt_state: {}
failed_response:
  action: invoke_tool
  tool: shell
  parameters:
    command: [rg, --files]
expected: {}
redactions: []
""",
        encoding="utf-8",
    )
    return replay


def test_comparison_flags_candidate_replay_regression(tmp_path: Path) -> None:
    repo_root = tmp_path / "repository"
    repo_root.mkdir()
    _git(repo_root, "init", "-b", "main")
    _git(repo_root, "config", "user.name", "Test")
    _git(repo_root, "config", "user.email", "test@example.invalid")
    replay = _write_skill_and_replay(repo_root, "rg")
    _git(repo_root, "add", ".")
    _git(repo_root, "commit", "-m", "baseline")

    _write_skill_and_replay(repo_root, "git")
    report = compare_workflow_definitions(
        repo_root=repo_root,
        baseline_ref="HEAD",
        replay_paths=[replay],
    )

    assert report.passed is False
    assert report.baseline_metrics.valid_replays == 1
    assert report.candidate_metrics.invalid_replays == 1
    assert "passed on baseline but failed on candidate" in report.regressions[0]


def test_comparison_rejects_unknown_metric_threshold(tmp_path: Path) -> None:
    repo_root = tmp_path / "repository"
    repo_root.mkdir()
    _git(repo_root, "init", "-b", "main")
    _git(repo_root, "config", "user.name", "Test")
    _git(repo_root, "config", "user.email", "test@example.invalid")
    replay = _write_skill_and_replay(repo_root, "rg")
    _git(repo_root, "add", ".")
    _git(repo_root, "commit", "-m", "baseline")

    try:
        compare_workflow_definitions(
            repo_root=repo_root,
            baseline_ref="HEAD",
            replay_paths=[replay],
            thresholds={"tokens": 0},
        )
    except ValueError as exc:
        assert "thresholds" in str(exc)
    else:
        raise AssertionError("Expected an invalid threshold to be rejected.")


def test_cli_emits_json_comparison_report(tmp_path: Path) -> None:
    from powdrr_lift.cli import main

    repo_root = tmp_path / "repository"
    repo_root.mkdir()
    _git(repo_root, "init", "-b", "main")
    _git(repo_root, "config", "user.name", "Test")
    _git(repo_root, "config", "user.email", "test@example.invalid")
    replay = _write_skill_and_replay(repo_root, "rg")
    _git(repo_root, "add", ".")
    _git(repo_root, "commit", "-m", "baseline")

    _write_skill_and_replay(repo_root, "git")
    stdout = StringIO()
    with redirect_stdout(stdout), redirect_stderr(StringIO()):
        exit_code = main(
            [
                "compare-workflow-definitions",
                "--baseline-ref",
                "HEAD",
                "--replay",
                str(replay),
                "--repo-root",
                str(repo_root),
                "--json",
            ]
        )

    assert exit_code == 1
    assert json.loads(stdout.getvalue())["status"] == "regressed"
