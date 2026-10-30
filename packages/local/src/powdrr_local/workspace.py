"""Transactional local persistence for the browser workspace.

The supervisor owns execution and recovery. This store records conversations,
job projections, and ordered events; opening it never starts or resumes work.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from powdrr_contracts.workspace import (
    Conversation,
    EventType,
    Job,
    JobKind,
    JobState,
    Message,
    MessageRole,
    WorkspaceEvent,
    WorkspaceRepository,
)


class WorkspaceConflict(ValueError):
    """The caller used stale state or reused an identifier for different data."""


_TRANSITIONS = {
    JobState.PENDING: {JobState.CANCELLED},
    JobState.READY: {JobState.RUNNING, JobState.CANCELLED},
    JobState.RUNNING: {
        JobState.WAITING_FOR_INPUT,
        JobState.WAITING_FOR_EXTERNAL,
        JobState.INTERRUPTED,
        JobState.SUCCEEDED,
        JobState.FAILED,
        JobState.CANCELLED,
    },
    JobState.WAITING_FOR_INPUT: {JobState.READY, JobState.CANCELLED},
    JobState.WAITING_FOR_EXTERNAL: {JobState.CANCELLED},
    JobState.INTERRUPTED: {JobState.READY, JobState.CANCELLED},
    JobState.FAILED: {JobState.READY, JobState.CANCELLED},
    JobState.SUCCEEDED: set(),
    JobState.CANCELLED: set(),
}

_SCHEMA = """
CREATE TABLE conversations (
    conversation_id TEXT PRIMARY KEY, repository_id TEXT NOT NULL,
    title TEXT NOT NULL, specification_refs TEXT NOT NULL,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, revision INTEGER NOT NULL
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
CREATE TABLE command_requests (
    repository_id TEXT NOT NULL, request_id TEXT NOT NULL,
    payload_digest TEXT NOT NULL, result_type TEXT NOT NULL,
    result_json TEXT NOT NULL, created_at TEXT NOT NULL,
    PRIMARY KEY(repository_id, request_id)
);
CREATE TABLE repositories (
    repository_id TEXT PRIMARY KEY, display_name TEXT NOT NULL,
    canonical_root TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL
);
PRAGMA user_version = 4;
"""

_REPOSITORY_TABLE = (
    "CREATE TABLE repositories ("
    "repository_id TEXT PRIMARY KEY, display_name TEXT NOT NULL, "
    "canonical_root TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL)"
)


def _required(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _request_digest(command: dict[str, Any]) -> str:
    canonical = json.dumps(
        command, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


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
            elif version == 1:
                db.execute("BEGIN IMMEDIATE")
                if db.execute("PRAGMA user_version").fetchone()[0] == 1:
                    db.execute(
                        "ALTER TABLE conversations ADD COLUMN revision INTEGER "
                        "NOT NULL DEFAULT 1"
                    )
                    db.execute(
                        "CREATE TABLE command_requests ("
                        "repository_id TEXT NOT NULL, request_id TEXT NOT NULL, "
                        "payload_digest TEXT NOT NULL, result_type TEXT NOT NULL, "
                        "result_json TEXT NOT NULL, created_at TEXT NOT NULL, "
                        "PRIMARY KEY(repository_id, request_id))"
                    )
                    db.execute(_REPOSITORY_TABLE)
                    db.execute("PRAGMA user_version = 4")
            elif version == 2:
                db.execute("BEGIN IMMEDIATE")
                if db.execute("PRAGMA user_version").fetchone()[0] == 2:
                    db.execute(
                        "CREATE TABLE command_requests ("
                        "repository_id TEXT NOT NULL, request_id TEXT NOT NULL, "
                        "payload_digest TEXT NOT NULL, result_type TEXT NOT NULL, "
                        "result_json TEXT NOT NULL, created_at TEXT NOT NULL, "
                        "PRIMARY KEY(repository_id, request_id))"
                    )
                    db.execute(_REPOSITORY_TABLE)
                    db.execute("PRAGMA user_version = 4")
            elif version == 3:
                db.execute("BEGIN IMMEDIATE")
                if db.execute("PRAGMA user_version").fetchone()[0] == 3:
                    db.execute(_REPOSITORY_TABLE)
                    db.execute("PRAGMA user_version = 4")
            elif version != 4:
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
    def _canonical_git_root(path: str | Path) -> Path:
        try:
            candidate = Path(path).expanduser().resolve(strict=True)
        except OSError as error:
            raise ValueError("Repository path does not exist") from error
        if not candidate.is_dir():
            raise ValueError("Repository path must be a directory")
        try:
            result = subprocess.run(
                ["git", "-C", str(candidate), "rev-parse", "--show-toplevel"],
                capture_output=True,
                check=False,
                encoding="utf-8",
                errors="replace",
                timeout=10,
            )
        except FileNotFoundError as error:
            raise RuntimeError(
                "Git must be installed to register a repository"
            ) from error
        except subprocess.TimeoutExpired as error:
            raise ValueError("Timed out while validating the Git repository") from error
        if result.returncode != 0:
            raise ValueError("Path is not inside a Git working tree")
        try:
            root = Path(result.stdout.strip()).resolve(strict=True)
        except OSError as error:
            raise ValueError(
                "Git returned a repository root that does not exist"
            ) from error
        if not root.is_dir():
            raise ValueError("Git repository root must be a directory")
        return root

    @staticmethod
    def _repository(row: sqlite3.Row | None) -> WorkspaceRepository:
        if row is None:
            raise KeyError("Repository is not registered")
        return WorkspaceRepository(
            row["repository_id"],
            row["display_name"],
            row["canonical_root"],
            row["created_at"],
        )

    def register_repository(
        self,
        path: str | Path,
        *,
        display_name: str | None = None,
        request_id: str | None = None,
    ) -> WorkspaceRepository:
        """Explicitly register a Git working tree and assign its opaque ID."""
        root = self._canonical_git_root(path)
        name = _required(
            root.name or str(root) if display_name is None else display_name,
            "display_name",
        )
        command = {
            "operation": "repository.register",
            "canonical_root": str(root),
            "display_name": name,
        }
        timestamp = _now()
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            replay = self._replay_request(
                db, "__registry__", request_id, command, "repository"
            )
            if replay is not None:
                return WorkspaceRepository.from_dict(replay)
            existing = db.execute(
                "SELECT * FROM repositories WHERE canonical_root=?", (str(root),)
            ).fetchone()
            if existing is not None:
                repository = self._repository(existing)
                if repository.display_name != name:
                    raise WorkspaceConflict(
                        "Repository root is already registered with another "
                        "display name"
                    )
                self._record_request(
                    db,
                    "__registry__",
                    request_id,
                    command,
                    "repository",
                    repository.to_dict(),
                    timestamp,
                )
                return repository

            repository_id = uuid4().hex
            db.execute(
                "INSERT INTO repositories VALUES (?,?,?,?)",
                (repository_id, name, str(root), timestamp),
            )
            self._event(
                db,
                repository_id,
                "repository",
                repository_id,
                EventType.REPOSITORY_REGISTERED,
                {"display_name": name},
                timestamp,
            )
            repository = WorkspaceRepository(repository_id, name, str(root), timestamp)
            self._record_request(
                db,
                "__registry__",
                request_id,
                command,
                "repository",
                repository.to_dict(),
                timestamp,
            )
            return repository

    def get_repository(self, repository_id: str) -> WorkspaceRepository:
        with self._connection() as db:
            return self._repository(
                db.execute(
                    "SELECT * FROM repositories WHERE repository_id=?",
                    (repository_id,),
                ).fetchone()
            )

    def list_repositories(self) -> tuple[WorkspaceRepository, ...]:
        with self._connection() as db:
            return tuple(
                self._repository(row)
                for row in db.execute(
                    "SELECT * FROM repositories ORDER BY display_name,repository_id"
                )
            )

    def resolve_repository_root(self, repository_id: str) -> Path:
        """Resolve an opaque registry ID and reject a moved or replaced root."""
        repository = self.get_repository(repository_id)
        current_root = self._canonical_git_root(repository.canonical_root)
        if str(current_root) != repository.canonical_root:
            raise ValueError("Registered repository root changed; register it again")
        return current_root

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
        event_type = EventType(event_type).value
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

    @staticmethod
    def _replay_request(
        db: sqlite3.Connection,
        repository_id: str,
        request_id: str | None,
        command: dict[str, Any],
        result_type: str,
    ) -> dict[str, Any] | None:
        if request_id is None:
            return None
        _required(request_id, "request_id")
        row = db.execute(
            "SELECT payload_digest,result_type,result_json FROM command_requests "
            "WHERE repository_id=? AND request_id=?",
            (repository_id, request_id),
        ).fetchone()
        if row is None:
            return None
        if row["payload_digest"] != _request_digest(command):
            raise WorkspaceConflict("Request ID was reused with different content")
        if row["result_type"] != result_type:
            raise WorkspaceConflict("Request ID was reused for a different command")
        return json.loads(row["result_json"])

    @staticmethod
    def _record_request(
        db: sqlite3.Connection,
        repository_id: str,
        request_id: str | None,
        command: dict[str, Any],
        result_type: str,
        result: dict[str, Any],
        timestamp: str,
    ) -> None:
        if request_id is None:
            return
        db.execute(
            "INSERT INTO command_requests VALUES (?,?,?,?,?,?)",
            (
                repository_id,
                request_id,
                _request_digest(command),
                result_type,
                json.dumps(result, sort_keys=True, separators=(",", ":")),
                timestamp,
            ),
        )

    def create_conversation(
        self, repository_id: str, title: str, *, request_id: str | None = None
    ) -> Conversation:
        _required(repository_id, "repository_id")
        _required(title, "title")
        command = {"operation": "conversation.create", "title": title}
        identity, timestamp = uuid4().hex, _now()
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            replay = self._replay_request(
                db, repository_id, request_id, command, "conversation"
            )
            if replay is not None:
                return Conversation.from_dict(replay)
            db.execute(
                "INSERT INTO conversations VALUES (?,?,?,?,?,?,?)",
                (identity, repository_id, title, "[]", timestamp, timestamp, 1),
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
            result = self._conversation(
                db.execute(
                    "SELECT * FROM conversations WHERE conversation_id=?", (identity,)
                ).fetchone()
            )
            self._record_request(
                db,
                repository_id,
                request_id,
                command,
                "conversation",
                result.to_dict(),
                timestamp,
            )
            return result

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
            row["revision"],
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
        expected_revision: int,
        specification_refs: Sequence[str] | None = None,
        request_id: str | None = None,
    ) -> Conversation:
        _required(title, "title")
        refs = tuple(
            dict.fromkeys(
                _required(ref, "specification_ref")
                for ref in (specification_refs or ())
            )
        )
        command = {
            "operation": "conversation.update",
            "conversation_id": conversation_id,
            "title": title,
            "expected_revision": expected_revision,
            "specification_refs": (None if specification_refs is None else list(refs)),
        }
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            conversation = self._conversation(
                db.execute(
                    "SELECT * FROM conversations WHERE conversation_id=?",
                    (conversation_id,),
                ).fetchone()
            )
            replay = self._replay_request(
                db, conversation.repository_id, request_id, command, "conversation"
            )
            if replay is not None:
                return Conversation.from_dict(replay)
            if conversation.revision != expected_revision:
                raise WorkspaceConflict("Conversation revision has changed")
            if specification_refs is None:
                refs = conversation.specification_refs
            timestamp = _now()
            db.execute(
                "UPDATE conversations SET title=?,specification_refs=?,updated_at=?,"
                "revision=revision+1 WHERE conversation_id=?",
                (title, json.dumps(refs), timestamp, conversation_id),
            )
            self._event(
                db,
                conversation.repository_id,
                "conversation",
                conversation_id,
                "conversation.updated",
                {
                    "title": title,
                    "specification_refs": list(refs),
                    "revision": conversation.revision + 1,
                },
                timestamp,
            )
            result = self._conversation(
                db.execute(
                    "SELECT * FROM conversations WHERE conversation_id=?",
                    (conversation_id,),
                ).fetchone()
            )
            self._record_request(
                db,
                conversation.repository_id,
                request_id,
                command,
                "conversation",
                result.to_dict(),
                timestamp,
            )
            return result

    def append_message(
        self,
        conversation_id: str,
        *,
        message_id: str,
        role: str,
        content: str,
        request_id: str | None = None,
    ) -> Message:
        """A retried submission with the same ID creates one message and event."""
        _required(message_id, "message_id")
        _required(content, "content")
        try:
            role = MessageRole(role)
        except (TypeError, ValueError) as error:
            raise ValueError("Unsupported message role") from error
        if role not in MessageRole:
            raise ValueError("Unsupported message role")
        command = {
            "operation": "message.append",
            "conversation_id": conversation_id,
            "message_id": message_id,
            "role": role.value,
            "content": content,
        }
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            conversation = self._conversation(
                db.execute(
                    "SELECT * FROM conversations WHERE conversation_id=?",
                    (conversation_id,),
                ).fetchone()
            )
            replay = self._replay_request(
                db, conversation.repository_id, request_id, command, "message"
            )
            if replay is not None:
                return Message.from_dict(replay)
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
                result = Message(
                    row["message_id"],
                    row["conversation_id"],
                    MessageRole(row["role"]),
                    row["content"],
                    row["created_at"],
                    row["sequence"],
                )
                self._record_request(
                    db,
                    conversation.repository_id,
                    request_id,
                    command,
                    "message",
                    result.to_dict(),
                    _now(),
                )
                return result
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
            result = Message(
                message_id, conversation_id, role, content, timestamp, sequence
            )
            self._record_request(
                db,
                conversation.repository_id,
                request_id,
                command,
                "message",
                result.to_dict(),
                timestamp,
            )
            return result

    def list_messages(self, conversation_id: str) -> tuple[Message, ...]:
        with self._connection() as db:
            return tuple(
                Message(
                    row["message_id"],
                    row["conversation_id"],
                    MessageRole(row["role"]),
                    row["content"],
                    row["created_at"],
                    row["sequence"],
                )
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
            JobKind(row["kind"]),
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
        request_id: str | None = None,
    ) -> Job:
        _required(repository_id, "repository_id")
        _required(title, "title")
        try:
            kind = JobKind(kind)
        except (TypeError, ValueError) as error:
            raise ValueError("Unsupported job kind") from error
        command = {
            "operation": "job.create",
            "kind": kind.value,
            "title": title,
            "conversation_id": conversation_id,
            "dependency_ids": list(dependency_ids),
        }
        identity, timestamp = uuid4().hex, _now()
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            replay = self._replay_request(db, repository_id, request_id, command, "job")
            if replay is not None:
                return Job.from_dict(replay)
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
                    kind.value,
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
                    "state": state.value,
                    "kind": JobKind(kind).value,
                    "title": title,
                    "conversation_id": conversation_id,
                    "dependency_ids": list(dependencies),
                    "revision": 1,
                },
                timestamp,
            )
            result = self._job(db, identity)
            self._record_request(
                db,
                repository_id,
                request_id,
                command,
                "job",
                result.to_dict(),
                timestamp,
            )
            return result

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
        request_id: str | None = None,
    ) -> Job:
        state = JobState(state)
        refs = tuple(
            dict.fromkeys(
                _required(ref, "artifact_ref") for ref in (artifact_refs or ())
            )
        )
        command = {
            "operation": "job.transition",
            "job_id": job_id,
            "expected_revision": expected_revision,
            "state": state.value,
            "detail": detail,
            "artifact_refs": None if artifact_refs is None else list(refs),
        }
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            job = self._job(db, job_id)
            replay = self._replay_request(
                db, job.repository_id, request_id, command, "job"
            )
            if replay is not None:
                return Job.from_dict(replay)
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
                    "state": state.value,
                    "detail": detail,
                    "artifact_refs": list(refs),
                    "revision": job.revision + 1,
                },
                timestamp,
            )
            if state == JobState.SUCCEEDED:
                self._release_dependents(db, job_id, timestamp)
            result = self._job(db, job_id)
            self._record_request(
                db,
                job.repository_id,
                request_id,
                command,
                "job",
                result.to_dict(),
                timestamp,
            )
            return result

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
                {"state": JobState.READY.value, "revision": job.revision + 1},
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
                    EventType(row["event_type"]),
                    json.loads(row["payload"]),
                    row["created_at"],
                )
                for row in db.execute(
                    "SELECT * FROM events WHERE repository_id=? AND sequence>? "
                    "ORDER BY sequence LIMIT ?",
                    (repository_id, after, limit),
                )
            )
