"""Archive completed call checkpoints with file hashes for review and replay."""

from __future__ import annotations

import argparse
import hashlib
import tarfile
from pathlib import Path
from typing import Any

from .common import load_json, write_json


def bundle(run_dir: Path, repo_root: Path) -> dict[str, Any]:
    paths = sorted(
        {
            path
            for dirname in (
                "calls",
                "review-calls",
                "inventory-calls",
                "jev-calls",
                "split-calls",
            )
            for directory in run_dir.rglob(dirname)
            for path in directory.rglob("*.json")
        }
    )
    if not paths:
        raise ValueError("no call checkpoints to archive")
    files = []
    for path in paths:
        record = load_json(path)
        if record.get("status") not in {"completed", "failed"}:
            raise ValueError(f"call still running or incomplete: {path}")
        files.append(
            {
                "path": str(path.resolve().relative_to(repo_root.resolve())),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "status": record["status"],
                "stage": record["stage"],
                "request_sha256": record["request_sha256"],
            }
        )
    archive = run_dir / "call-checkpoints.tar.gz"
    with tarfile.open(archive, "w:gz") as target:
        for path, item in zip(paths, files, strict=True):
            target.add(path, arcname=item["path"])
    manifest = {
        "archive": archive.name,
        "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
        "file_count": len(files),
        "files": files,
    }
    write_json(run_dir / "call-checkpoints.json", manifest)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    args = parser.parse_args()
    result = bundle(args.run_dir, args.repo_root)
    print(
        f"Archived {result['file_count']} call checkpoints; SHA256 "
        f"{result['archive_sha256']}"
    )


if __name__ == "__main__":
    main()
