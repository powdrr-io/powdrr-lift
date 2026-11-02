"""Service-owned parsing policy for structured changelog documents."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import yaml

from powdrr_service.changelog.validation_policy import ChangelogValidationIssue


@dataclass(frozen=True, slots=True)
class StructuredDocumentParseResult:
    mapping: Mapping[str, Any] | None
    issues: tuple[ChangelogValidationIssue, ...] = ()


def parse_structured_document(
    content: str,
    *,
    path: str,
    expected_schema: str,
) -> StructuredDocumentParseResult:
    """Parse one structured-document artifact and enforce its schema envelope."""
    try:
        parsed = yaml.safe_load(content)
    except Exception as exc:  # noqa: BLE001
        return StructuredDocumentParseResult(
            mapping=None,
            issues=(
                ChangelogValidationIssue(
                    code="structured_file_invalid_yaml",
                    message=(
                        f"Structured file {path} could not be parsed as YAML: {exc}"
                    ),
                    path=path,
                ),
            ),
        )

    if not isinstance(parsed, Mapping):
        return StructuredDocumentParseResult(
            mapping=None,
            issues=(
                ChangelogValidationIssue(
                    code="structured_file_invalid_yaml",
                    message=(f"Structured file {path} must decode to a YAML mapping."),
                    path=path,
                ),
            ),
        )

    if parsed.get("schema") != expected_schema:
        return StructuredDocumentParseResult(
            mapping=None,
            issues=(
                ChangelogValidationIssue(
                    code="structured_file_invalid_schema",
                    message=(
                        f"Structured file {path} must define a schema value of "
                        f"{expected_schema}."
                    ),
                    path=path,
                ),
            ),
        )

    return StructuredDocumentParseResult(mapping=dict(parsed))


def qualify_structured_document_issues(
    *,
    path: str,
    validator_name: str,
    report_issues: Sequence[object],
) -> tuple[ChangelogValidationIssue, ...]:
    """Add stable changelog context to specification-validation issues."""
    qualified_issues: list[ChangelogValidationIssue] = []
    for report_issue in report_issues:
        issue_code = getattr(report_issue, "code", "structured_file_validation_failed")
        issue_message = getattr(
            report_issue, "message", "Structured file validation failed."
        )
        qualified_issues.append(
            ChangelogValidationIssue(
                code=f"structured_file_{validator_name}_{issue_code}",
                message=(
                    f"Structured file {path} failed {validator_name} validation: "
                    f"{issue_message}"
                ),
                path=path,
            )
        )
    return tuple(qualified_issues)
