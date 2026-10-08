"""Validate and render boolean combinations produced by instruction splitting."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

MAX_BOOLEAN_EXPRESSION_DEPTH = 16


class BooleanExpressionError(ValueError):
    """Raised when a split expression is malformed or omits an atom."""


def validate_boolean_expression(expression: Any, atom_count: int) -> dict[str, Any]:
    """Return a normalized tree whose leaves cover every one-based atom once."""
    if atom_count < 2:
        raise BooleanExpressionError("boolean split must contain at least two atoms")
    leaves: list[int] = []

    def visit(node: Any, depth: int = 0) -> dict[str, Any]:
        if depth > MAX_BOOLEAN_EXPRESSION_DEPTH:
            raise BooleanExpressionError("boolean expression is too deeply nested")
        if not isinstance(node, Mapping):
            raise BooleanExpressionError("boolean expression node must be an object")
        if set(node) == {"atom"}:
            atom = node["atom"]
            if (
                not isinstance(atom, int)
                or isinstance(atom, bool)
                or not 1 <= atom <= atom_count
            ):
                raise BooleanExpressionError("boolean expression atom is invalid")
            leaves.append(atom)
            return {"atom": atom}
        operator = node.get("op")
        if operator == "not":
            if set(node) != {"op", "arg"}:
                raise BooleanExpressionError("not node must contain exactly arg")
            return {"op": "not", "arg": visit(node["arg"], depth + 1)}
        if operator in {"implies", "if_then_else"}:
            if set(node) != {"op", "args"}:
                raise BooleanExpressionError("implies node must contain args")
            args = node["args"]
            expected_count = 3 if operator == "if_then_else" else 2
            if not isinstance(args, list) or len(args) != expected_count:
                raise BooleanExpressionError(
                    f"{operator} node must have {expected_count} args"
                )
            return {
                "op": operator,
                "args": [visit(item, depth + 1) for item in args],
            }
        if operator in {"and", "or", "xor"}:
            if set(node) != {"op", "args"}:
                raise BooleanExpressionError("logical node must contain args")
            args = node["args"]
            if not isinstance(args, list) or len(args) < 2:
                raise BooleanExpressionError(
                    f"{operator} node must contain at least two args"
                )
            return {
                "op": operator,
                "args": [visit(item, depth + 1) for item in args],
            }
        raise BooleanExpressionError("boolean expression has an unsupported operator")

    normalized = visit(expression)
    counts = Counter(leaves)
    if set(counts) != set(range(1, atom_count + 1)) or any(
        count != 1 for count in counts.values()
    ):
        raise BooleanExpressionError(
            "boolean expression must reference every atom exactly once"
        )
    return normalized


def render_boolean_sentence(
    statements: Sequence[str], expression: Any
) -> tuple[dict[str, Any], str]:
    """Render a validated expression with explicit parentheses for mixed operators."""
    normalized = validate_boolean_expression(expression, len(statements))

    def render(node: Mapping[str, Any]) -> tuple[str, str | None]:
        if "atom" in node:
            text = statements[node["atom"] - 1].strip()
            return text.rstrip(" .!?;:"), None
        operator = str(node["op"])
        if operator == "not":
            child, _ = render(node["arg"])
            return f"It is not the case that ({child})", "not"
        children = [render(item) for item in node["args"]]
        if operator == "implies":
            left, right = (item[0] for item in children)
            return f"If ({left}), then ({right})", "implies"
        if operator == "if_then_else":
            condition, when_true, when_false = (item[0] for item in children)
            return (
                f"If ({condition}), then ({when_true}); otherwise, ({when_false})",
                "if_then_else",
            )
        rendered: list[str] = []
        for child_text, child_op in children:
            if child_op is not None and child_op != operator:
                child_text = f"({child_text})"
            rendered.append(child_text)
        if operator == "xor":
            sentence = "Exactly one of these holds: " + "; ".join(rendered)
        else:
            sentence = f" {operator} ".join(rendered)
        return sentence, operator

    text, _ = render(normalized)
    sentence = text
    if not sentence.endswith((".", "!", "?")):
        sentence += "."
    return normalized, sentence
