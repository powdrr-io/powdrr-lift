from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from powdrr_service.planning.driver import _planned_reference_path_pairs

from powdrr_lift.basedpyright_tools import (
    BASEDPYRIGHT_STRUCTURE_TOOL,
    BASEDPYRIGHT_SYMBOL_TOOL,
    execute_basedpyright_tool,
    find_workspace_document_analysis,
    find_workspace_document_symbols,
    find_workspace_reference_paths,
)
from powdrr_lift.errors import PowdrrExecutionError


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


def test_workspace_document_symbols_batches_planned_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "src").mkdir()
    for name in ("first.py", "second.py"):
        (tmp_path / "src" / name).write_text("class Example: ...\n")
    servers: list[_FakeServer] = []

    def server_factory(root: Path) -> _FakeServer:
        server = _FakeServer(root)
        servers.append(server)
        return server

    monkeypatch.setattr(
        "powdrr_lift.basedpyright_tools._BasedPyrightLanguageServer",
        server_factory,
    )

    result = find_workspace_document_symbols(
        ["src/second.py", "src/first.py", "src/first.py"],
        worktree_root=tmp_path,
    )

    assert list(result) == ["src/first.py", "src/second.py"]
    assert result["src/first.py"][0]["name"] == "Example"
    assert result["src/first.py"][0]["kind"] == "class"
    assert len(servers) == 1
    assert len(servers[0].notifications) == 2


def test_basedpyright_structure_tool_rejects_non_python_paths(tmp_path: Path) -> None:
    path = tmp_path / "example.txt"
    path.write_text("not Python")

    with pytest.raises(PowdrrExecutionError, match="Python files only"):
        execute_basedpyright_tool(
            BASEDPYRIGHT_STRUCTURE_TOOL,
            {"path": "example.txt"},
            worktree_root=tmp_path,
        )


def test_basedpyright_reference_search_maps_only_planned_consumer_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    producer = tmp_path / "src" / "catalog.py"
    consumer = tmp_path / "src" / "dashboard.py"
    unrelated = tmp_path / "src" / "unrelated.py"
    producer.parent.mkdir()
    producer.write_text("def lookup(): ...\n")
    consumer.write_text("from catalog import lookup\n")
    unrelated.write_text("def other(): ...\n")

    class ReferenceServer(_FakeServer):
        def request(self, method: str, params: Any) -> Any:
            if method == "textDocument/documentSymbol":
                uri = params["textDocument"]["uri"]
                if uri == producer.as_uri():
                    return [
                        {
                            "name": "lookup",
                            "kind": 12,
                            "range": {
                                "start": {"line": 0, "character": 4},
                                "end": {"line": 0, "character": 10},
                            },
                            "selectionRange": {
                                "start": {"line": 0, "character": 4},
                                "end": {"line": 0, "character": 10},
                            },
                        }
                    ]
                return []
            if method == "textDocument/references":
                return [
                    {"uri": consumer.as_uri(), "range": {}},
                    {"uri": unrelated.as_uri(), "range": {}},
                ]
            raise AssertionError(f"unexpected BasedPyright request: {method}")

    monkeypatch.setattr(
        "powdrr_lift.basedpyright_tools._BasedPyrightLanguageServer",
        ReferenceServer,
    )

    result = find_workspace_reference_paths(
        ["src/catalog.py", "src/dashboard.py"], worktree_root=tmp_path
    )

    assert result == {"src/catalog.py": ("src/dashboard.py",)}


def test_workspace_document_analysis_returns_symbols_and_references_in_one_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    producer = tmp_path / "src" / "catalog.py"
    consumer = tmp_path / "src" / "dashboard.py"
    producer.parent.mkdir()
    producer.write_text("def lookup(): ...\n")
    consumer.write_text("from catalog import lookup\n")

    class AnalysisServer(_FakeServer):
        def __init__(self, root: Path) -> None:
            super().__init__(root)
            self.document_symbol_requests = 0

        def request(self, method: str, params: Any) -> Any:
            if method == "textDocument/documentSymbol":
                self.document_symbol_requests += 1
                uri = params["textDocument"]["uri"]
                if uri == producer.as_uri():
                    return [
                        {
                            "name": "lookup",
                            "kind": 12,
                            "range": {
                                "start": {"line": 0, "character": 4},
                                "end": {"line": 0, "character": 10},
                            },
                            "selectionRange": {
                                "start": {"line": 0, "character": 4},
                                "end": {"line": 0, "character": 10},
                            },
                        }
                    ]
                return []
            if method == "textDocument/references":
                return [{"uri": consumer.as_uri(), "range": {}}]
            raise AssertionError(f"unexpected BasedPyright request: {method}")

    servers: list[AnalysisServer] = []

    def server_factory(root: Path) -> AnalysisServer:
        server = AnalysisServer(root)
        servers.append(server)
        return server

    monkeypatch.setattr(
        "powdrr_lift.basedpyright_tools._BasedPyrightLanguageServer",
        server_factory,
    )

    symbols, references = find_workspace_document_analysis(
        ["src/catalog.py", "src/dashboard.py"], worktree_root=tmp_path
    )

    assert symbols["src/catalog.py"][0]["name"] == "lookup"
    assert references == {"src/catalog.py": ("src/dashboard.py",)}
    assert len(servers) == 1
    assert servers[0].document_symbol_requests == 2


def test_reference_candidates_recall_labeled_cross_file_edges_without_decoys(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sources = {
        "src/identity.py": (
            "def normalize_id(value): ...\n"
            "def validate_id(value): ...\n"
        ),
        "src/storage.py": "def save_record(record): ...\ndef load_record(key): ...\n",
        "src/api.py": (
            "from identity import normalize_id\n"
            "from storage import save_record\n\n"
            "def handle_request(request): ...\n"
        ),
        "tests/test_api.py": "from api import handle_request\n",
        "src/unplanned.py": (
            "from identity import normalize_id\nfrom storage import load_record\n"
        ),
    }
    for relative_path, source in sources.items():
        path = tmp_path / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)

    planned_paths = (
        "src/identity.py",
        "src/storage.py",
        "src/api.py",
        "tests/test_api.py",
    )
    symbol_lines = {
        "src/identity.py": (("normalize_id", 0), ("validate_id", 1)),
        "src/storage.py": (("save_record", 0), ("load_record", 1)),
        "src/api.py": (("handle_request", 3),),
        "tests/test_api.py": (),
    }
    references_by_symbol = {
        ("src/identity.py", "normalize_id"): (
            "src/api.py",
            "src/unplanned.py",
        ),
        ("src/storage.py", "save_record"): ("src/api.py",),
        ("src/api.py", "handle_request"): ("tests/test_api.py",),
        ("src/storage.py", "load_record"): ("src/unplanned.py",),
    }

    class LabeledReferenceServer(_FakeServer):
        def request(self, method: str, params: Any) -> Any:
            uri = params["textDocument"]["uri"]
            relative_path = (
                Path(uri.removeprefix("file://")).relative_to(tmp_path).as_posix()
            )
            if method == "textDocument/documentSymbol":
                return [
                    {
                        "name": name,
                        "kind": 12,
                        "range": {
                            "start": {"line": line, "character": 4},
                            "end": {"line": line, "character": 20},
                        },
                        "selectionRange": {
                            "start": {"line": line, "character": 4},
                            "end": {"line": line, "character": 20},
                        },
                    }
                    for name, line in symbol_lines[relative_path]
                ]
            if method == "textDocument/references":
                position = params["position"]
                symbol = next(
                    (
                        name
                        for name, line in symbol_lines[relative_path]
                        if line == position["line"]
                    ),
                    None,
                )
                targets = (
                    ()
                    if symbol is None
                    else references_by_symbol.get((relative_path, symbol), ())
                )
                return [
                    {
                        "uri": (tmp_path / target).as_uri(),
                        "range": {},
                    }
                    for target in targets
                ]
            raise AssertionError(f"unexpected BasedPyright request: {method}")

    monkeypatch.setattr(
        "powdrr_lift.basedpyright_tools._BasedPyrightLanguageServer",
        LabeledReferenceServer,
    )

    symbols, references = find_workspace_document_analysis(
        list(planned_paths), worktree_root=tmp_path
    )
    candidate_pairs = _planned_reference_path_pairs(planned_paths, references)

    expected_labeled_edges = {
        ("src/identity.py", "src/api.py"),
        ("src/storage.py", "src/api.py"),
        ("src/api.py", "tests/test_api.py"),
    }
    assert candidate_pairs == tuple(sorted(expected_labeled_edges))
    assert "src/unplanned.py" not in symbols
    assert all("unplanned" not in path for pair in candidate_pairs for path in pair)
