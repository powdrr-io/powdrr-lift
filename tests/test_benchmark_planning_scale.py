from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from typing import Any, cast

from powdrr_service.planning.assembly import (
    AllocationGroup,
    ItemDescription,
    PendingUnitOutput,
    WorkUnit,
)
from powdrr_service.planning.checkpoints import PlanningCheckpoint
from powdrr_service.planning.contracts import PlanningContractError, Question

_SCRIPT_PATH = (
    Path(__file__).resolve().parents[1] / "scripts" / "benchmark_planning_scale.py"
)
_SCRIPT_SPEC = spec_from_file_location("benchmark_planning_scale", _SCRIPT_PATH)
assert _SCRIPT_SPEC is not None and _SCRIPT_SPEC.loader is not None
_SCRIPT_MODULE = module_from_spec(_SCRIPT_SPEC)
sys.modules[_SCRIPT_SPEC.name] = _SCRIPT_MODULE
_SCRIPT_SPEC.loader.exec_module(_SCRIPT_MODULE)
_BENCHMARK = cast(Any, _SCRIPT_MODULE)
PROFILES = _BENCHMARK.PROFILES
fixture_shape_mismatches = _BENCHMARK.fixture_shape_mismatches
item_sizing_summary = _BENCHMARK.item_sizing_summary
live_structural_issues = _BENCHMARK.live_structural_issues
dependency_candidate_summary = _BENCHMARK.dependency_candidate_summary
dependency_metrics_for = _BENCHMARK.dependency_metrics_for
unresolved_question_diagnostics = _BENCHMARK.unresolved_question_diagnostics
compact_profile_summary = _BENCHMARK.compact_profile_summary
planning_report_artifact_path = _BENCHMARK.planning_report_artifact_path
write_planning_report_artifact = _BENCHMARK.write_planning_report_artifact
progress_client = _BENCHMARK._ProgressClient
example_answer_client: Any = _BENCHMARK._ExampleAnswerClient


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


def test_live_structural_gate_uses_item_size_bounds_not_exact_item_count() -> None:
    profile = PROFILES["large"]
    report = {
        "terminal_status": "succeeded",
        "requirements": profile.requirements,
        "work_units": profile.work_units,
        "plan_items": profile.plan_items + 8,
        "item_sizing": {"violations": []},
    }

    assert fixture_shape_mismatches(profile, report)["plan_items"] == (
        profile.plan_items + 8,
        profile.plan_items,
    )
    assert live_structural_issues(profile, report) == []


def test_item_sizing_summary_reports_units_requirements_and_criteria() -> None:
    group = AllocationGroup(
        "allocation-0001",
        ("unit-0001", "unit-0002"),
        tuple(f"req-{index:04d}" for index in range(1, 9)),
        ("repository",),
        "python",
        5,
    )
    description = ItemDescription("Implement parser behavior", "", ())

    summary = item_sizing_summary(((group, description),))

    assert summary["item_count"] == 1
    assert summary["work_units_per_item"] == {2: 1}
    assert summary["requirements_per_item"] == {
        "min": 8,
        "max": 8,
        "average": 8.0,
    }
    assert summary["acceptance_criteria_per_item"] == {
        "min": 5,
        "max": 5,
        "average": 5.0,
    }
    assert summary["violations"] == []


def test_item_sizing_summary_flags_oversized_allocations() -> None:
    group = AllocationGroup(
        "allocation-0001",
        tuple(f"unit-{index:04d}" for index in range(1, 6)),
        tuple(f"req-{index:04d}" for index in range(1, 21)),
        ("one", "two", "three", "four"),
        "python",
        6,
    )
    description = ItemDescription("Large item", "", ())

    summary = item_sizing_summary(((group, description),))

    assert len(summary["violations"]) == 3


def test_dependency_candidate_summary_counts_output_consumers_and_reviews() -> None:
    first_output = PendingUnitOutput("unit-1", "schema contract", ("req-1",))
    second_output = PendingUnitOutput("unit-1", "error contract", ("req-2",))
    unused_output = PendingUnitOutput("unit-2", "status contract", ("req-3",))
    consumer_a = WorkUnit(
        "unit-a", ("req-a",), "A", "repository", "python", (), "A", ("A",), ()
    )
    consumer_b = WorkUnit(
        "unit-b", ("req-b",), "B", "repository", "python", (), "B", ("B",), ()
    )

    summary = dependency_candidate_summary(
        (first_output, second_output, unused_output),
        (
            (first_output, consumer_a),
            (first_output, consumer_b),
            (second_output, consumer_b),
        ),
        (True, False, None),
    )

    assert summary == {
        "pending_output_count": 3,
        "output_consumer_candidate_count": 3,
        "confirmation_question_count": 3,
        "confirmation_answer_count": 2,
        "required_dependency_count": 1,
        "independent_candidate_count": 1,
        "pending_confirmation_count": 1,
        "by_output": [
            {
                "producer_unit_id": "unit-1",
                "output_ordinal": 1,
                "candidate_consumers": 2,
                "answered": 2,
                "required": 1,
                "independent": 1,
                "pending": 0,
            },
            {
                "producer_unit_id": "unit-1",
                "output_ordinal": 2,
                "candidate_consumers": 1,
                "answered": 0,
                "required": 0,
                "independent": 0,
                "pending": 1,
            },
            {
                "producer_unit_id": "unit-2",
                "output_ordinal": 3,
                "candidate_consumers": 0,
                "answered": 0,
                "required": 0,
                "independent": 0,
                "pending": 0,
            },
        ],
    }


def test_dependency_metrics_preserve_incomplete_work_unit_checkpoint() -> None:
    class IncompleteMachine:
        def _dependency_discovery(self, _accepted: Any) -> Any:
            raise ValueError("work-unit proposals are incomplete at propose-units-1")

    metrics, error = dependency_metrics_for(IncompleteMachine(), {})

    assert metrics is None
    assert error == "work-unit proposals are incomplete at propose-units-1"


def test_unresolved_question_diagnostics_show_validation_error_without_answer() -> None:
    question = Question(
        question_id="propose-units-v3-req-0001-test",
        kind="propose_work_units",
        contract_version=3,
        subject_ids=("req-0001", "req-0002"),
        input_digest="sha256:" + "a" * 64,
        context_ref_ids=(),
        expected_answer_schema="work-unit-proposals-v3",
        attempt=3,
    )
    needs_answer = PlanningCheckpoint.needs_answer(
        session_id="benchmark-session",
        event_id="needs-answer-000001",
        sequence=1,
        parent_digest=None,
        question=question,
        error=PlanningContractError(
            "/records/0/planned_writes/0/source_refs",
            "must cite a source ref from each assigned requirement",
        ),
        previous_answer={"private": "response body is omitted"},
        id_counters={},
    )

    assert unresolved_question_diagnostics((needs_answer,)) == [
        {
            "question_id": "propose-units-v3-req-0001-test",
            "kind": "propose_work_units",
            "subject_ids": ["req-0001", "req-0002"],
            "attempt": 3,
            "input_digest": "sha256:" + "a" * 64,
            "error_pointer": "/records/0/planned_writes/0/source_refs",
            "error_message": "must cite a source ref from each assigned requirement",
        }
    ]
    assert "previous_answer" not in str(
        unresolved_question_diagnostics((needs_answer,))
    )


def test_resolved_question_is_removed_from_unresolved_diagnostics() -> None:
    question = Question(
        question_id="propose-units-v3-req-0001-test",
        kind="propose_work_units",
        contract_version=3,
        subject_ids=("req-0001",),
        input_digest="sha256:" + "a" * 64,
        context_ref_ids=(),
        expected_answer_schema="work-unit-proposals-v3",
        attempt=3,
    )
    needs_answer = PlanningCheckpoint.needs_answer(
        session_id="benchmark-session",
        event_id="needs-answer-000001",
        sequence=1,
        parent_digest=None,
        question=question,
        error=PlanningContractError("/records", "missing proposal"),
        previous_answer={},
        id_counters={},
    )
    accepted = PlanningCheckpoint.answer_accepted(
        session_id="benchmark-session",
        event_id="answer-accepted-000002",
        sequence=2,
        parent_digest=needs_answer.digest,
        question=question,
        accepted_answer={"records": []},
        validation_result={"accepted": True},
        id_counters={},
    )

    assert unresolved_question_diagnostics((needs_answer, accepted)) == []


def test_compact_summary_omits_large_shape_diagnostics_but_keeps_run_metrics() -> None:
    report = {
        "profile": "large",
        "provider": "deepinfra-cheap",
        "model": "example-model",
        "terminal_status": "succeeded",
        "elapsed_seconds": 972.3,
        "requirements": 500,
        "work_units": 150,
        "plan_items": 57,
        "provider_api_calls": 900,
        "provider_requests": 1200,
        "validation_retries": 4,
        "phases": {
            "dependency-and-pr-slicing": {
                "questions": 600,
                "api_calls": 150,
                "elapsed_ms": 4_200,
            }
        },
        "by_question_kind": {
            "review_dependency_candidate": {"questions": 600},
            "describe_confirmed_dependency": {"questions": 400},
        },
        "dependency_candidates": {
            "output_consumer_candidate_count": 600,
            "confirmation_answer_count": 590,
            "by_output": [{"verbose": "per-output diagnostic"}] * 500,
        },
        "fixture_shape_diagnostics": {
            "requirements": [
                {"statement": "large raw requirement payload"} for _ in range(500)
            ],
            "items": [{"title": "large generated item payload"} for _ in range(57)],
        },
        "live_structural_status": "passed",
        "live_structural_issues": [],
        "item_sizing": {
            "item_count": 57,
            "work_units_per_item": {2: 57},
            "requirements_per_item": {"min": 8, "max": 8, "average": 8.0},
            "acceptance_criteria_per_item": {
                "min": 2,
                "max": 5,
                "average": 4.1,
            },
            "components_per_item": {"min": 1, "max": 1, "average": 1.0},
            "maximum_work_units": 4,
            "maximum_acceptance_criteria": 5,
            "maximum_components": 3,
            "violations": [],
            "items": [{"title": "large item diagnostic"} for _ in range(57)],
        },
        "pending_questions": [],
    }

    summary = compact_profile_summary(report)
    serialized = json.dumps(summary)

    assert summary["elapsed_seconds"] == 972.3
    assert summary["provider_api_calls"] == 900
    assert summary["plan_items"] == 57
    assert summary["live_structural_status"] == "passed"
    assert summary["item_sizing"]["item_count"] == 57
    assert summary["phases"]["dependency-and-pr-slicing"] == {
        "questions": 600,
        "api_calls": 150,
        "provider_call_elapsed_ms": 4_200,
    }
    assert summary["dependency_candidates"] == {
        "output_consumer_candidate_count": 600,
        "confirmation_answer_count": 590,
    }
    assert "large raw requirement payload" not in serialized
    assert "large generated item payload" not in serialized
    assert "large item diagnostic" not in serialized
    assert "by_output" not in serialized
    assert len(serialized) < 2_000


def test_full_report_artifact_is_saved_separately_from_console_summary(
    tmp_path: Path,
) -> None:
    report = {
        "schema": "planning-scale-report-v1",
        "profiles": [{"profile": "large", "fixture_shape_diagnostics": [1, 2, 3]}],
    }
    report_path = tmp_path / "reports" / "planning-scale.json"

    write_planning_report_artifact(report, report_path)

    assert json.loads(report_path.read_text(encoding="utf-8")) == report


def test_default_report_artifact_path_is_timestamped_under_powdrr() -> None:
    timestamp = datetime(2026, 11, 2, 23, 59, 58, tzinfo=UTC)

    path = planning_report_artifact_path(Path("/repo"), timestamp=timestamp)

    assert path == Path(
        "/repo/.powdrr/benchmark-reports/planning-scale-20261102T235958000000Z.json"
    )


def test_live_progress_logs_periodically_instead_of_for_every_request(
    capsys: Any,
) -> None:
    class Client:
        def complete_json(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return {"ok": True}

    client = progress_client(Client())

    for _ in range(26):
        assert client.complete_json([]) == {"ok": True}

    progress = capsys.readouterr().err
    assert progress.count("Live benchmark completed") == 1
    assert "25 provider calls" in progress
    assert "request 1 started" not in progress
    assert "request 1 finished" not in progress


def test_blocked_live_report_diagnoses_unanchored_c_runtime_path() -> None:
    profile = _BENCHMARK.Profile(
        "blocked-work-units", requirements=8, work_units=2, plan_items=1
    )

    class InvalidProposalClient(example_answer_client):
        def _propose_work_units(self, packet: Any) -> dict[str, Any]:
            answer = super()._propose_work_units(packet)
            for record in answer["records"]:
                for write in record["planned_writes"]:
                    write["path"] = "src/behavior_outcomes.c"
            return answer

    report = _BENCHMARK.run_profile(
        profile,
        repository_root=Path(__file__).resolve().parents[1],
        client=InvalidProposalClient(profile),
        enforce_fixture_shape=False,
    )

    assert report["terminal_status"] == "blocked"
    diagnostics = report["unresolved_question_diagnostics"]
    assert len(diagnostics) == 1
    diagnostic = diagnostics[0]
    assert diagnostic["kind"] == "propose_work_units"
    assert diagnostic["subject_ids"] == [
        "req-0001",
        "req-0002",
        "req-0003",
        "req-0004",
    ]
    assert diagnostic["attempt"] == 3
    assert diagnostic["error_pointer"] == "/planned_writes/path"
    assert diagnostic["error_message"] == (
        "runtime for planned path 'src/behavior_outcomes.c' needs repository evidence"
    )
    assert "previous_answer" not in diagnostic


def test_live_report_includes_dependency_candidate_and_confirmation_counts() -> None:
    profile = _BENCHMARK.Profile(
        "dependency-metrics", requirements=8, work_units=2, plan_items=1
    )

    class DependencyAnswerClient(example_answer_client):
        def _answer(self, packet: Any) -> dict[str, Any]:
            kind = packet["exact_question"]["kind"]
            if kind == "review_dependency_candidate":
                return {"required": True}
            if kind == "describe_confirmed_dependency":
                return {
                    "requirement_ids": ["req-0001"],
                    "reason": "The consumer uses the producer response contract.",
                }
            return super()._answer(packet)

        def _propose_work_units(self, packet: Any) -> dict[str, Any]:
            answer = super()._propose_work_units(packet)
            group = (
                int(packet["exact_question"]["subject_ids"][0].removeprefix("req-")) - 1
            ) // 4
            if group == 0:
                answer["records"][0]["required_outputs"] = [
                    {
                        "description": "The operation response contract.",
                        "requirement_ids": answer["records"][0]["requirement_ids"],
                    }
                ]
            else:
                answer["records"][0]["implementation_step"] = (
                    "Consume the operation response contract."
                )
            return answer

    report = _BENCHMARK.run_profile(
        profile,
        repository_root=Path(__file__).resolve().parents[1],
        client=DependencyAnswerClient(profile),
        enforce_fixture_shape=True,
    )

    assert report["by_question_kind"]["propose_work_units"]["api_calls"] == 1
    assert report["dependency_candidates"] == {
        "pending_output_count": 1,
        "output_consumer_candidate_count": 1,
        "confirmation_question_count": 1,
        "confirmation_answer_count": 1,
        "required_dependency_count": 1,
        "independent_candidate_count": 0,
        "pending_confirmation_count": 0,
        "by_output": [
            {
                "producer_unit_id": "unit-0001",
                "output_ordinal": 1,
                "candidate_consumers": 1,
                "answered": 1,
                "required": 1,
                "independent": 0,
                "pending": 0,
            }
        ],
    }
