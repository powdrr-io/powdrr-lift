"""Typed declarations for tools exposed through the execution kernel."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

TOOL_MANIFEST_SCHEMA_VERSION = "tool-manifest-v1"
_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_-]*$")


class ToolEffect(StrEnum):
    WORKSPACE_READ = "workspace_read"
    WORKSPACE_WRITE = "workspace_write"
    PROCESS_EXECUTION = "process_execution"
    NETWORK_READ = "network_read"
    GIT_MUTATION = "git_mutation"
    GITHUB_MUTATION = "github_mutation"
    EXTERNAL_WRITE = "external_write"
    SECRET_READ = "secret_read"


class IdempotencyKind(StrEnum):
    NONE = "none"
    KEYED = "keyed"
    NATURALLY_IDEMPOTENT = "naturally_idempotent"


@dataclass(frozen=True, slots=True)
class ToolManifestValidationIssue:
    path: str
    message: str


@dataclass(frozen=True, slots=True)
class ToolManifestValidationReport:
    issues: tuple[ToolManifestValidationIssue, ...]

    @property
    def valid(self) -> bool:
        return not self.issues


@dataclass(frozen=True, slots=True)
class ToolManifest:
    tool_name: str
    semantic_actions: tuple[str, ...]
    effects: tuple[ToolEffect, ...]
    scope: str = "worktree"
    sandbox_profile: str = "workspace"
    reversible: bool = False
    idempotency: IdempotencyKind = IdempotencyKind.NONE
    evidence_producers: tuple[str, ...] = ()
    schema_version: str = TOOL_MANIFEST_SCHEMA_VERSION

    def to_data(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "tool_name": self.tool_name,
            "semantic_actions": list(self.semantic_actions),
            "effects": [effect.value for effect in self.effects],
            "scope": self.scope,
            "sandbox_profile": self.sandbox_profile,
            "reversible": self.reversible,
            "idempotency": self.idempotency.value,
            "evidence_producers": list(self.evidence_producers),
        }

    def fingerprint(self) -> str:
        content = json.dumps(self.to_data(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(content.encode("utf-8")).hexdigest()


def build_tool_manifest_validation_report(
    manifest: ToolManifest,
) -> ToolManifestValidationReport:
    issues: list[ToolManifestValidationIssue] = []
    if manifest.schema_version != TOOL_MANIFEST_SCHEMA_VERSION:
        issues.append(
            ToolManifestValidationIssue("schema_version", "unsupported version")
        )
    _check_identifier(issues, "tool_name", manifest.tool_name)
    if not manifest.semantic_actions:
        issues.append(
            ToolManifestValidationIssue("semantic_actions", "must not be empty")
        )
    _check_identifiers(issues, "semantic_actions", manifest.semantic_actions)
    if not manifest.effects:
        issues.append(ToolManifestValidationIssue("effects", "must not be empty"))
    if len(set(manifest.effects)) != len(manifest.effects):
        issues.append(
            ToolManifestValidationIssue("effects", "must not contain duplicates")
        )
    _check_identifier(issues, "scope", manifest.scope)
    _check_identifier(issues, "sandbox_profile", manifest.sandbox_profile)
    _check_identifiers(issues, "evidence_producers", manifest.evidence_producers)
    return ToolManifestValidationReport(tuple(issues))


def validate_tool_manifest(manifest: ToolManifest) -> None:
    report = build_tool_manifest_validation_report(manifest)
    if not report.valid:
        details = "; ".join(f"{item.path}: {item.message}" for item in report.issues)
        raise ValueError(f"Invalid tool manifest: {details}")


def _check_identifier(
    issues: list[ToolManifestValidationIssue], path: str, value: str
) -> None:
    if not _IDENTIFIER.fullmatch(value):
        issues.append(
            ToolManifestValidationIssue(path, "must be a lowercase identifier")
        )


def _check_identifiers(
    issues: list[ToolManifestValidationIssue], path: str, values: tuple[str, ...]
) -> None:
    if len(set(values)) != len(values):
        issues.append(ToolManifestValidationIssue(path, "must not contain duplicates"))
    for index, value in enumerate(values):
        _check_identifier(issues, f"{path}[{index}]", value)
