"""Current story proof from pinned links, semantic review and actual test events."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from powdrr_service.decision_obligation import content_fingerprint
from powdrr_service.planning.story_allocation import pin_story
from powdrr_service.structrr.story_coverage import (
    StoryCoverageError,
    declared_case_coverage,
    parse_story_case_refs,
    story_case_ref,
)
from powdrr_service.structrr.user_stories import parse_story_catalog
from powdrr_service.verification.story_test_links import require_story_test_quality

RECEIPT_SCHEMA = "story-execution-receipt-v1"


@dataclass(frozen=True, slots=True)
class StoryExecutionContext:
    repository: str
    candidate_sha: str
    configuration_digest: str
    environment_digest: str

    def to_data(self) -> dict[str, Any]:
        return {
            "repository": self.repository,
            "candidate_sha": self.candidate_sha,
            "configuration_digest": self.configuration_digest,
            "environment_digest": self.environment_digest,
        }

    @classmethod
    def from_data(cls, value: Mapping[str, Any]) -> StoryExecutionContext:
        if (
            set(value)
            != {
                "repository",
                "candidate_sha",
                "configuration_digest",
                "environment_digest",
            }
            or not isinstance(value.get("repository"), str)
            or not value["repository"].strip()
        ):
            raise StoryCoverageError(
                "execution context requires repository and exact "
                "candidate/configuration/environment identities"
            )
        if not isinstance(value.get("candidate_sha"), str) or not re.fullmatch(
            r"[a-f0-9]{40}|[a-f0-9]{64}", value["candidate_sha"]
        ):
            raise StoryCoverageError(
                "execution context requires an immutable candidate Git tree SHA"
            )
        for field in ("configuration_digest", "environment_digest"):
            if not isinstance(value.get(field), str) or not re.fullmatch(
                r"sha256:[a-f0-9]{64}", value[field]
            ):
                raise StoryCoverageError(f"execution context has invalid {field}")
        return cls(**value)


@dataclass(frozen=True, slots=True)
class StoryExecutionReceipt:
    """Canonical JSON makes a receipt immutable and independently reloadable."""

    _json: str

    def to_data(self) -> dict[str, Any]:
        return json.loads(self._json)

    @classmethod
    def from_data(cls, value: Mapping[str, Any]) -> StoryExecutionReceipt:
        required = {
            "schema_version",
            "receipt_id",
            "story_ref",
            "selector",
            "test_source_digest",
            "context",
            "dependency_digests",
            "link_fingerprint",
            "review_fingerprint",
            "execution_fingerprint",
            "observation_ids",
            "linked_observation_ids",
            "assertion_lines",
            "outcome",
            "reason",
            "artifact_refs",
        }
        if set(value) != required or value.get("schema_version") != RECEIPT_SCHEMA:
            raise StoryCoverageError(
                "unsupported or incomplete story execution receipt"
            )
        parse_story_case_refs([value["story_ref"]])
        StoryExecutionContext.from_data(value["context"])
        if value["outcome"] not in {"passed", "failed", "blocked", "not_run"}:
            raise StoryCoverageError(
                "receipt outcome must preserve passed/failed/blocked/not_run"
            )
        if not isinstance(value["selector"], str) or "::" not in value["selector"]:
            raise StoryCoverageError("receipt requires a collected test selector")
        for field in (
            "test_source_digest",
            "link_fingerprint",
            "review_fingerprint",
            "execution_fingerprint",
        ):
            if not isinstance(value[field], str) or not re.fullmatch(
                r"sha256:[a-f0-9]{64}", value[field]
            ):
                raise StoryCoverageError(f"receipt has invalid {field}")
        linked = value["linked_observation_ids"]
        if (
            not isinstance(linked, list)
            or not all(isinstance(item, str) for item in linked)
            or not set(linked) <= set(value["story_ref"]["observation_ids"])
        ):
            raise StoryCoverageError("receipt has foreign linked observations")
        if not isinstance(value["observation_ids"], list) or not set(
            value["observation_ids"]
        ) <= set(value["story_ref"]["observation_ids"]):
            raise StoryCoverageError("receipt contains foreign observations")
        if not set(value["observation_ids"]) <= set(linked) or (
            value["outcome"] != "passed" and value["observation_ids"]
        ):
            raise StoryCoverageError(
                "only passed linked assertions establish observations"
            )
        dependencies = value["dependency_digests"]
        if not isinstance(dependencies, Mapping) or any(
            not isinstance(key, str)
            or not isinstance(digest, str)
            or not re.fullmatch(r"sha256:[a-f0-9]{64}", digest)
            for key, digest in dependencies.items()
        ):
            raise StoryCoverageError("receipt dependency identities are invalid")
        if not isinstance(value["assertion_lines"], list) or any(
            not isinstance(line, list)
            or len(line) != 2
            or not isinstance(line[0], str)
            or type(line[1]) is not int
            or line[1] <= 0
            for line in value["assertion_lines"]
        ):
            raise StoryCoverageError("receipt assertion locations are invalid")
        if (
            not isinstance(value["artifact_refs"], list)
            or not value["artifact_refs"]
            or any(
                not isinstance(ref, str) or not ref for ref in value["artifact_refs"]
            )
        ):
            raise StoryCoverageError("receipt requires execution artifact references")
        unsigned = {key: item for key, item in value.items() if key != "receipt_id"}
        if value["receipt_id"] != "receipt:" + content_fingerprint(unsigned)[7:]:
            raise StoryCoverageError("story execution receipt fingerprint is invalid")
        return cls(json.dumps(dict(value), sort_keys=True, ensure_ascii=False))


def story_review_fingerprint(evidence: Mapping[str, Any]) -> str:
    return content_fingerprint(
        {
            key: evidence.get(key)
            for key in (
                "contract",
                "test_obligations",
                "story_test_links",
                "test_sources",
            )
        }
    )


def compile_story_execution_receipts(
    *,
    snapshot: Mapping[str, Any],
    manifest: Mapping[str, Any],
    review: Mapping[str, Any],
    review_evidence: Mapping[str, Any],
    context: Mapping[str, Any],
    execution: Mapping[str, Any],
    context_after: Mapping[str, Any],
    artifact_refs: Sequence[str],
) -> tuple[StoryExecutionReceipt, ...]:
    """Only observed call outcomes and executed linked assertions can pass."""
    identity = StoryExecutionContext.from_data(context)
    after = StoryExecutionContext.from_data(context_after)
    review_error = None
    try:
        require_story_test_quality(review, manifest)
        if review.get("story_review_fingerprint") != story_review_fingerprint(
            review_evidence
        ):
            raise StoryCoverageError(
                "story semantic review is stale or lacks input identity"
            )
        if review_evidence.get("story_test_links") != manifest:
            raise StoryCoverageError(
                "review names a different story assertion manifest"
            )
    except ValueError as error:
        review_error = str(error)
    observed = execution.get("story_execution", {})
    reports = observed.get("reports", [])
    collected = observed.get("selectors", [])
    catalog = parse_story_catalog(snapshot)
    receipts = []
    execution_fingerprint = content_fingerprint(dict(execution))
    for link in manifest.get("links", []):
        phases = [
            report for report in reports if report.get("selector") == link["selector"]
        ]
        call = next(
            (report for report in phases if report.get("phase") == "call"), None
        )
        reason = "The collected test passed and executed its linked assertions."
        outcome: Literal["passed", "failed", "blocked", "not_run"] = "passed"
        proven = sorted(
            {assertion["observation_id"] for assertion in link["assertions"]}
        )
        executed = {
            tuple(line)
            for line in observed.get("assertion_lines", {}).get(link["selector"], [])
        }
        expected_lines = {
            (link["path"], assertion["start_line"]) for assertion in link["assertions"]
        }
        expected_source = link["test_source_digest"].removeprefix("sha256:")
        source_before = observed.get("sources_before", {}).get(link["path"])
        source_after = observed.get("sources_after", {}).get(link["path"])
        if review_error:
            outcome, reason = "blocked", review_error
        elif identity != after:
            outcome, reason = (
                "blocked",
                "candidate or execution environment changed during verification",
            )
        elif observed.get("runtime") != observed.get("runtime_after"):
            outcome, reason = (
                "blocked",
                "runtime dependencies changed during verification",
            )
        elif (
            observed.get("collection_errors") != 0
            or observed.get("schema_version") != "pytest-story-execution-v1"
        ):
            outcome, reason = "blocked", "test collection or execution was unavailable"
        elif link["selector"] not in collected:
            outcome, reason = "not_run", "the required test was not collected"
        elif source_before != expected_source or source_after != expected_source:
            outcome, reason = (
                "blocked",
                "test source changed or differs from the reviewed source",
            )
        elif any(phase.get("wasxfail") for phase in phases):
            outcome, reason = (
                "not_run",
                "expected failures do not establish required story observations",
            )
        elif any(
            phase.get("outcome") == "failed" and phase.get("phase") != "call"
            for phase in phases
        ):
            outcome, reason = "blocked", "test setup or teardown failed"
        elif call is None:
            outcome, reason = "not_run", "the test call did not run"
        elif call.get("outcome") == "skipped":
            outcome, reason = "not_run", "the required test was skipped"
        elif call.get("outcome") == "failed":
            outcome, reason = (
                "failed",
                "the linked test failed: " + str(call.get("diagnostic", ""))[:1000],
            )
        elif call.get("outcome") != "passed" or {
            phase.get("phase") for phase in phases if phase.get("outcome") == "passed"
        } != {"setup", "call", "teardown"}:
            outcome, reason = "blocked", "test phase evidence is incomplete"
        elif not expected_lines <= executed:
            outcome, reason = "not_run", "linked assertion lines did not execute"
        for ref in parse_story_case_refs(link["story_refs"]):
            dependencies = dict(pin_story(snapshot, ref.story_id).dependency_digests)
            if ref != story_case_ref(catalog, ref.case_id):
                raise StoryCoverageError("cannot execute a stale story case contract")
            data = {
                "schema_version": RECEIPT_SCHEMA,
                "story_ref": ref.to_data(),
                "selector": link["selector"],
                "test_source_digest": link["test_source_digest"],
                "context": identity.to_data(),
                "dependency_digests": dependencies,
                "link_fingerprint": content_fingerprint(link),
                "review_fingerprint": story_review_fingerprint(review_evidence),
                "execution_fingerprint": execution_fingerprint,
                "observation_ids": proven if outcome == "passed" else [],
                "linked_observation_ids": proven,
                "assertion_lines": sorted([list(line) for line in executed]),
                "outcome": outcome,
                "reason": reason,
                "artifact_refs": list(artifact_refs),
            }
            data["receipt_id"] = "receipt:" + content_fingerprint(data)[7:]
            receipts.append(StoryExecutionReceipt.from_data(data))
    return tuple(receipts)


def story_coverage_report(
    snapshot: Mapping[str, Any],
    *,
    context: Mapping[str, Any],
    manifest: Mapping[str, Any],
    receipts: Sequence[StoryExecutionReceipt],
    story_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Current coverage is an explicit case/observation matrix, never a percentage."""
    current_context = StoryExecutionContext.from_data(context)
    catalog = parse_story_catalog(snapshot)
    links = {content_fingerprint(link): link for link in manifest.get("links", [])}
    selected = set(story_ids) if story_ids is not None else set(catalog.stories_by_id)
    rows = []
    history = []
    for receipt in receipts:
        raw = receipt.to_data()
        ref = parse_story_case_refs([raw["story_ref"]])[0]
        try:
            stale = ref != story_case_ref(catalog, ref.case_id) or raw[
                "dependency_digests"
            ] != dict(pin_story(snapshot, ref.story_id).dependency_digests)
        except (ValueError, KeyError):
            stale = True
        stale = (
            stale
            or current_context != StoryExecutionContext.from_data(raw["context"])
            or raw["link_fingerprint"] not in links
        )
        history.append({"receipt": raw, "status": "stale" if stale else raw["outcome"]})
    for story_id in sorted(selected):
        if story_id not in catalog.stories_by_id:
            raise StoryCoverageError(f"unknown story in coverage query: {story_id}")
        missing_targets = declared_case_coverage(catalog, story_id)
        case_rows = []
        for case in catalog.user_story_cases:
            if case.story_id != story_id:
                continue
            observations = []
            for observation_id in sorted(case.expected_observations):
                applicable = [
                    entry
                    for entry in history
                    if entry["receipt"]["story_ref"]["case_id"] == case.id
                    and observation_id in entry["receipt"]["linked_observation_ids"]
                ]
                statuses = {entry["status"] for entry in applicable}
                # A failing applicable test cannot be hidden by another passing link.
                status = next(
                    (
                        status
                        for status in (
                            "failed",
                            "blocked",
                            "not_run",
                            "passed",
                            "stale",
                        )
                        if status in statuses
                    ),
                    "not_run",
                )
                if status == "passed" and not any(
                    observation_id in entry["receipt"]["observation_ids"]
                    for entry in applicable
                    if entry["status"] == "passed"
                ):
                    status = "not_run"
                observations.append(
                    {
                        "observation_id": observation_id,
                        "status": status,
                        "tests": [
                            {
                                "selector": entry["receipt"]["selector"],
                                "receipt_id": entry["receipt"]["receipt_id"],
                                "status": entry["status"],
                                "assertion_lines": entry["receipt"]["assertion_lines"],
                                "review_fingerprint": entry["receipt"][
                                    "review_fingerprint"
                                ],
                            }
                            for entry in applicable
                        ],
                    }
                )
            status = (
                "passed"
                if observations
                and all(row["status"] == "passed" for row in observations)
                else next(
                    (
                        status
                        for status in ("failed", "blocked", "not_run", "stale")
                        if any(row["status"] == status for row in observations)
                    ),
                    "not_run",
                )
            )
            case_rows.append(
                {
                    "case_id": case.id,
                    "transition_ids": list(case.transition_ids),
                    "step_ids": list(story_case_ref(catalog, case.id).step_ids),
                    "outcome": next(
                        step.outcome
                        for step in catalog.user_story_steps
                        if step.id == story_case_ref(catalog, case.id).step_ids[-1]
                    ),
                    "observations": observations,
                    "status": status,
                }
            )
        verified = (
            bool(case_rows)
            and not missing_targets
            and all(row["status"] == "passed" for row in case_rows)
        )
        mapping = []
        for step in catalog.user_story_steps:
            if step.story_id != story_id:
                continue
            justifications = [
                {
                    "boundary": links[entry["receipt"]["link_fingerprint"]]["boundary"],
                    "receipt_id": entry["receipt"]["receipt_id"],
                    "review_fingerprint": entry["receipt"]["review_fingerprint"],
                    "reason": "The semantic review accepted this integration "
                    "boundary for the case containing this step.",
                }
                for entry in history
                if entry["status"] == "passed"
                and step.id in entry["receipt"]["story_ref"]["step_ids"]
            ]
            mapping.append(
                {
                    "step_id": step.id,
                    "functionality_refs": [
                        {
                            "section": reference.section,
                            "subject_id": reference.subject_id,
                        }
                        for reference in step.functionality_refs
                    ],
                    "integration_justifications": justifications,
                }
            )
        verified = verified and all(
            row["functionality_refs"] or row["integration_justifications"]
            for row in mapping
        )
        rows.append(
            {
                "story_id": story_id,
                "status": "verified"
                if verified
                else "implemented_partially"
                if any(
                    observation["status"] == "passed"
                    for case in case_rows
                    for observation in case["observations"]
                )
                else "ready_for_validation"
                if case_rows and not missing_targets
                else "planned",
                "missing_targets": list(missing_targets),
                "cases": case_rows,
                "implementation_mapping": mapping,
            }
        )
    return {
        "schema_version": "story-coverage-report-v1",
        "context": current_context.to_data(),
        "stories": rows,
        "receipts": history,
        "verified": bool(rows) and all(row["status"] == "verified" for row in rows),
        "assigned_verified": bool(links)
        and all(
            any(
                entry["status"] == "passed"
                and entry["receipt"]["story_ref"]["case_id"] == ref["case_id"]
                and observation in entry["receipt"]["observation_ids"]
                for entry in history
            )
            for link in links.values()
            for ref in link["story_refs"]
            for observation in ref["observation_ids"]
        )
        and all(entry["status"] in {"passed", "stale"} for entry in history),
    }
