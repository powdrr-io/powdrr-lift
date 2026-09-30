"""Render normalized production prompts for each step in a workflow definition."""

from __future__ import annotations

import argparse
from pathlib import Path

from powdrr_lift.workrr.definition_prompts import render_skill_prompt_snapshots


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--definition", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--repo-root", type=Path)
    args = parser.parse_args(argv)
    for path in render_skill_prompt_snapshots(
        args.definition, output_dir=args.output_dir, repo_root=args.repo_root
    ):
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
