from __future__ import annotations

import io
from contextlib import redirect_stdout
from pathlib import Path

from powdrr_lift.cli import main
from powdrr_lift.core import start_planning_feature


def test_start_planning_feature_reads_skill_template(tmp_path: Path) -> None:
    repo_root = _create_repo_with_planning_skill(tmp_path)

    instructions = start_planning_feature(
        work_item_name="powdrr-lift",
        repo_root=repo_root,
    )

    assert "plan-and-implement-feature" in instructions
    assert "powdrr-lift" in instructions
    assert "<work-item-name>" not in instructions
    assert "1. Build the system map." in instructions


def test_cli_start_planning_feature_prints_skill_instructions(tmp_path: Path) -> None:
    repo_root = _create_repo_with_planning_skill(tmp_path)

    stdout = io.StringIO()
    with redirect_stdout(stdout):
        exit_code = main(
            [
                "start-planning-feature",
                "--work-item-name",
                "powdrr-lift",
                "--repo-root",
                str(repo_root),
            ]
        )

    assert exit_code == 0
    output = stdout.getvalue()
    assert "plan-and-implement-feature" in output
    assert "powdrr-lift" in output
    assert "1. Build the system map." in output


def _create_repo_with_planning_skill(tmp_path: Path) -> Path:
    repo_root = tmp_path / "repo"
    skill_path = repo_root / "skills" / "plan-and-implement-feature"
    skill_path.mkdir(parents=True)
    (skill_path / "SKILL.md").write_text(
        """---
name: plan-and-implement-feature
description: Start planning a feature.
---

# Plan And Implement Feature

# - Use the work item folder `docs/specs/<work-item-name>`.

1. Build the system map.
2. Build the feature and PR template.
3. Plan and execute the code changes.
""",
        encoding="utf-8",
    )
    return repo_root
