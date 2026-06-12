from typing import Any

from powdrr_lift.change_log_parser import (
    Change,
    ChangeLog,
    Decision,
    Entity,
    Intent,
    RelationshipChange,
    Span,
    parse_change_log,
)

__all__ = [
    "Change",
    "ChangeLog",
    "Decision",
    "Entity",
    "Intent",
    "RelationshipChange",
    "Span",
    "create_change_log_template",
    "parse_change_log",
    "build_validation_report",
    "parse_validation_report",
    "validate_change_log_yaml",
    "ValidationIssue",
    "ValidationReport",
]


def __getattr__(name: str) -> Any:
    if name == "create_change_log_template":
        from powdrr_lift.change_log_template import create_change_log_template

        return create_change_log_template

    if name in {
        "build_validation_report",
        "parse_validation_report",
        "validate_change_log_yaml",
        "ValidationIssue",
        "ValidationReport",
    }:
        from powdrr_lift.change_log_validation import (
            ValidationIssue,
            ValidationReport,
            build_validation_report,
            parse_validation_report,
            validate_change_log_yaml,
        )

        return {
            "build_validation_report": build_validation_report,
            "parse_validation_report": parse_validation_report,
            "validate_change_log_yaml": validate_change_log_yaml,
            "ValidationIssue": ValidationIssue,
            "ValidationReport": ValidationReport,
        }[name]

    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
