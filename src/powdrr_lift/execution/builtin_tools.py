"""Capability adapters for existing deterministic intrinsic tools."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from powdrr_lift.core.tool_manifest import ToolEffect, ToolManifest
from powdrr_lift.execution.tools import (
    ToolContext,
    ToolRegistry,
    ToolResult,
    ToolValidationReport,
)
from powdrr_lift.intrinsic_enrich import execute_enrich_tool
from powdrr_lift.intrinsic_git_gh import execute_intrinsic_git_gh_tool


class IntrinsicRepositoryAdapter:
    manifest = ToolManifest(
        "repository",
        (
            "inspect_repository",
            "mutate_repository",
            "inspect_pull_request",
            "mutate_pull_request",
        ),
        (
            ToolEffect.WORKSPACE_READ,
            ToolEffect.GIT_MUTATION,
            ToolEffect.GITHUB_MUTATION,
        ),
        scope="worktree",
    )

    def validate(
        self, context: ToolContext, arguments: Mapping[str, Any]
    ) -> ToolValidationReport:
        operation = arguments.get("operation")
        if not isinstance(operation, str) or not operation:
            return ToolValidationReport(("repository operation is required",))
        supported = {
            "status",
            "add",
            "move",
            "rename",
            "pr_view",
            "pr_diff",
            "pr_checks",
            "pr_comments",
            "pr_create",
            "pr_edit",
            "pr_review_comment",
        }
        if operation not in supported:
            return ToolValidationReport(
                (f"unsupported repository operation: {operation}",)
            )
        return ToolValidationReport()

    def execute(self, context: ToolContext, arguments: Mapping[str, Any]) -> ToolResult:
        operation = str(arguments.get("operation", ""))
        tool = "gh" if operation.startswith("pr_") else "git"
        result = execute_intrinsic_git_gh_tool(
            tool, arguments, worktree_root=context.worktree_root
        )
        mutating = operation in {
            "add",
            "move",
            "rename",
            "pr_create",
            "pr_edit",
            "pr_review_comment",
        }
        effect = (
            ToolEffect.GITHUB_MUTATION
            if tool == "gh" and mutating
            else ToolEffect.GIT_MUTATION
            if tool == "git" and mutating
            else ToolEffect.WORKSPACE_READ
        )
        return ToolResult(result, frozenset({effect}))

    def effects_for(
        self, context: ToolContext, arguments: Mapping[str, Any]
    ) -> frozenset[ToolEffect]:
        operation = str(arguments.get("operation", ""))
        if operation.startswith("pr_"):
            return frozenset(
                {
                    ToolEffect.GITHUB_MUTATION
                    if operation in {"pr_create", "pr_edit", "pr_review_comment"}
                    else ToolEffect.WORKSPACE_READ
                }
            )
        return frozenset(
            {
                ToolEffect.GIT_MUTATION
                if operation in {"add", "move", "rename"}
                else ToolEffect.WORKSPACE_READ
            }
        )


class EnrichmentAdapter:
    manifest = ToolManifest(
        "enrich",
        ("enrich_test_output",),
        (ToolEffect.WORKSPACE_READ,),
        scope="execution",
        sandbox_profile="workspace",
        reversible=True,
        evidence_producers=("pytest",),
    )

    def validate(
        self, context: ToolContext, arguments: Mapping[str, Any]
    ) -> ToolValidationReport:
        if arguments.get("format") != "pytest":
            return ToolValidationReport(("enrich format must be pytest",))
        if not isinstance(arguments.get("tool_output"), Mapping):
            return ToolValidationReport(("enrich tool_output must be an object",))
        return ToolValidationReport()

    def execute(self, context: ToolContext, arguments: Mapping[str, Any]) -> ToolResult:
        return ToolResult(
            execute_enrich_tool(arguments),
            frozenset({ToolEffect.WORKSPACE_READ}),
        )

    def effects_for(
        self, context: ToolContext, arguments: Mapping[str, Any]
    ) -> frozenset[ToolEffect]:
        return frozenset({ToolEffect.WORKSPACE_READ})


def builtin_tool_registry() -> ToolRegistry:
    """Return built-ins that have been migrated to capability execution."""

    return ToolRegistry((IntrinsicRepositoryAdapter(), EnrichmentAdapter()))
