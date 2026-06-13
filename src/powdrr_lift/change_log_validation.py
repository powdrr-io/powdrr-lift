from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from powdrr_lift.change_log_parser import parse_change_log
from powdrr_lift.change_log_template import (
    BranchDiffEntry,
    _collect_branch_diff_entries,
    _resolve_default_branch,
    _resolve_repo_root,
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
        change.file
        for change in change_log.changes
        if change.file is not None and change.file != ""
    ]
    expected_change_entries_by_file: dict[str, list[BranchDiffEntry]] = {}
    for entry in diff_entries:
        if _is_changelog_artifact_path(entry.path):
            continue

        expected_change_entries_by_file.setdefault(entry.path, []).append(entry)

    proposed_change_entries_by_file: dict[str, list] = {}
    for change in change_log.changes:
        if change.file is None or change.file == "":
            continue

        proposed_change_entries_by_file.setdefault(change.file, []).append(change)

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
