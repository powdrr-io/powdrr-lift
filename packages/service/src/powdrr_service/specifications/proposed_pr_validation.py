"""Service-owned proposed-PR specification validation policy."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, cast

import yaml

from powdrr_service.specifications.common import validate_related_module_references
from powdrr_service.specifications.proposed_pr_coverage import (
    ProposedPRSourceDocument,
    validate_proposed_pr_coverage_and_effects,
)
from powdrr_service.structrr.verification_contract import (
    VERIFICATION_CONTRACT_FIELDS,
    VerificationContract,
)

_EFFECT_SECTIONS = (
    "entities",
    "modules",
    "tools",
    "entity_relationships",
    "features",
    "decisions",
)
_CONTENT_SECTIONS = (
    "requirements",
    "approach",
    "entities",
    "entity_relationships",
    "invariants",
    "guidance",
    "features",
    "human-decisions",
    "feature_ids",
    "intent",
    "acceptance_criteria",
    "expected_tests",
    "required_test_cases",
    "expected_outcomes",
    "non_goals",
    "risks",
    "modules",
    "tools",
)


@dataclass(frozen=True, slots=True)
class ProposedPRValidationIssue:
    code: str
    message: str
    path: str | None = None


@dataclass(frozen=True, slots=True)
class ProposedPRValidationReport:
    validation_successful: bool
    proposed_pr_id: str | None
    available_feature_ids: list[str] = field(default_factory=list)
    known_pr_ids: list[str] = field(default_factory=list)
    issues: list[ProposedPRValidationIssue] = field(default_factory=list)


def build_proposed_pr_specification_validation_report(
    proposed_pr_specification_yaml: str,
    *,
    available_feature_ids: Sequence[str],
    existing_pr_specifications: Sequence[ProposedPRSourceDocument],
    dependency_pr_specifications: Sequence[ProposedPRSourceDocument],
    feature_coverage_source: ProposedPRSourceDocument | None,
    effect_sources: Sequence[ProposedPRSourceDocument],
) -> ProposedPRValidationReport:
    """Validate proposed-PR content using repository evidence supplied by agent."""
    issues: list[ProposedPRValidationIssue] = []
    known_pr_ids = _collect_existing_pr_ids(existing_pr_specifications)
    try:
        raw_spec = _load_yaml_mapping(proposed_pr_specification_yaml)
    except Exception as exc:  # noqa: BLE001
        issues.append(
            ProposedPRValidationIssue(
                "invalid_yaml",
                f"Could not parse proposed PR specification YAML: {exc}",
            )
        )
        return ProposedPRValidationReport(
            validation_successful=False,
            proposed_pr_id=None,
            available_feature_ids=list(available_feature_ids),
            known_pr_ids=known_pr_ids,
            issues=issues,
        )

    _validate_template_boilerplate_removed(
        proposed_pr_specification_yaml,
        issues=issues,
    )
    raw_modules = raw_spec.get("modules")
    module_ids: set[str] = set()
    if isinstance(raw_modules, list):
        for item in raw_modules:
            if not isinstance(item, Mapping):
                continue
            item_id = item.get("id")
            if isinstance(item_id, str) and item_id.strip():
                module_ids.add(item_id.strip())
    scoped_sections = {
        str(section): value
        for section, value in raw_spec.items()
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes))
    }
    issues.extend(
        ProposedPRValidationIssue(issue.code, issue.message, issue.path)
        for issue in validate_related_module_references(
            scoped_sections, module_ids=module_ids
        )
    )
    if not any(raw_spec.get(section) for section in _CONTENT_SECTIONS):
        issues.append(
            ProposedPRValidationIssue(
                "empty_specification",
                "A specification must contain substantive design content; "
                "id and title alone are not sufficient.",
            )
        )

    if "proposed_prs" in raw_spec:
        return _build_multi_proposed_pr_validation_report(
            raw_spec,
            proposed_pr_specification_yaml=proposed_pr_specification_yaml,
            available_feature_ids=available_feature_ids,
            known_pr_ids=known_pr_ids,
            dependency_pr_specifications=dependency_pr_specifications,
            feature_coverage_source=feature_coverage_source,
            effect_sources=effect_sources,
            issues=issues,
        )

    seen_detail_ids: set[str] = set()
    proposed_pr_id = _required_string(
        raw_spec.get("id"),
        path="id",
        issues=issues,
        issue_code="proposed_pr_id_missing",
        issue_message="The id field is required.",
    )
    if proposed_pr_id is not None and _normalize_identifier(
        proposed_pr_id
    ) in _normalize_identifier_set(known_pr_ids):
        issues.append(
            ProposedPRValidationIssue(
                "duplicate_proposed_pr_id",
                f"Proposed PR id {proposed_pr_id!r} already exists.",
                "id",
            )
        )
    if proposed_pr_id is not None:
        seen_detail_ids.add(proposed_pr_id)

    dependent_prs = _collect_dependent_pr_ids(
        raw_spec.get("dependent_prs", []),
        proposed_pr_id=proposed_pr_id,
        known_pr_ids=known_pr_ids,
        issues=issues,
    )
    if proposed_pr_id is not None:
        _validate_proposed_pr_dependency_cycle(
            dependency_pr_specifications,
            proposed_pr_id,
            dependent_prs,
            issues=issues,
        )

    _collect_feature_ids(
        _coerce_sequence(
            raw_spec.get("feature_ids"),
            path="feature_ids",
            issues=issues,
            issue_code="invalid_feature_ids_section",
            issue_message="feature_ids must be a list of feature id strings.",
        ),
        available_feature_ids=set(available_feature_ids),
        issues=issues,
    )
    raw_intent = raw_spec.get("intent")
    intent = (
        None
        if raw_intent is None
        else _coerce_mapping(
            raw_intent,
            path="intent",
            issues=issues,
            issue_code="invalid_intent_section",
            issue_message="intent must be a mapping with problem, goal, and reasoning.",
        )
    )
    if intent is not None:
        for field_name in ("problem", "goal", "reasoning"):
            _required_string(
                intent.get(field_name),
                path=f"intent.{field_name}",
                issues=issues,
                issue_code=f"intent_{field_name}_missing",
                issue_message=f"The intent.{field_name} field is required.",
            )

    for section_name in (
        "acceptance_criteria",
        "expected_tests",
        "required_test_cases",
        "expected_outcomes",
        "non_goals",
        "risks",
    ):
        _collect_detail_items(
            _coerce_sequence(
                raw_spec.get(section_name),
                path=section_name,
                issues=issues,
                issue_code=f"invalid_{section_name}_section",
                issue_message=f"{section_name} must be a list of detail items.",
            ),
            section_name=section_name,
            seen_ids=seen_detail_ids,
            issues=issues,
        )

    return ProposedPRValidationReport(
        validation_successful=not issues,
        proposed_pr_id=proposed_pr_id,
        available_feature_ids=list(available_feature_ids),
        known_pr_ids=known_pr_ids,
        issues=issues,
    )


def build_proposed_pr_dependency_graph(
    proposed_pr_specifications: Sequence[ProposedPRSourceDocument],
) -> dict[str, tuple[str, ...]]:
    """Reconstruct the proposed-PR graph from supplied repository documents."""
    graph: dict[str, tuple[str, ...]] = {}
    for source in proposed_pr_specifications:
        if source.yaml_content is None or source.read_error is not None:
            continue
        try:
            raw_spec = _load_yaml_mapping(source.yaml_content)
        except Exception:  # noqa: BLE001
            continue
        nested_prs = raw_spec.get("proposed_prs")
        records = (
            [item for item in nested_prs if isinstance(item, Mapping)]
            if isinstance(nested_prs, Sequence)
            and not isinstance(nested_prs, (str, bytes))
            else [raw_spec]
        )
        for record in records:
            proposed_pr_id = _optional_string(record.get("id"))
            if proposed_pr_id is None:
                continue
            raw_dependencies = record.get("dependent_prs", [])
            if not isinstance(raw_dependencies, Sequence) or isinstance(
                raw_dependencies, (str, bytes)
            ):
                continue
            dependencies = tuple(
                dependency.strip()
                for dependency in raw_dependencies
                if isinstance(dependency, str) and dependency.strip()
            )
            graph[proposed_pr_id] = dependencies
    return graph


def _collect_existing_pr_ids(
    documents: Sequence[ProposedPRSourceDocument],
) -> list[str]:
    pr_ids: list[str] = []
    seen_ids: set[str] = set()
    for source in documents:
        if source.yaml_content is None or source.read_error is not None:
            continue
        try:
            raw_spec = _load_yaml_mapping(source.yaml_content)
        except Exception:  # noqa: BLE001
            continue
        proposed_pr_id = _optional_string(raw_spec.get("id"))
        nested_prs = raw_spec.get("proposed_prs")
        candidate_ids = (
            [
                _optional_string(item.get("id"))
                for item in nested_prs
                if isinstance(item, Mapping)
            ]
            if isinstance(nested_prs, Sequence)
            and not isinstance(nested_prs, (str, bytes))
            else [proposed_pr_id]
        )
        for candidate_id in candidate_ids:
            if candidate_id is None:
                continue
            normalized_id = _normalize_identifier(candidate_id)
            if normalized_id in seen_ids:
                continue
            seen_ids.add(normalized_id)
            pr_ids.append(candidate_id)
    return pr_ids


def _build_multi_proposed_pr_validation_report(
    raw_spec: Mapping[str, Any],
    *,
    proposed_pr_specification_yaml: str,
    available_feature_ids: Sequence[str],
    known_pr_ids: list[str],
    dependency_pr_specifications: Sequence[ProposedPRSourceDocument],
    feature_coverage_source: ProposedPRSourceDocument | None,
    effect_sources: Sequence[ProposedPRSourceDocument],
    issues: list[ProposedPRValidationIssue],
) -> ProposedPRValidationReport:
    _required_string(
        raw_spec.get("id"),
        path="id",
        issues=issues,
        issue_code="feature_id_missing",
        issue_message="The unified proposed PR file requires a top-level id.",
    )
    _collect_feature_ids(
        _coerce_sequence(
            raw_spec.get("feature_ids"),
            path="feature_ids",
            issues=issues,
            issue_code="invalid_feature_ids_section",
            issue_message="feature_ids must be a list of feature id strings.",
        ),
        available_feature_ids=set(available_feature_ids),
        issues=issues,
    )
    raw_prs = _coerce_sequence(
        raw_spec.get("proposed_prs"),
        path="proposed_prs",
        issues=issues,
        issue_code="invalid_proposed_prs_section",
        issue_message="proposed_prs must be a list of proposed PR mappings.",
    )
    ids: dict[str, str] = {}
    for index, raw_pr in enumerate(raw_prs):
        if not isinstance(raw_pr, Mapping):
            issues.append(
                ProposedPRValidationIssue(
                    "invalid_proposed_pr_entry",
                    "Each proposed_prs entry must be a mapping.",
                    f"proposed_prs[{index}]",
                )
            )
            continue
        pr_id = _required_string(
            raw_pr.get("id"),
            path=f"proposed_prs[{index}].id",
            issues=issues,
            issue_code="proposed_pr_id_missing",
            issue_message="Each proposed PR requires an id.",
        )
        if pr_id is not None:
            normalized = _normalize_identifier(pr_id)
            if normalized in ids:
                issues.append(
                    ProposedPRValidationIssue(
                        "duplicate_proposed_pr_id",
                        f"Proposed PR id {pr_id!r} is duplicated.",
                        f"proposed_prs[{index}].id",
                    )
                )
            ids[normalized] = pr_id
    graph: dict[str, tuple[str, ...]] = {}
    for index, raw_pr in enumerate(raw_prs):
        if not isinstance(raw_pr, Mapping):
            continue
        pr_id = _optional_string(raw_pr.get("id"))
        if pr_id is None:
            continue
        dependencies = _collect_dependent_pr_ids(
            raw_pr.get("dependent_prs", []),
            proposed_pr_id=pr_id,
            known_pr_ids=tuple(ids.values()),
            issues=issues,
        )
        graph[pr_id] = dependencies
        for detail_field in ("intent", "justification"):
            _required_string(
                raw_pr.get(detail_field),
                path=f"proposed_prs[{index}].{detail_field}",
                issues=issues,
                issue_code=f"proposed_pr_{detail_field}_missing",
                issue_message=f"Each proposed PR requires {detail_field}.",
            )
    _validate_dependency_graph(graph, issues=issues)
    _validate_feature_change_sections(
        raw_spec,
        proposed_pr_ids=tuple(ids.values()),
        issues=issues,
    )
    coverage_result = validate_proposed_pr_coverage_and_effects(
        proposed_pr_specification_yaml,
        proposed_pr_ids=tuple(ids.values()),
        feature_coverage_source=feature_coverage_source,
        effect_sources=effect_sources,
    )
    issues.extend(
        ProposedPRValidationIssue(issue.code, issue.message, issue.path)
        for issue in coverage_result.issues
    )
    return ProposedPRValidationReport(
        validation_successful=not issues,
        proposed_pr_id=None,
        available_feature_ids=list(available_feature_ids),
        known_pr_ids=known_pr_ids,
        issues=issues,
    )


def _validate_feature_change_sections(
    raw_spec: Mapping[str, Any],
    *,
    proposed_pr_ids: Sequence[str],
    issues: list[ProposedPRValidationIssue],
) -> None:
    known_ids = _normalize_identifier_set(proposed_pr_ids)
    for section in _EFFECT_SECTIONS:
        raw_items = raw_spec.get(section, [])
        if not isinstance(raw_items, Sequence) or isinstance(raw_items, (str, bytes)):
            issues.append(
                ProposedPRValidationIssue(
                    "invalid_specification_v1_section",
                    f"{section} must be a list.",
                    section,
                )
            )
            continue
        for item_index, item in enumerate(raw_items):
            path = f"{section}[{item_index}]"
            if not isinstance(item, Mapping):
                issues.append(
                    ProposedPRValidationIssue(
                        "invalid_specification_v1_item",
                        "Each specification-v1 item must be a mapping.",
                        path,
                    )
                )
                continue
            _required_string(
                item.get("id"),
                path=f"{path}.id",
                issues=issues,
                issue_code="specification_v1_id_missing",
                issue_message="Each specification-v1 item must include an id.",
            )
            action = _optional_string(item.get("action"))
            if action not in {"added", "removed", "changed"}:
                issues.append(
                    ProposedPRValidationIssue(
                        "invalid_specification_v1_action",
                        "Each specification-v1 action must be added, removed, or "
                        "changed.",
                        f"{path}.action",
                    )
                )
            proposed_pr_id = _optional_string(item.get("proposed_pr_id"))
            if proposed_pr_id is None:
                issues.append(
                    ProposedPRValidationIssue(
                        "proposed_pr_id_missing",
                        "Each feature change must identify its proposed PR.",
                        f"{path}.proposed_pr_id",
                    )
                )
            elif _normalize_identifier(proposed_pr_id) not in known_ids:
                issues.append(
                    ProposedPRValidationIssue(
                        "unknown_proposed_pr_id",
                        f"Proposed PR id {proposed_pr_id!r} is not declared above.",
                        f"{path}.proposed_pr_id",
                    )
                )


def _validate_dependency_graph(
    graph: Mapping[str, Sequence[str]],
    *,
    issues: list[ProposedPRValidationIssue],
) -> None:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str, path: tuple[str, ...]) -> None:
        normalized = _normalize_identifier(node)
        if normalized in visiting:
            issues.append(
                ProposedPRValidationIssue(
                    "proposed_pr_dependency_cycle",
                    "Proposed PR dependency cycle detected: "
                    f"{' -> '.join((*path, node))}.",
                    "proposed_prs",
                )
            )
            return
        if normalized in visited:
            return
        visiting.add(normalized)
        for dependency in graph.get(node, ()):
            visit(dependency, (*path, node))
        visiting.remove(normalized)
        visited.add(normalized)

    for node in graph:
        visit(node, ())


def _collect_dependent_pr_ids(
    raw_value: object,
    *,
    proposed_pr_id: str | None,
    known_pr_ids: Sequence[str],
    issues: list[ProposedPRValidationIssue],
) -> tuple[str, ...]:
    if not isinstance(raw_value, Sequence) or isinstance(raw_value, (str, bytes)):
        issues.append(
            ProposedPRValidationIssue(
                "invalid_dependent_prs_section",
                "dependent_prs must be a list of proposed PR ids.",
                "dependent_prs",
            )
        )
        return ()
    known_ids = _normalize_identifier_set(known_pr_ids)
    if proposed_pr_id is not None:
        known_ids.add(_normalize_identifier(proposed_pr_id))
    seen_ids: set[str] = set()
    dependent_prs: list[str] = []
    for index, raw_id in enumerate(raw_value):
        dependency_id = _required_string(
            raw_id,
            path=f"dependent_prs[{index}]",
            issues=issues,
            issue_code="dependent_pr_id_missing",
            issue_message="Each dependent_prs entry must be a proposed PR id.",
        )
        if dependency_id is None:
            continue
        normalized_id = _normalize_identifier(dependency_id)
        if normalized_id in seen_ids:
            issues.append(
                ProposedPRValidationIssue(
                    "duplicate_dependent_pr_id",
                    f"Dependent proposed PR id {dependency_id!r} is duplicated.",
                    f"dependent_prs[{index}]",
                )
            )
            continue
        seen_ids.add(normalized_id)
        dependent_prs.append(dependency_id)
        if proposed_pr_id is not None and normalized_id == _normalize_identifier(
            proposed_pr_id
        ):
            issues.append(
                ProposedPRValidationIssue(
                    "self_dependent_pr_id",
                    "A proposed PR cannot depend on itself.",
                    f"dependent_prs[{index}]",
                )
            )
        elif normalized_id not in known_ids:
            issues.append(
                ProposedPRValidationIssue(
                    "unknown_dependent_pr_id",
                    f"Dependent proposed PR id {dependency_id!r} does not "
                    "identify an existing proposed PR.",
                    f"dependent_prs[{index}]",
                )
            )
    return tuple(dependent_prs)


def _validate_proposed_pr_dependency_cycle(
    dependency_pr_specifications: Sequence[ProposedPRSourceDocument],
    proposed_pr_id: str,
    dependent_prs: Sequence[str],
    *,
    issues: list[ProposedPRValidationIssue],
) -> None:
    graph = build_proposed_pr_dependency_graph(dependency_pr_specifications)
    graph[proposed_pr_id] = tuple(dependent_prs)
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str, path: tuple[str, ...]) -> None:
        normalized_node = _normalize_identifier(node)
        if normalized_node in visiting:
            cycle = " -> ".join((*path, node))
            issues.append(
                ProposedPRValidationIssue(
                    "proposed_pr_dependency_cycle",
                    f"Proposed PR dependency cycle detected: {cycle}.",
                    "dependent_prs",
                )
            )
            return
        if normalized_node in visited:
            return
        visiting.add(normalized_node)
        for dependency in graph.get(node, ()):
            visit(dependency, (*path, node))
        visiting.remove(normalized_node)
        visited.add(normalized_node)

    visit(proposed_pr_id, ())


def _validate_template_boilerplate_removed(
    proposed_pr_specification_yaml: str,
    *,
    issues: list[ProposedPRValidationIssue],
) -> None:
    boilerplate_markers = (
        "# PR specification template.",
        "# Current feature ids:",
        "# - Delete these instructions and replace them with this comment at the top:",
    )
    if any(marker in proposed_pr_specification_yaml for marker in boilerplate_markers):
        issues.append(
            ProposedPRValidationIssue(
                "template_boilerplate_not_removed",
                "Remove the template instructions before validating the proposed "
                "PR specification.",
            )
        )


def _collect_detail_items(
    raw_items: Sequence[object],
    *,
    section_name: str,
    seen_ids: set[str],
    issues: list[ProposedPRValidationIssue],
) -> set[str]:
    item_ids: set[str] = set()
    for index, raw_item in enumerate(raw_items):
        item = _coerce_mapping(
            raw_item,
            path=f"{section_name}[{index}]",
            issues=issues,
            issue_code=f"invalid_{section_name}_item",
            issue_message=(
                f"Each {section_name.replace('_', ' ')} item must be a mapping."
            ),
        )
        if item is None:
            continue
        item_id = _required_string(
            item.get("id"),
            path=f"{section_name}[{index}].id",
            issues=issues,
            issue_code=f"{section_name}_id_missing",
            issue_message=(
                f"Each {section_name.replace('_', ' ')} item must include an id."
            ),
        )
        _required_string(
            item.get("description"),
            path=f"{section_name}[{index}].description",
            issues=issues,
            issue_code=f"{section_name}_description_missing",
            issue_message=(
                f"Each {section_name.replace('_', ' ')} item must include a "
                "description."
            ),
        )
        if item_id is None:
            continue
        if section_name == "required_test_cases":
            contract = VerificationContract.from_mapping(item)
            unknown_fields = (
                set(item) - {"id", "description"} - set(VERIFICATION_CONTRACT_FIELDS)
            )
            for field_name in sorted(unknown_fields):
                issues.append(
                    ProposedPRValidationIssue(
                        "verification_contract_field_unknown",
                        f"Unknown verification contract field {field_name!r}.",
                        f"{section_name}[{index}].{field_name}",
                    )
                )
            errors = [
                *VerificationContract.mapping_validation_errors(item),
                *contract.validation_errors(),
            ]
            for error in errors:
                issues.append(
                    ProposedPRValidationIssue(
                        "verification_contract_invalid",
                        error,
                        f"{section_name}[{index}]",
                    )
                )
        if item_id in seen_ids:
            issues.append(
                ProposedPRValidationIssue(
                    "duplicate_detail_id",
                    f"Detail id {item_id!r} appears more than once across the "
                    "proposed PR specification.",
                    f"{section_name}[{index}].id",
                )
            )
            continue
        seen_ids.add(item_id)
        item_ids.add(item_id)
    return item_ids


def _collect_feature_ids(
    raw_feature_ids: Sequence[object],
    *,
    available_feature_ids: set[str],
    issues: list[ProposedPRValidationIssue],
) -> set[str]:
    feature_ids: set[str] = set()
    for index, raw_feature_id in enumerate(raw_feature_ids):
        feature_id = _required_string(
            raw_feature_id,
            path=f"feature_ids[{index}]",
            issues=issues,
            issue_code="feature_id_missing",
            issue_message="Each feature id must be a string.",
        )
        if feature_id is None:
            continue
        if feature_id in feature_ids:
            issues.append(
                ProposedPRValidationIssue(
                    "duplicate_feature_id",
                    f"Feature id {feature_id!r} appears more than once.",
                    f"feature_ids[{index}]",
                )
            )
            continue
        if feature_id not in available_feature_ids:
            issues.append(
                ProposedPRValidationIssue(
                    "unknown_feature_id",
                    f"Feature id {feature_id!r} is not listed in the current source "
                    "specifications.",
                    f"feature_ids[{index}]",
                )
            )
            continue
        feature_ids.add(feature_id)
    return feature_ids


def _load_yaml_mapping(raw_yaml: str) -> Mapping[str, Any]:
    loaded = yaml.safe_load(raw_yaml)
    if loaded is None:
        return {}
    if not isinstance(loaded, Mapping):
        raise TypeError("Top-level PR specification must be a mapping.")
    return cast(Mapping[str, Any], loaded)


def _coerce_sequence(
    raw_value: object,
    *,
    path: str,
    issues: list[ProposedPRValidationIssue],
    issue_code: str,
    issue_message: str,
) -> Sequence[object]:
    if raw_value is None:
        return ()
    if isinstance(raw_value, Sequence) and not isinstance(raw_value, (str, bytes)):
        return raw_value
    issues.append(ProposedPRValidationIssue(issue_code, issue_message, path))
    return ()


def _coerce_mapping(
    raw_value: object,
    *,
    path: str,
    issues: list[ProposedPRValidationIssue],
    issue_code: str,
    issue_message: str,
) -> Mapping[str, Any] | None:
    if isinstance(raw_value, Mapping):
        return cast(Mapping[str, Any], raw_value)
    issues.append(ProposedPRValidationIssue(issue_code, issue_message, path))
    return None


def _required_string(
    raw_value: object,
    *,
    path: str,
    issues: list[ProposedPRValidationIssue],
    issue_code: str,
    issue_message: str,
) -> str | None:
    if raw_value is None:
        issues.append(ProposedPRValidationIssue(issue_code, issue_message, path))
        return None
    value = str(raw_value).strip()
    if value == "":
        issues.append(ProposedPRValidationIssue(issue_code, issue_message, path))
        return None
    return value


def _optional_string(raw_value: object) -> str | None:
    if raw_value is None:
        return None
    value = str(raw_value).strip()
    return value or None


def _normalize_identifier(value: str) -> str:
    return value.strip().casefold()


def _normalize_identifier_set(values: Sequence[str]) -> set[str]:
    return {_normalize_identifier(value) for value in values}
