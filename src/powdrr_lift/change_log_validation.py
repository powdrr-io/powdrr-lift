from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import yaml

from powdrr_lift.change_log_parser import Change, RelatedSection, parse_change_log
from powdrr_lift.change_log_template import (
    BranchDiffEntry,
    _collect_branch_diff_entries,
    _resolve_default_branch,
    _resolve_repo_root,
)
from powdrr_lift.core.index import (
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

    if version == 2:
        if "entities" in raw_change_log:
            issues.append(
                ValidationIssue(
                    code="top_level_entities_not_allowed",
                    message=(
                        "Version 2 changelogs must not include a top-level "
                        "entities section."
                    ),
                )
            )

        if "relationship_changes" in raw_change_log:
            issues.append(
                ValidationIssue(
                    code="top_level_relationship_changes_not_allowed",
                    message=(
                        "Version 2 changelogs must move relationship changes "
                        "under each change entry."
                    ),
                )
            )

    proposed_change_files = [
        change.file
        for change in change_log.changes
        if change.file is not None and change.file != ""
    ]
    proposed_entities_by_id = {
        entity_id: entity
        for entity in change_log.entities
        if (entity_id := _normalize_entity_id(entity.id)) is not None
    }
    expected_change_entries_by_file: dict[str, list[BranchDiffEntry]] = {}
    for entry in diff_entries:
        if _is_changelog_artifact_path(entry.path):
            continue

        expected_change_entries_by_file.setdefault(entry.path, []).append(entry)

    proposed_change_entries_by_file: dict[str, list[Change]] = {}
    for change in change_log.changes:
        if change.file is None or change.file == "":
            continue

        proposed_change_entries_by_file.setdefault(change.file, []).append(change)

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
        for change_index, change in enumerate(change_log.changes, start=1):
            file_paths = [
                file_entry.path
                for file_entry in change.files
                if file_entry.path is not None and file_entry.path != ""
            ]
            if not file_paths:
                issues.append(
                    ValidationIssue(
                        code="missing_files_section",
                        message=(
                            f"Change {change_index} must include at least one file "
                            "entry."
                        ),
                        path=change.file,
                    )
                )

            _validate_v2_change(
                issues=issues,
                change=change,
                change_index=change_index,
            )
    else:
        for relationship_change in change_log.relationship_changes:
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

    for file_path, expected_entries in expected_change_entries_by_file.items():
        proposed_entries = proposed_change_entries_by_file.get(file_path, [])
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

    for file_path in proposed_change_entries_by_file:
        if file_path in expected_change_entries_by_file:
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


def _validate_v2_change(
    *,
    issues: list[ValidationIssue],
    change: Change,
    change_index: int,
) -> None:
    file_types = {"added", "deleted", "modified", "renamed", "copied"}
    file_paths = [
        file_entry.path
        for file_entry in change.files
        if file_entry.path is not None and file_entry.path != ""
    ]
    if len(file_paths) != len(change.files):
        issues.append(
            ValidationIssue(
                code="file_entry_missing_path",
                message=(
                    f"Change {change_index} includes a file entry without a path."
                ),
                path=change.file,
            )
        )

    for file_entry_index, file_entry in enumerate(change.files, start=1):
        if file_entry.type is None or file_entry.type.strip() == "":
            issues.append(
                ValidationIssue(
                    code="file_entry_missing_type",
                    message=(
                        f"Change {change_index} file entry {file_entry_index} "
                        "must include a file type."
                    ),
                    path=file_entry.path,
                )
            )
            continue

        if file_entry.type not in file_types:
            issues.append(
                ValidationIssue(
                    code="file_entry_invalid_type",
                    message=(
                        f"Change {change_index} file entry {file_entry_index} "
                        f"uses unsupported type {file_entry.type!r}."
                    ),
                    path=file_entry.path,
                )
            )

    entity_ids_in_change = {
        normalized_entity_id
        for entity in change.entities
        if (normalized_entity_id := _normalize_entity_id(entity.id)) is not None
    }
    for entity in change.entities:
        normalized_entity_id = _normalize_entity_id(entity.id)
        if normalized_entity_id is None:
            issues.append(
                ValidationIssue(
                    code="entity_missing_id",
                    message=(
                        f"Change {change_index} contains an entity entry without an id."
                    ),
                    path=change.file,
                )
            )
            continue

        if entity.action not in {"added", "removed"}:
            issues.append(
                ValidationIssue(
                    code="entity_action_invalid",
                    message=(
                        f"Entity {normalized_entity_id} in change {change_index} "
                        "must be marked as added or removed."
                    ),
                    path=change.file,
                )
            )

    for relationship_change in change.entity_relationships:
        source = _normalize_entity_id(relationship_change.source)
        target = _normalize_entity_id(relationship_change.target)
        if source is None or target is None:
            issues.append(
                ValidationIssue(
                    code="relationship_unknown_entity",
                    message=(
                        f"Change {change_index} relationship entries must name both "
                        "source and target entities."
                    ),
                    path=change.file,
                )
            )
            continue

        missing_entities = [
            entity_id
            for entity_id in (source, target)
            if entity_id not in entity_ids_in_change
        ]
        for entity_id in missing_entities:
            issues.append(
                ValidationIssue(
                    code="relationship_unknown_entity",
                    message=(
                        f"Relationship references entity {entity_id}, but that "
                        "entity is not listed in the change entities section."
                    ),
                    path=change.file,
                )
            )

    available_files = set(file_paths)
    available_entities = set(entity_ids_in_change)
    available_invariants = {
        invariant_id
        for invariant in change.invariants
        if (invariant_id := _normalize_entity_id(invariant.id)) is not None
    }
    available_guidance = {
        guidance_id
        for guidance in change.guidance
        if (guidance_id := _normalize_entity_id(guidance.id)) is not None
    }

    for item_index, invariant in enumerate(change.invariants, start=1):
        _validate_lifecycle_item(
            issues=issues,
            change_index=change_index,
            item_index=item_index,
            item_kind="invariant",
            item_id=invariant.id,
            description=invariant.description,
            action=invariant.action,
            related=invariant.related,
            available_files=available_files,
            available_entities=available_entities,
            available_invariants=available_invariants,
            available_guidance=available_guidance,
            path=change.file,
        )

    for item_index, guidance in enumerate(change.guidance, start=1):
        _validate_lifecycle_item(
            issues=issues,
            change_index=change_index,
            item_index=item_index,
            item_kind="guidance",
            item_id=guidance.id,
            description=guidance.description,
            action=guidance.action,
            related=guidance.related,
            available_files=available_files,
            available_entities=available_entities,
            available_invariants=available_invariants,
            available_guidance=available_guidance,
            path=change.file,
        )


def _validate_lifecycle_item(
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
    available_entities: set[str],
    available_invariants: set[str],
    available_guidance: set[str],
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
