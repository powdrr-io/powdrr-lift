"""Stable workflow identifier normalization used by planning artifacts."""

from __future__ import annotations

import re


def slugify_workflow_id(value: str) -> str:
    """Normalize a workflow identifier without changing its persisted slug."""
    slug = re.sub(r"[^a-z0-9]+", "-", value.strip().casefold()).strip("-")
    if not slug:
        raise ValueError("The proposed PR id must contain a letter or digit.")
    return slug
