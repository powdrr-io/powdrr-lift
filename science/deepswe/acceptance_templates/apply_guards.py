"""Apply new semantic guards to saved bindings without repeating generation calls."""

from __future__ import annotations

import argparse
from copy import deepcopy
from pathlib import Path
from typing import Any

from .cli import DEFAULT_MODEL, _client
from .common import HERE, Recorder, digest, generation_input, load_json, write_json
from .generation import load_catalog, render_bindings, requirements, save_generation
from .guarding import confirm_guards
from .rerun import reused_inventory


def compatible_catalog(
    current: dict[str, Any], previous: dict[str, Any], expected_hash: str
) -> None:
    """Reuse bindings only when prerequisite metadata changed, not rendered rules."""
    keys = ("id", "required_sentence", "optional_sentences", "slots")

    def renderers(value: dict[str, Any]) -> list[dict[str, Any]]:
        return [{key: card[key] for key in keys} for card in value["templates"]]

    if digest(previous) != expected_hash or renderers(previous) != renderers(current):
        raise ValueError("baseline catalog fingerprint or renderers differ")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-run", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--inputs-dir", type=Path, default=HERE / "data/inputs")
    parser.add_argument("--catalog", type=Path, default=HERE / "catalog.json")
    parser.add_argument("--baseline-catalog", type=Path)
    parser.add_argument("--provider", default="deepinfra")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--tasks", nargs="+", required=True)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    catalog = load_catalog(args.catalog)
    client = _client(args.provider, args.model, 120, 12288)
    manifest = deepcopy(load_json(args.baseline_run / "run.json"))
    manifest.pop("status", None)
    manifest.pop("note", None)
    manifest.update(
        tasks=args.tasks,
        failures=[],
        generation_strategy="binding_with_guard_confirmation",
        guard_provider=args.provider,
        guard_model=args.model,
        catalog_sha256=digest(catalog),
    )
    sources = {}
    for tid in args.tasks:
        if Path(tid).name != tid or tid in {".", ".."}:
            raise ValueError("task IDs must be single directory names")
        baseline = load_json(args.baseline_run / tid / "templates/generation.json")
        task = generation_input(args.inputs_dir / f"{tid}.json")
        inv = reused_inventory(baseline, task)
        if baseline["catalog_sha256"] != digest(catalog):
            if args.baseline_catalog is None:
                raise ValueError(
                    "saved bindings require the same catalog or a baseline catalog"
                )
            previous = load_catalog(args.baseline_catalog)

            compatible_catalog(catalog, previous, baseline["catalog_sha256"])
        recorder = Recorder(
            client,
            args.output_dir / tid / "templates/calls",
            provider=args.provider,
            model=args.model,
            resume=args.resume,
        )
        result = deepcopy(baseline)
        result["decisions"] = confirm_guards(
            task, inv, catalog, baseline["decisions"], recorder
        )
        result["criteria"] = render_bindings(result["decisions"], catalog)
        represented = {
            rid for row in result["criteria"] for rid in row["requirement_ids"]
        }
        result["residual_requirements"] = [
            row for row in requirements(inv) if row["id"] not in represented
        ]
        result["calls"] = baseline["calls"] + recorder.calls
        result["guard_baseline_sha256"] = digest(baseline)
        result["generation_strategy"] = "binding_with_guard_confirmation"
        result["catalog_sha256"] = digest(catalog)
        save_generation(args.output_dir / tid / "templates", result)
        sources[tid] = digest(baseline)
    manifest["guard_baseline_sha256"] = sources
    write_json(args.output_dir / "run.json", manifest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
