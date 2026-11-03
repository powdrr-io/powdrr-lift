#!/usr/bin/env python3
"""Run deterministic end-to-end planning scale profiles without live providers.

Run with the repository's shared Python environment and the PYTHONPATH entries
listed in AGENTS.md:

    python scripts/benchmark_planning_scale.py --profile all

The generated reports measure local planning overhead and interaction counts.
They do not predict live provider latency or establish the two-hour target.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import Any

from powdrr_service.decision_classifier import JEV_MODEL, JevSemanticClassifierClient
from powdrr_service.planning.assembly import (
    MAX_ITEM_CRITERIA,
    MAX_ITEM_SCOPE_ENTRIES,
    MAX_PR_WORK_UNITS,
    MAX_WORK_UNIT_REQUIREMENTS_PER_PROPOSAL,
    AllocationGroup,
    ItemDescription,
    PathFact,
    PendingUnitOutput,
    WorkUnit,
)
from powdrr_service.planning.checkpoints import (
    JournalState,
    PlanningCheckpoint,
    prepare_checkpoint_append,
    reduce_checkpoint_comments,
)
from powdrr_service.planning.dependencies import (
    build_dependency_candidate_review_request,
)
from powdrr_service.planning.driver import SourceLedgerQuestionMachine
from powdrr_service.planning.service import (
    PlanningOptions,
    PlanningSession,
    run_planning_procedrr,
)
from powdrr_service.planning.source import PinnedSourceDocument, segment_markdown_source
from powdrr_service.providers.credentials import resolve_provider_credentials
from powdrr_service.resources import bundled_process_directory

from powdrr_lift.workrr.credential_check import configured_model_mapping
from powdrr_lift.workrr.github_issue_queue import QueueIssue
from powdrr_lift.workrr.planning.evidence import RequirementEvidenceSearch
from powdrr_lift.workrr.provider_runtime import (
    build_workflow_client,
    resolve_workflow_provider,
)


@dataclass(frozen=True, slots=True)
class Profile:
    name: str
    requirements: int
    work_units: int
    plan_items: int
    structural_subjects: int = 0

    @property
    def min_work_units(self) -> int:
        """Avoid requiring artificial splits when every unit can cover four reqs."""
        return (
            self.requirements + MAX_WORK_UNIT_REQUIREMENTS_PER_PROPOSAL - 1
        ) // MAX_WORK_UNIT_REQUIREMENTS_PER_PROPOSAL


PROFILES = {
    "typical": Profile("typical", requirements=40, work_units=16, plan_items=5),
    "large": Profile("large", requirements=500, work_units=150, plan_items=50),
    "structrr-stress": Profile(
        "structrr-stress",
        requirements=40,
        work_units=16,
        plan_items=5,
        structural_subjects=2_051,
    ),
}


def compact_profile_summary(report: Mapping[str, Any]) -> dict[str, Any]:
    """Return useful run metrics without large requirement and plan diagnostics."""
    dependency_candidates = report.get("dependency_candidates")
    dependency_count_fields = (
        "pending_output_count",
        "output_consumer_candidate_count",
        "confirmation_question_count",
        "confirmation_answer_count",
        "required_dependency_count",
        "independent_candidate_count",
        "pending_confirmation_count",
    )
    dependency_summary = None
    if isinstance(dependency_candidates, Mapping):
        dependency_summary = {
            key: dependency_candidates[key]
            for key in dependency_count_fields
            if key in dependency_candidates
        }
    phases = report.get("phases")
    question_kinds = report.get("by_question_kind")
    compact_phases = (
        {
            name: {
                "questions": values.get("questions"),
                "api_calls": values.get("api_calls"),
                "provider_call_elapsed_ms": values.get("elapsed_ms"),
            }
            for name, values in phases.items()
            if isinstance(phases, Mapping) and isinstance(values, Mapping)
        }
        if isinstance(phases, Mapping)
        else {}
    )
    compact_question_kinds = (
        {
            name: {
                "questions": values.get("questions"),
                "api_calls": values.get("api_calls"),
            }
            for name, values in question_kinds.items()
            if isinstance(question_kinds, Mapping) and isinstance(values, Mapping)
        }
        if isinstance(question_kinds, Mapping)
        else {}
    )
    item_sizing = report.get("item_sizing")
    compact_item_sizing = (
        {
            key: item_sizing[key]
            for key in (
                "item_count",
                "work_units_per_item",
                "requirements_per_item",
                "acceptance_criteria_per_item",
                "components_per_item",
                "maximum_work_units",
                "maximum_acceptance_criteria",
                "maximum_components",
                "violations",
            )
            if key in item_sizing
        }
        if isinstance(item_sizing, Mapping)
        else None
    )
    return {
        key: report.get(key)
        for key in (
            "profile",
            "provider",
            "model",
            "concurrency",
            "provider_timeout_seconds",
            "terminal_status",
            "elapsed_seconds",
            "source_blocks",
            "requirements",
            "work_units",
            "plan_items",
            "provider_api_calls",
            "provider_requests",
            "accepted_answer_pages",
            "deterministic_answers",
            "validation_retries",
            "prompt_tokens",
            "completion_tokens",
            "token_usage_known",
            "fixture_shape_status",
            "fixture_shape_mismatches",
            "live_structural_status",
            "live_structural_issues",
            "item_sizing",
            "expected_work_unit_range",
            "work_unit_requirement_counts",
        )
    } | {
        "phases": compact_phases,
        "by_question_kind": compact_question_kinds,
        "dependency_candidates": dependency_summary,
        "item_sizing": compact_item_sizing,
        "dependency_candidates_unavailable_reason": report.get(
            "dependency_candidates_unavailable_reason"
        ),
        "unresolved_question_count": len(report.get("pending_questions", ())),
        "compile_error": report.get("compile_error"),
    }


def planning_report_artifact_path(
    repository_root: Path, *, timestamp: datetime | None = None
) -> Path:
    """Choose a unique ignored artifact path for a full benchmark report."""
    run_time = timestamp or datetime.now(UTC)
    filename = f"planning-scale-{run_time.strftime('%Y%m%dT%H%M%S%fZ')}.json"
    return repository_root / ".powdrr" / "benchmark-reports" / filename


def write_planning_report_artifact(report: Mapping[str, Any], path: Path) -> None:
    """Persist the full report independently of the compact console summary."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


def _display_report_path(path: Path, repository_root: Path) -> str:
    try:
        return path.relative_to(repository_root).as_posix()
    except ValueError:
        return str(path)


def fixture_shape_mismatches(
    profile: Profile, report: Mapping[str, Any]
) -> dict[str, tuple[Any, Any]]:
    """Check exact plan shape plus a bounded, meaningful work-unit range."""
    expected = {
        "terminal_status": "succeeded",
        "requirements": profile.requirements,
        "plan_items": profile.plan_items,
    }
    mismatches = {
        field: (report.get(field), value)
        for field, value in expected.items()
        if report.get(field) != value
    }
    work_units = report.get("work_units")
    if (
        isinstance(work_units, bool)
        or not isinstance(work_units, int)
        or not profile.min_work_units <= work_units <= profile.work_units
    ):
        mismatches["work_units"] = (
            work_units,
            [profile.min_work_units, profile.work_units],
        )
    return mismatches


def item_sizing_summary(
    item_descriptions: Sequence[tuple[AllocationGroup, ItemDescription]],
) -> dict[str, Any]:
    """Summarize PR sizes and report violations of planner allocation bounds."""
    records = [
        {
            "allocation_id": group.id,
            "work_units": len(group.unit_ids),
            "requirements": len(group.requirement_ids),
            "acceptance_criteria": group.acceptance_criteria_count,
            "components": len(group.component_ids),
        }
        for group, _description in item_descriptions
    ]
    violations = []
    for record in records:
        allocation_id = record["allocation_id"]
        if not 1 <= record["work_units"] <= MAX_PR_WORK_UNITS:
            violations.append(
                f"{allocation_id} contains {record['work_units']} work units; "
                f"expected 1..{MAX_PR_WORK_UNITS}"
            )
        if record["acceptance_criteria"] > MAX_ITEM_CRITERIA:
            violations.append(
                f"{allocation_id} contains {record['acceptance_criteria']} "
                f"acceptance criteria; maximum is {MAX_ITEM_CRITERIA}"
            )
        if record["components"] > MAX_ITEM_SCOPE_ENTRIES:
            violations.append(
                f"{allocation_id} spans {record['components']} components; "
                f"maximum is {MAX_ITEM_SCOPE_ENTRIES}"
            )
    unit_counts = Counter(record["work_units"] for record in records)
    requirement_counts = [record["requirements"] for record in records]
    return {
        "item_count": len(records),
        "work_units_per_item": dict(sorted(unit_counts.items())),
        "requirements_per_item": _numeric_distribution(requirement_counts),
        "acceptance_criteria_per_item": _numeric_distribution(
            [record["acceptance_criteria"] for record in records]
        ),
        "components_per_item": _numeric_distribution(
            [record["components"] for record in records]
        ),
        "maximum_work_units": MAX_PR_WORK_UNITS,
        "maximum_acceptance_criteria": MAX_ITEM_CRITERIA,
        "maximum_components": MAX_ITEM_SCOPE_ENTRIES,
        "violations": violations,
        "items": records,
    }


def _numeric_distribution(values: Sequence[int]) -> dict[str, int | float | None]:
    if not values:
        return {"min": None, "max": None, "average": None}
    return {
        "min": min(values),
        "max": max(values),
        "average": round(sum(values) / len(values), 2),
    }


def live_structural_issues(profile: Profile, report: Mapping[str, Any]) -> list[str]:
    """Gate live runs on plan completeness and size bounds, not item count."""
    shape_issues = fixture_shape_mismatches(profile, report)
    shape_issues.pop("plan_items", None)
    issues = [
        f"{field}={actual!r}; expected {expected!r}"
        for field, (actual, expected) in sorted(shape_issues.items())
    ]
    item_sizing = report.get("item_sizing")
    if not isinstance(item_sizing, Mapping):
        issues.append("item sizing is unavailable")
    else:
        violations = item_sizing.get("violations")
        if not isinstance(violations, list) or any(
            not isinstance(item, str) for item in violations
        ):
            issues.append("item sizing violations are invalid")
        elif violations:
            issues.extend(violations)
    return issues


def dependency_candidate_summary(
    pending_outputs: Sequence[PendingUnitOutput],
    confirmation_pairs: Sequence[tuple[PendingUnitOutput, WorkUnit]],
    confirmation_results: Sequence[bool | None],
) -> dict[str, Any]:
    """Report output-to-consumer candidates and their review outcomes."""
    if len(confirmation_pairs) != len(confirmation_results):
        raise ValueError("each dependency candidate must have one review result")

    output_ordinals: dict[tuple[str, str, tuple[str, ...]], int] = {}
    by_output: dict[int, dict[str, int | str]] = {}
    for ordinal, pending in enumerate(pending_outputs, start=1):
        key = (pending.unit_id, pending.description, pending.requirement_ids)
        output_ordinals.setdefault(key, ordinal)
        by_output[ordinal] = {
            "producer_unit_id": pending.unit_id,
            "output_ordinal": ordinal,
            "candidate_consumers": 0,
            "answered": 0,
            "required": 0,
            "independent": 0,
            "pending": 0,
        }

    answered = required = independent = pending_count = 0
    for (pending_output, _consumer), result in zip(
        confirmation_pairs, confirmation_results, strict=True
    ):
        key = (
            pending_output.unit_id,
            pending_output.description,
            pending_output.requirement_ids,
        )
        output_ordinal = output_ordinals.get(key)
        if output_ordinal is None:
            raise ValueError("dependency candidate references an unknown output")
        output = by_output[output_ordinal]
        output["candidate_consumers"] = int(output["candidate_consumers"]) + 1
        if result is None:
            output["pending"] = int(output["pending"]) + 1
            pending_count += 1
        else:
            output["answered"] = int(output["answered"]) + 1
            answered += 1
            if result:
                output["required"] = int(output["required"]) + 1
                required += 1
            else:
                output["independent"] = int(output["independent"]) + 1
                independent += 1

    return {
        "pending_output_count": len(pending_outputs),
        "output_consumer_candidate_count": len(confirmation_pairs),
        "confirmation_question_count": len(confirmation_pairs),
        "confirmation_answer_count": answered,
        "required_dependency_count": required,
        "independent_candidate_count": independent,
        "pending_confirmation_count": pending_count,
        "by_output": list(by_output.values()),
    }


def dependency_metrics_for(
    machine: Any, accepted_answers: Mapping[str, Mapping[str, Any]]
) -> tuple[dict[str, Any] | None, str | None]:
    """Summarize dependency review, preserving incomplete planning checkpoints."""
    try:
        allocation, discovery = machine._dependency_discovery(accepted_answers)
        results = []
        units = {unit.id: unit for unit in allocation.units}
        for pending_output, consumer in discovery.confirmation_pairs:
            producer = units[pending_output.unit_id]
            review = build_dependency_candidate_review_request(
                producer,
                consumer,
                pending_output.description,
                allowed_requirement_ids=set(producer.requirement_ids)
                | set(consumer.requirement_ids),
                code_reference_paths=discovery.code_reference_paths.get(
                    (producer.id, consumer.id), ()
                ),
            )
            answer = accepted_answers.get(review.question.question_id)
            results.append(None if answer is None else review.parse_answer(answer))
        return (
            dependency_candidate_summary(
                allocation.pending_outputs, discovery.confirmation_pairs, results
            ),
            None,
        )
    except (ValueError, RuntimeError) as error:
        return None, str(error)


def unresolved_question_diagnostics(events: Sequence[Any]) -> list[dict[str, Any]]:
    """Summarize unresolved answer failures without retaining model responses."""
    unresolved: dict[str, dict[str, Any]] = {}
    for event in events:
        if event.event_type not in {"needs_answer", "answer_accepted"}:
            continue
        payload = event.payload
        question = payload.get("question")
        if not isinstance(question, Mapping):
            continue
        question_id = question.get("question_id")
        if not isinstance(question_id, str):
            continue
        if event.event_type == "answer_accepted":
            unresolved.pop(question_id, None)
            continue
        validation_result = payload.get("validation_result")
        if not isinstance(validation_result, Mapping):
            validation_result = {}
        subject_ids = question.get("subject_ids")
        unresolved[question_id] = {
            "question_id": question_id,
            "kind": question.get("kind"),
            "subject_ids": (
                [item for item in subject_ids if isinstance(item, str)]
                if isinstance(subject_ids, list)
                else []
            ),
            "attempt": question.get("attempt"),
            "input_digest": payload.get("input_digest"),
            "error_pointer": validation_result.get("json_pointer"),
            "error_message": validation_result.get("message"),
        }
    return list(unresolved.values())


class _ProgressClient:
    """Expose provider-call progress without printing prompt or response data."""

    def __init__(self, client: Any) -> None:
        self._client = client
        self._call_number = 0
        self._completed_count = 0
        self._call_number_lock = Lock()

    @property
    def last_usage(self) -> Any:
        return getattr(self._client, "last_usage", None)

    def complete_json(
        self,
        messages: list[dict[str, str]],
        *,
        response_schema: Mapping[str, Any] | None = None,
    ) -> Any:
        with self._call_number_lock:
            self._call_number += 1
            call_number = self._call_number
        kind = "unknown"
        if messages:
            try:
                packet = json.loads(messages[-1]["content"])
                question = packet.get("exact_question", {})
                kind = str(question.get("kind", kind))
                if kind == "planning_question_batch":
                    kinds = {
                        str(fact["value"]["exact_question"]["kind"])
                        for fact in packet.get("accepted_facts", [])
                        if isinstance(fact, Mapping)
                        and isinstance(fact.get("value"), Mapping)
                        and isinstance(fact["value"].get("exact_question"), Mapping)
                    }
                    if kinds:
                        kind = ",".join(sorted(kinds))
            except (KeyError, TypeError, json.JSONDecodeError):
                pass
        started = time.perf_counter()
        try:
            result = self._client.complete_json(
                messages, response_schema=response_schema
            )
        except Exception:
            elapsed = time.perf_counter() - started
            print(
                f"Live benchmark request {call_number} failed after "
                f"{elapsed:.1f}s ({kind}).",
                file=sys.stderr,
                flush=True,
            )
            raise
        elapsed = time.perf_counter() - started
        with self._call_number_lock:
            self._completed_count += 1
            completed_count = self._completed_count
        if completed_count % 25 == 0:
            print(
                f"Live benchmark completed {completed_count} provider calls; "
                f"most recent: {kind} in {elapsed:.1f}s.",
                file=sys.stderr,
                flush=True,
            )
        return result


class _BenchmarkCheckpointStore:
    """Keep journal semantics while avoiding repeated full-history reductions."""

    def __init__(self, *, session_id: str) -> None:
        self.session_id = session_id
        self.comments: list[dict[str, str]] = []
        self._state = reduce_checkpoint_comments((), session_id=session_id)

    def load(self, *, selected_checkpoint_digest: str | None = None) -> JournalState:
        if selected_checkpoint_digest is not None:
            return reduce_checkpoint_comments(
                self.comments,
                session_id=self.session_id,
                selected_checkpoint_digest=selected_checkpoint_digest,
            )
        return self._state

    def append(
        self,
        checkpoint: PlanningCheckpoint,
        *,
        expected_parent_digest: str | None,
        selected_checkpoint_digest: str | None = None,
    ) -> JournalState:
        current = self.load(selected_checkpoint_digest=selected_checkpoint_digest)
        rendered = prepare_checkpoint_append(
            checkpoint,
            current,
            session_id=self.session_id,
            expected_parent_digest=expected_parent_digest,
        )
        self.comments.extend({"body": body} for body in rendered)
        self._state = JournalState(
            status="active",
            head=checkpoint,
            events=(*current.events, checkpoint),
            fork_parent_digests=(),
            stale_event_digests=(),
            incomplete_manifest_digests=(),
            selected_checkpoint_digest=None,
        )
        return self._state


class _ExampleAnswerClient:
    """Answer each real planning request with a valid, nontrivial fixture."""

    def __init__(self, profile: Profile) -> None:
        self.profile = profile
        self.calls_by_kind: Counter[str] = Counter()

    def complete_json(
        self,
        messages: list[dict[str, str]],
        *,
        response_schema: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        del response_schema
        packet = json.loads(messages[-1]["content"])
        kind = packet["exact_question"]["kind"]
        self.calls_by_kind[kind] += 1
        if kind == "planning_question_batch":
            answers = {}
            example_answers = packet["examples"]["valid_positive"]["answers"]
            for fact in packet["accepted_facts"]:
                if not fact["ref_id"].startswith("question:"):
                    continue
                child = fact["value"]
                if "examples" not in child:
                    child = {
                        **child,
                        "examples": {
                            "valid_positive": example_answers[
                                child["exact_question"]["question_id"]
                            ]
                        },
                    }
                answer = self._answer(child)
                answers[child["exact_question"]["question_id"]] = answer
            return {"answers": answers}
        return self._answer(packet)

    def complete_json_batch(
        self,
        requests: list[tuple[list[dict[str, str]], Mapping[str, Any]]],
    ) -> list[dict[str, Any]]:
        """Model one bounded Jev batch while keeping member answers independent."""
        answers = []
        for messages, _schema in requests:
            packet = json.loads(messages[-1]["content"])
            kind = packet["exact_question"]["kind"]
            self.calls_by_kind[kind] += 1
            answers.append(self._answer(packet))
        return answers

    def _answer(self, packet: Mapping[str, Any]) -> dict[str, Any]:
        kind = packet["exact_question"]["kind"]
        if kind == "extract_requirements":
            return self._extract_requirements(packet)
        if kind == "propose_work_units":
            return self._propose_work_units(packet)
        answer = dict(packet["examples"]["valid_positive"])
        if isinstance(answer.get("title"), str):
            subjects = packet["exact_question"].get("subject_ids", [])
            suffix = str(subjects[0]) if subjects else str(self.calls_by_kind[kind])
            answer["title"] = f"{answer['title']} {suffix}"
        return answer

    @staticmethod
    def _extract_requirements(packet: Mapping[str, Any]) -> dict[str, Any]:
        excerpts = packet["source_excerpts"]
        facts = packet["accepted_facts"]
        if not excerpts:
            return {"records": [], "has_more": False, "next_anchor": None}
        excerpt = excerpts[0]
        anchor_records = [
            (fact["ref_id"], fact["value"])
            for fact in facts
            if isinstance(fact, Mapping)
            and isinstance(fact.get("value"), Mapping)
            and fact["value"].get("clause_id") is not None
            and fact["value"].get("block_id") == excerpt["ref_id"]
        ]
        records = []
        for ref_id, anchor in anchor_records:
            start = anchor["start_offset"] - excerpt["start_offset"]
            end = anchor["end_offset"] - excerpt["start_offset"]
            statement = excerpt["text"][start:end].strip()
            records.append(
                {
                    "statement": statement,
                    "kind": "behavior",
                    "source_refs": [ref_id],
                    "related_source_refs": [],
                }
            )
        return {"records": records, "has_more": False, "next_anchor": None}

    def _propose_work_units(self, packet: Mapping[str, Any]) -> dict[str, Any]:
        requirement_ids = tuple(packet["exact_question"]["subject_ids"])
        facts = {
            fact["ref_id"].removeprefix("requirement:"): fact["value"]
            for fact in packet["accepted_facts"]
            if isinstance(fact, Mapping)
            and isinstance(fact.get("ref_id"), str)
            and fact["ref_id"].startswith("requirement:")
        }
        group_ordinal = (int(requirement_ids[0].removeprefix("req-")) - 1) // 4
        total_groups = self.profile.requirements // 4
        extra_units = self.profile.work_units - total_groups
        partitions = (
            (requirement_ids[:2], requirement_ids[2:])
            if group_ordinal < extra_units
            else (requirement_ids,)
        )
        records = []
        for partition in partitions:
            first_number = partition[0].removeprefix("req-")
            source_refs = tuple(
                facts[requirement_id]["source_refs"][0] for requirement_id in partition
            )
            records.append(
                {
                    "requirement_ids": list(partition),
                    "deliverable": f"Implement behavior outcome {first_number}.",
                    "planned_writes": [
                        {
                            "path": f"src/generated/behavior-{first_number}.py",
                            "role": "implementation",
                            "source_refs": list(source_refs),
                            "evidence_ids": [],
                        }
                    ],
                    "implementation_step": (
                        f"Implement and verify behavior outcome {first_number}."
                    ),
                    "acceptance_criteria": [
                        f"Behavior outcome {first_number} is observable."
                    ],
                    "required_outputs": [],
                }
            )
        return {"records": records, "has_more": False, "next_anchor": None}


def _source_document(profile: Profile) -> PinnedSourceDocument:
    paragraphs = []
    for ordinal in range(1, profile.requirements + 1):
        paragraphs.append(f"Implement behavior outcome {ordinal} as a Python module.")
    text = "\n\n".join(paragraphs)
    return PinnedSourceDocument(
        repository="powdrr-io/planning-benchmark",
        commit_sha="a" * 40,
        path="docs/synthetic-plan.md",
        blob_sha="b" * 40,
        content_sha256=hashlib.sha256(text.encode()).hexdigest(),
        text=text,
    )


def _structrr_baseline(profile: Profile, paths: tuple[str, ...]) -> dict[str, Any]:
    entities = [
        {
            "id": f"benchmark:function:{index:05d}",
            "type": "Function",
            "name": f"benchmark_function_{index:05d}",
            "path": paths[index % len(paths)],
        }
        for index in range(profile.structural_subjects)
    ]
    relationships = [
        {
            "id": f"benchmark:relationship:{index:05d}",
            "source": entities[index]["id"],
            "relationship": "calls",
            "target": entities[index + 1]["id"],
        }
        for index in range(min(100, max(0, len(entities) - 1)))
    ]
    return {
        "entities": entities,
        "entity_relationships": relationships,
        "files": [],
    }


def _planned_paths(profile: Profile) -> tuple[str, ...]:
    paths: list[str] = []
    extra_units = profile.work_units - profile.requirements // 4
    for group in range(profile.requirements // 4):
        first = group * 4 + 1
        if group < extra_units:
            paths.extend(
                (
                    f"src/generated/behavior-{first:04d}.py",
                    f"src/generated/behavior-{first + 2:04d}.py",
                )
            )
        else:
            paths.append(f"src/generated/behavior-{first:04d}.py")
    return tuple(paths)


def run_profile(
    profile: Profile,
    *,
    repository_root: Path,
    client: Any | None = None,
    decision_client: Any | None = None,
    decision_provider: str = "JEV",
    decision_model: str = JEV_MODEL,
    provider: str = "deterministic-profile",
    model: str = "deterministic-profile",
    concurrency: int = 4,
    timeout_seconds: float = 120,
    use_provider_schema: bool = True,
    enforce_fixture_shape: bool = True,
) -> dict[str, Any]:
    document = _source_document(profile)
    source = segment_markdown_source(document)

    def search(requirements: Any) -> dict[str, RequirementEvidenceSearch]:
        return {
            item.id: RequirementEvidenceSearch((), (), 0, 0, document.commit_sha)
            for item in requirements
        }

    machine = SourceLedgerQuestionMachine(
        source,
        evidence_search=search,
        verification_command="make test",
        repository_paths={
            "src/generated/existing_behavior.py": PathFact(
                "src/generated/existing_behavior.py", "python"
            )
        },
        preferred_runtimes=("python",),
        plan_id=f"benchmark-{profile.name}",
        structrr_baseline=_structrr_baseline(profile, _planned_paths(profile)),
    )
    session_id = f"planning-benchmark-{profile.name}"
    session = PlanningSession(
        session_id=session_id,
        plan_id=f"benchmark-{profile.name}",
        repository=document.repository,
        base_commit=document.commit_sha,
        source_path=document.path,
        source_blob_sha=document.blob_sha,
        verification_command="make test",
        issue=QueueIssue(
            1,
            "Planning benchmark",
            "Synthetic planning profile.",
            "open",
            ("powdrr:planning",),
            "https://example.invalid/issues/1",
        ),
    )
    answer_client = client or _ExampleAnswerClient(profile)
    store = _BenchmarkCheckpointStore(session_id=session_id)
    calls: list[Mapping[str, Any]] = []
    started = time.perf_counter()
    result = run_planning_procedrr(
        process_directory=bundled_process_directory(),
        session=session,
        state_machine=machine,
        checkpoint_store=store,
        client=answer_client,
        options=PlanningOptions(
            model=model,
            provider=provider,
            decision_client=(
                answer_client if decision_client is None else decision_client
            ),
            decision_provider=(
                provider if decision_client is None else decision_provider
            ),
            decision_model=decision_model,
            max_concurrent_questions=concurrency,
            use_provider_schema=use_provider_schema,
            telemetry_sink=calls.append,
        ),
        stderr=None,
    )
    elapsed_seconds = time.perf_counter() - started
    journal = store.load()
    accepted = {
        str(event.payload["question"]["question_id"]): dict(
            event.payload["accepted_answer"]
        )
        for event in journal.events
        if event.event_type == "answer_accepted"
        and isinstance(event.payload.get("question"), Mapping)
        and isinstance(event.payload.get("accepted_answer"), Mapping)
    }
    compiled = None
    compile_error: str | None = None
    try:
        compiled = machine.compile_accepted_plan(
            accepted,
            plan_id=f"benchmark-{profile.name}",
            plan_path="docs/plans/benchmark.yaml",
            proposal_path="docs/plans/benchmark-structrr-proposal.json",
        )
    except Exception as error:  # The live benchmark must preserve blocked runs.
        compile_error = str(error)
    try:
        accepted_requirements = machine.accepted_ledger(accepted).requirements
    except (ValueError, RuntimeError):
        accepted_requirements = None
    try:
        accepted_units = machine.accepted_work_units(accepted)
    except (ValueError, RuntimeError):
        accepted_units = None
    try:
        pending_questions = machine.ready_questions(accepted)
    except (ValueError, RuntimeError):
        pending_questions = ()
    by_phase: dict[str, dict[str, float | int]] = defaultdict(
        lambda: {"questions": 0, "api_calls": 0, "elapsed_ms": 0.0}
    )
    by_question_kind: dict[str, dict[str, int]] = defaultdict(
        lambda: {"questions": 0, "api_calls": 0}
    )
    prompt_tokens = completion_tokens = 0
    token_usage_known = True
    for metric in calls:
        phase = str(metric["phase"])
        by_phase[phase]["questions"] += len(metric["question_ids"])
        by_phase[phase]["api_calls"] += int(metric["api_calls"])
        by_phase[phase]["elapsed_ms"] += float(metric["elapsed_ms"])
        kinds = metric.get("question_kinds", [])
        if isinstance(kinds, list):
            for kind in kinds:
                if isinstance(kind, str):
                    by_question_kind[kind]["questions"] += len(metric["question_ids"])
                    by_question_kind[kind]["api_calls"] += int(metric["api_calls"])
        prompt = metric.get("prompt_tokens")
        completion = metric.get("completion_tokens")
        if isinstance(prompt, int) and isinstance(completion, int):
            prompt_tokens += prompt
            completion_tokens += completion
        else:
            token_usage_known = False
    max_questions_per_provider_call = max(
        (len(metric["question_ids"]) for metric in calls), default=0
    )
    pages = sum(
        1
        for event in journal.events
        if event.event_type == "answer_accepted"
        and isinstance(event.payload.get("accepted_answer"), Mapping)
        and "has_more" in event.payload["accepted_answer"]
    )
    dependency_metrics, dependency_metrics_error = dependency_metrics_for(
        machine, accepted
    )
    item_sizing = (
        item_sizing_summary(machine.accepted_item_descriptions(accepted))
        if compiled is not None
        else None
    )
    report = {
        "profile": profile.name,
        "provider": provider,
        "model": model,
        "concurrency": concurrency,
        "provider_timeout_seconds": timeout_seconds if client is not None else None,
        "terminal_status": result.terminal_status.value,
        "elapsed_seconds": round(elapsed_seconds, 3),
        "source_blocks": len(source.blocks),
        "requirements": (
            None if accepted_requirements is None else len(accepted_requirements)
        ),
        "work_units": None if accepted_units is None else len(accepted_units),
        "plan_items": None if compiled is None else len(compiled.plan.items),
        "compile_error": compile_error,
        "pending_questions": [
            {
                "question_id": request.question.question_id,
                "kind": request.question.kind,
                "subject_ids": list(request.question.subject_ids),
                "member_ids": [
                    member.question.question_id for member in request.batch_members
                ],
            }
            for request in pending_questions
        ],
        "baseline_structural_subjects": profile.structural_subjects,
        "accepted_answer_pages": pages,
        "deterministic_answers": sum(
            1
            for event in journal.events
            if event.event_type == "answer_accepted"
            and event.payload.get("validation_result", {}).get("origin")
            == "deterministic"
        ),
        "provider_api_calls": sum(int(metric["api_calls"]) for metric in calls),
        "provider_requests": sum(len(metric["question_ids"]) for metric in calls),
        "prompt_tokens": prompt_tokens if token_usage_known else None,
        "completion_tokens": completion_tokens if token_usage_known else None,
        "token_usage_known": token_usage_known,
        "max_questions_per_provider_call": max_questions_per_provider_call,
        "validation_retries": sum(
            int(metric["validation_failures"]) for metric in calls
        ),
        "by_question_kind": dict(by_question_kind),
        "phases": dict(by_phase),
        "dependency_candidates": dependency_metrics,
        "dependency_candidates_unavailable_reason": dependency_metrics_error,
        "item_sizing": item_sizing,
        "unresolved_question_diagnostics": unresolved_question_diagnostics(
            journal.events
        ),
    }
    mismatches = fixture_shape_mismatches(profile, report)
    report["fixture_shape_mismatches"] = mismatches
    report["fixture_shape_status"] = "mismatch" if mismatches else "matched"
    structural_issues = live_structural_issues(profile, report)
    report["live_structural_issues"] = structural_issues
    report["live_structural_status"] = "mismatch" if structural_issues else "passed"
    report["expected_work_unit_range"] = [
        profile.min_work_units,
        profile.work_units,
    ]
    report["work_unit_requirement_counts"] = dict(
        sorted(
            Counter(
                len(unit.requirement_ids) for unit in (accepted_units or ())
            ).items()
        )
    )
    if mismatches:
        report["fixture_shape_diagnostics"] = {
            "requirements_by_source_ref": dict(
                Counter(
                    source_ref
                    for requirement in (accepted_requirements or ())
                    for source_ref in requirement.source_refs
                )
            ),
            "requirements": [
                {
                    "id": requirement.id,
                    "statement": requirement.statement,
                    "source_refs": list(requirement.source_refs),
                }
                for requirement in (accepted_requirements or ())
            ],
            "work_units": [
                {
                    "id": unit.id,
                    "requirement_ids": list(unit.requirement_ids),
                    "deliverable": unit.deliverable,
                    "paths": [write.path for write in unit.planned_writes],
                }
                for unit in (accepted_units or ())
            ],
            "items": [
                {
                    "id": item.item_id,
                    "title": item.title,
                    "scope": list(item.scope),
                }
                for item in (() if compiled is None else compiled.plan.items)
            ],
        }
    if mismatches and enforce_fixture_shape:
        raise RuntimeError(f"profile {profile.name} failed expectations: {mismatches}")
    if max_questions_per_provider_call > 4:
        raise RuntimeError(
            f"profile {profile.name} exceeded the four-question batch limit: "
            f"{max_questions_per_provider_call}"
        )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--profile",
        choices=(*PROFILES, "all"),
        default="typical",
        help="Synthetic scale profile to execute.",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help=(
            "Use Powdrr's configured live provider. This makes real provider calls; "
            "select one profile to control cost."
        ),
    )
    parser.add_argument(
        "--provider",
        default="auto",
        help="Provider for --live (default: Powdrr's configured provider).",
    )
    parser.add_argument(
        "--model",
        help="Optional model override for --live.",
    )
    parser.add_argument(
        "--base-url",
        help=(
            "Optional provider endpoint override for --live (credentials stay in env)."
        ),
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=4,
        help="Maximum concurrent planning questions (default: 4).",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=120,
        help="Per-provider-call timeout in live mode (default: 120).",
    )
    parser.add_argument(
        "--report-file",
        type=Path,
        help=(
            "Path for the full JSON report. Defaults to a timestamped artifact "
            "under .powdrr/benchmark-reports."
        ),
    )
    args = parser.parse_args()
    if args.live and args.profile == "all":
        parser.error("--live requires one explicit --profile to control provider cost")
    if args.concurrency < 1:
        parser.error("--concurrency must be positive")
    if args.timeout_seconds <= 0:
        parser.error("--timeout-seconds must be positive")
    repository_root = Path(__file__).resolve().parents[1]
    report_path = args.report_file or planning_report_artifact_path(repository_root)
    if not report_path.is_absolute():
        report_path = repository_root / report_path
    selected = (
        tuple(PROFILES.values()) if args.profile == "all" else (PROFILES[args.profile],)
    )
    live_client = None
    live_decision_client = None
    provider = "deterministic-profile"
    model = "deterministic-profile"
    decision_model = JEV_MODEL
    if args.live:
        provider = resolve_workflow_provider(args.provider)
        mapping = configured_model_mapping(provider, args.model)
        credentials = resolve_provider_credentials(
            provider, base_url_override=args.base_url
        )
        live_client = build_workflow_client(
            credentials,
            model=mapping.model,
            model_cache_dir=repository_root / ".powdrr" / "models",
            progress_stream=None,
            timeout=args.timeout_seconds,
        )
        set_output_limit = getattr(
            live_client, "set_structured_output_token_limit", None
        )
        if callable(set_output_limit):
            set_output_limit(8192)
        live_client = _ProgressClient(live_client)
        live_decision_client = JevSemanticClassifierClient(
            live_client, fail_closed=True
        )
        decision_model = os.environ.get("SYSTEM_ONE_MODEL") or JEV_MODEL
        model = mapping.model
    report = [
        run_profile(
            profile,
            repository_root=repository_root,
            client=live_client,
            decision_client=live_decision_client,
            decision_provider="JEV",
            decision_model=decision_model,
            provider=provider,
            model=model,
            concurrency=args.concurrency,
            timeout_seconds=args.timeout_seconds,
            use_provider_schema=provider != "deepinfra-cheap",
            enforce_fixture_shape=not args.live,
        )
        for profile in selected
    ]
    full_report = {"schema": "planning-scale-report-v1", "profiles": report}
    write_planning_report_artifact(full_report, report_path)
    summary = {
        "schema": "planning-scale-summary-v1",
        "report_file": _display_report_path(report_path, repository_root),
        "profiles": [compact_profile_summary(item) for item in report],
    }
    print(json.dumps(summary, indent=2))
    if args.live and any(item["live_structural_issues"] for item in report):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
