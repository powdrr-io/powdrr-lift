"""Convert untrusted GitHub API observations into versioned CI contracts."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from powdrr_contracts.ci import (
    CIAnnotation,
    CIConclusion,
    CIJobObservation,
    CIObservationStatus,
)
from powdrr_contracts.models import ContractError


def normalize_github_snapshot(
    snapshot: Mapping[str, Any],
) -> tuple[CIJobObservation, ...]:
    """Normalize job data and annotation pages while retaining provenance hashes."""
    pull_request = snapshot.get("pull_request")
    if not isinstance(pull_request, Mapping):
        raise ContractError("CI snapshot is missing pull_request metadata")
    head = pull_request.get("head")
    head_sha = head.get("sha") if isinstance(head, Mapping) else None
    if not isinstance(head_sha, str):
        raise ContractError("CI snapshot is missing the pull request head SHA")
    raw_jobs = snapshot.get("jobs_by_run")
    raw_annotations = snapshot.get("annotations_by_check_run", {})
    if not isinstance(raw_jobs, Mapping) or not isinstance(raw_annotations, Mapping):
        raise ContractError(
            "CI snapshot job and annotation collections must be objects"
        )
    normalized: list[CIJobObservation] = []
    for run_key in sorted(raw_jobs):
        jobs = raw_jobs[run_key]
        if not isinstance(jobs, (tuple, list)):
            raise ContractError(f"CI snapshot jobs for run {run_key} must be an array")
        matching_run = next(
            (
                item
                for item in snapshot.get("runs", ())
                if str(item.get("id")) == str(run_key)
            ),
            None,
        )
        if not isinstance(matching_run, Mapping):
            raise ContractError(
                f"CI snapshot has no workflow run metadata for {run_key}"
            )
        run_id = matching_run.get("id")
        attempt = matching_run.get("run_attempt", 1)
        for job in jobs:
            if not isinstance(job, Mapping):
                raise ContractError("CI snapshot contains a malformed job")
            if job.get("head_sha", head_sha) != head_sha:
                continue
            job_id = job.get("id")
            if not isinstance(job_id, int):
                raise ContractError("GitHub job is missing its numeric id")
            check_url = job.get("check_run_url")
            check_id = (
                check_url.rstrip("/").rsplit("/", 1)[-1]
                if isinstance(check_url, str)
                else ""
            )
            raw_items = raw_annotations.get(check_id, ())
            if not isinstance(raw_items, (tuple, list)):
                raise ContractError(
                    f"annotations for check run {check_id} must be an array"
                )
            annotations: list[CIAnnotation] = []
            for raw in raw_items:
                if not isinstance(raw, Mapping):
                    raise ContractError("GitHub returned a malformed check annotation")
                canonical = json.dumps(raw, sort_keys=True, separators=(",", ":"))
                annotations.append(
                    CIAnnotation(
                        check_run_id=int(check_id),
                        path=raw.get("path")
                        if isinstance(raw.get("path"), str)
                        else None,
                        start_line=raw.get("start_line")
                        if isinstance(raw.get("start_line"), int)
                        else None,
                        end_line=raw.get("end_line")
                        if isinstance(raw.get("end_line"), int)
                        else None,
                        level=raw.get("annotation_level")
                        if isinstance(raw.get("annotation_level"), str)
                        else None,
                        message=str(
                            raw.get("message")
                            or raw.get("title")
                            or "GitHub check annotation"
                        ),
                        raw_sha256=hashlib.sha256(
                            canonical.encode("utf-8")
                        ).hexdigest(),
                    )
                )
            labels = job.get("labels", ())
            steps = job.get("steps", ())
            normalized.append(
                CIJobObservation(
                    job_id=job_id,
                    run_id=run_id,
                    run_attempt=attempt,
                    head_sha=head_sha,
                    status=_status(job.get("status")),
                    conclusion=_conclusion(job.get("conclusion")),
                    runner_name=job.get("runner_name")
                    if isinstance(job.get("runner_name"), str)
                    else None,
                    runner_id=job.get("runner_id")
                    if isinstance(job.get("runner_id"), int)
                    else None,
                    runner_group_name=job.get("runner_group_name")
                    if isinstance(job.get("runner_group_name"), str)
                    else None,
                    runner_labels=tuple(
                        item for item in labels if isinstance(item, str)
                    )
                    if isinstance(labels, list)
                    else (),
                    steps=tuple(item for item in steps if isinstance(item, Mapping))
                    if isinstance(steps, list)
                    else (),
                    annotations=tuple(annotations),
                )
            )
    return tuple(normalized)


def _status(value: object) -> CIObservationStatus:
    return value if value in {"queued", "in_progress", "completed"} else "unknown"


def _conclusion(value: object) -> CIConclusion | None:
    allowed = {
        "success",
        "failure",
        "cancelled",
        "skipped",
        "neutral",
        "timed_out",
        "action_required",
        "stale",
    }
    return value if value in allowed else ("unknown" if value is not None else None)
