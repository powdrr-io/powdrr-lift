"""Capture GitHub review evidence and ask the service to prepare it."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

from powdrr_contracts.models import OperationRequest
from powdrr_local import JsonServiceBridge, LocalPowdrrService

from powdrr_lift.errors import PowdrrExecutionError
from powdrr_lift.execution.builtin_tools import invoke_intrinsic_capability
from powdrr_lift.execution.runtime import ExecutionRuntime
from powdrr_lift.intrinsic_git_gh import GH_TOOL


class ReviewFeedbackPreparationError(PowdrrExecutionError):
    """Review evidence could not be collected or prepared by the service."""


def prepare_review_feedback_ledger(
    *,
    worktree_root: str,
    runtime: ExecutionRuntime,
    workflow_context: Mapping[str, Any],
) -> dict[str, Any]:
    """Capture one PR's current review threads and return the service ledger."""
    reference = workflow_context.get("pr_number")
    pr_parameters: dict[str, Any] = {
        "operation": "pr_view",
        "json_fields": ["number", "url"],
    }
    if isinstance(reference, int) and not isinstance(reference, bool):
        pr_parameters["pr_reference"] = str(reference)
    elif isinstance(workflow_context.get("pr_url"), str):
        pr_parameters["pr_reference"] = workflow_context["pr_url"]

    pr_result = invoke_intrinsic_capability(
        GH_TOOL,
        pr_parameters,
        worktree_root=worktree_root,
        runtime=runtime,
    )
    pr_payload = _successful_json_result(
        pr_result, "could not inspect the pull request"
    )
    pr_number = pr_payload.get("number")
    pr_url = pr_payload.get("url")
    if not isinstance(pr_number, int) or isinstance(pr_number, bool) or pr_number < 1:
        raise ReviewFeedbackPreparationError(
            "GitHub pull-request response did not contain a valid number."
        )
    if not isinstance(pr_url, str):
        raise ReviewFeedbackPreparationError(
            "GitHub pull-request response did not contain its URL."
        )
    repository = _repository_from_pull_request_url(pr_url)

    thread_result = invoke_intrinsic_capability(
        GH_TOOL,
        {
            "operation": "pr_review_threads",
            "repository": repository,
            "pr_reference": pr_number,
        },
        worktree_root=worktree_root,
        runtime=runtime,
    )
    review_evidence = _successful_json_result(
        thread_result, "could not capture pull-request review threads"
    )

    request = OperationRequest(
        request_id=f"review-feedback-{uuid4().hex}",
        operation_kind="review_feedback",
        inputs={"review_evidence": review_evidence},
    )
    update = JsonServiceBridge(LocalPowdrrService()).start(request)
    if update.status != "succeeded":
        failure = update.failure
        detail = failure.message if failure is not None else "service operation failed"
        raise ReviewFeedbackPreparationError(
            f"service could not prepare review feedback: {detail}"
        )
    for artifact in update.output_artifacts:
        if (
            artifact.artifact_id == "review/review-feedback-ledger.json"
            and artifact.media_type == "application/vnd.powdrr.review-feedback+json"
        ):
            try:
                ledger = json.loads(artifact.data)
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise ReviewFeedbackPreparationError(
                    "service returned invalid review-feedback JSON"
                ) from error
            if isinstance(ledger, dict):
                return ledger
    raise ReviewFeedbackPreparationError(
        "service did not return the review-feedback ledger artifact"
    )


def _successful_json_result(
    result: Mapping[str, Any], failure_message: str
) -> dict[str, Any]:
    if result.get("returncode") != 0:
        detail = result.get("stderr")
        suffix = f": {detail}" if isinstance(detail, str) and detail else ""
        raise ReviewFeedbackPreparationError(f"{failure_message}{suffix}")
    try:
        payload = json.loads(str(result.get("stdout", "")))
    except json.JSONDecodeError as error:
        raise ReviewFeedbackPreparationError(
            f"{failure_message}: GitHub returned invalid JSON"
        ) from error
    if not isinstance(payload, dict):
        raise ReviewFeedbackPreparationError(
            f"{failure_message}: GitHub returned a non-object response"
        )
    return payload


def _repository_from_pull_request_url(url: str) -> str:
    parsed = urlparse(url)
    parts = [part for part in parsed.path.split("/") if part]
    if (
        parsed.scheme != "https"
        or parsed.netloc != "github.com"
        or len(parts) < 4
        or parts[2] != "pull"
        or not parts[3].isdecimal()
    ):
        raise ReviewFeedbackPreparationError(
            "GitHub returned an unsupported pull-request URL."
        )
    return f"{parts[0]}/{parts[1]}"


__all__ = [
    "ReviewFeedbackPreparationError",
    "prepare_review_feedback_ledger",
]
