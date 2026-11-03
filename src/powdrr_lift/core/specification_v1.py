"""Local file operations for specification-v1 normalization."""

from __future__ import annotations

from pathlib import Path

from powdrr_service.specifications.common import normalize_specification_v1_yaml


def normalize_specification_v1_file(path: str | Path) -> bool:
    """Normalize a specification file using the service content policy."""
    resolved_path = Path(path)
    original = resolved_path.read_text(encoding="utf-8")
    normalized = normalize_specification_v1_yaml(original)
    if normalized == original:
        return False
    resolved_path.write_text(normalized, encoding="utf-8")
    return True
