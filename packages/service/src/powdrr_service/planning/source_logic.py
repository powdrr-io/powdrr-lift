"""Validated, source-bound Boolean expressions for planning obligations."""

from __future__ import annotations

import unicodedata
from collections.abc import Mapping
from typing import Any

MAX_SOURCE_LOGIC_DEPTH = 8
MAX_SOURCE_LOGIC_NODES = 63
MAX_SOURCE_LOGIC_CHILDREN = 8
MAX_SOURCE_LOGIC_QUOTE_CHARS = 2_000


class SourceLogicError(ValueError):
    """A source logic expression is malformed or not pinned to its evidence."""


def parse_source_logic(
    value: Any,
    *,
    source_text_by_ref: Mapping[str, str],
    allowed_source_refs: set[str],
) -> dict[str, Any]:
    """Validate a bounded Boolean tree and bind every leaf to an exact source quote."""
    state = {"nodes": 0}
    parsed = _parse_node(
        value,
        pointer="/source_logic",
        depth=1,
        state=state,
        source_text_by_ref=source_text_by_ref,
        allowed_source_refs=allowed_source_refs,
    )
    if parsed["op"] == "atom":
        raise SourceLogicError(
            "source_logic must express a relationship between clauses"
        )
    return parsed


def _parse_node(
    value: Any,
    *,
    pointer: str,
    depth: int,
    state: dict[str, int],
    source_text_by_ref: Mapping[str, str],
    allowed_source_refs: set[str],
) -> dict[str, Any]:
    if depth > MAX_SOURCE_LOGIC_DEPTH:
        raise SourceLogicError(f"{pointer} exceeds maximum nesting depth")
    state["nodes"] += 1
    if state["nodes"] > MAX_SOURCE_LOGIC_NODES:
        raise SourceLogicError("source_logic exceeds maximum node count")
    if not isinstance(value, Mapping):
        raise SourceLogicError(f"{pointer} must be an object")
    op = value.get("op")
    if not isinstance(op, str):
        raise SourceLogicError(f"{pointer}/op must be a string")
    if op == "atom":
        _exact_fields(value, {"op", "source_ref", "quote"}, pointer)
        source_ref = _source_ref(
            value.get("source_ref"),
            pointer=f"{pointer}/source_ref",
            source_text_by_ref=source_text_by_ref,
            allowed_source_refs=allowed_source_refs,
        )
        quote = _source_quote(
            value.get("quote"),
            source_ref=source_ref,
            pointer=f"{pointer}/quote",
            source_text_by_ref=source_text_by_ref,
        )
        return {"op": "atom", "source_ref": source_ref, "quote": quote}
    if op in {"all_of", "any_of", "ordered"}:
        _exact_fields(value, {"op", "children"}, pointer)
        children = value.get("children")
        if (
            not isinstance(children, list)
            or not 2 <= len(children) <= MAX_SOURCE_LOGIC_CHILDREN
        ):
            raise SourceLogicError(
                f"{pointer}/children must contain 2-{MAX_SOURCE_LOGIC_CHILDREN} nodes"
            )
        return {
            "op": op,
            "children": [
                _parse_node(
                    child,
                    pointer=f"{pointer}/children/{index}",
                    depth=depth + 1,
                    state=state,
                    source_text_by_ref=source_text_by_ref,
                    allowed_source_refs=allowed_source_refs,
                )
                for index, child in enumerate(children)
            ],
        }
    if op == "conditional":
        _exact_fields(
            value,
            {"op", "condition", "then"},
            pointer,
            optional={"else"},
        )
        condition = value.get("condition")
        if not isinstance(condition, Mapping):
            raise SourceLogicError(f"{pointer}/condition must be an object")
        _exact_fields(condition, {"source_ref", "quote"}, f"{pointer}/condition")
        source_ref = _source_ref(
            condition.get("source_ref"),
            pointer=f"{pointer}/condition/source_ref",
            source_text_by_ref=source_text_by_ref,
            allowed_source_refs=allowed_source_refs,
        )
        quote = _source_quote(
            condition.get("quote"),
            source_ref=source_ref,
            pointer=f"{pointer}/condition/quote",
            source_text_by_ref=source_text_by_ref,
        )
        parsed = {
            "op": "conditional",
            "condition": {"source_ref": source_ref, "quote": quote},
            "then": _parse_node(
                value.get("then"),
                pointer=f"{pointer}/then",
                depth=depth + 1,
                state=state,
                source_text_by_ref=source_text_by_ref,
                allowed_source_refs=allowed_source_refs,
            ),
        }
        if "else" in value:
            parsed["else"] = _parse_node(
                value["else"],
                pointer=f"{pointer}/else",
                depth=depth + 1,
                state=state,
                source_text_by_ref=source_text_by_ref,
                allowed_source_refs=allowed_source_refs,
            )
        return parsed
    raise SourceLogicError(
        f"{pointer}/op must be atom, all_of, any_of, ordered, or conditional"
    )


def _source_ref(
    value: Any,
    *,
    pointer: str,
    source_text_by_ref: Mapping[str, str],
    allowed_source_refs: set[str],
) -> str:
    if (
        not isinstance(value, str)
        or value not in allowed_source_refs
        or value not in source_text_by_ref
    ):
        raise SourceLogicError(f"{pointer} must cite a supplied source anchor")
    return value


def _source_quote(
    value: Any,
    *,
    source_ref: str,
    pointer: str,
    source_text_by_ref: Mapping[str, str],
) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SourceLogicError(f"{pointer} must be non-empty source text")
    quote = value.strip()
    if len(quote) > MAX_SOURCE_LOGIC_QUOTE_CHARS:
        raise SourceLogicError(f"{pointer} exceeds the source quote limit")
    if _normalize_quote(quote) not in _normalize_quote(source_text_by_ref[source_ref]):
        raise SourceLogicError(f"{pointer} is not an exact quote from {source_ref}")
    return quote


def _normalize_quote(value: str) -> str:
    return " ".join(unicodedata.normalize("NFC", value).split())


def _exact_fields(
    value: Mapping[str, Any],
    required: set[str],
    pointer: str,
    *,
    optional: set[str] | None = None,
) -> None:
    optional_fields = optional or set()
    if not required <= set(value) or set(value) - required - optional_fields:
        raise SourceLogicError(f"{pointer} has missing or unknown fields")


__all__ = ["SourceLogicError", "parse_source_logic"]
