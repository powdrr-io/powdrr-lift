"""Deterministically rebalance adjacent plan work into reviewable PR slices."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ortools.sat.python import cp_model

MAX_PR_WORK_UNITS = 4
TARGET_PR_WORK_UNITS = 3
_RUNTIME = re.compile(
    r"\b(?:python|swift|rust|golang|java|javascript|typescript|kotlin|ruby|php|"
    r"scala|elixir|erlang|dart|c\+\+|c#|objective-c)\b",
    re.IGNORECASE,
)
_COMPONENT = re.compile(r"\bcomponent\s*:\s*([a-z0-9._/-]+)", re.IGNORECASE)
_PATH_RUNTIME_SUFFIXES = {
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
    model = cp_model.CpModel()
    boundaries = [model.new_bool_var(f"boundary_{i}") for i in range(count + 1)]
    model.add(boundaries[0] == 1)
    model.add(boundaries[count] == 1)
    group_count = model.new_int_var(1, count, "group_count")
    model.add(group_count == sum(boundaries[1:count]) + 1)
    segments: dict[tuple[int, int], Any] = {}
    cohesion_costs: list[Any] = []
    for start in range(count):
        for end in range(start + 1, min(count, start + max_units) + 1):
            chunk = units[start:end]
            if any(unit.id in standalone_unit_ids for unit in chunk) and len(chunk) > 1:
                continue
            if sum(unit.acceptance_criteria_count for unit in chunk) > max_criteria:
                continue
            components = {unit.component for unit in chunk}
            if len(components) > max_components:
                continue
            runtimes = {
                unit.implementation_runtime
                for unit in chunk
                if unit.implementation_runtime != "neutral"
            }
            if len(runtimes) > 1:
                continue
            selected = model.new_bool_var(f"segment_{start}_{end}")
            segments[(start, end)] = selected
            model.add(selected <= boundaries[start])
            for inner in range(start + 1, end):
                model.add(selected + boundaries[inner] <= 1)
            cohesion_costs.append(selected * (len(components) - 1))
    for index in range(count):
        model.add(
            sum(
                variable
                for (start, end), variable in segments.items()
                if start <= index < end
            )
            == 1
        )
    for boundary in range(count):
        model.add(
            boundaries[boundary]
            == sum(
                variable
                for (start, _end), variable in segments.items()
                if start == boundary
            )
        )
    distance = model.new_int_var(0, count * target_units + count, "target_distance")
    model.add_abs_equality(distance, group_count * target_units - count)
    tie_break = sum((index + 1) * boundaries[index] for index in range(1, count))
    model.minimize(distance * 1_000_000 + sum(cohesion_costs) * 1_000 + tie_break)
    solver = cp_model.CpSolver()
    solver.parameters.num_search_workers = 1
    solver.parameters.random_seed = 0
    status = solver.solve(model)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        raise PlanSizingError(
            "CP-SAT cannot allocate work units within hard constraints"
        )
    selected_segments = sorted(
        (start, end)
        for (start, end), variable in segments.items()
        if solver.value(variable)
    )
    if (
        not selected_segments
        or selected_segments[0][0] != 0
        or selected_segments[-1][1] != count
    ):
        raise PlanSizingError("CP-SAT returned incomplete work-unit coverage")
    for prior, current in zip(selected_segments, selected_segments[1:], strict=False):
        if prior[1] != current[0]:
            raise PlanSizingError("CP-SAT returned overlapping work-unit groups")
    return ExplicitAllocation(
        tuple(tuple(unit_ids[start:end]) for start, end in selected_segments),
        int(solver.objective_value),
    )


def size_plan_items(
    items: Sequence[Mapping[str, Any]],
    *,
    target_work_units: int = TARGET_PR_WORK_UNITS,
    max_work_units: int = MAX_PR_WORK_UNITS,
) -> list[dict[str, Any]]:
    """Group compatible adjacent items near the target size using CP-SAT.

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

    model = cp_model.CpModel()
    candidates: list[tuple[int, int, int, Any]] = []
    for start in range(len(normalized)):
        unit_count = 0
        group = []
        for end in range(start, len(normalized)):
            group.append(normalized[end])
            unit_count += item_units[end]
            if unit_count > max_work_units:
                break
            if _merge_is_valid(group):
                decision = model.new_bool_var(f"group_{start}_{end}")
                cost = abs(unit_count - target_work_units) * 10_000
                candidates.append((start, end, cost, decision))

    for index in range(len(normalized)):
        covering = [
            decision
            for start, end, _cost, decision in candidates
            if start <= index <= end
        ]
        model.add_exactly_one(covering)
    model.minimize(sum(cost * decision for _start, _end, cost, decision in candidates))

    solver = cp_model.CpSolver()
    solver.parameters.num_search_workers = 1
    solver.parameters.random_seed = 0
    status = solver.solve(model)
    if status != cp_model.OPTIMAL:
        raise PlanSizingError(
            "the deterministic PR sizing model did not find a proven optimum "
            f"(solver status: {solver.status_name(status)})"
        )

    selected = sorted(
        (start, end)
        for start, end, _cost, decision in candidates
        if solver.value(decision)
    )
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
