from __future__ import annotations

from scripts.benchmark_planning_scale import PROFILES, fixture_shape_mismatches


def test_live_scale_shape_accepts_cohesive_work_units_within_size_bounds() -> None:
    profile = PROFILES["typical"]
    report = {
        "terminal_status": "succeeded",
        "requirements": profile.requirements,
        "work_units": profile.min_work_units,
        "plan_items": profile.plan_items,
    }

    assert fixture_shape_mismatches(profile, report) == {}


def test_live_scale_shape_rejects_work_units_outside_size_bounds() -> None:
    profile = PROFILES["typical"]
    report = {
        "terminal_status": "succeeded",
        "requirements": profile.requirements,
        "work_units": profile.min_work_units - 1,
        "plan_items": profile.plan_items,
    }

    assert fixture_shape_mismatches(profile, report) == {
        "work_units": (profile.min_work_units - 1, [10, 16])
    }


def test_live_scale_shape_still_requires_requirement_and_pr_counts() -> None:
    profile = PROFILES["typical"]
    report = {
        "terminal_status": "succeeded",
        "requirements": profile.requirements - 1,
        "work_units": profile.work_units,
        "plan_items": profile.plan_items + 1,
    }

    assert fixture_shape_mismatches(profile, report) == {
        "requirements": (39, 40),
        "plan_items": (6, 5),
    }
