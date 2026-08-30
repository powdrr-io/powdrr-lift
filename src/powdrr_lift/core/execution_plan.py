"""Typed execution plans and deterministic Build eligibility evaluation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

EXECUTION_PLAN_SCHEMA_VERSION = "execution-plan-v1"


@dataclass(frozen=True, slots=True)
class ExecutionUnit:
    unit_id: str
    objective: str
    paths: tuple[str, ...]
    dependencies: tuple[str, ...] = ()
    validation_profiles: tuple[str, ...] = ()
    acceptance_criteria: tuple[str, ...] = ()

    def to_data(self) -> dict[str, Any]:
        return {
            "unit_id": self.unit_id,
            "objective": self.objective,
            "paths": list(self.paths),
            "dependencies": list(self.dependencies),
            "validation_profiles": list(self.validation_profiles),
            "acceptance_criteria": list(self.acceptance_criteria),
        }


@dataclass(frozen=True, slots=True)
class ExecutionPlan:
    plan_id: str
    proposed_pr_fingerprint: str
    units: tuple[ExecutionUnit, ...]
    allowed_paths: tuple[str, ...]
    introduced_decisions: tuple[str, ...] = ()
    schema_version: str = EXECUTION_PLAN_SCHEMA_VERSION

    def to_data(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "plan_id": self.plan_id,
            "proposed_pr_fingerprint": self.proposed_pr_fingerprint,
            "units": [unit.to_data() for unit in self.units],
            "allowed_paths": list(self.allowed_paths),
            "introduced_decisions": list(self.introduced_decisions),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_data(), indent=2) + "\n"

    @classmethod
    def from_data(cls, data: dict[str, Any]) -> ExecutionPlan:
        if data.get("schema_version") != EXECUTION_PLAN_SCHEMA_VERSION:
            raise ValueError("unsupported execution plan schema version")
        units = tuple(
            ExecutionUnit(
                item["unit_id"],
                item["objective"],
                tuple(item["paths"]),
                tuple(item.get("dependencies", ())),
                tuple(item.get("validation_profiles", ())),
                tuple(item.get("acceptance_criteria", ())),
            )
            for item in data["units"]
        )
        return cls(
            data["plan_id"],
            data["proposed_pr_fingerprint"],
            units,
            tuple(data["allowed_paths"]),
            tuple(data.get("introduced_decisions", ())),
        )


@dataclass(frozen=True, slots=True)
class PlanEvaluation:
    valid: bool
    build_eligible: bool
    issues: tuple[str, ...] = ()
    required_decisions: tuple[str, ...] = ()


def evaluate_execution_plan(
    plan: ExecutionPlan,
    *,
    proposed_pr_fingerprint: str,
    proposed_pr_paths: tuple[str, ...],
    known_validation_profiles: frozenset[str],
) -> PlanEvaluation:
    issues: list[str] = []
    unit_ids = {unit.unit_id for unit in plan.units}
    if len(unit_ids) != len(plan.units):
        issues.append("unit IDs must be unique")
    if plan.proposed_pr_fingerprint != proposed_pr_fingerprint:
        issues.append("proposed-PR fingerprint has changed")
    allowed = set(plan.allowed_paths)
    if not allowed:
        issues.append("allowed_paths must not be empty")
    for path in (*plan.allowed_paths, *proposed_pr_paths):
        if _unsafe_path(path):
            issues.append(f"unsafe path: {path!r}")
    for path in proposed_pr_paths:
        if not any(_path_in_scope(path, scope) for scope in allowed):
            issues.append(f"proposed-PR path is outside plan scope: {path!r}")
    for unit in plan.units:
        if not unit.objective.strip():
            issues.append(f"unit {unit.unit_id!r} has no objective")
        if not unit.acceptance_criteria:
            issues.append(f"unit {unit.unit_id!r} has no acceptance criteria")
        if not unit.validation_profiles:
            issues.append(f"unit {unit.unit_id!r} has no validation profiles")
        for profile in unit.validation_profiles:
            if profile not in known_validation_profiles:
                issues.append(
                    f"unit {unit.unit_id!r} uses unknown validation profile {profile!r}"
                )
        for dependency in unit.dependencies:
            if dependency not in unit_ids:
                issues.append(
                    f"unit {unit.unit_id!r} depends on unknown unit {dependency!r}"
                )
        for path in unit.paths:
            if _unsafe_path(path) or not any(
                _path_in_scope(path, scope) for scope in allowed
            ):
                issues.append(
                    f"unit {unit.unit_id!r} path is outside plan scope: {path!r}"
                )
    if _has_cycle(plan):
        issues.append("execution units contain a dependency cycle")
    decisions = tuple(plan.introduced_decisions)
    return PlanEvaluation(
        not issues, not issues and not decisions, tuple(issues), decisions
    )


@dataclass(frozen=True, slots=True)
class ExecutionPlanAmendment:
    plan_id: str
    affected_unit_ids: tuple[str, ...]
    reason: str
    supersedes_plan_id: str


class FileExecutionPlanStore:
    def __init__(self, workflow_directory: str | Path) -> None:
        self.root = Path(workflow_directory) / "execution" / "plans"

    def save(self, plan: ExecutionPlan) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / f"{plan.plan_id}.json"
        path.write_text(plan.to_json(), encoding="utf-8")
        return path

    def load(self, plan_id: str) -> ExecutionPlan:
        return ExecutionPlan.from_data(
            json.loads((self.root / f"{plan_id}.json").read_text(encoding="utf-8"))
        )


def _unsafe_path(path: str) -> bool:
    candidate = Path(path)
    return candidate.is_absolute() or ".." in candidate.parts


def _path_in_scope(path: str, scope: str) -> bool:
    return path == scope or path.startswith(scope.rstrip("/") + "/") or scope == "."


def _has_cycle(plan: ExecutionPlan) -> bool:
    dependencies = {unit.unit_id: set(unit.dependencies) for unit in plan.units}
    visiting: set[str] = set()
    complete: set[str] = set()

    def visit(unit_id: str) -> bool:
        if unit_id in visiting:
            return True
        if unit_id in complete:
            return False
        visiting.add(unit_id)
        if any(visit(dependency) for dependency in dependencies.get(unit_id, ())):
            return True
        visiting.remove(unit_id)
        complete.add(unit_id)
        return False

    return any(visit(unit_id) for unit_id in dependencies)
