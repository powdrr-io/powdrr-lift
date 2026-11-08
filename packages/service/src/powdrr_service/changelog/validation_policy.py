"""Service-owned policy for validating changelog structure and relationships."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from powdrr_service.changelog import ChangeEntity, ChangeFile, ChangeLog, RelatedSection
from powdrr_service.changelog.template import is_structured_document_path


@dataclass(frozen=True, slots=True)
class ChangelogValidationIssue:
    code: str
    message: str
    path: str | None = None


def normalize_changelog_id(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def parse_changelog_mapping(raw_data: object | None) -> dict[str, Any]:
    if raw_data is None:
        return {}

    if not isinstance(raw_data, Mapping):
        raise ValueError("Expected a mapping in the validation report structure.")

    return dict(raw_data)


def parse_changelog_sequence(raw_data: object | None) -> Sequence[object]:
    if raw_data is None:
        return ()

    if isinstance(raw_data, (str, bytes)) or not isinstance(raw_data, Sequence):
        raise ValueError("Expected a sequence in the validation report structure.")

    return raw_data


def validate_v2_file_sections(
    raw_change_log: Mapping[str, Any],
    issues: list[ChangelogValidationIssue],
    *,
    available_files: set[str],
    available_entity_ids: set[str],
    available_decision_ids: set[str],
    available_invariant_ids: set[str],
    available_guidance_ids: set[str],
    available_proposed_pr_ids: set[str],
    available_proposed_pr_detail_ids: set[str],
    available_rationale_ids: set[str],
) -> None:
    for section_name in ("structured_files", "files"):
        if section_name not in raw_change_log:
            issues.append(
                ChangelogValidationIssue(
                    code="missing_required_section",
                    message=(
                        f"Version 2 changelogs must include the {section_name} section."
                    ),
                    path=section_name,
                )
            )

    seen_structured_files: set[str] = set()
    for structured_file_index, raw_structured_file in enumerate(
        parse_changelog_sequence(raw_change_log.get("structured_files")),
        start=1,
    ):
        if isinstance(raw_structured_file, Mapping):
            issues.append(
                ChangelogValidationIssue(
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
                ChangelogValidationIssue(
                    code="structured_file_entry_missing_path",
                    message=(
                        f"Structured file {structured_file_index} must include a path."
                    ),
                    path=None,
                )
            )
            continue

        if not is_structured_document_path(structured_file_path):
            issues.append(
                ChangelogValidationIssue(
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
                ChangelogValidationIssue(
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
        parse_changelog_sequence(raw_change_log.get("files")),
        start=1,
    ):
        file_data = parse_changelog_mapping(raw_file_change)
        file_path = (
            None if file_data.get("path") is None else str(file_data.get("path"))
        )

        if "entities" in file_data:
            issues.append(
                ChangelogValidationIssue(
                    code="file_entry_entities_not_allowed",
                    message=(
                        f"File change {file_change_index} must move entity ids into "
                        "related.entities and remove the top-level entities field."
                    ),
                    path=file_path,
                )
            )

        if file_path is not None and is_structured_document_path(file_path):
            issues.append(
                ChangelogValidationIssue(
                    code="file_entry_structured_path_not_allowed",
                    message=(
                        f"File change {file_change_index} points to a "
                        "structured YAML document path. Move it to structured_files "
                        "and remove its span metadata."
                    ),
                    path=file_path,
                )
            )

        if file_path is None:
            continue

        raw_rationale = file_data.get("rationale")
        if raw_rationale is None:
            file_rationale = None
        else:
            file_rationale = str(raw_rationale).strip() or None
        validate_quoted_rationale_ids(
            rationale=file_rationale,
            available_ids=available_rationale_ids,
            issues=issues,
            code="file_entry_unknown_rationale_id",
            path=file_path,
            subject_label=f"File change {file_change_index}",
        )

        related = file_data.get("related")
        if "related" not in file_data or not isinstance(related, Mapping):
            continue

        if "prs" in related:
            issues.append(
                ChangelogValidationIssue(
                    code="file_entry_legacy_related_prs_not_allowed",
                    message=(
                        f"File change {file_change_index} must use "
                        "related.proposed_prs instead of related.prs."
                    ),
                    path=file_path,
                )
            )

        has_nonempty_related_value = False
        for related_key, available_ids in (
            ("files", available_files),
            ("entities", available_entity_ids),
            ("decisions", available_decision_ids),
            ("invariants", available_invariant_ids),
            ("guidance", available_guidance_ids),
            ("proposed_prs", available_proposed_pr_ids),
            ("acceptance_criteria", available_proposed_pr_detail_ids),
            ("expected_tests", available_proposed_pr_detail_ids),
            ("required_test_cases", available_proposed_pr_detail_ids),
            ("expected_outcomes", available_proposed_pr_detail_ids),
            ("non_goals", available_proposed_pr_detail_ids),
            ("risks", available_proposed_pr_detail_ids),
        ):
            if related_key not in related:
                continue

            raw_related_values = related.get(related_key)
            if raw_related_values is None:
                continue

            if not isinstance(raw_related_values, Sequence) or isinstance(
                raw_related_values, (str, bytes)
            ):
                issues.append(
                    ChangelogValidationIssue(
                        code="file_entry_invalid_related_section",
                        message=(
                            f"File change {file_change_index} related.{related_key} "
                            "must be a list of ids."
                        ),
                        path=f"{file_path}.related.{related_key}",
                    )
                )
                continue

            if len(raw_related_values) == 0:
                issues.append(
                    ChangelogValidationIssue(
                        code="file_entry_empty_related_list",
                        message=(
                            f"File change {file_change_index} related.{related_key} "
                            "is empty. Remove it unless it points at a current id."
                        ),
                        path=f"{file_path}.related.{related_key}",
                    )
                )
                continue

            has_nonempty_related_value = True
            for related_index, raw_related_value in enumerate(raw_related_values):
                if related_key == "proposed_prs" and isinstance(
                    raw_related_value, Mapping
                ):
                    issues.append(
                        ChangelogValidationIssue(
                            code="file_entry_related_invalid_reference_shape",
                            message=(
                                f"File change {file_change_index} related."
                                f"{related_key}[{related_index}] must be a plain "
                                "list of ids."
                            ),
                            path=(
                                f"{file_path}.related.{related_key}[{related_index}]"
                            ),
                        )
                    )
                    continue

                related_id = normalize_related_reference_value(raw_related_value)
                if related_id is None:
                    issues.append(
                        ChangelogValidationIssue(
                            code="file_entry_related_missing_id",
                            message=(
                                f"File change {file_change_index} related."
                                f"{related_key}[{related_index}] must include an id."
                            ),
                            path=f"{file_path}.related.{related_key}[{related_index}]",
                        )
                    )
                    continue

                if related_id not in available_ids:
                    issues.append(
                        ChangelogValidationIssue(
                            code="unknown_related_reference",
                            message=(
                                f"File change {file_change_index} related."
                                f"{related_key} references id {related_id!r}, but "
                                "that id is not current."
                            ),
                            path=f"{file_path}.related.{related_key}[{related_index}]",
                        )
                    )

        if "related" in file_data and not has_nonempty_related_value:
            issues.append(
                ChangelogValidationIssue(
                    code="file_entry_empty_related",
                    message=(
                        f"File change {file_change_index} has an empty related "
                        "block. Remove related unless it includes at least one "
                        "current reference."
                    ),
                    path=file_path,
                )
            )


def validate_v2_verification(
    raw_change_log: Mapping[str, Any],
    issues: list[ChangelogValidationIssue],
    *,
    available_files: set[str],
) -> None:
    """Validate checkable test obligations and their recorded command results."""
    raw_verification = raw_change_log.get("verification")
    if raw_verification is None:
        return
    if not isinstance(raw_verification, Mapping):
        issues.append(
            ChangelogValidationIssue(
                "verification_invalid",
                "The verification section must be a mapping.",
                "verification",
            )
        )
        return

    seen_ids: set[str] = set()
    for index, raw_case in enumerate(
        parse_changelog_sequence(raw_verification.get("required_test_cases")),
        start=1,
    ):
        path = f"verification.required_test_cases[{index - 1}]"
        if not isinstance(raw_case, Mapping):
            issues.append(
                ChangelogValidationIssue(
                    "required_test_case_invalid",
                    f"Required test case {index} must be a mapping.",
                    path,
                )
            )
            continue
        case_id = normalize_changelog_id(
            None if raw_case.get("id") is None else str(raw_case.get("id"))
        )
        description = raw_case.get("description")
        status = raw_case.get("status")
        if case_id is None:
            issues.append(
                ChangelogValidationIssue(
                    "required_test_case_id_missing",
                    f"Required test case {index} must have an id.",
                    path,
                )
            )
        elif case_id in seen_ids:
            issues.append(
                ChangelogValidationIssue(
                    "verification_id_duplicate",
                    f"Verification id {case_id!r} is repeated.",
                    path,
                )
            )
        else:
            seen_ids.add(case_id)
        if not isinstance(description, str) or not description.strip():
            issues.append(
                ChangelogValidationIssue(
                    "required_test_case_description_missing",
                    f"Required test case {index} must have a description.",
                    path,
                )
            )
        if status not in {"planned", "passed", "failed", "not_run"}:
            issues.append(
                ChangelogValidationIssue(
                    "required_test_case_status_invalid",
                    f"Required test case {index} has an unsupported status.",
                    path,
                )
            )
        test_type = raw_case.get("test_type")
        if test_type is not None and test_type not in {
            "unit",
            "integration",
            "end_to_end",
            "contract",
        }:
            issues.append(
                ChangelogValidationIssue(
                    "required_test_case_type_invalid",
                    f"Required test case {index} has an unsupported test type.",
                    path,
                )
            )
        for key in ("scenario", "expected_outcome"):
            value = raw_case.get(key)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                issues.append(
                    ChangelogValidationIssue(
                        f"required_test_case_{key}_invalid",
                        f"Required test case {index} {key.replace('_', ' ')} "
                        "must be non-empty text.",
                        path,
                    )
                )
        selectors = parse_changelog_sequence(raw_case.get("selectors"))
        if not selectors or any(
            not isinstance(selector, str) or "::test_" not in selector
            for selector in selectors
        ):
            issues.append(
                ChangelogValidationIssue(
                    "required_test_case_selectors_invalid",
                    f"Required test case {index} must name one or more "
                    "pytest selectors.",
                    path,
                )
            )
        else:
            for selector in selectors:
                if not isinstance(selector, str):
                    continue
                test_path = selector.split("::", 1)[0]
                if test_path not in available_files:
                    issues.append(
                        ChangelogValidationIssue(
                            "required_test_case_file_missing",
                            f"Required test case {case_id or index} points to "
                            f"{test_path!r}, which is absent from the files section.",
                            path,
                        )
                    )

    for index, raw_run in enumerate(
        parse_changelog_sequence(raw_verification.get("validation_runs")),
        start=1,
    ):
        path = f"verification.validation_runs[{index - 1}]"
        if not isinstance(raw_run, Mapping):
            issues.append(
                ChangelogValidationIssue(
                    "validation_run_invalid",
                    f"Validation run {index} must be a mapping.",
                    path,
                )
            )
            continue
        run_id = normalize_changelog_id(
            None if raw_run.get("id") is None else str(raw_run.get("id"))
        )
        if run_id is None:
            issues.append(
                ChangelogValidationIssue(
                    "validation_run_id_missing",
                    f"Validation run {index} must have an id.",
                    path,
                )
            )
        elif run_id in seen_ids:
            issues.append(
                ChangelogValidationIssue(
                    "verification_id_duplicate",
                    f"Verification id {run_id!r} is repeated.",
                    path,
                )
            )
        else:
            seen_ids.add(run_id)
        command = raw_run.get("command")
        if (
            not isinstance(command, Sequence)
            or isinstance(command, (str, bytes))
            or not command
            or any(not isinstance(token, str) or not token for token in command)
        ):
            issues.append(
                ChangelogValidationIssue(
                    "validation_run_command_invalid",
                    f"Validation run {index} command must be a non-empty "
                    "argument array.",
                    path,
                )
            )
        run_status = raw_run.get("status")
        if run_status not in {"passed", "failed", "blocked", "timed_out", "not_run"}:
            issues.append(
                ChangelogValidationIssue(
                    "validation_run_status_invalid",
                    f"Validation run {index} has an unsupported status.",
                    path,
                )
            )
        returncode = raw_run.get("returncode")
        if returncode is not None and (
            not isinstance(returncode, int) or isinstance(returncode, bool)
        ):
            issues.append(
                ChangelogValidationIssue(
                    "validation_run_returncode_invalid",
                    f"Validation run {index} returncode must be an integer or null.",
                    path,
                )
            )
        elif run_status == "passed" and returncode != 0:
            issues.append(
                ChangelogValidationIssue(
                    "validation_run_pass_mismatch",
                    f"Validation run {index} is marked passed without returncode 0.",
                    path,
                )
            )


def validate_v2_file_change(
    *,
    issues: list[ChangelogValidationIssue],
    file_change: ChangeFile,
    file_change_index: int,
) -> None:
    file_types = {"added", "deleted", "modified", "renamed", "copied"}
    if file_change.path is None or file_change.path.strip() == "":
        issues.append(
            ChangelogValidationIssue(
                code="file_entry_missing_path",
                message=f"File change {file_change_index} must include a path.",
                path=None,
            )
        )
        return

    if is_structured_document_path(file_change.path):
        issues.append(
            ChangelogValidationIssue(
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
            ChangelogValidationIssue(
                code="file_entry_missing_type",
                message=(f"File change {file_change_index} must include a file type."),
                path=file_change.path,
            )
        )
    elif file_change.type not in file_types:
        issues.append(
            ChangelogValidationIssue(
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
            ChangelogValidationIssue(
                code="file_entry_missing_span",
                message=f"File change {file_change_index} must include a span.",
                path=file_change.path,
            )
        )
    elif file_change.span.start_line > file_change.span.end_line:
        issues.append(
            ChangelogValidationIssue(
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
            ChangelogValidationIssue(
                code="file_entry_missing_summary",
                message=f"File change {file_change_index} must include a summary.",
                path=file_change.path,
            )
        )

    if file_change.rationale is None or str(file_change.rationale).strip() == "":
        issues.append(
            ChangelogValidationIssue(
                code="file_entry_missing_rationale",
                message=(f"File change {file_change_index} must include a rationale."),
                path=file_change.path,
            )
        )

    for entity_index, entity in enumerate(
        list(file_change.entities or file_change.related.entities), start=1
    ):
        if normalize_changelog_id(entity) is None:
            issues.append(
                ChangelogValidationIssue(
                    code="file_entry_entity_missing_id",
                    message=(
                        f"File change {file_change_index} entity {entity_index} "
                        "must include an id."
                    ),
                    path=file_change.path,
                )
            )


def validate_v2_entity_change(
    *,
    issues: list[ChangelogValidationIssue],
    entity_change: ChangeEntity,
    entity_change_index: int,
    allowed_entity_types: set[str] | None = None,
) -> None:
    normalized_entity_id = normalize_changelog_id(entity_change.id)
    if normalized_entity_id is None:
        issues.append(
            ChangelogValidationIssue(
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
        validate_added_entity_type(
            issues=issues,
            entity=entity_change,
            entity_id=normalized_entity_id,
            allowed_entity_types=allowed_entity_types,
        )

    if entity_change.action not in {"added", "deleted", "modified"}:
        issues.append(
            ChangelogValidationIssue(
                code="entity_action_invalid",
                message=(
                    f"Entity {normalized_entity_id} in entity change "
                    f"{entity_change_index} must be marked as added, deleted, "
                    "or modified."
                ),
                path=None,
            )
        )


def validate_added_entity_type(
    *,
    issues: list[ChangelogValidationIssue],
    entity: ChangeEntity,
    entity_id: str,
    allowed_entity_types: set[str] | None,
) -> None:
    if allowed_entity_types is None:
        return

    entity_type = entity.type.strip() if entity.type is not None else ""
    if entity_type == "":
        issues.append(
            ChangelogValidationIssue(
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
            ChangelogValidationIssue(
                code="entity_type_not_allowed",
                message=(
                    f"Entity {entity_id} is marked added with unsupported type "
                    f"{entity_type!r}."
                ),
                path=None,
            )
        )


def validate_unique_changelog_ids(
    change_log: ChangeLog,
    issues: list[ChangelogValidationIssue],
) -> None:
    seen_ids: dict[str, tuple[str, int]] = {}

    def _register(raw_id: str | None, section: str, item_index: int) -> None:
        normalized_id = normalize_changelog_id(raw_id)
        if normalized_id is None:
            return

        previous = seen_ids.get(normalized_id)
        if previous is not None:
            issues.append(
                ChangelogValidationIssue(
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

    _register(change_log.change_id, "change", 0)

    for index, feature in enumerate(change_log.feature_changes or [], start=1):
        _register(feature.id, "feature", index)

    for index, proposed_pr in enumerate(change_log.proposed_prs or [], start=1):
        _register(proposed_pr.id, "proposed PR", index)

    for index, test_case in enumerate(
        change_log.verification.required_test_cases, start=1
    ):
        _register(test_case.id, "required test case", index)

    for index, validation_run in enumerate(
        change_log.verification.validation_runs, start=1
    ):
        _register(validation_run.id, "validation run", index)


def collect_changelog_defined_ids(change_log: ChangeLog) -> set[str]:
    defined_ids: set[str] = set()
    change_id = normalize_changelog_id(change_log.change_id)
    if change_id is not None:
        defined_ids.add(change_id)
    for section in (
        change_log.decisions,
        change_log.entity_changes,
        change_log.entity_relationship_changes,
        change_log.invariant_changes,
        change_log.guidance_changes,
        change_log.feature_changes,
        change_log.proposed_prs,
    ):
        for item in section:
            normalized_id = normalize_changelog_id(getattr(item, "id", None))
            if normalized_id is not None:
                defined_ids.add(normalized_id)
    verification_items: list[object] = [
        *change_log.verification.required_test_cases,
        *change_log.verification.validation_runs,
    ]
    for verification_item in verification_items:
        normalized_id = normalize_changelog_id(getattr(verification_item, "id", None))
        if normalized_id is not None:
            defined_ids.add(normalized_id)
    return defined_ids


def extract_quoted_ids(text: str | None) -> list[str]:
    if text is None:
        return []

    return [
        quoted_id
        for quoted_id in re.findall(r'"([^"]+)"', text)
        if normalize_changelog_id(quoted_id) is not None
    ]


def validate_quoted_rationale_ids(
    *,
    rationale: str | None,
    available_ids: set[str],
    issues: list[ChangelogValidationIssue],
    code: str,
    path: str | None,
    subject_label: str,
) -> None:
    for quoted_id in extract_quoted_ids(rationale):
        if quoted_id not in available_ids:
            issues.append(
                ChangelogValidationIssue(
                    code=code,
                    message=(
                        f"{subject_label} rationale references unknown id "
                        f"{quoted_id!r}."
                    ),
                    path=path,
                )
            )


def normalize_related_reference_value(raw_value: object) -> str | None:
    if isinstance(raw_value, Mapping):
        raw_value = raw_value.get("id")
    if raw_value is None:
        return None
    return normalize_changelog_id(str(raw_value))


def validate_v2_lifecycle_item(
    *,
    issues: list[ChangelogValidationIssue],
    change_index: int,
    item_index: int,
    item_kind: str,
    item_id: str | None,
    description: str | None,
    rationale: str | None,
    action: str | None,
    related: RelatedSection | None,
    available_files: set[str],
    available_entities: set[str],
    available_invariants: set[str],
    available_guidance: set[str],
    available_proposed_pr_detail_ids: set[str],
    available_rationale_ids: set[str],
    path: str | None,
) -> None:
    normalized_item_id = normalize_changelog_id(item_id)
    if normalized_item_id is None:
        issues.append(
            ChangelogValidationIssue(
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
            ChangelogValidationIssue(
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
            ChangelogValidationIssue(
                code=f"{item_kind}_invalid_action",
                message=(
                    f"Change {change_index} {item_kind} entry {item_index} must "
                    "use action added, removed, or altered."
                ),
                path=path,
            )
        )

    validate_quoted_rationale_ids(
        rationale=rationale,
        available_ids=available_rationale_ids,
        issues=issues,
        code=f"{item_kind}_unknown_rationale_id",
        path=path,
        subject_label=f"{item_kind.capitalize()} {normalized_item_id or item_index}",
    )

    if related is None:
        return

    for file_id in related.files:
        if file_id not in available_files:
            issues.append(
                ChangelogValidationIssue(
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
                ChangelogValidationIssue(
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
                ChangelogValidationIssue(
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
                ChangelogValidationIssue(
                    code=f"{item_kind}_related_unknown_guidance",
                    message=(
                        f"{item_kind.capitalize()} {normalized_item_id or item_index} "
                        f"references guidance {guidance_id}, but that guidance is "
                        "not listed in the change guidance section."
                    ),
                    path=path,
                )
            )

    for proposed_pr_id in related.proposed_prs:
        if proposed_pr_id not in available_proposed_pr_detail_ids:
            issues.append(
                ChangelogValidationIssue(
                    code=f"{item_kind}_related_unknown_proposed_pr",
                    message=(
                        f"{item_kind.capitalize()} {normalized_item_id or item_index} "
                        f"references proposed PR {proposed_pr_id}, but that "
                        "proposed PR is not listed in the current proposed PR "
                        "specs."
                    ),
                    path=path,
                )
            )

    for required_test_case_id in related.required_test_cases:
        if required_test_case_id not in available_proposed_pr_detail_ids:
            issues.append(
                ChangelogValidationIssue(
                    code=f"{item_kind}_related_unknown_required_test_case",
                    message=(
                        f"{item_kind.capitalize()} {normalized_item_id or item_index} "
                        f"references required test case {required_test_case_id}, but "
                        "that test case is not listed in the current proposed PR "
                        "specs."
                    ),
                    path=path,
                )
            )


def validate_v2_state_change(
    *,
    issues: list[ChangelogValidationIssue],
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
    normalized_item_id = normalize_changelog_id(item_id)
    item_label = section_name[:-1] if section_name.endswith("s") else section_name
    path_prefix = f"{section_name}[{item_index - 1}]"

    if normalized_item_id is None:
        issues.append(
            ChangelogValidationIssue(
                code=f"{item_label}_missing_id",
                message=f"Change {item_index} {item_label} entry must include an id.",
                path=f"{path_prefix}.id",
            )
        )
    elif normalized_item_id not in available_ids:
        issues.append(
            ChangelogValidationIssue(
                code=unavailable_id_code,
                message=unavailable_id_message.format(item_id=normalized_item_id),
                path=f"{path_prefix}.id",
            )
        )

    if state not in {"in_progress", "completed"}:
        issues.append(
            ChangelogValidationIssue(
                code=invalid_state_code,
                message=invalid_state_message,
                path=f"{path_prefix}.state",
            )
        )
