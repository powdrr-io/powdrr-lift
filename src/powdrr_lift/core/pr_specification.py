from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import yaml

from powdrr_lift.change_log_template import _resolve_repo_root
from powdrr_lift.core.spec_paths import (
    SPECIFICATION_SCHEMA_URL,
    proposed_pr_specification_path,
)


@dataclass(frozen=True, slots=True)
class PRSpecificationValidationIssue:
    code: str
    message: str
    path: str | None = None


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
    proposed_pr_id: str
    work_item_name: str
    title: str | None
    feature_ids: tuple[str, ...]
    source_path: str
    score: int


@dataclass(frozen=True, slots=True)
class ProposedPRSearchReport:
    query: str
    results: list[ProposedPRSearchResult] = field(default_factory=list)


def pr_specification_default_output_path(
    work_item_name: str,
    repo_root: str | Path | None = None,
) -> Path:
    repo_root_path = _resolve_repo_root(repo_root)
    return proposed_pr_specification_path(repo_root_path, work_item_name)


def render_pr_specification_template(
    *,
    work_item_name: str,
    repo_root: str | Path | None = None,
) -> str:
    repo_root_path = _resolve_repo_root(repo_root)
    feature_catalog = _load_feature_catalog(repo_root_path)
    if not feature_catalog:
        raise ValueError(
            "No current feature ids were found in the codebase state. "
            "Generate the codebase state first and ensure it contains current "
            "entities before generating a PR specification."
        )
    normalized_work_item_name = work_item_name.strip()
    if not normalized_work_item_name:
        raise ValueError("work_item_name must not be empty.")

    lines = [
        "# PR specification template.",
        "#",
        "# Instructions:",
        f"# - Use the work item folder `docs/specs/{normalized_work_item_name}`.",
        "# - Create one template per proposed PR.",
        "# - Set `id` to a globally unique proposed PR id.",
        "# - Reference one or more current feature ids from the codebase state",
        "#   listed below.",
        "# - Fill in `intent.goal` and `intent.reasoning`.",
        "# - Delete these instructions when you are done.",
        "# - Add acceptance criteria, expected tests, expected outcomes,",
        "#   non-goals, and risks as concrete lists with `id` and",
        "#   `description`.",
        "# - Keep every detail id globally unique across those five sections.",
        "#",
        f"schema: {SPECIFICATION_SCHEMA_URL}",
        "# Current feature ids:",
        *[
            (
                f"# - {entry.feature_id} "
                f"({entry.entity_type or 'entity'}, {entry.source_path})"
            )
            for entry in feature_catalog
        ],
        "id: null",
        "feature_ids:",
        "  - null",
        "intent:",
        "  goal: null",
        "  reasoning: null",
        "acceptance_criteria:",
        "  - id: null",
        "    description: null",
        "expected_tests:",
        "  - id: null",
        "    description: null",
        "expected_outcomes:",
        "  - id: null",
        "    description: null",
        "non_goals:",
        "  - id: null",
        "    description: null",
        "risks:",
        "  - id: null",
        "    description: null",
        "",
    ]
    return "\n".join(lines)


def create_pr_specification_template(
    *,
    work_item_name: str,
    output_path: str | Path | None = None,
    repo_root: str | Path | None = None,
) -> Path:
    repo_root_path = _resolve_repo_root(repo_root)
    resolved_output_path = _resolve_output_path(
        repo_root_path,
        work_item_name=work_item_name,
        output_path=output_path,
    )
    resolved_output_path.parent.mkdir(parents=True, exist_ok=True)
    resolved_output_path.write_text(
        render_pr_specification_template(
            work_item_name=work_item_name,
            repo_root=repo_root_path,
        ),
        encoding="utf-8",
    )
    return resolved_output_path


def validate_pr_specification_yaml(
    proposed_pr_specification_yaml: str,
    *,
    work_item_name: str,
    repo_root: str | Path | None = None,
    specification_path: str | Path | None = None,
) -> str:
    report = build_pr_specification_validation_report(
        proposed_pr_specification_yaml,
        work_item_name=work_item_name,
        repo_root=repo_root,
        specification_path=specification_path,
    )
    return yaml.safe_dump(_report_to_data(report), sort_keys=False)


def build_pr_specification_validation_report(
    proposed_pr_specification_yaml: str,
    *,
    work_item_name: str,
    repo_root: str | Path | None = None,
    specification_path: str | Path | None = None,
) -> PRSpecificationValidationReport:
    repo_root_path = _resolve_repo_root(repo_root)
    issues: list[PRSpecificationValidationIssue] = []

    feature_catalog = _load_feature_catalog(repo_root_path)
    available_feature_ids = [entry.feature_id for entry in feature_catalog]
    current_specification_path = (
        Path(specification_path)
        if specification_path is not None
        else pr_specification_default_output_path(work_item_name, repo_root_path)
    )
    known_pr_ids = _load_existing_pr_ids(
        repo_root_path,
        exclude_paths={current_specification_path.resolve()},
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

    for section_name in (
        "id",
        "feature_ids",
        "intent",
        "acceptance_criteria",
        "expected_tests",
        "expected_outcomes",
        "non_goals",
        "risks",
    ):
        if section_name not in raw_spec:
            issues.append(
                PRSpecificationValidationIssue(
                    code="missing_required_section",
                    message=f"The {section_name} section is required.",
                    path=section_name,
                )
            )

    seen_detail_ids: set[str] = set()

    proposed_pr_id = _required_string(
        raw_spec.get("id"),
        path="id",
        issues=issues,
        issue_code="proposed_pr_id_missing",
        issue_message="The id field is required.",
    )
    if proposed_pr_id is not None and proposed_pr_id in known_pr_ids:
        issues.append(
            PRSpecificationValidationIssue(
                code="duplicate_proposed_pr_id",
                message=f"Proposed PR id {proposed_pr_id!r} already exists.",
                path="id",
            )
        )
    if proposed_pr_id is not None:
        seen_detail_ids.add(proposed_pr_id)

    feature_ids = _collect_feature_ids(
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
    if not feature_ids:
        issues.append(
            PRSpecificationValidationIssue(
                code="no_feature_ids_defined",
                message="Reference at least one feature id.",
                path="feature_ids",
            )
        )

    intent = _coerce_mapping(
        raw_spec.get("intent"),
        path="intent",
        issues=issues,
        issue_code="invalid_intent_section",
        issue_message="intent must be a mapping with goal and reasoning.",
    )
    if intent is not None:
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


def show_proposed_pr_specification(
    proposed_pr_id: str,
    *,
    repo_root: str | Path | None = None,
) -> str:
    repo_root_path = _resolve_repo_root(repo_root)
    specification_path = _find_proposed_pr_specification_path(
        repo_root_path,
        proposed_pr_id,
    )
    if specification_path is None:
        raise FileNotFoundError(
            f"Could not find a proposed PR specification for id {proposed_pr_id!r}."
        )

    return specification_path.read_text(encoding="utf-8")


def search_proposed_pr_specifications(
    query: str,
    *,
    repo_root: str | Path | None = None,
    limit: int = 5,
) -> ProposedPRSearchReport:
    repo_root_path = _resolve_repo_root(repo_root)
    normalized_query = query.strip().lower()
    if not normalized_query:
        raise ValueError("query must not be empty.")

    results: list[ProposedPRSearchResult] = []
    for specification_path in _iter_proposed_pr_specification_paths(repo_root_path):
        try:
            raw_spec = _load_yaml_mapping(
                specification_path.read_text(encoding="utf-8")
            )
        except Exception:  # noqa: BLE001
            continue

        proposed_pr_id = _optional_string(raw_spec.get("id"))
        if proposed_pr_id is None:
            continue

        title = _optional_string(raw_spec.get("title"))
        feature_ids = tuple(
            feature_id
            for feature_id in (
                _optional_string(raw_feature_id)
                for raw_feature_id in _coerce_sequence(
                    raw_spec.get("feature_ids"),
                    path="feature_ids",
                    issues=[],
                    issue_code="invalid_feature_ids_section",
                    issue_message="",
                )
            )
            if feature_id is not None
        )
        haystack = " ".join(
            [
                proposed_pr_id,
                title or "",
                " ".join(feature_ids),
                specification_path.stem,
                specification_path.parent.name,
                specification_path.as_posix(),
            ]
        ).lower()
        if normalized_query not in haystack:
            continue

        score = haystack.count(normalized_query)
        results.append(
            ProposedPRSearchResult(
                proposed_pr_id=proposed_pr_id,
                work_item_name=specification_path.parent.name,
                title=title,
                feature_ids=feature_ids,
                source_path=str(specification_path.relative_to(repo_root_path)),
                score=score,
            )
        )

    results.sort(key=lambda result: (-result.score, result.proposed_pr_id))
    return ProposedPRSearchReport(query=query, results=results[:limit])


def render_proposed_pr_search_report(report: ProposedPRSearchReport) -> str:
    return yaml.safe_dump(
        _proposed_pr_search_report_to_data(report),
        sort_keys=False,
        allow_unicode=False,
    )


def _load_feature_catalog(repo_root: Path) -> list[_FeatureCatalogEntry]:
    catalog: list[_FeatureCatalogEntry] = []
    seen_feature_ids: set[str] = set()
    for specification_path in _iter_implementation_specification_paths(repo_root):
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
    exclude_paths: set[Path] | None = None,
) -> list[str]:
    pr_ids: list[str] = []
    seen_ids: set[str] = set()
    normalized_exclude_paths = {
        path.resolve() for path in (exclude_paths or set()) if path is not None
    }
    for specification_path in _iter_proposed_pr_specification_paths(repo_root):
        if specification_path.resolve() in normalized_exclude_paths:
            continue

        try:
            raw_spec = _load_yaml_mapping(
                specification_path.read_text(encoding="utf-8")
            )
        except Exception:  # noqa: BLE001
            continue

        proposed_pr_id = _optional_string(raw_spec.get("id"))
        if proposed_pr_id is None or proposed_pr_id in seen_ids:
            continue

        seen_ids.add(proposed_pr_id)
        pr_ids.append(proposed_pr_id)

    return pr_ids


def _iter_implementation_specification_paths(repo_root: Path) -> list[Path]:
    spec_root = repo_root / "docs" / "specs"
    if not spec_root.exists():
        return []

    return sorted(
        specification_path
        for specification_path in spec_root.rglob("implementation-specification.yaml")
        if specification_path.is_file()
    )


def _iter_proposed_pr_specification_paths(repo_root: Path) -> list[Path]:
    spec_root = repo_root / "docs" / "specs"
    if not spec_root.exists():
        return []

    candidate_paths = list(
        sorted(
            specification_path
            for specification_path in spec_root.rglob("proposed-pr-specification.yaml")
            if specification_path.is_file()
        )
    )
    proposal_root = repo_root / "docs" / "proposals"
    if proposal_root.exists():
        candidate_paths.extend(
            sorted(
                specification_path
                for specification_path in proposal_root.glob(
                    "PR-*-proposed-pr-specification.yaml"
                )
                if specification_path.is_file()
            )
        )

    seen_paths: set[Path] = set()
    ordered_paths: list[Path] = []
    for specification_path in candidate_paths:
        if specification_path in seen_paths:
            continue

        seen_paths.add(specification_path)
        ordered_paths.append(specification_path)

    return ordered_paths


def _find_proposed_pr_specification_path(
    repo_root: Path,
    proposed_pr_id: str,
) -> Path | None:
    normalized_proposed_pr_id = _optional_string(proposed_pr_id)
    if normalized_proposed_pr_id is None:
        return None

    for specification_path in _iter_proposed_pr_specification_paths(repo_root):
        try:
            raw_spec = _load_yaml_mapping(
                specification_path.read_text(encoding="utf-8")
            )
        except Exception:  # noqa: BLE001
            continue

        current_id = _optional_string(raw_spec.get("id"))
        if current_id == normalized_proposed_pr_id:
            return specification_path

    return None


def _validate_template_boilerplate_removed(
    proposed_pr_specification_yaml: str,
    *,
    issues: list[PRSpecificationValidationIssue],
) -> None:
    boilerplate_markers = (
        "# PR specification template.",
        "# Current feature ids:",
        "Delete these instructions when you are done.",
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


def _collect_detail_items(
    raw_items: Sequence[object],
    *,
    section_name: str,
    seen_ids: set[str],
    issues: list[PRSpecificationValidationIssue],
) -> set[str]:
    item_ids: set[str] = set()
    saw_any_item = False
    for index, raw_item in enumerate(raw_items):
        saw_any_item = True
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

    if not saw_any_item:
        issues.append(
            PRSpecificationValidationIssue(
                code=f"no_{section_name}_defined",
                message=(f"Add at least one {section_name.replace('_', ' ')} item."),
                path=section_name,
            )
        )

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
                        "implementation specifications."
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
    *,
    work_item_name: str,
    output_path: str | Path | None,
) -> Path:
    if output_path is None:
        return proposed_pr_specification_path(repo_root, work_item_name)

    resolved_output_path = Path(output_path)
    if not resolved_output_path.is_absolute():
        resolved_output_path = repo_root / resolved_output_path
    return resolved_output_path


def _report_to_data(
    report: PRSpecificationValidationReport,
) -> Mapping[str, Any]:
    return {
        "validation_successful": report.validation_successful,
        "proposed_pr_id": report.proposed_pr_id,
        "available_feature_ids": report.available_feature_ids,
        "known_pr_ids": report.known_pr_ids,
        "issues": [
            {
                "code": issue.code,
                "message": issue.message,
                **({"path": issue.path} if issue.path is not None else {}),
            }
            for issue in report.issues
        ],
    }


def _proposed_pr_search_report_to_data(
    report: ProposedPRSearchReport,
) -> Mapping[str, Any]:
    return {
        "query": report.query,
        "results": [
            {
                "proposed_pr_id": result.proposed_pr_id,
                "work_item_name": result.work_item_name,
                "title": result.title,
                "feature_ids": list(result.feature_ids),
                "source_path": result.source_path,
                "score": result.score,
            }
            for result in report.results
        ],
    }
