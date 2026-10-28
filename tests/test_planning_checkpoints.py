from __future__ import annotations

import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from powdrr_lift.workrr.github_issue_queue import QueueIssue
from powdrr_lift.workrr.planning.checkpoints import (
    MAX_CHECKPOINT_COMMENT_CHARS,
    GitHubCommentCheckpointStore,
    InMemoryCheckpointStore,
    JournalForkError,
    LocalCheckpointStore,
    LocalDiagnosticCache,
    PlanningCheckpoint,
    PlanningJournalError,
    StaleCheckpointParent,
    reduce_checkpoint_comments,
    render_checkpoint_comments,
)
from powdrr_lift.workrr.planning.contracts import PlanningContractError, Question


def _question(question_id: str = "q-000001") -> dict[str, Any]:
    return Question(
        question_id=question_id,
        kind="extract_requirements",
        contract_version=1,
        subject_ids=("block-0001",),
        input_digest="sha256:" + "a" * 64,
        context_ref_ids=("anchor-0001",),
        expected_answer_schema="requirement-page-v1",
        attempt=1,
    ).to_data()


def _checkpoint(
    *,
    event_id: str,
    sequence: int,
    parent: str | None,
    event_type: str,
    payload: Mapping[str, Any],
    counters: Mapping[str, int] | None = None,
    session_id: str = "planning-session-1",
) -> PlanningCheckpoint:
    return PlanningCheckpoint.create(
        session_id=session_id,
        event_id=event_id,
        sequence=sequence,
        parent_digest=parent,
        event_type=event_type,
        payload=payload,
        id_counters=counters or {"anchor": 1, "requirement": 0},
    )


class _FakeIssueComments:
    def __init__(self) -> None:
        self.comments: list[dict[str, Any]] = []

    def list_comments(self, issue_number: int) -> tuple[Mapping[str, Any], ...]:
        assert issue_number == 44
        return tuple(self.comments)

    def add_comment(self, issue: QueueIssue, body: str) -> None:
        assert issue.number == 44
        self.comments.append({"id": len(self.comments) + 1, "body": body})


def _issue() -> QueueIssue:
    return QueueIssue(
        number=44,
        title="Planning session",
        body="Session journal",
        state="open",
        labels=("powdrr:planning",),
        url="https://github.com/powdrr-io/example/issues/44",
    )


def test_question_start_survives_crash_before_answer_and_resume_reuses_identity() -> (
    None
):
    api = _FakeIssueComments()
    store = GitHubCommentCheckpointStore(api, _issue(), session_id="planning-session-1")
    question = _question()
    started = PlanningCheckpoint.question_started(
        session_id="planning-session-1",
        event_id="event-start-1",
        sequence=1,
        parent_digest=None,
        question=Question.from_mapping(question),
        id_counters={"anchor": 1, "requirement": 0},
    )

    store.append(started, expected_parent_digest=None)
    # Simulate process exit before an accepted-answer checkpoint is appended.
    resumed = GitHubCommentCheckpointStore(
        api, _issue(), session_id="planning-session-1"
    ).load()

    assert resumed.pending_question == question
    assert resumed.accepted_answers == ()
    assert resumed.id_counters == {"anchor": 1, "requirement": 0}


def test_local_checkpoint_store_recovers_events_with_private_atomic_journal(
    tmp_path: Path,
) -> None:
    store = LocalCheckpointStore(tmp_path, session_id="planning-session-1")
    checkpoint = _checkpoint(
        event_id="event-start-1",
        sequence=1,
        parent=None,
        event_type="question_started",
        payload={"question": _question()},
    )

    updated = store.append(checkpoint, expected_parent_digest=None)
    resumed = LocalCheckpointStore(tmp_path, session_id="planning-session-1").load()

    assert updated.head == checkpoint
    assert resumed.events == (checkpoint,)
    assert resumed.pending_question == _question()
    assert store.path.stat().st_mode & 0o777 == 0o600
    assert store.directory.stat().st_mode & 0o777 == 0o700


def test_local_checkpoint_store_rejects_stale_parent(tmp_path: Path) -> None:
    store = LocalCheckpointStore(tmp_path, session_id="planning-session-1")
    checkpoint = _checkpoint(
        event_id="event-start-1",
        sequence=1,
        parent=None,
        event_type="question_started",
        payload={"question": _question()},
    )
    store.append(checkpoint, expected_parent_digest=None)

    with pytest.raises(StaleCheckpointParent):
        store.append(checkpoint, expected_parent_digest=None)


def test_local_checkpoint_store_imports_legacy_issue_comments(tmp_path: Path) -> None:
    api = _FakeIssueComments()
    legacy_store = GitHubCommentCheckpointStore(
        api, _issue(), session_id="planning-session-1"
    )
    checkpoint = _checkpoint(
        event_id="event-start-1",
        sequence=1,
        parent=None,
        event_type="question_started",
        payload={"question": _question()},
    )
    legacy_store.append(checkpoint, expected_parent_digest=None)
    local_store = LocalCheckpointStore(tmp_path, session_id="planning-session-1")

    migrated = local_store.import_legacy_comments(api.comments)

    assert migrated.events == (checkpoint,)
    assert local_store.load().pending_question == _question()


def test_accepted_answer_and_id_counters_resume_without_a_local_cache(
    tmp_path: Path,
) -> None:
    api = _FakeIssueComments()
    store = GitHubCommentCheckpointStore(api, _issue(), session_id="planning-session-1")
    question = _question()
    question_contract = Question.from_mapping(question)
    started = PlanningCheckpoint.question_started(
        session_id="planning-session-1",
        event_id="event-start-1",
        sequence=1,
        parent_digest=None,
        question=question_contract,
        id_counters={"anchor": 1, "requirement": 0},
    )
    first_state = store.append(started, expected_parent_digest=None)
    assert first_state.head is not None
    accepted = PlanningCheckpoint.answer_accepted(
        session_id="planning-session-1",
        event_id="event-answer-1",
        sequence=2,
        parent_digest=started.digest,
        question=question_contract,
        accepted_answer={
            "records": [{"statement": "Preserve this accepted requirement."}],
            "has_more": False,
            "next_anchor": None,
        },
        validation_result={"valid": True},
        id_counters={"anchor": 1, "requirement": 1},
    )
    store.append(accepted, expected_parent_digest=first_state.head.digest)

    # A new machine shares only GitHub comments; its local diagnostics directory
    # starts empty and has no role in reconstructing journal state.
    new_machine = GitHubCommentCheckpointStore(
        api, _issue(), session_id="planning-session-1"
    )
    first_cache = LocalDiagnosticCache(
        tmp_path / "machine-a" / ".git", session_id="planning-session-1"
    )
    second_cache = LocalDiagnosticCache(
        tmp_path / "machine-b" / ".git", session_id="planning-session-1"
    )
    state = new_machine.load()

    assert state.status == "active"
    assert state.head == accepted
    assert (
        state.accepted_answers[0]["accepted_answer"]
        == accepted.payload["accepted_answer"]
    )
    assert state.pending_question is None
    assert state.id_counters == {"anchor": 1, "requirement": 1}
    assert first_cache.directory != second_cache.directory
    assert second_cache.read("q-000001") is None


def test_needs_answer_checkpoint_resumes_the_same_question_and_error() -> None:
    store = InMemoryCheckpointStore(session_id="planning-session-1")
    question = Question.from_mapping(_question())
    started = PlanningCheckpoint.question_started(
        session_id="planning-session-1",
        event_id="event-start",
        sequence=1,
        parent_digest=None,
        question=question,
        id_counters={"requirement": 0},
    )
    state = store.append(started, expected_parent_digest=None)
    assert state.head is not None
    needs_answer = PlanningCheckpoint.needs_answer(
        session_id="planning-session-1",
        event_id="event-needs-answer",
        sequence=2,
        parent_digest=state.head.digest,
        question=question,
        error=PlanningContractError("/records", "must be an array"),
        previous_answer={"records": "not-an-array"},
        id_counters={"requirement": 0},
    )
    resumed = store.append(
        needs_answer,
        expected_parent_digest=state.head.digest,
    )

    assert resumed.pending_question == question.to_data()
    assert resumed.accepted_answers == ()
    assert resumed.events[-1].payload["validation_result"] == {
        "json_pointer": "/records",
        "message": "must be an array",
    }


def test_incomplete_chunk_manifest_is_not_accepted_and_complete_set_recovers() -> None:
    large = _checkpoint(
        event_id="event-large-1",
        sequence=1,
        parent=None,
        event_type="answer_accepted",
        payload={
            "question": _question(),
            "accepted_answer": {"large_text": "αβγδ" * 40_000},
        },
        counters={"requirement": 1},
    )
    rendered = render_checkpoint_comments(large)
    assert len(rendered) > 2
    assert all(
        len(comment.encode("utf-8")) < MAX_CHECKPOINT_COMMENT_CHARS
        for comment in rendered
    )
    assert "checkpoint-manifest" in rendered[-1]
    assert "checkpoint-chunk" in rendered[0]

    incomplete_comments = [{"body": body} for body in rendered[1:]]
    incomplete = reduce_checkpoint_comments(
        incomplete_comments, session_id="planning-session-1"
    )
    assert incomplete.status == "empty"
    assert incomplete.head is None
    assert incomplete.incomplete_manifest_digests == (large.digest,)

    complete = reduce_checkpoint_comments(
        [{"body": body} for body in rendered], session_id="planning-session-1"
    )
    assert complete.status == "active"
    assert complete.head == large


def test_duplicate_comment_delivery_is_idempotent() -> None:
    store = InMemoryCheckpointStore(session_id="planning-session-1")
    event = _checkpoint(
        event_id="event-1",
        sequence=1,
        parent=None,
        event_type="answer_accepted",
        payload={"question": _question(), "accepted_answer": {"records": []}},
    )
    store.append(event, expected_parent_digest=None)
    store.duplicate_comment()

    state = store.load()

    assert state.status == "active"
    assert state.head == event
    assert len(state.events) == 1
    assert len(state.accepted_answers) == 1
    assert state.can_publish


def test_stale_parent_is_rejected_before_append_and_by_the_reducer() -> None:
    store = InMemoryCheckpointStore(session_id="planning-session-1")
    root = _checkpoint(
        event_id="event-root",
        sequence=1,
        parent=None,
        event_type="question_started",
        payload={"question": _question()},
    )
    store.append(root, expected_parent_digest=None)
    stale_parent = "sha256:" + "b" * 64
    stale = _checkpoint(
        event_id="event-stale",
        sequence=2,
        parent=stale_parent,
        event_type="answer_accepted",
        payload={"question": _question(), "accepted_answer": {}},
    )

    with pytest.raises(StaleCheckpointParent):
        store.append(stale, expected_parent_digest=stale_parent)

    reduced = reduce_checkpoint_comments(
        [
            *store.comments,
            {"body": render_checkpoint_comments(stale)[0]},
        ],
        session_id="planning-session-1",
    )
    assert reduced.status == "conflicted"
    assert reduced.stale_event_digests == (stale.digest,)
    assert not reduced.can_publish


def test_fork_blocks_publication_until_resume_selects_a_checkpoint() -> None:
    root = _checkpoint(
        event_id="event-root",
        sequence=1,
        parent=None,
        event_type="question_started",
        payload={"question": _question()},
    )
    left = _checkpoint(
        event_id="event-left",
        sequence=2,
        parent=root.digest,
        event_type="answer_accepted",
        payload={"question": _question(), "accepted_answer": {"branch": "left"}},
    )
    right = _checkpoint(
        event_id="event-right",
        sequence=2,
        parent=root.digest,
        event_type="answer_accepted",
        payload={"question": _question(), "accepted_answer": {"branch": "right"}},
    )
    comments = [
        {"body": comment}
        for checkpoint in (root, left, right)
        for comment in render_checkpoint_comments(checkpoint)
    ]
    conflicted = reduce_checkpoint_comments(comments, session_id="planning-session-1")
    assert conflicted.status == "conflicted"
    assert conflicted.fork_parent_digests == (root.digest,)
    assert not conflicted.can_publish

    selected = reduce_checkpoint_comments(
        comments,
        session_id="planning-session-1",
        selected_checkpoint_digest=left.digest,
    )
    assert selected.status == "active"
    assert selected.head == left
    assert [event.digest for event in selected.events] == [root.digest, left.digest]

    store = InMemoryCheckpointStore(session_id="planning-session-1")
    store.comments.extend(comments)
    successor = _checkpoint(
        event_id="event-successor",
        sequence=3,
        parent=left.digest,
        event_type="question_started",
        payload={"question": _question("q-000002")},
    )
    with pytest.raises(JournalForkError):
        store.append(successor, expected_parent_digest=left.digest)
    resumed = store.append(
        successor,
        expected_parent_digest=left.digest,
        selected_checkpoint_digest=left.digest,
    )
    assert resumed.status == "active"
    assert resumed.head == successor
    assert resumed.can_publish


def test_local_diagnostics_are_atomic_redacted_and_cache_only(tmp_path: Path) -> None:
    common_dir = tmp_path / "repo-a" / ".git"
    common_dir.mkdir(parents=True)
    secret = "private-value-which-must-not-be-written"
    cache = LocalDiagnosticCache(
        common_dir,
        session_id="planning-session-1",
        secret_values=(secret,),
    )
    diagnostic = {
        "question_id": "q-000001",
        "provider": "deepinfra",
        "model": "deepinfra-cheap",
        "schema_version": "coverage-answer-v1",
        "request": {
            "messages": [{"content": f"raw context includes {secret}"}],
            "headers": {
                "Authorization": "Bearer auth-secret-token",
                "X-API-Key": "api-secret-value",
                "Cookie": "session-cookie-value",
            },
        },
        "response": "key sk-test_secret_1234567890",
        "usage": {"input_tokens": 12, "output_tokens": 7},
        "wall_time_ms": 35,
        "validation_errors": [],
    }

    path = cache.write("q-000001", diagnostic)
    written = path.read_text(encoding="utf-8")
    loaded = cache.read("q-000001")
    mode = stat.S_IMODE(path.stat().st_mode)

    assert (
        path
        == common_dir / "powdrr-lift/planning-runs/planning-session-1/q-000001.json"
    )
    assert loaded is not None
    assert loaded["provider"] == "deepinfra"
    assert loaded["usage"] == {"input_tokens": 12, "output_tokens": 7}
    assert loaded["request"]["headers"] == {}
    assert "[REDACTED]" in written
    for sensitive in (
        secret,
        "auth-secret-token",
        "api-secret-value",
        "session-cookie-value",
        "sk-test_secret_1234567890",
    ):
        assert sensitive not in written
    assert mode == 0o600

    other_machine_cache = LocalDiagnosticCache(
        tmp_path / "repo-b" / ".git", session_id="planning-session-1"
    )
    assert other_machine_cache.read("q-000001") is None


def test_checkpoint_data_rejects_tampering_and_non_finite_json() -> None:
    event = _checkpoint(
        event_id="event-1",
        sequence=1,
        parent=None,
        event_type="answer_accepted",
        payload={"accepted_answer": {"records": []}},
    )
    tampered = event.to_data()
    tampered["payload"] = {"accepted_answer": {"records": [{"id": "changed"}]}}

    with pytest.raises(PlanningJournalError, match="digest"):
        PlanningCheckpoint.from_data(tampered)
    with pytest.raises(PlanningJournalError, match="finite JSON"):
        PlanningCheckpoint.create(
            session_id="planning-session-1",
            event_id="event-bad",
            sequence=1,
            parent_digest=None,
            event_type="answer_accepted",
            payload={"invalid": float("nan")},
            id_counters={},
        )


def test_diagnostic_record_requires_reproducibility_fields(tmp_path: Path) -> None:
    cache = LocalDiagnosticCache(
        tmp_path / "repo" / ".git", session_id="planning-session-1"
    )

    with pytest.raises(PlanningJournalError, match="missing"):
        cache.write("q-000001", {"question_id": "q-000001"})
