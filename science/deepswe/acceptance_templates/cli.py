"""Collect blinded inputs, generate criteria, review, and score offline experiments."""

from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
import tomllib
from pathlib import Path
from typing import Any

from powdrr_lift.workrr.providers import (
    build_workflow_client,
    resolve_provider_credentials,
)

from .common import (
    HERE,
    Recorder,
    digest,
    generation_input,
    load_json,
    write_json,
)
from .evaluation import aggregate, report_markdown, review, score
from .generation import generate, inventory, load_catalog, requirements, save_generation
from .references import draft_reference

ROOT = HERE.parents[2]
DEVELOPMENT_MANIFEST = ROOT / "docs/plans/acceptance-criteria-template-evidence.json"
DEFAULT_MODEL = "deepseek-ai/DeepSeek-V4-Flash-0731"
DEFAULT_JUDGE_MODEL = "Qwen/Qwen3-Next-80B-A3B-Instruct"
PILOT_TASKS = (
    "cattrs-partial-structuring-recovery",
    "helm-array-merge-strategies",
    "koota-entity-snapshot-rollback",
    "dateutil-rfc5545-timezone-interop",
)


def _repository(task_dir: Path) -> str:
    path = task_dir / "task.toml"
    if not path.exists():
        return ""
    raw = tomllib.loads(path.read_text())
    return (
        str(raw.get("metadata", {}).get("repository_url", ""))
        .rstrip("/")
        .removesuffix(".git")
        .casefold()
    )


def collect(
    tasks_dir: Path,
    output_dir: Path,
    task_ids: list[str],
    *,
    development_manifest: Path = DEVELOPMENT_MANIFEST,
) -> dict[str, Any]:
    """The generation directory contains instructions only; never patch contents."""
    development = load_json(development_manifest)
    excluded = {row["task"] for row in development["tasks"]}
    families = {_repository(tasks_dir / task_id) for task_id in excluded} - {""}
    rows = []
    for task_id in task_ids:
        if Path(task_id).name != task_id or task_id in {".", ".."}:
            raise ValueError("task IDs must be single directory names")
        task_dir = tasks_dir / task_id
        repo = _repository(task_dir)
        if task_id in excluded or (repo and repo in families):
            raise ValueError(f"catalog development task/repository excluded: {task_id}")
        instruction = (task_dir / "instruction.md").read_text(encoding="utf-8")
        write_json(
            output_dir / "inputs" / f"{task_id}.json",
            {
                "task_id": task_id,
                "instruction": instruction,
            },
        )
        artifacts = {}
        for name in (
            "instruction.md",
            "task.toml",
            "solution/solution.patch",
            "tests/test.patch",
        ):
            path = task_dir / name
            if path.exists():
                artifacts[name] = {
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "bytes": path.stat().st_size,
                }
        rows.append({"task_id": task_id, "repository": repo, "artifacts": artifacts})
    manifest = {
        "version": 1,
        "tasks": rows,
        "excluded_development_tasks": sorted(excluded),
        "excluded_repository_families": sorted(families),
        "selection": "Explicit pilot selection across four new repository families; "
        "a development evaluation sample, not an untouched final holdout.",
        "boundary": "inputs/ contains only task_id and original instruction; "
        "patch hashes are provenance, never generation input.",
    }
    write_json(output_dir / "manifest.json", manifest)
    return manifest


def load_reference(
    path: Path, task: dict[str, Any], catalog: dict[str, Any]
) -> dict[str, Any]:
    reference = load_json(path)
    if reference.get("task_id") != task["task_id"]:
        raise ValueError("reference task mismatch")
    if reference.get("instruction_sha256") != digest(task["instruction"]):
        raise ValueError("reference instruction mismatch")
    if reference.get("label_status") not in {
        "agent_authored",
        "model_draft",
        "human_reviewed",
    }:
        raise ValueError("reference label provenance required")
    sources = {row["id"]: row["text"] for row in task["source_spans"]}
    tids = {row["id"] for row in catalog["templates"]}
    seen = set()
    for row in reference["validations"]:
        rid = row["id"]
        if rid in seen or not row.get("behavior") or not row.get("source_ids"):
            raise ValueError("duplicate or incomplete reference validation")
        seen.add(rid)
        if row.get("basis") != "instruction":
            raise ValueError(
                "patch-only expectations cannot enter required-validation scoring"
            )
        if any(sid not in sources for sid in row["source_ids"]):
            raise ValueError("unknown reference source")
        for group in row.get("template_groups", []):
            if not group or any(tid not in tids for tid in group):
                raise ValueError("unknown template group")
    if not seen:
        raise ValueError("empty reference set")
    return reference


def _client(provider: str, model: str, timeout: float, output_tokens: int) -> Any:
    client = build_workflow_client(
        resolve_provider_credentials(provider),
        model=model,
        model_cache_dir=Path(tempfile.gettempdir()) / "acceptance-template-models",
        timeout=timeout,
    )
    setter = getattr(client, "set_structured_output_token_limit", None)
    if callable(setter):
        setter(output_tokens)
    return client


def draft(args: argparse.Namespace) -> int:
    catalog = load_catalog(args.catalog)
    client = _client(args.provider, args.model, args.timeout, args.output_tokens)
    count = 0
    for path in sorted(args.inputs_dir.glob("*.json")):
        task = generation_input(path)
        recorder = Recorder(
            client,
            args.output_dir / "draft-calls" / task["task_id"],
            provider=args.provider,
            model=args.model,
            resume=args.resume,
            repairs=args.repairs,
        )
        reference = draft_reference(task, catalog, recorder, args.tasks_dir)
        write_json(args.output_dir / f"{task['task_id']}.json", reference)
        count += 1
    if not count:
        raise ValueError("no task inputs for reference drafting")
    print(json.dumps({"drafted_references": count, "label_status": "model_draft"}))
    return 0


def run(args: argparse.Namespace) -> int:
    catalog = load_catalog(args.catalog)
    paths = sorted(args.inputs_dir.glob("*.json"))
    if args.tasks:
        requested = set(args.tasks)
        paths = [path for path in paths if path.stem in requested]
        if {path.stem for path in paths} != requested:
            raise ValueError("requested task inputs not found")
    if not paths:
        raise ValueError("no task inputs")
    client = _client(args.provider, args.model, args.timeout, args.output_tokens)
    failures = []
    for path in paths:
        task = generation_input(path)
        task_dir = args.output_dir / task["task_id"]
        task_dir.mkdir(parents=True, exist_ok=True)
        inv_recorder = Recorder(
            client,
            task_dir / "inventory-calls",
            provider=args.provider,
            model=args.model,
            resume=args.resume,
            repairs=args.repairs,
        )
        try:
            inv = inventory(task, inv_recorder)
            write_json(
                task_dir / "inventory.json",
                {**inv, "input_sha256": digest(task), "calls": inv_recorder.calls},
            )
        except RuntimeError as exc:
            failures.append(
                {"task_id": task["task_id"], "arm": "inventory", "error": str(exc)}
            )
            continue
        for arm in args.arms:
            arm_dir = task_dir / arm
            recorder = Recorder(
                client,
                arm_dir / "calls",
                provider=args.provider,
                model=args.model,
                resume=args.resume,
                repairs=args.repairs,
            )
            try:
                result = generate(task, inv, catalog, recorder, arm, args.batch_size)
                result["shared_inventory_calls"] = inv_recorder.calls
            except RuntimeError as exc:
                failures.append(
                    {"task_id": task["task_id"], "arm": arm, "error": str(exc)}
                )
                result = {
                    "task_id": task["task_id"],
                    "arm": arm,
                    "status": "failed",
                    "input_sha256": digest(task),
                    "inventory": inv,
                    "criteria": [],
                    "residual_requirements": requirements(inv),
                    "calls": recorder.calls,
                }
            save_generation(arm_dir, result)
    write_json(
        args.output_dir / "run.json",
        {
            "provider": args.provider,
            "model": args.model,
            "catalog_sha256": digest(catalog),
            "tasks": [path.stem for path in paths],
            "arms": args.arms,
            "batch_size": args.batch_size,
            "output_tokens": args.output_tokens,
            "repairs": args.repairs,
            "failures": failures,
        },
    )
    print(json.dumps({"tasks": len(paths), "failures": failures}, indent=2))
    return 1 if failures else 0


def evaluate(args: argparse.Namespace) -> int:
    catalog = load_catalog(args.catalog)
    client = _client(args.provider, args.model, args.timeout, args.output_tokens)
    failures = []
    count = 0
    for task_dir in sorted(args.run_dir.iterdir()):
        if (
            not task_dir.is_dir()
            or not (args.inputs_dir / f"{task_dir.name}.json").exists()
        ):
            continue
        task = generation_input(args.inputs_dir / f"{task_dir.name}.json")
        reference = load_reference(
            args.references_dir / f"{task_dir.name}.json", task, catalog
        )
        for arm_dir in sorted(task_dir.iterdir()):
            path = arm_dir / "generation.json"
            if not path.exists():
                continue
            generation = load_json(path)
            if generation["status"] != "completed":
                failures.append(
                    {
                        "task_id": task["task_id"],
                        "arm": generation["arm"],
                        "error": "generation incomplete; cannot review",
                    }
                )
                continue
            if generation["input_sha256"] != digest(task):
                raise ValueError("generation input fingerprint mismatch")
            recorder = Recorder(
                client,
                arm_dir / "review-calls",
                provider=args.provider,
                model=args.model,
                resume=args.resume,
                repairs=args.repairs,
            )
            try:
                judgments = review(
                    task, generation, reference, recorder, args.batch_size
                )
                write_json(arm_dir / "review.json", judgments)
                count += 1
            except RuntimeError as exc:
                failures.append(
                    {
                        "task_id": task["task_id"],
                        "arm": generation["arm"],
                        "error": str(exc),
                    }
                )
    if not count and not failures:
        raise ValueError("no completed generation artifacts to evaluate")
    write_json(
        args.run_dir / "review-run.json",
        {
            "provider": args.provider,
            "model": args.model,
            "completed_reviews": count,
            "failures": failures,
        },
    )
    print(json.dumps({"completed_reviews": count, "failures": failures}, indent=2))
    return 1 if failures else 0


def score_run(args: argparse.Namespace) -> int:
    catalog = load_catalog(args.catalog)
    run_manifest = load_json(args.run_dir / "run.json")
    scores = []
    failures = list(run_manifest["failures"])
    for tid in run_manifest["tasks"]:
        task = generation_input(args.inputs_dir / f"{tid}.json")
        reference = load_reference(args.references_dir / f"{tid}.json", task, catalog)
        for arm in run_manifest["arms"]:
            directory = args.run_dir / tid / arm
            if (
                not (directory / "generation.json").exists()
                or not (directory / "review.json").exists()
            ):
                failures.append(
                    {
                        "task_id": tid,
                        "arm": arm,
                        "error": "generation/review incomplete",
                    }
                )
                continue
            generation = load_json(directory / "generation.json")
            if generation["status"] != "completed":
                continue
            scores.append(
                score(generation, reference, load_json(directory / "review.json"), task)
            )
    paired = [
        tid
        for tid in run_manifest["tasks"]
        if {row["arm"] for row in scores if row["task_id"] == tid}
        == set(run_manifest["arms"])
    ]
    report = {
        "run": run_manifest,
        "eligible_task_count": len(run_manifest["tasks"]),
        "paired_task_ids": paired,
        "aggregate": aggregate(scores),
        "paired_aggregate": aggregate(
            [row for row in scores if row["task_id"] in paired]
        ),
        "scores": scores,
        "failures": failures,
        "limitations": [
            "Agent-authored source-based references and automated judgments are "
            "not human gold labels.",
            "Template-group agreement is diagnostic, not proof.",
            "No coding agent, task validation suite, or Structrr diff was executed.",
            "Only declared reference validations contribute to recall; references "
            "may omit behavior. Inspect source coverage before interpreting scores.",
        ],
    }
    write_json(args.run_dir / "report.json", report)
    report_markdown(report, args.run_dir / "report.md")
    print(json.dumps(report["paired_aggregate"], indent=2))
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    collector = commands.add_parser(
        "collect", help="Collect instruction-only inputs from new repository families"
    )
    collector.add_argument("--tasks-dir", type=Path, required=True)
    collector.add_argument("--output-dir", type=Path, required=True)
    collector.add_argument("--tasks", nargs="+", default=list(PILOT_TASKS))
    for command in ("generate", "evaluate", "score", "draft-reference"):
        sub = commands.add_parser(command)
        sub.add_argument("--inputs-dir", type=Path, required=True)
        sub.add_argument("--catalog", type=Path, default=HERE / "catalog.json")
        if command in {"generate", "draft-reference"}:
            sub.add_argument("--output-dir", type=Path, required=True)
        if command == "draft-reference":
            sub.add_argument(
                "--tasks-dir",
                type=Path,
                help="Optional offline patches for reference drafting only",
            )
        elif command == "generate":
            sub.add_argument("--tasks", nargs="+")
            sub.add_argument(
                "--arms",
                nargs="+",
                choices=("direct", "templates"),
                default=["direct", "templates"],
            )
        else:
            sub.add_argument("--run-dir", type=Path, required=True)
            sub.add_argument("--references-dir", type=Path, required=True)
        if command != "score":
            sub.add_argument("--provider", default="deepinfra")
            sub.add_argument(
                "--model",
                default=DEFAULT_MODEL if command == "generate" else DEFAULT_JUDGE_MODEL,
            )
            sub.add_argument("--timeout", type=float, default=120)
            sub.add_argument("--output-tokens", type=int, default=12288)
            sub.add_argument(
                "--batch-size", type=int, default=6 if command == "generate" else 10
            )
            sub.add_argument("--repairs", type=int, default=1)
            sub.add_argument("--resume", action="store_true")
    args = parser.parse_args(argv)
    if args.command != "score" and args.command != "collect":
        if (
            args.batch_size < 1
            or args.timeout <= 0
            or args.output_tokens < 1
            or args.repairs < 0
        ):
            parser.error(
                "batch size, timeout and token budget must be positive; "
                "repairs nonnegative"
            )
    if args.command == "collect":
        manifest = collect(args.tasks_dir, args.output_dir, args.tasks)
        print(
            json.dumps(
                {"tasks": [row["task_id"] for row in manifest["tasks"]]}, indent=2
            )
        )
        return 0
    if args.command == "generate":
        return run(args)
    if args.command == "evaluate":
        return evaluate(args)
    if args.command == "draft-reference":
        return draft(args)
    return score_run(args)


if __name__ == "__main__":
    raise SystemExit(main())
