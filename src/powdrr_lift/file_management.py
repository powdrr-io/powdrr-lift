"""Safe file mutations available to workflow agents."""

from __future__ import annotations

from pathlib import Path


class FileManagementError(RuntimeError):
    """A requested file mutation violated the workflow worktree boundary."""


_OPERATIONS = frozenset({"delete", "move", "rename"})


def manage_worktree_file(
    worktree_root: Path,
    *,
    operation: str,
    file_path: str,
    destination_path: str | None = None,
) -> dict[str, str | None]:
    """Apply one safe file mutation and return normalized relative paths.

    All path components are checked before the mutation.  In particular, ``..``
    is forbidden even when it would resolve back inside the worktree, and every
    existing component must be non-symlinked.  Destinations must not already
    exist; this prevents accidental overwrites and symlink following.
    """
    if operation not in _OPERATIONS:
        raise FileManagementError(
            f"file_management operation must be one of {sorted(_OPERATIONS)}."
        )
    source = _resolve_safe_path(
        worktree_root,
        file_path,
        label="file_path",
        allow_missing=False,
    )
    if not source.is_file():
        raise FileManagementError(
            f"file_management file_path must identify an existing regular file: "
            f"{file_path!r}."
        )

    normalized_destination: str | None = None
    destination: Path | None = None
    if operation in {"move", "rename"}:
        if destination_path is None or not destination_path.strip():
            raise FileManagementError(
                f"file_management {operation} requires destination_path."
            )
        destination = _resolve_safe_path(
            worktree_root,
            destination_path,
            label="destination_path",
            allow_missing=True,
        )
        if destination.exists() or destination.is_symlink():
            raise FileManagementError(
                f"file_management destination_path already exists: "
                f"{destination_path!r}."
            )
        normalized_destination = _relative_path(destination, worktree_root)
        if destination.parent == source:
            raise FileManagementError(
                "file_management destination_path cannot be inside the file "
                "being moved."
            )
    normalized_source = _relative_path(source, worktree_root)
    if operation == "delete":
        source.unlink()
    else:
        assert destination is not None
        source.rename(destination)
    return {
        "operation": operation,
        "file_path": normalized_source,
        "destination_path": normalized_destination,
    }


def _resolve_safe_path(
    worktree_root: Path,
    value: str,
    *,
    label: str,
    allow_missing: bool,
) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise FileManagementError(f"file_management {label} must be non-empty.")
    relative = Path(value)
    if relative.is_absolute():
        raise FileManagementError(
            f"file_management {label} must be relative to the worktree."
        )
    if ".." in relative.parts:
        raise FileManagementError(f"file_management {label} must not contain '..'.")
    if not relative.parts or relative == Path("."):
        raise FileManagementError(f"file_management {label} must identify a file.")

    if worktree_root.is_symlink():
        raise FileManagementError("file_management worktree root cannot be a symlink.")
    root = worktree_root.resolve(strict=True)
    if not root.is_dir():
        raise FileManagementError("file_management worktree root must be a directory.")
    current = root
    parts = relative.parts
    for index, part in enumerate(parts):
        current = current / part
        if current.is_symlink():
            raise FileManagementError(
                f"file_management {label} cannot pass through symlink: {value!r}."
            )
        is_last = index == len(parts) - 1
        if not is_last and (not current.exists() or not current.is_dir()):
            raise FileManagementError(
                f"file_management {label} parent directory does not exist: {value!r}."
            )
        if is_last and not allow_missing and not current.exists():
            raise FileManagementError(
                f"file_management {label} does not exist: {value!r}."
            )
    return current


def _relative_path(path: Path, worktree_root: Path) -> str:
    return path.relative_to(worktree_root.resolve(strict=True)).as_posix()
