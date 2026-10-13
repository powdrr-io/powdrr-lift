"""Package experiment checkpoints and verify their integrity offline."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tarfile
from pathlib import Path
from typing import Any

from science.deepswe.acceptance_templates.common import write_json


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    root = args.directory
    secrets = [
        v.encode()
        for k, v in os.environ.items()
        if re.search(r"KEY|TOKEN|PASSWORD|SECRET", k, re.I) and len(v) >= 16
    ]
    records: list[dict[str, Any]] = []
    with tarfile.open(root / "checkpoints.tar.gz", "w:gz") as archive:
        for category in ("calls", "reads", "setup"):
            for path in sorted((root / category).rglob("*")):
                if not path.is_file():
                    continue
                data = path.read_bytes()
                if any(secret in data for secret in secrets):
                    raise ValueError(f"Credential found in {path}; do not publish")
                relative = str(path.relative_to(root))
                archive.add(path, arcname=relative)
                records.append(
                    {
                        "path": relative,
                        "bytes": len(data),
                        "sha256": hashlib.sha256(data).hexdigest(),
                    }
                )
    with tarfile.open(root / "checkpoints.tar.gz") as archive:
        for row in records:
            stream = archive.extractfile(row["path"])
            if (
                stream is None
                or hashlib.sha256(stream.read()).hexdigest() != row["sha256"]
            ):
                raise ValueError("Archive integrity mismatch")
    write_json(
        root / "checkpoints.json",
        {
            "checkpoints": records,
            "credential_scan": "No environment credential values found",
            "integrity_verified": True,
        },
    )
    print(json.dumps({"archived_files": len(records)}))


if __name__ == "__main__":
    main()
