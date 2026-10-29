"""Compatibility re-exports for service-owned plan sizing policy."""

from __future__ import annotations

from powdrr_service.planning.plan_sizing import (
    CANONICAL_RUNTIMES,
    MAX_PR_WORK_UNITS,
    PATH_RUNTIME_SUFFIXES,
    TARGET_PR_WORK_UNITS,
    ExplicitAllocation,
    ExplicitWorkUnit,
    PlanSizingError,
    allocate_explicit_work_units,
    size_plan_items,
    split_mixed_runtime_items,
)

__all__ = [
    "CANONICAL_RUNTIMES",
    "MAX_PR_WORK_UNITS",
    "PATH_RUNTIME_SUFFIXES",
    "TARGET_PR_WORK_UNITS",
    "ExplicitAllocation",
    "ExplicitWorkUnit",
    "PlanSizingError",
    "allocate_explicit_work_units",
    "size_plan_items",
    "split_mixed_runtime_items",
]
