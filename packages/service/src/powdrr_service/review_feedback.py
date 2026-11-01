"""Deterministic review-thread evidence preparation for review workflows."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any


class ReviewFeedbackError(ValueError):
    """Review-thread evidence is missing fields required for a trustworthy ledger."""


def build_review_feedback_ledger(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Build a stable ledger from the successful output of ``gh pr_review_threads``.

    GitHub reads remain an agent capability. The service owns the deterministic
    projection of that evidence: resolved and outdated threads are excluded, while
    every current unresolved thread retains its complete conversation and anchor.
    Semantic classification (fix, answer, defer, or clarify) is left to the
    review process.
    """
    pull_request = payload.get("pull_request")
    threads = payload.get("review_threads")
    if not isinstance(pull_request, Mapping):
        raise ReviewFeedbackError("review evidence must contain pull_request metadata")
    if not isinstance(threads, list):
        raise ReviewFeedbackError("review evidence must contain a review_threads list")

    pr_number = pull_request.get("number")
    head_sha = pull_request.get("headRefOid")
    if not isinstance(pr_number, int) or isinstance(pr_number, bool) or pr_number < 1:
        raise ReviewFeedbackError("pull_request.number must be a positive integer")
    if not isinstance(head_sha, str) or not re.fullmatch(
        r"(?:[0-9a-f]{40}|[0-9a-f]{64})", head_sha
    ):
        raise ReviewFeedbackError(
            "pull_request.headRefOid must be a full hexadecimal commit SHA"
        )

    ledger: list[dict[str, Any]] = []
    for index, thread in enumerate(threads):
        if not isinstance(thread, Mapping):
            raise ReviewFeedbackError(f"review_threads[{index}] must be an object")
        thread_id = thread.get("id")
        resolved = thread.get("isResolved")
        outdated = thread.get("isOutdated")
        if not isinstance(thread_id, str) or not thread_id:
            raise ReviewFeedbackError(f"review_threads[{index}].id must be non-empty")
        if not isinstance(resolved, bool) or not isinstance(outdated, bool):
            raise ReviewFeedbackError(
                f"review_threads[{index}] must contain boolean isResolved "
                "and isOutdated"
            )
        if resolved or outdated:
            continue

        comments_connection = thread.get("comments")
        comments = (
            comments_connection.get("nodes")
            if isinstance(comments_connection, Mapping)
            else None
        )
        if not isinstance(comments, list) or not comments:
            raise ReviewFeedbackError(
                f"current unresolved thread {thread_id} must contain comments"
            )
        if any(not isinstance(comment, Mapping) for comment in comments):
            raise ReviewFeedbackError(
                f"current unresolved thread {thread_id} contains an invalid comment"
            )

        root = comments[0]
        root_comment_id = root.get("id")
        if not isinstance(root_comment_id, str) or not root_comment_id:
            raise ReviewFeedbackError(
                f"current unresolved thread {thread_id} has no root comment ID"
            )
        author = root.get("author")
        author_login = author.get("login") if isinstance(author, Mapping) else None
        ledger.append(
            {
                "thread_id": thread_id,
                "root_comment_id": root_comment_id,
                "author": author_login,
                "path": thread.get("path"),
                "line": thread.get("line"),
                "start_line": thread.get("startLine"),
                "diff_side": thread.get("diffSide"),
                "start_diff_side": thread.get("startDiffSide"),
                "conversation": [dict(comment) for comment in comments],
            }
        )

    return {
        "status": "ready" if ledger else "empty",
        "pull_request": dict(pull_request),
        "captured_head_sha": head_sha,
        "threads": ledger,
        "thread_count": len(ledger),
    }


__all__ = ["ReviewFeedbackError", "build_review_feedback_ledger"]
