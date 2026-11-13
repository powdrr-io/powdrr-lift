"""Compact, durable issue checkpoint rendering for CI response observations."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence

from powdrr_contracts.ci import CIJobObservation, LocalReproduction

from powdrr_service.ci_response.evidence import CIFailureUnit, build_failure_units

_CHECKPOINT_MARKER = "<!-- powdrr-ci-response:v1 -->"
_STALE_MARKER = "<!-- powdrr-ci-response-stale:v1 -->"
_OBSERVATION_HEADER = re.compile(
    r"CI response observation `([0-9a-f]{16})` for PR #([1-9][0-9]*)\s+"
    r"at head `([0-9a-f]{40,64})` \(issue #([1-9][0-9]*)\)\."
)
_OBSERVATION_TIME = re.compile(r"Observed at `([^`]+)`")


def render_ci_observation_checkpoint(
    *,
    repository: str,
    pull_request_number: int,
    issue_number: int,
    head_sha: str,
    jobs: Sequence[CIJobObservation],
    failures: Sequence[CIFailureUnit] | None = None,
    reproductions: dict[str, LocalReproduction] | None = None,
    observed_at: str | None = None,
) -> tuple[str, str]:
    """Return a deterministic observation ID and bounded issue comment."""
    units = (
        tuple(failures) if failures is not None else build_failure_units(tuple(jobs))
    )
    identity = {
        "repository": repository,
        "pull_request": pull_request_number,
        "issue": issue_number,
        "head_sha": head_sha,
        "jobs": [job.to_dict() for job in jobs],
        "reproductions": {
            failure_id: result.to_dict()
            for failure_id, result in sorted((reproductions or {}).items())
        },
    }
    observation_id = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    visible_units = units[:20]
    rows = [
        _render_failure(unit, (reproductions or {}).get(unit.failure_id))
        for unit in visible_units
    ]
    if len(units) > len(visible_units):
        rows.append(
            f"- {len(units) - len(visible_units)} additional failure units "
            "were omitted; "
            "retrieve them from the current GitHub observation."
        )
    if not rows:
        rows.append(
            "No completed failed Actions jobs were found for the current PR head."
        )
    body = "\n".join(
        (
            _CHECKPOINT_MARKER,
            "CI response observation "
            f"`{observation_id[:16]}` for PR #{pull_request_number} ",
            f"at head `{head_sha}` (issue #{issue_number}).",
            f"Observed at `{observed_at or 'unknown'}`; freshness **current**.",
            "",
            *rows,
            "",
            "This checkpoint records observed CI evidence only; it does not mark "
            "the PR ready for review.",
        )
    )
    return observation_id, body


def render_stale_ci_observation_checkpoint(
    *,
    comments: Sequence[Mapping[str, object]],
    repository: str,
    pull_request_number: int,
    issue_number: int,
    stale_since: str,
    reason: str,
) -> tuple[str, str] | None:
    """Mark the latest saved CI observation stale after a failed refresh."""
    candidates: list[tuple[str, int, str, str, str | None]] = []
    already_stale: set[str] = set()
    stale_payload_pattern = re.compile(r"```json\s*\n(.*?)\n```", re.DOTALL)
    for index, comment in enumerate(comments):
        body = comment.get("body")
        if not isinstance(body, str):
            continue
        if _STALE_MARKER in body:
            match = stale_payload_pattern.search(body)
            id_match = re.search(r"Staleness record `([0-9a-f]{16})`", body)
            if match is not None:
                try:
                    payload = json.loads(match.group(1))
                except json.JSONDecodeError:
                    payload = None
                fingerprint = (
                    hashlib.sha256(
                        json.dumps(
                            payload, sort_keys=True, separators=(",", ":")
                        ).encode("utf-8")
                    ).hexdigest()[:16]
                    if isinstance(payload, Mapping)
                    else None
                )
                if (
                    isinstance(payload, Mapping)
                    and payload.get("schema")
                    == "https://powdrr.io/schemas/ci-response-stale-checkpoint-v1"
                    and payload.get("repository") == repository
                    and payload.get("pull_request") == pull_request_number
                    and payload.get("issue") == issue_number
                    and isinstance(payload.get("prior_observation_id"), str)
                    and id_match is not None
                    and fingerprint == id_match.group(1)
                ):
                    already_stale.add(str(payload["prior_observation_id"]))
            continue
        if _CHECKPOINT_MARKER not in body:
            continue
        header = _OBSERVATION_HEADER.search(body)
        if header is None:
            continue
        observation_id, pr_value, head_sha, issue_value = header.groups()
        if int(pr_value) != pull_request_number or int(issue_value) != issue_number:
            continue
        observed_match = _OBSERVATION_TIME.search(body)
        observed_at = observed_match.group(1) if observed_match is not None else None
        if observed_at is None and isinstance(comment.get("created_at"), str):
            observed_at = str(comment["created_at"])
        candidates.append(
            (observed_at or "", index, observation_id, head_sha, observed_at)
        )
    if not candidates:
        return None
    _, _, observation_id, head_sha, last_remote_observed_at = max(
        candidates, key=lambda item: (item[0], item[1])
    )
    if observation_id in already_stale:
        return None

    bounded_reason = " ".join(reason.split())[:500]
    payload = {
        "schema": "https://powdrr.io/schemas/ci-response-stale-checkpoint-v1",
        "repository": repository,
        "pull_request": pull_request_number,
        "issue": issue_number,
        "status": "unknown",
        "prior_observation_id": observation_id,
        "head_sha": head_sha,
        "last_remote_observed_at": last_remote_observed_at,
        "stale_since": stale_since,
        "stale_reason": bounded_reason,
    }
    stale_id = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    body = "\n".join(
        (
            _STALE_MARKER,
            f"CI response refresh for PR #{pull_request_number} "
            f"(issue #{issue_number}) is **stale; current status unknown**.",
            f"Last successful observation `{observation_id}` was for head "
            f"`{head_sha}` at `{last_remote_observed_at or 'unknown time'}`.",
            f"Refresh failed at `{stale_since}`: {bounded_reason}",
            f"Staleness record `{stale_id[:16]}`.",
            "",
            "```json",
            json.dumps(payload, indent=2, sort_keys=True),
            "```",
        )
    )
    return stale_id, body


def _render_failure(unit: CIFailureUnit, reproduction: LocalReproduction | None) -> str:
    repro = reproduction.status if reproduction is not None else "not_run"
    reason = f" ({reproduction.reason[:240]})" if reproduction is not None else ""
    output_fingerprint = ""
    if reproduction is not None and (
        reproduction.stdout_sha256 or reproduction.stderr_sha256
    ):
        output_fingerprint = (
            "; output sha256 "
            f"{reproduction.stdout_sha256 or 'none'}/"
            f"{reproduction.stderr_sha256 or 'none'}"
        )
    return (
        f"- `{unit.failure_id}`: **{unit.phase}**; {unit.signature[:500]}; "
        f"evidence `{', '.join(unit.evidence)}`; local reproduction `{repro}`"
        f"{output_fingerprint}{reason}."
    )


__all__ = ["render_ci_observation_checkpoint"]
