"""Deterministic first-pass resolvers for source semantic decisions."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DeterministicResolution:
    value: str
    evidence: str


def resolve_deterministic_source_decision(
    decision_kind: str, proposition_text: str
) -> DeterministicResolution | None:
    """Resolve only exact high-precision lexical cases.

    ``None`` means that no deterministic rule applies. It is not an unresolved
    semantic result; the configured provider cascade must continue.
    """
    normalized = " ".join(proposition_text.casefold().split())
    if decision_kind == "polarity":
        return _resolve_polarity(normalized)
    if decision_kind == "quantifier":
        return _resolve_quantifier(normalized)
    if decision_kind == "requirement_strength":
        return _resolve_requirement_strength(normalized)
    return None


def _resolve_polarity(text: str) -> DeterministicResolution | None:
    prohibited = _first_match(
        text,
        (
            r"\bdo not\b",
            r"\bdon't\b",
            r"\bmust not\b",
            r"\bshould not\b",
            r"\bshall not\b",
            r"\bmay not\b",
            r"\bnever\b",
        ),
    )
    if prohibited is not None:
        return DeterministicResolution("prohibited", prohibited)
    permitted = _first_match(text, (r"\bmay\b",))
    if permitted is not None:
        return DeterministicResolution("permitted", permitted)
    required = _first_match(
        text,
        (r"\bmust\b", r"\bshould\b", r"\bshall\b", r"\brequired to\b"),
    )
    if required is not None:
        return DeterministicResolution("required", required)
    return None


def _resolve_quantifier(text: str) -> DeterministicResolution | None:
    every = _first_match(text, (r"\ball\b", r"\bevery\b", r"\balways\b"))
    if every is not None:
        return DeterministicResolution("every", every)
    some = _first_match(text, (r"\bsome\b",))
    if some is not None:
        return DeterministicResolution("some", some)
    return None


def _resolve_requirement_strength(text: str) -> DeterministicResolution | None:
    must = _first_match(text, (r"\bmust\b", r"\bmust not\b"))
    if must is not None:
        return DeterministicResolution("must", must)
    should = _first_match(text, (r"\bshould\b", r"\bshould not\b"))
    if should is not None:
        return DeterministicResolution("should", should)
    may = _first_match(text, (r"\bmay\b", r"\bmay not\b"))
    if may is not None:
        return DeterministicResolution("may", may)
    return None


def _first_match(text: str, patterns: tuple[str, ...]) -> str | None:
    matches = [match for pattern in patterns if (match := re.search(pattern, text))]
    if not matches:
        return None
    selected = min(matches, key=lambda item: (item.start(), -len(item.group(0))))
    return selected.group(0)


__all__ = ["DeterministicResolution", "resolve_deterministic_source_decision"]
