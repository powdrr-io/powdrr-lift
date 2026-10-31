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
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from jsonschema import Draft202012Validator, SchemaError, ValidationError
from powdrr_contracts.workspace import (
    Conversation,
    DispatchIntent,
    EventType,
    Job,
    JobKind,
    JobOperation,
    JobState,
    Message,
    MessageRole,
    QuestionState,
    SupervisorLease,
    TurnState,
    WorkerOwnership,
    WorkerState,
    WorkspaceEvent,
    WorkspaceQuestion,
    WorkspaceRepository,
    WorkspaceTurn,
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

_TURN_TRANSITIONS = {
    TurnState.ACCEPTED: {
        TurnState.RUNNING,
        TurnState.FAILED,
        TurnState.CANCELLED,
    },
    TurnState.RUNNING: {
        TurnState.WAITING_FOR_INPUT,
        TurnState.SUCCEEDED,
        TurnState.FAILED,
        TurnState.CANCELLED,
    },
    TurnState.WAITING_FOR_INPUT: {
        TurnState.RUNNING,
        TurnState.FAILED,
        TurnState.CANCELLED,
    },
    TurnState.SUCCEEDED: set(),
    TurnState.FAILED: set(),
    TurnState.CANCELLED: set(),
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
CREATE TABLE turns (
    turn_id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
    user_message_id TEXT NOT NULL UNIQUE REFERENCES messages(message_id),
    run_id TEXT, attempt_id TEXT, state TEXT NOT NULL,
    final_message_id TEXT REFERENCES messages(message_id), error TEXT,
    revision INTEGER NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    CHECK ((run_id IS NULL) = (attempt_id IS NULL))
);
CREATE TABLE questions (
    question_id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
    turn_id TEXT NOT NULL REFERENCES turns(turn_id),
    prompt TEXT NOT NULL, response_schema TEXT NOT NULL,
    run_id TEXT NOT NULL, attempt_id TEXT NOT NULL,
    state TEXT NOT NULL, answer_json TEXT, delivery_receipt_id TEXT UNIQUE,
    revision INTEGER NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX conversation_turns ON turns(conversation_id, created_at);
CREATE INDEX turn_questions ON questions(turn_id, created_at);
CREATE UNIQUE INDEX one_active_turn_per_conversation ON turns(conversation_id)
WHERE state IN ('accepted','running','waiting_for_input');
CREATE UNIQUE INDEX one_unresolved_question_per_turn ON questions(turn_id)
WHERE state IN ('open','answered');
CREATE TABLE job_operations (
    operation_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(job_id),
    run_id TEXT NOT NULL, attempt_id TEXT NOT NULL,
    workflow_directory TEXT NOT NULL, checkpoint_ref TEXT NOT NULL,
    source_spec_revision TEXT, source_refs TEXT NOT NULL,
    job_inputs TEXT NOT NULL, provider_config_refs TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(job_id, attempt_id), UNIQUE(run_id, attempt_id)
);
CREATE INDEX job_operation_history ON job_operations(job_id, created_at);
CREATE TABLE supervisor_lease (
    lease_key INTEGER PRIMARY KEY CHECK (lease_key=1),
    supervisor_id TEXT NOT NULL, generation INTEGER NOT NULL,
    process_id INTEGER NOT NULL, process_started_at TEXT NOT NULL,
    lease_expires_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE worker_ownership (
    worker_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL, run_id TEXT NOT NULL, attempt_id TEXT NOT NULL,
    supervisor_id TEXT NOT NULL, supervisor_generation INTEGER NOT NULL,
    process_id INTEGER NOT NULL, process_started_at TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL, lease_expires_at TEXT NOT NULL,
    cancellation_requested INTEGER NOT NULL CHECK (cancellation_requested IN (0,1)),
    dispatch_intent TEXT NOT NULL,
    state TEXT NOT NULL, revision INTEGER NOT NULL, updated_at TEXT NOT NULL,
    UNIQUE(job_id, attempt_id),
    FOREIGN KEY(job_id, attempt_id) REFERENCES job_operations(job_id, attempt_id)
);
CREATE INDEX worker_lease_expiry ON worker_ownership(lease_expires_at);
PRAGMA user_version = 7;
"""

_REPOSITORY_TABLE = (
    "CREATE TABLE repositories ("
    "repository_id TEXT PRIMARY KEY, display_name TEXT NOT NULL, "
    "canonical_root TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL)"
)

_TURN_TABLES = (
    "CREATE TABLE turns ("
    "turn_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES "
    "conversations(conversation_id), user_message_id TEXT NOT NULL UNIQUE "
    "REFERENCES messages(message_id), run_id TEXT, attempt_id TEXT, "
    "state TEXT NOT NULL, final_message_id TEXT REFERENCES messages(message_id), "
    "error TEXT, revision INTEGER NOT NULL, created_at TEXT NOT NULL, "
    "updated_at TEXT NOT NULL, CHECK ((run_id IS NULL) = (attempt_id IS NULL)))",
    "CREATE TABLE questions ("
    "question_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES "
    "conversations(conversation_id), turn_id TEXT NOT NULL REFERENCES turns(turn_id), "
    "prompt TEXT NOT NULL, response_schema TEXT NOT NULL, run_id TEXT NOT NULL, "
    "attempt_id TEXT NOT NULL, state TEXT NOT NULL, answer_json TEXT, "
    "delivery_receipt_id TEXT UNIQUE, revision INTEGER NOT NULL, "
    "created_at TEXT NOT NULL, updated_at TEXT NOT NULL)",
    "CREATE INDEX conversation_turns ON turns(conversation_id, created_at)",
    "CREATE INDEX turn_questions ON questions(turn_id, created_at)",
    "CREATE UNIQUE INDEX one_active_turn_per_conversation ON turns(conversation_id) "
    "WHERE state IN ('accepted','running','waiting_for_input')",
    "CREATE UNIQUE INDEX one_unresolved_question_per_turn ON questions(turn_id) "
    "WHERE state IN ('open','answered')",
)

_JOB_OPERATION_TABLES = (
    "CREATE TABLE job_operations ("
    "operation_id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(job_id), "
    "run_id TEXT NOT NULL, attempt_id TEXT NOT NULL, "
    "workflow_directory TEXT NOT NULL, checkpoint_ref TEXT NOT NULL, "
    "source_spec_revision TEXT, source_refs TEXT NOT NULL, job_inputs TEXT NOT NULL, "
    "provider_config_refs TEXT NOT NULL, created_at TEXT NOT NULL, "
    "UNIQUE(job_id, attempt_id), UNIQUE(run_id, attempt_id))",
    "CREATE INDEX job_operation_history ON job_operations(job_id, created_at)",
)

_SUPERVISOR_TABLES = (
    "CREATE TABLE supervisor_lease ("
    "lease_key INTEGER PRIMARY KEY CHECK (lease_key=1), "
    "supervisor_id TEXT NOT NULL, generation INTEGER NOT NULL, "
    "process_id INTEGER NOT NULL, process_started_at TEXT NOT NULL, "
    "lease_expires_at TEXT NOT NULL, updated_at TEXT NOT NULL)",
    "CREATE TABLE worker_ownership ("
    "worker_id TEXT PRIMARY KEY, job_id TEXT NOT NULL, run_id TEXT NOT NULL, "
    "attempt_id TEXT NOT NULL, supervisor_id TEXT NOT NULL, "
    "supervisor_generation INTEGER NOT NULL, process_id INTEGER NOT NULL, "
    "process_started_at TEXT NOT NULL, heartbeat_at TEXT NOT NULL, "
    "lease_expires_at TEXT NOT NULL, cancellation_requested INTEGER NOT NULL "
    "CHECK (cancellation_requested IN (0,1)), "
    "dispatch_intent TEXT NOT NULL, state TEXT NOT NULL, revision INTEGER NOT NULL, "
    "updated_at TEXT NOT NULL, UNIQUE(job_id, attempt_id), "
    "FOREIGN KEY(job_id, attempt_id) REFERENCES "
    "job_operations(job_id, attempt_id))",
    "CREATE INDEX worker_lease_expiry ON worker_ownership(lease_expires_at)",
)


def _required(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _utc_datetime(value: datetime | None = None) -> datetime:
    result = datetime.now(UTC) if value is None else value
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("Lease timestamps must include a timezone")
    return result.astimezone(UTC)


def _lease_expiry(now: datetime, lease_seconds: int) -> str:
    if (
        not isinstance(lease_seconds, int)
        or isinstance(lease_seconds, bool)
        or not 1 <= lease_seconds <= 3600
    ):
        raise ValueError("lease_seconds must be between 1 and 3600")
    return (now + timedelta(seconds=lease_seconds)).isoformat()


def _timestamp(value: datetime) -> str:
    return value.isoformat()


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Stored lease timestamps must include a timezone")
    return parsed.astimezone(UTC)


def _request_digest(command: dict[str, Any]) -> str:
    canonical = json.dumps(
        command, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _validate_json_value(value: Any) -> None:
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError) as error:
        raise ValueError("Value must contain finite JSON data") from error


_SECRET_INPUT_KEYS = {
    "apikey",
    "apitoken",
    "accesstoken",
    "refreshtoken",
    "token",
    "secret",
    "password",
    "credential",
    "authorization",
    "privatekey",
}


def _reject_secret_input_fields(value: Any, path: str = "job_inputs") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{path} keys must be strings")
            normalized = "".join(
                character for character in key.lower() if character.isalnum()
            )
            if (
                normalized in _SECRET_INPUT_KEYS
                or any(
                    marker in normalized
                    for marker in (
                        "apikey",
                        "secret",
                        "password",
                        "credential",
                        "authorization",
                        "privatekey",
                    )
                )
                or normalized.endswith("token")
            ):
                raise ValueError(f"{path} must not contain secret field {key!r}")
            _reject_secret_input_fields(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_secret_input_fields(child, f"{path}[{index}]")


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
            elif version not in {4, 5, 6, 7}:
                raise ValueError(f"Unsupported workspace schema version: {version}")
            current_version = db.execute("PRAGMA user_version").fetchone()[0]
            if current_version == 4:
                if not db.in_transaction:
                    db.execute("BEGIN IMMEDIATE")
                if db.execute("PRAGMA user_version").fetchone()[0] == 4:
                    for statement in _TURN_TABLES:
                        db.execute(statement)
                    db.execute("PRAGMA user_version = 5")
            elif current_version not in {5, 6, 7}:
                raise ValueError(
                    f"Unsupported workspace schema version: {current_version}"
                )
            current_version = db.execute("PRAGMA user_version").fetchone()[0]
            if current_version == 5:
                if not db.in_transaction:
                    db.execute("BEGIN IMMEDIATE")
                if db.execute("PRAGMA user_version").fetchone()[0] == 5:
                    for statement in _JOB_OPERATION_TABLES:
                        db.execute(statement)
                    db.execute("PRAGMA user_version = 6")
            elif current_version not in {6, 7}:
                raise ValueError(
                    f"Unsupported workspace schema version: {current_version}"
                )
            current_version = db.execute("PRAGMA user_version").fetchone()[0]
            if current_version == 6:
                if not db.in_transaction:
                    db.execute("BEGIN IMMEDIATE")
                if db.execute("PRAGMA user_version").fetchone()[0] == 6:
                    for statement in _SUPERVISOR_TABLES:
                        db.execute(statement)
                    db.execute("PRAGMA user_version = 7")
            elif current_version != 7:
                raise ValueError(
                    f"Unsupported workspace schema version: {current_version}"
                )

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
    def _turn(db: sqlite3.Connection, turn_id: str) -> WorkspaceTurn:
        row = db.execute("SELECT * FROM turns WHERE turn_id=?", (turn_id,)).fetchone()
        if row is None:
            raise KeyError("Turn does not exist")
        return WorkspaceTurn(
            row["turn_id"],
            row["conversation_id"],
            row["user_message_id"],
            row["run_id"],
            row["attempt_id"],
            TurnState(row["state"]),
            row["final_message_id"],
            row["error"],
            row["revision"],
            row["created_at"],
            row["updated_at"],
        )

    @staticmethod
    def _question(db: sqlite3.Connection, question_id: str) -> WorkspaceQuestion:
        row = db.execute(
            "SELECT * FROM questions WHERE question_id=?", (question_id,)
        ).fetchone()
        if row is None:
            raise KeyError("Question does not exist")
        return WorkspaceQuestion(
            row["question_id"],
            row["conversation_id"],
            row["turn_id"],
            row["prompt"],
            json.loads(row["response_schema"]),
            row["run_id"],
            row["attempt_id"],
            QuestionState(row["state"]),
            None if row["answer_json"] is None else json.loads(row["answer_json"]),
            row["delivery_receipt_id"],
            row["revision"],
            row["created_at"],
            row["updated_at"],
        )

    def create_turn(
        self,
        conversation_id: str,
        *,
        turn_id: str,
        message_id: str,
        content: str,
        request_id: str | None = None,
    ) -> WorkspaceTurn:
        """Atomically persist a user message and its single active turn."""
        _required(turn_id, "turn_id")
        _required(message_id, "message_id")
        _required(content, "content")
        command = {
            "operation": "turn.create",
            "conversation_id": conversation_id,
            "turn_id": turn_id,
            "message_id": message_id,
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
                db, conversation.repository_id, request_id, command, "turn"
            )
            if replay is not None:
                return WorkspaceTurn.from_dict(replay)
            active = db.execute(
                "SELECT 1 FROM turns WHERE conversation_id=? "
                "AND state IN ('accepted','running','waiting_for_input')",
                (conversation_id,),
            ).fetchone()
            if active is not None:
                raise WorkspaceConflict("Conversation already has an active turn")
            if (
                db.execute("SELECT 1 FROM turns WHERE turn_id=?", (turn_id,)).fetchone()
                is not None
            ):
                raise WorkspaceConflict("Turn ID is already in use")
            if (
                db.execute(
                    "SELECT 1 FROM messages WHERE message_id=?", (message_id,)
                ).fetchone()
                is not None
            ):
                raise WorkspaceConflict("Message ID is already in use")

            timestamp = _now()
            message_sequence = self._event(
                db,
                conversation.repository_id,
                "conversation",
                conversation_id,
                EventType.MESSAGE_CREATED,
                {"message_id": message_id, "role": MessageRole.USER.value},
                timestamp,
            )
            db.execute(
                "INSERT INTO messages VALUES (?,?,?,?,?,?)",
                (
                    message_id,
                    conversation_id,
                    MessageRole.USER.value,
                    content,
                    timestamp,
                    message_sequence,
                ),
            )
            db.execute(
                "UPDATE conversations SET updated_at=? WHERE conversation_id=?",
                (timestamp, conversation_id),
            )
            db.execute(
                "INSERT INTO turns VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    turn_id,
                    conversation_id,
                    message_id,
                    None,
                    None,
                    TurnState.ACCEPTED.value,
                    None,
                    None,
                    1,
                    timestamp,
                    timestamp,
                ),
            )
            self._event(
                db,
                conversation.repository_id,
                "turn",
                turn_id,
                EventType.TURN_CREATED,
                {
                    "conversation_id": conversation_id,
                    "user_message_id": message_id,
                    "state": TurnState.ACCEPTED.value,
                    "revision": 1,
                },
                timestamp,
            )
            result = self._turn(db, turn_id)
            self._record_request(
                db,
                conversation.repository_id,
                request_id,
                command,
                "turn",
                result.to_dict(),
                timestamp,
            )
            return result

    def get_turn(self, turn_id: str) -> WorkspaceTurn:
        with self._connection() as db:
            return self._turn(db, turn_id)

    def list_turns(
        self, conversation_id: str, *, limit: int = 50
    ) -> tuple[WorkspaceTurn, ...]:
        if not 1 <= limit <= 100:
            raise ValueError("Invalid turn page size")
        with self._connection() as db:
            rows = db.execute(
                "SELECT turn_id FROM turns WHERE conversation_id=? "
                "ORDER BY created_at DESC,turn_id LIMIT ?",
                (conversation_id, limit),
            ).fetchall()
            return tuple(self._turn(db, row["turn_id"]) for row in rows)

    def transition_turn(
        self,
        turn_id: str,
        *,
        expected_revision: int,
        state: TurnState,
        run_id: str | None = None,
        attempt_id: str | None = None,
        final_message_id: str | None = None,
        error: str | None = None,
        request_id: str | None = None,
    ) -> WorkspaceTurn:
        state = TurnState(state)
        if (run_id is None) != (attempt_id is None):
            raise ValueError("run_id and attempt_id must be provided together")
        if run_id is not None:
            _required(run_id, "run_id")
            _required(attempt_id or "", "attempt_id")
        if error is not None:
            _required(error, "error")
        command = {
            "operation": "turn.transition",
            "turn_id": turn_id,
            "expected_revision": expected_revision,
            "state": state.value,
            "run_id": run_id,
            "attempt_id": attempt_id,
            "final_message_id": final_message_id,
            "error": error,
        }
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            turn = self._turn(db, turn_id)
            conversation = self._conversation(
                db.execute(
                    "SELECT * FROM conversations WHERE conversation_id=?",
                    (turn.conversation_id,),
                ).fetchone()
            )
            replay = self._replay_request(
                db, conversation.repository_id, request_id, command, "turn"
            )
            if replay is not None:
                return WorkspaceTurn.from_dict(replay)
            if turn.revision != expected_revision:
                raise WorkspaceConflict("Turn revision has changed")
            if state != turn.state and state not in _TURN_TRANSITIONS[turn.state]:
                raise ValueError(f"Invalid turn transition: {turn.state} -> {state}")
            next_run_id = turn.run_id if run_id is None else run_id
            next_attempt_id = turn.attempt_id if attempt_id is None else attempt_id
            if turn.run_id is not None and (next_run_id, next_attempt_id) != (
                turn.run_id,
                turn.attempt_id,
            ):
                raise WorkspaceConflict("A turn cannot change its run or attempt")
            if state in {TurnState.RUNNING, TurnState.WAITING_FOR_INPUT} and (
                next_run_id is None or next_attempt_id is None
            ):
                raise ValueError("An active turn must be bound to a run and attempt")
            if state == TurnState.WAITING_FOR_INPUT:
                if (
                    db.execute(
                        "SELECT 1 FROM questions WHERE turn_id=? AND state='open'",
                        (turn_id,),
                    ).fetchone()
                    is None
                ):
                    raise ValueError(
                        "A turn can wait for input only with an open question"
                    )
            if state == TurnState.RUNNING and turn.state == TurnState.WAITING_FOR_INPUT:
                if (
                    db.execute(
                        "SELECT 1 FROM questions WHERE turn_id=? "
                        "AND state IN ('open','answered')",
                        (turn_id,),
                    ).fetchone()
                    is not None
                ):
                    raise ValueError("The pending question must be resolved first")
            if final_message_id is not None and state != TurnState.SUCCEEDED:
                raise ValueError("Only a successful turn can have a final message")
            if error is not None and state != TurnState.FAILED:
                raise ValueError("Only a failed turn can have an error description")
            if state == TurnState.SUCCEEDED:
                if final_message_id is None:
                    raise ValueError(
                        "A successful turn requires its final assistant message"
                    )
                final_message = db.execute(
                    "SELECT role FROM messages WHERE message_id=? "
                    "AND conversation_id=?",
                    (final_message_id, turn.conversation_id),
                ).fetchone()
                if (
                    final_message is None
                    or final_message["role"] != MessageRole.ASSISTANT
                ):
                    raise ValueError(
                        "Final message must be an assistant message in this chat"
                    )
                if (
                    db.execute(
                        "SELECT 1 FROM questions WHERE turn_id=? "
                        "AND state IN ('open','answered')",
                        (turn_id,),
                    ).fetchone()
                    is not None
                ):
                    raise ValueError("A turn cannot finish with an unresolved question")
            if state == TurnState.FAILED and error is None:
                raise ValueError("A failed turn requires an error description")
            timestamp = _now()
            db.execute(
                "UPDATE turns SET run_id=?,attempt_id=?,state=?,final_message_id=?,"
                "error=?,revision=revision+1,updated_at=? WHERE turn_id=?",
                (
                    next_run_id,
                    next_attempt_id,
                    state.value,
                    final_message_id,
                    error,
                    timestamp,
                    turn_id,
                ),
            )
            if state in {TurnState.FAILED, TurnState.CANCELLED}:
                pending = db.execute(
                    "SELECT question_id,revision FROM questions WHERE turn_id=? "
                    "AND state IN ('open','answered')",
                    (turn_id,),
                ).fetchall()
                for question_row in pending:
                    db.execute(
                        "UPDATE questions SET state=?,revision=revision+1,"
                        "updated_at=? WHERE question_id=?",
                        (
                            QuestionState.CANCELLED.value,
                            timestamp,
                            question_row["question_id"],
                        ),
                    )
                    self._event(
                        db,
                        conversation.repository_id,
                        "question",
                        question_row["question_id"],
                        EventType.QUESTION_UPDATED,
                        {
                            "state": QuestionState.CANCELLED.value,
                            "revision": question_row["revision"] + 1,
                        },
                        timestamp,
                    )
            self._event(
                db,
                conversation.repository_id,
                "turn",
                turn_id,
                EventType.TURN_UPDATED,
                {
                    "state": state.value,
                    "run_id": next_run_id,
                    "attempt_id": next_attempt_id,
                    "final_message_id": final_message_id,
                    "error": error,
                    "revision": turn.revision + 1,
                },
                timestamp,
            )
            result = self._turn(db, turn_id)
            self._record_request(
                db,
                conversation.repository_id,
                request_id,
                command,
                "turn",
                result.to_dict(),
                timestamp,
            )
            return result

    def create_question(
        self,
        turn_id: str,
        *,
        question_id: str,
        prompt: str,
        response_schema: dict[str, Any],
        run_id: str,
        attempt_id: str,
        expected_turn_revision: int,
        request_id: str | None = None,
    ) -> WorkspaceQuestion:
        _required(question_id, "question_id")
        _required(prompt, "prompt")
        _required(run_id, "run_id")
        _required(attempt_id, "attempt_id")
        if not isinstance(response_schema, dict):
            raise ValueError("response_schema must be an object")
        try:
            Draft202012Validator.check_schema(response_schema)
        except (SchemaError, ValidationError) as error:
            raise ValueError(f"Invalid response schema: {error.message}") from error
        command = {
            "operation": "question.create",
            "turn_id": turn_id,
            "question_id": question_id,
            "prompt": prompt,
            "response_schema": response_schema,
            "run_id": run_id,
            "attempt_id": attempt_id,
            "expected_turn_revision": expected_turn_revision,
        }
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            turn = self._turn(db, turn_id)
            conversation = self._conversation(
                db.execute(
                    "SELECT * FROM conversations WHERE conversation_id=?",
                    (turn.conversation_id,),
                ).fetchone()
            )
            replay = self._replay_request(
                db, conversation.repository_id, request_id, command, "question"
            )
            if replay is not None:
                return WorkspaceQuestion.from_dict(replay)
            if turn.revision != expected_turn_revision:
                raise WorkspaceConflict("Turn revision has changed")
            if turn.state != TurnState.RUNNING:
                raise ValueError("A question can only be attached to a running turn")
            if (turn.run_id, turn.attempt_id) != (run_id, attempt_id):
                raise WorkspaceConflict(
                    "Question run and attempt do not match the turn"
                )
            if (
                db.execute(
                    "SELECT 1 FROM questions WHERE turn_id=? "
                    "AND state IN ('open','answered')",
                    (turn_id,),
                ).fetchone()
                is not None
            ):
                raise WorkspaceConflict("Turn already has an unanswered question")
            if (
                db.execute(
                    "SELECT 1 FROM questions WHERE question_id=?", (question_id,)
                ).fetchone()
                is not None
            ):
                raise WorkspaceConflict("Question ID is already in use")
            timestamp = _now()
            db.execute(
                "INSERT INTO questions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    question_id,
                    turn.conversation_id,
                    turn_id,
                    prompt,
                    json.dumps(response_schema, allow_nan=False),
                    run_id,
                    attempt_id,
                    QuestionState.OPEN.value,
                    None,
                    None,
                    1,
                    timestamp,
                    timestamp,
                ),
            )
            db.execute(
                "UPDATE turns SET state=?,revision=revision+1,updated_at=? "
                "WHERE turn_id=?",
                (TurnState.WAITING_FOR_INPUT.value, timestamp, turn_id),
            )
            self._event(
                db,
                conversation.repository_id,
                "question",
                question_id,
                EventType.QUESTION_CREATED,
                {
                    "turn_id": turn_id,
                    "prompt": prompt,
                    "run_id": run_id,
                    "attempt_id": attempt_id,
                    "state": QuestionState.OPEN.value,
                    "revision": 1,
                },
                timestamp,
            )
            self._event(
                db,
                conversation.repository_id,
                "turn",
                turn_id,
                EventType.TURN_UPDATED,
                {
                    "state": TurnState.WAITING_FOR_INPUT.value,
                    "revision": turn.revision + 1,
                    "question_id": question_id,
                },
                timestamp,
            )
            result = self._question(db, question_id)
            self._record_request(
                db,
                conversation.repository_id,
                request_id,
                command,
                "question",
                result.to_dict(),
                timestamp,
            )
            return result

    def get_question(self, question_id: str) -> WorkspaceQuestion:
        with self._connection() as db:
            return self._question(db, question_id)

    def list_undelivered_question_answers(
        self, repository_id: str, *, limit: int = 100
    ) -> tuple[WorkspaceQuestion, ...]:
        if not 1 <= limit <= 1000:
            raise ValueError("Invalid question page size")
        with self._connection() as db:
            rows = db.execute(
                "SELECT q.question_id FROM questions q "
                "JOIN conversations c ON c.conversation_id=q.conversation_id "
                "WHERE c.repository_id=? AND q.state='answered' "
                "ORDER BY q.updated_at,q.question_id LIMIT ?",
                (repository_id, limit),
            ).fetchall()
            return tuple(self._question(db, row["question_id"]) for row in rows)

    def answer_question(
        self,
        question_id: str,
        *,
        answer: Any,
        expected_revision: int,
        request_id: str | None = None,
    ) -> WorkspaceQuestion:
        _validate_json_value(answer)
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            question = self._question(db, question_id)
            conversation = self._conversation(
                db.execute(
                    "SELECT * FROM conversations WHERE conversation_id=?",
                    (question.conversation_id,),
                ).fetchone()
            )
            command = {
                "operation": "question.answer",
                "question_id": question_id,
                "answer": answer,
                "expected_revision": expected_revision,
            }
            replay = self._replay_request(
                db, conversation.repository_id, request_id, command, "question"
            )
            if replay is not None:
                return WorkspaceQuestion.from_dict(replay)
            if question.revision != expected_revision:
                raise WorkspaceConflict("Question revision has changed")
            if question.state != QuestionState.OPEN:
                raise WorkspaceConflict("Question has already been answered or closed")
            try:
                Draft202012Validator(question.response_schema).validate(answer)
            except ValidationError as error:
                raise ValueError(
                    f"Answer does not match the question: {error.message}"
                ) from error
            timestamp = _now()
            db.execute(
                "UPDATE questions SET state=?,answer_json=?,revision=revision+1,"
                "updated_at=? WHERE question_id=?",
                (
                    QuestionState.ANSWERED.value,
                    json.dumps(answer, allow_nan=False),
                    timestamp,
                    question_id,
                ),
            )
            self._event(
                db,
                conversation.repository_id,
                "question",
                question_id,
                EventType.QUESTION_UPDATED,
                {
                    "state": QuestionState.ANSWERED.value,
                    "answer_available": True,
                    "revision": question.revision + 1,
                },
                timestamp,
            )
            result = self._question(db, question_id)
            self._record_request(
                db,
                conversation.repository_id,
                request_id,
                command,
                "question",
                result.to_dict(),
                timestamp,
            )
            return result

    def mark_question_answer_delivered(
        self,
        question_id: str,
        *,
        expected_revision: int,
        run_id: str,
        attempt_id: str,
        delivery_receipt_id: str,
        request_id: str | None = None,
    ) -> WorkspaceQuestion:
        _required(run_id, "run_id")
        _required(attempt_id, "attempt_id")
        _required(delivery_receipt_id, "delivery_receipt_id")
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            question = self._question(db, question_id)
            conversation = self._conversation(
                db.execute(
                    "SELECT * FROM conversations WHERE conversation_id=?",
                    (question.conversation_id,),
                ).fetchone()
            )
            command = {
                "operation": "question.deliver",
                "question_id": question_id,
                "expected_revision": expected_revision,
                "run_id": run_id,
                "attempt_id": attempt_id,
                "delivery_receipt_id": delivery_receipt_id,
            }
            replay = self._replay_request(
                db, conversation.repository_id, request_id, command, "question"
            )
            if replay is not None:
                return WorkspaceQuestion.from_dict(replay)
            if (question.run_id, question.attempt_id) != (run_id, attempt_id):
                raise WorkspaceConflict("Answer belongs to a different run or attempt")
            if question.state == QuestionState.DELIVERED:
                if question.delivery_receipt_id != delivery_receipt_id:
                    raise WorkspaceConflict("Answer was delivered with another receipt")
                self._record_request(
                    db,
                    conversation.repository_id,
                    request_id,
                    command,
                    "question",
                    question.to_dict(),
                    _now(),
                )
                return question
            if question.revision != expected_revision:
                raise WorkspaceConflict("Question revision has changed")
            if question.state != QuestionState.ANSWERED:
                raise WorkspaceConflict("Question has no answer to deliver")
            turn = self._turn(db, question.turn_id)
            if turn.state != TurnState.WAITING_FOR_INPUT or (
                turn.run_id,
                turn.attempt_id,
            ) != (run_id, attempt_id):
                raise WorkspaceConflict(
                    "Turn is no longer waiting on this continuation"
                )
            receipt_owner = db.execute(
                "SELECT question_id FROM questions WHERE delivery_receipt_id=?",
                (delivery_receipt_id,),
            ).fetchone()
            if (
                receipt_owner is not None
                and receipt_owner["question_id"] != question_id
            ):
                raise WorkspaceConflict(
                    "Delivery receipt is already used by another question"
                )
            timestamp = _now()
            db.execute(
                "UPDATE questions SET state=?,delivery_receipt_id=?,"
                "revision=revision+1,updated_at=? WHERE question_id=?",
                (
                    QuestionState.DELIVERED.value,
                    delivery_receipt_id,
                    timestamp,
                    question_id,
                ),
            )
            db.execute(
                "UPDATE turns SET state=?,revision=revision+1,updated_at=? "
                "WHERE turn_id=?",
                (TurnState.RUNNING.value, timestamp, turn.turn_id),
            )
            self._event(
                db,
                conversation.repository_id,
                "question",
                question_id,
                EventType.QUESTION_UPDATED,
                {
                    "state": QuestionState.DELIVERED.value,
                    "delivery_receipt_id": delivery_receipt_id,
                    "revision": question.revision + 1,
                },
                timestamp,
            )
            self._event(
                db,
                conversation.repository_id,
                "turn",
                turn.turn_id,
                EventType.TURN_UPDATED,
                {
                    "state": TurnState.RUNNING.value,
                    "revision": turn.revision + 1,
                },
                timestamp,
            )
            result = self._question(db, question_id)
            self._record_request(
                db,
                conversation.repository_id,
                request_id,
                command,
                "question",
                result.to_dict(),
                timestamp,
            )
            return result

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

    @staticmethod
    def _operation(db: sqlite3.Connection, operation_id: str) -> JobOperation:
        row = db.execute(
            "SELECT * FROM job_operations WHERE operation_id=?", (operation_id,)
        ).fetchone()
        if row is None:
            raise KeyError("Job operation does not exist")
        return JobOperation(
            row["operation_id"],
            row["job_id"],
            row["run_id"],
            row["attempt_id"],
            row["workflow_directory"],
            row["checkpoint_ref"],
            row["source_spec_revision"],
            tuple(json.loads(row["source_refs"])),
            json.loads(row["job_inputs"]),
            tuple(json.loads(row["provider_config_refs"])),
            row["created_at"],
        )

    def start_job_operation(
        self,
        job_id: str,
        *,
        expected_revision: int,
        run_id: str,
        attempt_id: str,
        workflow_directory: str | Path,
        checkpoint_ref: str,
        job_inputs: dict[str, Any],
        source_refs: Sequence[str],
        provider_config_refs: Sequence[str] = (),
        source_spec_revision: str | None = None,
        request_id: str | None = None,
    ) -> JobOperation:
        """Durably bind pinned inputs and attempt identity before dispatch."""
        for value, name in (
            (run_id, "run_id"),
            (attempt_id, "attempt_id"),
            (checkpoint_ref, "checkpoint_ref"),
        ):
            _required(value, name)
        if not isinstance(job_inputs, dict):
            raise ValueError("job_inputs must be an object")
        _validate_json_value(job_inputs)
        _reject_secret_input_fields(job_inputs)
        refs = tuple(dict.fromkeys(_required(ref, "source_ref") for ref in source_refs))
        if not refs:
            raise ValueError("At least one immutable source reference is required")
        provider_refs = tuple(
            dict.fromkeys(
                _required(ref, "provider_config_ref") for ref in provider_config_refs
            )
        )
        if not provider_refs:
            raise ValueError(
                "At least one provider configuration reference is required"
            )
        try:
            workdir = str(Path(workflow_directory).expanduser().resolve())
        except (OSError, RuntimeError) as error:
            raise ValueError("workflow_directory cannot be resolved") from error
        _required(workdir, "workflow_directory")
        if source_spec_revision is not None:
            _required(source_spec_revision, "source_spec_revision")

        command = {
            "operation": "job.start_operation",
            "job_id": job_id,
            "expected_revision": expected_revision,
            "run_id": run_id,
            "attempt_id": attempt_id,
            "workflow_directory": workdir,
            "checkpoint_ref": checkpoint_ref,
            "job_inputs": job_inputs,
            "source_refs": list(refs),
            "provider_config_refs": list(provider_refs),
            "source_spec_revision": source_spec_revision,
        }
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            job = self._job(db, job_id)
            replay = self._replay_request(
                db, job.repository_id, request_id, command, "job_operation"
            )
            if replay is not None:
                return JobOperation.from_dict(replay)
            if job.revision != expected_revision:
                raise WorkspaceConflict("Job revision has changed")
            if job.state != JobState.READY:
                raise WorkspaceConflict("Only a ready job can start a new operation")
            if any(
                self._job(db, dependency).state != JobState.SUCCEEDED
                for dependency in job.dependency_ids
            ):
                raise ValueError("Job dependencies are incomplete")
            if job.kind == JobKind.PLANNING:
                pinned_spec_ref = job_inputs.get("pinned_spec_ref")
                if not isinstance(pinned_spec_ref, str) or not pinned_spec_ref.strip():
                    raise ValueError("Planning inputs require pinned_spec_ref")
                if pinned_spec_ref not in refs or source_spec_revision is None:
                    raise ValueError(
                        "Planning operations require the pinned spec and its revision"
                    )
            elif job.kind == JobKind.PR_EXECUTION:
                approved_ref = job_inputs.get("approved_change_ref")
                if not isinstance(approved_ref, str) or not approved_ref.strip():
                    raise ValueError("PR execution inputs require approved_change_ref")
                if approved_ref not in refs:
                    raise ValueError("Approved change must be included in source_refs")
            if (
                db.execute(
                    "SELECT 1 FROM job_operations WHERE job_id=? AND attempt_id=?",
                    (job_id, attempt_id),
                ).fetchone()
                is not None
                or db.execute(
                    "SELECT 1 FROM job_operations WHERE run_id=? AND attempt_id=?",
                    (run_id, attempt_id),
                ).fetchone()
                is not None
            ):
                raise WorkspaceConflict("Run attempt already has an operation binding")

            timestamp = _now()
            operation_id = uuid4().hex
            db.execute(
                "INSERT INTO job_operations VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    operation_id,
                    job_id,
                    run_id,
                    attempt_id,
                    workdir,
                    checkpoint_ref,
                    source_spec_revision,
                    json.dumps(refs),
                    json.dumps(job_inputs, sort_keys=True, allow_nan=False),
                    json.dumps(provider_refs),
                    timestamp,
                ),
            )
            next_revision = job.revision + 1
            db.execute(
                "UPDATE jobs SET state=?,detail=?,revision=?,updated_at=? "
                "WHERE job_id=?",
                (
                    JobState.RUNNING.value,
                    f"Run {run_id}, attempt {attempt_id}",
                    next_revision,
                    timestamp,
                    job_id,
                ),
            )
            self._event(
                db,
                job.repository_id,
                "job_operation",
                operation_id,
                EventType.JOB_OPERATION_BOUND,
                {
                    "job_id": job_id,
                    "run_id": run_id,
                    "attempt_id": attempt_id,
                    "revision": next_revision,
                },
                timestamp,
            )
            self._event(
                db,
                job.repository_id,
                "job",
                job_id,
                EventType.JOB_UPDATED,
                {
                    "state": JobState.RUNNING.value,
                    "detail": f"Run {run_id}, attempt {attempt_id}",
                    "revision": next_revision,
                    "operation_id": operation_id,
                },
                timestamp,
            )
            result = self._operation(db, operation_id)
            self._record_request(
                db,
                job.repository_id,
                request_id,
                command,
                "job_operation",
                result.to_dict(),
                timestamp,
            )
            return result

    def get_job_operation(self, job_id: str, attempt_id: str) -> JobOperation:
        with self._connection() as db:
            row = db.execute(
                "SELECT operation_id FROM job_operations "
                "WHERE job_id=? AND attempt_id=?",
                (job_id, attempt_id),
            ).fetchone()
            if row is None:
                raise KeyError("Job operation does not exist")
            return self._operation(db, row["operation_id"])

    def list_job_operations(self, job_id: str) -> tuple[JobOperation, ...]:
        with self._connection() as db:
            rows = db.execute(
                "SELECT operation_id FROM job_operations WHERE job_id=? "
                "ORDER BY created_at,attempt_id",
                (job_id,),
            ).fetchall()
            return tuple(self._operation(db, row["operation_id"]) for row in rows)

    @staticmethod
    def _supervisor_lease(db: sqlite3.Connection) -> SupervisorLease:
        row = db.execute("SELECT * FROM supervisor_lease WHERE lease_key=1").fetchone()
        if row is None:
            raise KeyError("No supervisor lease has been acquired")
        return SupervisorLease(
            row["supervisor_id"],
            row["generation"],
            row["process_id"],
            row["process_started_at"],
            row["lease_expires_at"],
            row["updated_at"],
        )

    @staticmethod
    def _worker_ownership(row: sqlite3.Row | None) -> WorkerOwnership:
        if row is None:
            raise KeyError("Worker ownership does not exist")
        return WorkerOwnership(
            row["worker_id"],
            row["job_id"],
            row["run_id"],
            row["attempt_id"],
            row["supervisor_id"],
            row["supervisor_generation"],
            row["process_id"],
            row["process_started_at"],
            row["heartbeat_at"],
            row["lease_expires_at"],
            bool(row["cancellation_requested"]),
            DispatchIntent(row["dispatch_intent"]),
            WorkerState(row["state"]),
            row["revision"],
            row["updated_at"],
        )

    def acquire_supervisor_lease(
        self,
        supervisor_id: str,
        *,
        process_id: int,
        process_started_at: str,
        lease_seconds: int = 30,
        now: datetime | None = None,
    ) -> SupervisorLease:
        _required(supervisor_id, "supervisor_id")
        _required(process_started_at, "process_started_at")
        if (
            not isinstance(process_id, int)
            or isinstance(process_id, bool)
            or process_id < 1
        ):
            raise ValueError("process_id must be a positive integer")
        instant = _utc_datetime(now)
        expires_at = _lease_expiry(instant, lease_seconds)
        timestamp = _timestamp(instant)
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM supervisor_lease WHERE lease_key=1"
            ).fetchone()
            if row is None:
                generation = 1
                db.execute(
                    "INSERT INTO supervisor_lease VALUES (1,?,?,?,?,?,?)",
                    (
                        supervisor_id,
                        generation,
                        process_id,
                        process_started_at,
                        expires_at,
                        timestamp,
                    ),
                )
            else:
                current_expiry = _parse_timestamp(row["lease_expires_at"])
                active = current_expiry > instant
                same_process = (
                    row["supervisor_id"] == supervisor_id
                    and row["process_id"] == process_id
                    and row["process_started_at"] == process_started_at
                )
                if active and not same_process:
                    raise WorkspaceConflict(
                        "Another supervisor holds the workspace lease"
                    )
                generation = row["generation"] if active else row["generation"] + 1
                db.execute(
                    "UPDATE supervisor_lease SET supervisor_id=?,generation=?,"
                    "process_id=?,process_started_at=?,lease_expires_at=?,updated_at=? "
                    "WHERE lease_key=1",
                    (
                        supervisor_id,
                        generation,
                        process_id,
                        process_started_at,
                        expires_at,
                        timestamp,
                    ),
                )
            return self._supervisor_lease(db)

    def get_supervisor_lease(self) -> SupervisorLease:
        with self._connection() as db:
            return self._supervisor_lease(db)

    def renew_supervisor_lease(
        self,
        supervisor_id: str,
        generation: int,
        *,
        lease_seconds: int = 30,
        now: datetime | None = None,
    ) -> SupervisorLease:
        instant = _utc_datetime(now)
        expires_at = _lease_expiry(instant, lease_seconds)
        timestamp = _timestamp(instant)
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            lease = self._supervisor_lease(db)
            if (
                lease.supervisor_id != supervisor_id
                or lease.generation != generation
                or _parse_timestamp(lease.lease_expires_at) <= instant
            ):
                raise WorkspaceConflict("Supervisor lease is stale or expired")
            db.execute(
                "UPDATE supervisor_lease SET lease_expires_at=?,updated_at=? "
                "WHERE lease_key=1",
                (expires_at, timestamp),
            )
            return self._supervisor_lease(db)

    def release_supervisor_lease(
        self,
        supervisor_id: str,
        generation: int,
        *,
        now: datetime | None = None,
    ) -> SupervisorLease:
        instant = _utc_datetime(now)
        timestamp = _timestamp(instant)
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            lease = self._supervisor_lease(db)
            if (lease.supervisor_id, lease.generation) != (
                supervisor_id,
                generation,
            ):
                raise WorkspaceConflict(
                    "Supervisor lease is owned by another generation"
                )
            db.execute(
                "UPDATE supervisor_lease SET lease_expires_at=?,updated_at=? "
                "WHERE lease_key=1",
                (timestamp, timestamp),
            )
            return self._supervisor_lease(db)

    def claim_job_worker(
        self,
        job_id: str,
        attempt_id: str,
        *,
        worker_id: str,
        supervisor_id: str,
        supervisor_generation: int,
        process_id: int,
        process_started_at: str,
        dispatch_intent: DispatchIntent,
        lease_seconds: int = 30,
        now: datetime | None = None,
    ) -> WorkerOwnership:
        for value, name in (
            (worker_id, "worker_id"),
            (supervisor_id, "supervisor_id"),
            (attempt_id, "attempt_id"),
            (process_started_at, "process_started_at"),
        ):
            _required(value, name)
        if (
            not isinstance(process_id, int)
            or isinstance(process_id, bool)
            or process_id < 1
        ):
            raise ValueError("process_id must be a positive integer")
        intent = DispatchIntent(dispatch_intent)
        instant = _utc_datetime(now)
        timestamp = _timestamp(instant)
        expires_at = _lease_expiry(instant, lease_seconds)
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            supervisor = self._supervisor_lease(db)
            if (
                supervisor.supervisor_id != supervisor_id
                or supervisor.generation != supervisor_generation
                or _parse_timestamp(supervisor.lease_expires_at) <= instant
            ):
                raise WorkspaceConflict("Supervisor lease is stale or expired")
            expires_at = min(
                _parse_timestamp(expires_at),
                _parse_timestamp(supervisor.lease_expires_at),
            ).isoformat()
            job = self._job(db, job_id)
            operation_row = db.execute(
                "SELECT run_id FROM job_operations WHERE job_id=? AND attempt_id=?",
                (job_id, attempt_id),
            ).fetchone()
            if operation_row is None:
                raise KeyError("Job attempt has no durable operation binding")
            if job.state != JobState.RUNNING:
                raise WorkspaceConflict("Only a running job attempt can be claimed")
            existing_row = db.execute(
                "SELECT * FROM worker_ownership WHERE job_id=? AND attempt_id=?",
                (job_id, attempt_id),
            ).fetchone()
            if existing_row is not None:
                existing = self._worker_ownership(existing_row)
                existing_expiry = _parse_timestamp(existing.lease_expires_at)
                if existing_expiry > instant:
                    same_owner = (
                        existing.worker_id == worker_id
                        and existing.supervisor_id == supervisor_id
                        and existing.supervisor_generation == supervisor_generation
                        and existing.process_id == process_id
                        and existing.process_started_at == process_started_at
                        and existing.dispatch_intent == intent
                    )
                    if same_owner:
                        return existing
                    raise WorkspaceConflict("Job attempt already has a live worker")
                if existing.cancellation_requested:
                    raise WorkspaceConflict(
                        "Cancelled worker attempt cannot be reclaimed"
                    )
                revision = existing.revision + 1
                db.execute(
                    "UPDATE worker_ownership SET worker_id=?,run_id=?,supervisor_id=?,"
                    "supervisor_generation=?,process_id=?,process_started_at=?,"
                    "heartbeat_at=?,lease_expires_at=?,dispatch_intent=?,state=?,"
                    "revision=?,updated_at=? WHERE job_id=? AND attempt_id=?",
                    (
                        worker_id,
                        operation_row["run_id"],
                        supervisor_id,
                        supervisor_generation,
                        process_id,
                        process_started_at,
                        timestamp,
                        expires_at,
                        intent.value,
                        WorkerState.CLAIMED.value,
                        revision,
                        timestamp,
                        job_id,
                        attempt_id,
                    ),
                )
            else:
                revision = 1
                db.execute(
                    "INSERT INTO worker_ownership "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        worker_id,
                        job_id,
                        operation_row["run_id"],
                        attempt_id,
                        supervisor_id,
                        supervisor_generation,
                        process_id,
                        process_started_at,
                        timestamp,
                        expires_at,
                        0,
                        intent.value,
                        WorkerState.CLAIMED.value,
                        revision,
                        timestamp,
                    ),
                )
            ownership = self._worker_ownership(
                db.execute(
                    "SELECT * FROM worker_ownership WHERE job_id=? AND attempt_id=?",
                    (job_id, attempt_id),
                ).fetchone()
            )
            self._event(
                db,
                job.repository_id,
                "worker_ownership",
                worker_id,
                EventType.WORKER_OWNERSHIP_UPDATED,
                {
                    "job_id": job_id,
                    "run_id": ownership.run_id,
                    "attempt_id": attempt_id,
                    "supervisor_generation": supervisor_generation,
                    "dispatch_intent": intent.value,
                    "state": WorkerState.CLAIMED.value,
                    "revision": revision,
                },
                timestamp,
            )
            return ownership

    def heartbeat_job_worker(
        self,
        job_id: str,
        attempt_id: str,
        *,
        worker_id: str,
        supervisor_id: str,
        supervisor_generation: int,
        process_id: int,
        process_started_at: str,
        lease_seconds: int = 30,
        now: datetime | None = None,
    ) -> WorkerOwnership:
        instant = _utc_datetime(now)
        timestamp = _timestamp(instant)
        expires_at = _lease_expiry(instant, lease_seconds)
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            supervisor = self._supervisor_lease(db)
            if (
                supervisor.supervisor_id != supervisor_id
                or supervisor.generation != supervisor_generation
                or _parse_timestamp(supervisor.lease_expires_at) <= instant
            ):
                raise WorkspaceConflict("Supervisor lease is stale or expired")
            expires_at = min(
                _parse_timestamp(expires_at),
                _parse_timestamp(supervisor.lease_expires_at),
            ).isoformat()
            row = db.execute(
                "SELECT * FROM worker_ownership WHERE job_id=? AND attempt_id=?",
                (job_id, attempt_id),
            ).fetchone()
            ownership = self._worker_ownership(row)
            matches = (
                ownership.worker_id == worker_id
                and ownership.supervisor_id == supervisor_id
                and ownership.supervisor_generation == supervisor_generation
                and ownership.process_id == process_id
                and ownership.process_started_at == process_started_at
            )
            if not matches or _parse_timestamp(ownership.lease_expires_at) <= instant:
                raise WorkspaceConflict("Worker ownership is stale or expired")
            if ownership.state == WorkerState.EXITED:
                raise WorkspaceConflict("Exited worker cannot renew its lease")
            db.execute(
                "UPDATE worker_ownership SET heartbeat_at=?,lease_expires_at=?,"
                "state=?,revision=revision+1,updated_at=? "
                "WHERE job_id=? AND attempt_id=?",
                (
                    timestamp,
                    expires_at,
                    WorkerState.RUNNING.value,
                    timestamp,
                    job_id,
                    attempt_id,
                ),
            )
            updated = self._worker_ownership(
                db.execute(
                    "SELECT * FROM worker_ownership WHERE job_id=? AND attempt_id=?",
                    (job_id, attempt_id),
                ).fetchone()
            )
            return updated

    def get_job_worker_ownership(self, job_id: str, attempt_id: str) -> WorkerOwnership:
        with self._connection() as db:
            return self._worker_ownership(
                db.execute(
                    "SELECT * FROM worker_ownership WHERE job_id=? AND attempt_id=?",
                    (job_id, attempt_id),
                ).fetchone()
            )

    def mark_job_worker_exited(
        self,
        job_id: str,
        attempt_id: str,
        *,
        expected_revision: int,
        worker_id: str,
        supervisor_id: str,
        supervisor_generation: int,
        process_id: int,
        process_started_at: str,
        now: datetime | None = None,
    ) -> WorkerOwnership:
        instant = _utc_datetime(now)
        timestamp = _timestamp(instant)
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            supervisor = self._supervisor_lease(db)
            if (
                supervisor.supervisor_id != supervisor_id
                or supervisor.generation != supervisor_generation
                or _parse_timestamp(supervisor.lease_expires_at) <= instant
            ):
                raise WorkspaceConflict("Supervisor lease is stale or expired")
            row = db.execute(
                "SELECT * FROM worker_ownership WHERE job_id=? AND attempt_id=?",
                (job_id, attempt_id),
            ).fetchone()
            ownership = self._worker_ownership(row)
            same_process = (
                ownership.worker_id == worker_id
                and ownership.supervisor_id == supervisor_id
                and ownership.supervisor_generation == supervisor_generation
                and ownership.process_id == process_id
                and ownership.process_started_at == process_started_at
            )
            if same_process and ownership.state == WorkerState.EXITED:
                return ownership
            if ownership.revision != expected_revision:
                raise WorkspaceConflict("Worker ownership revision has changed")
            if (
                not same_process
                or _parse_timestamp(ownership.lease_expires_at) <= instant
            ):
                raise WorkspaceConflict("Worker ownership is stale or expired")
            db.execute(
                "UPDATE worker_ownership SET state=?,lease_expires_at=?,"
                "revision=revision+1,updated_at=? WHERE job_id=? AND attempt_id=?",
                (
                    WorkerState.EXITED.value,
                    timestamp,
                    timestamp,
                    job_id,
                    attempt_id,
                ),
            )
            job = self._job(db, job_id)
            self._event(
                db,
                job.repository_id,
                "worker_ownership",
                worker_id,
                EventType.WORKER_OWNERSHIP_UPDATED,
                {
                    "job_id": job_id,
                    "attempt_id": attempt_id,
                    "state": WorkerState.EXITED.value,
                    "revision": ownership.revision + 1,
                },
                timestamp,
            )
            return self._worker_ownership(
                db.execute(
                    "SELECT * FROM worker_ownership WHERE job_id=? AND attempt_id=?",
                    (job_id, attempt_id),
                ).fetchone()
            )

    def request_job_worker_cancellation(
        self,
        job_id: str,
        attempt_id: str,
        *,
        expected_revision: int,
        request_id: str | None = None,
    ) -> WorkerOwnership:
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM worker_ownership WHERE job_id=? AND attempt_id=?",
                (job_id, attempt_id),
            ).fetchone()
            ownership = self._worker_ownership(row)
            job = self._job(db, job_id)
            command = {
                "operation": "worker.cancel",
                "job_id": job_id,
                "attempt_id": attempt_id,
                "expected_revision": expected_revision,
            }
            replay = self._replay_request(
                db, job.repository_id, request_id, command, "worker_ownership"
            )
            if replay is not None:
                return WorkerOwnership.from_dict(replay)
            if ownership.revision != expected_revision:
                raise WorkspaceConflict("Worker ownership revision has changed")
            if ownership.state == WorkerState.EXITED:
                raise WorkspaceConflict("Exited worker cannot be cancelled")
            timestamp = _now()
            if not ownership.cancellation_requested:
                db.execute(
                    "UPDATE worker_ownership SET cancellation_requested=1,"
                    "revision=revision+1,updated_at=? WHERE job_id=? AND attempt_id=?",
                    (timestamp, job_id, attempt_id),
                )
                self._event(
                    db,
                    job.repository_id,
                    "worker_ownership",
                    ownership.worker_id,
                    EventType.WORKER_OWNERSHIP_UPDATED,
                    {
                        "job_id": job_id,
                        "attempt_id": attempt_id,
                        "cancellation_requested": True,
                        "revision": ownership.revision + 1,
                    },
                    timestamp,
                )
            updated = self._worker_ownership(
                db.execute(
                    "SELECT * FROM worker_ownership WHERE job_id=? AND attempt_id=?",
                    (job_id, attempt_id),
                ).fetchone()
            )
            self._record_request(
                db,
                job.repository_id,
                request_id,
                command,
                "worker_ownership",
                updated.to_dict(),
                timestamp,
            )
            return updated

    def list_expired_job_workers(
        self,
        repository_id: str,
        *,
        limit: int = 100,
        now: datetime | None = None,
    ) -> tuple[WorkerOwnership, ...]:
        if not 1 <= limit <= 1000:
            raise ValueError("Invalid worker page size")
        cutoff = _timestamp(_utc_datetime(now))
        with self._connection() as db:
            rows = db.execute(
                "SELECT w.job_id,w.attempt_id FROM worker_ownership w "
                "JOIN jobs j ON j.job_id=w.job_id WHERE j.repository_id=? "
                "AND w.lease_expires_at<=? AND w.state!='exited' "
                "ORDER BY w.lease_expires_at LIMIT ?",
                (repository_id, cutoff, limit),
            ).fetchall()
            return tuple(
                self._worker_ownership(
                    db.execute(
                        "SELECT * FROM worker_ownership "
                        "WHERE job_id=? AND attempt_id=?",
                        (row["job_id"], row["attempt_id"]),
                    ).fetchone()
                )
                for row in rows
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
