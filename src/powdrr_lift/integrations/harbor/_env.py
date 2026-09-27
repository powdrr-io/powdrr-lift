"""Environment parsing helpers that do not depend on Harbor or Pier."""

from __future__ import annotations


def env_flag_is_enabled(value: str | None) -> bool:
    """Return whether an environment value uses a supported true spelling."""
    return (value or "").casefold() in {"1", "true", "yes"}
