"""Constraint-first resolution of tool requests."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from powdrr_lift.core.capability_exception import (
    CapabilityExceptionAuthority,
    CapabilityExceptionDecision,
    CapabilityExceptionRequest,
    is_expired,
    utc_now,
)
from powdrr_lift.core.tool_manifest import ToolEffect
from powdrr_lift.errors import PowdrrExecutionError
from powdrr_lift.execution.tools import (
    ToolAdapter,
    ToolContext,
    ToolRegistry,
    ToolResult,
)


class CapabilityResolutionKind(StrEnum):
    EXECUTABLE = "executable"
    CORRECTABLE = "correctable"
    EXCEPTION_REQUIRED = "exception_required"
    DENIED = "denied"


@dataclass(frozen=True, slots=True)
class CapabilityRequest:
    tool_name: str
    semantic_action: str
    arguments: Mapping[str, Any]
    exception_token: str | None = None


@dataclass(frozen=True, slots=True)
class CapabilityResolution:
    kind: CapabilityResolutionKind
    reason: str
    adapter: ToolAdapter | None = None
    arguments: Mapping[str, Any] | None = None
    manifest_fingerprint: str | None = None


@dataclass(frozen=True, slots=True)
class CapabilityDecision:
    """Auditable result of resolving one tool request."""

    tool_name: str
    semantic_action: str
    kind: CapabilityResolutionKind
    reason: str
    manifest_fingerprint: str | None = None
    arguments: Mapping[str, Any] = field(default_factory=dict)
    checkpoint_id: str | None = None

    def to_data(self) -> dict[str, Any]:
        return {
            "tool_name": self.tool_name,
            "semantic_action": self.semantic_action,
            "kind": self.kind.value,
            "reason": self.reason,
            "manifest_fingerprint": self.manifest_fingerprint,
            "arguments": dict(self.arguments),
            "checkpoint_id": self.checkpoint_id,
        }


class CapabilityExceptionStore(Protocol):
    def save(
        self,
        exception: CapabilityExceptionRequest,
        decision: CapabilityExceptionDecision,
    ) -> None: ...

    def load(
        self, exception_id: str
    ) -> tuple[CapabilityExceptionRequest, CapabilityExceptionDecision] | None: ...

    def save_request(self, exception: CapabilityExceptionRequest) -> None: ...

    def load_request(self, exception_id: str) -> CapabilityExceptionRequest | None: ...

    def pending(self) -> tuple[CapabilityExceptionRequest, ...]: ...


class CapabilityCheckpointStore(Protocol):
    def create(
        self,
        workspace_root: str | Path,
        checkpoint_id: str,
        *,
        state_json: str | None = None,
    ) -> Any: ...


class FileCapabilityExceptionStore:
    def __init__(self, workflow_directory: str | Path) -> None:
        self.root = Path(workflow_directory) / "execution" / "exceptions"

    def _write(self, path: Path, payload: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=self.root, delete=False
        ) as temporary:
            temporary.write(json.dumps(payload, indent=2) + "\n")
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_path = Path(temporary.name)
        os.replace(temporary_path, path)

    def save(
        self,
        exception: CapabilityExceptionRequest,
        decision: CapabilityExceptionDecision,
    ) -> None:
        path = self.root / f"{exception.exception_id.replace(':', '_')}.json"
        self._write(
            path,
            {"exception": exception.to_data(), "decision": decision.to_data()},
        )

    def save_request(self, exception: CapabilityExceptionRequest) -> None:
        """Persist a pending request before a human decision exists."""
        path = self.root / f"{exception.exception_id.replace(':', '_')}.request.json"
        self._write(path, {"exception": exception.to_data()})

    def load(
        self, exception_id: str
    ) -> tuple[CapabilityExceptionRequest, CapabilityExceptionDecision] | None:
        path = self.root / f"{exception_id.replace(':', '_')}.json"
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return CapabilityExceptionRequest.from_data(
            data["exception"]
        ), CapabilityExceptionDecision.from_data(data["decision"])

    def load_request(self, exception_id: str) -> CapabilityExceptionRequest | None:
        path = self.root / f"{exception_id.replace(':', '_')}.request.json"
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return CapabilityExceptionRequest.from_data(data["exception"])

    def pending(self) -> tuple[CapabilityExceptionRequest, ...]:
        if not self.root.exists():
            return ()
        requests: list[CapabilityExceptionRequest] = []
        for path in sorted(self.root.glob("*.request.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            request = CapabilityExceptionRequest.from_data(data["exception"])
            if self.load(request.exception_id) is None:
                requests.append(request)
        return tuple(requests)


class CapabilityBroker:
    def __init__(
        self,
        registry: ToolRegistry,
        exception_authority: CapabilityExceptionAuthority | None = None,
        exception_store: CapabilityExceptionStore | None = None,
        checkpoint_store: CapabilityCheckpointStore | None = None,
        state_json_provider: Callable[[ToolContext], str | None] | None = None,
    ) -> None:
        self.registry = registry
        self.exception_authority = exception_authority
        self.exception_store = exception_store
        self.checkpoint_store = checkpoint_store
        self.state_json_provider = state_json_provider
        self._decisions: dict[str, CapabilityExceptionDecision] = {}
        self._exceptions: dict[str, CapabilityExceptionRequest] = {}
        self._decision_log: list[CapabilityDecision] = []

    @property
    def decision_log(self) -> tuple[CapabilityDecision, ...]:
        return tuple(self._decision_log)

    def create_exception_request(
        self,
        context: ToolContext,
        request: CapabilityRequest,
        reason: str,
        *,
        expires_at: str,
        max_uses: int = 1,
    ) -> CapabilityExceptionRequest | None:
        if context.execution_id is None:
            return None
        adapter = self.registry.get(request.tool_name)
        if (
            adapter is None
            or request.semantic_action not in adapter.manifest.semantic_actions
        ):
            return None
        effects_for = getattr(adapter, "effects_for", None)
        effects = (
            tuple(effects_for(context, request.arguments))
            if callable(effects_for)
            else adapter.manifest.effects
        )
        if max_uses < 1:
            raise PowdrrExecutionError(
                "max_uses must be positive",
                error_code="capability_exception_invalid_use_count",
                action_kind=request.semantic_action,
                remediation="Set max_uses to a positive integer.",
            )
        exception = CapabilityExceptionRequest(
            exception_id=f"{context.execution_id}:{request.tool_name}:{request.semantic_action}",
            execution_id=context.execution_id,
            tool_name=request.tool_name,
            semantic_action=request.semantic_action,
            arguments=dict(request.arguments),
            manifest_fingerprint=adapter.manifest.fingerprint(),
            effects=effects,
            reason=reason,
            created_at=utc_now().isoformat(),
            expires_at=expires_at,
            max_uses=max_uses,
        )
        existing = self._exceptions.get(exception.exception_id)
        if existing is None and self.exception_store is not None:
            existing = self.exception_store.load_request(exception.exception_id)
        if existing is not None:
            if existing.arguments == exception.arguments:
                return existing
            exception = replace(
                exception,
                exception_id=(
                    exception.exception_id
                    + ":"
                    + hashlib.sha256(
                        json.dumps(
                            exception.arguments, sort_keys=True, default=str
                        ).encode()
                    ).hexdigest()[:12]
                ),
            )
        self._exceptions[exception.exception_id] = exception
        save_request = getattr(self.exception_store, "save_request", None)
        if callable(save_request):
            save_request(exception)
        return exception

    def decide_exception(
        self,
        exception: CapabilityExceptionRequest,
        *,
        approved: bool,
        decided_by: str,
    ) -> CapabilityExceptionDecision:
        if self.exception_authority is None:
            raise PowdrrExecutionError(
                "An exception authority is required to decide capabilities.",
                error_code="capability_exception_authority_missing",
                action_kind=exception.semantic_action,
                remediation="Configure an exception authority before deciding.",
            )
        if is_expired(exception, utc_now()):
            raise PowdrrExecutionError(
                "Cannot decide an expired capability exception.",
                error_code="capability_exception_expired",
                action_kind=exception.semantic_action,
                remediation="Create a new exception request with a future expiry.",
            )
        existing = self._decisions.get(exception.exception_id)
        if existing is None and self.exception_store is not None:
            stored = self.exception_store.load(exception.exception_id)
            if stored is not None:
                _, existing = stored
                self._decisions[exception.exception_id] = existing
        if existing is not None:
            if existing.approved != approved or existing.decided_by != decided_by:
                raise PowdrrExecutionError(
                    f"capability exception {exception.exception_id!r} "
                    "already has a decision",
                    error_code="capability_exception_decision_conflict",
                    action_kind=exception.semantic_action,
                    remediation="Reuse the existing decision or create a new request.",
                )
            return existing
        token = self.exception_authority.sign(exception) if approved else None
        decision = CapabilityExceptionDecision(
            exception.exception_id,
            exception.binding(),
            approved,
            decided_by,
            utc_now().isoformat(),
            token,
        )
        self._decisions[exception.exception_id] = decision
        self._decision_log.append(
            CapabilityDecision(
                exception.tool_name,
                exception.semantic_action,
                (
                    CapabilityResolutionKind.EXECUTABLE
                    if approved
                    else CapabilityResolutionKind.DENIED
                ),
                "exception approved" if approved else "exception denied",
                exception.manifest_fingerprint,
            )
        )
        if self.exception_store is not None:
            self.exception_store.save(exception, decision)
        return decision

    def resolve(
        self, context: ToolContext, request: CapabilityRequest
    ) -> CapabilityResolution:
        adapter = self.registry.get(request.tool_name)
        if adapter is None:
            return self._record(
                request,
                CapabilityResolution(CapabilityResolutionKind.DENIED, "unknown tool"),
            )
        manifest = adapter.manifest
        effects_for = getattr(adapter, "effects_for", None)
        effective_effects = (
            effects_for(context, request.arguments)
            if callable(effects_for)
            else frozenset(manifest.effects)
        )
        if request.semantic_action not in manifest.semantic_actions:
            return self._record(
                request,
                CapabilityResolution(
                    CapabilityResolutionKind.DENIED, "action is not supported by tool"
                ),
                manifest.fingerprint(),
            )
        if request.semantic_action not in context.semantic_actions:
            return self._record(
                request,
                CapabilityResolution(
                    CapabilityResolutionKind.DENIED,
                    "action is not allowed in this step",
                ),
                manifest.fingerprint(),
            )
        missing_effects = set(effective_effects) - set(context.allowed_effects)
        if missing_effects:
            effects = ", ".join(sorted(effect.value for effect in missing_effects))
            kind = (
                CapabilityResolutionKind.DENIED
                if ToolEffect.SECRET_READ in missing_effects
                else CapabilityResolutionKind.EXCEPTION_REQUIRED
            )
            exception_id = (
                f"{context.execution_id}:{request.tool_name}:{request.semantic_action}"
            )
            decision = self._decisions.get(exception_id)
            exception = self._exceptions.get(exception_id)
            if exception is None and self.exception_store is not None:
                pending = getattr(self.exception_store, "pending", lambda: ())()
                exception = next(
                    (
                        item
                        for item in pending
                        if item.execution_id == context.execution_id
                        and item.tool_name == request.tool_name
                        and item.semantic_action == request.semantic_action
                        and item.arguments == dict(request.arguments)
                    ),
                    None,
                )
                if exception is not None:
                    self._exceptions[exception.exception_id] = exception
                    exception_id = exception.exception_id
            if decision is None and self.exception_store is not None:
                stored = self.exception_store.load(exception_id)
                if stored is not None:
                    stored_exception, stored_decision = stored
                    self._exceptions[exception_id] = stored_exception
                    self._decisions[exception_id] = stored_decision
                    decision = stored_decision
            if (
                kind is CapabilityResolutionKind.EXCEPTION_REQUIRED
                and decision is not None
                and decision.approved
                and decision.token is not None
                and self.exception_authority is not None
                and context.execution_id is not None
            ):
                exception = self._exceptions.get(decision.exception_id)
                if (
                    exception is not None
                    and not is_expired(exception, utc_now())
                    and exception.arguments == dict(request.arguments)
                    and self.exception_authority.verify(
                        exception, request.exception_token or ""
                    )
                    and decision.binding == exception.binding()
                    and decision.uses < exception.max_uses
                ):
                    consumed = CapabilityExceptionDecision(
                        decision.exception_id,
                        decision.binding,
                        decision.approved,
                        decision.decided_by,
                        decision.decided_at,
                        decision.token,
                        decision.uses + 1,
                    )
                    self._decisions[decision.exception_id] = consumed
                    if self.exception_store is not None:
                        self.exception_store.save(exception, consumed)
                    return self._record(
                        request,
                        CapabilityResolution(
                            CapabilityResolutionKind.EXECUTABLE,
                            "approved exception",
                            adapter,
                            request.arguments,
                            manifest.fingerprint(),
                        ),
                        manifest.fingerprint(),
                    )
            return self._record(
                request,
                CapabilityResolution(
                    kind, f"tool requires unavailable effects: {effects}"
                ),
                manifest.fingerprint(),
            )
        scope_error = _scope_error(context.worktree_root, request.arguments)
        if scope_error:
            return self._record(
                request,
                CapabilityResolution(CapabilityResolutionKind.CORRECTABLE, scope_error),
                manifest.fingerprint(),
            )
        validation = adapter.validate(context, request.arguments)
        if not validation.valid:
            return self._record(
                request,
                CapabilityResolution(
                    CapabilityResolutionKind.CORRECTABLE, "; ".join(validation.errors)
                ),
                manifest.fingerprint(),
            )
        return self._record(
            request,
            CapabilityResolution(
                CapabilityResolutionKind.EXECUTABLE,
                "request satisfies manifest and step constraints",
                adapter,
                request.arguments,
                manifest.fingerprint(),
            ),
            manifest.fingerprint(),
        )

    def _record(
        self,
        request: CapabilityRequest,
        resolution: CapabilityResolution,
        fingerprint: str | None = None,
    ) -> CapabilityResolution:
        self._decision_log.append(
            CapabilityDecision(
                request.tool_name,
                request.semantic_action,
                resolution.kind,
                resolution.reason,
                fingerprint or resolution.manifest_fingerprint,
                dict(request.arguments),
            )
        )
        return resolution

    def invoke(
        self, context: ToolContext, request: CapabilityRequest
    ) -> ToolResult | CapabilityResolution:
        resolution = self.resolve(context, request)
        if resolution.kind is not CapabilityResolutionKind.EXECUTABLE:
            return resolution
        assert resolution.adapter is not None and resolution.arguments is not None
        effects_for = getattr(resolution.adapter, "effects_for", None)
        effects = (
            effects_for(context, resolution.arguments)
            if callable(effects_for)
            else resolution.adapter.manifest.effects
        )
        mutating = any(effect is not ToolEffect.WORKSPACE_READ for effect in effects)
        if (
            mutating
            and context.execution_id is not None
            and self.checkpoint_store is None
        ):
            raise PowdrrExecutionError(
                "Mutating capability execution requires a checkpoint store.",
                error_code="capability_checkpoint_required",
                action_kind=request.semantic_action,
                remediation=(
                    "Run the action through an ExecutionRuntime with checkpoints "
                    "enabled."
                ),
            )
        checkpoint_id: str | None = None
        if self.checkpoint_store is not None and mutating:
            encoded = json.dumps(
                {
                    "execution_id": context.execution_id,
                    "tool": request.tool_name,
                    "action": request.semantic_action,
                    "arguments": dict(request.arguments),
                },
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            )
            checkpoint_id = (
                "before-" + hashlib.sha256(encoded.encode()).hexdigest()[:24]
            )
            self.checkpoint_store.create(
                context.worktree_root,
                checkpoint_id,
                state_json=(
                    self.state_json_provider(context)
                    if self.state_json_provider is not None
                    else None
                ),
            )
        try:
            result = resolution.adapter.execute(context, resolution.arguments)
        except Exception as error:
            changed_paths: tuple[str, ...] = ()
            if checkpoint_id is not None and self.checkpoint_store is not None:
                changed_paths_method = getattr(
                    self.checkpoint_store, "changed_paths", None
                )
                if callable(changed_paths_method):
                    load_checkpoint = getattr(self.checkpoint_store, "load", None)
                    if callable(load_checkpoint):
                        checkpoint = load_checkpoint(checkpoint_id)
                        changed_paths = tuple(
                            changed_paths_method(checkpoint, context.worktree_root)
                        )
            partial = bool(changed_paths)
            raise PowdrrExecutionError(
                f"Capability {request.tool_name!r} failed: {error}",
                error_code=(
                    "capability_partial_mutation"
                    if partial
                    else "capability_execution_failed"
                ),
                action_kind=request.semantic_action,
                remediation=(
                    "Restore the associated checkpoint before retrying."
                    if partial
                    else (
                        "Correct the capability request or adapter failure "
                        "before retrying."
                    )
                ),
                details={
                    "checkpoint_id": checkpoint_id or "",
                    "changed_paths": json.dumps(changed_paths),
                },
                cause_error=error,
            ) from error
        if self._decision_log:
            self._decision_log[-1] = replace(
                self._decision_log[-1], checkpoint_id=checkpoint_id
            )
        return (
            replace(result, checkpoint_id=checkpoint_id)
            if checkpoint_id is not None
            else result
        )


def _scope_error(worktree_root: Path, arguments: Mapping[str, Any]) -> str | None:
    root = worktree_root.resolve()
    for key, value in arguments.items():
        if not isinstance(value, str) or key not in {
            "path",
            "file_path",
            "directory",
            "cwd",
        }:
            continue
        candidate = Path(value)
        if candidate.is_absolute():
            return f"{key} must be relative to the active worktree"
        resolved = (root / candidate).resolve()
        try:
            resolved.relative_to(root)
        except ValueError:
            return f"{key} escapes the active worktree"
    return None
