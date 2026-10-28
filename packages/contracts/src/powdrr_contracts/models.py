"""Validated JSON-compatible public contracts. This package uses only stdlib."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from typing import Any, Self

SCHEMA_VERSION = 1
OperationStatus = str
JsonObject = dict[str, Any]


class ContractError(ValueError):
    """A payload is malformed or uses an unsupported public schema."""


def _object(value: object, name: str) -> JsonObject:
    if not isinstance(value, dict) or not all(isinstance(k, str) for k in value):
        raise ContractError(f"{name} must be a JSON object with string keys")
    _json_value(value, name)
    return json_round_trip(value)


def _json_value(value: object, name: str = "value") -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ContractError(f"{name} contains a non-finite number")
        return
    if isinstance(value, list):
        for item in value:
            _json_value(item, name)
        return
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        for item in value.values():
            _json_value(item, name)
        return
    raise ContractError(f"{name} must contain JSON-compatible values")


def _string(value: object, name: str, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value):
        raise ContractError(f"{name} must be a non-empty string")
    return value


def _integer(value: object, name: str, *, minimum: int = 0) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ContractError(f"{name} must be an integer >= {minimum}")
    return value


def _strings(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ContractError(f"{name} must be an array")
    return tuple(_string(item, name) for item in value)


def _commit_sha(value: object, name: str) -> str:
    sha = _string(value, name)
    if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", sha):
        raise ContractError(f"{name} must be a full hexadecimal commit SHA")
    return sha


def _sha256(value: object, name: str) -> str:
    digest = _string(value, name)
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ContractError(f"{name} must be a lowercase SHA-256 digest")
    return digest


def _schema(value: JsonObject, name: str) -> None:
    version = _integer(value.get("schema_version"), f"{name}.schema_version")
    if version != SCHEMA_VERSION:
        raise ContractError(f"unsupported {name} schema_version: {version}")


@dataclass(frozen=True)
class RepositorySnapshot:
    repository_id: str
    branch: str
    base_sha: str | None
    head_sha: str
    manifest_digest: str
    worktree_digest: str
    source_artifact_refs: tuple[str, ...] = ()
    includes_dirty_and_untracked: bool = False

    def to_dict(self) -> JsonObject:
        return {
            "schema_version": SCHEMA_VERSION,
            "repository_id": self.repository_id,
            "branch": self.branch,
            "base_sha": self.base_sha,
            "head_sha": self.head_sha,
            "manifest_digest": self.manifest_digest,
            "worktree_digest": self.worktree_digest,
            "source_artifact_refs": list(self.source_artifact_refs),
            "includes_dirty_and_untracked": self.includes_dirty_and_untracked,
        }

    @classmethod
    def from_dict(cls, data: object) -> Self:
        obj = _object(data, "repository_snapshot")
        _schema(obj, "repository_snapshot")
        base_sha = obj.get("base_sha")
        if base_sha is not None:
            base_sha = _commit_sha(base_sha, "repository_snapshot.base_sha")
        dirty = obj.get("includes_dirty_and_untracked", False)
        if not isinstance(dirty, bool):
            raise ContractError(
                "repository_snapshot.includes_dirty_and_untracked must be boolean"
            )
        return cls(
            repository_id=_string(
                obj.get("repository_id"), "repository_snapshot.repository_id"
            ),
            branch=_string(obj.get("branch"), "repository_snapshot.branch"),
            base_sha=base_sha,
            head_sha=_commit_sha(obj.get("head_sha"), "repository_snapshot.head_sha"),
            manifest_digest=_sha256(
                obj.get("manifest_digest"), "repository_snapshot.manifest_digest"
            ),
            worktree_digest=_sha256(
                obj.get("worktree_digest"), "repository_snapshot.worktree_digest"
            ),
            source_artifact_refs=_strings(
                obj.get("source_artifact_refs", []),
                "repository_snapshot.source_artifact_refs",
            ),
            includes_dirty_and_untracked=dirty,
        )


@dataclass(frozen=True)
class Artifact:
    artifact_id: str
    media_type: str
    data: bytes
    scope: str = "operation"
    encoding: str = "base64"

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()

    def __post_init__(self) -> None:
        if self.encoding != "base64":
            raise ContractError("artifact encoding must be 'base64'")

    def to_dict(self) -> JsonObject:
        return {
            "schema_version": SCHEMA_VERSION,
            "artifact_id": self.artifact_id,
            "media_type": self.media_type,
            "encoding": self.encoding,
            "data": base64.b64encode(self.data).decode("ascii"),
            "sha256": self.sha256,
            "scope": self.scope,
        }

    @classmethod
    def from_dict(cls, data: object) -> Self:
        obj = _object(data, "artifact")
        _schema(obj, "artifact")
        if obj.get("encoding") != "base64":
            raise ContractError("artifact.encoding must be 'base64'")
        try:
            raw = base64.b64decode(
                _string(obj.get("data"), "artifact.data", empty=True), validate=True
            )
        except (ValueError, binascii.Error) as exc:
            raise ContractError("artifact.data is not valid base64") from exc
        digest = _string(obj.get("sha256"), "artifact.sha256")
        if hashlib.sha256(raw).hexdigest() != digest:
            raise ContractError("artifact.sha256 does not match artifact.data")
        return cls(
            artifact_id=_string(obj.get("artifact_id"), "artifact.artifact_id"),
            media_type=_string(obj.get("media_type"), "artifact.media_type"),
            data=raw,
            scope=_string(obj.get("scope", "operation"), "artifact.scope"),
        )


@dataclass(frozen=True)
class LocalAction:
    action_id: str
    capability: str
    capability_version: str
    arguments: JsonObject
    repository_id: str
    preconditions: JsonObject = field(default_factory=dict)
    budget: JsonObject = field(default_factory=dict)

    def to_dict(self) -> JsonObject:
        return {
            "schema_version": SCHEMA_VERSION,
            "action_id": self.action_id,
            "capability": self.capability,
            "capability_version": self.capability_version,
            "arguments": self.arguments,
            "repository_id": self.repository_id,
            "preconditions": self.preconditions,
            "budget": self.budget,
        }

    @classmethod
    def from_dict(cls, data: object) -> Self:
        obj = _object(data, "local_action")
        _schema(obj, "local_action")
        return cls(
            action_id=_string(obj.get("action_id"), "local_action.action_id"),
            capability=_string(obj.get("capability"), "local_action.capability"),
            capability_version=_string(
                obj.get("capability_version"), "local_action.capability_version"
            ),
            arguments=_object(obj.get("arguments", {}), "local_action.arguments"),
            repository_id=_string(
                obj.get("repository_id"), "local_action.repository_id"
            ),
            preconditions=_object(
                obj.get("preconditions", {}), "local_action.preconditions"
            ),
            budget=_object(obj.get("budget", {}), "local_action.budget"),
        )


@dataclass(frozen=True)
class ActionReceipt:
    action_id: str
    payload_digest: str
    status: str
    observed_revisions: JsonObject = field(default_factory=dict)
    exit_code: int | None = None
    signal: str | None = None
    stdout_ref: str | None = None
    stderr_ref: str | None = None
    changes: tuple[str, ...] = ()
    artifact_refs: tuple[str, ...] = ()

    def to_dict(self) -> JsonObject:
        return {
            "schema_version": SCHEMA_VERSION,
            "action_id": self.action_id,
            "payload_digest": self.payload_digest,
            "status": self.status,
            "observed_revisions": self.observed_revisions,
            "exit_code": self.exit_code,
            "signal": self.signal,
            "stdout_ref": self.stdout_ref,
            "stderr_ref": self.stderr_ref,
            "changes": list(self.changes),
            "artifact_refs": list(self.artifact_refs),
        }

    @classmethod
    def from_dict(cls, data: object) -> Self:
        obj = _object(data, "action_receipt")
        _schema(obj, "action_receipt")
        exit_code = obj.get("exit_code")
        if exit_code is not None:
            exit_code = _integer(exit_code, "action_receipt.exit_code")

        def optional_string(name: str) -> str | None:
            value = obj.get(name)
            return (
                _string(value, f"action_receipt.{name}") if value is not None else None
            )

        return cls(
            action_id=_string(obj.get("action_id"), "action_receipt.action_id"),
            payload_digest=_string(
                obj.get("payload_digest"), "action_receipt.payload_digest"
            ),
            status=_string(obj.get("status"), "action_receipt.status"),
            observed_revisions=_object(
                obj.get("observed_revisions", {}), "action_receipt.observed_revisions"
            ),
            exit_code=exit_code,
            signal=optional_string("signal"),
            stdout_ref=optional_string("stdout_ref"),
            stderr_ref=optional_string("stderr_ref"),
            changes=_strings(obj.get("changes", []), "action_receipt.changes"),
            artifact_refs=_strings(
                obj.get("artifact_refs", []), "action_receipt.artifact_refs"
            ),
        )


@dataclass(frozen=True)
class UserQuestion:
    question_id: str
    text: str
    answer_shape: JsonObject
    options: tuple[JsonObject, ...] = ()
    context: JsonObject = field(default_factory=dict)

    def to_dict(self) -> JsonObject:
        return {
            "schema_version": SCHEMA_VERSION,
            "question_id": self.question_id,
            "text": self.text,
            "answer_shape": self.answer_shape,
            "options": list(self.options),
            "context": self.context,
        }

    @classmethod
    def from_dict(cls, data: object) -> Self:
        obj = _object(data, "user_question")
        _schema(obj, "user_question")
        options = obj.get("options", [])
        if not isinstance(options, list):
            raise ContractError("user_question.options must be an array")
        return cls(
            _string(obj.get("question_id"), "user_question.question_id"),
            _string(obj.get("text"), "user_question.text"),
            _object(obj.get("answer_shape"), "user_question.answer_shape"),
            tuple(_object(item, "user_question.options[]") for item in options),
            _object(obj.get("context", {}), "user_question.context"),
        )


@dataclass(frozen=True)
class UserAnswer:
    question_id: str
    answer: JsonObject
    decision: JsonObject = field(default_factory=dict)

    def to_dict(self) -> JsonObject:
        return {
            "schema_version": SCHEMA_VERSION,
            "question_id": self.question_id,
            "answer": self.answer,
            "decision": self.decision,
        }

    @classmethod
    def from_dict(cls, data: object) -> Self:
        obj = _object(data, "user_answer")
        _schema(obj, "user_answer")
        return cls(
            _string(obj.get("question_id"), "user_answer.question_id"),
            _object(obj.get("answer"), "user_answer.answer"),
            _object(obj.get("decision", {}), "user_answer.decision"),
        )


@dataclass(frozen=True)
class OperationRequest:
    request_id: str
    operation_kind: str
    inputs: JsonObject = field(default_factory=dict)
    repository: RepositorySnapshot | None = None
    artifacts: tuple[Artifact, ...] = ()
    capabilities: tuple[JsonObject, ...] = ()
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> JsonObject:
        return {
            "schema_version": self.schema_version,
            "request_id": self.request_id,
            "operation_kind": self.operation_kind,
            "inputs": self.inputs,
            "repository": self.repository.to_dict() if self.repository else None,
            "artifacts": [item.to_dict() for item in self.artifacts],
            "capabilities": list(self.capabilities),
        }

    @classmethod
    def from_dict(cls, data: object) -> Self:
        obj = _object(data, "operation_request")
        _schema(obj, "operation_request")
        artifacts = obj.get("artifacts", [])
        capabilities = obj.get("capabilities", [])
        if not isinstance(artifacts, list) or not isinstance(capabilities, list):
            raise ContractError(
                "operation_request artifacts and capabilities must be arrays"
            )
        repository = obj.get("repository")
        return cls(
            request_id=_string(obj.get("request_id"), "operation_request.request_id"),
            operation_kind=_string(
                obj.get("operation_kind"), "operation_request.operation_kind"
            ),
            inputs=_object(obj.get("inputs", {}), "operation_request.inputs"),
            repository=RepositorySnapshot.from_dict(repository)
            if repository is not None
            else None,
            artifacts=tuple(Artifact.from_dict(item) for item in artifacts),
            capabilities=tuple(
                _object(item, "operation_request.capabilities[]")
                for item in capabilities
            ),
            schema_version=SCHEMA_VERSION,
        )


@dataclass(frozen=True)
class OperationReference:
    operation_id: str
    expected_revision: int | None = None

    def to_dict(self) -> JsonObject:
        return {
            "schema_version": SCHEMA_VERSION,
            "operation_id": self.operation_id,
            "expected_revision": self.expected_revision,
        }

    @classmethod
    def from_dict(cls, data: object) -> Self:
        obj = _object(data, "operation_reference")
        _schema(obj, "operation_reference")
        revision = obj.get("expected_revision")
        return cls(
            _string(obj.get("operation_id"), "operation_reference.operation_id"),
            _integer(revision, "operation_reference.expected_revision")
            if revision is not None
            else None,
        )


@dataclass(frozen=True)
class OperationInput:
    operation_id: str
    expected_revision: int
    input_id: str
    action_receipts: tuple[ActionReceipt, ...] = ()
    user_answers: tuple[UserAnswer, ...] = ()
    evidence_additions: tuple[str, ...] = ()
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> JsonObject:
        return {
            "schema_version": self.schema_version,
            "operation_id": self.operation_id,
            "expected_revision": self.expected_revision,
            "input_id": self.input_id,
            "action_receipts": [item.to_dict() for item in self.action_receipts],
            "user_answers": [item.to_dict() for item in self.user_answers],
            "evidence_additions": list(self.evidence_additions),
        }

    @classmethod
    def from_dict(cls, data: object) -> Self:
        obj = _object(data, "operation_input")
        _schema(obj, "operation_input")
        receipts, answers = obj.get("action_receipts", []), obj.get("user_answers", [])
        if not isinstance(receipts, list) or not isinstance(answers, list):
            raise ContractError("operation_input receipts and answers must be arrays")
        return cls(
            _string(obj.get("operation_id"), "operation_input.operation_id"),
            _integer(obj.get("expected_revision"), "operation_input.expected_revision"),
            _string(obj.get("input_id"), "operation_input.input_id"),
            tuple(ActionReceipt.from_dict(item) for item in receipts),
            tuple(UserAnswer.from_dict(item) for item in answers),
            _strings(
                obj.get("evidence_additions", []), "operation_input.evidence_additions"
            ),
        )


@dataclass(frozen=True)
class ServiceError:
    category: str
    message: str
    retryable: bool
    correlation_id: str

    def to_dict(self) -> JsonObject:
        return {
            "schema_version": SCHEMA_VERSION,
            "category": self.category,
            "message": self.message,
            "retryable": self.retryable,
            "correlation_id": self.correlation_id,
        }

    @classmethod
    def from_dict(cls, data: object) -> Self:
        obj = _object(data, "service_error")
        _schema(obj, "service_error")
        retryable = obj.get("retryable")
        if not isinstance(retryable, bool):
            raise ContractError("service_error.retryable must be boolean")
        return cls(
            _string(obj.get("category"), "service_error.category"),
            _string(obj.get("message"), "service_error.message"),
            retryable,
            _string(obj.get("correlation_id"), "service_error.correlation_id"),
        )


@dataclass(frozen=True)
class OperationUpdate:
    operation_id: str
    revision: int
    status: OperationStatus
    progress: tuple[JsonObject, ...] = ()
    pending_actions: tuple[LocalAction, ...] = ()
    pending_questions: tuple[UserQuestion, ...] = ()
    output_artifacts: tuple[Artifact, ...] = ()
    failure: ServiceError | None = None
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> JsonObject:
        return {
            "schema_version": self.schema_version,
            "operation_id": self.operation_id,
            "revision": self.revision,
            "status": self.status,
            "progress": list(self.progress),
            "pending_actions": [item.to_dict() for item in self.pending_actions],
            "pending_questions": [item.to_dict() for item in self.pending_questions],
            "output_artifacts": [item.to_dict() for item in self.output_artifacts],
            "failure": self.failure.to_dict() if self.failure else None,
        }

    @classmethod
    def from_dict(cls, data: object) -> Self:
        obj = _object(data, "operation_update")
        _schema(obj, "operation_update")
        progress, actions, questions, outputs = (
            obj.get(key, [])
            for key in (
                "progress",
                "pending_actions",
                "pending_questions",
                "output_artifacts",
            )
        )
        if not all(
            isinstance(value, list) for value in (progress, actions, questions, outputs)
        ):
            raise ContractError("operation_update collections must be arrays")
        status = _string(obj.get("status"), "operation_update.status")
        if status not in {
            "running",
            "waiting_for_agent",
            "waiting_for_user",
            "succeeded",
            "failed",
            "cancelled",
            "blocked",
            "suspended",
        }:
            raise ContractError(f"unsupported operation status: {status}")
        failure = obj.get("failure")
        return cls(
            _string(obj.get("operation_id"), "operation_update.operation_id"),
            _integer(obj.get("revision"), "operation_update.revision"),
            status,
            tuple(_object(item, "operation_update.progress[]") for item in progress),
            tuple(LocalAction.from_dict(item) for item in actions),
            tuple(UserQuestion.from_dict(item) for item in questions),
            tuple(Artifact.from_dict(item) for item in outputs),
            ServiceError.from_dict(failure) if failure is not None else None,
        )


@dataclass(frozen=True)
class ChatCompletionRequest:
    model: str
    messages: tuple[JsonObject, ...]
    tools: tuple[JsonObject, ...] = ()
    options: JsonObject = field(default_factory=dict)
    request_id: str = ""

    def to_dict(self) -> JsonObject:
        return {
            "schema_version": SCHEMA_VERSION,
            "model": self.model,
            "messages": list(self.messages),
            "tools": list(self.tools),
            "options": self.options,
            "request_id": self.request_id,
        }

    @classmethod
    def from_dict(cls, data: object) -> Self:
        obj = _object(data, "chat_completion_request")
        _schema(obj, "chat_completion_request")
        messages, tools = obj.get("messages"), obj.get("tools", [])
        if not isinstance(messages, list) or not isinstance(tools, list):
            raise ContractError("chat request messages and tools must be arrays")
        return cls(
            _string(obj.get("model"), "chat_completion_request.model"),
            tuple(
                _object(item, "chat_completion_request.messages[]") for item in messages
            ),
            tuple(_object(item, "chat_completion_request.tools[]") for item in tools),
            _object(obj.get("options", {}), "chat_completion_request.options"),
            _string(
                obj.get("request_id", ""),
                "chat_completion_request.request_id",
                empty=True,
            ),
        )


@dataclass(frozen=True)
class ChatCompletionResponse:
    content: str | None
    tool_calls: tuple[JsonObject, ...] = ()
    finish_reason: str | None = None
    usage: JsonObject = field(default_factory=dict)
    provider_data: JsonObject = field(default_factory=dict)

    def to_dict(self) -> JsonObject:
        return {
            "schema_version": SCHEMA_VERSION,
            "content": self.content,
            "tool_calls": list(self.tool_calls),
            "finish_reason": self.finish_reason,
            "usage": self.usage,
            "provider_data": self.provider_data,
        }

    @classmethod
    def from_dict(cls, data: object) -> Self:
        obj = _object(data, "chat_completion_response")
        _schema(obj, "chat_completion_response")
        calls = obj.get("tool_calls", [])
        if not isinstance(calls, list):
            raise ContractError("chat_completion_response.tool_calls must be an array")
        content = obj.get("content")
        finish_reason = obj.get("finish_reason")
        if content is not None and not isinstance(content, str):
            raise ContractError(
                "chat_completion_response.content must be string or null"
            )
        if finish_reason is not None and not isinstance(finish_reason, str):
            raise ContractError(
                "chat_completion_response.finish_reason must be string or null"
            )
        return cls(
            content,
            tuple(
                _object(item, "chat_completion_response.tool_calls[]") for item in calls
            ),
            finish_reason,
            _object(obj.get("usage", {}), "chat_completion_response.usage"),
            _object(
                obj.get("provider_data", {}), "chat_completion_response.provider_data"
            ),
        )


def json_round_trip(value: JsonObject) -> JsonObject:
    """Return a detached JSON copy, rejecting non-serializable values."""
    try:
        return json.loads(json.dumps(value, separators=(",", ":")))
    except (TypeError, ValueError) as exc:
        raise ContractError("payload is not JSON serializable") from exc
