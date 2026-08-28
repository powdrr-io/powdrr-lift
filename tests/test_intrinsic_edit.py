from pathlib import Path

from powdrr_lift.intrinsic_edit import (
    execute_apply_edit_tool,
    execute_validate_edit_tool,
)


def _edit(path: str = "example.py") -> dict[str, object]:
    return {
        "action": "edit",
        "file_path": path,
        "edits": [{"kind": "replace", "start_line": 2, "end_line": 2, "text": "fixed"}],
    }


def test_validate_edit_checks_ranges_without_mutating(tmp_path: Path) -> None:
    path = tmp_path / "example.py"
    path.write_text("one\ntwo\n", encoding="utf-8")

    result = execute_validate_edit_tool({"edit": _edit()}, worktree_root=tmp_path)

    assert result["returncode"] == 0
    assert path.read_text(encoding="utf-8") == "one\ntwo\n"


def test_validate_edit_reports_legacy_shape(tmp_path: Path) -> None:
    path = tmp_path / "example.py"
    path.write_text("one\ntwo\n", encoding="utf-8")

    result = execute_validate_edit_tool(
        {
            "edit": {
                "action": "edit",
                "file_path": "example.py",
                "edits": [{"line_number": 1, "content": "bad"}],
            }
        },
        worktree_root=tmp_path,
    )

    assert result["returncode"] == 1
    assert "kind" in result["error"]


def test_apply_edit_applies_multiple_file_groups(tmp_path: Path) -> None:
    first = tmp_path / "first.py"
    second = tmp_path / "second.py"
    first.write_text("one\ntwo\n", encoding="utf-8")
    second.write_text("old\n", encoding="utf-8")
    edit = {
        "action": "edit",
        "file_edits": [
            {
                "file_path": "first.py",
                "edits": [
                    {"kind": "replace", "start_line": 2, "end_line": 2, "text": "new"}
                ],
            },
            {
                "file_path": "second.py",
                "edits": [
                    {"kind": "replace", "start_line": 1, "end_line": 1, "text": "newer"}
                ],
            },
        ],
    }

    result = execute_apply_edit_tool({"edit": edit}, worktree_root=tmp_path)

    assert result["returncode"] == 0
    assert first.read_text(encoding="utf-8") == "one\nnew\n"
    assert second.read_text(encoding="utf-8") == "newer\n"
