"""Tool wrapper for the interaction source."""

from __future__ import annotations

from typing import Any

from ..interaction_log.interaction_source import InteractionSource


def interaction_source_tool(role: str, content: Any) -> dict[str, Any]:
    """Record an interaction input or output through the source."""
    source = InteractionSource()
    source.record(role, content)
    return {"status": "ok"}
