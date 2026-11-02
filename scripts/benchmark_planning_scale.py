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
import time
from collections import Counter, defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from powdrr_service.planning.checkpoints import (
    JournalState,
    PlanningCheckpoint,
    prepare_checkpoint_append,
    reduce_checkpoint_comments,
)
from powdrr_service.planning.driver import SourceLedgerQuestionMachine
from powdrr_service.planning.service import (
    PlanningOptions,
    PlanningSession,
    run_planning_procedrr,
)
from powdrr_service.planning.source import PinnedSourceDocument, segment_markdown_source
from powdrr_service.resources import bundled_process_directory

from powdrr_lift.workrr.github_issue_queue import QueueIssue
from powdrr_lift.workrr.planning.evidence import RequirementEvidenceSearch


@dataclass(frozen=True, slots=True)
class Profile:
    name: str
    requirements: int
    work_units: int
    plan_items: int
    structural_subjects: int = 0


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
            for fact in packet["accepted_facts"]:
                child = fact["value"]
                answer = self._answer(child)
                answers[child["exact_question"]["question_id"]] = answer
            return {"answers": answers}
        return self._answer(packet)

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
        paragraphs.append(f"Implement behavior outcome {ordinal}.")
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


def run_profile(profile: Profile, *, repository_root: Path) -> dict[str, Any]:
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
    client = _ExampleAnswerClient(profile)
    store = _BenchmarkCheckpointStore(session_id=session_id)
    calls: list[Mapping[str, Any]] = []
    started = time.perf_counter()
    result = run_planning_procedrr(
        process_directory=bundled_process_directory(),
        session=session,
        state_machine=machine,
        checkpoint_store=store,
        client=client,
        options=PlanningOptions(
            model="deterministic-profile",
            provider="deterministic-profile",
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
    try:
        compiled = machine.compile_accepted_plan(
            accepted,
            plan_id=f"benchmark-{profile.name}",
            plan_path="docs/plans/benchmark.yaml",
            proposal_path="docs/plans/benchmark-structrr-proposal.json",
        )
    except Exception as error:
        raise RuntimeError(
            f"profile {profile.name} could not compile; terminal status="
            f"{result.terminal_status.value}"
        ) from error
    by_phase: dict[str, dict[str, float | int]] = defaultdict(
        lambda: {"questions": 0, "api_calls": 0, "elapsed_ms": 0.0}
    )
    for metric in calls:
        phase = str(metric["phase"])
        by_phase[phase]["questions"] += len(metric["question_ids"])
        by_phase[phase]["api_calls"] += int(metric["api_calls"])
        by_phase[phase]["elapsed_ms"] += float(metric["elapsed_ms"])
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
    report = {
        "profile": profile.name,
        "terminal_status": result.terminal_status.value,
        "elapsed_seconds": round(elapsed_seconds, 3),
        "source_blocks": len(source.blocks),
        "requirements": len(machine.accepted_ledger(accepted).requirements),
        "work_units": len(machine.accepted_work_units(accepted)),
        "plan_items": len(compiled.plan.items),
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
        "max_questions_per_provider_call": max_questions_per_provider_call,
        "validation_retries": sum(
            int(metric["validation_failures"]) for metric in calls
        ),
        "api_calls_by_question_kind": dict(client.calls_by_kind),
        "phases": dict(by_phase),
    }
    expected = {
        "terminal_status": "succeeded",
        "requirements": profile.requirements,
        "work_units": profile.work_units,
        "plan_items": profile.plan_items,
    }
    mismatches = {
        field: (report[field], value)
        for field, value in expected.items()
        if report[field] != value
    }
    if mismatches:
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
    args = parser.parse_args()
    repository_root = Path(__file__).resolve().parents[1]
    selected = (
        tuple(PROFILES.values()) if args.profile == "all" else (PROFILES[args.profile],)
    )
    report = [
        run_profile(profile, repository_root=repository_root) for profile in selected
    ]
    print(
        json.dumps({"schema": "planning-scale-report-v1", "profiles": report}, indent=2)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
