"""Catalog values for discovered process-language definitions."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from powdrr_lift.core import Skill


@dataclass(frozen=True, slots=True)
class SkillCatalogEntry:
    """A loaded process definition together with the file that defined it."""

    path: Path
    skill: Skill


WorkflowTemplateCatalogEntry = SkillCatalogEntry
