from __future__ import annotations

import subprocess
from pathlib import Path

import yaml

from powdrr_lift.cli import _evaluate_workflow_changed_files


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _setup_repo(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    (tmp_path / "README").write_text("base\n", encoding="utf-8")
    _git(tmp_path, "add", "README")
    _git(tmp_path, "commit", "-m", "base")
    _git(tmp_path, "switch", "-c", "feature")


def test_evaluate_discovers_changed_tasks_without_directory_arguments(
    tmp_path: Path,
) -> None:
    _setup_repo(tmp_path)
    templates = tmp_path / "templates"
    templates.mkdir()
    (templates / "workflow.yaml").write_text(
        yaml.safe_dump(
            {
                "when_to_use": ["test"],
                "how_to_fill_this_out": ["test"],
                "invariants": [
                    {
                        "id": "owns-target",
                        "relationship": "owns",
                        "cardinality": "exactly_one",
                    }
                ],
                "task_templates": [
                    {
                        "description": "Do work",
                        "complexity": "low",
                        "input_state": {},
                        "assignee_type": "agent",
                        "assignee_role": "coder",
                        "output_state_type": "state",
                        "dependent_state": [],
                    }
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    workflow = tmp_path / "generated" / "task.yaml"
    workflow.parent.mkdir()
    workflow.write_text(
        "task_id: example-task-001\nworkflow_template: workflow\n", encoding="utf-8"
    )
    state = tmp_path / "generated" / "state.yaml"
    state.write_text(
        yaml.safe_dump(
            {
                "invariants": [
                    {
                        "id": "owns-target",
                        "relationship": "owns",
                        "cardinality": "exactly_one",
                    }
                ],
                "relationships": [
                    {
                        "invariant_id": "owns-target",
                        "relationship": "owns",
                        "source_type": "workflow",
                        "source_id": "example",
                        "target_type": "artifact",
                        "target_id": "target",
                    }
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    _git(tmp_path, "add", "templates", "generated")
    _git(tmp_path, "commit", "-m", "workflow")

    assert (
        _evaluate_workflow_changed_files(repo_root=tmp_path, base_branch="main") is True
    )


def test_evaluate_rejects_changed_task_without_template_identity(
    tmp_path: Path,
) -> None:
    _setup_repo(tmp_path)
    task = tmp_path / "task.yaml"
    task.write_text("task_id: example-task-001\n", encoding="utf-8")
    _git(tmp_path, "add", "task.yaml")

    assert (
        _evaluate_workflow_changed_files(repo_root=tmp_path, base_branch="main")
        is False
    )
