from __future__ import annotations

from pathlib import Path

import yaml

from powdrr_lift.cli import _add_llm_guidance_to_report, _automatic_repair_guidance
from powdrr_lift.core.specification_deduplication import (
    deduplicate_specification_ids,
)


def test_renames_structured_duplicates_and_deduplicates_references(
    tmp_path: Path,
) -> None:
    path = tmp_path / "implementation-specification.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "features": [
                    {"id": "feature-one", "description": "keep"},
                    {
                        "id": " FEATURE-ONE ",
                        "description": "keep as a separate entry",
                        "supercedes": ["old-feature", "old-feature"],
                    },
                ],
                "decisions": [
                    {"id": "feature-one", "description": "remove"},
                    {"id": "decision-one", "description": "keep"},
                ],
                "feature_ids": ["feature-one", "feature-one"],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    removed = deduplicate_specification_ids(path)

    result = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert result["features"] == [
        {"id": "feature-one", "description": "keep"},
        {
            "id": "FEATURE-ONE-2",
            "description": "keep as a separate entry",
            "supercedes": ["old-feature"],
        },
    ]
    assert result["decisions"] == [
        {"id": "feature-one-3", "description": "remove"},
        {"id": "decision-one", "description": "keep"},
    ]
    assert result["feature_ids"] == ["feature-one"]
    assert removed == (
        "features[1].id= FEATURE-ONE  -> FEATURE-ONE-2 (renamed)",
        "supercedes[1]=old-feature (duplicate reference)",
        "decisions[0].id=feature-one -> feature-one-3 (renamed)",
        "feature_ids[1]=feature-one (duplicate reference)",
    )


def test_deduplication_does_not_rewrite_valid_file(tmp_path: Path) -> None:
    path = tmp_path / "system-specification.yaml"
    content = "id: system-one\nrequirements:\n  - id: req-one\n"
    path.write_text(content, encoding="utf-8")

    assert deduplicate_specification_ids(path) == ()
    assert path.read_text(encoding="utf-8") == (
        "id: system-one\nrequirements:\n- id: req-one\n"
    )


def test_repair_guidance_requires_rereading_the_rewritten_file(
    tmp_path: Path,
) -> None:
    path = tmp_path / "implementation-specification.yaml"
    guidance = _automatic_repair_guidance(
        path, ("features[1].id=feature-one -> feature-one-2 (renamed)",)
    )

    assert guidance is not None
    assert "Re-read the rewritten file" in guidance
    report = yaml.safe_load(
        _add_llm_guidance_to_report("validation_successful: true\n", guidance)
    )
    assert report["llm_guidance"] == guidance
