"""Compact, durable issue checkpoint rendering for CI response observations."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence

from powdrr_contracts.ci import CIJobObservation, LocalReproduction

from powdrr_service.ci_response.evidence import CIFailureUnit, build_failure_units

_CHECKPOINT_MARKER = "<!-- powdrr-ci-response:v1 -->"


def render_ci_observation_checkpoint(
    *,
    repository: str,
    pull_request_number: int,
    issue_number: int,
    head_sha: str,
    jobs: Sequence[CIJobObservation],
    failures: Sequence[CIFailureUnit] | None = None,
    reproductions: dict[str, LocalReproduction] | None = None,
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
            "",
            *rows,
            "",
            "This checkpoint records observed CI evidence only; it does not mark "
            "the PR ready for review.",
        )
    )
    return observation_id, body


def _render_failure(unit: CIFailureUnit, reproduction: LocalReproduction | None) -> str:
    repro = reproduction.status if reproduction is not None else "not_run"
    return (
        f"- `{unit.failure_id}`: **{unit.phase}**; {unit.signature[:500]}; "
        f"evidence `{', '.join(unit.evidence)}`; local reproduction `{repro}`."
    )


__all__ = ["render_ci_observation_checkpoint"]
