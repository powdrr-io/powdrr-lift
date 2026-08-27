from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from powdrr_lift.builtin_tool_help import builtin_tool_help

BASEDPYRIGHT_SYMBOL_TOOL = "basedpyright-symbol"
BASEDPYRIGHT_STRUCTURE_TOOL = "basedpyright-structure"
BASEDPYRIGHT_TOOLS = frozenset({BASEDPYRIGHT_SYMBOL_TOOL, BASEDPYRIGHT_STRUCTURE_TOOL})
_MAX_RESULTS = 200


def is_basedpyright_tool(tool: str) -> bool:
    return tool in BASEDPYRIGHT_TOOLS


def execute_basedpyright_tool(
    tool: str,
    parameters: dict[str, Any],
    *,
    worktree_root: Path,
) -> dict[str, Any]:
    if parameters.get("help") is True:
        return builtin_tool_help(tool)
    if tool == BASEDPYRIGHT_SYMBOL_TOOL:
        return _find_symbols(parameters, worktree_root=worktree_root)
    if tool == BASEDPYRIGHT_STRUCTURE_TOOL:
        return _discover_structure(parameters, worktree_root=worktree_root)
    raise RuntimeError(f"Unsupported basedpyright tool: {tool!r}")


def _find_symbols(
    parameters: dict[str, Any],
    *,
    worktree_root: Path,
) -> dict[str, Any]:
    query = _required_string(parameters, "query")
    limit = _result_limit(parameters)
    with _BasedPyrightLanguageServer(worktree_root) as server:
        _open_workspace_python_documents(server, worktree_root)
        symbols = server.request("workspace/symbol", {"query": query})
    if not isinstance(symbols, list):
        raise RuntimeError("basedpyright returned an invalid workspace symbol result.")
    return {
        "tool": BASEDPYRIGHT_SYMBOL_TOOL,
        "query": query,
        "results": [
            _symbol_to_data(symbol, worktree_root) for symbol in symbols[:limit]
        ],
    }


def _discover_structure(
    parameters: dict[str, Any],
    *,
    worktree_root: Path,
) -> dict[str, Any]:
    relative_path = _required_string(parameters, "path")
    path = _resolve_worktree_path(relative_path, worktree_root)
    if not path.is_file():
        raise RuntimeError(
            f"basedpyright structure path is not a file: {relative_path}"
        )
    if path.suffix.casefold() != ".py":
        raise RuntimeError(
            "basedpyright structure currently supports Python files only."
        )

    with _BasedPyrightLanguageServer(worktree_root) as server:
        server.notify(
            "textDocument/didOpen",
            {
                "textDocument": {
                    "uri": path.as_uri(),
                    "languageId": "python",
                    "version": 1,
                    "text": path.read_text(encoding="utf-8"),
                }
            },
        )
        symbols = server.request(
            "textDocument/documentSymbol",
            {"textDocument": {"uri": path.as_uri()}},
        )
    if not isinstance(symbols, list):
        raise RuntimeError("basedpyright returned an invalid document symbol result.")
    return {
        "tool": BASEDPYRIGHT_STRUCTURE_TOOL,
        "path": str(path.relative_to(worktree_root)),
        "symbols": [
            _document_symbol_to_data(symbol, worktree_root) for symbol in symbols
        ],
    }


def _required_string(parameters: dict[str, Any], name: str) -> str:
    value = parameters.get(name)
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"{name} must be a non-empty string.")
    return value.strip()


def _open_workspace_python_documents(
    server: _BasedPyrightLanguageServer,
    worktree_root: Path,
) -> None:
    server.notify(
        "workspace/didChangeConfiguration",
        {"settings": {"basedpyright": {"analysis": {"diagnosticMode": "workspace"}}}},
    )
    ignored_parts = {".git", ".venv", "node_modules", ".worktrees"}
    for path in sorted(worktree_root.rglob("*.py")):
        if ignored_parts.intersection(path.parts):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        server.notify(
            "textDocument/didOpen",
            {
                "textDocument": {
                    "uri": path.as_uri(),
                    "languageId": "python",
                    "version": 1,
                    "text": text,
                }
            },
        )


def _result_limit(parameters: dict[str, Any]) -> int:
    value = parameters.get("limit", 50)
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= _MAX_RESULTS
    ):
        raise RuntimeError(f"limit must be an integer from 1 through {_MAX_RESULTS}.")
    return value


def _resolve_worktree_path(relative_path: str, worktree_root: Path) -> Path:
    root = worktree_root.resolve()
    path = (root / relative_path).resolve()
    if path != root and root not in path.parents:
        raise RuntimeError(f"Path escapes the worktree: {relative_path}")
    return path


class _BasedPyrightLanguageServer:
    def __init__(self, worktree_root: Path) -> None:
        command = shutil.which("basedpyright-langserver")
        if command is None:
            raise RuntimeError(
                "basedpyright-langserver is not installed or not on PATH. "
                "Install basedpyright to use basedpyright tools."
            )
        self.process = subprocess.Popen(
            [command, "--stdio"],
            cwd=worktree_root,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=os.environ.copy(),
        )
        self.worktree_root = worktree_root.resolve()
        self._next_id = 1

    def __enter__(self) -> _BasedPyrightLanguageServer:
        self.request(
            "initialize",
            {
                "processId": os.getpid(),
                "rootUri": self.worktree_root.as_uri(),
                "capabilities": {
                    "textDocument": {
                        "documentSymbol": {
                            "hierarchicalDocumentSymbolSupport": True,
                        }
                    }
                },
                "workspaceFolders": [
                    {
                        "uri": self.worktree_root.as_uri(),
                        "name": self.worktree_root.name,
                    }
                ],
            },
        )
        self.notify("initialized", {})
        return self

    def __exit__(self, *_: object) -> None:
        try:
            self.request("shutdown", None)
            self.notify("exit", None)
        finally:
            if self.process.stdin is not None:
                self.process.stdin.close()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()

    def request(self, method: str, params: Any) -> Any:
        request_id = self._next_id
        self._next_id += 1
        self._write(
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        )
        while True:
            message = self._read()
            if message.get("id") != request_id:
                continue
            if "error" in message:
                raise RuntimeError(f"basedpyright {method} failed: {message['error']}")
            return message.get("result")

    def notify(self, method: str, params: Any) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    def _write(self, message: dict[str, Any]) -> None:
        if self.process.stdin is None:
            raise RuntimeError("basedpyright language server stdin is unavailable.")
        payload = json.dumps(message, separators=(",", ":")).encode("utf-8")
        self.process.stdin.write(
            f"Content-Length: {len(payload)}\r\n\r\n".encode("ascii") + payload
        )
        self.process.stdin.flush()

    def _read(self) -> dict[str, Any]:
        if self.process.stdout is None:
            raise RuntimeError("basedpyright language server stdout is unavailable.")
        content_length: int | None = None
        while True:
            line = self.process.stdout.readline()
            if not line:
                error = (
                    self.process.stderr.read().decode("utf-8", errors="replace")
                    if self.process.stderr
                    else ""
                )
                raise RuntimeError(
                    "basedpyright language server exited before responding"
                    + (f": {error.strip()}" if error.strip() else ".")
                )
            if line in {b"\r\n", b"\n"}:
                break
            name, _, value = line.decode("ascii", errors="replace").partition(":")
            if name.casefold() == "content-length":
                content_length = int(value.strip())
        if content_length is None:
            raise RuntimeError("basedpyright returned an LSP message without a length.")
        payload = self.process.stdout.read(content_length)
        if len(payload) != content_length:
            raise RuntimeError("basedpyright returned a truncated LSP message.")
        message = json.loads(payload)
        if not isinstance(message, dict):
            raise RuntimeError("basedpyright returned an invalid LSP message.")
        return message


def _symbol_to_data(symbol: Any, worktree_root: Path) -> dict[str, Any]:
    if not isinstance(symbol, dict):
        return {"invalid": symbol}
    location = symbol.get("location")
    result = {
        "name": symbol.get("name"),
        "kind": _symbol_kind(symbol.get("kind")),
        "container_name": symbol.get("containerName"),
    }
    if isinstance(location, dict):
        result["path"] = _uri_to_relative_path(location.get("uri"), worktree_root)
        result["range"] = _range_to_data(location.get("range"))
    return result


def _document_symbol_to_data(symbol: Any, worktree_root: Path) -> dict[str, Any]:
    if not isinstance(symbol, dict):
        return {"invalid": symbol}
    location = symbol.get("location")
    symbol_range = symbol.get("range")
    if not isinstance(symbol_range, dict) and isinstance(location, dict):
        symbol_range = location.get("range")
    selection_range = symbol.get("selectionRange")
    if not isinstance(selection_range, dict):
        selection_range = symbol_range
    result: dict[str, Any] = {
        "name": symbol.get("name"),
        "kind": _symbol_kind(symbol.get("kind")),
        "range": _range_to_data(symbol_range),
        "selection_range": _range_to_data(selection_range),
    }
    if isinstance(symbol.get("containerName"), str):
        result["container_name"] = symbol["containerName"]
    children = symbol.get("children")
    if isinstance(children, list) and children:
        result["children"] = [
            _document_symbol_to_data(child, worktree_root) for child in children
        ]
    _ = worktree_root
    return result


_SYMBOL_KINDS = {
    5: "class",
    6: "method",
    8: "field",
    9: "constructor",
    10: "enum",
    11: "interface",
    12: "function",
    13: "variable",
    14: "constant",
    22: "struct",
}


def _symbol_kind(value: Any) -> str | int | None:
    return _SYMBOL_KINDS.get(value, value if isinstance(value, int) else None)


def _range_to_data(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    return {
        "start_line": _position_line(value.get("start")),
        "start_character": _position_character(value.get("start")),
        "end_line": _position_line(value.get("end")),
        "end_character": _position_character(value.get("end")),
    }


def _position_line(value: Any) -> int | None:
    return (
        value.get("line", 0) + 1
        if isinstance(value, dict) and isinstance(value.get("line", 0), int)
        else None
    )


def _position_character(value: Any) -> int | None:
    return (
        value.get("character")
        if isinstance(value, dict) and isinstance(value.get("character"), int)
        else None
    )


def _uri_to_relative_path(uri: Any, worktree_root: Path) -> str | None:
    if not isinstance(uri, str):
        return None
    parsed = urlparse(uri)
    if parsed.scheme != "file":
        return uri
    path = Path(unquote(parsed.path))
    try:
        return str(path.resolve().relative_to(worktree_root))
    except ValueError:
        return str(path)
