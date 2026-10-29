"""Local filesystem adapters for durable planning state."""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

from powdrr_service.planning.checkpoints import (
    JournalForkError,
    JournalState,
    PlanningCheckpoint,
    PlanningJournalError,
    QuestionDiagnostic,
    StaleCheckpointParent,
    prepare_checkpoint_append,
    reduce_checkpoint_comments,
    reduce_checkpoint_events,
    sanitize_diagnostic,
    validate_session_id,
)


class LocalCheckpointStore:
    """Durable, hash-linked planning journal stored in Git metadata."""

    def __init__(self, repo_root: str | Path, *, session_id: str) -> None:
        validate_session_id(session_id)
        self.session_id = session_id
        root = Path(repo_root).resolve()
        try:
            result = subprocess.run(
                [
                    "git",
                    "-C",
                    str(root),
                    "rev-parse",
                    "--path-format=absolute",
                    "--git-common-dir",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
        except (OSError, subprocess.CalledProcessError) as error:
            raise PlanningJournalError(
                "could not locate Git metadata for the local planning journal"
            ) from error
        common_git_directory = Path(result.stdout.strip()).resolve()
        self.directory = (
            common_git_directory / "powdrr-lift" / "planning" / "checkpoints"
        )
        self.path = self.directory / f"{session_id}.json"
        self.lock_path = self.directory / f"{session_id}.lock"

    def load(self, *, selected_checkpoint_digest: str | None = None) -> JournalState:
        if not self.path.exists():
            return reduce_checkpoint_comments(
                (),
                session_id=self.session_id,
                selected_checkpoint_digest=selected_checkpoint_digest,
            )
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise PlanningJournalError(
                "local planning journal is unreadable"
            ) from error
        if (
            not isinstance(raw, Mapping)
            or raw.get("schema") != "planning-checkpoint-journal-v1"
            or raw.get("session_id") != self.session_id
            or not isinstance(raw.get("events"), list)
        ):
            raise PlanningJournalError("local planning journal is malformed")
        legacy_comments = raw.get("legacy_comments", [])
        if not isinstance(legacy_comments, list):
            raise PlanningJournalError(
                "local planning journal legacy data is malformed"
            )
        if legacy_comments:
            return reduce_checkpoint_comments(
                legacy_comments,
                session_id=self.session_id,
                selected_checkpoint_digest=selected_checkpoint_digest,
            )
        events: list[PlanningCheckpoint] = []
        for event_data in raw["events"]:
            if not isinstance(event_data, Mapping):
                raise PlanningJournalError("local planning journal event is malformed")
            event = PlanningCheckpoint.from_data(event_data)
            if event.session_id != self.session_id:
                raise PlanningJournalError("local planning journal has a foreign event")
            events.append(event)
        return reduce_checkpoint_events(
            tuple(events),
            selected_checkpoint_digest=selected_checkpoint_digest,
            incomplete_manifest_digests=(),
        )

    def import_legacy_comments(
        self, comments: Sequence[Mapping[str, Any]]
    ) -> JournalState:
        """Cache an older GitHub journal locally after one migration read."""
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.directory, 0o700)
        if not self.path.exists():
            self._write_raw(
                {
                    "schema": "planning-checkpoint-journal-v1",
                    "session_id": self.session_id,
                    "events": [],
                    "legacy_comments": list(comments),
                }
            )
        return self.load()

    def append(
        self,
        checkpoint: PlanningCheckpoint,
        *,
        expected_parent_digest: str | None,
        selected_checkpoint_digest: str | None = None,
    ) -> JournalState:
        if checkpoint.session_id != self.session_id:
            raise PlanningJournalError("checkpoint belongs to another planning session")
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.directory, 0o700)
        lock_descriptor = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            os.fchmod(lock_descriptor, 0o600)
            fcntl.flock(lock_descriptor, fcntl.LOCK_EX)
            current = self.load(selected_checkpoint_digest=selected_checkpoint_digest)
            if (
                current.status == "conflicted"
                or current.head is None
                and current.events
            ):
                raise JournalForkError(
                    "planning journal is conflicted; select a checkpoint to resume"
                )
            current_digest = None if current.head is None else current.head.digest
            if current_digest != expected_parent_digest:
                raise StaleCheckpointParent(
                    "checkpoint parent is stale: expected "
                    f"{expected_parent_digest!r}, current head is {current_digest!r}"
                )
            if checkpoint.parent_digest != current_digest:
                raise StaleCheckpointParent(
                    "new checkpoint does not extend the current head"
                )
            expected_sequence = 1 if current.head is None else current.head.sequence + 1
            if checkpoint.sequence != expected_sequence:
                raise StaleCheckpointParent(
                    "new checkpoint sequence does not follow its parent"
                )
            events = (*current.events, checkpoint)
            self._write(events)
            updated = self.load(
                selected_checkpoint_digest=(
                    checkpoint.digest
                    if selected_checkpoint_digest is not None
                    else None
                )
            )
            if updated.head is None or updated.head.digest != checkpoint.digest:
                raise JournalForkError(
                    "local checkpoint append did not produce a unique canonical head"
                )
            return updated
        finally:
            fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
            os.close(lock_descriptor)

    def _write(self, events: Sequence[PlanningCheckpoint]) -> None:
        self._write_raw(
            {
                "schema": "planning-checkpoint-journal-v1",
                "session_id": self.session_id,
                "events": [event.to_data() for event in events],
            }
        )

    def _write_raw(self, record: Mapping[str, Any]) -> None:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self.session_id}-", suffix=".tmp", dir=self.directory
        )
        temporary_path = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(
                    record,
                    stream,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, self.path)
        finally:
            temporary_path.unlink(missing_ok=True)


class LocalDiagnosticCache:
    """Best-effort diagnostic files under Git common-dir; never journal authority."""

    def __init__(
        self,
        git_common_dir: Path,
        *,
        session_id: str,
        secret_values: Sequence[str] = (),
    ) -> None:
        validate_session_id(session_id)
        common_dir = git_common_dir.resolve()
        self.directory = common_dir / "powdrr-lift" / "planning-runs" / session_id
        self.directory.mkdir(parents=True, exist_ok=True)
        self.secret_values = tuple(secret for secret in secret_values if secret)

    def write(
        self,
        question_id: str,
        diagnostic: QuestionDiagnostic | Mapping[str, Any],
    ) -> Path:
        validate_session_id(question_id)
        record = (
            diagnostic
            if isinstance(diagnostic, QuestionDiagnostic)
            else QuestionDiagnostic.from_mapping(diagnostic)
        )
        if record.question_id != question_id:
            raise PlanningJournalError("diagnostic question ID does not match filename")
        sanitized = sanitize_diagnostic(
            record.to_data(), secret_values=self.secret_values
        )
        serialized = json.dumps(
            sanitized,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        destination = self.directory / f"{question_id}.json"
        file_descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{question_id}.", suffix=".tmp", dir=self.directory
        )
        try:
            with os.fdopen(file_descriptor, "w", encoding="utf-8") as handle:
                handle.write(serialized)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, destination)
            destination.chmod(0o600)
        finally:
            if os.path.exists(temporary_name):
                os.unlink(temporary_name)
        return destination

    def read(self, question_id: str) -> Mapping[str, Any] | None:
        validate_session_id(question_id)
        path = self.directory / f"{question_id}.json"
        if not path.is_file():
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, Mapping) else None


class IssueCommentAPI(Protocol):
    def list_comments(self, issue_number: int) -> tuple[Mapping[str, Any], ...]: ...

    def add_issue_comment(self, issue_number: int, body: str) -> None: ...


class GitHubCommentCheckpointStore:
    """Local adapter persisting service-rendered checkpoints in GitHub comments."""

    def __init__(
        self, client: IssueCommentAPI, issue_number: int, *, session_id: str
    ) -> None:
        validate_session_id(session_id)
        if (
            isinstance(issue_number, bool)
            or not isinstance(issue_number, int)
            or issue_number < 1
        ):
            raise PlanningJournalError("issue number must be a positive integer")
        self.client = client
        self.issue_number = issue_number
        self.session_id = session_id

    def load(self, *, selected_checkpoint_digest: str | None = None) -> JournalState:
        return reduce_checkpoint_comments(
            self.client.list_comments(self.issue_number),
            session_id=self.session_id,
            selected_checkpoint_digest=selected_checkpoint_digest,
        )

    def append(
        self,
        checkpoint: PlanningCheckpoint,
        *,
        expected_parent_digest: str | None,
        selected_checkpoint_digest: str | None = None,
    ) -> JournalState:
        current = self.load(selected_checkpoint_digest=selected_checkpoint_digest)
        comments = prepare_checkpoint_append(
            checkpoint,
            current,
            session_id=self.session_id,
            expected_parent_digest=expected_parent_digest,
        )
        for body in comments:
            self.client.add_issue_comment(self.issue_number, body)
        updated = self.load(
            selected_checkpoint_digest=(
                checkpoint.digest if selected_checkpoint_digest is not None else None
            )
        )
        if updated.head is None or updated.head.digest != checkpoint.digest:
            raise JournalForkError(
                "checkpoint append did not produce a unique canonical head"
            )
        return updated
