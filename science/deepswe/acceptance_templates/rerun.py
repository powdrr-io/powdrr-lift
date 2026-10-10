"""Rerun templates against an unchanged saved inventory for a controlled comparison."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from . import PROMPT_VERSION
from .cli import DEFAULT_MODEL, _client
from .common import HERE, Recorder, digest, generation_input, load_json, write_json
from .generation import generate, load_catalog, save_generation


def reused_inventory(baseline: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
    if (
        baseline.get("status") != "completed"
        or baseline.get("arm") != "templates"
        or baseline.get("task_id") != task["task_id"]
        or baseline.get("input_sha256") != digest(task)
        or baseline.get("inventory_sha256") != digest(baseline["inventory"])
    ):
        raise ValueError("baseline must be a completed matching template generation")
    return baseline["inventory"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-runs", nargs="+", type=Path, required=True)
    parser.add_argument("--tasks", nargs="+", required=True)
    parser.add_argument("--inputs-dir", type=Path, default=HERE / "data/inputs")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--catalog", type=Path, default=HERE / "catalog.json")
    parser.add_argument("--provider", default="deepinfra")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--batch-size", type=int, default=6)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    if args.batch_size < 1:
        parser.error("batch size must be positive")
    if len(set(args.tasks)) != len(args.tasks):
        parser.error("repeated task IDs")
    catalog = load_catalog(args.catalog)
    client = _client(args.provider, args.model, 120, 12288)
    sources = {}
    failures = []
    for tid in args.tasks:
        if Path(tid).name != tid or tid in {".", ".."}:
            raise ValueError("task IDs must be single directory names")
        matches = [
            p / tid / "templates/generation.json"
            for p in args.baseline_runs
            if (p / tid / "templates/generation.json").exists()
        ]
        if len(matches) != 1:
            raise ValueError(f"task must have exactly one baseline: {tid}")
        baseline = load_json(matches[0])
        task = generation_input(args.inputs_dir / f"{tid}.json")
        inv = reused_inventory(baseline, task)
        sources[tid] = {
            "generation_sha256": digest(baseline),
            "inventory_sha256": digest(inv),
            "input_sha256": digest(task),
        }
        recorder = Recorder(
            client,
            args.output_dir / tid / "templates/calls",
            provider=args.provider,
            model=args.model,
            resume=args.resume,
        )
        try:
            result = generate(
                task, inv, catalog, recorder, "templates", args.batch_size
            )
        except RuntimeError as exc:
            result = {
                **baseline,
                "status": "failed",
                "criteria": [],
                "residual_requirements": [
                    r for r in inv["items"] if r["kind"] == "requirement"
                ],
                "calls": recorder.calls,
                "prompt_version": PROMPT_VERSION,
                "catalog_sha256": digest(catalog),
                "error": str(exc),
                "selections": [],
                "decisions": [],
            }
            failures.append({"task_id": tid, "error": str(exc)})
        result["comparison_baseline"] = sources[tid]
        save_generation(args.output_dir / tid / "templates", result)
    write_json(
        args.output_dir / "run.json",
        {
            "provider": args.provider,
            "model": args.model,
            "catalog_sha256": digest(catalog),
            "batch_size": args.batch_size,
            "output_tokens": 12288,
            "repairs": 1,
            "tasks": args.tasks,
            "arms": ["templates"],
            "failures": failures,
            "inventory_sources": sources,
            "prompt_version": PROMPT_VERSION,
            "comparison_scope": "Development rerun of template selection and "
            "binding. Original inventories and references unchanged; "
            "no new inventory or direct-generation calls.",
        },
    )
    print(f"Reran {len(args.tasks)} tasks; {len(failures)} failed")
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())
