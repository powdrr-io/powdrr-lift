from __future__ import annotations

import json
import stat
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from powdrr_local.planning.checkpoints import (
    GitHubCommentCheckpointStore,
    LocalCheckpointStore,
    LocalDiagnosticCache,
    LocalPlanningTelemetryStore,
)
from powdrr_service.planning.checkpoints import (
    MAX_CHECKPOINT_COMMENT_CHARS,
    InMemoryCheckpointStore,
    JournalForkError,
    PlanningCheckpoint,
    PlanningJournalError,
    StaleCheckpointParent,
    has_complete_checkpoint_archive,
    reduce_checkpoint_comments,
    render_checkpoint_archive_comments,
    render_checkpoint_comments,
)
from powdrr_service.planning.contracts import PlanningContractError, Question


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

    def add_issue_comment(self, issue_number: int, body: str) -> None:
        assert issue_number == 44
        self.comments.append({"id": len(self.comments) + 1, "body": body})


def _initialize_git_repository(path: Path) -> Path:
    subprocess.run(["git", "-C", str(path), "init", "--quiet"], check=True)
    return path


def test_question_start_survives_crash_before_answer_and_resume_reuses_identity() -> (
    None
):
    api = _FakeIssueComments()
    store = GitHubCommentCheckpointStore(api, 44, session_id="planning-session-1")
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
        api, 44, session_id="planning-session-1"
    ).load()

    assert resumed.pending_question == question
    assert resumed.accepted_answers == ()
    assert resumed.id_counters == {"anchor": 1, "requirement": 0}


def test_local_checkpoint_store_recovers_events_with_private_atomic_journal(
    tmp_path: Path,
) -> None:
    _initialize_git_repository(tmp_path)
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


def test_local_checkpoint_store_appends_compact_events_without_rewriting_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _initialize_git_repository(tmp_path)
    store = LocalCheckpointStore(tmp_path, session_id="planning-session-1")
    question = Question.from_mapping(_question())
    started = PlanningCheckpoint.question_started(
        session_id="planning-session-1",
        event_id="event-start-1",
        sequence=1,
        parent_digest=None,
        question=question,
        id_counters={"anchor": 1, "requirement": 0},
    )
    accepted = PlanningCheckpoint.answer_accepted(
        session_id="planning-session-1",
        event_id="event-answer-1",
        sequence=2,
        parent_digest=started.digest,
        question=question,
        accepted_answer={"records": [], "has_more": False, "next_anchor": None},
        validation_result={"valid": True},
        id_counters={"anchor": 1, "requirement": 0},
    )
    store.append(started, expected_parent_digest=None)

    def fail_if_rewritten(_events: Any) -> None:
        pytest.fail("append rewrote the existing journal")

    monkeypatch.setattr(store, "_write", fail_if_rewritten)
    updated = store.append(accepted, expected_parent_digest=started.digest)

    lines = store.path.read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[0]) == {
        "schema": "planning-checkpoint-journal-v2",
        "session_id": "planning-session-1",
    }
    assert len(lines) == 3
    assert updated.events == (started, accepted)
    assert LocalCheckpointStore(
        tmp_path, session_id="planning-session-1"
    ).load().events == (
        started,
        accepted,
    )


def test_local_checkpoint_store_truncates_an_unacknowledged_partial_event(
    tmp_path: Path,
) -> None:
    _initialize_git_repository(tmp_path)
    store = LocalCheckpointStore(tmp_path, session_id="planning-session-1")
    question = Question.from_mapping(_question())
    started = PlanningCheckpoint.question_started(
        session_id="planning-session-1",
        event_id="event-start-1",
        sequence=1,
        parent_digest=None,
        question=question,
        id_counters={"anchor": 1, "requirement": 0},
    )
    store.append(started, expected_parent_digest=None)
    with store.path.open("ab") as stream:
        stream.write(b'{"partial":')

    resumed = LocalCheckpointStore(tmp_path, session_id="planning-session-1")
    assert resumed.load().events == (started,)
    accepted = PlanningCheckpoint.answer_accepted(
        session_id="planning-session-1",
        event_id="event-answer-1",
        sequence=2,
        parent_digest=started.digest,
        question=question,
        accepted_answer={"records": [], "has_more": False, "next_anchor": None},
        validation_result={"valid": True},
        id_counters={"anchor": 1, "requirement": 0},
    )

    resumed.append(accepted, expected_parent_digest=started.digest)

    assert resumed.load().events == (started, accepted)


def test_local_checkpoint_store_rejects_stale_parent(tmp_path: Path) -> None:
    _initialize_git_repository(tmp_path)
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
    _initialize_git_repository(tmp_path)
    api = _FakeIssueComments()
    legacy_store = GitHubCommentCheckpointStore(
        api, 44, session_id="planning-session-1"
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
    accepted = _checkpoint(
        event_id="event-answer-1",
        sequence=2,
        parent=checkpoint.digest,
        event_type="answer_accepted",
        payload={"question": _question(), "accepted_answer": {"records": []}},
    )

    local_store.append(accepted, expected_parent_digest=checkpoint.digest)

    assert (
        json.loads(local_store.path.read_text(encoding="utf-8").splitlines()[0])[
            "schema"
        ]
        == "planning-checkpoint-journal-v2"
    )
    assert LocalCheckpointStore(
        tmp_path, session_id="planning-session-1"
    ).load().events == (checkpoint, accepted)


def test_local_checkpoint_journal_does_not_dirty_pinned_checkout(
    tmp_path: Path,
) -> None:
    repository = _initialize_git_repository(tmp_path)
    store = LocalCheckpointStore(repository, session_id="planning-session-1")

    store.import_legacy_comments([])

    status = subprocess.run(
        [
            "git",
            "-C",
            str(repository),
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
        ],
        check=True,
        capture_output=True,
    ).stdout
    assert status == b""
    assert store.directory == (
        Path(
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(repository),
                    "rev-parse",
                    "--path-format=absolute",
                    "--git-common-dir",
                ],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
        )
        / "powdrr-lift"
        / "planning"
        / "checkpoints"
    )


def test_accepted_answer_and_id_counters_resume_without_a_local_cache(
    tmp_path: Path,
) -> None:
    _initialize_git_repository(tmp_path)
    api = _FakeIssueComments()
    store = GitHubCommentCheckpointStore(api, 44, session_id="planning-session-1")
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
    new_machine = GitHubCommentCheckpointStore(api, 44, session_id="planning-session-1")
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


def test_compressed_archive_reduces_to_original_events_and_new_tail() -> None:
    root = _checkpoint(
        event_id="event-root",
        sequence=1,
        parent=None,
        event_type="question_started",
        payload={"question": _question()},
    )
    accepted = _checkpoint(
        event_id="event-answer",
        sequence=2,
        parent=root.digest,
        event_type="answer_accepted",
        payload={
            "question": _question(),
            "input_digest": "sha256:" + "a" * 64,
            "accepted_answer": {"requirements": [{"id": "req-1"}]},
        },
    )
    archive_comments = render_checkpoint_archive_comments(
        (root, accepted), session_id="planning-session-1"
    )
    tail = _checkpoint(
        event_id="event-next-question",
        sequence=3,
        parent=accepted.digest,
        event_type="question_started",
        payload={"question": _question("q-000002")},
    )

    state = reduce_checkpoint_comments(
        [
            *({"body": body} for body in archive_comments),
            {"body": render_checkpoint_comments(tail)[0]},
        ],
        session_id="planning-session-1",
    )

    assert state.status == "active"
    assert [event.digest for event in state.events] == [
        root.digest,
        accepted.digest,
        tail.digest,
    ]
    assert state.accepted_answers == (accepted.payload,)
    assert state.pending_question == _question("q-000002")


def test_checkpoint_archive_delta_requires_its_parent_archive() -> None:
    root = _checkpoint(
        event_id="event-root",
        sequence=1,
        parent=None,
        event_type="question_started",
        payload={"question": _question()},
    )
    accepted = _checkpoint(
        event_id="event-answer",
        sequence=2,
        parent=root.digest,
        event_type="answer_accepted",
        payload={"question": _question(), "accepted_answer": {"records": []}},
    )
    tail = _checkpoint(
        event_id="event-next-question",
        sequence=3,
        parent=accepted.digest,
        event_type="question_started",
        payload={"question": _question("q-000002")},
    )
    base_archive = render_checkpoint_archive_comments(
        (root, accepted), session_id="planning-session-1"
    )
    delta_archive = render_checkpoint_archive_comments(
        (tail,), session_id="planning-session-1"
    )

    assert not has_complete_checkpoint_archive(
        [{"body": body} for body in delta_archive],
        session_id="planning-session-1",
    )
    combined = [
        *({"body": body} for body in delta_archive),
        *({"body": body} for body in base_archive),
    ]
    assert has_complete_checkpoint_archive(combined, session_id="planning-session-1")
    state = reduce_checkpoint_comments(combined, session_id="planning-session-1")
    assert state.status == "active"
    assert [event.digest for event in state.events] == [
        root.digest,
        accepted.digest,
        tail.digest,
    ]


def test_github_checkpoint_store_publishes_archive_once_and_reloads_it() -> None:
    api = _FakeIssueComments()
    store = GitHubCommentCheckpointStore(api, 44, session_id="planning-session-1")
    root = _checkpoint(
        event_id="event-root",
        sequence=1,
        parent=None,
        event_type="question_started",
        payload={"question": _question()},
    )
    accepted = _checkpoint(
        event_id="event-answer",
        sequence=2,
        parent=root.digest,
        event_type="answer_accepted",
        payload={
            "question": _question(),
            "accepted_answer": {"requirements": []},
        },
    )

    archived = store.publish_archive((root, accepted))
    comment_count = len(api.comments)
    unchanged = store.publish_archive((root, accepted))

    assert archived.head == accepted
    assert unchanged.head == accepted
    assert len(api.comments) == comment_count
    assert any("checkpoint-archive-manifest" in item["body"] for item in api.comments)


def test_github_checkpoint_store_publishes_only_events_after_existing_archive() -> None:
    api = _FakeIssueComments()
    store = GitHubCommentCheckpointStore(api, 44, session_id="planning-session-1")
    root = _checkpoint(
        event_id="event-root",
        sequence=1,
        parent=None,
        event_type="question_started",
        payload={"question": _question()},
    )
    accepted = _checkpoint(
        event_id="event-answer",
        sequence=2,
        parent=root.digest,
        event_type="answer_accepted",
        payload={"question": _question(), "accepted_answer": {"records": []}},
    )
    tail = _checkpoint(
        event_id="event-next-question",
        sequence=3,
        parent=accepted.digest,
        event_type="question_started",
        payload={"question": _question("q-000002")},
    )

    store.publish_archive((root, accepted))
    old_comment_count = len(api.comments)
    tail_archive = render_checkpoint_archive_comments(
        (tail,), session_id="planning-session-1"
    )

    updated = store.publish_archive((root, accepted, tail))

    assert updated.head == tail
    assert len(api.comments) - old_comment_count == len(tail_archive)
    assert [event.digest for event in store.load().events] == [
        root.digest,
        accepted.digest,
        tail.digest,
    ]


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


def test_local_planning_telemetry_summarizes_large_runs_without_journal_data(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(
        ["git", "init", str(repository)], check=True, capture_output=True, text=True
    )
    store = LocalPlanningTelemetryStore(repository, session_id="planning-session-1")
    prior_run = store.start_run()
    current_run = store.start_run()

    def metric(index: int) -> dict[str, Any]:
        return {
            "question_ids": [f"extract-requirements-{index:05d}"],
            "question_kinds": ["extract_requirements"],
            "phase": "source-ledger",
            "input_digests": ["sha256:" + "a" * 64],
            "batch_member_count": 1,
            "request_chars": 900,
            "request_chars_all_attempts": 900,
            "answer_chars": 120,
            "attempts": 1,
            "validation_failures": 0,
            "api_calls": 1,
            "provider_failures": 0,
            "elapsed_ms": 25.0,
            "provider": "test-provider",
            "model": "test-model",
            "outcome": "accepted",
            "deterministic": False,
            "input_digest": "sha256:" + "a" * 64,
            "prompt_tokens": None,
            "completion_tokens": None,
            "pagination": [
                {
                    "question_id": f"extract-requirements-{index:05d}",
                    "subject_id": "block-1",
                    "page_ordinal": 1,
                    "has_more": False,
                    "returned_records": 2,
                    "unique_new_records": 2,
                    "made_progress": True,
                }
            ],
        }

    for index in range(1_000):
        store.record(prior_run if index < 700 else current_run, metric(index))
    store.record_deterministic_answer(
        current_run,
        {
            "question_id": "coverage-req-0001",
            "question_kind": "assess_requirement_coverage",
            "phase": "repository-coverage",
            "input_digest": "sha256:" + "b" * 64,
            "rule_id": "completed-zero-hit-search-v1",
        },
    )
    store.record_repository_search(
        current_run,
        "req-0001",
        {
            "matching_line_count": 0,
            "matching_path_count": 0,
            "evidence_count": 0,
            "basedpyright_query_count": 2,
            "basedpyright_match_count": 0,
            "rg_match_count": 0,
            "basedpyright_available": True,
            "base_commit": "a" * 40,
        },
    )

    summary = store.summary(current_run_id=current_run)

    assert summary["questions"] == 1_000
    assert summary["api_calls"] == 1_000
    assert summary["token_usage_known"] is False
    assert summary["by_question_kind"]["extract_requirements"]["questions"] == 1_000
    assert (
        summary["by_phase_and_question_kind"]["source-ledger"]["extract_requirements"][
            "questions"
        ]
        == 1_000
    )
    assert summary["pagination"]["pages"] == 1_000
    assert summary["pagination"]["unique_new_records"] == 2_000
    assert summary["deterministic_answers"] == {
        "count": 1,
        "by_rule": {"completed-zero-hit-search-v1": 1},
    }
    assert summary["repository_search"]["requirements"] == 1
    assert summary["repository_search"]["basedpyright_queries"] == 2
    assert [run["run"] for run in summary["runs"]] == ["prior", "current"]
    assert summary["runs"][0]["questions"] == 700
    assert summary["runs"][1]["questions"] == 300
    assert len(summary["largest_calls"]) == 10
    persisted = store.path.read_text(encoding="utf-8")
    assert "source_text" not in persisted
    assert "The exact question text" not in persisted
    assert "answer text" not in persisted


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
