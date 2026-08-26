from __future__ import annotations

from typing import Any

import pytest

from powdrr_lift import mcp_server


def test_build_server_registers_codebase_state_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registered_tools: list[str] = []

    class _FakeServer:
        def tool(self) -> object:
            def _decorator(func: object) -> object:
                registered_tools.append(getattr(func, "__name__", ""))
                return func

            return _decorator

    monkeypatch.setattr(mcp_server, "FastMCP", lambda _: _FakeServer())

    server = mcp_server.build_server()

    assert server is not None
    assert "get_codebase_state" in registered_tools
    assert "synthesize_current_state" in registered_tools
    assert "create_architecture_specification" in registered_tools
    assert "validate_architecture_specification" in registered_tools
    assert "create_implementation_specification" in registered_tools
    assert "validate_implementation_specification" in registered_tools
    assert "create_system_specification" in registered_tools
    assert "create_system_map_specification" in registered_tools
    assert "validate_system_specification" in registered_tools
    assert "create_feature_pr_specification" in registered_tools
    assert "start_planning_feature" in registered_tools
    assert "init_change_log_template_from_plan_diff" in registered_tools
    assert "create_plan_diff_specification" in registered_tools
    assert "create_pr_specification" in registered_tools
    assert "validate_pr_specification" in registered_tools
    assert "search_proposed_prs" in registered_tools
    assert "show_proposed_pr" in registered_tools
    assert "get_invariants" in registered_tools
    assert "get_current_decisions" in registered_tools


def test_every_builtin_tool_supports_progressive_help(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registered_tools: dict[str, Any] = {}

    class _FakeServer:
        def tool(self) -> object:
            def _decorator(func: object) -> object:
                registered_tools[getattr(func, "__name__", "")] = func
                return func

            return _decorator

    monkeypatch.setattr(mcp_server, "FastMCP", lambda _: _FakeServer())

    mcp_server.build_server()

    assert set(registered_tools) == set(mcp_server._TOOL_HELP)
    for tool in registered_tools.values():
        assert tool(help=True)
