from __future__ import annotations

from pathlib import Path

from powdrr_lift.core.entity_taxonomy import parse_entity_taxonomy_markdown


def test_parse_entity_taxonomy_markdown_reads_repository_taxonomy() -> None:
    taxonomy_path = (
        Path(__file__).resolve().parents[1] / "software_development_entity_taxonomy.md"
    )
    taxonomy = parse_entity_taxonomy_markdown(
        taxonomy_path.read_text(encoding="utf-8"),
        source_path=taxonomy_path,
    )

    assert taxonomy.source_path == taxonomy_path
    assert len(taxonomy.entity_types) == 401
    assert taxonomy.entity_types[:5] == (
        "Organization",
        "Business unit",
        "Engineering team",
        "Service owner",
        "Code owner",
    )
    assert taxonomy.sections[0].title == "Product, org, and ownership entities"
    assert taxonomy.sections[-1].title == (
        "Architecture, ML, docs, and workflow entities"
    )
    assert taxonomy.sections[0].entity_types[:3] == (
        "Organization",
        "Business unit",
        "Engineering team",
    )
    assert taxonomy.entity_types[-1] == "Skill"
