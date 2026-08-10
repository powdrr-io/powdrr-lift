from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from powdrr_lift.basedpyright_tools import (
    BASEDPYRIGHT_STRUCTURE_TOOL,
    BASEDPYRIGHT_SYMBOL_TOOL,
    execute_basedpyright_tool,
)


class _FakeServer:
    responses = {
        "workspace/symbol": [
            {
                "name": "Example",
                "kind": 5,
                "location": {
                    "uri": "file:///tmp/repo/src/example.py",
                    "range": {
                        "start": {"line": 3, "character": 0},
                        "end": {"line": 8, "character": 10},
                    },
                },
            }
        ],
        "textDocument/documentSymbol": [
            {
                "name": "Example",
                "kind": 5,
                "location": {
                    "uri": "file:///tmp/repo/src/example.py",
                    "range": {
                        "start": {"line": 3, "character": 0},
                        "end": {"line": 8, "character": 10},
                    },
                },
                "containerName": "module",
            }
        ],
    }

    def __init__(self, root: Path) -> None:
        self.root = root
        self.notifications: list[tuple[str, Any]] = []

    def __enter__(self) -> _FakeServer:
        return self

    def __exit__(self, *_: object) -> None:
        pass

    def notify(self, method: str, params: Any) -> None:
        self.notifications.append((method, params))

    def request(self, method: str, params: Any) -> Any:
        return self.responses[method]


def test_basedpyright_symbol_tool_returns_workspace_symbols(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "example.py").write_text("class Example: ...\n")
    monkeypatch.setattr(
        "powdrr_lift.basedpyright_tools._BasedPyrightLanguageServer",
        _FakeServer,
    )

    result = execute_basedpyright_tool(
        BASEDPYRIGHT_SYMBOL_TOOL,
        {"query": "Example"},
        worktree_root=tmp_path,
    )

    symbol = result["results"][0]
    assert symbol["name"] == "Example"
    assert symbol["kind"] == "class"
    assert symbol["container_name"] is None
    assert symbol["path"].endswith("src/example.py")
    assert symbol["range"] == {
        "start_line": 4,
        "start_character": 0,
        "end_line": 9,
        "end_character": 10,
    }


def test_basedpyright_structure_tool_returns_file_symbols(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "example.py"
    source.write_text("class Example: ...\n")
    monkeypatch.setattr(
        "powdrr_lift.basedpyright_tools._BasedPyrightLanguageServer",
        _FakeServer,
    )

    result = execute_basedpyright_tool(
        BASEDPYRIGHT_STRUCTURE_TOOL,
        {"path": "example.py"},
        worktree_root=tmp_path,
    )

    assert result["path"] == "example.py"
    assert result["symbols"][0]["name"] == "Example"
    assert result["symbols"][0]["kind"] == "class"
    assert result["symbols"][0]["container_name"] == "module"


def test_basedpyright_structure_tool_rejects_non_python_paths(tmp_path: Path) -> None:
    path = tmp_path / "example.txt"
    path.write_text("not Python")

    with pytest.raises(RuntimeError, match="Python files only"):
        execute_basedpyright_tool(
            BASEDPYRIGHT_STRUCTURE_TOOL,
            {"path": "example.txt"},
            worktree_root=tmp_path,
        )
