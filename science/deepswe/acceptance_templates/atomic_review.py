"""Split a criterion without repairing it, then review each assertion in isolation."""

from __future__ import annotations

from functools import partial
from typing import Any

from .common import (
    TEXT,
    Recorder,
    array_schema,
    checked_rows,
    object_schema,
    require_text,
)
from .evaluation import (
    SUPPORT_PROMPT,
    SUPPORT_SCHEMA,
    rendered_criteria,
    validate_support,
)

EXTRACT_SCHEMA = object_schema(assertions=array_schema(object_schema(text=TEXT)))
EXTRACT_PROMPT = """Split the supplied acceptance criterion into its individually
mandated assertions. Preserve every condition, quantifier, exception, operation
scope, alternative, and outcome. Each assertion must be independently
understandable, with the conditions necessary to interpret it. Do not repair,
justify, weaken, or omit an assertion because it seems incorrect. Split distinct
outcomes even when they share a setup. Do not add assertions. Return JSON only."""
ASSERTION_SUPPORT_PROMPT = (
    SUPPORT_PROMPT
    + """
This request contains one assertion. Decide only whether this entire assertion
is required by the instruction. Plausible behavior is not mandated behavior.
Consider whether an implementation could satisfy the instruction while violating
this assertion. If it could, describe that alternative in the rationale and
reject support for the assertion. Definitions elsewhere in the full instruction
still apply. Do not import conventional behavior absent from the source."""
)


def combined_status(statuses: list[str]) -> str:
    if not statuses:
        raise ValueError("empty assertion review")
    if all(status == "supported" for status in statuses):
        return "supported"
    if all(status == "unsupported" for status in statuses):
        return "unsupported"
    if "uncertain" in statuses:
        return "uncertain"
    return "partial"


def review_assertions(
    task: dict[str, Any], criterion: dict[str, Any], recorder: Recorder
) -> dict[str, Any]:
    def validate(raw: dict[str, Any]) -> list[dict[str, Any]]:
        rows = checked_rows(raw, "assertions")
        if not rows:
            raise ValueError("criterion decomposition cannot be empty")
        seen = set()
        for i, row in enumerate(rows, 1):
            text = require_text(row, "text")
            if text in seen:
                raise ValueError("duplicate extracted assertion")
            seen.add(text)
            row["id"] = f"{criterion['id']}-a{i:03}"
        return rows

    assertions = recorder.call(
        f"extract-{criterion['id']}",
        EXTRACT_PROMPT,
        {"criterion_id": criterion["id"], "text": criterion["text"]},
        EXTRACT_SCHEMA,
        validate,
    )
    judgments = []
    for assertion in assertions:
        row = {**criterion, "id": assertion["id"], "text": assertion["text"]}
        result = recorder.call(
            f"support-{assertion['id']}",
            ASSERTION_SUPPORT_PROMPT,
            {**task, "criteria": rendered_criteria([row])},
            SUPPORT_SCHEMA,
            partial(validate_support, criteria=[row], task=task),
        )
        judgments.append({**assertion, **result[0]})
    return {
        "criterion_id": criterion["id"],
        "criterion_text": criterion["text"],
        "assertions": judgments,
        "derived_status": combined_status([row["status"] for row in judgments]),
        "limitation": "Decomposition and entailment are model judgments. Splitting "
        "may omit meaning; the original criterion and all extracted assertions "
        "remain available for inspection. Not a human review.",
    }
