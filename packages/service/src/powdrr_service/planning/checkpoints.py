"""Local planning journal, legacy GitHub resume reducer, and diagnostics cache."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import zlib
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from powdrr_service.planning.contracts import PlanningContractError, Question

MAX_CHECKPOINT_COMMENT_CHARS = 48 * 1024
MAX_CHECKPOINT_ARCHIVE_BYTES = 64 * 1024 * 1024
_CHUNK_RAW_BYTES = 24 * 1024
_SESSION_ID_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$")
_DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_EVENT_MARKER = "<!-- powdrr-planning-checkpoint:v1 -->"
_CHUNK_MARKER = "<!-- powdrr-planning-checkpoint-chunk:v1 -->"
_MANIFEST_MARKER = "<!-- powdrr-planning-checkpoint-manifest:v1 -->"
_ARCHIVE_CHUNK_MARKER = "<!-- powdrr-planning-checkpoint-archive-chunk:v1 -->"
_ARCHIVE_MANIFEST_MARKER = "<!-- powdrr-planning-checkpoint-archive-manifest:v1 -->"
_SECRET_KEY_PATTERN = re.compile(
    r"(?:authorization|authentication|api[_-]?key|(?:access|refresh|auth)?[_-]?token$|"
    r"secret|password|passwd|credential|cookie|private[_-]?key)",
    re.IGNORECASE,
)
_TOKEN_PATTERN = re.compile(
    r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+|\b(?:sk[-_]|hf_|gh[pousr]_|github_pat_)[A-Za-z0-9_-]{8,}"
)


class PlanningJournalError(RuntimeError):
    """Invalid or conflicting durable planning journal state."""


class StaleCheckpointParent(PlanningJournalError):
    """An append attempted to extend a checkpoint other than the current head."""


class JournalForkError(PlanningJournalError):
    """More than one durable checkpoint extends the same parent."""


def canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise PlanningJournalError(
            "checkpoint data must be finite JSON data"
        ) from error


def _digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


@dataclass(frozen=True, slots=True)
class PlanningCheckpoint:
    """One immutable, hash-linked planning state transition."""

    session_id: str
    event_id: str
    sequence: int
    parent_digest: str | None
    event_type: str
    payload: Mapping[str, Any]
    id_counters: Mapping[str, int]
    digest: str

    @classmethod
    def question_started(
        cls,
        *,
        session_id: str,
        event_id: str,
        sequence: int,
        parent_digest: str | None,
        question: Question,
        id_counters: Mapping[str, int],
    ) -> PlanningCheckpoint:
        return cls.create(
            session_id=session_id,
            event_id=event_id,
            sequence=sequence,
            parent_digest=parent_digest,
            event_type="question_started",
            payload={
                "question": question.to_data(),
                "input_digest": question.input_digest,
                "accepted_answer": None,
                "validation_result": None,
                "state_transition": "question_started",
            },
            id_counters=id_counters,
        )

    @classmethod
    def answer_accepted(
        cls,
        *,
        session_id: str,
        event_id: str,
        sequence: int,
        parent_digest: str | None,
        question: Question,
        accepted_answer: Mapping[str, Any],
        validation_result: Mapping[str, Any],
        id_counters: Mapping[str, int],
    ) -> PlanningCheckpoint:
        return cls.create(
            session_id=session_id,
            event_id=event_id,
            sequence=sequence,
            parent_digest=parent_digest,
            event_type="answer_accepted",
            payload={
                "question": question.to_data(),
                "input_digest": question.input_digest,
                "accepted_answer": dict(accepted_answer),
                "validation_result": dict(validation_result),
                "state_transition": "question_accepted",
            },
            id_counters=id_counters,
        )

    @classmethod
    def needs_answer(
        cls,
        *,
        session_id: str,
        event_id: str,
        sequence: int,
        parent_digest: str | None,
        question: Question,
        error: PlanningContractError,
        previous_answer: Mapping[str, Any],
        id_counters: Mapping[str, int],
    ) -> PlanningCheckpoint:
        return cls.create(
            session_id=session_id,
            event_id=event_id,
            sequence=sequence,
            parent_digest=parent_digest,
            event_type="needs_answer",
            payload={
                "question": question.to_data(),
                "input_digest": question.input_digest,
                "accepted_answer": None,
                "previous_answer": dict(previous_answer),
                "validation_result": error.to_data(),
                "state_transition": "needs_answer",
            },
            id_counters=id_counters,
        )

    @classmethod
    def create(
        cls,
        *,
        session_id: str,
        event_id: str,
        sequence: int,
        parent_digest: str | None,
        event_type: str,
        payload: Mapping[str, Any],
        id_counters: Mapping[str, int],
    ) -> PlanningCheckpoint:
        validate_session_id(session_id)
        _require_text(event_id, "event_id")
        _require_text(event_type, "event_type")
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
            raise PlanningJournalError("checkpoint sequence must be positive")
        if parent_digest is not None and not _DIGEST_PATTERN.fullmatch(parent_digest):
            raise PlanningJournalError("parent_digest must be a sha256 digest or null")
        counters = _validate_counters(id_counters)
        _ = canonical_json(payload)
        data = {
            "schema": "planning-checkpoint-v1",
            "session_id": session_id,
            "event_id": event_id,
            "sequence": sequence,
            "parent_digest": parent_digest,
            "event_type": event_type,
            "payload": dict(payload),
            "id_counters": counters,
        }
        digest = _digest(canonical_json(data).encode("utf-8"))
        return cls(
            session_id=session_id,
            event_id=event_id,
            sequence=sequence,
            parent_digest=parent_digest,
            event_type=event_type,
            payload=dict(payload),
            id_counters=counters,
            digest=digest,
        )

    @classmethod
    def from_data(cls, value: Mapping[str, Any]) -> PlanningCheckpoint:
        expected = {
            "schema",
            "session_id",
            "event_id",
            "sequence",
            "parent_digest",
            "event_type",
            "payload",
            "id_counters",
            "digest",
        }
        _exact_fields(value, expected, "checkpoint")
        if value["schema"] != "planning-checkpoint-v1":
            raise PlanningJournalError("unsupported planning checkpoint schema")
        payload = value["payload"]
        counters = value["id_counters"]
        if not isinstance(payload, Mapping) or not isinstance(counters, Mapping):
            raise PlanningJournalError(
                "checkpoint payload and id_counters must be objects"
            )
        parent_digest = value["parent_digest"]
        if parent_digest is not None and not isinstance(parent_digest, str):
            raise PlanningJournalError("parent_digest must be a string or null")
        sequence = value["sequence"]
        if isinstance(sequence, bool) or not isinstance(sequence, int):
            raise PlanningJournalError("checkpoint sequence must be an integer")
        checkpoint = cls.create(
            session_id=_require_text(value["session_id"], "session_id"),
            event_id=_require_text(value["event_id"], "event_id"),
            sequence=sequence,
            parent_digest=parent_digest,
            event_type=_require_text(value["event_type"], "event_type"),
            payload=payload,
            id_counters=counters,
        )
        if value["digest"] != checkpoint.digest:
            raise PlanningJournalError("checkpoint digest does not match its contents")
        return checkpoint

    def to_data(self) -> dict[str, Any]:
        return {
            "schema": "planning-checkpoint-v1",
            "session_id": self.session_id,
            "event_id": self.event_id,
            "sequence": self.sequence,
            "parent_digest": self.parent_digest,
            "event_type": self.event_type,
            "payload": dict(self.payload),
            "id_counters": dict(self.id_counters),
            "digest": self.digest,
        }


@dataclass(frozen=True, slots=True)
class JournalState:
    status: str
    head: PlanningCheckpoint | None
    events: tuple[PlanningCheckpoint, ...]
    fork_parent_digests: tuple[str | None, ...]
    stale_event_digests: tuple[str, ...]
    incomplete_manifest_digests: tuple[str, ...]
    selected_checkpoint_digest: str | None = None

    @property
    def id_counters(self) -> Mapping[str, int]:
        return {} if self.head is None else self.head.id_counters

    @property
    def can_publish(self) -> bool:
        return self.status == "active" and self.head is not None

    @property
    def accepted_answers(self) -> tuple[Mapping[str, Any], ...]:
        return tuple(
            event.payload
            for event in self.events
            if event.event_type == "answer_accepted"
        )

    @property
    def pending_question(self) -> Mapping[str, Any] | None:
        pending: Mapping[str, Any] | None = None
        for event in self.events:
            question = event.payload.get("question")
            if event.event_type in {"question_started", "needs_answer"} and isinstance(
                question, Mapping
            ):
                pending = question
            elif event.event_type == "answer_accepted" and isinstance(
                question, Mapping
            ):
                if pending is not None and pending.get("question_id") == question.get(
                    "question_id"
                ):
                    pending = None
        return pending


@dataclass(frozen=True, slots=True)
class QuestionDiagnostic:
    """Local trace fields required to reproduce one model question safely."""

    question_id: str
    request: Any
    response: Any
    schema_version: str
    provider: str
    model: str
    usage: Mapping[str, Any] | None
    wall_time_ms: float
    validation_errors: tuple[Any, ...]

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> QuestionDiagnostic:
        expected = {
            "question_id",
            "request",
            "response",
            "schema_version",
            "provider",
            "model",
            "usage",
            "wall_time_ms",
            "validation_errors",
        }
        _exact_fields(value, expected, "question diagnostic")
        question_id = _require_text(value["question_id"], "question_id")
        validate_session_id(question_id)
        schema_version = _require_text(value["schema_version"], "schema_version")
        provider = _require_text(value["provider"], "provider")
        model = _require_text(value["model"], "model")
        usage = value["usage"]
        if usage is not None and not isinstance(usage, Mapping):
            raise PlanningJournalError("diagnostic usage must be an object or null")
        wall_time_ms = value["wall_time_ms"]
        if (
            isinstance(wall_time_ms, bool)
            or not isinstance(wall_time_ms, int | float)
            or wall_time_ms < 0
        ):
            raise PlanningJournalError("diagnostic wall_time_ms must be non-negative")
        errors = value["validation_errors"]
        if not isinstance(errors, Sequence) or isinstance(errors, (str, bytes)):
            raise PlanningJournalError("diagnostic validation_errors must be an array")
        diagnostic = cls(
            question_id=question_id,
            request=value["request"],
            response=value["response"],
            schema_version=schema_version,
            provider=provider,
            model=model,
            usage=usage,
            wall_time_ms=float(wall_time_ms),
            validation_errors=tuple(errors),
        )
        _ = canonical_json(diagnostic.to_data())
        return diagnostic

    def to_data(self) -> dict[str, Any]:
        return {
            "question_id": self.question_id,
            "request": self.request,
            "response": self.response,
            "schema_version": self.schema_version,
            "provider": self.provider,
            "model": self.model,
            "usage": None if self.usage is None else dict(self.usage),
            "wall_time_ms": self.wall_time_ms,
            "validation_errors": list(self.validation_errors),
        }


class CheckpointStore(Protocol):
    def load(
        self, *, selected_checkpoint_digest: str | None = None
    ) -> JournalState: ...

    def append(
        self,
        checkpoint: PlanningCheckpoint,
        *,
        expected_parent_digest: str | None,
        selected_checkpoint_digest: str | None = None,
    ) -> JournalState: ...


def render_checkpoint_comment(checkpoint: PlanningCheckpoint) -> str:
    body = _render_comment(
        _EVENT_MARKER,
        "Powdrr planning checkpoint",
        checkpoint.to_data(),
    )
    if _comment_size(body) >= MAX_CHECKPOINT_COMMENT_CHARS:
        raise PlanningJournalError("checkpoint is too large for one issue comment")
    return body


def render_checkpoint_comments(
    checkpoint: PlanningCheckpoint,
) -> tuple[str, ...]:
    """Render one checkpoint comment or chunks followed by a manifest."""
    direct = _render_comment(
        _EVENT_MARKER,
        "Powdrr planning checkpoint",
        checkpoint.to_data(),
    )
    if _comment_size(direct) < MAX_CHECKPOINT_COMMENT_CHARS:
        return (direct,)

    payload_bytes = canonical_json(checkpoint.to_data()).encode("utf-8")
    pieces = [
        payload_bytes[offset : offset + _CHUNK_RAW_BYTES]
        for offset in range(0, len(payload_bytes), _CHUNK_RAW_BYTES)
    ]
    chunk_comments: list[str] = []
    chunk_records: list[dict[str, Any]] = []
    for index, piece in enumerate(pieces, start=1):
        chunk_digest = _digest(piece)
        chunk = {
            "schema": "planning-checkpoint-chunk-v1",
            "session_id": checkpoint.session_id,
            "event_digest": checkpoint.digest,
            "chunk_index": index,
            "chunk_count": len(pieces),
            "chunk_digest": chunk_digest,
            "content_base64": base64.b64encode(piece).decode("ascii"),
        }
        body = _render_comment(
            _CHUNK_MARKER,
            f"Powdrr planning checkpoint chunk {index}/{len(pieces)}",
            chunk,
        )
        if _comment_size(body) >= MAX_CHECKPOINT_COMMENT_CHARS:
            raise AssertionError("checkpoint chunk exceeds the GitHub comment limit")
        chunk_comments.append(body)
        chunk_records.append({"index": index, "digest": chunk_digest})

    manifest = {
        "schema": "planning-checkpoint-manifest-v1",
        "session_id": checkpoint.session_id,
        "event_digest": checkpoint.digest,
        "parent_digest": checkpoint.parent_digest,
        "chunk_count": len(pieces),
        "payload_digest": _digest(payload_bytes),
        "chunks": chunk_records,
    }
    manifest_body = _render_comment(
        _MANIFEST_MARKER,
        "Powdrr planning checkpoint manifest",
        manifest,
    )
    if _comment_size(manifest_body) >= MAX_CHECKPOINT_COMMENT_CHARS:
        raise PlanningJournalError(
            "checkpoint manifest exceeds the GitHub comment limit"
        )
    return (*chunk_comments, manifest_body)


def render_checkpoint_archive_comments(
    events: Sequence[PlanningCheckpoint], *, session_id: str
) -> tuple[str, ...]:
    """Render a lossless compressed archive that can replace an old event tail."""
    validate_session_id(session_id)
    ordered = tuple(events)
    if any(event.session_id != session_id for event in ordered):
        raise PlanningJournalError("archive contains a checkpoint from another session")
    payload_bytes = canonical_json(
        {
            "schema": "planning-checkpoint-archive-v1",
            "session_id": session_id,
            "events": [event.to_data() for event in ordered],
        }
    ).encode("utf-8")
    if len(payload_bytes) > MAX_CHECKPOINT_ARCHIVE_BYTES:
        raise PlanningJournalError("planning checkpoint archive exceeds the size limit")
    compressed = zlib.compress(payload_bytes, level=9)
    archive_digest = _digest(compressed)
    pieces = [
        compressed[offset : offset + _CHUNK_RAW_BYTES]
        for offset in range(0, len(compressed), _CHUNK_RAW_BYTES)
    ]
    chunk_comments: list[str] = []
    chunk_records: list[dict[str, Any]] = []
    for index, piece in enumerate(pieces, start=1):
        chunk_digest = _digest(piece)
        chunk_payload = {
            "schema": "planning-checkpoint-archive-chunk-v1",
            "session_id": session_id,
            "archive_digest": archive_digest,
            "chunk_index": index,
            "chunk_count": len(pieces),
            "chunk_digest": chunk_digest,
            "content_base64": base64.b64encode(piece).decode("ascii"),
        }
        body = _render_comment(
            _ARCHIVE_CHUNK_MARKER,
            f"Powdrr planning checkpoint archive chunk {index}/{len(pieces)}",
            chunk_payload,
        )
        if _comment_size(body) >= MAX_CHECKPOINT_COMMENT_CHARS:
            raise AssertionError("checkpoint archive chunk exceeds the comment limit")
        chunk_comments.append(body)
        chunk_records.append({"index": index, "digest": chunk_digest})
    manifest = {
        "schema": "planning-checkpoint-archive-manifest-v1",
        "session_id": session_id,
        "archive_digest": archive_digest,
        "head_digest": None if not ordered else ordered[-1].digest,
        "event_count": len(ordered),
        "chunk_count": len(pieces),
        "payload_digest": _digest(payload_bytes),
        "chunks": chunk_records,
    }
    manifest_body = _render_comment(
        _ARCHIVE_MANIFEST_MARKER,
        "Powdrr planning checkpoint archive manifest",
        manifest,
    )
    if _comment_size(manifest_body) >= MAX_CHECKPOINT_COMMENT_CHARS:
        raise PlanningJournalError(
            "checkpoint archive manifest exceeds the comment limit"
        )
    return (*chunk_comments, manifest_body)


def reduce_checkpoint_comments(
    comments: Sequence[Mapping[str, Any]],
    *,
    session_id: str,
    selected_checkpoint_digest: str | None = None,
) -> JournalState:
    """Reduce immutable GitHub comments into one safe resumable journal state."""
    validate_session_id(session_id)
    chunks: dict[str, dict[int, tuple[Mapping[str, Any], bytes]]] = defaultdict(dict)
    manifests: list[Mapping[str, Any]] = []
    events: dict[str, PlanningCheckpoint] = {}
    archive_chunks: dict[str, dict[int, tuple[Mapping[str, Any], bytes]]] = defaultdict(
        dict
    )
    archive_manifests: list[Mapping[str, Any]] = []
    for comment in comments:
        body = comment.get("body")
        if not isinstance(body, str):
            continue
        parsed = _comment_payload(body)
        if parsed is None:
            continue
        marker, payload = parsed
        if marker == _EVENT_MARKER:
            try:
                checkpoint = PlanningCheckpoint.from_data(payload)
            except PlanningJournalError:
                continue
            if checkpoint.session_id == session_id:
                events[checkpoint.digest] = checkpoint
        elif marker == _CHUNK_MARKER:
            if payload.get("session_id") != session_id:
                continue
            event_digest = payload.get("event_digest")
            index = payload.get("chunk_index")
            encoded = payload.get("content_base64")
            if (
                not isinstance(event_digest, str)
                or isinstance(index, bool)
                or not isinstance(index, int)
                or not isinstance(encoded, str)
            ):
                continue
            try:
                piece = base64.b64decode(encoded, validate=True)
            except ValueError:
                continue
            if payload.get("chunk_digest") != _digest(piece):
                continue
            chunks[event_digest][index] = (payload, piece)
        elif marker == _MANIFEST_MARKER and payload.get("session_id") == session_id:
            manifests.append(payload)
        elif marker == _ARCHIVE_CHUNK_MARKER:
            if payload.get("session_id") != session_id:
                continue
            archive_digest = payload.get("archive_digest")
            index = payload.get("chunk_index")
            encoded = payload.get("content_base64")
            if (
                not isinstance(archive_digest, str)
                or isinstance(index, bool)
                or not isinstance(index, int)
                or not isinstance(encoded, str)
            ):
                continue
            try:
                piece = base64.b64decode(encoded, validate=True)
            except ValueError:
                continue
            if payload.get("chunk_digest") != _digest(piece):
                continue
            archive_chunks[archive_digest][index] = (payload, piece)
        elif (
            marker == _ARCHIVE_MANIFEST_MARKER
            and payload.get("session_id") == session_id
        ):
            archive_manifests.append(payload)

    incomplete: set[str] = set()
    for manifest in manifests:
        event_digest = manifest.get("event_digest")
        count = manifest.get("chunk_count")
        records = manifest.get("chunks")
        if (
            not isinstance(event_digest, str)
            or isinstance(count, bool)
            or not isinstance(count, int)
            or count < 1
            or not isinstance(records, list)
            or len(records) != count
        ):
            continue
        chunk_set = chunks.get(event_digest, {})
        if set(chunk_set) != set(range(1, count + 1)):
            incomplete.add(event_digest)
            continue
        expected_digests: dict[int, str] = {}
        valid_manifest = True
        for record in records:
            if not isinstance(record, Mapping):
                valid_manifest = False
                break
            index = record.get("index")
            digest = record.get("digest")
            if (
                isinstance(index, bool)
                or not isinstance(index, int)
                or not isinstance(digest, str)
            ):
                valid_manifest = False
                break
            expected_digests[index] = digest
        if not valid_manifest:
            continue
        if any(
            expected_digests.get(index) != value[0].get("chunk_digest")
            for index, value in chunk_set.items()
        ):
            continue
        payload_bytes = b"".join(chunk_set[index][1] for index in range(1, count + 1))
        if manifest.get("payload_digest") != _digest(payload_bytes):
            continue
        try:
            decoded = json.loads(payload_bytes)
            if not isinstance(decoded, Mapping):
                continue
            checkpoint = PlanningCheckpoint.from_data(decoded)
        except (UnicodeDecodeError, json.JSONDecodeError, PlanningJournalError):
            continue
        if checkpoint.session_id == session_id and checkpoint.digest == event_digest:
            events[checkpoint.digest] = checkpoint

    for manifest in archive_manifests:
        archive_digest = manifest.get("archive_digest")
        count = manifest.get("chunk_count")
        records = manifest.get("chunks")
        if (
            not isinstance(archive_digest, str)
            or isinstance(count, bool)
            or not isinstance(count, int)
            or count < 1
            or not isinstance(records, list)
            or len(records) != count
        ):
            continue
        chunk_set = archive_chunks.get(archive_digest, {})
        if set(chunk_set) != set(range(1, count + 1)):
            incomplete.add(archive_digest)
            continue
        expected_archive_digests: dict[int, str] = {}
        valid_manifest = True
        for record in records:
            if not isinstance(record, Mapping):
                valid_manifest = False
                break
            index = record.get("index")
            digest = record.get("digest")
            if (
                isinstance(index, bool)
                or not isinstance(index, int)
                or not isinstance(digest, str)
            ):
                valid_manifest = False
                break
            expected_archive_digests[index] = digest
        if not valid_manifest or any(
            expected_archive_digests.get(index) != value[0].get("chunk_digest")
            for index, value in chunk_set.items()
        ):
            continue
        compressed = b"".join(chunk_set[index][1] for index in range(1, count + 1))
        if archive_digest != _digest(compressed):
            continue
        try:
            decompressor = zlib.decompressobj()
            payload_bytes = decompressor.decompress(
                compressed, MAX_CHECKPOINT_ARCHIVE_BYTES + 1
            )
            if (
                len(payload_bytes) > MAX_CHECKPOINT_ARCHIVE_BYTES
                or decompressor.unconsumed_tail
                or not decompressor.eof
            ):
                continue
            if manifest.get("payload_digest") != _digest(payload_bytes):
                continue
            archive = json.loads(payload_bytes)
            if (
                not isinstance(archive, Mapping)
                or archive.get("schema") != "planning-checkpoint-archive-v1"
                or archive.get("session_id") != session_id
                or not isinstance(archive.get("events"), list)
            ):
                continue
            archived_events = [
                PlanningCheckpoint.from_data(value)
                for value in archive["events"]
                if isinstance(value, Mapping)
            ]
        except (
            UnicodeDecodeError,
            json.JSONDecodeError,
            zlib.error,
            PlanningJournalError,
        ):
            continue
        if (
            len(archived_events) != len(archive["events"])
            or manifest.get("event_count") != len(archived_events)
            or (None if not archived_events else archived_events[-1].digest)
            != manifest.get("head_digest")
            or any(event.session_id != session_id for event in archived_events)
        ):
            continue
        for checkpoint in archived_events:
            events[checkpoint.digest] = checkpoint

    return reduce_checkpoint_events(
        tuple(events.values()),
        selected_checkpoint_digest=selected_checkpoint_digest,
        incomplete_manifest_digests=tuple(sorted(incomplete)),
    )


def has_complete_checkpoint_archive(
    comments: Sequence[Mapping[str, Any]], *, session_id: str
) -> bool:
    """Return whether the supplied recent comments can restore a full archive."""
    if not any(
        isinstance(comment.get("body"), str)
        and _ARCHIVE_MANIFEST_MARKER in comment["body"]
        for comment in comments
    ):
        return False
    state = reduce_checkpoint_comments(comments, session_id=session_id)
    return state.status == "active" and state.head is not None


def reduce_checkpoint_events(
    events: tuple[PlanningCheckpoint, ...],
    *,
    selected_checkpoint_digest: str | None,
    incomplete_manifest_digests: tuple[str, ...],
) -> JournalState:
    by_digest = {event.digest: event for event in events}
    children: dict[str | None, list[PlanningCheckpoint]] = defaultdict(list)
    stale: list[str] = []
    for event in events:
        if event.parent_digest is not None and event.parent_digest not in by_digest:
            stale.append(event.digest)
            continue
        expected_sequence = (
            1
            if event.parent_digest is None
            else by_digest[event.parent_digest].sequence + 1
        )
        if event.sequence != expected_sequence:
            stale.append(event.digest)
            continue
        children[event.parent_digest].append(event)
    forks = tuple(
        sorted(
            (parent for parent, values in children.items() if len(values) > 1),
            key=lambda value: "" if value is None else value,
        )
    )

    selected_path: tuple[PlanningCheckpoint, ...] = ()
    if selected_checkpoint_digest is not None:
        if selected_checkpoint_digest not in by_digest:
            raise PlanningJournalError(
                "selected checkpoint digest is not in the journal"
            )
        path: list[PlanningCheckpoint] = []
        cursor = by_digest[selected_checkpoint_digest]
        while True:
            path.append(cursor)
            if cursor.parent_digest is None:
                break
            parent = by_digest.get(cursor.parent_digest)
            if parent is None:
                raise PlanningJournalError("selected checkpoint has a stale parent")
            cursor = parent
        selected_path = tuple(reversed(path))

    conflicted = bool(forks or stale)
    if selected_path:
        head = selected_path[-1]
        status = "active"
        canonical_events = selected_path
    elif conflicted:
        head = None
        status = "conflicted"
        canonical_events = ()
    elif not events:
        head = None
        status = "empty"
        canonical_events = ()
    else:
        roots = children.get(None, [])
        current = roots[0]
        ordered = [current]
        while children.get(current.digest):
            current = children[current.digest][0]
            ordered.append(current)
        head = ordered[-1]
        status = "active"
        canonical_events = tuple(ordered)
    return JournalState(
        status=status,
        head=head,
        events=canonical_events,
        fork_parent_digests=forks,
        stale_event_digests=tuple(sorted(stale)),
        incomplete_manifest_digests=incomplete_manifest_digests,
        selected_checkpoint_digest=selected_checkpoint_digest,
    )


class InMemoryCheckpointStore:
    """Append-only fake with the same parent and reducer semantics as GitHub."""

    def __init__(self, *, session_id: str) -> None:
        validate_session_id(session_id)
        self.session_id = session_id
        self.comments: list[dict[str, str]] = []

    def load(self, *, selected_checkpoint_digest: str | None = None) -> JournalState:
        return reduce_checkpoint_comments(
            self.comments,
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
            self._add(body)
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

    def _comments(self) -> tuple[Mapping[str, Any], ...]:
        return tuple(self.comments)

    def _add(self, body: str) -> None:
        self.comments.append({"body": body})

    def duplicate_comment(self, index: int = -1) -> None:
        """Simulate GitHub redelivery without altering the original comment."""
        self.comments.append(dict(self.comments[index]))


def prepare_checkpoint_append(
    checkpoint: PlanningCheckpoint,
    current: JournalState,
    *,
    session_id: str,
    expected_parent_digest: str | None,
) -> tuple[str, ...]:
    """Validate an append against service-owned journal policy and render it."""
    if checkpoint.session_id != session_id:
        raise PlanningJournalError("checkpoint belongs to another planning session")
    if current.status == "conflicted" or current.head is None and current.events:
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
        raise StaleCheckpointParent("new checkpoint does not extend the current head")
    expected_sequence = 1 if current.head is None else current.head.sequence + 1
    if checkpoint.sequence != expected_sequence:
        raise StaleCheckpointParent(
            "new checkpoint sequence does not follow its parent"
        )
    return render_checkpoint_comments(checkpoint)


def sanitize_diagnostic(
    value: Any, *, key: str = "", secret_values: Sequence[str] = ()
) -> Any:
    """Remove credential fields and redact recognizable bearer/API tokens."""
    if _SECRET_KEY_PATTERN.search(key):
        return "[REDACTED]"
    if isinstance(value, Mapping):
        return {
            str(child_key): sanitize_diagnostic(
                child_value, key=str(child_key), secret_values=secret_values
            )
            for child_key, child_value in value.items()
            if not _SECRET_KEY_PATTERN.search(str(child_key))
        }
    if isinstance(value, list | tuple):
        return [
            sanitize_diagnostic(item, secret_values=secret_values) for item in value
        ]
    if isinstance(value, str):
        redacted = _TOKEN_PATTERN.sub("[REDACTED]", value)
        for secret in secret_values:
            redacted = redacted.replace(secret, "[REDACTED]")
        return redacted
    return value


def _render_comment(marker: str, title: str, payload: Mapping[str, Any]) -> str:
    return (
        f"### {title}\n\n{marker}\n```json\n"
        f"{json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True)}\n```"
    )


def _comment_size(body: str) -> int:
    return len(body.encode("utf-8"))


def _comment_payload(body: str) -> tuple[str, Mapping[str, Any]] | None:
    for marker in (
        _EVENT_MARKER,
        _CHUNK_MARKER,
        _MANIFEST_MARKER,
        _ARCHIVE_CHUNK_MARKER,
        _ARCHIVE_MANIFEST_MARKER,
    ):
        if marker not in body:
            continue
        try:
            encoded = body.split("```json\n", 1)[1].split("\n```", 1)[0]
            payload = json.loads(encoded)
        except (IndexError, json.JSONDecodeError):
            return None
        if isinstance(payload, Mapping):
            return marker, payload
        return None
    return None


def _validate_counters(value: Mapping[str, int]) -> dict[str, int]:
    counters: dict[str, int] = {}
    for key, count in value.items():
        if not isinstance(key, str) or not key:
            raise PlanningJournalError("id counter names must be non-empty strings")
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise PlanningJournalError("id counters must be non-negative integers")
        counters[key] = count
    return counters


def validate_session_id(value: str) -> None:
    if not isinstance(value, str) or not _SESSION_ID_PATTERN.fullmatch(value):
        raise PlanningJournalError(
            "session and question IDs must be safe path components"
        )


def _require_text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PlanningJournalError(f"{name} must be a non-empty string")
    return value.strip()


def _exact_fields(value: Mapping[str, Any], expected: set[str], name: str) -> None:
    if value.keys() != expected:
        missing = expected - value.keys()
        unknown = value.keys() - expected
        details = []
        if missing:
            details.append(f"missing {sorted(missing)}")
        if unknown:
            details.append(f"unknown {sorted(unknown)}")
        raise PlanningJournalError(f"invalid {name} fields: {'; '.join(details)}")


__all__ = [
    "MAX_CHECKPOINT_COMMENT_CHARS",
    "CheckpointStore",
    "InMemoryCheckpointStore",
    "JournalForkError",
    "JournalState",
    "PlanningCheckpoint",
    "PlanningJournalError",
    "QuestionDiagnostic",
    "StaleCheckpointParent",
    "canonical_json",
    "prepare_checkpoint_append",
    "reduce_checkpoint_comments",
    "reduce_checkpoint_events",
    "has_complete_checkpoint_archive",
    "render_checkpoint_comment",
    "render_checkpoint_comments",
    "render_checkpoint_archive_comments",
    "sanitize_diagnostic",
    "validate_session_id",
]
