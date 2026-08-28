"""Intrinsic validation and application of deferred workflow edits."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

VALIDATE_EDIT_TOOL = "validate_edit"
APPLY_EDIT_TOOL = "apply_edit"


def _groups(value: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    if value.get("action") != "edit":
        raise ValueError("deferred edit must have action 'edit'.")
    if "file_edits" in value:
        groups = value["file_edits"]
    elif "file_path" in value and "edits" in value:
        groups = [{"file_path": value["file_path"], "edits": value["edits"]}]
    else:
        raise ValueError("deferred edit must contain file_path/edits or file_edits.")
    if (
        not isinstance(groups, Sequence)
        or isinstance(groups, (str, bytes))
        or not groups
    ):
        raise ValueError("deferred edit file_edits must be a non-empty array.")
    if not all(isinstance(group, Mapping) for group in groups):
        raise ValueError("deferred edit file_edits entries must be objects.")
    return list(groups)


def _resolve(root: Path, raw_path: object) -> Path:
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise ValueError("deferred edit file_path must be a non-empty string.")
    path = Path(raw_path)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(
            "deferred edit paths must be relative and cannot contain '..'."
        )
    current = root
    for part in path.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(f"deferred edit path contains a symlink: {raw_path}")
    return root / path


def _validate(value: object, root: Path) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, Mapping):
        raise ValueError("deferred edit must be an object.")
    results: list[dict[str, Any]] = []
    for group in _groups(value):
        path = _resolve(root, group.get("file_path"))
        if not path.is_file():
            raise ValueError(f"deferred edit target is not a regular file: {path}")
        edits = group.get("edits")
        if (
            not isinstance(edits, Sequence)
            or isinstance(edits, (str, bytes))
            or not edits
        ):
            raise ValueError(f"deferred edit edits must be non-empty for {path}.")
        line_count = len(path.read_text(encoding="utf-8").splitlines())
        for edit in edits:
            if not isinstance(edit, Mapping):
                raise ValueError("deferred edit entries must be objects.")
            kind = edit.get("kind")
            if kind not in {"add", "remove", "replace"}:
                raise ValueError("deferred edit kind must be add, remove, or replace.")
            start = edit.get("start_line")
            end = edit.get("end_line", start)
            if not isinstance(start, int) or isinstance(start, bool) or start < 1:
                raise ValueError("deferred edit start_line must be a positive integer.")
            if not isinstance(end, int) or isinstance(end, bool) or end < start:
                raise ValueError("deferred edit end_line must be >= start_line.")
            if kind == "add":
                if start > line_count + 1:
                    raise ValueError(f"deferred edit add line is beyond {path}.")
            elif end > line_count:
                raise ValueError(
                    f"deferred edit range ends beyond {line_count} lines in {path}."
                )
            if kind in {"add", "replace"} and not isinstance(edit.get("text"), str):
                raise ValueError("deferred edit add/replace requires string text.")
            if kind == "remove" and "text" in edit:
                raise ValueError("deferred edit remove must not include text.")
        results.append(
            {"file_path": str(path.relative_to(root)), "line_count": line_count}
        )
    return results


def execute_validate_edit_tool(
    parameters: Mapping[str, Any], *, worktree_root: Path
) -> dict[str, Any]:
    try:
        targets = _validate(parameters.get("edit"), worktree_root)
    except (OSError, UnicodeError, ValueError) as exc:
        return {
            "tool": VALIDATE_EDIT_TOOL,
            "returncode": 1,
            "valid": False,
            "error": str(exc),
        }
    return {
        "tool": VALIDATE_EDIT_TOOL,
        "returncode": 0,
        "valid": True,
        "targets": targets,
    }


def execute_apply_edit_tool(
    parameters: Mapping[str, Any], *, worktree_root: Path
) -> dict[str, Any]:
    edit = parameters.get("edit")
    if edit is None:
        return {
            "tool": APPLY_EDIT_TOOL,
            "returncode": 0,
            "applied": False,
            "targets": [],
        }
    targets = _validate(edit, worktree_root)
    assert isinstance(edit, Mapping)
    for group in _groups(edit):
        path = _resolve(worktree_root, group["file_path"])
        lines = path.read_text(encoding="utf-8").splitlines()
        for item in sorted(
            group["edits"], key=lambda entry: entry["start_line"], reverse=True
        ):
            start = item["start_line"] - 1
            end = item.get("end_line", item["start_line"])
            if item["kind"] == "add":
                lines[start:start] = item["text"].splitlines()
            elif item["kind"] == "remove":
                del lines[start:end]
            else:
                lines[start:end] = item["text"].splitlines()
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {
        "tool": APPLY_EDIT_TOOL,
        "returncode": 0,
        "applied": True,
        "targets": targets,
    }
