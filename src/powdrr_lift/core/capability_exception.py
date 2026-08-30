"""Decision-bound contracts for exceptional tool capabilities."""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from powdrr_lift.core.tool_manifest import ToolEffect


@dataclass(frozen=True, slots=True)
class CapabilityExceptionRequest:
    exception_id: str
    execution_id: str
    tool_name: str
    semantic_action: str
    arguments: dict[str, Any]
    manifest_fingerprint: str
    effects: tuple[ToolEffect, ...]
    reason: str
    created_at: str
    expires_at: str
    max_uses: int = 1

    def binding(self) -> str:
        data = {
            "exception_id": self.exception_id,
            "execution_id": self.execution_id,
            "tool_name": self.tool_name,
            "semantic_action": self.semantic_action,
            "arguments": self.arguments,
            "manifest_fingerprint": self.manifest_fingerprint,
            "effects": [effect.value for effect in self.effects],
            "expires_at": self.expires_at,
            "max_uses": self.max_uses,
        }
        encoded = json.dumps(data, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def to_data(self) -> dict[str, Any]:
        return {
            "exception_id": self.exception_id,
            "execution_id": self.execution_id,
            "tool_name": self.tool_name,
            "semantic_action": self.semantic_action,
            "arguments": self.arguments,
            "manifest_fingerprint": self.manifest_fingerprint,
            "effects": [effect.value for effect in self.effects],
            "reason": self.reason,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "max_uses": self.max_uses,
        }

    def decision_packet(self) -> dict[str, Any]:
        """Return the exact context a human needs to approve this request."""
        return {
            "exception_id": self.exception_id,
            "execution_id": self.execution_id,
            "tool_name": self.tool_name,
            "semantic_action": self.semantic_action,
            "arguments": self.arguments,
            "manifest_fingerprint": self.manifest_fingerprint,
            "effects": [effect.value for effect in self.effects],
            "reason": self.reason,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "max_uses": self.max_uses,
        }

    @classmethod
    def from_data(cls, data: dict[str, Any]) -> CapabilityExceptionRequest:
        return cls(
            data["exception_id"],
            data["execution_id"],
            data["tool_name"],
            data["semantic_action"],
            dict(data["arguments"]),
            data["manifest_fingerprint"],
            tuple(ToolEffect(item) for item in data["effects"]),
            data["reason"],
            data["created_at"],
            data["expires_at"],
            data["max_uses"],
        )


@dataclass(frozen=True, slots=True)
class CapabilityExceptionDecision:
    exception_id: str
    binding: str
    approved: bool
    decided_by: str
    decided_at: str
    token: str | None = None
    uses: int = 0

    def to_data(self) -> dict[str, Any]:
        return {
            "exception_id": self.exception_id,
            "binding": self.binding,
            "approved": self.approved,
            "decided_by": self.decided_by,
            "decided_at": self.decided_at,
            "token": self.token,
            "uses": self.uses,
        }

    @classmethod
    def from_data(cls, data: dict[str, Any]) -> CapabilityExceptionDecision:
        return cls(
            data["exception_id"],
            data["binding"],
            data["approved"],
            data["decided_by"],
            data["decided_at"],
            data.get("token"),
            data["uses"],
        )


class CapabilityExceptionAuthority:
    """Signs exact, single-purpose capability decisions."""

    def __init__(self, secret: bytes) -> None:
        if not secret:
            raise ValueError("Capability exception signing secret must not be empty.")
        self._secret = secret

    def sign(self, request: CapabilityExceptionRequest) -> str:
        return hmac.new(
            self._secret, request.binding().encode(), hashlib.sha256
        ).hexdigest()

    def verify(self, request: CapabilityExceptionRequest, token: str) -> bool:
        return hmac.compare_digest(self.sign(request), token)


def utc_now() -> datetime:
    return datetime.now(UTC)


def is_expired(request: CapabilityExceptionRequest, now: datetime) -> bool:
    return now >= datetime.fromisoformat(request.expires_at)
