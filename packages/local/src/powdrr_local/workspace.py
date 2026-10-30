"""Transactional local persistence for the browser workspace.

The supervisor owns execution and recovery. This store records conversations,
job projections, and ordered events; opening it never starts or resumes work.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from powdrr_contracts.workspace import (
    Conversation,
    Job,
    JobState,
    Message,
    WorkspaceEvent,
)


class WorkspaceConflict(ValueError):
    """The caller used stale state or reused an identifier for different data."""


_TRANSITIONS = {
    JobState.PENDING: {JobState.CANCELLED},
    JobState.READY: {JobState.RUNNING, JobState.CANCELLED},
    JobState.RUNNING: {
        JobState.WAITING_FOR_INPUT,
        JobState.INTERRUPTED,
        JobState.SUCCEEDED,
        JobState.FAILED,
        JobState.CANCELLED,
    },
    JobState.WAITING_FOR_INPUT: {JobState.READY, JobState.CANCELLED},
    JobState.INTERRUPTED: {JobState.READY, JobState.CANCELLED},
    JobState.FAILED: {JobState.READY, JobState.CANCELLED},
    JobState.SUCCEEDED: set(),
    JobState.CANCELLED: set(),
}

_SCHEMA = """
CREATE TABLE conversations (
    conversation_id TEXT PRIMARY KEY, repository_id TEXT NOT NULL,
    title TEXT NOT NULL, specification_refs TEXT NOT NULL,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT, repository_id TEXT NOT NULL,
    entity_type TEXT NOT NULL, entity_id TEXT NOT NULL, event_type TEXT NOT NULL,
    payload TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE messages (
    message_id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
    role TEXT NOT NULL, content TEXT NOT NULL, created_at TEXT NOT NULL,
    sequence INTEGER NOT NULL UNIQUE REFERENCES events(sequence)
);
CREATE TABLE jobs (
    job_id TEXT PRIMARY KEY, repository_id TEXT NOT NULL, kind TEXT NOT NULL,
    title TEXT NOT NULL, state TEXT NOT NULL,
    conversation_id TEXT REFERENCES conversations(conversation_id),
    artifact_refs TEXT NOT NULL, detail TEXT NOT NULL, revision INTEGER NOT NULL,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE dependencies (
    job_id TEXT NOT NULL REFERENCES jobs(job_id),
    dependency_id TEXT NOT NULL REFERENCES jobs(job_id),
    PRIMARY KEY(job_id, dependency_id)
);
CREATE INDEX conversation_repository ON conversations(repository_id, updated_at);
CREATE INDEX message_conversation ON messages(conversation_id, sequence);
CREATE INDEX job_repository ON jobs(repository_id, state, updated_at);
CREATE INDEX event_repository ON events(repository_id, sequence);
PRAGMA user_version = 1;
"""


def _required(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _now() -> str:
    return datetime.now(UTC).isoformat()


class WorkspaceStore:
    """One SQLite database shared by local UI sessions and worker adapters."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as db:
            db.execute("PRAGMA journal_mode = WAL")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                # Serialize first-open initialization across application processes.
                db.execute("BEGIN IMMEDIATE")
                if db.execute("PRAGMA user_version").fetchone()[0] == 0:
                    for statement in _SCHEMA.split(";"):
                        if statement.strip():
                            db.execute(statement)
            elif version != 1:
                raise ValueError(f"Unsupported workspace schema version: {version}")

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _event(
        db: sqlite3.Connection,
        repository: str,
        entity_type: str,
        entity_id: str,
        event_type: str,
        payload: dict[str, Any],
        timestamp: str,
    ) -> int:
        cursor = db.execute(
            "INSERT INTO events(repository_id,entity_type,entity_id,event_type,"
            "payload,created_at) VALUES (?,?,?,?,?,?)",
            (
                repository,
                entity_type,
                entity_id,
                event_type,
                json.dumps(payload, allow_nan=False),
                timestamp,
            ),
        )
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    def create_conversation(self, repository_id: str, title: str) -> Conversation:
        _required(repository_id, "repository_id")
        _required(title, "title")
        identity, timestamp = uuid4().hex, _now()
        with self._connection() as db:
            db.execute(
                "INSERT INTO conversations VALUES (?,?,?,?,?,?)",
                (identity, repository_id, title, "[]", timestamp, timestamp),
            )
            self._event(
                db,
                repository_id,
                "conversation",
                identity,
                "conversation.created",
                {"title": title},
                timestamp,
            )
        return self.get_conversation(identity)

    @staticmethod
    def _conversation(row: sqlite3.Row | None) -> Conversation:
        if row is None:
            raise KeyError("Conversation does not exist")
        return Conversation(
            row["conversation_id"],
            row["repository_id"],
            row["title"],
            tuple(json.loads(row["specification_refs"])),
            row["created_at"],
            row["updated_at"],
        )

    def get_conversation(self, conversation_id: str) -> Conversation:
        with self._connection() as db:
            return self._conversation(
                db.execute(
                    "SELECT * FROM conversations WHERE conversation_id=?",
                    (conversation_id,),
                ).fetchone()
            )

    def list_conversations(self, repository_id: str) -> tuple[Conversation, ...]:
        with self._connection() as db:
            return tuple(
                self._conversation(row)
                for row in db.execute(
                    "SELECT * FROM conversations WHERE repository_id=? "
                    "ORDER BY updated_at DESC, conversation_id",
                    (repository_id,),
                )
            )

    def update_conversation(
        self,
        conversation_id: str,
        *,
        title: str,
        specification_refs: Sequence[str] | None = None,
    ) -> Conversation:
        _required(title, "title")
        refs = tuple(
            dict.fromkeys(
                _required(ref, "specification_ref")
                for ref in (specification_refs or ())
            )
        )
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            conversation = self._conversation(
                db.execute(
                    "SELECT * FROM conversations WHERE conversation_id=?",
                    (conversation_id,),
                ).fetchone()
            )
            if specification_refs is None:
                refs = conversation.specification_refs
            timestamp = _now()
            db.execute(
                "UPDATE conversations SET title=?,specification_refs=?,updated_at=? "
                "WHERE conversation_id=?",
                (title, json.dumps(refs), timestamp, conversation_id),
            )
            self._event(
                db,
                conversation.repository_id,
                "conversation",
                conversation_id,
                "conversation.updated",
                {"title": title, "specification_refs": list(refs)},
                timestamp,
            )
        return self.get_conversation(conversation_id)

    def append_message(
        self,
        conversation_id: str,
        *,
        message_id: str,
        role: str,
        content: str,
    ) -> Message:
        """A retried submission with the same ID creates one message and event."""
        _required(message_id, "message_id")
        _required(content, "content")
        if role not in {"user", "assistant", "system"}:
            raise ValueError("Unsupported message role")
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM messages WHERE message_id=?", (message_id,)
            ).fetchone()
            if row is not None:
                if (row["conversation_id"], row["role"], row["content"]) != (
                    conversation_id,
                    role,
                    content,
                ):
                    raise WorkspaceConflict("Message ID already has different content")
                return Message(**dict(row))
            conversation = self._conversation(
                db.execute(
                    "SELECT * FROM conversations WHERE conversation_id=?",
                    (conversation_id,),
                ).fetchone()
            )
            timestamp = _now()
            sequence = self._event(
                db,
                conversation.repository_id,
                "conversation",
                conversation_id,
                "message.created",
                {"message_id": message_id, "role": role, "content": content},
                timestamp,
            )
            db.execute(
                "INSERT INTO messages VALUES (?,?,?,?,?,?)",
                (message_id, conversation_id, role, content, timestamp, sequence),
            )
            db.execute(
                "UPDATE conversations SET updated_at=? WHERE conversation_id=?",
                (timestamp, conversation_id),
            )
            return Message(
                message_id, conversation_id, role, content, timestamp, sequence
            )

    def list_messages(self, conversation_id: str) -> tuple[Message, ...]:
        with self._connection() as db:
            return tuple(
                Message(**dict(row))
                for row in db.execute(
                    "SELECT * FROM messages WHERE conversation_id=? ORDER BY sequence",
                    (conversation_id,),
                )
            )

    @staticmethod
    def _job(db: sqlite3.Connection, job_id: str) -> Job:
        row = db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            raise KeyError("Job does not exist")
        dependencies = tuple(
            item[0]
            for item in db.execute(
                "SELECT dependency_id FROM dependencies WHERE job_id=? "
                "ORDER BY dependency_id",
                (job_id,),
            )
        )
        return Job(
            row["job_id"],
            row["repository_id"],
            row["kind"],
            row["title"],
            JobState(row["state"]),
            row["conversation_id"],
            dependencies,
            tuple(json.loads(row["artifact_refs"])),
            row["detail"],
            row["revision"],
            row["created_at"],
            row["updated_at"],
        )

    def create_job(
        self,
        repository_id: str,
        *,
        kind: str,
        title: str,
        conversation_id: str | None = None,
        dependency_ids: Sequence[str] = (),
    ) -> Job:
        _required(repository_id, "repository_id")
        _required(title, "title")
        if kind not in {"planning", "pr_execution"}:
            raise ValueError("Unsupported job kind")
        identity, timestamp = uuid4().hex, _now()
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if conversation_id is not None:
                conversation = self._conversation(
                    db.execute(
                        "SELECT * FROM conversations WHERE conversation_id=?",
                        (conversation_id,),
                    ).fetchone()
                )
                if conversation.repository_id != repository_id:
                    raise ValueError("Job and conversation repositories differ")
            dependencies = tuple(dict.fromkeys(dependency_ids))
            prerequisite_jobs = tuple(self._job(db, item) for item in dependencies)
            if any(job.repository_id != repository_id for job in prerequisite_jobs):
                raise ValueError("Dependencies must belong to the same repository")
            state = (
                JobState.READY
                if all(job.state == JobState.SUCCEEDED for job in prerequisite_jobs)
                else JobState.PENDING
            )
            db.execute(
                "INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    identity,
                    repository_id,
                    kind,
                    title,
                    state,
                    conversation_id,
                    "[]",
                    "",
                    1,
                    timestamp,
                    timestamp,
                ),
            )
            db.executemany(
                "INSERT INTO dependencies VALUES (?,?)",
                ((identity, dependency) for dependency in dependencies),
            )
            self._event(
                db,
                repository_id,
                "job",
                identity,
                "job.created",
                {
                    "state": state,
                    "kind": kind,
                    "title": title,
                    "conversation_id": conversation_id,
                    "dependency_ids": list(dependencies),
                    "revision": 1,
                },
                timestamp,
            )
            return self._job(db, identity)

    def get_job(self, job_id: str) -> Job:
        with self._connection() as db:
            return self._job(db, job_id)

    def list_jobs(
        self,
        repository_id: str,
        *,
        states: Sequence[JobState] = (),
    ) -> tuple[Job, ...]:
        with self._connection() as db:
            rows = db.execute(
                "SELECT job_id,state FROM jobs WHERE repository_id=? "
                "ORDER BY updated_at DESC,job_id",
                (repository_id,),
            ).fetchall()
            return tuple(
                self._job(db, row["job_id"])
                for row in rows
                if not states or row["state"] in states
            )

    def transition_job(
        self,
        job_id: str,
        *,
        expected_revision: int,
        state: JobState,
        detail: str = "",
        artifact_refs: Sequence[str] | None = None,
    ) -> Job:
        state = JobState(state)
        refs = tuple(
            dict.fromkeys(
                _required(ref, "artifact_ref") for ref in (artifact_refs or ())
            )
        )
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            job = self._job(db, job_id)
            if artifact_refs is None:
                refs = job.artifact_refs
            if job.revision != expected_revision:
                raise WorkspaceConflict("Job revision has changed")
            if job.state in {JobState.SUCCEEDED, JobState.CANCELLED}:
                raise ValueError("A finished job cannot be updated")
            if state != job.state and state not in _TRANSITIONS[job.state]:
                raise ValueError(f"Invalid job transition: {job.state} -> {state}")
            if state == JobState.READY and any(
                self._job(db, item).state != JobState.SUCCEEDED
                for item in job.dependency_ids
            ):
                raise ValueError("Job dependencies are incomplete")
            timestamp = _now()
            db.execute(
                "UPDATE jobs SET state=?,detail=?,artifact_refs=?,revision=revision+1,"
                "updated_at=? WHERE job_id=?",
                (state, detail, json.dumps(refs), timestamp, job_id),
            )
            self._event(
                db,
                job.repository_id,
                "job",
                job_id,
                "job.updated",
                {
                    "state": state,
                    "detail": detail,
                    "artifact_refs": list(refs),
                    "revision": job.revision + 1,
                },
                timestamp,
            )
            if state == JobState.SUCCEEDED:
                self._release_dependents(db, job_id, timestamp)
            return self._job(db, job_id)

    def _release_dependents(
        self,
        db: sqlite3.Connection,
        completed_id: str,
        timestamp: str,
    ) -> None:
        ids = [
            row[0]
            for row in db.execute(
                "SELECT job_id FROM dependencies WHERE dependency_id=?",
                (completed_id,),
            )
        ]
        for identity in ids:
            job = self._job(db, identity)
            if job.state != JobState.PENDING or any(
                self._job(db, dependency).state != JobState.SUCCEEDED
                for dependency in job.dependency_ids
            ):
                continue
            db.execute(
                "UPDATE jobs SET state=?,revision=revision+1,updated_at=? "
                "WHERE job_id=?",
                (JobState.READY, timestamp, identity),
            )
            self._event(
                db,
                job.repository_id,
                "job",
                identity,
                "job.updated",
                {"state": JobState.READY, "revision": job.revision + 1},
                timestamp,
            )

    def read_events(
        self,
        repository_id: str,
        *,
        after: int = 0,
        limit: int = 100,
    ) -> tuple[WorkspaceEvent, ...]:
        """Replay events after an exclusive cursor for browser reconnection."""
        if after < 0 or not 1 <= limit <= 1000:
            raise ValueError("Invalid event cursor or limit")
        with self._connection() as db:
            return tuple(
                WorkspaceEvent(
                    row["sequence"],
                    row["repository_id"],
                    row["entity_type"],
                    row["entity_id"],
                    row["event_type"],
                    json.loads(row["payload"]),
                    row["created_at"],
                )
                for row in db.execute(
                    "SELECT * FROM events WHERE repository_id=? AND sequence>? "
                    "ORDER BY sequence LIMIT ?",
                    (repository_id, after, limit),
                )
            )
