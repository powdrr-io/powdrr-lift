#!/usr/bin/env python3
"""Evaluate validation discovery against pinned, manually reviewed repositories."""

from __future__ import annotations

import argparse
import json
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml
from powdrr_service.structrr.validation_corpus import (
    evaluate_repository,
    render_corpus_markdown,
    validate_corpus_definition,
)


def _repo_overrides(values: Sequence[str]) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for value in values:
        repository_id, separator, directory = value.partition("=")
        if not separator or not repository_id or not directory:
            raise ValueError("--repo-dir must use ID=PATH")
        if repository_id in result:
            raise ValueError(f"duplicate --repo-dir for {repository_id!r}")
        result[repository_id] = Path(directory).resolve()
    return result


def _checkout(entry: Mapping[str, Any], destination: Path) -> Path:
    subprocess.run(
        [
            "git",
            "clone",
            "--filter=blob:none",
            "--depth=1",
            "--no-checkout",
            entry["repository"],
            str(destination),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(destination),
            "fetch",
            "--depth=1",
            "origin",
            entry["revision"],
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(destination),
            "checkout",
            "--detach",
            entry["revision"],
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return destination


def _revision(directory: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(directory), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _evaluate(
    corpus: Mapping[str, Any],
    *,
    selected: set[str],
    overrides: Mapping[str, Path],
    download: bool,
) -> dict[str, Any]:
    entries = [
        entry
        for entry in corpus["repositories"]
        if not selected or entry["id"] in selected
    ]
    unknown = selected - {entry["id"] for entry in entries}
    if unknown:
        raise ValueError(f"unknown repository ids: {', '.join(sorted(unknown))}")
    if overrides and download:
        raise ValueError("use either --repo-dir or --download, not both")
    if overrides and {entry["id"] for entry in entries} - set(overrides):
        missing = {entry["id"] for entry in entries} - set(overrides)
        raise ValueError(f"missing --repo-dir values for: {', '.join(sorted(missing))}")
    results: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="powdrr-validation-corpus-") as temporary:
        for entry in entries:
            repository_id = entry["id"]
            if overrides:
                directory = overrides[repository_id]
            elif download:
                directory = _checkout(entry, Path(temporary) / repository_id)
            else:
                raise ValueError(
                    "provide all repositories with --repo-dir, or pass --download"
                )
            actual_revision = _revision(directory)
            if actual_revision != entry["revision"]:
                raise ValueError(
                    f"{repository_id} is at {actual_revision}, expected "
                    f"{entry['revision']}"
                )
            result = evaluate_repository(entry, directory)
            result["actual_revision"] = actual_revision
            results.append(result)
    totals: dict[str, int] = {
        "expected_obligations": 0,
        "exact_executable_discoveries": 0,
        "missed_obligations": 0,
        "unreviewed_discoveries": 0,
        "invented_required_checks": 0,
        "command_context_mismatches": 0,
        "provider_mismatches": 0,
        "evidence_gaps": 0,
        "expected_unresolved_obligations": 0,
        "observed_unresolved_profiles": 0,
    }
    for result in results:
        totals["expected_obligations"] += result["expected_obligations"]
        totals["exact_executable_discoveries"] += result["exact_executable_discoveries"]
        totals["missed_obligations"] += len(result["missed_obligations"])
        totals["unreviewed_discoveries"] += result["unreviewed_discoveries"]
        totals["invented_required_checks"] += len(result["invented_required_checks"])
        totals["command_context_mismatches"] += len(
            result["command_context_mismatches"]
        )
        totals["provider_mismatches"] += len(result["provider_mismatches"])
        totals["evidence_gaps"] += len(result["evidence_gaps"])
        totals["expected_unresolved_obligations"] += result[
            "expected_unresolved_obligations"
        ]
        totals["observed_unresolved_profiles"] += len(
            result["observed_unresolved_profiles"]
        )
    return {
        "schema_version": "validation-corpus-report-v1",
        "corpus_revision": corpus.get("corpus_revision", "unspecified"),
        "repositories": results,
        "totals": totals,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--corpus",
        type=Path,
        default=Path("tests/fixtures/validation_corpus/corpus.yaml"),
    )
    parser.add_argument("--repo-dir", action="append", default=[], metavar="ID=PATH")
    parser.add_argument("--download", action="store_true")
    parser.add_argument("--only", action="append", default=[], metavar="ID")
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    corpus = yaml.safe_load(args.corpus.read_text(encoding="utf-8"))
    issues = validate_corpus_definition(corpus)
    if issues:
        parser.error("invalid corpus: " + "; ".join(issues))
    try:
        report = _evaluate(
            corpus,
            selected=set(args.only),
            overrides=_repo_overrides(args.repo_dir),
            download=args.download,
        )
    except (OSError, subprocess.CalledProcessError, ValueError) as error:
        parser.error(str(error))
    rendered = (
        render_corpus_markdown(report)
        if args.format == "markdown"
        else json.dumps(report, indent=2, sort_keys=True) + "\n"
    )
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
