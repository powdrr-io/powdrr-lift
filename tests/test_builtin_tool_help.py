from __future__ import annotations

import io
from pathlib import Path

import pytest

from powdrr_lift.basedpyright_tools import (
    BASEDPYRIGHT_STRUCTURE_TOOL,
    BASEDPYRIGHT_SYMBOL_TOOL,
    execute_basedpyright_tool,
)
from powdrr_lift.builtin_tool_help import BUILTIN_TOOL_NAMES, builtin_tool_help
from powdrr_lift.intrinsic_git_gh import execute_intrinsic_git_gh_tool
from powdrr_lift.workflow_chat_agent import (
    _execute_fuzzy_match_tool,
    _execute_shell_tool,
)


def test_help_catalog_covers_each_builtin_tool() -> None:
    for tool in BUILTIN_TOOL_NAMES:
        result = builtin_tool_help(tool)
        assert result["tool"] == tool
        assert result["help"] is True
        assert result["when_to_use"]
        assert result["examples"]
        assert result["parameters"]["help"]


@pytest.mark.parametrize("tool", ["git", "gh"])
def test_intrinsic_tools_return_help_without_executing(
    tool: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "subprocess.run", lambda *args, **kwargs: pytest.fail("executed")
    )

    result = execute_intrinsic_git_gh_tool(tool, {"help": True}, worktree_root=tmp_path)

    assert result["tool"] == tool
    assert result["help"] is True


@pytest.mark.parametrize(
    "tool", [BASEDPYRIGHT_SYMBOL_TOOL, BASEDPYRIGHT_STRUCTURE_TOOL]
)
def test_basedpyright_tools_return_help_without_starting_language_server(
    tool: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "powdrr_lift.basedpyright_tools._BasedPyrightLanguageServer",
        lambda *_args, **_kwargs: pytest.fail("started"),
    )

    result = execute_basedpyright_tool(tool, {"help": True}, worktree_root=tmp_path)

    assert result["tool"] == tool
    assert result["help"] is True


def test_shell_and_fuzzy_match_return_help_without_running(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = io.StringIO()
    monkeypatch.setattr(
        "subprocess.run", lambda *args, **kwargs: pytest.fail("executed")
    )

    shell_result = _execute_shell_tool(
        {"help": True},
        worktree_root=tmp_path,
        stdout=output,
        stderr=output,
        verbose=False,
    )
    fuzzy_result = _execute_fuzzy_match_tool({"help": True}, worktree_root=tmp_path)

    assert shell_result["tool"] == "shell"
    assert fuzzy_result["tool"] == "fuzzy-match"
    assert shell_result["help"] is fuzzy_result["help"] is True
