from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, cast

import yaml

from powdrr_lift.change_log_template import _resolve_repo_root
from powdrr_lift.core.codebase_state import build_codebase_state_report

_DEFAULT_OUTPUT_PATH = Path("docs") / "specs"
_IMPLEMENTATION_SPECIFICATION_DIR = Path("docs") / "specs"


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
    pr_number: int
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
    pr_number: int
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


def render_pr_specification_template(*, repo_root: str | Path | None = None) -> str:
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

    lines = [
        "# PR specification template.",
        "#",
        "# Instructions:",
        "# - Create one template per proposed PR.",
        "# - Set `id` to a globally unique proposed PR id.",
        "# - Reference one or more current feature ids from the codebase state",
        "#   listed below.",
        "# - Fill in `intent.problem`, `intent.goal`, and `intent.reasoning`.",
        "# - Delete these instructions and replace with a comment saying that",
        "#   this file is read-only and should never be editted by a tool or",
        "#   agent.",
        "# - Add acceptance criteria, expected tests, expected outcomes,",
        "#   required test cases, non-goals, and risks as concrete lists with `id` and",
        "#   `description`.",
        "# - Keep every detail id globally unique across those six sections.",
        "#",
        "# Current feature ids:",
        *feature_lines,
        "schema: https://powdrr.io/schemas/specification-v1",
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
        "required_test_cases:",
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
        work_item_name,
        output_path,
    )
    resolved_output_path.parent.mkdir(parents=True, exist_ok=True)
    resolved_output_path.write_text(
        render_pr_specification_template(repo_root=repo_root_path),
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
        key=lambda result: (-result.score, result.pr_number, result.path.name),
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
) -> str:
    report = build_pr_specification_validation_report(
        proposed_pr_specification_yaml,
        work_item_name=work_item_name,
        repo_root=repo_root,
    )
    return yaml.safe_dump(_report_to_data(report), sort_keys=False)


def build_pr_specification_validation_report(
    proposed_pr_specification_yaml: str,
    *,
    work_item_name: str,
    repo_root: str | Path | None = None,
) -> PRSpecificationValidationReport:
    repo_root_path = _resolve_repo_root(repo_root)
    issues: list[PRSpecificationValidationIssue] = []
    current_proposed_pr_id = _normalize_identifier(work_item_name)

    feature_catalog = _load_feature_catalog(repo_root_path)
    available_feature_ids = [entry.feature_id for entry in feature_catalog]
    known_pr_ids = _load_existing_pr_ids(
        repo_root_path,
        excluded_proposed_pr_id=current_proposed_pr_id,
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
        "required_test_cases",
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
        issue_message="intent must be a mapping with problem, goal, and reasoning.",
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
        if catalog:
            return catalog

    implementation_dir = repo_root / _IMPLEMENTATION_SPECIFICATION_DIR
    if not implementation_dir.exists():
        return catalog

    for specification_path in sorted(
        implementation_dir.rglob("implementation-specification.yaml")
    ):
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
    excluded_proposed_pr_id: str | None = None,
) -> list[str]:
    pr_ids: list[str] = []
    seen_ids: set[str] = set()
    excluded_identifier = (
        _normalize_identifier(excluded_proposed_pr_id)
        if excluded_proposed_pr_id is not None
        else None
    )
    for specification_path in _iter_proposed_pr_specification_paths(repo_root):
        try:
            raw_spec = _load_yaml_mapping(
                specification_path.read_text(encoding="utf-8")
            )
        except Exception:  # noqa: BLE001
            continue

        proposed_pr_id = _optional_string(raw_spec.get("id"))
        if proposed_pr_id is None:
            continue

        normalized_proposed_pr_id = _normalize_identifier(proposed_pr_id)
        if (
            excluded_identifier is not None
            and normalized_proposed_pr_id == excluded_identifier
        ):
            continue

        if normalized_proposed_pr_id in seen_ids:
            continue

        seen_ids.add(normalized_proposed_pr_id)
        pr_ids.append(proposed_pr_id)

    return pr_ids


def _load_proposed_pr_documents(repo_root: Path) -> list[_ProposedPRDocument]:
    documents: list[_ProposedPRDocument] = []
    for specification_path in _iter_proposed_pr_specification_paths(repo_root):
        pr_number = _parse_proposed_pr_number(specification_path)
        if pr_number is None:
            continue

        try:
            raw_spec = _load_yaml_mapping(
                specification_path.read_text(encoding="utf-8")
            )
        except Exception:  # noqa: BLE001
            continue

        proposed_pr_id = _optional_string(raw_spec.get("id"))
        feature_ids = tuple(
            feature_id
            for feature_id in (
                _optional_string(raw_feature_id)
                for raw_feature_id in _coerce_sequence(
                    raw_spec.get("feature_ids"),
                    path=f"{specification_path}#feature_ids",
                    issues=[],
                    issue_code="invalid_feature_ids_section",
                    issue_message="",
                )
            )
            if feature_id is not None
        )
        intent = _coerce_mapping(
            raw_spec.get("intent"),
            path=f"{specification_path}#intent",
            issues=[],
            issue_code="invalid_intent_section",
            issue_message="",
        )
        intent_goal = _optional_string(intent.get("goal")) if intent else None
        intent_reasoning = _optional_string(intent.get("reasoning")) if intent else None
        documents.append(
            _ProposedPRDocument(
                pr_number=pr_number,
                path=specification_path,
                data=raw_spec,
                proposed_pr_id=proposed_pr_id,
                feature_ids=feature_ids,
                intent_goal=intent_goal,
                intent_reasoning=intent_reasoning,
            )
        )

    return documents


def _iter_proposed_pr_specification_paths(repo_root: Path) -> list[Path]:
    spec_root = repo_root / "docs" / "specs"
    if not spec_root.exists():
        specification_paths = []
    else:
        specification_paths = [
            specification_path
            for specification_path in spec_root.rglob("proposed-pr-specification.yaml")
            if specification_path.is_file()
        ]

    proposal_root = repo_root / "docs" / "proposals"
    if proposal_root.exists():
        specification_paths.extend(
            specification_path
            for specification_path in proposal_root.glob(
                "PR-*-proposed-pr-specification.yaml"
            )
            if specification_path.is_file()
        )

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
        "# - Delete these instructions and replace with a comment saying that",
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
