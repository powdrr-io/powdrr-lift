from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True, slots=True)
class EntityTaxonomySection:
    title: str
    entity_types: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class EntityTaxonomy:
    source_path: Path | None = None
    entity_types: tuple[str, ...] = field(default_factory=tuple)
    sections: tuple[EntityTaxonomySection, ...] = field(default_factory=tuple)


def load_entity_taxonomy(
    repo_root: str | Path,
    taxonomy_path: str | Path = "software_development_entity_taxonomy.md",
) -> EntityTaxonomy:
    repo_root_path = Path(repo_root).resolve()
    resolved_taxonomy_path = Path(taxonomy_path)
    if not resolved_taxonomy_path.is_absolute():
        resolved_taxonomy_path = repo_root_path / resolved_taxonomy_path

    return parse_entity_taxonomy_markdown(
        resolved_taxonomy_path.read_text(encoding="utf-8"),
        source_path=resolved_taxonomy_path,
    )


def parse_entity_taxonomy_markdown(
    markdown_content: str,
    *,
    source_path: str | Path | None = None,
) -> EntityTaxonomy:
    section_pattern = re.compile(r"^##\s+(?P<title>.+?)\s*$")
    item_pattern = re.compile(r"^\s*\d+\.\s+(?P<entity_type>.+?)\s*$")

    sections: list[EntityTaxonomySection] = []
    current_title: str | None = None
    current_entries: list[str] = []
    all_entries: list[str] = []

    def flush_section() -> None:
        nonlocal current_title, current_entries
        if current_title is None:
            return

        sections.append(
            EntityTaxonomySection(
                title=current_title,
                entity_types=tuple(current_entries),
            )
        )
        current_title = None
        current_entries = []

    for raw_line in markdown_content.splitlines():
        line = raw_line.rstrip()
        section_match = section_pattern.match(line)
        if section_match is not None:
            flush_section()
            current_title = section_match.group("title").strip()
            continue

        item_match = item_pattern.match(line)
        if item_match is None:
            continue

        entity_type = item_match.group("entity_type").strip()
        if not entity_type:
            continue

        if current_title is None:
            current_title = "Uncategorized"

        current_entries.append(entity_type)
        all_entries.append(entity_type)

    flush_section()

    if not all_entries:
        raise ValueError("Entity taxonomy markdown did not contain any entity types.")

    return EntityTaxonomy(
        source_path=None if source_path is None else Path(source_path),
        entity_types=tuple(all_entries),
        sections=tuple(sections),
    )


def allowed_entity_types(markdown_content: str) -> tuple[str, ...]:
    return parse_entity_taxonomy_markdown(markdown_content).entity_types


def is_allowed_entity_type(
    entity_type: str | None,
    taxonomy: EntityTaxonomy | Sequence[str],
) -> bool:
    if entity_type is None:
        return False

    normalized_entity_type = entity_type.strip()
    if not normalized_entity_type:
        return False

    allowed_types = (
        taxonomy.entity_types if isinstance(taxonomy, EntityTaxonomy) else taxonomy
    )
    return normalized_entity_type in set(allowed_types)
