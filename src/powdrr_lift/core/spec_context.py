from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from powdrr_lift.change_log_template import _resolve_repo_root

_CONTEXT_TYPE_ALIASES: dict[str, str] = {
    "requirements": "requirements",
    "approach": "approach",
    "entities": "entities",
    "entity-relationships": "entity-relationships",
    "entity_relationships": "entity-relationships",
    "invariants": "invariants",
    "guidance": "guidance",
    "features": "features",
    "human-decisions": "human-decisions",
    "human_decisions": "human-decisions",
    "intent": "intent",
    "intents": "intents",
    "acceptance_criteria": "acceptance_criteria",
    "expected_tests": "expected_tests",
    "required_test_cases": "required_test_cases",
    "expected_outcomes": "expected_outcomes",
    "non_goals": "non_goals",
    "risks": "risks",
    "decisions": "decisions",
    "proposed_prs": "proposed_prs",
    "proposed-prs": "proposed_prs",
    "modules": "modules",
    "tools": "tools",
}

_SUPPORTED_CONTEXT_TYPES: tuple[str, ...] = (
    "requirements",
    "approach",
    "entities",
    "entity-relationships",
    "invariants",
    "guidance",
    "features",
    "human-decisions",
    "intent",
    "intents",
    "acceptance_criteria",
    "expected_tests",
    "required_test_cases",
    "expected_outcomes",
    "non_goals",
    "risks",
    "decisions",
    "proposed_prs",
    "modules",
    "tools",
)


@dataclass(frozen=True, slots=True)
class GatherContextMatch:
    path: str
    section: str
    item_index: int | None
    work_item_name: str | None
    specification_type: str | None
    item: Any


@dataclass(frozen=True, slots=True)
class GatherContextReport:
    repo_root: str
    types: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    filters: dict[str, list[str]] = field(default_factory=dict)
    matches: list[GatherContextMatch] = field(default_factory=list)


def supported_context_types() -> tuple[str, ...]:
    return _SUPPORTED_CONTEXT_TYPES


def normalize_context_type(value: str) -> str:
    normalized_value = value.strip().lower()
    if not normalized_value:
        raise ValueError("Context type must not be empty.")
    try:
        return _CONTEXT_TYPE_ALIASES[normalized_value]
    except KeyError as exc:
        raise ValueError(f"Unsupported context type {value!r}.") from exc


def gather_specification_context(
    repo_root: str | Path | None,
    *,
    types: list[str],
    keywords: list[str] | None = None,
    filters: dict[str, object] | None = None,
) -> GatherContextReport:
    repo_root_path = _resolve_repo_root(repo_root)
    normalized_types = _normalize_context_types(types)
    normalized_keywords = _normalize_keywords(keywords)
    normalized_filters = _normalize_filters(filters)
    matches: list[GatherContextMatch] = []

    for spec_path in _iter_context_specification_paths(repo_root_path):
        raw_spec = _load_yaml_mapping(spec_path)
        if raw_spec is None:
            continue

        work_item_name, specification_type = _describe_specification_path(
            repo_root_path,
            spec_path,
        )
        for section_name, section_value in raw_spec.items():
            normalized_section_name = _CONTEXT_TYPE_ALIASES.get(
                str(section_name).strip().lower(),
                str(section_name).strip().lower(),
            )
            if normalized_section_name not in normalized_types:
                continue

            matches.extend(
                _collect_section_matches(
                    path=spec_path,
                    section=normalized_section_name,
                    section_value=section_value,
                    work_item_name=work_item_name,
                    specification_type=specification_type,
                    keywords=normalized_keywords,
                    filters=normalized_filters,
                )
            )

    return GatherContextReport(
        repo_root=str(repo_root_path),
        types=normalized_types,
        keywords=normalized_keywords,
        filters=normalized_filters,
        matches=matches,
    )


def render_gather_context_report(report: GatherContextReport) -> str:
    return json.dumps(
        _gather_context_report_to_data(report),
        indent=2,
        ensure_ascii=False,
    )


def _normalize_context_types(types: list[str]) -> list[str]:
    normalized_types: list[str] = []
    seen: set[str] = set()
    for raw_type in types:
        normalized_type = normalize_context_type(raw_type)
        if normalized_type in seen:
            continue
        seen.add(normalized_type)
        normalized_types.append(normalized_type)
    if not normalized_types:
        raise ValueError("At least one context type must be provided.")
    return normalized_types


def _normalize_keywords(keywords: list[str] | None) -> list[str]:
    if keywords is None:
        return []

    normalized_keywords: list[str] = []
    seen: set[str] = set()
    for raw_keyword in keywords:
        normalized_keyword = raw_keyword.strip()
        if not normalized_keyword:
            continue
        normalized_key = normalized_keyword.casefold()
        if normalized_key in seen:
            continue
        seen.add(normalized_key)
        normalized_keywords.append(normalized_keyword)
    return normalized_keywords


def _normalize_filters(filters: dict[str, object] | None) -> dict[str, list[str]]:
    if filters is None:
        return {}
    normalized: dict[str, list[str]] = {}
    for raw_field, raw_values in filters.items():
        field = str(raw_field).strip()
        if not field:
            raise ValueError("Context filter fields must not be empty.")
        values = (
            raw_values if isinstance(raw_values, (list, tuple, set)) else [raw_values]
        )
        normalized_values = _normalize_keywords([str(value) for value in values])
        if not normalized_values:
            raise ValueError(f"Context filter {field!r} must have a value.")
        normalized[field] = normalized_values
    return normalized


def _iter_context_specification_paths(repo_root: Path) -> list[Path]:
    paths: list[Path] = []
    docs_root = repo_root / "docs" / "specs"
    if docs_root.exists():
        paths.extend(
            path for path in sorted(docs_root.rglob("*.yaml")) if path.is_file()
        )

    current_state_path = repo_root / ".powdrr-lift" / "state" / "current-state.yaml"
    if current_state_path.is_file():
        paths.append(current_state_path)

    return paths


def _load_yaml_mapping(path: Path) -> dict[str, Any] | None:
    try:
        raw_data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None

    if not isinstance(raw_data, dict):
        return None

    return raw_data


def _describe_specification_path(
    repo_root: Path,
    path: Path,
) -> tuple[str | None, str | None]:
    try:
        relative_path = path.relative_to(repo_root)
    except ValueError:
        return None, None

    path_parts = relative_path.parts
    if len(path_parts) >= 4 and path_parts[0] == "docs" and path_parts[1] == "specs":
        work_item_name = path_parts[2]
        specification_type = path.stem.removesuffix("-specification")
        return work_item_name, specification_type

    if path_parts == (".powdrr-lift", "state", "current-state.yaml"):
        return "current-state", "current-state"

    return None, None


def _collect_section_matches(
    *,
    path: Path,
    section: str,
    section_value: Any,
    work_item_name: str | None,
    specification_type: str | None,
    keywords: list[str],
    filters: dict[str, list[str]],
) -> list[GatherContextMatch]:
    matches: list[GatherContextMatch] = []
    if isinstance(section_value, list):
        for index, item in enumerate(section_value):
            if not _item_matches(item, keywords, filters, section=section):
                continue
            matches.append(
                GatherContextMatch(
                    path=str(path),
                    section=section,
                    item_index=index,
                    work_item_name=work_item_name,
                    specification_type=specification_type,
                    item=item,
                )
            )
        return matches

    if not _item_matches(section_value, keywords, filters, section=section):
        return matches

    matches.append(
        GatherContextMatch(
            path=str(path),
            section=section,
            item_index=None,
            work_item_name=work_item_name,
            specification_type=specification_type,
            item=section_value,
        )
    )
    return matches


def _item_matches_keywords(item: Any, keywords: list[str]) -> bool:
    if not keywords:
        return True

    haystack = json.dumps(item, ensure_ascii=False, sort_keys=True).casefold()
    return any(keyword.casefold() in haystack for keyword in keywords)


def _item_matches(
    item: Any,
    keywords: list[str],
    filters: dict[str, list[str]],
    *,
    section: str,
) -> bool:
    if not _item_matches_keywords(item, keywords):
        return False
    if not filters:
        return True
    if not isinstance(item, dict):
        return False
    for raw_field, values in filters.items():
        field = "type" if raw_field == "entity_type" else raw_field
        item_value = item.get(field)
        if raw_field == "entity_type" and item_value is None:
            item_value = section.removesuffix("s").casefold()
        if isinstance(item_value, (list, tuple, set)):
            item_values = {str(value).casefold() for value in item_value}
            if not all(value.casefold() in item_values for value in values):
                return False
        elif len(values) != 1 or str(item_value).casefold() != values[0].casefold():
            return False
    return True


def _gather_context_report_to_data(report: GatherContextReport) -> dict[str, Any]:
    return {
        "repo_root": report.repo_root,
        "types": report.types,
        "keywords": report.keywords,
        "filters": report.filters,
        "matches": [
            {
                "path": match.path,
                "section": match.section,
                "item_index": match.item_index,
                "work_item_name": match.work_item_name,
                "specification_type": match.specification_type,
                "item": match.item,
            }
            for match in report.matches
        ],
    }
