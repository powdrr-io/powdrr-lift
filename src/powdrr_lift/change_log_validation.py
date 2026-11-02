from __future__ import annotations

import subprocess
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import yaml
from powdrr_service.changelog import (
    ChangeEntity,
    parse_change_log,
    parse_structured_document,
    qualify_structured_document_issues,
)
from powdrr_service.changelog.template import (
    is_structured_document_path as _is_structured_document_path,
)
from powdrr_service.changelog.validation_policy import (
    ChangelogValidationIssue,
    collect_changelog_defined_ids,
    parse_changelog_mapping,
    parse_changelog_sequence,
    validate_added_entity_type,
    validate_quoted_rationale_ids,
    validate_unique_changelog_ids,
    validate_v2_entity_change,
    validate_v2_file_change,
    validate_v2_file_sections,
    validate_v2_lifecycle_item,
    validate_v2_state_change,
)

from powdrr_lift.change_log_template import (
    BranchDiffEntry,
    _collect_branch_diff_entries,
    _current_branch,
    _resolve_default_branch,
    _resolve_repo_root,
)
from powdrr_lift.core.architecture_specification import (
    build_architecture_specification_validation_report,
)
from powdrr_lift.core.entity_taxonomy import load_entity_taxonomy
from powdrr_lift.core.implementation_specification import (
    build_implementation_specification_validation_report,
)
from powdrr_lift.core.index import (
    _load_changelog_documents,
    _load_changelog_documents_at_ref,
    _normalize_entity_id,
)
from powdrr_lift.core.pr_specification import (
    build_pr_specification_validation_report,
)
from powdrr_lift.core.spec_paths import SPECIFICATION_SCHEMA_URL
from powdrr_lift.core.system_specification import (
    build_system_specification_validation_report,
)
from powdrr_lift.core.validation_messages import (
    ValidationError,
    validation_error_to_data,
)


@dataclass(frozen=True, slots=True)
class ValidationIssue(ValidationError):
    pass


@dataclass(frozen=True, slots=True)
class ValidationReport:
    validation_successful: bool
    branch_name: str
    default_branch_name: str
    expected_change_files: list[str] = field(default_factory=list)
    proposed_change_files: list[str] = field(default_factory=list)
    issues: list[ValidationIssue] = field(default_factory=list)


def _run_changelog_policy(
    validator: Callable[..., None],
    **arguments: Any,
) -> list[ValidationIssue]:
    policy_issues: list[ChangelogValidationIssue] = []
    validator(issues=policy_issues, **arguments)
    return [
        ValidationIssue(code=issue.code, message=issue.message, path=issue.path)
        for issue in policy_issues
    ]


def validate_change_log_yaml(
    proposed_change_log_yaml: str,
    branch_name: str | None = None,
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
    branch_name: str | None = None,
    repo_root: str | Path | None = None,
    default_branch: str | None = None,
) -> ValidationReport:
    repo_root_path = _resolve_repo_root(repo_root)
    branch_name = branch_name or _current_branch(repo_root_path)
    default_branch_name = default_branch or _resolve_default_branch(
        repo_root_path,
        branch_name=branch_name,
    )
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
                        "guidance, features, human-decisions, and proposed_prs "
                        "sections."
                    ),
                )
            )
        if "prs" in raw_change_log:
            issues.append(
                ValidationIssue(
                    code="top_level_prs_not_allowed",
                    message=(
                        "Version 2 changelogs must use top-level proposed_prs "
                        "instead of prs."
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
            for entity_data in parse_changelog_sequence(raw_change_log.get("entities"))
        ]
    else:
        proposed_entities = list(change_log.entity_changes or [])

    proposed_entities_by_id = {
        entity_id: entity
        for entity in proposed_entities
        if (entity_id := _normalize_entity_id(entity.id)) is not None
    }
    available_changelog_ids = collect_changelog_defined_ids(change_log)
    available_proposed_pr_detail_ids = _load_available_proposed_pr_detail_ids(
        repo_root_path
    )
    available_proposed_pr_ids = _load_known_proposed_pr_ids(repo_root_path)
    available_rationale_ids = (
        _load_available_rationale_ids(repo_root_path)
        | available_changelog_ids
        | available_proposed_pr_detail_ids
    )

    if version == 2:
        issues.extend(
            _run_changelog_policy(
                validate_v2_file_sections,
                raw_change_log=raw_change_log,
                available_files={
                    file_change.path
                    for file_change in change_log.file_changes
                    if file_change.path is not None and file_change.path != ""
                },
                available_entity_ids={
                    entity_id
                    for entity_id in (
                        _normalize_entity_id(entity.id)
                        for entity in (change_log.entity_changes or [])
                    )
                    if entity_id is not None
                },
                available_decision_ids={
                    decision_id
                    for decision_id in (
                        _normalize_entity_id(decision.id)
                        for decision in (change_log.decisions or [])
                    )
                    if decision_id is not None
                },
                available_invariant_ids={
                    invariant_id
                    for invariant_id in (
                        _normalize_entity_id(invariant.id)
                        for invariant in (change_log.invariant_changes or [])
                    )
                    if invariant_id is not None
                },
                available_guidance_ids={
                    guidance_id
                    for guidance_id in (
                        _normalize_entity_id(guidance.id)
                        for guidance in (change_log.guidance_changes or [])
                    )
                    if guidance_id is not None
                },
                available_proposed_pr_ids=available_proposed_pr_ids,
                available_proposed_pr_detail_ids=available_proposed_pr_detail_ids,
                available_rationale_ids=available_rationale_ids,
            )
        )
        if issues:
            return ValidationReport(
                validation_successful=False,
                branch_name=branch_name,
                default_branch_name=default_branch_name,
                expected_change_files=expected_change_files,
                proposed_change_files=proposed_change_files,
                issues=issues,
            )

    issues.extend(
        _run_changelog_policy(
            validate_unique_changelog_ids,
            change_log=change_log,
        )
    )
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

    parent_entity_ids = _load_entity_ids_from_changelogs_at_ref(
        repo_root_path, default_branch_name
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
            issues.extend(
                _run_changelog_policy(
                    validate_added_entity_type,
                    entity=entity,
                    entity_id=entity_id,
                    allowed_entity_types=allowed_entity_types,
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
        proposed_structured_files = list(
            dict.fromkeys(
                structured_file
                for structured_file in change_log.structured_files
                if structured_file is not None and structured_file != ""
            )
        )
        proposed_structured_file_set = set(proposed_structured_files)
        branch_changed_paths = _load_branch_changed_paths(
            repo_root_path,
            default_branch_name=default_branch_name,
            branch_name=branch_name,
        )
        merged_structured_file_paths = _load_merged_structured_file_paths(
            repo_root_path,
            default_branch_name=default_branch_name,
        )
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
            issues.extend(
                _run_changelog_policy(
                    validate_v2_file_change,
                    file_change=file_change,
                    file_change_index=file_change_index,
                )
            )

        for entity_change_index, entity_change in enumerate(
            change_log.entity_changes or [],
            start=1,
        ):
            issues.extend(
                _run_changelog_policy(
                    validate_v2_entity_change,
                    entity_change=entity_change,
                    entity_change_index=entity_change_index,
                    allowed_entity_types=allowed_entity_types,
                )
            )

        for invariant_change_index, invariant_change in enumerate(
            change_log.invariant_changes or [],
            start=1,
        ):
            issues.extend(
                _run_changelog_policy(
                    validate_v2_lifecycle_item,
                    change_index=invariant_change_index,
                    item_index=invariant_change_index,
                    item_kind="invariant",
                    item_id=invariant_change.id,
                    description=invariant_change.description,
                    rationale=None,
                    action=invariant_change.action,
                    related=invariant_change.related,
                    available_files={
                        file_change.path
                        for file_change in change_log.file_changes
                        if file_change.path is not None and file_change.path != ""
                    },
                    available_entities={
                        entity_id
                        for entity in (change_log.entity_changes or [])
                        if (entity_id := _normalize_entity_id(entity.id)) is not None
                    },
                    available_invariants={
                        invariant_id
                        for invariant in (change_log.invariant_changes or [])
                        if (invariant_id := _normalize_entity_id(invariant.id))
                        is not None
                    },
                    available_guidance={
                        guidance_id
                        for guidance in (change_log.guidance_changes or [])
                        if (guidance_id := _normalize_entity_id(guidance.id))
                        is not None
                    },
                    available_proposed_pr_detail_ids=available_proposed_pr_detail_ids,
                    available_rationale_ids=available_rationale_ids,
                    path=None,
                )
            )

        for guidance_change_index, guidance_change in enumerate(
            change_log.guidance_changes or [],
            start=1,
        ):
            issues.extend(
                _run_changelog_policy(
                    validate_v2_lifecycle_item,
                    change_index=guidance_change_index,
                    item_index=guidance_change_index,
                    item_kind="guidance",
                    item_id=guidance_change.id,
                    description=guidance_change.description,
                    rationale=None,
                    action=guidance_change.action,
                    related=guidance_change.related,
                    available_files={
                        file_change.path
                        for file_change in change_log.file_changes
                        if file_change.path is not None and file_change.path != ""
                    },
                    available_entities={
                        entity_id
                        for entity in (change_log.entity_changes or [])
                        if (entity_id := _normalize_entity_id(entity.id)) is not None
                    },
                    available_invariants={
                        invariant_id
                        for invariant in (change_log.invariant_changes or [])
                        if (invariant_id := _normalize_entity_id(invariant.id))
                        is not None
                    },
                    available_guidance={
                        guidance_id
                        for guidance in (change_log.guidance_changes or [])
                        if (guidance_id := _normalize_entity_id(guidance.id))
                        is not None
                    },
                    available_proposed_pr_detail_ids=available_proposed_pr_detail_ids,
                    available_rationale_ids=available_rationale_ids,
                    path=None,
                )
            )

        available_feature_ids = _load_available_feature_ids(repo_root_path)
        known_pr_ids = _load_known_proposed_pr_ids(repo_root_path)

        for feature_change_index, feature_change in enumerate(
            change_log.feature_changes or [],
            start=1,
        ):
            issues.extend(
                _run_changelog_policy(
                    validate_v2_state_change,
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
            )

        for pr_change_index, pr_change in enumerate(
            change_log.proposed_prs or [], start=1
        ):
            issues.extend(
                _run_changelog_policy(
                    validate_v2_state_change,
                    section_name="proposed_prs",
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
            if structured_file_path not in branch_changed_paths:
                issues.append(
                    ValidationIssue(
                        code="structured_file_not_in_branch_diff",
                        message=(
                            "Structured file entry for "
                            f"{structured_file_path} does not appear in the branch "
                            "diff"
                        ),
                        path=structured_file_path,
                    )
                )
                continue

            if structured_file_path in merged_structured_file_paths:
                issues.append(
                    ValidationIssue(
                        code="structured_file_edited_after_merge",
                        message=(
                            f"Structured file {structured_file_path} was already "
                            "merged into main. Do not edit merged structured "
                            "files; create a new work item folder under "
                            "`docs/proposals/` or `docs/current/` instead."
                        ),
                        path=structured_file_path,
                    )
                )
                continue

            _validate_v2_structured_file_contents(
                issues=issues,
                repo_root=repo_root_path,
                structured_file_path=structured_file_path,
                allowed_entity_types=allowed_entity_types,
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
            issues.extend(
                _run_changelog_policy(
                    validate_quoted_rationale_ids,
                    rationale=relationship_change.rationale,
                    available_ids=available_rationale_ids,
                    code="relationship_unknown_rationale_id",
                    path=None,
                    subject_label=f"Relationship {relationship_change.id or 'unknown'}",
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
            issues.extend(
                _run_changelog_policy(
                    validate_quoted_rationale_ids,
                    rationale=relationship_change.rationale,
                    available_ids=available_rationale_ids,
                    code="relationship_unknown_rationale_id",
                    path=None,
                    subject_label=f"Relationship {relationship_change.id or 'unknown'}",
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
            for change_file in parse_changelog_sequence(
                data.get("expected_change_files")
            )
        ],
        proposed_change_files=[
            str(change_file)
            for change_file in parse_changelog_sequence(
                data.get("proposed_change_files")
            )
        ],
        issues=[
            _parse_validation_issue(issue_data)
            for issue_data in parse_changelog_sequence(data.get("issues"))
        ],
    )


def _parse_validation_issue(raw_issue: object) -> ValidationIssue:
    data = parse_changelog_mapping(raw_issue)
    return ValidationIssue(
        code=str(data.get("code", "")),
        message=str(data.get("message", "")),
        path=None if data.get("path") is None else str(data.get("path")),
    )


def _parse_validation_entity(raw_entity: object) -> ChangeEntity:
    data = parse_changelog_mapping(raw_entity)
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
        "issues": [validation_error_to_data(issue) for issue in report.issues],
    }


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


def _validate_v2_structured_file_contents(
    *,
    issues: list[ValidationIssue],
    repo_root: Path,
    structured_file_path: str,
    allowed_entity_types: set[str] | None = None,
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
        raw_yaml = structured_file.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        issues.append(
            ValidationIssue(
                code="structured_file_invalid_yaml",
                message=(
                    f"Structured file {structured_file_path} could not be read "
                    f"as UTF-8 YAML: {exc}"
                ),
                path=structured_file_path,
            )
        )
        return
    parse_result = parse_structured_document(
        raw_yaml,
        path=structured_file_path,
        expected_schema=SPECIFICATION_SCHEMA_URL,
    )
    if parse_result.issues:
        issues.extend(
            ValidationIssue(code=issue.code, message=issue.message, path=issue.path)
            for issue in parse_result.issues
        )
        return

    _validate_v2_structured_file_specification(
        issues=issues,
        repo_root=repo_root,
        structured_file_path=structured_file_path,
        raw_yaml=raw_yaml,
        allowed_entity_types=allowed_entity_types,
    )


def _validate_v2_structured_file_specification(
    *,
    issues: list[ValidationIssue],
    repo_root: Path,
    structured_file_path: str,
    raw_yaml: str,
    allowed_entity_types: set[str] | None,
) -> None:
    structured_file_name = Path(structured_file_path).name
    work_item_name = Path(structured_file_path).parent.name
    if structured_file_name == "system-specification.yaml":
        system_report = build_system_specification_validation_report(
            raw_yaml,
            work_item_name=work_item_name,
        )
        issues.extend(
            ValidationIssue(code=issue.code, message=issue.message, path=issue.path)
            for issue in qualify_structured_document_issues(
                path=structured_file_path,
                validator_name="system_specification",
                report_issues=system_report.issues,
            )
        )
        return

    if structured_file_name == "architecture-specification.yaml":
        effective_allowed_entity_types = allowed_entity_types
        if effective_allowed_entity_types is None:
            try:
                effective_allowed_entity_types = set(
                    load_entity_taxonomy(repo_root).entity_types
                )
            except FileNotFoundError:
                issues.append(
                    ValidationIssue(
                        code="entity_taxonomy_missing",
                        message=(
                            "Architecture structured-file validation requires "
                            "software_development_entity_taxonomy.md at the repo "
                            "root."
                        ),
                    )
                )
                return
            except Exception as exc:  # noqa: BLE001
                issues.append(
                    ValidationIssue(
                        code="entity_taxonomy_invalid",
                        message=(
                            "Could not parse software_development_entity_taxonomy."
                            f"md: {exc}"
                        ),
                    )
                )
                return
        architecture_report = build_architecture_specification_validation_report(
            raw_yaml,
            entity_types=sorted(effective_allowed_entity_types),
            work_item_name=work_item_name,
            repo_root=repo_root,
        )
        issues.extend(
            ValidationIssue(code=issue.code, message=issue.message, path=issue.path)
            for issue in qualify_structured_document_issues(
                path=structured_file_path,
                validator_name="architecture_specification",
                report_issues=architecture_report.issues,
            )
        )
        return

    if structured_file_name == "implementation-specification.yaml":
        implementation_report = build_implementation_specification_validation_report(
            raw_yaml,
            work_item_name=work_item_name,
            repo_root=repo_root,
        )
        issues.extend(
            ValidationIssue(code=issue.code, message=issue.message, path=issue.path)
            for issue in qualify_structured_document_issues(
                path=structured_file_path,
                validator_name="implementation_specification",
                report_issues=implementation_report.issues,
            )
        )
        return

    if structured_file_name == "proposed-pr-specification.yaml":
        proposed_pr_report = build_pr_specification_validation_report(
            raw_yaml,
            work_item_name=work_item_name,
            repo_root=repo_root,
            file_path=structured_file_path,
        )
        issues.extend(
            ValidationIssue(code=issue.code, message=issue.message, path=issue.path)
            for issue in qualify_structured_document_issues(
                path=structured_file_path,
                validator_name="pr_specification",
                report_issues=proposed_pr_report.issues,
            )
        )
        return


def _load_available_proposed_pr_detail_ids(repo_root: Path) -> set[str]:
    return _load_specification_ids(repo_root, "proposed_pr") | _load_specification_ids(
        repo_root, "feature_pr"
    )


def _load_available_rationale_ids(repo_root: Path) -> set[str]:
    rationale_ids: set[str] = set()

    for specification_kind in (
        "architecture",
        "system",
        "implementation",
        "proposed_pr",
    ):
        rationale_ids.update(_load_specification_ids(repo_root, specification_kind))

    try:
        changelog_documents = _load_changelog_documents(repo_root, "docs/changelogs")
    except Exception:  # noqa: BLE001
        changelog_documents = []

    for document in changelog_documents:
        rationale_ids.update(collect_changelog_defined_ids(document.changelog))

    return rationale_ids


def _load_entity_ids_from_changelogs_at_ref(repo_root: Path, ref: str) -> set[str]:
    try:
        documents = _load_changelog_documents_at_ref(repo_root, ref)
    except Exception:  # noqa: BLE001
        return set()

    return {
        entity_id
        for document in documents
        for entity in (document.changelog.entity_changes or [])
        if (entity_id := _normalize_entity_id(entity.id)) is not None
    }


def _load_specification_ids(repo_root: Path, specification_kind: str) -> set[str]:
    specification_ids: set[str] = set()
    for specification_path in _iter_specification_paths(repo_root, specification_kind):
        try:
            raw_spec = _load_yaml_mapping(
                specification_path.read_text(encoding="utf-8")
            )
        except Exception:  # noqa: BLE001
            continue

        if specification_kind == "architecture":
            specification_ids.update(
                _collect_section_ids(raw_spec, "entities", "id")
                | _collect_section_ids(raw_spec, "entity_relationships", "id")
                | _collect_section_ids(raw_spec, "invariants", "id")
                | _collect_section_ids(raw_spec, "guidance", "id")
            )
            normalized_id = _normalize_entity_id(
                None if raw_spec.get("id") is None else str(raw_spec.get("id"))
            )
            if normalized_id is not None:
                specification_ids.add(normalized_id)
        elif specification_kind == "system":
            specification_ids.update(
                _collect_section_ids(raw_spec, "requirements", "id")
                | _collect_section_ids(raw_spec, "approach", "id")
            )
            normalized_id = _normalize_entity_id(
                None if raw_spec.get("id") is None else str(raw_spec.get("id"))
            )
            if normalized_id is not None:
                specification_ids.add(normalized_id)
        elif specification_kind == "implementation":
            specification_ids.update(
                _collect_section_ids(raw_spec, "entities", "id")
                | _collect_section_ids(raw_spec, "entity_relationships", "id")
                | _collect_section_ids(raw_spec, "features", "id")
                | _collect_section_ids(raw_spec, "decisions", "id")
            )
            architecture_id = _normalize_entity_id(
                None
                if raw_spec.get("architecture_id") is None
                else str(raw_spec.get("architecture_id"))
            )
            if architecture_id is not None:
                specification_ids.add(architecture_id)
        else:
            specification_ids.update(
                _collect_section_ids(raw_spec, "acceptance_criteria", "id")
                | _collect_section_ids(raw_spec, "expected_tests", "id")
                | _collect_section_ids(raw_spec, "required_test_cases", "id")
                | _collect_section_ids(raw_spec, "expected_outcomes", "id")
                | _collect_section_ids(raw_spec, "non_goals", "id")
                | _collect_section_ids(raw_spec, "risks", "id")
            )
            normalized_id = _normalize_entity_id(
                None if raw_spec.get("id") is None else str(raw_spec.get("id"))
            )
            if normalized_id is not None:
                specification_ids.add(normalized_id)

            for feature_id in parse_changelog_sequence(raw_spec.get("feature_ids")):
                normalized_feature_id = _normalize_entity_id(str(feature_id))
                if normalized_feature_id is not None:
                    specification_ids.add(normalized_feature_id)

    return specification_ids


def _iter_specification_paths(repo_root: Path, specification_kind: str) -> list[Path]:
    spec_root = repo_root / "docs" / "current"
    filename_by_kind = {
        "architecture": "architecture-specification.yaml",
        "feature_pr": "feature-pr-specification.yaml",
        "system": "system-specification.yaml",
        "implementation": "implementation-specification.yaml",
        "proposed_pr": "proposed-pr-specification.yaml",
    }
    filename = filename_by_kind.get(specification_kind)
    if filename is None:
        return []

    specification_paths = (
        [
            specification_path
            for specification_path in spec_root.rglob(filename)
            if specification_path.is_file()
        ]
        if spec_root.is_dir()
        else []
    )

    if specification_kind == "proposed_pr":
        proposal_root = repo_root / "docs" / "proposals"
        if proposal_root.exists():
            specification_paths.extend(
                specification_path
                for specification_path in proposal_root.rglob(filename)
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


def _collect_section_ids(
    raw_spec: Mapping[str, Any],
    section_name: str,
    item_id_key: str,
) -> set[str]:
    ids: set[str] = set()
    for raw_item in parse_changelog_sequence(raw_spec.get(section_name)):
        item = parse_changelog_mapping(raw_item)
        item_id = _normalize_entity_id(
            None if item.get(item_id_key) is None else str(item.get(item_id_key))
        )
        if item_id is not None:
            ids.add(item_id)
    return ids


def _load_merged_structured_file_paths(
    repo_root: Path,
    *,
    default_branch_name: str,
) -> set[str]:
    try:
        output = subprocess.run(
            [
                "git",
                "-C",
                str(repo_root),
                "ls-tree",
                "-r",
                "--name-only",
                default_branch_name,
                "docs/proposals",
                "docs/current",
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    except Exception:  # noqa: BLE001
        return set()

    return {
        path
        for path in (line.strip() for line in output.splitlines())
        if path and _is_structured_document_path(path)
    }


def _load_branch_changed_paths(
    repo_root: Path,
    *,
    default_branch_name: str,
    branch_name: str,
) -> set[str]:
    try:
        output = subprocess.run(
            [
                "git",
                "-C",
                str(repo_root),
                "diff",
                "--name-only",
                "--diff-filter=ACMR",
                f"{default_branch_name}...{branch_name}",
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    except Exception:  # noqa: BLE001
        return set()

    return {
        path
        for path in (line.strip() for line in output.splitlines())
        if path and not _is_changelog_artifact_path(path)
    }


def _load_available_feature_ids(repo_root: Path) -> set[str]:
    feature_ids: set[str] = set()
    for specification_kind in ("implementation", "feature_pr"):
        for specification_path in _iter_specification_paths(
            repo_root, specification_kind
        ):
            try:
                raw_spec = _load_yaml_mapping(
                    specification_path.read_text(encoding="utf-8")
                )
            except Exception:  # noqa: BLE001
                continue

            for raw_feature in parse_changelog_sequence(raw_spec.get("features")):
                feature = parse_changelog_mapping(raw_feature)
                feature_id = _normalize_entity_id(
                    None if feature.get("id") is None else str(feature.get("id"))
                )
                if feature_id is not None:
                    feature_ids.add(feature_id)

    return feature_ids


def _load_known_proposed_pr_ids(repo_root: Path) -> set[str]:
    return {
        proposed_pr_id
        for proposed_pr_id in _load_specification_ids(repo_root, "proposed_pr")
        if proposed_pr_id is not None
    }
