"""Tool adapter and registry contracts for the execution kernel."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from powdrr_lift.core.tool_manifest import (
    ToolEffect,
    ToolManifest,
    validate_tool_manifest,
)


@dataclass(frozen=True, slots=True)
class ToolContext:
    repo_root: Path
    worktree_root: Path
    semantic_actions: frozenset[str]
    allowed_effects: frozenset[ToolEffect]
    execution_id: str | None = None
    active_unit_id: str | None = None
    active_persona_id: str | None = None


@dataclass(frozen=True, slots=True)
class ToolValidationReport:
    errors: tuple[str, ...] = ()

    @property
    def valid(self) -> bool:
        return not self.errors


@dataclass(frozen=True, slots=True)
class ToolResult:
    output: Any = None
    observed_effects: frozenset[ToolEffect] = frozenset()
    evidence: tuple[str, ...] = ()
    checkpoint_id: str | None = None


class ToolAdapter(Protocol):
    manifest: ToolManifest

    def validate(
        self, context: ToolContext, arguments: Mapping[str, Any]
    ) -> ToolValidationReport: ...

    def execute(
        self, context: ToolContext, arguments: Mapping[str, Any]
    ) -> ToolResult: ...


class ToolRegistry:
    def __init__(self, adapters: Sequence[ToolAdapter] = ()) -> None:
        self._adapters: dict[str, ToolAdapter] = {}
        self._fingerprints: dict[str, str] = {}
        for adapter in adapters:
            self.register(adapter)

    def register(self, adapter: ToolAdapter) -> None:
        manifest = adapter.manifest
        validate_tool_manifest(manifest)
        if manifest.tool_name in self._adapters:
            raise ValueError(f"Tool is already registered: {manifest.tool_name}")
        self._adapters[manifest.tool_name] = adapter
        self._fingerprints[manifest.tool_name] = manifest.fingerprint()

    def replace(self, adapter: ToolAdapter) -> None:
        """Install the current invocation adapter for a shared runtime."""
        manifest = adapter.manifest
        validate_tool_manifest(manifest)
        self._adapters[manifest.tool_name] = adapter
        self._fingerprints[manifest.tool_name] = manifest.fingerprint()

    def get(self, tool_name: str) -> ToolAdapter | None:
        return self._adapters.get(tool_name)

    def manifest_fingerprint(self, tool_name: str) -> str | None:
        return self._fingerprints.get(tool_name)

    def manifests(self) -> tuple[ToolManifest, ...]:
        return tuple(adapter.manifest for adapter in self._adapters.values())
