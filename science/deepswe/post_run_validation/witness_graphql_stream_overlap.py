"""Replay the @stream list merge branch from the saved GraphQL solution patch."""

from __future__ import annotations

import json
from typing import Any


def _navigate_to_parent(data: Any, path: list[str | int]) -> tuple[Any, str | int]:
    target = data
    for key in path[:-1]:
        target = target[key]
    return target, path[-1]


def merge_stream_items(
    data: dict[str, Any], path: list[str | int], items: list[Any]
) -> None:
    """Mirror `_merge_stream_items` from the saved solution patch."""
    if not path:
        return
    if len(path) == 1:
        parent = data
        start_index = path[0]
    else:
        parent, start_index = _navigate_to_parent(data, path)
    if not isinstance(start_index, int):
        return

    target_list = parent if isinstance(parent, list) else None
    if target_list is None:
        for index in range(len(path) - 1, -1, -1):
            key = path[index]
            if isinstance(key, str) and isinstance(parent.get(key), list):
                target_list = parent[key]
                break
        if target_list is None:
            return

    for offset, item in enumerate(items):
        target_index = start_index + offset
        while len(target_list) <= target_index:
            target_list.append(None)
        target_list[target_index] = item


initial = {"list": ["a", "b", "c"]}
merge_stream_items(initial, ["list", 1], ["x", "y"])
overlap_result = initial["list"]

append_case = {"list": ["a", "b", "c"]}
merge_stream_items(append_case, ["list", 3], ["x", "y"])
append_result = append_case["list"]

shift_mutant = {"list": ["a", "b", "c"]}
shift_mutant["list"][1:1] = ["x", "y"]
shift_result = shift_mutant["list"]
expected = ["a", "x", "y"]

assert overlap_result == expected
assert shift_result != expected
assert append_result == ["a", "b", "c", "x", "y"]
print(
    json.dumps(
        {
            "overlap_case": {
                "initial": ["a", "b", "c"],
                "path": ["list", 1],
                "items": ["x", "y"],
                "observed": overlap_result,
                "oracle_expected": expected,
                "solution_passes": overlap_result == expected,
                "shift_mutant": shift_result,
                "shift_mutant_passes": shift_result == expected,
            },
            "append_control": append_result,
        }
    )
)
