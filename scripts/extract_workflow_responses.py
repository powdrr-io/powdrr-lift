"""Create a deterministic replay fixture from a live scenario report."""

from __future__ import annotations

import argparse
from pathlib import Path

import yaml

from powdrr_lift.workrr.scenario import (
    WorkflowScenarioError,
    extract_scripted_responses,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        responses = extract_scripted_responses(args.report)
    except WorkflowScenarioError as exc:
        parser.exit(1, f"Could not extract workflow responses: {exc}\n")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(yaml.safe_dump(responses, sort_keys=False), encoding="utf-8")
    print(f"Wrote {len(responses)} scripted responses to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
