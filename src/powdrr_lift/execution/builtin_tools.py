"""Capability adapters for existing deterministic intrinsic tools."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from powdrr_lift.core.tool_manifest import ToolEffect, ToolManifest
from powdrr_lift.execution.capabilities import CapabilityBroker, CapabilityRequest
from powdrr_lift.execution.tools import (
    ToolContext,
    ToolRegistry,
    ToolResult,
    ToolValidationReport,
)


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
        from powdrr_lift.intrinsic_git_gh import execute_intrinsic_git_gh_tool

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
        from powdrr_lift.intrinsic_enrich import execute_enrich_tool

        return ToolResult(
            execute_enrich_tool(arguments),
            frozenset({ToolEffect.WORKSPACE_READ}),
        )

    def effects_for(
        self, context: ToolContext, arguments: Mapping[str, Any]
    ) -> frozenset[ToolEffect]:
        return frozenset({ToolEffect.WORKSPACE_READ})


class ShellAdapter:
    """Constrained adapter for argv-based process execution.

    The executor is supplied by the workflow boundary so the capability layer
    owns validation while the presentation layer retains stdout/stderr policy.
    """

    manifest = ToolManifest(
        "process",
        ("run_process",),
        (ToolEffect.PROCESS_EXECUTION,),
        scope="worktree",
        sandbox_profile="workspace-process",
        reversible=False,
    )

    def __init__(self, executor: Callable[[Mapping[str, Any]], Any]) -> None:
        self._executor = executor

    def validate(
        self, context: ToolContext, arguments: Mapping[str, Any]
    ) -> ToolValidationReport:
        command = arguments.get("command")
        if not isinstance(command, (list, tuple)) or not command:
            return ToolValidationReport(("process command must be a non-empty argv",))
        if not all(isinstance(item, str) and item for item in command):
            return ToolValidationReport(
                ("process argv items must be non-empty strings",)
            )
        env = arguments.get("env", {})
        if not isinstance(env, Mapping) or any(
            not isinstance(key, str) or not key or not isinstance(value, str)
            for key, value in env.items()
        ):
            return ToolValidationReport(("process env must map strings to strings",))
        return ToolValidationReport()

    def execute(self, context: ToolContext, arguments: Mapping[str, Any]) -> ToolResult:
        return ToolResult(
            self._executor(arguments),
            frozenset({ToolEffect.PROCESS_EXECUTION}),
        )


def invoke_shell_capability(
    arguments: Mapping[str, Any],
    *,
    worktree_root: Any,
    executor: Callable[[Mapping[str, Any]], Any],
) -> Any:
    """Run one argv process after broker validation and scope checks."""
    context = ToolContext(
        repo_root=worktree_root,
        worktree_root=worktree_root,
        semantic_actions=frozenset({"run_process"}),
        allowed_effects=frozenset(ToolEffect),
    )
    result = CapabilityBroker(ToolRegistry((ShellAdapter(executor),))).invoke(
        context,
        CapabilityRequest("process", "run_process", dict(arguments)),
    )
    if isinstance(result, ToolResult):
        return result.output
    raise ValueError(f"Process capability request was not executable: {result.reason}")


def builtin_tool_registry() -> ToolRegistry:
    """Return built-ins that have been migrated to capability execution."""

    return ToolRegistry((IntrinsicRepositoryAdapter(), EnrichmentAdapter()))


def invoke_intrinsic_capability(
    tool: str,
    arguments: Mapping[str, Any],
    *,
    worktree_root: Any,
) -> Any:
    """Execute legacy git/gh/enrich calls through the typed broker.

    The workflow adapters deliberately grant the effects already represented by
    their existing action contracts. The broker still owns registration,
    operation validation, worktree scoping, and the resulting decision.
    """
    operation = str(arguments.get("operation", ""))
    if tool == "enrich":
        semantic_action = "enrich_test_output"
        request_tool = "enrich"
    elif tool == "git":
        semantic_action = (
            "mutate_repository"
            if operation in {"add", "move", "rename"}
            else "inspect_repository"
        )
        request_tool = "repository"
    elif tool == "gh":
        semantic_action = (
            "mutate_pull_request"
            if operation in {"pr_create", "pr_edit", "pr_review_comment"}
            else "inspect_pull_request"
        )
        request_tool = "repository"
    else:
        raise ValueError(f"Unsupported intrinsic capability: {tool}")
    context = ToolContext(
        repo_root=worktree_root,
        worktree_root=worktree_root,
        semantic_actions=frozenset(
            {
                "inspect_repository",
                "mutate_repository",
                "inspect_pull_request",
                "mutate_pull_request",
                "enrich_test_output",
            }
        ),
        allowed_effects=frozenset(ToolEffect),
    )
    result = CapabilityBroker(builtin_tool_registry()).invoke(
        context,
        CapabilityRequest(request_tool, semantic_action, dict(arguments)),
    )
    if isinstance(result, ToolResult):
        return result.output
    raise ValueError(
        f"Intrinsic capability request was not executable: {result.reason}"
    )
