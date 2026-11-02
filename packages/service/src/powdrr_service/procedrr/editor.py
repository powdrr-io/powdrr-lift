"""Pure editing helpers for parsed procedrr documents."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Any


def apply_json_edits(
    document: Mapping[str, Any], edits: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """Apply RFC 6901-addressed add, replace, and remove edits to a copy."""
    result = deepcopy(dict(document))
    for index, edit in enumerate(edits):
        operation = edit.get("op")
        pointer = edit.get("path")
        if operation not in {"add", "replace", "remove"}:
            raise ValueError(f"edits[{index}].op must be add, replace, or remove")
        if operation in {"add", "replace"} and "value" not in edit:
            raise ValueError(f"edits[{index}].value is required for {operation}")
        if operation == "remove" and "value" in edit:
            raise ValueError(f"edits[{index}].value is not allowed for remove")
        if not isinstance(pointer, str) or not pointer.startswith("/"):
            raise ValueError(f"edits[{index}].path must be a JSON Pointer")
        segments = [_decode_pointer_segment(item) for item in pointer[1:].split("/")]
        if not segments or segments == [""]:
            raise ValueError(f"edits[{index}].path cannot target the document root")
        parent: Any = result
        for segment in segments[:-1]:
            parent = _pointer_child(parent, segment, pointer)
        final = segments[-1]
        if isinstance(parent, list):
            if operation == "add" and final == "-":
                parent.append(deepcopy(edit.get("value")))
                continue
            position = _list_index(
                final, pointer, length=len(parent), allow_end=operation == "add"
            )
            if operation == "add":
                parent.insert(position, deepcopy(edit.get("value")))
            elif operation == "replace":
                parent[position] = deepcopy(edit.get("value"))
            else:
                del parent[position]
            continue
        if not isinstance(parent, dict):
            raise ValueError(f"JSON Pointer {pointer!r} has a non-container parent")
        if operation in {"replace", "remove"} and final not in parent:
            raise ValueError(f"JSON Pointer {pointer!r} does not exist")
        if operation == "remove":
            del parent[final]
        else:
            parent[final] = deepcopy(edit.get("value"))
    return result


def _decode_pointer_segment(segment: str) -> str:
    return segment.replace("~1", "/").replace("~0", "~")


def _pointer_child(parent: Any, segment: str, pointer: str) -> Any:
    if isinstance(parent, list):
        return parent[_list_index(segment, pointer, length=len(parent))]
    if isinstance(parent, dict) and segment in parent:
        return parent[segment]
    raise ValueError(f"JSON Pointer {pointer!r} does not exist")


def _list_index(
    segment: str, pointer: str, *, length: int, allow_end: bool = False
) -> int:
    try:
        index = int(segment)
    except ValueError as exc:
        raise ValueError(f"JSON Pointer {pointer!r} has an invalid list index") from exc
    maximum = length if allow_end else length - 1
    if index < 0 or index > maximum:
        raise ValueError(f"JSON Pointer {pointer!r} has an invalid list index")
    return index


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


__all__ = ["append_step", "apply_json_edits", "set_value"]
