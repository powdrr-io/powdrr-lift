"""Show a unified diff between two recorded LLM exchange JSON files."""

from __future__ import annotations

import argparse
import difflib
import json
import sys
from pathlib import Path
from typing import Any


def _read_exchange(path: Path) -> dict[str, Any]:
    try:
        exchange = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path}: invalid JSON: {exc.msg}") from exc
    if not isinstance(exchange, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return exchange


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("first_file", type=Path)
    parser.add_argument("second_file", type=Path)
    args = parser.parse_args(argv)
    try:
        first = _read_exchange(args.first_file)
        second = _read_exchange(args.second_file)
    except (OSError, ValueError) as exc:
        print(f"llm-diff: {exc}", file=sys.stderr)
        return 2
    diff = difflib.unified_diff(
        json.dumps(first, ensure_ascii=False, indent=2, sort_keys=True).splitlines(),
        json.dumps(second, ensure_ascii=False, indent=2, sort_keys=True).splitlines(),
        fromfile=str(args.first_file),
        tofile=str(args.second_file),
        lineterm="",
    )
    output = "\n".join(diff)
    if output:
        sys.stdout.write(output + "\n")
    else:
        print("No differences.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
