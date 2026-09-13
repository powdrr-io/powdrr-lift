"""Pure editing helpers for parsed procedrr documents."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Any


def set_value(
    document: Mapping[str, Any], path: Sequence[str | int], value: Any
) -> dict[str, Any]:
    """Return a copy with one mapping/list path replaced."""
    if not path:
        raise ValueError("path cannot be empty")
    result = deepcopy(dict(document))
    target: Any = result
    for segment in path[:-1]:
        target = target[segment]
    target[path[-1]] = deepcopy(value)
    return result


def append_step(
    document: Mapping[str, Any], path: Sequence[str | int], step: Mapping[str, Any]
) -> dict[str, Any]:
    """Return a copy with one declarative step appended to a steps list."""
    result = deepcopy(dict(document))
    target: Any = result
    for segment in path:
        target = target[segment]
    if not isinstance(target, list):
        raise ValueError("path must identify a steps list")
    target.append(deepcopy(dict(step)))
    return result


__all__ = ["append_step", "set_value"]
