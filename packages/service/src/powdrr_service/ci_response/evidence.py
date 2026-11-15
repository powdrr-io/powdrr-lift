"""Deterministic normalization of GitHub Actions failure evidence."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from powdrr_contracts.ci import CIJobLogEvidence, CIJobObservation

_RUNNER_REJECTION_PATTERNS = (
    re.compile(r"configuration .+ is not supported", re.IGNORECASE),
    re.compile(r"requested labels? .+ (?:not found|not supported)", re.IGNORECASE),
    re.compile(r"no runner matching the specified labels", re.IGNORECASE),
)
_PYTEST_FAILURE_SUMMARY_RE = re.compile(
    r"^\s*FAILED\s+(?P<node>\S+::\S+)(?:\s+-\s+(?P<reason>.*))?$"
)


@dataclass(frozen=True, slots=True)
class CIFailureUnit:
    """One deterministic failure surface from a completed Actions job."""

    failure_id: str
    job_id: int
    run_id: int
    run_attempt: int
    head_sha: str
    phase: str
    signature: str
    evidence: tuple[str, ...]
    locally_executable: bool
    test_node_id: str | None = None


def normalize_failure_signature(message: str) -> str:
    """Normalize volatile details while preserving the actual reported error."""
    value = message.strip().lower()
    value = re.sub(r"\bgh[pousr]_[a-z0-9_]{20,}\b", "<redacted-token>", value)
    value = re.sub(r"\bgithub_pat_[a-z0-9_]{20,}\b", "<redacted-token>", value)
    value = re.sub(r"\bbearer\s+\S+", "bearer <redacted>", value)
    value = re.sub(
        r"\b(password|token|secret|api[_-]?key)(\s*[=:]\s*)\S+",
        r"\1\2<redacted>",
        value,
    )
    value = re.sub(r"/home/runner/work/[^\s]+", "<checkout>", value)
    value = re.sub(r"/Users/[^\s]+", "<checkout>", value)
    value = re.sub(r"\b[0-9a-f]{40,64}\b", "<sha>", value)
    value = re.sub(r"\s+", " ", value)
    return value


def build_failure_units(
    jobs: tuple[CIJobObservation, ...],
) -> tuple[CIFailureUnit, ...]:
    """Extract only completed failed jobs, including pre-job annotations."""
    units: list[CIFailureUnit] = []
    for job in jobs:
        if job.status != "completed" or job.conclusion not in {
            "failure",
            "timed_out",
            "cancelled",
        }:
            continue
        if job.conclusion == "cancelled" and not job.annotations:
            continue
        if job.annotations:
            for annotation in job.annotations:
                signature = normalize_failure_signature(annotation.message)
                runner_rejected = any(
                    pattern.search(annotation.message)
                    for pattern in _RUNNER_REJECTION_PATTERNS
                )
                before_runner = job.runner_id in (None, 0) and not job.steps
                phase = (
                    "runner_provisioning"
                    if runner_rejected and before_runner
                    else "check_annotation"
                )
                units.append(
                    _failure_unit(
                        job,
                        phase,
                        signature,
                        (
                            f"annotation:{annotation.check_run_id}",
                            *_log_evidence_reference(job),
                        ),
                        not (runner_rejected and before_runner),
                    )
                )
            continue
        step_failures = [
            step
            for step in job.steps
            if step.get("conclusion") in {"failure", "timed_out"}
        ]
        pytest_failures = _pytest_failure_summaries(job.log_evidence)
        if pytest_failures:
            step_refs = tuple(
                f"step:{step.get('name') or 'unnamed step'}" for step in step_failures
            )
            for test_node_id, signature in pytest_failures:
                units.append(
                    _failure_unit(
                        job,
                        "test_failure",
                        signature,
                        (
                            f"test_node:{test_node_id}",
                            *step_refs,
                            *_log_evidence_reference(job),
                        ),
                        True,
                        test_node_id=test_node_id,
                    )
                )
            continue
        for step in step_failures:
            name = str(step.get("name") or "unnamed step")
            # The Actions job API exposes the failed step label but not its
            # failure text. Mark it as a label so local output cannot
            # accidentally match a generic word such as "pytest".
            signature = f"failed step: {normalize_failure_signature(name)}"
            units.append(
                _failure_unit(
                    job,
                    "step_failure",
                    signature,
                    (f"step:{name}", *_log_evidence_reference(job)),
                    True,
                )
            )
        if not step_failures:
            units.append(
                _failure_unit(
                    job,
                    "unknown_job_failure",
                    "job failed without step evidence",
                    (f"job:{job.job_id}", *_log_evidence_reference(job)),
                    bool(job.steps),
                )
            )
    return tuple(units)


def _log_evidence_reference(job: CIJobObservation) -> tuple[str, ...]:
    evidence = job.log_evidence
    if evidence is None:
        return ()
    if evidence.status == "available":
        return (f"job_log:{job.job_id}:sha256:{evidence.raw_sha256}",)
    return (f"job_log_unavailable:{job.job_id}:{evidence.reason}",)


def _pytest_failure_summaries(
    log_evidence: CIJobLogEvidence | None,
) -> tuple[tuple[str, str], ...]:
    if (
        log_evidence is None
        or log_evidence.status != "available"
        or log_evidence.excerpt is None
    ):
        return ()
    summaries: list[tuple[str, str]] = []
    for line in log_evidence.excerpt.splitlines():
        match = _PYTEST_FAILURE_SUMMARY_RE.match(line)
        if match is None:
            continue
        node_id = match.group("node")
        path, separator, test_name = node_id.partition("::")
        if (
            not separator
            or not test_name
            or not path.endswith(".py")
            or path.startswith(("/", "-"))
            or ".." in path.replace("\\", "/").split("/")
            or any(ord(char) < 32 for char in node_id)
        ):
            continue
        reason = (match.group("reason") or "").strip()
        signature = f"FAILED {node_id}"
        if reason:
            signature += f" - {reason}"
        summaries.append((node_id, normalize_failure_signature(signature)))
    return tuple(dict.fromkeys(summaries))


def _failure_unit(
    job: CIJobObservation,
    phase: str,
    signature: str,
    evidence: tuple[str, ...],
    locally_executable: bool,
    *,
    test_node_id: str | None = None,
) -> CIFailureUnit:
    identity = "\0".join(
        (
            job.head_sha,
            str(job.run_id),
            str(job.run_attempt),
            str(job.job_id),
            phase,
            signature,
        )
    )
    return CIFailureUnit(
        failure_id="ci-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16],
        job_id=job.job_id,
        run_id=job.run_id,
        run_attempt=job.run_attempt,
        head_sha=job.head_sha,
        phase=phase,
        signature=signature,
        evidence=evidence,
        locally_executable=locally_executable,
        test_node_id=test_node_id,
    )
