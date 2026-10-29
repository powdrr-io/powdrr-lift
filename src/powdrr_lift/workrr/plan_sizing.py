"""Deterministically rebalance adjacent plan work into reviewable PR slices."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

MAX_PR_WORK_UNITS = 4
TARGET_PR_WORK_UNITS = 3
_RUNTIME = re.compile(
    r"\b(?:python|swift|rust|golang|java|javascript|typescript|kotlin|ruby|php|"
    r"scala|elixir|erlang|dart|c\+\+|c#|objective-c)\b",
    re.IGNORECASE,
)
_COMPONENT = re.compile(r"\bcomponent\s*:\s*([a-z0-9._/-]+)", re.IGNORECASE)
_PATH_RUNTIME_SUFFIXES = {
    ".sh": "shell",
    ".py": "python",
    ".pyi": "python",
    ".swift": "swift",
    ".rs": "rust",
    ".go": "golang",
    ".java": "java",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".rb": "ruby",
    ".php": "php",
    ".scala": "scala",
    ".ex": "elixir",
    ".exs": "elixir",
    ".erl": "erlang",
    ".dart": "dart",
    ".cc": "c++",
    ".cpp": "c++",
    ".cxx": "c++",
    ".h": "c++",
    ".hpp": "c++",
    ".cs": "c#",
    ".m": "objective-c",
    ".mm": "objective-c",
}
PATH_RUNTIME_SUFFIXES = dict(_PATH_RUNTIME_SUFFIXES)
CANONICAL_RUNTIMES = frozenset(PATH_RUNTIME_SUFFIXES.values())
_IMPLEMENTATION_ACTION = re.compile(
    r"\b(?:implement|create|build|develop|port|introduce|extend|add)\w*\b",
    re.IGNORECASE,
)
_CODE_ARTIFACT = re.compile(
    r"\b(?:parser|package|library|module|record|service|daemon|cli|binary|target|"
    r"provider|client|worker|adapter|implementation)\w*\b",
    re.IGNORECASE,
)


class PlanSizingError(ValueError):
    """A plan could not be represented as valid bounded PR slices."""


@dataclass(frozen=True, slots=True)
class ExplicitWorkUnit:
    """Solver inputs whose runtime and cohesion facts are already typed."""

    id: str
    component: str
    implementation_runtime: str
    acceptance_criteria_count: int


@dataclass(frozen=True, slots=True)
class ExplicitAllocation:
    """A deterministic partition of typed work-unit IDs."""

    groups: tuple[tuple[str, ...], ...]
    objective_value: int


def allocate_explicit_work_units(
    units: Sequence[ExplicitWorkUnit],
    *,
    target_units: int = TARGET_PR_WORK_UNITS,
    max_units: int = MAX_PR_WORK_UNITS,
    max_criteria: int = 5,
    max_components: int = 3,
    standalone_unit_ids: frozenset[str] = frozenset(),
) -> ExplicitAllocation:
    """Partition explicitly classified units without inspecting description text."""
    if target_units < 1 or max_units < target_units:
        raise PlanSizingError("target and maximum work-unit counts are invalid")
    if not units:
        return ExplicitAllocation((), 0)
    unit_ids = [unit.id for unit in units]
    if len(set(unit_ids)) != len(unit_ids):
        raise PlanSizingError("work-unit IDs must be unique")
    if not standalone_unit_ids <= set(unit_ids):
        raise PlanSizingError("standalone unit ID is unknown")
    if any(
        unit.acceptance_criteria_count < 0
        or unit.implementation_runtime not in {*CANONICAL_RUNTIMES, "neutral"}
        for unit in units
    ):
        raise PlanSizingError("work-unit sizing facts are invalid")

    count = len(units)
    # This is a bounded contiguous partition problem. Dynamic programming gives
    # the exact same minimum as the former CP-SAT model with predictable runtime.
    segments: list[list[tuple[int, int]]] = [[] for _ in range(count)]
    for start in range(count):
        components: set[str] = set()
        runtimes: set[str] = set()
        criteria_count = 0
        has_standalone = False
        for end in range(start + 1, min(count, start + max_units) + 1):
            unit = units[end - 1]
            has_standalone = has_standalone or unit.id in standalone_unit_ids
            components.add(unit.component)
            if unit.implementation_runtime != "neutral":
                runtimes.add(unit.implementation_runtime)
            criteria_count += unit.acceptance_criteria_count
            if (
                (has_standalone and end - start > 1)
                or criteria_count > max_criteria
                or len(components) > max_components
                or len(runtimes) > 1
            ):
                break
            segments[start].append((end, len(components) - 1))

    # For each (prefix length, group count), retain the cheapest cohesion and
    # boundary score, plus one predecessor for reconstruction.
    costs: list[dict[int, int]] = [dict() for _ in range(count + 1)]
    previous: list[dict[int, tuple[int, int]]] = [dict() for _ in range(count + 1)]
    costs[0][0] = 0
    for start in range(count):
        for end, cohesion_cost in segments[start]:
            boundary_cost = 0 if start == 0 else start + 1
            for prior_groups, prior_cost in costs[start].items():
                group_count = prior_groups + 1
                candidate = prior_cost + cohesion_cost * 1_000 + boundary_cost
                if candidate < costs[end].get(group_count, 2**63):
                    costs[end][group_count] = candidate
                    previous[end][group_count] = (start, prior_groups)

    if not costs[count]:
        raise PlanSizingError("no work-unit allocation satisfies the hard constraints")
    best_group_count = min(
        costs[count],
        key=lambda groups: (
            abs(groups * target_units - count) * 1_000_000 + costs[count][groups]
        ),
    )
    objective_value = (
        abs(best_group_count * target_units - count) * 1_000_000
        + costs[count][best_group_count]
    )
    selected_segments_reversed: list[tuple[int, int]] = []
    end = count
    group_count = best_group_count
    while end:
        start, prior_groups = previous[end][group_count]
        selected_segments_reversed.append((start, end))
        end, group_count = start, prior_groups
    selected_segments = list(reversed(selected_segments_reversed))
    return ExplicitAllocation(
        tuple(tuple(unit_ids[start:end]) for start, end in selected_segments),
        objective_value,
    )


def size_plan_items(
    items: Sequence[Mapping[str, Any]],
    *,
    target_work_units: int = TARGET_PR_WORK_UNITS,
    max_work_units: int = MAX_PR_WORK_UNITS,
) -> list[dict[str, Any]]:
    """Group compatible adjacent items near the target size with dynamic programming.

    One concrete implementation step is one work unit. The optimizer only joins
    adjacent items when their paths/components are cohesive, runtimes are
    compatible, and the merged item still fits the plan schema. It never splits
    an item or joins the dedicated CI bootstrap into product work.
    """
    if target_work_units < 1 or max_work_units < target_work_units:
        raise PlanSizingError(
            "PR sizing target and maximum must be positive and ordered"
        )
    normalized = [dict(item) for item in items]
    if not normalized:
        return []

    # Leave malformed or already-oversized items untouched so the planner's
    # canonical contract validator can report the precise field error.
    if any(not _is_sizable_item(item, max_work_units) for item in normalized):
        return normalized

    normalized = split_mixed_runtime_items(normalized)

    item_ids: list[str] = []
    item_units: list[int] = []
    for item in normalized:
        item_id = str(item["id"])
        steps = item["implementation_steps"]
        item_ids.append(item_id)
        item_units.append(len(set(steps)))

    candidates: list[list[tuple[int, int]]] = [[] for _ in range(len(normalized))]
    for start in range(len(normalized)):
        unit_count = 0
        group = []
        for end in range(start, len(normalized)):
            group.append(normalized[end])
            unit_count += item_units[end]
            if unit_count > max_work_units:
                break
            if _merge_is_valid(group):
                cost = abs(unit_count - target_work_units) * 10_000
                candidates[start].append((end + 1, cost))

    # Group choices cover adjacent intervals, so a shortest path over item
    # positions finds the global minimum in linear time for the bounded group
    # size. Keeping the first equal-cost path makes ties reproducible.
    best_cost: list[int | None] = [None] * (len(normalized) + 1)
    previous_position: list[int | None] = [None] * (len(normalized) + 1)
    best_cost[0] = 0
    for start, options in enumerate(candidates):
        prefix_cost = best_cost[start]
        if prefix_cost is None:
            continue
        for end, cost in options:
            candidate_cost = prefix_cost + cost
            current_cost = best_cost[end]
            if current_cost is None or candidate_cost < current_cost:
                best_cost[end] = candidate_cost
                previous_position[end] = start
    if best_cost[-1] is None:
        raise PlanSizingError(
            "the deterministic PR sizing model found no valid partition"
        )
    selected_reversed: list[tuple[int, int]] = []
    end = len(normalized)
    while end:
        previous_index = previous_position[end]
        if previous_index is None:
            raise PlanSizingError("the PR sizing solver returned incomplete coverage")
        selected_reversed.append((previous_index, end - 1))
        end = previous_index
    selected = list(reversed(selected_reversed))
    if not selected or selected[0][0] != 0 or selected[-1][1] != len(normalized) - 1:
        raise PlanSizingError("the PR sizing solver returned incomplete coverage")
    for prior, current in zip(selected, selected[1:], strict=False):
        if prior[1] + 1 != current[0]:
            raise PlanSizingError("the PR sizing solver returned overlapping groups")

    old_to_new: dict[str, str] = {}
    for start, end in selected:
        output_id = item_ids[start]
        for item_id in item_ids[start : end + 1]:
            old_to_new[item_id] = output_id

    result: list[dict[str, Any]] = []
    for start, end in selected:
        group = normalized[start : end + 1]
        merged = _merge_items(group)
        dependencies: list[str] = []
        for item in group:
            raw_dependencies = item.get("dependencies", [])
            if not isinstance(raw_dependencies, list) or not all(
                isinstance(dependency, str) for dependency in raw_dependencies
            ):
                raise PlanSizingError(
                    f"plan item {item.get('id')!r} has invalid dependencies"
                )
            for dependency in raw_dependencies:
                if not isinstance(dependency, str):
                    raise PlanSizingError(
                        f"plan item {item.get('id')!r} has invalid dependencies"
                    )
                mapped = old_to_new.get(dependency, dependency)
                if mapped != merged["id"] and mapped not in dependencies:
                    dependencies.append(mapped)
        merged["dependencies"] = dependencies
        result.append(merged)
    return result


def _merge_is_valid(group: Sequence[Mapping[str, Any]]) -> bool:
    if len(group) == 1:
        return len(_item_runtimes(group[0])) <= 1
    if any("manual_prerequisite" in item for item in group):
        return False
    if any(str(item.get("id", "")).startswith("ci-bootstrap") for item in group):
        return False
    if not _has_shared_component(group):
        return False
    runtimes = [_item_runtimes(item) for item in group]
    known_runtimes = set().union(*runtimes)
    if len(known_runtimes) > 1:
        return False
    for field, limit in (
        ("scope", 3),
        ("acceptance_criteria", 5),
        ("allowed_paths", 20),
    ):
        values = _unique_group_values(group, field)
        if values is None or not values or len(values) > limit:
            return False
    steps = _unique_group_values(group, "implementation_steps")
    if steps is None or not steps or len(steps) > MAX_PR_WORK_UNITS:
        return False
    objectives: list[str] = []
    for item in group:
        objective = item.get("objective")
        if not isinstance(objective, str) or not objective.strip():
            return False
        objectives.append(objective)
    if sum(len(value) for value in objectives) + 2 * (len(group) - 1) > 4000:
        return False
    titles: list[str] = []
    for item in group:
        title = item.get("title")
        if not isinstance(title, str) or not title.strip():
            return False
        titles.append(title)
    return sum(len(value) for value in titles) + 3 * (len(group) - 1) <= 200


def _is_sizable_item(item: Mapping[str, Any], max_work_units: int) -> bool:
    item_id = item.get("id")
    title = item.get("title")
    objective = item.get("objective")
    scope = item.get("scope")
    paths = item.get("allowed_paths")
    steps = item.get("implementation_steps")
    criteria = item.get("acceptance_criteria")
    dependencies = item.get("dependencies")
    return (
        set(item)
        <= {
            "id",
            "title",
            "objective",
            "scope",
            "allowed_paths",
            "implementation_steps",
            "acceptance_criteria",
            "dependencies",
            "manual_prerequisite",
        }
        and {
            "id",
            "title",
            "objective",
            "scope",
            "allowed_paths",
            "implementation_steps",
            "acceptance_criteria",
            "dependencies",
        }
        <= set(item)
        and isinstance(title, str)
        and isinstance(objective, str)
        and isinstance(scope, list)
        and all(isinstance(value, str) for value in scope)
        and isinstance(paths, list)
        and all(isinstance(value, str) for value in paths)
        and isinstance(item_id, str)
        and bool(item_id)
        and isinstance(steps, list)
        and 1 <= len(steps) <= max_work_units
        and all(isinstance(step, str) and step.strip() for step in steps)
        and isinstance(criteria, list)
        and all(isinstance(value, str) for value in criteria)
        and isinstance(dependencies, list)
        and all(isinstance(value, str) for value in dependencies)
    )


def _has_shared_component(group: Sequence[Mapping[str, Any]]) -> bool:
    component_sets = [_item_components(item) for item in group]
    shared = set.intersection(*component_sets) if component_sets else set()
    return bool(shared)


def _item_components(item: Mapping[str, Any]) -> set[str]:
    components: set[str] = set()
    scope = item.get("scope", [])
    if isinstance(scope, list):
        for value in scope:
            if isinstance(value, str):
                components.update(
                    f"component:{match.group(1).casefold()}"
                    for match in _COMPONENT.finditer(value)
                )
    paths = item.get("allowed_paths", [])
    if isinstance(paths, list):
        for value in paths:
            if not isinstance(value, str) or value in {".", ""}:
                continue
            parts = [part.casefold() for part in value.strip("/").split("/") if part]
            if len(parts) >= 2:
                components.add(f"path:{'/'.join(parts[:2])}")
    return components


def _item_runtimes(item: Mapping[str, Any]) -> set[str]:
    runtimes: set[str] = set()
    for field in ("scope", "implementation_steps"):
        values = item.get(field, [])
        if not isinstance(values, list):
            continue
        for value in values:
            if isinstance(value, str):
                runtimes.update(_implementation_runtimes(value))
    paths = item.get("allowed_paths", [])
    if isinstance(paths, list):
        for path in paths:
            if isinstance(path, str):
                runtimes.update(_path_runtimes(path))
    return runtimes


def _implementation_runtimes(value: str) -> set[str]:
    runtimes: set[str] = set()
    for match in _RUNTIME.finditer(value):
        if re.search(r"\b(?:with|from|against|to)\s+$", value[: match.start()]):
            continue
        context = value[max(0, match.start() - 80) : match.end() + 80]
        if _IMPLEMENTATION_ACTION.search(context) and _CODE_ARTIFACT.search(context):
            runtimes.add(match.group(0).casefold())
    return runtimes


def _path_runtimes(path: str) -> set[str]:
    suffix = "." + path.rsplit(".", 1)[-1].casefold() if "." in path else ""
    runtime = _PATH_RUNTIME_SUFFIXES.get(suffix)
    return {runtime} if runtime else set()


def split_mixed_runtime_items(
    items: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Split multi-runtime items into runtime-homogeneous solver work units.

    The solver only groups these normalized units. It must never be asked to
    accept a mixed-runtime singleton as a valid PR.
    """
    split_items: list[dict[str, Any]] = []
    item_expansions: dict[str, list[str]] = {}
    used_ids = {str(item.get("id", "")) for item in items}
    for item in items:
        source_id = str(item["id"])
        runtimes = sorted(_item_runtimes(item))
        if len(runtimes) <= 1:
            split_items.append(dict(item))
            item_expansions[source_id] = [source_id]
            continue
        if "manual_prerequisite" in item:
            raise PlanSizingError(
                f"cannot split mixed-runtime item {source_id!r} with a "
                "manual_prerequisite; assign it to one runtime-specific item"
            )

        steps_by_runtime: dict[str, list[str]] = {runtime: [] for runtime in runtimes}
        for step in item["implementation_steps"]:
            step_runtimes = _implementation_runtimes(step) | _path_runtimes(step)
            if len(step_runtimes) != 1:
                raise PlanSizingError(
                    f"cannot split mixed-runtime item {source_id!r}: "
                    "each implementation "
                    "step must identify exactly one runtime"
                )
            runtime = next(iter(step_runtimes))
            if runtime not in steps_by_runtime:
                raise PlanSizingError(
                    f"mixed-runtime item {source_id!r} has a step for unexpected "
                    f"runtime {runtime!r}"
                )
            steps_by_runtime[runtime].append(step)

        if any(not steps for steps in steps_by_runtime.values()):
            missing = sorted(
                runtime for runtime, steps in steps_by_runtime.items() if not steps
            )
            raise PlanSizingError(
                f"cannot split mixed-runtime item {source_id!r}: no independently "
                f"attributed implementation step for {', '.join(missing)}"
            )

        scope_by_runtime: dict[str, list[str]] = {runtime: [] for runtime in runtimes}
        for entry in item["scope"]:
            entry_runtimes = _implementation_runtimes(entry) | _path_runtimes(entry)
            if len(entry_runtimes) > 1:
                raise PlanSizingError(
                    f"cannot split mixed-runtime item {source_id!r}: split this scope "
                    "entry into runtime-specific entries"
                )
            if not entry_runtimes:
                for runtime in runtimes:
                    scope_by_runtime[runtime].append(entry)
                continue
            runtime = next(iter(entry_runtimes))
            if runtime in scope_by_runtime:
                scope_by_runtime[runtime].append(entry)

        paths_by_runtime: dict[str, list[str]] = {runtime: [] for runtime in runtimes}
        for path in item["allowed_paths"]:
            path_runtimes = _path_runtimes(path)
            if len(path_runtimes) == 1:
                runtime = next(iter(path_runtimes))
                if runtime in paths_by_runtime:
                    paths_by_runtime[runtime].append(path)
                continue
            for runtime in runtimes:
                paths_by_runtime[runtime].append(path)

        generated_ids: list[str] = []
        for index, runtime in enumerate(runtimes):
            if not scope_by_runtime[runtime]:
                raise PlanSizingError(
                    f"cannot split mixed-runtime item {source_id!r}: no scope entry "
                    f"describes the {runtime} work"
                )
            if not paths_by_runtime[runtime]:
                raise PlanSizingError(
                    f"cannot split mixed-runtime item {source_id!r}: no allowed path "
                    f"is attributable to the {runtime} work"
                )
            generated_id = (
                source_id if index == 0 else _runtime_item_id(source_id, runtime)
            )
            if generated_id in generated_ids or (
                generated_id in used_ids and generated_id != source_id
            ):
                raise PlanSizingError(
                    f"runtime splitting produced duplicate item id {generated_id!r}"
                )
            generated_ids.append(generated_id)
            used_ids.add(generated_id)
            runtime_label = runtime.title()
            title_suffix = f" ({runtime_label})"
            title = str(item["title"])
            if len(title) + len(title_suffix) > 200:
                title = f"{title[: 200 - len(title_suffix) - 1].rstrip()}…"
            split_items.append(
                {
                    **dict(item),
                    "id": generated_id,
                    "title": f"{title}{title_suffix}",
                    "scope": scope_by_runtime[runtime],
                    "allowed_paths": paths_by_runtime[runtime],
                    "implementation_steps": steps_by_runtime[runtime],
                }
            )
        item_expansions[source_id] = generated_ids

    for item in split_items:
        dependencies = item.get("dependencies", [])
        expanded: list[str] = []
        for dependency in dependencies:
            for mapped in item_expansions.get(dependency, [dependency]):
                if mapped != item["id"] and mapped not in expanded:
                    expanded.append(mapped)
        item["dependencies"] = expanded
    return split_items


def _runtime_item_id(item_id: str, runtime: str) -> str:
    suffix = f"-{runtime}"
    return f"{item_id[: 100 - len(suffix)]}{suffix}"


def _unique_group_values(
    group: Sequence[Mapping[str, Any]], field: str
) -> list[str] | None:
    values: list[str] = []
    for item in group:
        field_values = item.get(field)
        if not isinstance(field_values, list) or not all(
            isinstance(value, str) and value.strip() for value in field_values
        ):
            return None
        for value in field_values:
            if value not in values:
                values.append(value)
    return values


def _merge_items(group: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    first = group[0]
    result = dict(first)
    result["title"] = "; ".join(str(item["title"]) for item in group)
    result["objective"] = "\n\n".join(str(item["objective"]) for item in group)
    for field in (
        "scope",
        "allowed_paths",
        "implementation_steps",
        "acceptance_criteria",
    ):
        result[field] = _unique_group_values(group, field) or []
    return result
