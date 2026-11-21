from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace
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
source_obligation_evaluation = _BENCHMARK.source_obligation_evaluation
item_sizing_summary = _BENCHMARK.item_sizing_summary
live_structural_issues = _BENCHMARK.live_structural_issues
dependency_candidate_summary = _BENCHMARK.dependency_candidate_summary
dependency_metrics_for = _BENCHMARK.dependency_metrics_for
unresolved_question_diagnostics = _BENCHMARK.unresolved_question_diagnostics
compact_profile_summary = _BENCHMARK.compact_profile_summary
provider_usage_summary = _BENCHMARK.provider_usage_summary
planning_report_artifact_path = _BENCHMARK.planning_report_artifact_path
write_planning_report_artifact = _BENCHMARK.write_planning_report_artifact
progress_client = _BENCHMARK._ProgressClient
example_answer_client: Any = _BENCHMARK._ExampleAnswerClient


def test_source_obligation_evaluation_checks_exact_source_and_work_unit_coverage() -> (
    None
):
    profile = PROFILES["typical"]
    statements = [
        f"Implement behavior outcome {ordinal} as a Python module."
        for ordinal in range(1, profile.requirements + 1)
    ]
    requirements = [
        SimpleNamespace(
            id=f"req-{ordinal}",
            statement=statement,
            source_refs=(f"anchor-{ordinal}",),
        )
        for ordinal, statement in enumerate(statements, start=1)
    ]
    work_units = [
        SimpleNamespace(
            requirement_ids=tuple(
                f"req-{ordinal}"
                for ordinal in range(start, min(start + 4, len(requirements) + 1))
            )
        )
        for start in range(1, len(requirements) + 1, 4)
    ]

    report = source_obligation_evaluation(
        profile,
        anchor_text_by_id={
            f"anchor-{ordinal}": statement
            for ordinal, statement in enumerate(statements, start=1)
        },
        requirements=requirements,
        work_units=work_units,
    )

    assert report["status"] == "matched"
    assert report["exact_source_statement_matches"] == profile.requirements
    assert report["missing_work_unit_requirement_ids"] == []
    assert report["duplicate_work_unit_requirement_ids"] == []
    assert report["unknown_work_unit_requirement_ids"] == []


def test_source_obligation_evaluation_reports_semantic_fixture_mismatches() -> None:
    profile = PROFILES["typical"]
    statements = [
        f"Implement behavior outcome {ordinal} as a Python module."
        for ordinal in range(1, profile.requirements + 1)
    ]
    requirements = [
        SimpleNamespace(
            id=f"req-{ordinal}",
            statement=("Misstated obligation." if ordinal == 1 else statement),
            source_refs=(f"anchor-{ordinal}",),
        )
        for ordinal, statement in enumerate(statements, start=1)
    ]
    work_units = [
        SimpleNamespace(
            requirement_ids=("req-1", "req-2", "req-2", "unknown-requirement")
        )
    ]

    report = source_obligation_evaluation(
        profile,
        anchor_text_by_id={
            f"anchor-{ordinal}": statement
            for ordinal, statement in enumerate(statements, start=1)
        },
        requirements=requirements,
        work_units=work_units,
    )

    assert report["status"] == "mismatch"
    assert report["exact_source_statement_matches"] == profile.requirements - 1
    assert report["source_binding_error_requirement_ids"] == ["req-1"]
    assert report["duplicate_work_unit_requirement_ids"] == ["req-2"]
    assert report["unknown_work_unit_requirement_ids"] == ["unknown-requirement"]


def test_structural_stress_profile_compiles_the_reviewed_operation_set() -> None:
    profile = PROFILES["structrr-stress"]
    report = _BENCHMARK.run_profile(
        profile,
        repository_root=Path(__file__).resolve().parents[1],
        enforce_fixture_shape=True,
    )

    assert report["terminal_status"] == "succeeded"
    assert report["compile_error"] is None
    assert report["source_obligation_evaluation"]["status"] == "matched"


def test_work_unit_proposal_batching_reduces_large_profile_call_count() -> None:
    profile = _BENCHMARK.Profile(
        "work-unit-batching",
        requirements=40,
        work_units=16,
        plan_items=5,
    )

    report = _BENCHMARK.run_profile(
        profile,
        repository_root=Path(__file__).resolve().parents[1],
        enforce_fixture_shape=True,
    )

    proposal_metrics = report["by_question_kind"]["propose_work_units"]
    assert proposal_metrics["questions"] == 10
    assert proposal_metrics["api_calls"] == 5
    assert report["max_batch_members_by_question_kind"]["propose_work_units"] == 2
    assert report["source_obligation_evaluation"]["status"] == "matched"


def test_provider_usage_summary_counts_batches_once_and_reports_live_metrics() -> None:
    report = provider_usage_summary(
        (
            {
                "api_calls": 1,
                "question_ids": ["question-a", "question-b"],
                "provider_calls": [
                    {
                        "elapsed_ms": 12.5,
                        "prompt_tokens": 10,
                        "completion_tokens": 3,
                        "cost_amount": 0.03,
                        "cost_currency": "USD",
                    }
                ],
            },
            {"api_calls": 0, "question_ids": ["question-b"], "provider_calls": []},
            {
                "api_calls": 1,
                "question_ids": ["question-c"],
                "provider_calls": [
                    {
                        "elapsed_ms": 7.5,
                        "prompt_tokens": 5,
                        "completion_tokens": 2,
                        "cost_amount": 0.02,
                        "cost_currency": "USD",
                    }
                ],
            },
        )
    )

    assert report["provider_call_count"] == 2
    assert report["provider_call_latency_ms"] == {
        "known_count": 2,
        "total": 20.0,
        "average": 10.0,
        "maximum": 12.5,
    }
    assert report["prompt_tokens"] == 15
    assert report["completion_tokens"] == 5
    assert report["token_usage_known"] is True
    assert report["reported_cost"] == {
        "status": "complete",
        "known_subtotal": 0.05,
        "known_subtotals_by_currency": {"USD": 0.05},
        "currency": "USD",
        "reported_calls": 2,
        "unreported_calls": 0,
    }


def test_provider_usage_summary_exposes_partial_unknown_and_mixed_cost() -> None:
    partial = provider_usage_summary(
        (
            {
                "api_calls": 2,
                "provider_calls": [
                    {"cost_amount": 0.2, "cost_currency": "USD"},
                    {"cost_amount": None, "cost_currency": None},
                ],
            },
        )
    )
    unknown = provider_usage_summary(({"api_calls": 1, "provider_calls": []},))
    mixed = provider_usage_summary(
        (
            {
                "api_calls": 2,
                "provider_calls": [
                    {"cost_amount": 0.2, "cost_currency": "USD"},
                    {"cost_amount": 0.4, "cost_currency": "EUR"},
                ],
            },
        )
    )

    assert partial["reported_cost"] == {
        "status": "partial",
        "known_subtotal": 0.2,
        "known_subtotals_by_currency": {"USD": 0.2},
        "currency": "USD",
        "reported_calls": 1,
        "unreported_calls": 1,
    }
    assert unknown["reported_cost"]["status"] == "unknown"
    assert unknown["reported_cost"]["unreported_calls"] == 1
    assert mixed["reported_cost"] == {
        "status": "mixed_currency",
        "known_subtotal": None,
        "known_subtotals_by_currency": {"EUR": 0.4, "USD": 0.2},
        "currency": None,
        "reported_calls": 2,
        "unreported_calls": 0,
    }


def test_provider_usage_summary_marks_missing_records_and_zero_calls_correctly() -> (
    None
):
    missing = provider_usage_summary(
        ({"api_calls": 2, "provider_calls": [{"prompt_tokens": 1}]},)
    )
    no_calls = provider_usage_summary(({"api_calls": 0, "provider_calls": []},))

    assert missing["provider_call_count"] == 1
    assert missing["token_usage_known"] is False
    assert missing["reported_cost"]["unreported_calls"] == 2
    assert no_calls["token_usage_known"] is True
    assert no_calls["prompt_tokens"] == 0
    assert no_calls["completion_tokens"] == 0


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
        question_id="propose-units-v10-req-0001-test",
        kind="propose_work_units",
        contract_version=10,
        subject_ids=("req-0001", "req-0002"),
        input_digest="sha256:" + "a" * 64,
        context_ref_ids=(),
        expected_answer_schema="work-unit-proposals-v10",
        attempt=3,
    )
    needs_answer = PlanningCheckpoint.needs_answer(
        session_id="benchmark-session",
        event_id="needs-answer-000001",
        sequence=1,
        parent_digest=None,
        question=question,
        error=PlanningContractError(
            "/records/0/planned_writes/0/requirement_ids",
            "must assign each requirement to a planned write",
        ),
        previous_answer={"private": "response body is omitted"},
        id_counters={},
    )

    assert unresolved_question_diagnostics((needs_answer,)) == [
        {
            "question_id": "propose-units-v10-req-0001-test",
            "kind": "propose_work_units",
            "subject_ids": ["req-0001", "req-0002"],
            "attempt": 3,
            "input_digest": "sha256:" + "a" * 64,
            "error_pointer": "/records/0/planned_writes/0/requirement_ids",
            "error_message": "must assign each requirement to a planned write",
        }
    ]
    assert "previous_answer" not in str(
        unresolved_question_diagnostics((needs_answer,))
    )


def test_resolved_question_is_removed_from_unresolved_diagnostics() -> None:
    question = Question(
        question_id="propose-units-v10-req-0001-test",
        kind="propose_work_units",
        contract_version=10,
        subject_ids=("req-0001",),
        input_digest="sha256:" + "a" * 64,
        context_ref_ids=(),
        expected_answer_schema="work-unit-proposals-v10",
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
        "provider_call_count": 900,
        "provider_call_latency_ms": {
            "known_count": 900,
            "total": 9_000,
            "average": 10.0,
            "maximum": 22.0,
        },
        "reported_cost": {
            "status": "partial",
            "known_subtotal": 1.25,
            "known_subtotals_by_currency": {"USD": 1.25},
            "currency": "USD",
            "reported_calls": 800,
            "unreported_calls": 100,
        },
        "source_obligation_evaluation": {
            "status": "mismatch",
            "scope": "synthetic_source_and_assignment_checks_only",
        },
        "max_batch_members_by_question_kind": {
            "extract_requirements": 6,
            "propose_work_units": 4,
        },
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
    assert summary["provider_call_count"] == 900
    assert summary["reported_cost"]["status"] == "partial"
    assert summary["source_obligation_evaluation"]["status"] == "mismatch"
    assert summary["max_batch_members_by_question_kind"] == {
        "extract_requirements": 6,
        "propose_work_units": 4,
    }
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
            return super()._answer(packet)

        def _propose_work_units(self, packet: Any) -> dict[str, Any]:
            answer = super()._propose_work_units(packet)
            for record in answer["records"]:
                first_requirement = int(
                    record["requirement_ids"][0].removeprefix("req-")
                )
                if first_requirement <= 4:
                    record["required_outputs"] = [
                        {
                            "description": "The operation response contract.",
                            "requirement_ids": record["requirement_ids"],
                        }
                    ]
                else:
                    record["implementation_step"] = (
                        "Consume the operation response contract."
                    )
            return answer

    report = _BENCHMARK.run_profile(
        profile,
        repository_root=Path(__file__).resolve().parents[1],
        client=DependencyAnswerClient(profile),
        enforce_fixture_shape=True,
    )

    # The two independent proposal pages share one bounded provider request.
    assert report["by_question_kind"]["propose_work_units"]["api_calls"] == 1
    assert "check_requirement_fidelity" not in report["by_question_kind"]
    assert "assess_requirement_coverage" not in report["by_question_kind"]
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
