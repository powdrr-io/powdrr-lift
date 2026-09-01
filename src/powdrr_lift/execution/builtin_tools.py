"""Capability adapters for existing deterministic intrinsic tools."""

from __future__ import annotations

import json
import shlex
from collections.abc import Callable, Mapping, MutableMapping
from pathlib import Path
from typing import Any

from powdrr_lift.core.tool_manifest import ToolEffect, ToolManifest
from powdrr_lift.errors import PowdrrExecutionError
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
        if isinstance(command, str):
            try:
                command_items = shlex.split(command)
            except ValueError:
                return ToolValidationReport(
                    ("process command is not valid shell syntax",)
                )
            if not command_items:
                return ToolValidationReport(("process command must not be empty",))
            if any(item in {";", "&&", "||", "|", ">", ">>"} for item in command_items):
                return ToolValidationReport(
                    ("process command contains shell control syntax",)
                )
        elif isinstance(command, (list, tuple)):
            if not command or not all(
                isinstance(item, str) and item for item in command
            ):
                return ToolValidationReport(
                    ("process argv items must be non-empty strings",)
                )
        else:
            return ToolValidationReport(("process command must be a string or argv",))
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


class FileMutationAdapter:
    manifest = ToolManifest(
        "file-mutation",
        ("edit_files",),
        (ToolEffect.WORKSPACE_WRITE,),
        scope="worktree",
        sandbox_profile="workspace-files",
        reversible=False,
    )

    def __init__(self, executor: Callable[[], Any]) -> None:
        self._executor = executor

    def validate(
        self, context: ToolContext, arguments: Mapping[str, Any]
    ) -> ToolValidationReport:
        paths = arguments.get("paths")
        if not isinstance(paths, (list, tuple)) or not paths:
            return ToolValidationReport(("file mutation requires target paths",))
        root = context.worktree_root.resolve()
        for value in paths:
            if not isinstance(value, str) or not value:
                return ToolValidationReport(("file mutation paths must be strings",))
            if Path(value).is_absolute():
                return ToolValidationReport(("file mutation paths must be relative",))
            try:
                (root / value).resolve().relative_to(root)
            except ValueError:
                return ToolValidationReport(
                    (f"file mutation escapes worktree: {value}",)
                )
        return ToolValidationReport()

    def execute(self, context: ToolContext, arguments: Mapping[str, Any]) -> ToolResult:
        return ToolResult(self._executor(), frozenset({ToolEffect.WORKSPACE_WRITE}))


class BasedPyrightAdapter:
    """Read-only adapter for the bounded BasedPyright discovery tools."""

    def __init__(self, tool_name: str) -> None:
        self.manifest = ToolManifest(
            tool_name,
            ("inspect_code",),
            (ToolEffect.WORKSPACE_READ,),
            scope="worktree",
            sandbox_profile="workspace-read",
            reversible=True,
        )
        self._tool_name = tool_name

    def validate(
        self, context: ToolContext, arguments: Mapping[str, Any]
    ) -> ToolValidationReport:
        if arguments.get("help") is True:
            return ToolValidationReport()
        if self._tool_name == "basedpyright-symbol":
            query = arguments.get("query")
            if not isinstance(query, str) or not query.strip():
                return ToolValidationReport(("basedpyright symbol query is required",))
            limit = arguments.get("limit", 50)
            if (
                isinstance(limit, bool)
                or not isinstance(limit, int)
                or not 1 <= limit <= 200
            ):
                return ToolValidationReport(
                    ("basedpyright symbol limit must be from 1 through 200",)
                )
            return ToolValidationReport()
        path = arguments.get("path")
        if not isinstance(path, str) or not path.strip():
            return ToolValidationReport(("basedpyright structure path is required",))
        if Path(path).is_absolute():
            return ToolValidationReport(
                ("basedpyright structure path must be relative",)
            )
        root = context.worktree_root.resolve()
        target = (root / path).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            return ToolValidationReport(
                ("basedpyright structure path escapes worktree",)
            )
        if target.suffix.casefold() != ".py":
            return ToolValidationReport(
                ("basedpyright structure currently supports Python files only",)
            )
        if not target.is_file():
            return ToolValidationReport(("basedpyright structure path must be a file",))
        return ToolValidationReport()

    def execute(self, context: ToolContext, arguments: Mapping[str, Any]) -> ToolResult:
        from powdrr_lift.basedpyright_tools import execute_basedpyright_tool

        return ToolResult(
            execute_basedpyright_tool(
                self._tool_name, dict(arguments), worktree_root=context.worktree_root
            ),
            frozenset({ToolEffect.WORKSPACE_READ}),
        )


def invoke_basedpyright_capability(
    tool: str,
    arguments: Mapping[str, Any],
    *,
    worktree_root: Path,
    runtime: Any = None,
) -> Any:
    context = ToolContext(
        repo_root=worktree_root,
        worktree_root=worktree_root,
        semantic_actions=frozenset({"inspect_code"}),
        allowed_effects=frozenset({ToolEffect.WORKSPACE_READ}),
    )
    adapter = BasedPyrightAdapter(tool)
    request = CapabilityRequest(tool, "inspect_code", dict(arguments))
    result = (
        runtime.invoke_adapter(adapter, context, request)
        if runtime is not None
        else CapabilityBroker(ToolRegistry((adapter,))).invoke(context, request)
    )
    if isinstance(result, ToolResult):
        return result.output
    raise PowdrrExecutionError(
        f"BasedPyright capability was not executable: {result.reason}",
        error_code="capability_not_executable",
    )


class FuzzyMatchAdapter:
    """Read-only adapter for bounded repository path discovery."""

    manifest = ToolManifest(
        "fuzzy-match",
        ("discover_files",),
        (ToolEffect.WORKSPACE_READ,),
        scope="worktree",
        sandbox_profile="workspace-read",
        reversible=True,
    )

    def __init__(
        self,
        path_cache: MutableMapping[tuple[str, int, int | None], tuple[Path, ...]]
        | None = None,
    ) -> None:
        self._path_cache = path_cache

    def validate(
        self, context: ToolContext, arguments: Mapping[str, Any]
    ) -> ToolValidationReport:
        command = arguments.get("command")
        if not isinstance(command, (str, list, tuple)):
            return ToolValidationReport(
                ("fuzzy-match command must be a string or array",)
            )
        if isinstance(command, str) and not command.strip():
            return ToolValidationReport(("fuzzy-match command must not be empty",))
        if isinstance(command, (list, tuple)) and (
            not command or not all(isinstance(item, str) and item for item in command)
        ):
            return ToolValidationReport(
                ("fuzzy-match command items must be non-empty strings",)
            )
        return ToolValidationReport()

    def execute(self, context: ToolContext, arguments: Mapping[str, Any]) -> ToolResult:
        from powdrr_lift.fuzzy_match import fuzzy_match_json

        command = arguments["command"]
        return ToolResult(
            {
                "tool": "fuzzy-match",
                "command": command,
                "result": json.loads(
                    fuzzy_match_json(
                        command,
                        worktree_root=context.worktree_root,
                        path_cache=self._path_cache,
                    )
                ),
            },
            frozenset({ToolEffect.WORKSPACE_READ}),
        )


def invoke_fuzzy_match_capability(
    arguments: Mapping[str, Any],
    *,
    worktree_root: Path,
    path_cache: MutableMapping[tuple[str, int, int | None], tuple[Path, ...]]
    | None = None,
    runtime: Any = None,
) -> Any:
    context = ToolContext(
        repo_root=worktree_root,
        worktree_root=worktree_root,
        semantic_actions=frozenset({"discover_files"}),
        allowed_effects=frozenset({ToolEffect.WORKSPACE_READ}),
    )
    adapter = FuzzyMatchAdapter(path_cache)
    request = CapabilityRequest("fuzzy-match", "discover_files", dict(arguments))
    result = (
        runtime.invoke_adapter(adapter, context, request)
        if runtime is not None
        else CapabilityBroker(ToolRegistry((adapter,))).invoke(context, request)
    )
    if isinstance(result, ToolResult):
        return result.output
    raise PowdrrExecutionError(
        f"Fuzzy-match capability was not executable: {result.reason}",
        error_code="capability_not_executable",
    )


class RepositoryReadAdapter:
    """Adapter for bounded, read-only repository discovery operations."""

    def __init__(
        self, operation: str, executor: Callable[[Mapping[str, Any]], Any]
    ) -> None:
        self._operation = operation
        self._executor = executor
        self.manifest = ToolManifest(
            f"repository-{operation}",
            (operation,),
            (ToolEffect.WORKSPACE_READ,),
            scope="worktree",
            sandbox_profile="workspace-read",
            reversible=True,
        )

    def validate(
        self, context: ToolContext, arguments: Mapping[str, Any]
    ) -> ToolValidationReport:
        path_value = arguments.get("file_path", arguments.get("directory", "."))
        if not isinstance(path_value, str) or not path_value.strip():
            return ToolValidationReport((f"{self._operation} path must be non-empty",))
        path = Path(path_value)
        if path.is_absolute():
            return ToolValidationReport((f"{self._operation} path must be relative",))
        root = context.worktree_root.resolve()
        try:
            (root / path).resolve().relative_to(root)
        except ValueError:
            return ToolValidationReport((f"{self._operation} path escapes worktree",))
        if self._operation == "read_document":
            start_line = arguments.get("start_line")
            end_line = arguments.get("end_line")
            if (
                isinstance(start_line, bool)
                or not isinstance(start_line, int)
                or isinstance(end_line, bool)
                or not isinstance(end_line, int)
                or start_line < 1
                or end_line < start_line
                or end_line - start_line + 1 > 2000
            ):
                return ToolValidationReport(
                    ("read_document requires a valid range of at most 2000 lines",)
                )
        elif not isinstance(arguments.get("recursive", False), bool):
            return ToolValidationReport(("list_files recursive must be a boolean",))
        return ToolValidationReport()

    def execute(self, context: ToolContext, arguments: Mapping[str, Any]) -> ToolResult:
        return ToolResult(
            self._executor(arguments),
            frozenset({ToolEffect.WORKSPACE_READ}),
        )


class DeferredEditAdapter:
    """Broker adapter for validation/application of deferred edit payloads."""

    def __init__(self, tool_name: str, worktree_root: Path) -> None:
        self._tool_name = tool_name
        self._worktree_root = worktree_root
        self.manifest = ToolManifest(
            tool_name,
            ("validate_edit",) if tool_name == "validate-edit" else ("apply_edit",),
            (ToolEffect.WORKSPACE_READ,)
            if tool_name == "validate-edit"
            else (ToolEffect.WORKSPACE_WRITE,),
            scope="worktree",
            sandbox_profile="workspace-files",
            reversible=tool_name == "validate-edit",
        )

    def validate(
        self, context: ToolContext, arguments: Mapping[str, Any]
    ) -> ToolValidationReport:
        if (
            not isinstance(arguments.get("edit"), Mapping)
            and arguments.get("edit") is not None
        ):
            return ToolValidationReport(("deferred edit must be an object",))
        return ToolValidationReport()

    def execute(self, context: ToolContext, arguments: Mapping[str, Any]) -> ToolResult:
        from powdrr_lift.intrinsic_edit import (
            execute_apply_edit_tool,
            execute_validate_edit_tool,
        )

        result = (
            execute_validate_edit_tool(arguments, worktree_root=self._worktree_root)
            if self._tool_name == "validate-edit"
            else execute_apply_edit_tool(arguments, worktree_root=self._worktree_root)
        )
        effects = (
            frozenset({ToolEffect.WORKSPACE_READ})
            if self._tool_name == "validate-edit"
            else frozenset({ToolEffect.WORKSPACE_WRITE})
        )
        return ToolResult(result, effects)


def invoke_repository_read(
    operation: str,
    arguments: Mapping[str, Any],
    *,
    worktree_root: Path,
    executor: Callable[[Mapping[str, Any]], Any],
    runtime: Any = None,
) -> Any:
    context = ToolContext(
        repo_root=worktree_root,
        worktree_root=worktree_root,
        semantic_actions=frozenset({operation}),
        allowed_effects=frozenset({ToolEffect.WORKSPACE_READ}),
    )
    adapter = RepositoryReadAdapter(operation, executor)
    request = CapabilityRequest(f"repository-{operation}", operation, dict(arguments))
    result = (
        runtime.invoke_adapter(adapter, context, request)
        if runtime is not None
        else CapabilityBroker(ToolRegistry((adapter,))).invoke(context, request)
    )
    if isinstance(result, ToolResult):
        return result.output
    raise PowdrrExecutionError(
        f"Repository read was not executable: {result.reason}",
        error_code="capability_not_executable",
    )


def invoke_deferred_edit_capability(
    tool: str,
    arguments: Mapping[str, Any],
    *,
    worktree_root: Path,
    runtime: Any = None,
) -> Any:
    request_action = "validate_edit" if tool == "validate-edit" else "apply_edit"
    adapter = DeferredEditAdapter(tool, worktree_root)
    context = ToolContext(
        repo_root=worktree_root,
        worktree_root=worktree_root,
        semantic_actions=frozenset({request_action}),
        allowed_effects=frozenset(ToolEffect),
    )
    request = CapabilityRequest(tool, request_action, dict(arguments))
    result = (
        runtime.invoke_adapter(adapter, context, request)
        if runtime is not None
        else CapabilityBroker(ToolRegistry((adapter,))).invoke(context, request)
    )
    if isinstance(result, ToolResult):
        return result.output
    raise PowdrrExecutionError(
        f"Deferred edit capability was not executable: {result.reason}",
        error_code="capability_not_executable",
    )


def invoke_file_mutation(
    paths: tuple[str, ...],
    *,
    worktree_root: Any,
    executor: Callable[[], Any],
    runtime: Any = None,
) -> Any:
    context = ToolContext(
        repo_root=worktree_root,
        worktree_root=worktree_root,
        semantic_actions=frozenset({"edit_files"}),
        allowed_effects=frozenset(ToolEffect),
    )
    adapter = FileMutationAdapter(executor)
    request = CapabilityRequest("file-mutation", "edit_files", {"paths": list(paths)})
    result = (
        runtime.invoke_adapter(adapter, context, request)
        if runtime is not None
        else CapabilityBroker(ToolRegistry((adapter,))).invoke(context, request)
    )
    if isinstance(result, ToolResult):
        return result.output
    raise PowdrrExecutionError(
        f"File mutation was not executable: {result.reason}",
        error_code="capability_not_executable",
    )


def invoke_shell_capability(
    arguments: Mapping[str, Any],
    *,
    worktree_root: Any,
    executor: Callable[[Mapping[str, Any]], Any],
    runtime: Any = None,
) -> Any:
    """Run one argv process after broker validation and scope checks."""
    context = ToolContext(
        repo_root=worktree_root,
        worktree_root=worktree_root,
        semantic_actions=frozenset({"run_process"}),
        allowed_effects=frozenset(ToolEffect),
    )
    adapter = ShellAdapter(executor)
    request = CapabilityRequest("process", "run_process", dict(arguments))
    result = (
        runtime.invoke_adapter(adapter, context, request)
        if runtime is not None
        else CapabilityBroker(ToolRegistry((adapter,))).invoke(context, request)
    )
    if isinstance(result, ToolResult):
        return result.output
    raise PowdrrExecutionError(
        f"Process capability request was not executable: {result.reason}",
        error_code="capability_not_executable",
    )


def builtin_tool_registry() -> ToolRegistry:
    """Return built-ins that have been migrated to capability execution."""

    return ToolRegistry((IntrinsicRepositoryAdapter(), EnrichmentAdapter()))


def invoke_intrinsic_capability(
    tool: str,
    arguments: Mapping[str, Any],
    *,
    worktree_root: Any,
    runtime: Any = None,
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
        raise PowdrrExecutionError(
            f"Unsupported intrinsic capability: {tool}",
            error_code="unsupported_tool",
        )
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
    request = CapabilityRequest(request_tool, semantic_action, dict(arguments))
    adapter = builtin_tool_registry().get(request_tool)
    if adapter is None:
        raise PowdrrExecutionError(
            f"No adapter registered for intrinsic tool {request_tool!r}",
            error_code="intrinsic_tool_unregistered",
            action_kind=semantic_action,
            remediation="Use a registered builtin tool or request tool discovery.",
        )
    result = (
        runtime.invoke_adapter(adapter, context, request)
        if runtime is not None
        else CapabilityBroker(builtin_tool_registry()).invoke(context, request)
    )
    if isinstance(result, ToolResult):
        return result.output
    raise PowdrrExecutionError(
        f"Intrinsic capability request was not executable: {result.reason}",
        error_code="intrinsic_capability_not_executable",
        action_kind=semantic_action,
        remediation="Correct the capability request using the reported reason.",
    )
