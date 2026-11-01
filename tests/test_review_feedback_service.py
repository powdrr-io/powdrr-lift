from __future__ import annotations

import json

import pytest
from powdrr_contracts.models import OperationRequest
from powdrr_service.local_service import LocalPowdrrService
from powdrr_service.review_feedback import (
    ReviewFeedbackError,
    build_review_feedback_ledger,
)


def _thread(
    thread_id: str,
    *,
    resolved: bool = False,
    outdated: bool = False,
    comments: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    if comments is None:
        comments = [
            {
                "id": f"{thread_id}-root",
                "author": {"login": "reviewer"},
                "body": "Please handle this edge case.",
            }
        ]
    return {
        "id": thread_id,
        "isResolved": resolved,
        "isOutdated": outdated,
        "path": "src/example.py",
        "line": 12,
        "startLine": 10,
        "diffSide": "RIGHT",
        "startDiffSide": "RIGHT",
        "comments": {"nodes": comments},
    }


def _evidence(threads: list[dict[str, object]]) -> dict[str, object]:
    return {
        "pull_request": {
            "number": 42,
            "title": "Example change",
            "headRefOid": "a" * 40,
        },
        "review_threads": threads,
    }


def test_review_feedback_ledger_keeps_only_current_unresolved_full_threads() -> None:
    reply: dict[str, object] = {
        "id": "current-reply",
        "author": {"login": "maintainer"},
        "body": "I will add a regression test.",
    }
    root: dict[str, object] = {
        "id": "current-root",
        "author": {"login": "reviewer"},
        "body": "Please handle this edge case.",
    }
    ledger = build_review_feedback_ledger(
        _evidence(
            [
                _thread("current", comments=[root, reply]),
                _thread("resolved", resolved=True),
                _thread("outdated", outdated=True),
            ]
        )
    )

    assert ledger["status"] == "ready"
    assert ledger["thread_count"] == 1
    assert ledger["captured_head_sha"] == "a" * 40
    assert ledger["threads"] == [
        {
            "thread_id": "current",
            "root_comment_id": "current-root",
            "author": "reviewer",
            "path": "src/example.py",
            "line": 12,
            "start_line": 10,
            "diff_side": "RIGHT",
            "start_diff_side": "RIGHT",
            "conversation": [
                {
                    "id": "current-root",
                    "author": {"login": "reviewer"},
                    "body": "Please handle this edge case.",
                },
                reply,
            ],
        }
    ]


def test_review_feedback_empty_threads_are_successfully_distinguished() -> None:
    ledger = build_review_feedback_ledger(
        _evidence([_thread("resolved", resolved=True), _thread("old", outdated=True)])
    )

    assert ledger["status"] == "empty"
    assert ledger["thread_count"] == 0
    assert ledger["threads"] == []


@pytest.mark.parametrize(
    ("evidence", "message"),
    [
        ({"review_threads": []}, "pull_request metadata"),
        ({"pull_request": {"number": 42}, "review_threads": []}, "headRefOid"),
        (
            {
                **_evidence([_thread("missing-state")]),
                "review_threads": [{"id": "missing-state"}],
            },
            "isResolved and isOutdated",
        ),
        (
            _evidence([_thread("empty-comments", comments=[])]),
            "must contain comments",
        ),
    ],
)
def test_review_feedback_rejects_incomplete_evidence(
    evidence: dict[str, object], message: str
) -> None:
    with pytest.raises(ReviewFeedbackError, match=message):
        build_review_feedback_ledger(evidence)


def test_review_feedback_operation_returns_ledger_artifact() -> None:
    update = LocalPowdrrService().start(
        OperationRequest(
            request_id="review-42",
            operation_kind="review_feedback",
            inputs={"review_evidence": _evidence([_thread("current")])},
        )
    )

    assert update.status == "succeeded"
    assert update.progress == ({"phase": "review_threads_prepared"},)
    artifact = update.output_artifacts[0]
    assert artifact.artifact_id == "review/review-feedback-ledger.json"
    assert artifact.media_type == "application/vnd.powdrr.review-feedback+json"
    assert json.loads(artifact.data)["threads"][0]["thread_id"] == "current"


def test_review_feedback_operation_returns_failure_for_invalid_evidence() -> None:
    update = LocalPowdrrService().start(
        OperationRequest(
            request_id="review-invalid",
            operation_kind="review_feedback",
            inputs={"review_evidence": {"review_threads": []}},
        )
    )

    assert update.status == "failed"
    assert update.failure is not None
    assert update.failure.category == "invalid_review_evidence"


def test_review_feedback_operation_marks_no_actionable_threads_as_empty() -> None:
    update = LocalPowdrrService().start(
        OperationRequest(
            request_id="review-empty",
            operation_kind="review_feedback",
            inputs={"review_evidence": _evidence([_thread("resolved", resolved=True)])},
        )
    )

    assert update.status == "succeeded"
    assert update.progress == ({"phase": "review_threads_empty"},)
    result = json.loads(update.output_artifacts[0].data)
    assert result["status"] == "empty"
