"""Service orchestration between a CI observation and a repair worker request."""

from __future__ import annotations

import shlex
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from powdrr_contracts.ci import LocalReproduction

from powdrr_service.ci_response.evidence import CIFailureUnit
from powdrr_service.ci_response.repair import compile_ci_repair_request
from powdrr_service.ci_response.research import research_github_actions_failure


def research_observed_ci_failure(
    observation: Mapping[str, Any],
    *,
    api_key: str | None,
    artifact_root: Path | None = None,
    search: Callable[[str, str], Mapping[str, object]] | None = None,
    fetch: Callable[[str], tuple[int, str, bytes]] | None = None,
) -> dict[str, Any]:
    """Select the first failure deterministically and research its CI context."""
    failures = observation.get("failures")
    if not isinstance(failures, list):
        raise ValueError("CI observation must contain a failure list")
    if not failures:
        return {"status": "no_failure", "failure": None, "evidence": []}
    failure = failures[0]
    if not isinstance(failure, Mapping):
        raise ValueError("CI observation contains a malformed failure")
    failure_id = failure.get("failure_id")
    signature = failure.get("signature")
    if not isinstance(failure_id, str) or not failure_id.strip():
        raise ValueError("CI observation failure is missing its stable ID")
    if not isinstance(signature, str) or not signature.strip():
        raise ValueError("CI observation failure is missing its signature")
    labels = failure.get("runner_labels", [])
    if not isinstance(labels, list) or not all(
        isinstance(item, str) for item in labels
    ):
        raise ValueError("CI observation runner labels are malformed")
    workflow_path = failure.get("workflow_path")
    if workflow_path is not None and not isinstance(workflow_path, str):
        raise ValueError("CI observation workflow path is malformed")
    head_sha = observation.get("head_sha")
    if not isinstance(head_sha, str) or len(head_sha) not in (40, 64):
        raise ValueError("CI observation is missing its exact PR head")
    destination = artifact_root or (
        Path(tempfile.gettempdir()) / "powdrr-lift-ci-research" / failure_id
    )
    research = research_github_actions_failure(
        failure_signature=signature,
        workflow_path=workflow_path,
        runner_labels=labels,
        artifact_root=destination,
        api_key=api_key,
        search=search,
        fetch=fetch,
    )
    return {
        "status": "failure_selected",
        "head_sha": head_sha,
        "failure": dict(failure),
        "research": research,
        "evidence": research.get("evidence", []),
    }


def compile_observed_ci_repair_request(
    *,
    observation: Mapping[str, Any],
    research_result: Mapping[str, Any],
    decision: Mapping[str, Any],
    verification_command: str,
) -> dict[str, Any]:
    """Bind a small Procedrr targeting decision to the selected observed failure."""
    if research_result.get("status") == "no_failure":
        return {"status": "no_failure", "repair_request": None}
    failure_data = research_result.get("failure")
    if not isinstance(failure_data, Mapping):
        raise ValueError("research result does not contain the selected failure")
    selected_failure_id = failure_data.get("failure_id")
    if decision.get("failure_id") not in (None, selected_failure_id):
        raise ValueError("repair decision does not match the selected failure ID")
    disposition = decision.get("disposition")
    if disposition not in {"repository_change", "human_prerequisite", "unresolved"}:
        raise ValueError("repair targeting decision has an unsupported disposition")
    if disposition != "repository_change":
        return {
            "status": disposition,
            "failure_id": selected_failure_id,
            "explanation": str(decision.get("explanation", ""))[:1200],
        }
    pr_metadata = observation.get("pull_request_metadata")
    if (
        not isinstance(pr_metadata, Mapping)
        or not isinstance(pr_metadata.get("head_ref"), str)
        or not str(pr_metadata["head_ref"]).startswith("powdrr/")
        or not isinstance(pr_metadata.get("base_ref"), str)
        or not str(pr_metadata["base_ref"]).strip()
    ):
        return {
            "status": "unresolved",
            "failure_id": selected_failure_id,
            "explanation": (
                "The PR branch metadata does not support safe in-place publication. "
                + str(decision.get("explanation", ""))
            )[:1200],
        }
    observation_head = observation.get("head_sha")
    research_head = research_result.get("head_sha")
    if not isinstance(observation_head, str) or research_head != observation_head:
        raise ValueError("research result belongs to a different PR head")
    failure = _failure_from_data(failure_data, research_head)
    reproduction_data = failure_data.get("local_reproduction")
    reproduction = LocalReproduction.from_dict(reproduction_data)
    repository = observation.get("repository")
    pull_request = observation.get("pull_request")
    changed_paths = observation.get("changed_paths")
    proposed_paths = decision.get("target_paths")
    required_behavior = decision.get("required_behavior")
    if not isinstance(repository, str) or not isinstance(pull_request, int):
        raise ValueError("CI observation is missing repository or PR identity")
    if not isinstance(changed_paths, list) or not all(
        isinstance(item, str) for item in changed_paths
    ):
        raise ValueError("CI observation changed paths are malformed")
    if not isinstance(proposed_paths, list) or not all(
        isinstance(item, str) for item in proposed_paths
    ):
        raise ValueError("repair targeting paths are malformed")
    if not isinstance(required_behavior, str) or not required_behavior.strip():
        raise ValueError("repair targeting decision omitted required behavior")
    try:
        acceptance = tuple(shlex.split(verification_command))
    except ValueError as error:
        raise ValueError("declared verification command is malformed") from error
    if not acceptance:
        raise ValueError("declared verification command is empty")
    raw_evidence = research_result.get("evidence", [])
    if not isinstance(raw_evidence, list) or not all(
        isinstance(item, Mapping) for item in raw_evidence
    ):
        raise ValueError("research evidence is malformed")
    evidence = tuple(
        {str(key): str(value) for key, value in item.items()} for item in raw_evidence
    )
    request = compile_ci_repair_request(
        repository=repository,
        pull_request=pull_request,
        failure=failure,
        reproduction=reproduction,
        changed_paths=tuple(changed_paths),
        proposed_paths=tuple(proposed_paths),
        required_behavior=required_behavior,
        acceptance_command=acceptance,
        research_evidence=evidence,
    )
    return {
        "status": "repository_change",
        "failure_id": failure.failure_id,
        "explanation": str(decision.get("explanation", ""))[:1200],
        "repair_request": request,
    }


def finalize_ci_repair_result(
    *,
    preparation: Mapping[str, Any],
    attempt: Mapping[str, Any],
    validation: Mapping[str, Any],
    publication_url: str | None = None,
    follow_up_observation: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble a bounded repair transition record for the issue-backed task."""
    result: dict[str, Any] = {
        "status": "published"
        if publication_url
        else "validated"
        if validation.get("passed") is True
        else "rejected",
        "failure_id": attempt.get("failure_id"),
        "head_sha": attempt.get("head_sha"),
        "contract_fingerprint": validation.get("contract_fingerprint"),
        "validation": dict(validation),
        "attempt": {
            "attempt_id": attempt.get("attempt_id"),
            "status": attempt.get("status"),
            "changed_paths": attempt.get("changed_paths", []),
            "diff_fingerprint": attempt.get("diff_fingerprint"),
        },
        "repair_request": preparation.get("repair_request"),
    }
    if publication_url is not None:
        result["pull_request_url"] = publication_url
    if follow_up_observation is not None:
        result["follow_up_observation"] = dict(follow_up_observation)
    return result


def _failure_from_data(data: Mapping[str, Any], head_sha: str) -> CIFailureUnit:
    strings = ("failure_id", "phase", "signature")
    for key in strings:
        if not isinstance(data.get(key), str) or not str(data[key]).strip():
            raise ValueError(f"observed CI failure is missing {key}")
    numbers = ("job_id", "run_id")
    for key in numbers:
        value = data.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise ValueError(f"observed CI failure has invalid {key}")
    attempt = data.get("run_attempt")
    if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1:
        raise ValueError("observed CI failure has invalid run_attempt")
    evidence = data.get("evidence", [])
    if not isinstance(evidence, list) or not all(
        isinstance(item, str) for item in evidence
    ):
        raise ValueError("observed CI failure evidence is malformed")
    return CIFailureUnit(
        failure_id=str(data["failure_id"]),
        job_id=data["job_id"],
        run_id=data["run_id"],
        run_attempt=attempt,
        head_sha=head_sha,
        phase=str(data["phase"]),
        signature=str(data["signature"]),
        evidence=tuple(evidence),
        locally_executable=bool(data.get("locally_executable", True)),
    )
