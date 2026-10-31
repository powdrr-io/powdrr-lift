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
    if decision_kind == "temporal_scope":
        return _resolve_temporal_scope(normalized)
    return None


def has_explicit_prohibition_directive(proposition_text: str) -> bool:
    """Return whether the source explicitly directs that product behavior be barred."""
    normalized = " ".join(proposition_text.casefold().split())
    return (
        _first_match(
            normalized,
            (
                r"\bdo not\b",
                r"\bdon't\b",
                r"\bmust not\b",
                r"\bshould not\b",
                r"\bshall not\b",
                r"\bnever\b",
                r"\bout of scope\b",
                r"\bnot required\b",
            ),
        )
        is not None
    )


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
    some = _first_match(
        text,
        (r"\bsome\b", r"\bseveral\b", r"\ba few\b", r"\bat least one\b"),
    )
    if some is not None:
        return DeterministicResolution("some", some)
    one = _first_match(text, (r"\b(?:only\s+|exactly\s+)?one\b", r"\ba single\b"))
    if one is not None:
        return DeterministicResolution("one", one)
    return DeterministicResolution("unspecified", "no explicit quantifier")


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
    return DeterministicResolution("unspecified", "no explicit modal")


def _resolve_temporal_scope(text: str) -> DeterministicResolution:
    """Default to unspecified unless the source explicitly marks time or events."""
    current = _first_match(
        text,
        (
            r"\bcurrently\b",
            r"\bcurrent(?:ly)?\s+(?:version|release)\b",
            r"\btoday\b",
            r"\bat present\b",
        ),
    )
    future = _first_match(
        text,
        (
            r"\bfuture\b",
            r"\bnext\s+(?:version|release)\b",
            r"\bwill\b",
            r"\bgoing forward\b",
        ),
    )
    if current is not None and future is not None:
        return DeterministicResolution("current_and_future", current + "; " + future)
    if future is not None:
        return DeterministicResolution("future", future)
    if current is not None:
        return DeterministicResolution("current", current)
    event = _first_match(
        text,
        (
            r"\bwhen\b",
            r"\bwhenever\b",
            r"\bon\s+(?:entry|exit|failure|success)\b",
            r"\bduring\b",
            r"\buntil\b",
        ),
    )
    if event is not None:
        return DeterministicResolution("event_bound", event)
    return DeterministicResolution("unspecified", "no explicit temporal marker")


def _first_match(text: str, patterns: tuple[str, ...]) -> str | None:
    matches = [match for pattern in patterns if (match := re.search(pattern, text))]
    if not matches:
        return None
    selected = min(matches, key=lambda item: (item.start(), -len(item.group(0))))
    return selected.group(0)


__all__ = [
    "DeterministicResolution",
    "has_explicit_prohibition_directive",
    "resolve_deterministic_source_decision",
]
