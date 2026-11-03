from __future__ import annotations

import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from typing import Any, cast

from powdrr_service.planning.assembly import PendingUnitOutput, WorkUnit
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
dependency_candidate_summary = _BENCHMARK.dependency_candidate_summary
dependency_metrics_for = _BENCHMARK.dependency_metrics_for
unresolved_question_diagnostics = _BENCHMARK.unresolved_question_diagnostics
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
            if packet["exact_question"]["kind"] == "confirm_dependency":
                return {
                    "result": "required",
                    "output": "The operation response contract.",
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
