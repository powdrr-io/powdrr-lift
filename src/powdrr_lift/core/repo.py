"""Repository-root resolution shared by core modules."""

from __future__ import annotations

import subprocess
from pathlib import Path


def resolve_repo_root(repo_root: str | Path | None = None) -> Path:
    if repo_root is not None:
        return Path(repo_root).resolve()
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=Path.cwd(),
        check=True,
        capture_output=True,
        text=True,
    )
    return Path(result.stdout.strip())
