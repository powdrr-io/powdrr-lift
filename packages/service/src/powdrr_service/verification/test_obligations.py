"""Deterministic obligation IDs and coverage checks shared by test workflows."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any


class ObligationError(ValueError):
    """A required test obligation is malformed or has no evidence of coverage."""


def acceptance_criteria_contract(criteria: Sequence[Any]) -> list[dict[str, str]]:
    """Assign stable, ordered IDs to the exact acceptance criteria supplied."""
    if (
        not isinstance(criteria, Sequence)
        or isinstance(criteria, (str, bytes))
        or not criteria
    ):
        raise ObligationError("test coverage requires acceptance criteria")
    obligations: list[dict[str, str]] = []
    for index, criterion in enumerate(criteria, start=1):
        if not isinstance(criterion, str) or not criterion.strip():
            raise ObligationError(
                f"acceptance criterion {index} must be a non-empty string"
            )
        obligations.append(
            {
                "obligation_id": f"AC-{index:02d}",
                "source_text": criterion.strip(),
            }
        )
    return obligations


def missing_obligation_ids(
    required_ids: Sequence[str], covered_ids: Sequence[str] | set[str]
) -> list[str]:
    """Return deterministic, sorted required IDs without linked evidence."""
    return sorted(set(required_ids) - set(covered_ids))
