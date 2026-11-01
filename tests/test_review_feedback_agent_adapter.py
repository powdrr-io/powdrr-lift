from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from powdrr_lift.execution.runtime import ExecutionRuntime
from powdrr_lift.workrr import review_feedback
from powdrr_lift.workrr.review_feedback import (
    ReviewFeedbackPreparationError,
    prepare_review_feedback_ledger,
)


def test_agent_captures_github_threads_then_invokes_review_feedback_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []
    evidence = {
        "pull_request": {
            "number": 42,
            "title": "Fix behavior",
            "headRefOid": "a" * 40,
        },
        "review_threads": [
            {
                "id": "thread-1",
                "isResolved": False,
                "isOutdated": False,
                "path": "src/example.py",
                "line": 12,
                "comments": {
                    "nodes": [
                        {
                            "id": "comment-1",
                            "author": {"login": "reviewer"},
                            "body": "Add an edge-case test.",
                        }
                    ]
                },
            },
            {
                "id": "thread-resolved",
                "isResolved": True,
                "isOutdated": False,
                "comments": {"nodes": []},
            },
        ],
    }

    def fake_invoke(
        tool: str,
        parameters: dict[str, Any],
        *,
        worktree_root: str,
        runtime: ExecutionRuntime,
    ) -> dict[str, Any]:
        calls.append({"tool": tool, "parameters": parameters, "root": worktree_root})
        assert runtime is not None
        if parameters["operation"] == "pr_view":
            return {
                "returncode": 0,
                "stdout": json.dumps(
                    {"number": 42, "url": "https://github.com/owner/repo/pull/42"}
                ),
            }
        return {"returncode": 0, "stdout": json.dumps(evidence)}

    monkeypatch.setattr(review_feedback, "invoke_intrinsic_capability", fake_invoke)
    runtime = ExecutionRuntime(
        "review-feedback-test",
        profile_id="address-review-comments",
        workflow_directory=tmp_path / ".powdrr",
        repo_root=tmp_path,
    )

    ledger = prepare_review_feedback_ledger(
        worktree_root=str(tmp_path),
        runtime=runtime,
        workflow_context={"pr_number": 42},
    )

    assert [call["parameters"]["operation"] for call in calls] == [
        "pr_view",
        "pr_review_threads",
    ]
    assert calls[1]["parameters"]["repository"] == "owner/repo"
    assert ledger["status"] == "ready"
    assert ledger["captured_head_sha"] == "a" * 40
    assert [thread["thread_id"] for thread in ledger["threads"]] == ["thread-1"]


def test_agent_fails_review_preparation_when_github_thread_fetch_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    responses = [
        {
            "returncode": 0,
            "stdout": json.dumps(
                {"number": 42, "url": "https://github.com/owner/repo/pull/42"}
            ),
        },
        {"returncode": 1, "stderr": "GitHub unavailable"},
    ]

    def fake_invoke(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return responses.pop(0)

    monkeypatch.setattr(review_feedback, "invoke_intrinsic_capability", fake_invoke)
    runtime = ExecutionRuntime(
        "review-feedback-failure-test",
        profile_id="address-review-comments",
        workflow_directory=tmp_path / ".powdrr",
        repo_root=tmp_path,
    )

    with pytest.raises(ReviewFeedbackPreparationError, match="could not capture"):
        prepare_review_feedback_ledger(
            worktree_root=str(tmp_path),
            runtime=runtime,
            workflow_context={},
        )
