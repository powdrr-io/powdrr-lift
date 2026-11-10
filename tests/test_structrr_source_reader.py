from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path

import pytest
from powdrr_service.structrr import source_reader as module
from powdrr_service.structrr.reference_catalog import compile_reference_catalog
from powdrr_service.structrr.reference_definitions import ReferenceDefinition
from powdrr_service.structrr.source_reader import (
    ReaderLimits,
    SourceReader,
    SourceReadResult,
)
from powdrr_service.structrr.source_references import SourceReferenceIndex


def reader(tmp_path: Path, **limits: int) -> SourceReader:
    definitions = [
        ReferenceDefinition(ref, "invariant", "Exact statement " + ref, "spec:1")
        for ref in ("module", "function", "other")
    ]
    catalog = compile_reference_catalog(
        {"reference_definitions": [d.to_data() for d in definitions]},
        base_commit="a" * 40,
    )
    return SourceReader(tmp_path, catalog, limits=ReaderLimits(**limits))


def test_body_read_inherits_context_and_preserves_snapshot(tmp_path: Path) -> None:
    source = (
        b"# structrr[module]: module\r\n# structrr: function\r\n"
        b"def run():\r\n    return 42\r\n"
    )
    (tmp_path / "a.py").write_bytes(source)
    result = reader(tmp_path).read("a.py", 4, 4).to_data()
    assert result["status"] == "complete"
    assert result["excerpts"][0]["lines"] == ["    return 42\r\n"]
    assert result["excerpts"][0]["line_numbers"] == [4]
    assert [r["ref"] for r in result["references"]] == ["function", "module"]
    assert all("Exact statement" in p["text"] for p in result["context_pages"])
    assert SourceReadResult.from_data(result).to_data() == result
    result["excerpts"][0]["lines"][0] = "forged"
    with pytest.raises(ValueError, match="fingerprint"):
        SourceReadResult.from_data(result)


@pytest.mark.parametrize("path", ["../outside.py", "/etc/a.py", "C:/a.py", "a\\b.py"])
def test_unsafe_paths(tmp_path: Path, path: str) -> None:
    assert reader(tmp_path).read(path).status == "error"


def test_links_and_nonregular_files(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (tmp_path / "outside.py").write_text("secret = 1\n")
    (workspace / "escape.py").symlink_to(tmp_path / "outside.py")
    (workspace / "a.py").write_text("x = 1\n")
    (workspace / "alias.py").symlink_to(workspace / "a.py")
    (workspace / "directory.py").mkdir()
    os.mkfifo(workspace / "pipe.py")
    session = reader(workspace)
    assert session.read("escape.py").status == "error"
    assert session.read("alias.py").status == "complete"
    assert session.read("directory.py").status == "error"
    assert session.read("pipe.py").status == "error"


def test_limits_and_invalid_source_are_explicit(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("def broken(:\n    pass\n")
    data = reader(tmp_path).read("a.py", 2, 2).to_data()
    assert data["status"] == "incomplete"
    assert data["source_complete"] and not data["scope_complete"]
    assert data["excerpts"][0]["lines"] == ["    pass\n"]
    assert reader(tmp_path, max_file_bytes=2).read("a.py").status == "error"
    assert reader(tmp_path, max_source_lines=1).read("a.py", 1, 2).status == "error"
    assert reader(tmp_path, max_source_lines=1).read("a.py").status == "error"
    assert reader(tmp_path).read("a.py", True).status == "error"
    for value in (0, -1, True):
        with pytest.raises(ValueError):
            ReaderLimits(max_context_chars=value)
    limits = ReaderLimits()
    assert ReaderLimits.from_data(limits.to_data()) == limits
    assert limits.digest != ReaderLimits(max_context_chars=1).digest


@pytest.mark.parametrize(
    "source,code",
    [(b"x=\x00", "binary_source"), (b"# coding: nonexistent\nx=1", "encoding_failed")],
)
def test_decode_failures(tmp_path: Path, source: bytes, code: str) -> None:
    (tmp_path / "a.py").write_bytes(source)
    data = reader(tmp_path).read("a.py").to_data()
    assert data["status"] == "error"
    assert data["diagnostics"][0]["code"] == code


def test_empty_and_encoded_sources(tmp_path: Path) -> None:
    (tmp_path / "empty.py").write_bytes(b"")
    (tmp_path / "latin.py").write_bytes(b"# coding: latin-1\nx = '\xe9'\n")
    (tmp_path / "unicode.py").write_text("x = '\u2028'\n")
    session = reader(tmp_path)
    assert session.read("empty.py").to_data()["excerpts"][0]["lines"] == []
    assert session.read("latin.py", 2, 2).to_data()["excerpts"][0]["lines"] == [
        "x = 'é'\n"
    ]
    assert len(session.read("unicode.py").to_data()["excerpts"][0]["lines"]) == 1


def test_search_is_sorted_literal_and_bounded(tmp_path: Path) -> None:
    for name in ("z.py", "a.py"):
        (tmp_path / name).write_text(
            "# structrr: function\ndef run():\n    return '[.*] $(touch nope)'\n"
        )
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "hidden.py").write_text("'[.*]'\n")
    session = reader(tmp_path)
    data = session.search("[.*]").to_data()
    assert data["status"] == "complete"
    assert [e["path"] for e in data["excerpts"]] == ["a.py", "z.py"]
    assert len(data["references"]) == 1
    assert len(data["references"][0]["locations"]) == 2
    assert not (tmp_path / "nope").exists()
    assert reader(tmp_path, max_search_matches=1).search("[.*]").status == "incomplete"
    assert reader(tmp_path, max_search_matches=2).search("[.*]").status == "complete"
    assert session.search("\n").status == "error"


def test_search_skipped_paths_report_incomplete(tmp_path: Path) -> None:
    (tmp_path / "dir").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "dir", target_is_directory=True)
    (tmp_path / "broken.py").write_bytes(b"\x00")
    data = reader(tmp_path).search("x").to_data()
    assert data["status"] == "incomplete"
    assert {d["code"] for d in data["diagnostics"]} == {
        "symlink_directory_skipped",
        "binary_source",
    }


def test_context_pagination_reconstructs_exact_resolution(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("# structrr: function\ndef run(): pass\n")
    session = reader(tmp_path, max_context_chars=17)
    data = session.read("a.py").to_data()
    assert data["status"] == "incomplete"
    assert data["remaining_refs"] == ["function"]
    text = data["context_pages"][0]["text"]
    while data["remaining_refs"]:
        continuation = data["continuations"][0]
        assert "--ref=function" in continuation["command"]
        data = session.context(continuation["ref"], continuation["offset"]).to_data()
        assert sum(len(p["text"]) for p in data["context_pages"]) <= 17
        text += data["context_pages"][0]["text"]
    assert json.loads(text)["entry"]["statement"] == "Exact statement function"
    assert session.context("function", len(text)).status == "complete"
    assert session.context("function", len(text) + 1).status == "error"
    assert session.context("function", -1).status == "error"
    assert session.context("unknown").status == "incomplete"


def test_shared_budget_and_inactive_reference(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text(
        "# structrr[module]: module\n# structrr: function\ndef run(): pass\n"
    )
    session = reader(tmp_path, max_context_chars=17)
    data = session.read("a.py").to_data()
    assert sum(len(p["text"]) for p in data["context_pages"]) == 17
    assert data["remaining_refs"] == ["function", "module"]
    assert data["continuations"][1]["offset"] == 0
    old = replace(
        ReferenceDefinition("old", "decision", "Historical wording", "spec:1"),
        lifecycle="superseded",
        successor="function",
    )
    catalog = compile_reference_catalog(
        {"reference_definitions": [old.to_data()]}, base_commit="a" * 40
    )
    data = SourceReader(tmp_path, catalog).context("old").to_data()
    assert data["status"] == "incomplete"
    resolution = json.loads(data["context_pages"][0]["text"])
    assert resolution["status"] == "inactive_reference"
    assert resolution["entry"]["successor"] == "function"


def test_cache_uses_content_not_mtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "a.py"
    path.write_text("x = 1\n")
    original = module.index_python_source
    calls: list[bytes] = []

    def counted(path: str, data: bytes) -> SourceReferenceIndex:
        calls.append(data)
        return original(path, data)

    monkeypatch.setattr(module, "index_python_source", counted)
    session = reader(tmp_path)
    session.read("a.py")
    session.read("a.py")
    modified = path.stat()
    path.write_text("x = 2\n")
    os.utime(path, ns=(modified.st_atime_ns, modified.st_mtime_ns))
    session.read("a.py")
    assert calls == [b"x = 1\n", b"x = 2\n"]


def test_cli_configuration_and_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("POWDRR_STRUCTRR_WORKSPACE", raising=False)
    assert module.main(["read", "a.py"]) == 2
    assert json.loads(capsys.readouterr().out)["code"] == "invalid_reader_configuration"
    (tmp_path / "a.py").write_text("x = 1\n")
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps(reader(tmp_path).catalog.to_data()))
    monkeypatch.setenv("POWDRR_STRUCTRR_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("POWDRR_STRUCTRR_CATALOG", str(catalog))
    for args in (["read", "a.py"], ["search", "x ="], ["context", "--ref=function"]):
        assert module.main(args) == 0
        assert json.loads(capsys.readouterr().out)["status"] == "complete"
