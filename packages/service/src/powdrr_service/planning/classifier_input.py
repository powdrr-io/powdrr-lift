"""Stable text serialization for proposition and optional local context."""

from __future__ import annotations

from collections.abc import Mapping


def format_classifier_input(
    proposition: str, local_context: Mapping[str, str] | None = None
) -> str:
    """Serialize local context without losing the proposition being labeled."""
    if not local_context:
        return proposition

    context_labels = {
        "previous_sentence": "Previous sentence",
        "source_sentence": "Containing source sentence",
        "next_sentence": "Next sentence",
        "surrounding_text": "Surrounding source text",
    }
    context_lines = [
        f"{context_labels.get(key, key.replace('_', ' ').title())}: {value}"
        for key, value in local_context.items()
        if isinstance(value, str) and value.strip()
    ]
    if not context_lines:
        return proposition
    return (
        "Local source context:\n"
        + "\n".join(context_lines)
        + "\n\nProposition to classify:\n"
        + proposition
    )
