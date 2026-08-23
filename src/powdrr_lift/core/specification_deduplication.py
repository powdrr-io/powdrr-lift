from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

_ID_ITEM_GROUPS: tuple[tuple[str, ...], ...] = (
    ("requirements", "approach"),
    ("entities",),
    ("modules",),
    ("tools",),
    ("entity_relationships",),
    ("features", "decisions", "human-decisions"),
    (
        "acceptance_criteria",
        "expected_tests",
        "required_test_cases",
        "expected_outcomes",
        "non_goals",
        "risks",
    ),
)
_REFERENCE_LIST_FIELDS = {"feature_ids", "related_modules", "supercedes"}


def deduplicate_specification_ids(
    path: str | Path, *, reformat: bool = True
) -> tuple[str, ...]:
    """Delete later duplicate specification entries and rewrite the file.

    Structured entries are retained: later duplicate ids are renamed with a
    stable ``-2``, ``-3``, ... suffix. Scalar reference lists are deduplicated
    because repeating a reference does not add information.
    """
    resolved_path = Path(path)
    try:
        data = yaml.safe_load(resolved_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return ()
    if not isinstance(data, Mapping):
        return ()

    normalized_data: dict[str, Any] = dict(data)
    removed: list[str] = []
    changed = False
    for group in _ID_ITEM_GROUPS:
        seen: set[str] = set()
        for section in group:
            value = normalized_data.get(section)
            if not isinstance(value, list):
                continue
            retained: list[Any] = []
            for original_index, item in enumerate(value):
                item_id = item.get("id") if isinstance(item, Mapping) else None
                normalized_id = _normalize_id(item_id)
                if normalized_id is not None and normalized_id in seen:
                    original_item_id = item_id
                    renamed_id = _next_id(str(item_id), seen)
                    renamed_item = dict(item)
                    renamed_item["id"] = renamed_id
                    item = renamed_item
                    item_id = renamed_id
                    normalized_id = _normalize_id(item_id)
                    removed.append(
                        f"{section}[{original_index}].id={original_item_id} -> "
                        f"{renamed_id} (renamed)"
                    )
                    changed = True
                if normalized_id is not None:
                    seen.add(normalized_id)
                retained.append(item)
                if isinstance(item, dict):
                    changed |= _deduplicate_reference_fields(item, removed=removed)
            normalized_data[section] = retained

    changed |= _deduplicate_reference_fields(normalized_data, removed=removed)

    # Evaluation also canonicalizes valid YAML even when no duplicate was
    # found, so the next validation pass sees one stable representation.
    if changed or (reformat and isinstance(data, Mapping)):
        resolved_path.write_text(
            yaml.safe_dump(normalized_data, sort_keys=False), encoding="utf-8"
        )
    return tuple(removed)


def reformat_specification_file(path: str | Path) -> bool:
    """Rewrite a valid specification as canonical YAML and report changes."""
    resolved_path = Path(path)
    try:
        data = yaml.safe_load(resolved_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return False
    if not isinstance(data, Mapping):
        return False
    formatted = yaml.safe_dump(dict(data), sort_keys=False)
    if formatted == resolved_path.read_text(encoding="utf-8"):
        return False
    resolved_path.write_text(formatted, encoding="utf-8")
    return True


def _deduplicate_reference_fields(
    mapping: dict[str, Any], *, removed: list[str]
) -> bool:
    changed = False
    for field_name in _REFERENCE_LIST_FIELDS:
        values = mapping.get(field_name)
        if not isinstance(values, list):
            continue
        seen: set[str] = set()
        retained: list[Any] = []
        for index, value in enumerate(values):
            normalized_value = _normalize_id(value)
            if normalized_value is not None and normalized_value in seen:
                removed.append(f"{field_name}[{index}]={value} (duplicate reference)")
                changed = True
                continue
            if normalized_value is not None:
                seen.add(normalized_value)
            retained.append(value)
        mapping[field_name] = retained
    return changed


def _next_id(original_id: str, used_ids: set[str]) -> str:
    original_id = original_id.strip()
    suffix = 2
    while True:
        candidate = f"{original_id}-{suffix}"
        if _normalize_id(candidate) not in used_ids:
            return candidate
        suffix += 1


def _normalize_id(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip().casefold()
