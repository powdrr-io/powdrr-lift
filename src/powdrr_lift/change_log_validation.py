from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import yaml

from powdrr_lift.change_log_parser import (
    ChangeEntity,
    ChangeFile,
    ChangeLog,
    RelatedSection,
    parse_change_log,
)
from powdrr_lift.change_log_template import (
    BranchDiffEntry,
    _collect_branch_diff_entries,
    _is_structured_document_path,
    _resolve_default_branch,
    _resolve_repo_root,
)
from powdrr_lift.core.codebase_state import build_codebase_state_report
from powdrr_lift.core.entity_taxonomy import load_entity_taxonomy
from powdrr_lift.core.index import (
    _file_change_entity_ids,
    _normalize_entity_id,
    build_changelog_index_at_ref,
)


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    code: str
    message: str
    path: str | None = None


@dataclass(frozen=True, slots=True)
class ValidationReport:
    validation_successful: bool
    branch_name: str
    default_branch_name: str
    expected_change_files: list[str] = field(default_factory=list)
    proposed_change_files: list[str] = field(default_factory=list)
    issues: list[ValidationIssue] = field(default_factory=list)


def validate_change_log_yaml(
    proposed_change_log_yaml: str,
    branch_name: str,
    repo_root: str | Path | None = None,
    default_branch: str | None = None,
) -> str:
    report = build_validation_report(
        proposed_change_log_yaml=proposed_change_log_yaml,
        branch_name=branch_name,
        repo_root=repo_root,
        default_branch=default_branch,
    )
    return yaml.safe_dump(_report_to_data(report), sort_keys=False)


def build_validation_report(
    proposed_change_log_yaml: str,
    branch_name: str,
    repo_root: str | Path | None = None,
    default_branch: str | None = None,
) -> ValidationReport:
    repo_root_path = _resolve_repo_root(repo_root)
    default_branch_name = default_branch or _resolve_default_branch(repo_root_path)
    diff_entries = _collect_branch_diff_entries(
        repo_root_path,
        default_branch_name,
        branch_name,
    )
    expected_change_files = [
        entry.path
        for entry in diff_entries
        if not _is_changelog_artifact_path(entry.path)
    ]
    expected_change_files = list(dict.fromkeys(expected_change_files))

    issues: list[ValidationIssue] = []
    proposed_change_files: list[str] = []

    if _contains_instruction_comments(proposed_change_log_yaml):
        issues.append(
            ValidationIssue(
                code="instructions_not_removed",
                message=(
                    "The changelog file still contains instruction comments. "
                    "Remove them before validating the final artifact."
                ),
            )
        )
        return ValidationReport(
            validation_successful=False,
            branch_name=branch_name,
            default_branch_name=default_branch_name,
            expected_change_files=expected_change_files,
            proposed_change_files=proposed_change_files,
            issues=issues,
        )

    if _contains_null_entity_actions(proposed_change_log_yaml):
        issues.append(
            ValidationIssue(
                code="entity_action_null_not_allowed",
                message=(
                    "Entity entries must omit action when the entity is not new. "
                    "Do not write action: null."
                ),
            )
        )
        return ValidationReport(
            validation_successful=False,
            branch_name=branch_name,
            default_branch_name=default_branch_name,
            expected_change_files=expected_change_files,
            proposed_change_files=proposed_change_files,
            issues=issues,
        )

    try:
        raw_change_log = _load_yaml_mapping(proposed_change_log_yaml)
    except Exception as exc:  # noqa: BLE001
        issues.append(
            ValidationIssue(
                code="invalid_yaml",
                message=f"Could not parse proposed change log YAML: {exc}",
            )
        )
        return ValidationReport(
            validation_successful=False,
            branch_name=branch_name,
            default_branch_name=default_branch_name,
            expected_change_files=expected_change_files,
            proposed_change_files=proposed_change_files,
            issues=issues,
        )

    version = _normalize_version(raw_change_log.get("version"))
    if version is None:
        version = _infer_version_from_schema(raw_change_log.get("schema"))

    if version == 2:
        if "changes" in raw_change_log:
            issues.append(
                ValidationIssue(
                    code="top_level_changes_not_allowed",
                    message=(
                        "Version 2 changelogs must use top-level structured_files, "
                        "files, entities, entity_relationships, invariants, "
                        "guidance, features, and prs sections."
                    ),
                )
            )
        if "relationship_changes" in raw_change_log:
            issues.append(
                ValidationIssue(
                    code="top_level_relationship_changes_not_allowed",
                    message=(
                        "Version 2 changelogs must move relationship changes "
                        "to the entity_relationships section."
                    ),
                )
            )
        _validate_v2_file_sections(raw_change_log, issues)
        if issues:
            return ValidationReport(
                validation_successful=False,
                branch_name=branch_name,
                default_branch_name=default_branch_name,
                expected_change_files=expected_change_files,
                proposed_change_files=proposed_change_files,
                issues=issues,
            )

    try:
        change_log = parse_change_log(proposed_change_log_yaml)
    except Exception as exc:  # noqa: BLE001
        issues.append(
            ValidationIssue(
                code="invalid_yaml",
                message=f"Could not parse proposed change log YAML: {exc}",
            )
        )
        return ValidationReport(
            validation_successful=False,
            branch_name=branch_name,
            default_branch_name=default_branch_name,
            expected_change_files=expected_change_files,
            proposed_change_files=proposed_change_files,
            issues=issues,
        )

    proposed_change_files = [
        file_change.path
        for file_change in change_log.file_changes
        if file_change.path is not None and file_change.path != ""
    ]
    proposed_change_files.extend(
        structured_file
        for structured_file in change_log.structured_files
        if structured_file is not None and structured_file != ""
    )
    proposed_change_files = list(dict.fromkeys(proposed_change_files))
    if version == 1:
        proposed_entities = [
            _parse_validation_entity(entity_data)
            for entity_data in _parse_sequence(raw_change_log.get("entities"))
        ]
    else:
        proposed_entities = list(change_log.entity_changes or [])

    proposed_entities_by_id = {
        entity_id: entity
        for entity in proposed_entities
        if (entity_id := _normalize_entity_id(entity.id)) is not None
    }
    _validate_unique_changelog_ids(change_log, issues)
    added_entities = [
        entity for entity in proposed_entities if entity.action == "added"
    ]
    allowed_entity_types: set[str] | None = None
    if added_entities:
        try:
            allowed_entity_types = set(
                load_entity_taxonomy(repo_root_path).entity_types
            )
        except FileNotFoundError:
            issues.append(
                ValidationIssue(
                    code="entity_taxonomy_missing",
                    message=(
                        "Entity validation requires "
                        "software_development_entity_taxonomy.md at the repo root."
                    ),
                )
            )
            return ValidationReport(
                validation_successful=False,
                branch_name=branch_name,
                default_branch_name=default_branch_name,
                expected_change_files=expected_change_files,
                proposed_change_files=proposed_change_files,
                issues=issues,
            )
        except Exception as exc:  # noqa: BLE001
            issues.append(
                ValidationIssue(
                    code="entity_taxonomy_invalid",
                    message=(
                        "Could not parse software_development_entity_taxonomy.md: "
                        f"{exc}"
                    ),
                )
            )
            return ValidationReport(
                validation_successful=False,
                branch_name=branch_name,
                default_branch_name=default_branch_name,
                expected_change_files=expected_change_files,
                proposed_change_files=proposed_change_files,
                issues=issues,
            )
    expected_change_entries_by_file: dict[str, list[BranchDiffEntry]] = {}
    expected_structured_file_paths: list[str] = []
    seen_expected_structured_file_paths: set[str] = set()
    for entry in diff_entries:
        if _is_changelog_artifact_path(entry.path):
            continue

        if _is_structured_document_path(entry.path):
            if entry.path in seen_expected_structured_file_paths:
                continue

            seen_expected_structured_file_paths.add(entry.path)
            expected_structured_file_paths.append(entry.path)
            continue

        expected_change_entries_by_file.setdefault(entry.path, []).append(entry)

    proposed_change_entries_by_file: dict[str, list[Any]] = {}
    for file_change in change_log.file_changes:
        if file_change.path is None or file_change.path == "":
            continue

        proposed_change_entries_by_file.setdefault(file_change.path, []).append(
            file_change
        )

    parent_entity_ids = set(
        build_changelog_index_at_ref(
            repo_root=repo_root_path,
            ref=default_branch_name,
        ).entity_graph.entities
    )
    proposed_entity_ids = set(proposed_entities_by_id)

    for entity_id, entity in proposed_entities_by_id.items():
        if entity.action == "added":
            if entity_id in parent_entity_ids:
                issues.append(
                    ValidationIssue(
                        code="entity_already_exists",
                        message=(
                            f"Entity {entity_id} is already present in the parent "
                            "branch graph and should not be marked added."
                        ),
                        path=None,
                    )
                )
            _validate_added_entity_type(
                issues=issues,
                entity=entity,
                entity_id=entity_id,
                allowed_entity_types=allowed_entity_types,
            )
            continue

        if entity_id not in parent_entity_ids:
            issues.append(
                ValidationIssue(
                    code="entity_missing_from_parent",
                    message=(
                        f"Entity {entity_id} does not exist in the parent branch "
                        "graph and must be marked added."
                    ),
                    path=None,
                )
            )

    if version == 2:
        proposed_structured_files = list(
            dict.fromkeys(
                structured_file
                for structured_file in change_log.structured_files
                if structured_file is not None and structured_file != ""
            )
        )
        proposed_structured_file_set = set(proposed_structured_files)
        proposed_structured_file_changes = [
            file_change
            for file_change in change_log.file_changes
            if _is_structured_document_path(file_change.path or "")
        ]
        proposed_regular_file_changes = [
            file_change
            for file_change in change_log.file_changes
            if not _is_structured_document_path(file_change.path or "")
        ]

        for file_change in proposed_structured_file_changes:
            issues.append(
                ValidationIssue(
                    code="structured_file_in_files_section",
                    message=(
                        "Structured document files must be listed in the "
                        "structured_files section without spans."
                    ),
                    path=file_change.path,
                )
            )

        for file_change_index, file_change in enumerate(
            proposed_regular_file_changes,
            start=1,
        ):
            _validate_v2_file_change(
                issues=issues,
                file_change=file_change,
                file_change_index=file_change_index,
            )

        for entity_change_index, entity_change in enumerate(
            change_log.entity_changes or [],
            start=1,
        ):
            _validate_v2_entity_change(
                issues=issues,
                entity_change=entity_change,
                entity_change_index=entity_change_index,
                allowed_entity_types=allowed_entity_types,
            )

        for invariant_change_index, invariant_change in enumerate(
            change_log.invariant_changes or [],
            start=1,
        ):
            _validate_v2_lifecycle_item(
                issues=issues,
                change_index=invariant_change_index,
                item_index=invariant_change_index,
                item_kind="invariant",
                item_id=invariant_change.id,
                description=invariant_change.description,
                action=invariant_change.action,
                related=invariant_change.related,
                available_files={
                    file_change.path
                    for file_change in change_log.file_changes
                    if file_change.path is not None and file_change.path != ""
                },
                available_entities={
                    _normalize_entity_id(entity.id)
                    for entity in (change_log.entity_changes or [])
                    if _normalize_entity_id(entity.id) is not None
                },
                available_invariants={
                    _normalize_entity_id(invariant.id)
                    for invariant in (change_log.invariant_changes or [])
                    if _normalize_entity_id(invariant.id) is not None
                },
                available_guidance={
                    _normalize_entity_id(guidance.id)
                    for guidance in (change_log.guidance_changes or [])
                    if _normalize_entity_id(guidance.id) is not None
                },
                path=None,
            )

        for guidance_change_index, guidance_change in enumerate(
            change_log.guidance_changes or [],
            start=1,
        ):
            _validate_v2_lifecycle_item(
                issues=issues,
                change_index=guidance_change_index,
                item_index=guidance_change_index,
                item_kind="guidance",
                item_id=guidance_change.id,
                description=guidance_change.description,
                action=guidance_change.action,
                related=guidance_change.related,
                available_files={
                    file_change.path
                    for file_change in change_log.file_changes
                    if file_change.path is not None and file_change.path != ""
                },
                available_entities={
                    _normalize_entity_id(entity.id)
                    for entity in (change_log.entity_changes or [])
                    if _normalize_entity_id(entity.id) is not None
                },
                available_invariants={
                    _normalize_entity_id(invariant.id)
                    for invariant in (change_log.invariant_changes or [])
                    if _normalize_entity_id(invariant.id) is not None
                },
                available_guidance={
                    _normalize_entity_id(guidance.id)
                    for guidance in (change_log.guidance_changes or [])
                    if _normalize_entity_id(guidance.id) is not None
                },
                path=None,
            )

        available_feature_ids = _load_available_feature_ids(repo_root_path)
        known_pr_ids = _load_known_proposed_pr_ids(repo_root_path)

        for feature_change_index, feature_change in enumerate(
            change_log.feature_changes or [],
            start=1,
        ):
            _validate_v2_state_change(
                issues=issues,
                section_name="features",
                item_index=feature_change_index,
                item_id=feature_change.id,
                state=feature_change.state,
                available_ids=available_feature_ids,
                unavailable_id_code="unknown_feature_id",
                unavailable_id_message=(
                    "Feature id {item_id!r} is not listed in the current codebase "
                    "state."
                ),
                invalid_state_code="invalid_feature_state",
                invalid_state_message=(
                    "Feature state must be in_progress or completed."
                ),
            )

        for pr_change_index, pr_change in enumerate(
            change_log.pr_changes or [], start=1
        ):
            _validate_v2_state_change(
                issues=issues,
                section_name="prs",
                item_index=pr_change_index,
                item_id=pr_change.id,
                state=pr_change.state,
                available_ids=known_pr_ids,
                unavailable_id_code="unknown_proposed_pr_id",
                unavailable_id_message=(
                    "Proposed PR id {item_id!r} is not listed in the current "
                    "changelog index."
                ),
                invalid_state_code="invalid_pr_state",
                invalid_state_message=(
                    "Proposed PR state must be in_progress or completed."
                ),
            )

        for structured_file_path in expected_structured_file_paths:
            if structured_file_path not in proposed_structured_file_set:
                issues.append(
                    ValidationIssue(
                        code="missing_structured_change",
                        message=(
                            f"Missing structured file entry for {structured_file_path}"
                        ),
                        path=structured_file_path,
                    )
                )

        for structured_file_path in proposed_structured_files:
            if structured_file_path in expected_structured_file_paths:
                _validate_v2_structured_file_contents(
                    issues=issues,
                    repo_root=repo_root_path,
                    structured_file_path=structured_file_path,
                )
                continue

            issues.append(
                ValidationIssue(
                    code="unexpected_structured_change",
                    message=(
                        "Structured file entry for "
                        f"{structured_file_path} does not appear in the branch diff"
                    ),
                    path=structured_file_path,
                )
            )

        for relationship_change in change_log.entity_relationship_changes or []:
            source = _normalize_entity_id(relationship_change.source)
            target = _normalize_entity_id(relationship_change.target)
            if source is None or target is None:
                issues.append(
                    ValidationIssue(
                        code="relationship_unknown_entity",
                        message=(
                            "Relationship references must name both source and "
                            "target entities."
                        ),
                    )
                )
                continue

            missing_entities = [
                name for name in (source, target) if name not in proposed_entity_ids
            ]
            for entity_id in missing_entities:
                issues.append(
                    ValidationIssue(
                        code="relationship_unknown_entity",
                        message=(
                            f"Relationship references entity {entity_id}, but "
                            "that entity is not described in the entity changes "
                            "section."
                        ),
                    )
                )
    else:
        for relationship_change in change_log.entity_relationship_changes or []:
            source = _normalize_entity_id(relationship_change.source)
            target = _normalize_entity_id(relationship_change.target)
            if source is None or target is None:
                issues.append(
                    ValidationIssue(
                        code="relationship_unknown_entity",
                        message=(
                            "Relationship references must name both source and "
                            "target entities."
                        ),
                    )
                )
                continue

            missing_entities = [
                name for name in (source, target) if name not in proposed_entity_ids
            ]
            for entity_id in missing_entities:
                issues.append(
                    ValidationIssue(
                        code="relationship_unknown_entity",
                        message=(
                            f"Relationship references entity {entity_id}, but "
                            "that entity is not described in the file entities "
                            "section."
                        ),
                    )
                )

    expected_regular_change_entries_by_file = expected_change_entries_by_file
    proposed_regular_change_entries_by_file = {
        file_path: entries
        for file_path, entries in proposed_change_entries_by_file.items()
        if not _is_structured_document_path(file_path)
    }

    for file_path, expected_entries in expected_regular_change_entries_by_file.items():
        proposed_entries = proposed_regular_change_entries_by_file.get(file_path, [])
        expected_spans = Counter(
            (entry.start_line, entry.end_line) for entry in expected_entries
        )
        proposed_spans: Counter[tuple[int, int]] = Counter()
        for change_index, proposed_entry in enumerate(proposed_entries, start=1):
            if (
                proposed_entry.span.start_line is None
                or proposed_entry.span.end_line is None
                or proposed_entry.span.start_line > proposed_entry.span.end_line
            ):
                issues.append(
                    ValidationIssue(
                        code="span_mismatch",
                        message=(
                            f"Span for {file_path} change {change_index} is invalid: "
                            f"{proposed_entry.span.start_line}-"
                            f"{proposed_entry.span.end_line}"
                        ),
                        path=file_path,
                    )
                )
                continue

            proposed_spans[
                (proposed_entry.span.start_line, proposed_entry.span.end_line)
            ] += 1

        for span, count in expected_spans.items():
            difference = count - proposed_spans.get(span, 0)
            for _ in range(max(0, difference)):
                issues.append(
                    ValidationIssue(
                        code="missing_change",
                        message=(
                            f"Missing change entry for {file_path} "
                            f"({span[0]}-{span[1]})"
                        ),
                        path=file_path,
                    )
                )

        for span, count in proposed_spans.items():
            difference = count - expected_spans.get(span, 0)
            for _ in range(max(0, difference)):
                issues.append(
                    ValidationIssue(
                        code="unexpected_change",
                        message=(
                            f"Unexpected change entry for {file_path} "
                            f"({span[0]}-{span[1]}) does not appear in the branch diff"
                        ),
                        path=file_path,
                    )
                )

    for file_path in proposed_regular_change_entries_by_file:
        if file_path in expected_regular_change_entries_by_file:
            continue

        issues.append(
            ValidationIssue(
                code="unexpected_change",
                message=(
                    f"Change entry for {file_path} does not appear in the branch diff"
                ),
                path=file_path,
            )
        )

    return ValidationReport(
        validation_successful=not issues,
        branch_name=branch_name,
        default_branch_name=default_branch_name,
        expected_change_files=expected_change_files,
        proposed_change_files=proposed_change_files,
        issues=issues,
    )


def parse_validation_report(yaml_content: str) -> ValidationReport:
    loaded_content = yaml.safe_load(yaml_content)
    if loaded_content is None:
        return ValidationReport(
            validation_successful=False,
            branch_name="",
            default_branch_name="",
        )

    if not isinstance(loaded_content, Mapping):
        raise ValueError("Validation report YAML must decode to a mapping.")

    data = dict(loaded_content)
    return ValidationReport(
        validation_successful=bool(data.get("validation_successful")),
        branch_name=str(data.get("branch_name", "")),
        default_branch_name=str(data.get("default_branch_name", "")),
        expected_change_files=[
            str(change_file)
            for change_file in _parse_sequence(data.get("expected_change_files"))
        ],
        proposed_change_files=[
            str(change_file)
            for change_file in _parse_sequence(data.get("proposed_change_files"))
        ],
        issues=[
            _parse_validation_issue(issue_data)
            for issue_data in _parse_sequence(data.get("issues"))
        ],
    )


def _parse_validation_issue(raw_issue: object) -> ValidationIssue:
    data = _parse_mapping(raw_issue)
    return ValidationIssue(
        code=str(data.get("code", "")),
        message=str(data.get("message", "")),
        path=None if data.get("path") is None else str(data.get("path")),
    )


def _parse_validation_entity(raw_entity: object) -> ChangeEntity:
    data = _parse_mapping(raw_entity)
    return ChangeEntity(
        id=None if data.get("id") is None else str(data.get("id")),
        type=None if data.get("type") is None else str(data.get("type")),
        action=None if data.get("action") is None else str(data.get("action")),
    )


def _report_to_data(report: ValidationReport) -> dict[str, Any]:
    return {
        "validation_successful": report.validation_successful,
        "branch_name": report.branch_name,
        "default_branch_name": report.default_branch_name,
        "expected_change_files": report.expected_change_files,
        "proposed_change_files": report.proposed_change_files,
        "issues": [
            {
                "code": issue.code,
                "message": issue.message,
                **({"path": issue.path} if issue.path is not None else {}),
            }
            for issue in report.issues
        ],
    }


def _parse_mapping(raw_data: object | None) -> dict[str, Any]:
    if raw_data is None:
        return {}

    if not isinstance(raw_data, Mapping):
        raise ValueError("Expected a mapping in the validation report structure.")

    return dict(raw_data)


def _parse_sequence(raw_data: object | None) -> Sequence[object]:
    if raw_data is None:
        return ()

    if isinstance(raw_data, (str, bytes)) or not isinstance(raw_data, Sequence):
        raise ValueError("Expected a sequence in the validation report structure.")

    return raw_data


def _is_changelog_artifact_path(path: str) -> bool:
    return path.startswith("docs/changelogs/PR-") and path.endswith("-changelog.yaml")


def _contains_instruction_comments(yaml_content: str) -> bool:
    return any(
        line.lstrip().startswith("#")
        for line in yaml_content.splitlines()
        if line.strip()
    )


def _contains_null_entity_actions(yaml_content: str) -> bool:
    return any(line.strip() == "action: null" for line in yaml_content.splitlines())


def _load_yaml_mapping(yaml_content: str) -> dict[str, Any]:
    loaded_content = yaml.safe_load(yaml_content)
    if loaded_content is None:
        return {}

    if not isinstance(loaded_content, Mapping):
        raise ValueError("Change log YAML must decode to a mapping.")

    return dict(loaded_content)


def _normalize_version(raw_version: object | None) -> int | str | None:
    if isinstance(raw_version, str) and raw_version.isdigit():
        return int(raw_version)

    return cast(int | str | None, raw_version)


def _infer_version_from_schema(raw_schema: object | None) -> int | None:
    if not isinstance(raw_schema, str):
        return None

    if raw_schema.endswith("changelog-v1"):
        return 1
    if raw_schema.endswith("changelog-v2"):
        return 2

    return None


def _contains_nonempty_value(raw_value: object | None) -> bool:
    if raw_value is None:
        return False

    if isinstance(raw_value, str):
        return raw_value.strip() != ""

    if isinstance(raw_value, Mapping):
        return any(_contains_nonempty_value(value) for value in raw_value.values())

    if isinstance(raw_value, Sequence):
        return any(_contains_nonempty_value(value) for value in raw_value)

    return True


def _validate_v2_file_sections(
    raw_change_log: Mapping[str, Any],
    issues: list[ValidationIssue],
) -> None:
    for section_name in ("structured_files", "files"):
        if section_name not in raw_change_log:
            issues.append(
                ValidationIssue(
                    code="missing_required_section",
                    message=(
                        f"Version 2 changelogs must include the {section_name} section."
                    ),
                    path=section_name,
                )
            )

    seen_structured_files: set[str] = set()
    for structured_file_index, raw_structured_file in enumerate(
        _parse_sequence(raw_change_log.get("structured_files")),
        start=1,
    ):
        if isinstance(raw_structured_file, Mapping):
            issues.append(
                ValidationIssue(
                    code="structured_file_entry_invalid",
                    message=(
                        "Structured file entries must be plain repository paths "
                        "without nested metadata."
                    ),
                    path=None,
                )
            )
            continue

        structured_file_path = str(raw_structured_file).strip()
        if structured_file_path == "":
            issues.append(
                ValidationIssue(
                    code="structured_file_entry_missing_path",
                    message=(
                        f"Structured file {structured_file_index} must include a path."
                    ),
                    path=None,
                )
            )
            continue

        if not _is_structured_document_path(structured_file_path):
            issues.append(
                ValidationIssue(
                    code="structured_file_entry_not_structured_document",
                    message=(
                        f"Structured file {structured_file_index} points to "
                        f"{structured_file_path!r}, which is not a structured "
                        "YAML document path."
                    ),
                    path=structured_file_path,
                )
            )

        if structured_file_path in seen_structured_files:
            issues.append(
                ValidationIssue(
                    code="duplicate_structured_file_entry",
                    message=(
                        f"Structured file {structured_file_path!r} is listed more "
                        "than once."
                    ),
                    path=structured_file_path,
                )
            )
            continue

        seen_structured_files.add(structured_file_path)

    for file_change_index, raw_file_change in enumerate(
        _parse_sequence(raw_change_log.get("files")),
        start=1,
    ):
        file_data = _parse_mapping(raw_file_change)
        file_path = (
            None if file_data.get("path") is None else str(file_data.get("path"))
        )

        if "entities" in file_data:
            issues.append(
                ValidationIssue(
                    code="file_entry_entities_not_allowed",
                    message=(
                        f"File change {file_change_index} must move entity ids into "
                        "related.entities and remove the top-level entities field."
                    ),
                    path=file_path,
                )
            )

        if file_path is not None and _is_structured_document_path(file_path):
            issues.append(
                ValidationIssue(
                    code="file_entry_structured_path_not_allowed",
                    message=(
                        f"File change {file_change_index} points to a "
                        "structured YAML document path. Move it to structured_files "
                        "and remove its span metadata."
                    ),
                    path=file_path,
                )
            )

        if "related" in file_data and not _contains_nonempty_value(
            file_data.get("related")
        ):
            issues.append(
                ValidationIssue(
                    code="file_entry_empty_related",
                    message=(
                        f"File change {file_change_index} has an empty related "
                        "block. Remove related unless it includes at least one "
                        "file, entity, invariant, or guidance reference."
                    ),
                    path=file_path,
                )
            )


def _validate_v2_structured_file_contents(
    *,
    issues: list[ValidationIssue],
    repo_root: Path,
    structured_file_path: str,
) -> None:
    structured_file = repo_root / structured_file_path
    if not structured_file.exists():
        issues.append(
            ValidationIssue(
                code="structured_file_missing",
                message=(
                    f"Structured file {structured_file_path} does not exist in the "
                    "repository."
                ),
                path=structured_file_path,
            )
        )
        return

    try:
        raw_structured_content = yaml.safe_load(
            structured_file.read_text(encoding="utf-8")
        )
    except Exception as exc:  # noqa: BLE001
        issues.append(
            ValidationIssue(
                code="structured_file_invalid_yaml",
                message=(
                    f"Structured file {structured_file_path} could not be parsed "
                    f"as YAML: {exc}"
                ),
                path=structured_file_path,
            )
        )
        return

    if not isinstance(raw_structured_content, Mapping):
        issues.append(
            ValidationIssue(
                code="structured_file_invalid_yaml",
                message=(
                    f"Structured file {structured_file_path} must decode to a YAML "
                    "mapping."
                ),
                path=structured_file_path,
            )
        )
        return

    schema_value = raw_structured_content.get("schema")
    if not isinstance(schema_value, str) or not schema_value.startswith(
        "https://powdrr.io/schemas"
    ):
        issues.append(
            ValidationIssue(
                code="structured_file_invalid_schema",
                message=(
                    f"Structured file {structured_file_path} must define a schema "
                    "value that starts with https://powdrr.io/schemas."
                ),
                path=structured_file_path,
            )
        )


def _validate_v2_file_change(
    *,
    issues: list[ValidationIssue],
    file_change: ChangeFile,
    file_change_index: int,
) -> None:
    file_types = {"added", "deleted", "modified", "renamed", "copied"}
    if file_change.path is None or file_change.path.strip() == "":
        issues.append(
            ValidationIssue(
                code="file_entry_missing_path",
                message=f"File change {file_change_index} must include a path.",
                path=None,
            )
        )
        return

    if _is_structured_document_path(file_change.path):
        issues.append(
            ValidationIssue(
                code="file_entry_structured_path_not_allowed",
                message=(
                    f"File change {file_change_index} points to a structured "
                    "YAML document path. Move it to structured_files and remove its "
                    "span metadata."
                ),
                path=file_change.path,
            )
        )
        return

    if file_change.type is None or file_change.type.strip() == "":
        issues.append(
            ValidationIssue(
                code="file_entry_missing_type",
                message=(f"File change {file_change_index} must include a file type."),
                path=file_change.path,
            )
        )
    elif file_change.type not in file_types:
        issues.append(
            ValidationIssue(
                code="file_entry_invalid_type",
                message=(
                    f"File change {file_change_index} uses unsupported type "
                    f"{file_change.type!r}."
                ),
                path=file_change.path,
            )
        )

    if file_change.span.start_line is None or file_change.span.end_line is None:
        issues.append(
            ValidationIssue(
                code="file_entry_missing_span",
                message=f"File change {file_change_index} must include a span.",
                path=file_change.path,
            )
        )
    elif file_change.span.start_line > file_change.span.end_line:
        issues.append(
            ValidationIssue(
                code="file_entry_invalid_span",
                message=(
                    f"File change {file_change_index} has an invalid span "
                    f"{file_change.span.start_line}-{file_change.span.end_line}."
                ),
                path=file_change.path,
            )
        )

    if file_change.summary is None or str(file_change.summary).strip() == "":
        issues.append(
            ValidationIssue(
                code="file_entry_missing_summary",
                message=f"File change {file_change_index} must include a summary.",
                path=file_change.path,
            )
        )

    if file_change.rationale is None or str(file_change.rationale).strip() == "":
        issues.append(
            ValidationIssue(
                code="file_entry_missing_rationale",
                message=(f"File change {file_change_index} must include a rationale."),
                path=file_change.path,
            )
        )

    for entity_index, entity in enumerate(
        _file_change_entity_ids(file_change), start=1
    ):
        if _normalize_entity_id(entity) is None:
            issues.append(
                ValidationIssue(
                    code="file_entry_entity_missing_id",
                    message=(
                        f"File change {file_change_index} entity {entity_index} "
                        "must include an id."
                    ),
                    path=file_change.path,
                )
            )


def _validate_v2_entity_change(
    *,
    issues: list[ValidationIssue],
    entity_change: ChangeEntity,
    entity_change_index: int,
    allowed_entity_types: set[str] | None = None,
) -> None:
    normalized_entity_id = _normalize_entity_id(entity_change.id)
    if normalized_entity_id is None:
        issues.append(
            ValidationIssue(
                code="entity_missing_id",
                message=(
                    f"Entity change {entity_change_index} contains an entry "
                    "without an id."
                ),
                path=None,
            )
        )
        return

    if entity_change.action == "added":
        _validate_added_entity_type(
            issues=issues,
            entity=entity_change,
            entity_id=normalized_entity_id,
            allowed_entity_types=allowed_entity_types,
        )

    if entity_change.action not in {"added", "deleted", "modified"}:
        issues.append(
            ValidationIssue(
                code="entity_action_invalid",
                message=(
                    f"Entity {normalized_entity_id} in entity change "
                    f"{entity_change_index} must be marked as added, deleted, "
                    "or modified."
                ),
                path=None,
            )
        )


def _validate_added_entity_type(
    *,
    issues: list[ValidationIssue],
    entity: ChangeEntity,
    entity_id: str,
    allowed_entity_types: set[str] | None,
) -> None:
    if allowed_entity_types is None:
        return

    entity_type = entity.type.strip() if entity.type is not None else ""
    if entity_type == "":
        issues.append(
            ValidationIssue(
                code="entity_type_missing",
                message=(
                    f"Entity {entity_id} is marked added but does not include a type."
                ),
                path=None,
            )
        )
        return

    if entity_type not in allowed_entity_types:
        issues.append(
            ValidationIssue(
                code="entity_type_not_allowed",
                message=(
                    f"Entity {entity_id} is marked added with unsupported type "
                    f"{entity_type!r}."
                ),
                path=None,
            )
        )


def _validate_unique_changelog_ids(
    change_log: ChangeLog,
    issues: list[ValidationIssue],
) -> None:
    seen_ids: dict[str, tuple[str, int]] = {}

    def _register(raw_id: str | None, section: str, item_index: int) -> None:
        normalized_id = _normalize_entity_id(raw_id)
        if normalized_id is None:
            return

        previous = seen_ids.get(normalized_id)
        if previous is not None:
            issues.append(
                ValidationIssue(
                    code="duplicate_changelog_id",
                    message=(
                        f"Changelog id {normalized_id!r} is repeated in "
                        f"{section} {item_index} and {previous[0]} {previous[1]}. "
                        "All changelog ids must be unique across the entire file."
                    ),
                    path=None,
                )
            )
            return

        seen_ids[normalized_id] = (section, item_index)

    for index, decision in enumerate(change_log.decisions or [], start=1):
        _register(decision.id, "decision", index)

    for index, entity in enumerate(change_log.entity_changes or [], start=1):
        _register(entity.id, "entity", index)

    for index, relationship in enumerate(
        change_log.entity_relationship_changes or [],
        start=1,
    ):
        _register(relationship.id, "relationship", index)

    for index, invariant in enumerate(change_log.invariant_changes or [], start=1):
        _register(invariant.id, "invariant", index)

    for index, guidance in enumerate(change_log.guidance_changes or [], start=1):
        _register(guidance.id, "guidance", index)


def _validate_v2_lifecycle_item(
    *,
    issues: list[ValidationIssue],
    change_index: int,
    item_index: int,
    item_kind: str,
    item_id: str | None,
    description: str | None,
    action: str | None,
    related: RelatedSection | None,
    available_files: set[str],
    available_entities: set[str | None],
    available_invariants: set[str | None],
    available_guidance: set[str | None],
    path: str | None,
) -> None:
    normalized_item_id = _normalize_entity_id(item_id)
    if normalized_item_id is None:
        issues.append(
            ValidationIssue(
                code=f"{item_kind}_missing_id",
                message=(
                    f"Change {change_index} {item_kind} entry {item_index} must "
                    "include an id."
                ),
                path=path,
            )
        )

    if description is None or str(description).strip() == "":
        issues.append(
            ValidationIssue(
                code=f"{item_kind}_missing_description",
                message=(
                    f"Change {change_index} {item_kind} entry {item_index} must "
                    "include a description."
                ),
                path=path,
            )
        )

    if action not in {"added", "removed", "altered"}:
        issues.append(
            ValidationIssue(
                code=f"{item_kind}_invalid_action",
                message=(
                    f"Change {change_index} {item_kind} entry {item_index} must "
                    "use action added, removed, or altered."
                ),
                path=path,
            )
        )

    if related is None:
        return

    for file_id in related.files:
        if file_id not in available_files:
            issues.append(
                ValidationIssue(
                    code=f"{item_kind}_related_unknown_file",
                    message=(
                        f"{item_kind.capitalize()} {normalized_item_id or item_index} "
                        f"references file {file_id}, but that file is not listed in "
                        "the change files section."
                    ),
                    path=path,
                )
            )

    for entity_id in related.entities:
        if entity_id not in available_entities:
            issues.append(
                ValidationIssue(
                    code=f"{item_kind}_related_unknown_entity",
                    message=(
                        f"{item_kind.capitalize()} {normalized_item_id or item_index} "
                        f"references entity {entity_id}, but that entity is not "
                        "listed in the change entities section."
                    ),
                    path=path,
                )
            )

    for invariant_id in related.invariants:
        if invariant_id not in available_invariants:
            issues.append(
                ValidationIssue(
                    code=f"{item_kind}_related_unknown_invariant",
                    message=(
                        f"{item_kind.capitalize()} {normalized_item_id or item_index} "
                        f"references invariant {invariant_id}, but that invariant is "
                        "not listed in the change invariants section."
                    ),
                    path=path,
                )
            )

    for guidance_id in related.guidance:
        if guidance_id not in available_guidance:
            issues.append(
                ValidationIssue(
                    code=f"{item_kind}_related_unknown_guidance",
                    message=(
                        f"{item_kind.capitalize()} {normalized_item_id or item_index} "
                        f"references guidance {guidance_id}, but that guidance is "
                        "not listed in the change guidance section."
                    ),
                    path=path,
                )
            )


def _validate_v2_state_change(
    *,
    issues: list[ValidationIssue],
    section_name: str,
    item_index: int,
    item_id: str | None,
    state: str | None,
    available_ids: set[str],
    unavailable_id_code: str,
    unavailable_id_message: str,
    invalid_state_code: str,
    invalid_state_message: str,
) -> None:
    normalized_item_id = _normalize_entity_id(item_id)
    item_label = section_name[:-1] if section_name.endswith("s") else section_name
    path_prefix = f"{section_name}[{item_index - 1}]"

    if normalized_item_id is None:
        issues.append(
            ValidationIssue(
                code=f"{item_label}_missing_id",
                message=f"Change {item_index} {item_label} entry must include an id.",
                path=f"{path_prefix}.id",
            )
        )
    elif normalized_item_id not in available_ids:
        issues.append(
            ValidationIssue(
                code=unavailable_id_code,
                message=unavailable_id_message.format(item_id=normalized_item_id),
                path=f"{path_prefix}.id",
            )
        )

    if state not in {"in_progress", "completed"}:
        issues.append(
            ValidationIssue(
                code=invalid_state_code,
                message=invalid_state_message,
                path=f"{path_prefix}.state",
            )
        )


def _load_available_feature_ids(repo_root: Path) -> set[str]:
    try:
        report = build_codebase_state_report(repo_root=repo_root)
    except Exception:  # noqa: BLE001
        return set()

    return {
        entity.id
        for entity in report.entities
        if _normalize_entity_id(entity.id) is not None
    }


def _load_known_proposed_pr_ids(repo_root: Path) -> set[str]:
    pr_dir = repo_root / "docs" / "changelogs"
    if not pr_dir.exists():
        return set()

    known_pr_ids: set[str] = set()
    for changelog_path in pr_dir.glob("PR-*-changelog.yaml"):
        name = changelog_path.name
        if not name.startswith("PR-") or not name.endswith("-changelog.yaml"):
            continue

        pr_id = name.removeprefix("PR-").removesuffix("-changelog.yaml")
        if pr_id:
            known_pr_ids.add(pr_id)

    return known_pr_ids
