"""Constraint-first resolution of tool requests."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from powdrr_lift.core.tool_manifest import ToolEffect
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


@dataclass(frozen=True, slots=True)
class CapabilityResolution:
    kind: CapabilityResolutionKind
    reason: str
    adapter: ToolAdapter | None = None
    arguments: Mapping[str, Any] | None = None
    manifest_fingerprint: str | None = None


class CapabilityBroker:
    def __init__(self, registry: ToolRegistry) -> None:
        self.registry = registry

    def resolve(
        self, context: ToolContext, request: CapabilityRequest
    ) -> CapabilityResolution:
        adapter = self.registry.get(request.tool_name)
        if adapter is None:
            return CapabilityResolution(CapabilityResolutionKind.DENIED, "unknown tool")
        manifest = adapter.manifest
        if request.semantic_action not in manifest.semantic_actions:
            return CapabilityResolution(
                CapabilityResolutionKind.DENIED, "action is not supported by tool"
            )
        if request.semantic_action not in context.semantic_actions:
            return CapabilityResolution(
                CapabilityResolutionKind.DENIED, "action is not allowed in this step"
            )
        missing_effects = set(manifest.effects) - set(context.allowed_effects)
        if missing_effects:
            effects = ", ".join(sorted(effect.value for effect in missing_effects))
            kind = (
                CapabilityResolutionKind.DENIED
                if ToolEffect.SECRET_READ in missing_effects
                else CapabilityResolutionKind.EXCEPTION_REQUIRED
            )
            return CapabilityResolution(
                kind, f"tool requires unavailable effects: {effects}"
            )
        scope_error = _scope_error(context.worktree_root, request.arguments)
        if scope_error:
            return CapabilityResolution(
                CapabilityResolutionKind.CORRECTABLE, scope_error
            )
        validation = adapter.validate(context, request.arguments)
        if not validation.valid:
            return CapabilityResolution(
                CapabilityResolutionKind.CORRECTABLE, "; ".join(validation.errors)
            )
        return CapabilityResolution(
            CapabilityResolutionKind.EXECUTABLE,
            "request satisfies manifest and step constraints",
            adapter,
            request.arguments,
            manifest.fingerprint(),
        )

    def invoke(
        self, context: ToolContext, request: CapabilityRequest
    ) -> ToolResult | CapabilityResolution:
        resolution = self.resolve(context, request)
        if resolution.kind is not CapabilityResolutionKind.EXECUTABLE:
            return resolution
        assert resolution.adapter is not None and resolution.arguments is not None
        return resolution.adapter.execute(context, resolution.arguments)


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
