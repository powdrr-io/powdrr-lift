"""Deterministically rebalance adjacent plan work into reviewable PR slices."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
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


class PlanSizingError(ValueError):
    """A plan could not be represented as valid bounded PR slices."""


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
        return True
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
        == {
            "id",
            "title",
            "objective",
            "scope",
            "allowed_paths",
            "implementation_steps",
            "acceptance_criteria",
            "dependencies",
        }
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
                runtimes.update(
                    match.group(0).casefold() for match in _RUNTIME.finditer(value)
                )
    return runtimes


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
