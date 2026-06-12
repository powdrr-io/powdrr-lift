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
]


def __getattr__(name: str) -> Any:
    if name == "create_change_log_template":
        from powdrr_lift.change_log_template import create_change_log_template

        return create_change_log_template

    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
