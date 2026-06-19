from __future__ import annotations

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
    assert "create_architecture_specification" in registered_tools
    assert "validate_architecture_specification" in registered_tools
    assert "get_invariants" in registered_tools
    assert "get_current_decisions" in registered_tools
