"""Service-owned proposed-PR coverage and effect-equivalence policy."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, cast

import yaml

_EFFECT_SECTIONS = (
    "entities",
    "modules",
    "tools",
    "entity_relationships",
    "features",
    "decisions",
)
_FEATURE_COVERAGE_SECTIONS = ("requirements", "acceptance_criteria", "expected_tests")


@dataclass(frozen=True, slots=True)
class ProposedPRCoverageIssue:
    code: str
    message: str
    path: str | None = None


@dataclass(frozen=True, slots=True)
class ProposedPRSourceDocument:
    """Repository evidence collected by the agent and evaluated by the service."""

    path: str
    yaml_content: str | None = None
    read_error: str | None = None


@dataclass(frozen=True, slots=True)
class ProposedPRCoverageResult:
    issues: list[ProposedPRCoverageIssue] = field(default_factory=list)


def validate_proposed_pr_coverage_and_effects(
    proposed_pr_specification_yaml: str,
    *,
    proposed_pr_ids: Sequence[str],
    feature_coverage_source: ProposedPRSourceDocument | None,
    effect_sources: Sequence[ProposedPRSourceDocument],
) -> ProposedPRCoverageResult:
    """Validate feature ownership and preserve authoritative ordered effects.

    The agent supplies the proposed specification and the applicable source
    documents. This policy function performs no filesystem or repository I/O.
    """
    issues: list[ProposedPRCoverageIssue] = []
    try:
        proposed_spec = _load_yaml_mapping(proposed_pr_specification_yaml)
    except Exception as exc:  # noqa: BLE001
        return ProposedPRCoverageResult(
            issues=[
                ProposedPRCoverageIssue(
                    "invalid_proposed_pr_specification",
                    f"Could not parse proposed PR specification YAML: {exc}",
                )
            ]
        )

    if feature_coverage_source is not None:
        _validate_feature_coverage(
            proposed_spec,
            proposed_pr_ids=proposed_pr_ids,
            source=feature_coverage_source,
            issues=issues,
        )
    _validate_effect_equivalence(
        proposed_spec,
        effect_sources=effect_sources,
        issues=issues,
    )
    return ProposedPRCoverageResult(issues=issues)


def _validate_feature_coverage(
    proposed_spec: Mapping[str, Any],
    *,
    proposed_pr_ids: Sequence[str],
    source: ProposedPRSourceDocument,
    issues: list[ProposedPRCoverageIssue],
) -> None:
    if source.read_error is not None:
        issues.append(
            ProposedPRCoverageIssue(
                "invalid_feature_coverage_source",
                f"Could not read feature coverage source: {source.read_error}",
                source.path,
            )
        )
        return
    if source.yaml_content is None:
        issues.append(
            ProposedPRCoverageIssue(
                "invalid_feature_coverage_source",
                "Feature coverage source has no supplied content.",
                source.path,
            )
        )
        return
    try:
        authoritative_spec = _load_yaml_mapping(source.yaml_content)
    except Exception as exc:  # noqa: BLE001
        issues.append(
            ProposedPRCoverageIssue(
                "invalid_feature_coverage_source",
                f"Could not read feature coverage source: {exc}",
                source.path,
            )
        )
        return

    authoritative: dict[str, set[str]] = {}
    for section in _FEATURE_COVERAGE_SECTIONS:
        ids: set[str] = set()
        raw_items = authoritative_spec.get(section, [])
        if not isinstance(raw_items, Sequence) or isinstance(raw_items, (str, bytes)):
            continue
        for index, raw_item in enumerate(raw_items):
            if not isinstance(raw_item, Mapping):
                continue
            item_id = _optional_string(raw_item.get("id"))
            if item_id is None:
                issues.append(
                    ProposedPRCoverageIssue(
                        "feature_coverage_id_missing",
                        "Authoritative feature coverage items require an id.",
                        f"{source.path}:{section}[{index}].id",
                    )
                )
                continue
            if item_id in ids:
                issues.append(
                    ProposedPRCoverageIssue(
                        "duplicate_feature_coverage_id",
                        f"Authoritative feature item id {item_id!r} is duplicated.",
                        f"{source.path}:{section}[{index}].id",
                    )
                )
            ids.add(item_id)
        if ids:
            authoritative[section] = ids

    owners: dict[str, dict[str, str]] = {section: {} for section in authoritative}
    known_pr_ids = _normalize_identifier_set(proposed_pr_ids)
    for index, raw_pr in enumerate(
        _coerce_sequence(
            proposed_spec.get("proposed_prs"),
            path="proposed_prs",
            issues=issues,
            issue_code="invalid_proposed_prs_section",
            issue_message="proposed_prs must be a list of proposed PR mappings.",
        )
    ):
        if not isinstance(raw_pr, Mapping):
            continue
        proposed_pr_id = _optional_string(raw_pr.get("id"))
        if proposed_pr_id is None:
            continue
        for section, expected_ids in authoritative.items():
            raw_values = raw_pr.get(section, [])
            if not isinstance(raw_values, Sequence) or isinstance(
                raw_values, (str, bytes)
            ):
                issues.append(
                    ProposedPRCoverageIssue(
                        "invalid_feature_coverage_section",
                        f"{section} must be a list of ids in each proposed PR.",
                        f"proposed_prs[{index}].{section}",
                    )
                )
                continue
            for value_index, raw_value in enumerate(raw_values):
                coverage_id = _optional_string(raw_value)
                path = f"proposed_prs[{index}].{section}[{value_index}]"
                if coverage_id is None:
                    issues.append(
                        ProposedPRCoverageIssue(
                            "feature_coverage_id_missing",
                            f"{section} entries must be non-empty ids.",
                            path,
                        )
                    )
                    continue
                if coverage_id not in expected_ids:
                    issues.append(
                        ProposedPRCoverageIssue(
                            "unknown_feature_coverage_id",
                            f"{coverage_id!r} is not an authoritative {section} id.",
                            path,
                        )
                    )
                    continue
                if coverage_id in owners[section]:
                    issues.append(
                        ProposedPRCoverageIssue(
                            "duplicate_feature_coverage_assignment",
                            f"{section} id {coverage_id!r} is assigned to both "
                            f"{owners[section][coverage_id]!r} and {proposed_pr_id!r}.",
                            path,
                        )
                    )
                owners[section][coverage_id] = proposed_pr_id
                if _normalize_identifier(proposed_pr_id) not in known_pr_ids:
                    issues.append(
                        ProposedPRCoverageIssue(
                            "unknown_proposed_pr_id",
                            f"Proposed PR id {proposed_pr_id!r} is not declared.",
                            f"proposed_prs[{index}].id",
                        )
                    )
    for section, expected_ids in authoritative.items():
        missing = sorted(expected_ids - set(owners[section]))
        if missing:
            issues.append(
                ProposedPRCoverageIssue(
                    "uncovered_feature_items",
                    f"Every authoritative {section} item must be assigned to a "
                    f"proposed PR; missing: {', '.join(missing)}.",
                    f"proposed_prs.*.{section}",
                )
            )


def _validate_effect_equivalence(
    proposed_spec: Mapping[str, Any],
    *,
    effect_sources: Sequence[ProposedPRSourceDocument],
    issues: list[ProposedPRCoverageIssue],
) -> None:
    source_effects: dict[str, list[tuple[str, str]]] = {
        section: [] for section in _EFFECT_SECTIONS
    }
    seen_source_effects: dict[str, set[tuple[str, str]]] = {
        section: set() for section in _EFFECT_SECTIONS
    }
    found_source = False
    for source in effect_sources:
        found_source = True
        if source.read_error is not None:
            issues.append(
                ProposedPRCoverageIssue(
                    "invalid_effect_source",
                    f"Could not read v1 effect source: {source.read_error}",
                    source.path,
                )
            )
            continue
        if source.yaml_content is None:
            issues.append(
                ProposedPRCoverageIssue(
                    "invalid_effect_source",
                    "V1 effect source has no supplied content.",
                    source.path,
                )
            )
            continue
        try:
            data = _load_yaml_mapping(source.yaml_content)
        except Exception as exc:  # noqa: BLE001
            issues.append(
                ProposedPRCoverageIssue(
                    "invalid_effect_source",
                    f"Could not read v1 effect source: {exc}",
                    source.path,
                )
            )
            continue
        for section in _EFFECT_SECTIONS:
            raw_items = data.get(section, [])
            if not isinstance(raw_items, Sequence) or isinstance(
                raw_items, (str, bytes)
            ):
                continue
            for item in raw_items:
                if isinstance(item, Mapping):
                    effect = _effect_pair(item)
                    if (
                        effect is not None
                        and effect not in seen_source_effects[section]
                    ):
                        seen_source_effects[section].add(effect)
                        source_effects[section].append(effect)
    if not found_source:
        return

    proposed_effects: dict[str, list[tuple[str, str]]] = {
        section: [] for section in _EFFECT_SECTIONS
    }
    seen_proposed_effects: dict[str, set[tuple[str, str]]] = {
        section: set() for section in _EFFECT_SECTIONS
    }
    for section in _EFFECT_SECTIONS:
        raw_items = proposed_spec.get(section, [])
        if not isinstance(raw_items, Sequence) or isinstance(raw_items, (str, bytes)):
            continue
        for item in raw_items:
            if isinstance(item, Mapping):
                effect = _effect_pair(item)
                if effect is not None and effect not in seen_proposed_effects[section]:
                    seen_proposed_effects[section].add(effect)
                    proposed_effects[section].append(effect)
    for section in _EFFECT_SECTIONS:
        if proposed_effects[section] == source_effects[section]:
            continue
        expected = source_effects[section]
        actual = proposed_effects[section]
        missing = [effect for effect in expected if effect not in actual]
        unexpected = [effect for effect in actual if effect not in expected]
        details = [
            f"Expected ordered id/action pairs: {_format_effect_pairs(expected)}.",
            f"Actual ordered id/action pairs: {_format_effect_pairs(actual)}.",
        ]
        if missing:
            details.append(f"Missing: {_format_effect_pairs(missing)}.")
        if unexpected:
            details.append(f"Unexpected: {_format_effect_pairs(unexpected)}.")
        issues.append(
            ProposedPRCoverageIssue(
                "v1_effect_mismatch",
                f"Proposed PR {section} entries do not match the authoritative "
                "ordered id/action effects. " + " ".join(details),
                f"proposed_prs.{section}",
            )
        )


def _effect_pair(item: Mapping[str, Any]) -> tuple[str, str] | None:
    item_id = _optional_string(item.get("id"))
    action = _optional_string(item.get("action"))
    if item_id is None or action is None:
        return None
    return item_id, action


def _format_effect_pairs(effects: Sequence[tuple[str, str]]) -> str:
    if not effects:
        return "[]"
    return (
        "["
        + ", ".join(f"({item_id!r}, {action!r})" for item_id, action in effects)
        + "]"
    )


def _normalize_identifier(value: str) -> str:
    return value.strip().casefold()


def _normalize_identifier_set(values: Sequence[str]) -> set[str]:
    return {_normalize_identifier(value) for value in values}


def _optional_string(raw_value: object) -> str | None:
    if raw_value is None:
        return None
    value = str(raw_value).strip()
    return value or None


def _coerce_sequence(
    raw_value: object,
    *,
    path: str,
    issues: list[ProposedPRCoverageIssue],
    issue_code: str,
    issue_message: str,
) -> Sequence[object]:
    if raw_value is None:
        return ()
    if isinstance(raw_value, Sequence) and not isinstance(raw_value, (str, bytes)):
        return raw_value
    issues.append(ProposedPRCoverageIssue(issue_code, issue_message, path))
    return ()


def _load_yaml_mapping(raw_yaml: str) -> Mapping[str, Any]:
    loaded = yaml.safe_load(raw_yaml)
    if loaded is None:
        return {}
    if not isinstance(loaded, Mapping):
        raise TypeError("Top-level PR specification must be a mapping.")
    return cast(Mapping[str, Any], loaded)
