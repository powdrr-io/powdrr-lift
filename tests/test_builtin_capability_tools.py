from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from powdrr_lift.core.tool_manifest import ToolEffect
from powdrr_lift.execution.builtin_tools import (
    builtin_tool_registry,
    invoke_file_mutation,
    invoke_shell_capability,
)
from powdrr_lift.execution.capabilities import (
    CapabilityBroker,
    CapabilityRequest,
    CapabilityResolutionKind,
)
from powdrr_lift.execution.tools import ToolContext, ToolResult


def test_builtin_repository_inspection_resolves_through_broker(tmp_path: Path) -> None:
    broker = CapabilityBroker(builtin_tool_registry())
    context = ToolContext(
        tmp_path,
        tmp_path,
        frozenset({"inspect_repository"}),
        frozenset({ToolEffect.WORKSPACE_READ}),
    )
    result = broker.invoke(
        context,
        CapabilityRequest("repository", "inspect_repository", {"operation": "status"}),
    )
    assert isinstance(result, ToolResult)
    assert result.output["command"] == ["git", "status", "--short"]


def test_builtin_mutation_requires_the_declared_effect(tmp_path: Path) -> None:
    broker = CapabilityBroker(builtin_tool_registry())
    context = ToolContext(
        tmp_path,
        tmp_path,
        frozenset({"mutate_repository"}),
        frozenset({ToolEffect.WORKSPACE_READ}),
    )
    result = broker.resolve(
        context,
        CapabilityRequest(
            "repository",
            "mutate_repository",
            {"operation": "add", "paths": ["file.txt"]},
        ),
    )
    assert result.kind is CapabilityResolutionKind.EXCEPTION_REQUIRED


def test_shell_capability_requires_argv_and_scopes_cwd(tmp_path: Path) -> None:
    seen: list[Mapping[str, Any]] = []

    def execute(arguments: Mapping[str, Any]) -> dict[str, object]:
        seen.append(arguments)
        return {"returncode": 0}

    result = invoke_shell_capability(
        {"command": ["pytest"], "cwd": "tests"},
        worktree_root=tmp_path,
        executor=execute,
    )
    assert result == {"returncode": 0}
    assert seen[0]["command"] == ["pytest"]


def test_shell_capability_rejects_string_commands_and_escape(tmp_path: Path) -> None:
    def execute(arguments: Mapping[str, Any]) -> object:
        raise AssertionError("invalid process must not execute")

    for arguments in (
        {"command": "pytest ; touch escaped"},
        {"command": ["pytest"], "cwd": "../outside"},
    ):
        try:
            invoke_shell_capability(arguments, worktree_root=tmp_path, executor=execute)
        except ValueError as error:
            assert "not executable" in str(error)
        else:
            raise AssertionError("invalid process should fail")


def test_file_mutation_capability_validates_targets_before_execution(
    tmp_path: Path,
) -> None:
    seen: list[bool] = []

    def execute() -> dict[str, list[str]]:
        seen.append(True)
        return {"changed": ["src/example.py"]}

    result = invoke_file_mutation(
        ("src/example.py",),
        worktree_root=tmp_path,
        executor=execute,
    )

    assert result == {"changed": ["src/example.py"]}
    assert seen == [True]


def test_file_mutation_capability_rejects_absolute_and_escape_targets(
    tmp_path: Path,
) -> None:
    def execute() -> object:
        raise AssertionError("invalid file mutation must not execute")

    for path in ("../outside.py", str(tmp_path / "outside.py")):
        with pytest.raises(ValueError, match="not executable"):
            invoke_file_mutation((path,), worktree_root=tmp_path, executor=execute)
