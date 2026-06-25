from __future__ import annotations

import io
from contextlib import redirect_stdout
from pathlib import Path

import yaml

from powdrr_lift.cli import main
from powdrr_lift.core import (
    build_plan_diff_report,
    plan_diff_specification_default_output_path,
)


def test_build_plan_diff_report_detects_missing_feature_ids(tmp_path: Path) -> None:
    repo_root = tmp_path
    feature_plan_specification = _write_feature_plan_specification(
        repo_root,
        "powdrr-lift",
        features=["feature-a", "feature-b"],
    )
    changelog_path = _write_changelog(
        repo_root,
        "PR-1-changelog.yaml",
        features=["feature-a"],
    )

    report = build_plan_diff_report(
        feature_plan_specification_path=feature_plan_specification,
        changelog_paths=[changelog_path],
        repo_root=repo_root,
    )

    assert report.feature_plan_path == (
        "docs/specs/powdrr-lift/feature-pr-specification.yaml"
    )
    assert report.changelog_paths == ["docs/changelogs/PR-1-changelog.yaml"]
    assert [difference.kind for difference in report.differences] == [
        "missing_from_changelog"
    ]
    assert report.differences[0].section == "features"
    assert report.differences[0].plan_value == "feature-b"
    assert report.differences[0].source_spans
    assert report.differences[0].source_spans[0].path == "src/powdrr_lift/example.py"


def test_create_plan_diff_specification_writes_default_file(tmp_path: Path) -> None:
    repo_root = tmp_path
    feature_plan_specification = _write_feature_plan_specification(
        repo_root,
        "powdrr-lift",
        features=["feature-a"],
    )
    changelog_path = _write_changelog(
        repo_root,
        "PR-1-changelog.yaml",
        features=["feature-a"],
    )
    output_path = plan_diff_specification_default_output_path(
        feature_plan_specification,
        repo_root,
    )

    stdout = io.StringIO()
    with redirect_stdout(stdout):
        exit_code = main(
            [
                "plan-diff",
                "--feature-plan-specification",
                str(feature_plan_specification),
                "--changelog",
                str(changelog_path),
                "--repo-root",
                str(repo_root),
            ]
        )

    assert exit_code == 0
    assert output_path.exists()
    assert str(output_path) in stdout.getvalue()

    rendered_template = yaml.safe_load(output_path.read_text(encoding="utf-8"))
    assert rendered_template["schema"] == "https://powdrr.io/schema/plan-diff-v1"
    assert rendered_template["differences"] == []


def _write_feature_plan_specification(
    repo_root: Path,
    work_item_name: str,
    *,
    features: list[str],
) -> Path:
    feature_plan_specification = (
        repo_root / "docs" / "specs" / work_item_name / "feature-pr-specification.yaml"
    )
    feature_plan_specification.parent.mkdir(parents=True, exist_ok=True)
    feature_plan_specification.write_text(
        yaml.safe_dump(
            {
                "schema": "https://powdrr.io/schemas/specification-v1",
                "id": work_item_name,
                "title": "Feature Plan",
                "intent": {
                    "problem": "Problem",
                    "goal": "Goal",
                },
                "requirements": [],
                "approach": [],
                "entities": [],
                "entity_relationships": [],
                "invariants": [],
                "guidance": [],
                "features": [{"id": feature_id} for feature_id in features],
                "decisions": [],
                "acceptance_criteria": [{"id": "ac-1"}],
                "expected_tests": [{"id": "test-1"}],
                "expected_outcomes": [{"id": "outcome-1"}],
                "non_goals": [{"id": "non-goal-1"}],
                "risks": [{"id": "risk-1"}],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return feature_plan_specification


def _write_changelog(
    repo_root: Path,
    filename: str,
    *,
    features: list[str],
) -> Path:
    changelog_path = repo_root / "docs" / "changelogs" / filename
    changelog_path.parent.mkdir(parents=True, exist_ok=True)
    changelog_path.write_text(
        yaml.safe_dump(
            {
                "schema": "https://powdrr.io/schema/changelog-v2",
                "version": 2,
                "change_id": "PR-1",
                "title": "Feature Plan",
                "intent": {
                    "problem": "Problem",
                    "goal": "Goal",
                },
                "decisions": [],
                "structured_files": [],
                "files": [
                    {
                        "path": "src/powdrr_lift/example.py",
                        "span": {
                            "start_line": 1,
                            "end_line": 1,
                        },
                        "entities": [],
                        "related": {
                            "acceptance_criteria": ["ac-1"],
                            "expected_tests": ["test-1"],
                            "expected_outcomes": ["outcome-1"],
                            "non_goals": ["non-goal-1"],
                            "risks": ["risk-1"],
                        },
                    }
                ],
                "entities": [],
                "entity_relationships": [],
                "invariants": [],
                "guidance": [],
                "features": [
                    {"id": feature_id, "state": "implemented"}
                    for feature_id in features
                ],
                "proposed_prs": [],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return changelog_path
