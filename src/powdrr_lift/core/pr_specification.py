from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, cast

import yaml

from powdrr_lift.change_log_template import _resolve_repo_root
from powdrr_lift.core.codebase_state import build_codebase_state_report
from powdrr_lift.core.spec_paths import CURRENT_ROOT, PROPOSALS_ROOT
from powdrr_lift.core.template_generation import merge_existing_template_content
from powdrr_lift.core.validation_messages import (
    ValidationError,
    validation_error_to_data,
)

_DEFAULT_OUTPUT_PATH = PROPOSALS_ROOT
_IMPLEMENTATION_SPECIFICATION_DIRS = (
    CURRENT_ROOT,
    PROPOSALS_ROOT,
    Path("docs") / "specs",
)
_EFFECT_SECTIONS = (
    "entities",
    "modules",
    "tools",
    "entity_relationships",
    "features",
    "decisions",
)
_EFFECT_SOURCE_FILES = (
    "architecture-specification.yaml",
    "implementation-specification.yaml",
)


@dataclass(frozen=True, slots=True)
class PRSpecificationValidationIssue(ValidationError):
    pass


@dataclass(frozen=True, slots=True)
class PRSpecificationValidationReport:
    validation_successful: bool
    proposed_pr_id: str | None
    available_feature_ids: list[str] = field(default_factory=list)
    known_pr_ids: list[str] = field(default_factory=list)
    issues: list[PRSpecificationValidationIssue] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _FeatureCatalogEntry:
    feature_id: str
    source_path: str
    entity_type: str | None


@dataclass(frozen=True, slots=True)
class ProposedPRSearchResult:
    pr_number: int | None
    proposed_pr_id: str | None
    path: Path
    score: float
    matched_fields: tuple[str, ...] = field(default_factory=tuple)
    feature_ids: tuple[str, ...] = field(default_factory=tuple)
    intent_goal: str | None = None
    intent_reasoning: str | None = None


@dataclass(frozen=True, slots=True)
class ProposedPRSearchReport:
    query: str
    results: list[ProposedPRSearchResult] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _ProposedPRDocument:
    pr_number: int | None
    path: Path
    data: Mapping[str, Any]
    proposed_pr_id: str | None
    feature_ids: tuple[str, ...]
    intent_goal: str | None
    intent_reasoning: str | None


def pr_specification_default_output_path(
    work_item_name: str,
    repo_root: str | Path | None = None,
) -> Path:
    repo_root_path = _resolve_repo_root(repo_root)
    return (
        repo_root_path
        / _DEFAULT_OUTPUT_PATH
        / work_item_name
        / "proposed-pr-specification.yaml"
    )


def render_pr_specification_template(
    *,
    repo_root: str | Path | None = None,
    work_item_name: str | None = None,
) -> str:
    repo_root_path = _resolve_repo_root(repo_root)
    feature_catalog = _load_feature_catalog(repo_root_path)
    if not feature_catalog:
        raise ValueError(
            "No current feature ids were found in the codebase state. "
            "Generate the codebase state first and ensure it contains current "
            "entities before generating a PR specification."
        )

    feature_lines = [
        (
            f"# - {entry.feature_id} "
            f"({entry.entity_type or 'entity'}, {entry.source_path})"
        )
        for entry in feature_catalog
    ]

    authoritative_effects = _load_authoritative_effects(
        repo_root_path,
        work_item_name=work_item_name,
    )
    lines = [
        "# PR specification template.",
        "#",
        "# Instructions:",
        "# - Keep every proposed PR for this feature in this one file.",
        "# - Set the top-level `id` to the feature/work-item id.",
        "# - Add one ordered entry under `proposed_prs` for each proposed PR.",
        "# - Set each proposed PR `id` to a globally unique proposed PR id.",
        "# - List prerequisite proposed PR ids under each entry's `dependent_prs`.",
        "# - Dependency ids must identify another entry in this same file; cycles",
        "#   are not allowed.",
        "# - Reference one or more current feature ids from the codebase state",
        "#   listed below.",
        "# - Fill in each proposed PR's intent, justification, and dependencies.",
        "# - In the feature-wide specification-v1 sections below",
        "#   (`entities`, `modules`, `tools`, `entity_relationships`, `features`,",
        "#   and `decisions`), record every action-bearing id once and label each",
        "#   item with the proposed_pr_id that will deliver it.",
        "#   These IDs and actions are copied from the authoritative v1 proposal",
        "#   specs and must not be changed, omitted, or duplicated.",
        "# - Delete these instructions and replace them with this comment at the top:",
        '#   "# This file is read-only and should never be edited by a tool or agent."',
        "#",
        "# Current feature ids:",
        *feature_lines,
        "schema: https://powdrr.io/schemas/proposed-pr-specification-v1",
        "id: null",
        "feature_ids:",
        "  - null",
        "proposed_prs:",
        "  - id: null",
        "    intent: null",
        "    justification: null",
        "    dependent_prs: []",
        *[
            line
            for section in _EFFECT_SECTIONS
            for line in _render_authoritative_effect_section(
                section,
                authoritative_effects.get(section, ()),
            )
        ],
        "",
    ]
    return "\n".join(lines)


def _load_authoritative_effects(
    repo_root: Path,
    *,
    work_item_name: str | None,
) -> dict[str, tuple[tuple[str, str], ...]]:
    effects: dict[str, list[tuple[str, str]]] = {
        section: [] for section in _EFFECT_SECTIONS
    }
    if work_item_name is None:
        return {section: tuple(items) for section, items in effects.items()}
    proposal_dir = repo_root / PROPOSALS_ROOT / work_item_name
    seen: dict[str, set[tuple[str, str]]] = {
        section: set() for section in _EFFECT_SECTIONS
    }
    for filename in _EFFECT_SOURCE_FILES:
        path = proposal_dir / filename
        if not path.is_file():
            continue
        try:
            data = _load_yaml_mapping(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        for section in _EFFECT_SECTIONS:
            raw_items = data.get(section, [])
            if not isinstance(raw_items, Sequence) or isinstance(
                raw_items, (str, bytes)
            ):
                continue
            for item in raw_items:
                if not isinstance(item, Mapping):
                    continue
                item_id = _optional_string(item.get("id"))
                action = _optional_string(item.get("action"))
                if item_id is None or action is None:
                    continue
                effect = (item_id, action)
                if effect not in seen[section]:
                    seen[section].add(effect)
                    effects[section].append(effect)
    return {section: tuple(items) for section, items in effects.items()}


def _render_authoritative_effect_section(
    section: str,
    effects: Sequence[tuple[str, str]],
) -> list[str]:
    lines = [f"{section}:"]
    if not effects:
        lines[-1] = f"{section}: []"
        return lines
    for item_id, action in effects:
        lines.extend(
            [
                f"  - id: {item_id}",
                f"    action: {action}",
                "    proposed_pr_id: null",
            ]
        )
    return lines


def create_pr_specification_template(
    *,
    work_item_name: str,
    output_path: str | Path | None = None,
    repo_root: str | Path | None = None,
) -> Path:
    repo_root_path = _resolve_repo_root(repo_root)
    resolved_output_path = _resolve_output_path(
        repo_root_path,
        work_item_name,
        output_path,
    )
    resolved_output_path.parent.mkdir(parents=True, exist_ok=True)
    existing_content = (
        resolved_output_path.read_text(encoding="utf-8")
        if resolved_output_path.is_file()
        else None
    )
    resolved_output_path.write_text(
        merge_existing_template_content(
            render_pr_specification_template(
                repo_root=repo_root_path,
                work_item_name=work_item_name,
            ),
            existing_content,
        ),
        encoding="utf-8",
    )
    return resolved_output_path


def proposed_pr_specification_path(
    pr_number: int,
    *,
    repo_root: str | Path | None = None,
) -> Path:
    repo_root_path = _resolve_repo_root(repo_root)
    for specification_path in _iter_proposed_pr_specification_paths(repo_root_path):
        if _parse_proposed_pr_number(specification_path) == pr_number:
            return specification_path

    proposal_path = (
        repo_root_path
        / PROPOSALS_ROOT
        / f"PR-{pr_number}"
        / "proposed-pr-specification.yaml"
    )
    if proposal_path.exists():
        return proposal_path
    return (
        repo_root_path
        / "docs"
        / "specs"
        / f"PR-{pr_number}"
        / "proposed-pr-specification.yaml"
    )


def show_proposed_pr_specification(
    pr_number: int,
    *,
    repo_root: str | Path | None = None,
) -> str:
    specification_path = proposed_pr_specification_path(pr_number, repo_root=repo_root)
    if not specification_path.exists():
        raise FileNotFoundError(
            f"Proposed PR specification not found: {specification_path}"
        )
    return specification_path.read_text(encoding="utf-8")


def search_proposed_pr_specifications(
    query: str,
    *,
    repo_root: str | Path | None = None,
    limit: int = 10,
) -> ProposedPRSearchReport:
    repo_root_path = _resolve_repo_root(repo_root)
    normalized_query = query.strip()
    if normalized_query == "":
        raise ValueError("Query must not be empty.")

    documents = _load_proposed_pr_documents(repo_root_path)
    results = sorted(
        (
            _score_proposed_pr_document(normalized_query, document)
            for document in documents
        ),
        key=lambda result: (
            -result.score,
            result.pr_number if result.pr_number is not None else float("inf"),
            result.path.name,
        ),
    )
    filtered_results = [result for result in results if result.score > 0.0][:limit]
    return ProposedPRSearchReport(query=normalized_query, results=filtered_results)


def render_proposed_pr_search_report(report: ProposedPRSearchReport) -> str:
    return yaml.safe_dump(
        _proposed_pr_search_report_to_data(report),
        sort_keys=False,
        allow_unicode=False,
    )


def validate_pr_specification_yaml(
    proposed_pr_specification_yaml: str,
    *,
    work_item_name: str,
    repo_root: str | Path | None = None,
    file_path: str | Path | None = None,
) -> str:
    report = build_pr_specification_validation_report(
        proposed_pr_specification_yaml,
        work_item_name=work_item_name,
        repo_root=repo_root,
        file_path=file_path,
    )
    return yaml.safe_dump(_report_to_data(report, file_path=file_path), sort_keys=False)


def build_pr_specification_validation_report(
    proposed_pr_specification_yaml: str,
    *,
    work_item_name: str,
    repo_root: str | Path | None = None,
    file_path: str | Path | None = None,
) -> PRSpecificationValidationReport:
    repo_root_path = _resolve_repo_root(repo_root)
    issues: list[PRSpecificationValidationIssue] = []
    feature_catalog = _load_feature_catalog(repo_root_path)
    available_feature_ids = [entry.feature_id for entry in feature_catalog]
    known_pr_ids = _load_existing_pr_ids(
        repo_root_path,
        excluded_file_path=file_path,
    )

    try:
        raw_spec = _load_yaml_mapping(proposed_pr_specification_yaml)
    except Exception as exc:  # noqa: BLE001
        issues.append(
            PRSpecificationValidationIssue(
                code="invalid_yaml",
                message=f"Could not parse proposed PR specification YAML: {exc}",
            )
        )
        return PRSpecificationValidationReport(
            validation_successful=False,
            proposed_pr_id=None,
            available_feature_ids=available_feature_ids,
            known_pr_ids=known_pr_ids,
            issues=issues,
        )

    _validate_template_boilerplate_removed(
        proposed_pr_specification_yaml,
        issues=issues,
    )

    if "proposed_prs" in raw_spec:
        return _build_multi_proposed_pr_validation_report(
            raw_spec,
            proposed_pr_specification_yaml=proposed_pr_specification_yaml,
            work_item_name=work_item_name,
            repo_root=repo_root_path,
            file_path=file_path,
            available_feature_ids=available_feature_ids,
            known_pr_ids=known_pr_ids,
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
            PRSpecificationValidationIssue(
                code="duplicate_proposed_pr_id",
                message=f"Proposed PR id {proposed_pr_id!r} already exists.",
                path="id",
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
            repo_root_path,
            proposed_pr_id,
            dependent_prs,
            excluded_file_path=file_path,
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
            issue_message=(
                "intent must be a mapping with problem, goal, and reasoning."
            ),
        )
    )
    if intent is not None:
        _required_string(
            intent.get("problem"),
            path="intent.problem",
            issues=issues,
            issue_code="intent_problem_missing",
            issue_message="The intent.problem field is required.",
        )
        _required_string(
            intent.get("goal"),
            path="intent.goal",
            issues=issues,
            issue_code="intent_goal_missing",
            issue_message="The intent.goal field is required.",
        )
        _required_string(
            intent.get("reasoning"),
            path="intent.reasoning",
            issues=issues,
            issue_code="intent_reasoning_missing",
            issue_message="The intent.reasoning field is required.",
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
                issue_message=(f"{section_name} must be a list of detail items."),
            ),
            section_name=section_name,
            seen_ids=seen_detail_ids,
            issues=issues,
        )

    return PRSpecificationValidationReport(
        validation_successful=not issues,
        proposed_pr_id=proposed_pr_id,
        available_feature_ids=available_feature_ids,
        known_pr_ids=known_pr_ids,
        issues=issues,
    )


def _build_multi_proposed_pr_validation_report(
    raw_spec: Mapping[str, Any],
    *,
    proposed_pr_specification_yaml: str,
    work_item_name: str,
    repo_root: Path,
    file_path: str | Path | None,
    available_feature_ids: list[str],
    known_pr_ids: list[str],
    issues: list[PRSpecificationValidationIssue],
) -> PRSpecificationValidationReport:
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
                PRSpecificationValidationIssue(
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
                    PRSpecificationValidationIssue(
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
        entry = raw_pr
        pr_id = _optional_string(entry.get("id"))
        if pr_id is None:
            continue
        dependencies = _collect_dependent_pr_ids(
            entry.get("dependent_prs", []),
            proposed_pr_id=pr_id,
            known_pr_ids=tuple(ids.values()),
            issues=issues,
        )
        graph[pr_id] = dependencies
        for detail_field in ("intent", "justification"):
            _required_string(
                entry.get(detail_field),
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
    _validate_v1_effect_equivalence(
        raw_spec,
        repo_root=repo_root,
        work_item_name=work_item_name,
        file_path=file_path,
        issues=issues,
    )
    return PRSpecificationValidationReport(
        validation_successful=not issues,
        proposed_pr_id=None,
        available_feature_ids=available_feature_ids,
        known_pr_ids=known_pr_ids,
        issues=issues,
    )


def _validate_feature_change_sections(
    raw_spec: Mapping[str, Any],
    *,
    proposed_pr_ids: Sequence[str],
    issues: list[PRSpecificationValidationIssue],
) -> None:
    known_ids = _normalize_identifier_set(proposed_pr_ids)
    for section in _EFFECT_SECTIONS:
        raw_items = raw_spec.get(section, [])
        if not isinstance(raw_items, Sequence) or isinstance(raw_items, (str, bytes)):
            issues.append(
                PRSpecificationValidationIssue(
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
                    PRSpecificationValidationIssue(
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
                    PRSpecificationValidationIssue(
                        "invalid_specification_v1_action",
                        "Each specification-v1 action must be added, removed, or "
                        "changed.",
                        f"{path}.action",
                    )
                )
            proposed_pr_id = _optional_string(item.get("proposed_pr_id"))
            if proposed_pr_id is None:
                issues.append(
                    PRSpecificationValidationIssue(
                        "proposed_pr_id_missing",
                        "Each feature change must identify its proposed PR.",
                        f"{path}.proposed_pr_id",
                    )
                )
            elif _normalize_identifier(proposed_pr_id) not in known_ids:
                issues.append(
                    PRSpecificationValidationIssue(
                        "unknown_proposed_pr_id",
                        f"Proposed PR id {proposed_pr_id!r} is not declared above.",
                        f"{path}.proposed_pr_id",
                    )
                )


def _validate_multi_pr_details(
    entry: Mapping[str, Any],
    *,
    index: int,
    issues: list[PRSpecificationValidationIssue],
) -> None:
    intent = entry.get("intent")
    if not isinstance(intent, Mapping):
        issues.append(
            PRSpecificationValidationIssue(
                "intent_missing",
                "Each proposed PR must include an intent mapping.",
                f"proposed_prs[{index}].intent",
            )
        )
    else:
        for field in ("problem", "goal", "reasoning"):
            _required_string(
                intent.get(field),
                path=f"proposed_prs[{index}].intent.{field}",
                issues=issues,
                issue_code=f"intent_{field}_missing",
                issue_message=f"The intent.{field} field is required.",
            )
    seen_detail_ids: set[str] = set()
    for section in (
        "acceptance_criteria",
        "expected_tests",
        "required_test_cases",
        "expected_outcomes",
        "non_goals",
        "risks",
    ):
        _collect_detail_items(
            _coerce_sequence(
                entry.get(section, []),
                path=f"proposed_prs[{index}].{section}",
                issues=issues,
                issue_code=f"invalid_{section}_section",
                issue_message=f"{section} must be a list of detail items.",
            ),
            section_name=f"proposed_prs[{index}].{section}",
            seen_ids=seen_detail_ids,
            issues=issues,
        )
    for section in _EFFECT_SECTIONS:
        raw_items = entry.get(section, [])
        if not isinstance(raw_items, Sequence) or isinstance(raw_items, (str, bytes)):
            issues.append(
                PRSpecificationValidationIssue(
                    "invalid_specification_v1_section",
                    f"{section} must be a list in each proposed PR entry.",
                    f"proposed_prs[{index}].{section}",
                )
            )
            continue
        for item_index, item in enumerate(raw_items):
            if not isinstance(item, Mapping):
                issues.append(
                    PRSpecificationValidationIssue(
                        "invalid_specification_v1_item",
                        "Each specification-v1 item must be a mapping with id "
                        "and action.",
                        f"proposed_prs[{index}].{section}[{item_index}]",
                    )
                )
                continue
            _required_string(
                item.get("id"),
                path=f"proposed_prs[{index}].{section}[{item_index}].id",
                issues=issues,
                issue_code="specification_v1_id_missing",
                issue_message="Each specification-v1 item must include an id.",
            )
            action = _optional_string(item.get("action"))
            if action not in {"added", "removed", "changed"}:
                issues.append(
                    PRSpecificationValidationIssue(
                        "invalid_specification_v1_action",
                        "Each specification-v1 action must be added, removed, or "
                        "changed.",
                        f"proposed_prs[{index}].{section}[{item_index}].action",
                    )
                )


def _validate_dependency_graph(
    graph: Mapping[str, Sequence[str]],
    *,
    issues: list[PRSpecificationValidationIssue],
) -> None:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str, path: tuple[str, ...]) -> None:
        normalized = _normalize_identifier(node)
        if normalized in visiting:
            issues.append(
                PRSpecificationValidationIssue(
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


def _validate_v1_effect_equivalence(
    raw_spec: Mapping[str, Any],
    *,
    repo_root: Path,
    work_item_name: str,
    file_path: str | Path | None,
    issues: list[PRSpecificationValidationIssue],
) -> None:
    proposal_dir = (
        Path(file_path).resolve().parent
        if file_path is not None
        else repo_root / PROPOSALS_ROOT / work_item_name
    )
    source_effects: dict[str, list[tuple[str, str]]] = {
        section: [] for section in _EFFECT_SECTIONS
    }
    seen_source_effects: dict[str, set[tuple[str, str]]] = {
        section: set() for section in _EFFECT_SECTIONS
    }
    found_source = False
    for filename in _EFFECT_SOURCE_FILES:
        path = proposal_dir / filename
        if not path.is_file():
            continue
        found_source = True
        try:
            data = _load_yaml_mapping(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            issues.append(
                PRSpecificationValidationIssue(
                    "invalid_effect_source",
                    f"Could not read v1 effect source: {exc}",
                    str(path),
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
                    item_id = _optional_string(item.get("id"))
                    action = _optional_string(item.get("action"))
                    if item_id is not None and action is not None:
                        effect = (item_id, action)
                        if effect not in seen_source_effects[section]:
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
        raw_items = raw_spec.get(section, [])
        if not isinstance(raw_items, Sequence) or isinstance(raw_items, (str, bytes)):
            continue
        for item in raw_items:
            if isinstance(item, Mapping):
                item_id = _optional_string(item.get("id"))
                action = _optional_string(item.get("action"))
                if item_id is not None and action is not None:
                    effect = (item_id, action)
                    if effect not in seen_proposed_effects[section]:
                        seen_proposed_effects[section].add(effect)
                        proposed_effects[section].append(effect)
    for section in _EFFECT_SECTIONS:
        if proposed_effects[section] != source_effects[section]:
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
                PRSpecificationValidationIssue(
                    "v1_effect_mismatch",
                    f"Proposed PR {section} entries do not match the authoritative "
                    "ordered id/action effects. " + " ".join(details),
                    f"proposed_prs.{section}",
                )
            )


def _format_effect_pairs(effects: Sequence[tuple[str, str]]) -> str:
    if not effects:
        return "[]"
    return (
        "["
        + ", ".join(f"({item_id!r}, {action!r})" for item_id, action in effects)
        + "]"
    )


def _collect_dependent_pr_ids(
    raw_value: object,
    *,
    proposed_pr_id: str | None,
    known_pr_ids: Sequence[str],
    issues: list[PRSpecificationValidationIssue],
) -> tuple[str, ...]:
    if not isinstance(raw_value, Sequence) or isinstance(raw_value, (str, bytes)):
        issues.append(
            PRSpecificationValidationIssue(
                code="invalid_dependent_prs_section",
                message="dependent_prs must be a list of proposed PR ids.",
                path="dependent_prs",
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
                PRSpecificationValidationIssue(
                    code="duplicate_dependent_pr_id",
                    message=(
                        f"Dependent proposed PR id {dependency_id!r} is duplicated."
                    ),
                    path=f"dependent_prs[{index}]",
                )
            )
            continue
        seen_ids.add(normalized_id)
        dependent_prs.append(dependency_id)
        if proposed_pr_id is not None and normalized_id == _normalize_identifier(
            proposed_pr_id
        ):
            issues.append(
                PRSpecificationValidationIssue(
                    code="self_dependent_pr_id",
                    message="A proposed PR cannot depend on itself.",
                    path=f"dependent_prs[{index}]",
                )
            )
        elif normalized_id not in known_ids:
            issues.append(
                PRSpecificationValidationIssue(
                    code="unknown_dependent_pr_id",
                    message=(
                        f"Dependent proposed PR id {dependency_id!r} does not "
                        "identify an existing proposed PR."
                    ),
                    path=f"dependent_prs[{index}]",
                )
            )
    return tuple(dependent_prs)


def load_proposed_pr_dependency_graph(
    repo_root: str | Path,
) -> dict[str, tuple[str, ...]]:
    """Load the proposed-PR dependency graph from checked-in specifications."""
    repo_root_path = _resolve_repo_root(repo_root)
    graph: dict[str, tuple[str, ...]] = {}
    for specification_path in _iter_proposed_pr_specification_paths(repo_root_path):
        try:
            raw_spec = _load_yaml_mapping(
                specification_path.read_text(encoding="utf-8")
            )
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


def _validate_proposed_pr_dependency_cycle(
    repo_root: Path,
    proposed_pr_id: str,
    dependent_prs: Sequence[str],
    *,
    excluded_file_path: str | Path | None,
    issues: list[PRSpecificationValidationIssue],
) -> None:
    graph = load_proposed_pr_dependency_graph(repo_root)
    if excluded_file_path is not None:
        for existing_id, _dependencies in list(graph.items()):
            if existing_id == proposed_pr_id:
                del graph[existing_id]
    graph[proposed_pr_id] = tuple(dependent_prs)

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str, path: tuple[str, ...]) -> None:
        normalized_node = _normalize_identifier(node)
        if normalized_node in visiting:
            cycle = " -> ".join((*path, node))
            issues.append(
                PRSpecificationValidationIssue(
                    code="proposed_pr_dependency_cycle",
                    message=f"Proposed PR dependency cycle detected: {cycle}.",
                    path="dependent_prs",
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


def _load_feature_catalog(repo_root: Path) -> list[_FeatureCatalogEntry]:
    try:
        codebase_state_report = build_codebase_state_report(repo_root=repo_root)
    except Exception:  # noqa: BLE001
        codebase_state_report = None

    catalog: list[_FeatureCatalogEntry] = []
    seen_feature_ids: set[str] = set()
    if codebase_state_report is not None and codebase_state_report.entities:
        for entity in codebase_state_report.entities:
            if entity.id in seen_feature_ids:
                continue
            seen_feature_ids.add(entity.id)
            source_path = entity.source.changelog_path or "current codebase state"
            catalog.append(
                _FeatureCatalogEntry(
                    feature_id=entity.id,
                    source_path=source_path,
                    entity_type=entity.type,
                )
            )

    # The checked-in codebase state is authoritative for repository-wide
    # entities, but a workflow may be validating a specification that exists
    # only in the current worktree. Include those local implementation
    # features as well instead of falling back to them only when the codebase
    # state is empty.
    implementation_paths = [
        specification_path
        for implementation_dir in _IMPLEMENTATION_SPECIFICATION_DIRS
        if (repo_root / implementation_dir).exists()
        for specification_path in (repo_root / implementation_dir).rglob(
            "implementation-specification.yaml"
        )
    ]
    for specification_path in sorted(implementation_paths):
        try:
            raw_spec = _load_yaml_mapping(
                specification_path.read_text(encoding="utf-8")
            )
        except Exception:  # noqa: BLE001
            continue

        features = _coerce_sequence(
            raw_spec.get("features"),
            path=f"{specification_path}#features",
            issues=[],
            issue_code="invalid_feature_registry",
            issue_message="",
        )
        for feature in features:
            if not isinstance(feature, Mapping):
                continue
            feature_id = _optional_string(feature.get("id"))
            if feature_id is None or feature_id in seen_feature_ids:
                continue
            seen_feature_ids.add(feature_id)
            catalog.append(
                _FeatureCatalogEntry(
                    feature_id=feature_id,
                    source_path=str(specification_path.relative_to(repo_root)),
                    entity_type="feature",
                )
            )

    return catalog


def _load_existing_pr_ids(
    repo_root: Path,
    *,
    excluded_file_path: str | Path | None = None,
) -> list[str]:
    pr_ids: list[str] = []
    seen_ids: set[str] = set()
    excluded_path = (
        (
            (repo_root / Path(excluded_file_path))
            if not Path(excluded_file_path).expanduser().is_absolute()
            else Path(excluded_file_path).expanduser()
        ).resolve()
        if excluded_file_path is not None
        else None
    )
    for specification_path in _iter_proposed_pr_specification_paths(repo_root):
        if excluded_path is not None and specification_path.resolve() == excluded_path:
            continue
        try:
            raw_spec = _load_yaml_mapping(
                specification_path.read_text(encoding="utf-8")
            )
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
            normalized_proposed_pr_id = _normalize_identifier(candidate_id)
            if normalized_proposed_pr_id in seen_ids:
                continue
            seen_ids.add(normalized_proposed_pr_id)
            pr_ids.append(candidate_id)

    return pr_ids


def _load_proposed_pr_documents(repo_root: Path) -> list[_ProposedPRDocument]:
    documents: list[_ProposedPRDocument] = []
    for specification_path in _iter_proposed_pr_specification_paths(repo_root):
        pr_number = _parse_proposed_pr_number(specification_path)

        try:
            raw_spec = _load_yaml_mapping(
                specification_path.read_text(encoding="utf-8")
            )
        except Exception:  # noqa: BLE001
            continue

        nested_prs = raw_spec.get("proposed_prs")
        records = (
            [item for item in nested_prs if isinstance(item, Mapping)]
            if isinstance(nested_prs, Sequence)
            and not isinstance(nested_prs, (str, bytes))
            else [raw_spec]
        )
        top_level_feature_ids = raw_spec.get("feature_ids")
        for record in records:
            proposed_pr_id = _optional_string(record.get("id"))
            feature_values = record.get("feature_ids", top_level_feature_ids)
            feature_ids = tuple(
                feature_id
                for feature_id in (
                    _optional_string(raw_feature_id)
                    for raw_feature_id in _coerce_sequence(
                        feature_values,
                        path=f"{specification_path}#feature_ids",
                        issues=[],
                        issue_code="invalid_feature_ids_section",
                        issue_message="",
                    )
                )
                if feature_id is not None
            )
            intent = _coerce_mapping(
                record.get("intent"),
                path=f"{specification_path}#intent",
                issues=[],
                issue_code="invalid_intent_section",
                issue_message="",
            )
            documents.append(
                _ProposedPRDocument(
                    pr_number=pr_number,
                    path=specification_path,
                    data=record,
                    proposed_pr_id=proposed_pr_id,
                    feature_ids=feature_ids,
                    intent_goal=_optional_string(intent.get("goal"))
                    if intent
                    else None,
                    intent_reasoning=(
                        _optional_string(intent.get("reasoning")) if intent else None
                    ),
                )
            )

    return documents


def _iter_proposed_pr_specification_paths(repo_root: Path) -> list[Path]:
    specification_paths = [
        specification_path
        for proposal_root in (
            repo_root / PROPOSALS_ROOT,
            repo_root / "docs" / "specs",
        )
        if proposal_root.exists()
        for specification_path in proposal_root.rglob("*.yaml")
        if specification_path.is_file()
        and (
            specification_path.name == "proposed-pr-specification.yaml"
            or specification_path.name.endswith("-proposed-pr-specification.yaml")
        )
    ]

    seen_paths: set[Path] = set()
    ordered_paths: list[Path] = []
    for specification_path in sorted(specification_paths):
        if specification_path in seen_paths:
            continue

        seen_paths.add(specification_path)
        ordered_paths.append(specification_path)

    return ordered_paths


def _parse_proposed_pr_number(specification_path: Path) -> int | None:
    parent_name = specification_path.parent.name
    if parent_name.startswith("PR-"):
        number_text = parent_name.removeprefix("PR-")
        if number_text.isdigit():
            return int(number_text)

    filename = specification_path.name
    if filename.startswith("PR-") and filename.endswith(
        "-proposed-pr-specification.yaml"
    ):
        number_text = filename.removeprefix("PR-").removesuffix(
            "-proposed-pr-specification.yaml"
        )
        if number_text.isdigit():
            return int(number_text)

    return None


def _score_proposed_pr_document(
    query: str,
    document: _ProposedPRDocument,
) -> ProposedPRSearchResult:
    field_texts = {
        "id": document.proposed_pr_id or "",
        "feature_ids": " ".join(document.feature_ids),
        "intent.goal": document.intent_goal or "",
        "intent.reasoning": document.intent_reasoning or "",
        "acceptance_criteria": _collect_detail_text(
            document.data.get("acceptance_criteria")
        ),
        "expected_tests": _collect_detail_text(document.data.get("expected_tests")),
        "required_test_cases": _collect_detail_text(
            document.data.get("required_test_cases")
        ),
        "expected_outcomes": _collect_detail_text(
            document.data.get("expected_outcomes")
        ),
        "non_goals": _collect_detail_text(document.data.get("non_goals")),
        "risks": _collect_detail_text(document.data.get("risks")),
    }

    matched_fields: list[str] = []
    best_score = 0.0
    for field_name, field_text in field_texts.items():
        score = _score_text(query, field_text)
        if score > best_score:
            best_score = score
        if score > 0.25:
            matched_fields.append(field_name)

    if document.proposed_pr_id and document.proposed_pr_id == query:
        best_score = 1.0
        if "id" not in matched_fields:
            matched_fields.append("id")

    return ProposedPRSearchResult(
        pr_number=document.pr_number,
        proposed_pr_id=document.proposed_pr_id,
        path=document.path,
        score=round(best_score, 4),
        matched_fields=tuple(dict.fromkeys(matched_fields)),
        feature_ids=document.feature_ids,
        intent_goal=document.intent_goal,
        intent_reasoning=document.intent_reasoning,
    )


def _collect_detail_text(raw_value: object | None) -> str:
    if not isinstance(raw_value, Sequence) or isinstance(raw_value, (str, bytes)):
        return ""

    collected: list[str] = []
    for item in raw_value:
        if not isinstance(item, Mapping):
            continue
        item_id = _optional_string(item.get("id"))
        description = _optional_string(item.get("description"))
        if item_id is not None:
            collected.append(item_id)
        if description is not None:
            collected.append(description)
    return " ".join(collected)


def _score_text(query: str, text: str) -> float:
    normalized_text = text.strip().lower()
    if normalized_text == "":
        return 0.0

    normalized_query = query.strip().lower()
    if normalized_query == "":
        return 0.0

    if normalized_query in normalized_text:
        return 1.0

    query_tokens = [token for token in _tokenize(normalized_query) if len(token) > 1]
    if query_tokens:
        overlap = sum(1 for token in query_tokens if token in normalized_text)
        token_score = overlap / len(query_tokens)
    else:
        token_score = 0.0

    return max(
        SequenceMatcher(None, normalized_query, normalized_text).ratio(), token_score
    )


def _tokenize(text: str) -> list[str]:
    tokens: list[str] = []
    current: list[str] = []
    for character in text:
        if character.isalnum():
            current.append(character)
            continue
        if current:
            tokens.append("".join(current))
            current = []
    if current:
        tokens.append("".join(current))
    return tokens


def _validate_template_boilerplate_removed(
    proposed_pr_specification_yaml: str,
    *,
    issues: list[PRSpecificationValidationIssue],
) -> None:
    boilerplate_markers = (
        "# PR specification template.",
        "# Current feature ids:",
        "# - Delete these instructions and replace them with this comment at the top:",
    )
    for marker in boilerplate_markers:
        if marker in proposed_pr_specification_yaml:
            issues.append(
                PRSpecificationValidationIssue(
                    code="template_boilerplate_not_removed",
                    message=(
                        "Remove the template instructions before validating the "
                        "proposed PR specification."
                    ),
                    path=None,
                )
            )
            return


def _normalize_identifier(value: str) -> str:
    return value.strip().casefold()


def _normalize_identifier_set(values: Sequence[str]) -> set[str]:
    return {_normalize_identifier(value) for value in values}


def _collect_detail_items(
    raw_items: Sequence[object],
    *,
    section_name: str,
    seen_ids: set[str],
    issues: list[PRSpecificationValidationIssue],
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

        if item_id in seen_ids:
            issues.append(
                PRSpecificationValidationIssue(
                    code="duplicate_detail_id",
                    message=(
                        f"Detail id {item_id!r} appears more than once across "
                        "the proposed PR specification."
                    ),
                    path=f"{section_name}[{index}].id",
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
    issues: list[PRSpecificationValidationIssue],
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
                PRSpecificationValidationIssue(
                    code="duplicate_feature_id",
                    message=f"Feature id {feature_id!r} appears more than once.",
                    path=f"feature_ids[{index}]",
                )
            )
            continue

        if feature_id not in available_feature_ids:
            issues.append(
                PRSpecificationValidationIssue(
                    code="unknown_feature_id",
                    message=(
                        f"Feature id {feature_id!r} is not listed in the current "
                        "source specifications."
                    ),
                    path=f"feature_ids[{index}]",
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
    issues: list[PRSpecificationValidationIssue],
    issue_code: str,
    issue_message: str,
) -> Sequence[object]:
    if raw_value is None:
        return ()
    if isinstance(raw_value, Sequence) and not isinstance(raw_value, (str, bytes)):
        return raw_value
    issues.append(
        PRSpecificationValidationIssue(
            code=issue_code,
            message=issue_message,
            path=path,
        )
    )
    return ()


def _coerce_mapping(
    raw_value: object,
    *,
    path: str,
    issues: list[PRSpecificationValidationIssue],
    issue_code: str,
    issue_message: str,
) -> Mapping[str, Any] | None:
    if isinstance(raw_value, Mapping):
        return cast(Mapping[str, Any], raw_value)

    issues.append(
        PRSpecificationValidationIssue(
            code=issue_code,
            message=issue_message,
            path=path,
        )
    )
    return None


def _required_string(
    raw_value: object,
    *,
    path: str,
    issues: list[PRSpecificationValidationIssue],
    issue_code: str,
    issue_message: str,
) -> str | None:
    if raw_value is None:
        issues.append(
            PRSpecificationValidationIssue(
                code=issue_code,
                message=issue_message,
                path=path,
            )
        )
        return None

    value = str(raw_value).strip()
    if value == "":
        issues.append(
            PRSpecificationValidationIssue(
                code=issue_code,
                message=issue_message,
                path=path,
            )
        )
        return None

    return value


def _optional_string(raw_value: object) -> str | None:
    if raw_value is None:
        return None

    value = str(raw_value).strip()
    return value or None


def _resolve_output_path(
    repo_root: Path,
    work_item_name: str,
    output_path: str | Path | None,
) -> Path:
    if output_path is None:
        return pr_specification_default_output_path(work_item_name, repo_root)

    resolved_output_path = Path(output_path)
    if not resolved_output_path.is_absolute():
        resolved_output_path = repo_root / resolved_output_path
    return resolved_output_path


def _report_to_data(
    report: PRSpecificationValidationReport,
    *,
    file_path: str | Path | None = None,
) -> Mapping[str, Any]:
    return {
        "validation_successful": report.validation_successful,
        "issues": [
            validation_error_to_data(issue, file_path=str(file_path))
            for issue in report.issues
        ],
        "proposed_pr_id": report.proposed_pr_id,
        "available_feature_ids": report.available_feature_ids,
        "known_pr_ids": report.known_pr_ids,
    }


def _proposed_pr_search_report_to_data(
    report: ProposedPRSearchReport,
) -> Mapping[str, Any]:
    return {
        "query": report.query,
        "results": [
            {
                "pr_number": result.pr_number,
                "proposed_pr_id": result.proposed_pr_id,
                "path": str(result.path),
                "score": result.score,
                "matched_fields": list(result.matched_fields),
                "feature_ids": list(result.feature_ids),
                "intent_goal": result.intent_goal,
                "intent_reasoning": result.intent_reasoning,
            }
            for result in report.results
        ],
    }
