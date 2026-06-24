from __future__ import annotations

import io
from contextlib import redirect_stdout
from pathlib import Path

import yaml

from powdrr_lift.cli import main
from powdrr_lift.core import (
    feature_pr_specification_default_output_path,
    system_map_specification_default_output_path,
)


def test_create_system_map_specification_template_writes_default_file(
    tmp_path: Path,
) -> None:
    output_path = system_map_specification_default_output_path(
        "powdrr-lift",
        tmp_path,
    )

    stdout = io.StringIO()
    with redirect_stdout(stdout):
        exit_code = main(
            [
                "system-map-specification",
                "--work-item-name",
                "powdrr-lift",
                "--repo-root",
                str(tmp_path),
            ]
        )

    assert exit_code == 0
    assert output_path.exists()
    assert str(output_path) in stdout.getvalue()
    template_text = output_path.read_text(encoding="utf-8")
    assert "# System map specification template." in template_text
    assert (
        "# - Analyze the full codebase deeply before writing anything." in template_text
    )
    assert "requirements:" in template_text
    assert "approach:" in template_text
    assert "entities:" in template_text
    assert "entity_relationships:" in template_text
    assert "invariants:" in template_text
    assert "guidance:" in template_text
    assert "features:" in template_text
    assert "decisions:" in template_text
    assert "feature_ids:" not in template_text

    rendered_template = yaml.safe_load(template_text)
    assert [section for section in rendered_template] == [
        "schema",
        "id",
        "title",
        "requirements",
        "approach",
        "entities",
        "entity_relationships",
        "invariants",
        "guidance",
        "features",
        "decisions",
    ]


def test_create_feature_pr_specification_template_writes_default_file(
    tmp_path: Path,
) -> None:
    output_path = feature_pr_specification_default_output_path(
        "powdrr-lift",
        tmp_path,
    )

    stdout = io.StringIO()
    with redirect_stdout(stdout):
        exit_code = main(
            [
                "feature-pr-specification",
                "--work-item-name",
                "powdrr-lift",
                "--repo-root",
                str(tmp_path),
            ]
        )

    assert exit_code == 0
    assert output_path.exists()
    assert str(output_path) in stdout.getvalue()
    template_text = output_path.read_text(encoding="utf-8")
    assert "# Feature and PR specification template." in template_text
    assert (
        "# - Start from the filled system map template and the requested feature."
        in template_text
    )
    assert "requirements:" in template_text
    assert "approach:" in template_text
    assert "entities:" in template_text
    assert "entity_relationships:" in template_text
    assert "invariants:" in template_text
    assert "guidance:" in template_text
    assert "features:" in template_text
    assert "decisions:" in template_text
    assert "feature_ids:" in template_text
    assert "intent:" in template_text
    assert "acceptance_criteria:" in template_text
    assert "expected_tests:" in template_text
    assert "expected_outcomes:" in template_text
    assert "non_goals:" in template_text
    assert "risks:" in template_text

    rendered_template = yaml.safe_load(template_text)
    assert [section for section in rendered_template] == [
        "schema",
        "id",
        "title",
        "requirements",
        "approach",
        "entities",
        "entity_relationships",
        "invariants",
        "guidance",
        "features",
        "decisions",
        "feature_ids",
        "intent",
        "acceptance_criteria",
        "expected_tests",
        "expected_outcomes",
        "non_goals",
        "risks",
    ]
