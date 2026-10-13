#!/usr/bin/env python3
"""Collect source-faithful routing candidates from DeepSWE instructions."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import tomllib
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from powdrr_lift.core.instruction_ledger import compile_instruction_ledger

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_SILVER = (
    ROOT / "science/classifications/root_disposition/data/root_disposition.jsonl"
)
DEFAULT_OUTPUT = Path(__file__).resolve().parent
ROUTES = ("context", "include", "include_prohibition", "exclude", "unclear")
DISPOSITION_TO_ROUTE = {
    "context": "context",
    "entity": "include",
    "feature": "include",
    "guidance": "include",
    "interface": "include",
    "invariant": "include",
    "non_goal": "include_prohibition",
    "nonactionable": "exclude",
}


def _jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(
            json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows
        ),
        encoding="utf-8",
    )


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _family_id(repository_url: Any, task_id: str) -> str:
    if not isinstance(repository_url, str) or not repository_url.strip():
        return f"family:deepswe-task:{task_id}"
    normalized = repository_url.strip().rstrip("/").removesuffix(".git").casefold()
    return f"family:deepswe-repository:{normalized}"


def _route_suggestion(row: dict[str, Any]) -> str | None:
    labels = row.get("labels", {})
    if labels.get("answerable") is not True:
        return "unclear"
    return DISPOSITION_TO_ROUTE.get(labels.get("class"))


def _silver_suggestions(path: Path) -> dict[tuple[str, str], list[dict[str, Any]]]:
    if not path.is_file():
        return {}
    result: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in _jsonl(path):
        task_id = row.get("source", {}).get("task_id")
        proposition = row.get("inputs", {}).get("proposition")
        route = _route_suggestion(row)
        if (
            isinstance(task_id, str)
            and isinstance(proposition, str)
            and route in ROUTES
        ):
            result[(task_id, proposition)].append(row)
    return result


def _slice_tags(text: str, heading: str | None) -> list[str]:
    lower = text.casefold()
    tags = []
    if re.search(r"\b(?:is|are|was|were|has|have|lacks|cannot|can't)\b", lower):
        tags.append("present_state_cue")
    for cue, tag in (
        ("must", "must_cue"),
        ("should", "should_cue"),
        ("will", "will_cue"),
    ):
        if re.search(rf"\b{cue}\b", lower):
            tags.append(tag)
    if re.match(
        r"(?i)^(?:start|use|keep|add|preserve|ensure|do|don't|never|avoid|run)\b",
        text.strip(),
    ):
        tags.append("imperative_cue")
    if re.search(r"(?i)\b(?:is|are|was|were)\s+\w+ed\b", text):
        tags.append("passive_form")
    if re.search(r"\b(?:not|never|no|cannot|can't|without|lacks?)\b", lower):
        tags.append("negation_or_absence")
    if heading:
        tags.append("heading_context")
        if re.search(
            r"(?i)definition|background|current|existing|today|motivation", heading
        ):
            tags.append("context_heading")
        if re.search(r"(?i)required|requirement|behavior|expected|must", heading):
            tags.append("requirement_heading")
    return tags


def _nearest_heading(source_text: str, start: int) -> str | None:
    for line in reversed(source_text[:start].splitlines()):
        stripped = line.strip()
        if stripped.startswith("#"):
            return stripped
    return None


def _sentence_span_without_heading(
    source_text: str, span: dict[str, Any]
) -> tuple[int, int] | None:
    """Keep Markdown headings as context, not part of the first target span."""
    start, end = span.get("start"), span.get("end")
    if not isinstance(start, int) or not isinstance(end, int):
        return None
    chunk = source_text[start:end]
    heading_prefix = re.match(r"(?:[ \t]*#{1,6}[ \t]+[^\n]*\n)+", chunk)
    if heading_prefix:
        start += heading_prefix.end()
    while start < end and source_text[start].isspace():
        start += 1
    while end > start and source_text[end - 1].isspace():
        end -= 1
    return (start, end) if start < end else None


def _build(
    args: argparse.Namespace,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, str]]:
    existing_rows = _jsonl(args.silver_dataset) if args.silver_dataset.is_file() else []
    known_families = {
        row.get("source", {}).get("source_family_id")
        for row in existing_rows
        if isinstance(row.get("source", {}).get("source_family_id"), str)
    }
    suggestions = _silver_suggestions(args.silver_dataset)
    documents: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    family_splits: dict[str, str] = {}
    task_files = sorted(args.tasks_dir.glob("*/task.toml"))
    if not task_files:
        raise ValueError(f"no task.toml files found in {args.tasks_dir}")

    for metadata_path in task_files:
        metadata = tomllib.loads(metadata_path.read_text(encoding="utf-8"))
        task_metadata = metadata.get("metadata", {})
        task_id = task_metadata.get("task_id")
        instruction_path = metadata_path.parent / "instruction.md"
        if not isinstance(task_id, str) or not instruction_path.is_file():
            continue
        raw = instruction_path.read_bytes()
        source_text = raw.decode("utf-8")
        source_hash = _sha256(raw)
        family_id = _family_id(task_metadata.get("repository_url"), task_id)
        # Any family that has appeared in prior classifier work is development only.
        split = (
            "development"
            if family_id in known_families
            else (
                "fresh_holdout"
                if int(_sha256(family_id.encode())[:8], 16) % 5 == 0
                else "development"
            )
        )
        family_splits[family_id] = split
        ledger = compile_instruction_ledger(task_id, source_text)
        clauses = []
        for ledger_clause in ledger.clauses:
            clause_data = ledger_clause.to_data()
            target_span = _sentence_span_without_heading(
                source_text, clause_data["source_span"]
            )
            if target_span is None:
                continue
            clause_data["source_span"] = {
                "start": target_span[0],
                "end": target_span[1],
            }
            clause_data["text"] = source_text[target_span[0] : target_span[1]]
            clauses.append(clause_data)
        doc_id = f"doc:{task_id}"
        source_ref = f"deepswe:{task_id}:instruction.md"
        documents.append(
            {
                "schema_version": "routing-source-document-v1",
                "document_id": doc_id,
                "task_id": task_id,
                "family_id": family_id,
                "source_ref": source_ref,
                "origin": "deepswe_task_instruction",
                "repository_url": task_metadata.get("repository_url"),
                "language": task_metadata.get("language"),
                "license": task_metadata.get("license") or "unknown",
                "source_sha256": source_hash,
                "text": source_text,
                "clauses": clauses,
            }
        )
        for ordinal, source_clause in enumerate(clauses):
            span = source_clause.get("source_span", {})
            start, end = span.get("start"), span.get("end")
            if (
                not isinstance(start, int)
                or not isinstance(end, int)
                or not (0 <= start < end <= len(source_text))
            ):
                raise ValueError(f"invalid sentence span in {task_id}: {span!r}")
            proposition = source_text[start:end]
            previous_text = (
                source_text[
                    clauses[ordinal - 1]["source_span"]["start"] : clauses[ordinal - 1][
                        "source_span"
                    ]["end"]
                ]
                if ordinal
                else None
            )
            next_text = (
                source_text[
                    clauses[ordinal + 1]["source_span"]["start"] : clauses[ordinal + 1][
                        "source_span"
                    ]["end"]
                ]
                if ordinal + 1 < len(clauses)
                else None
            )
            heading = _nearest_heading(source_text, start)
            base_key = f"{task_id}:{start}:{end}:{_sha256(proposition.encode())}"
            example_id = f"routing:{_sha256(base_key.encode())[:20]}"
            old = suggestions.get((task_id, proposition), [])
            old_routes = {_route_suggestion(item) for item in old}
            old_routes.discard(None)
            suggested = next(iter(old_routes)) if len(old_routes) == 1 else None
            candidates.append(
                {
                    "schema_version": "routing-example-v1",
                    "example_id": example_id,
                    "decision_kind": "routing",
                    "input_revision": "routing-input-v1",
                    "rubric_revision": "routing-rubric-v1",
                    "source": {
                        "document_id": doc_id,
                        "family_id": family_id,
                        "origin": "real_instruction",
                        "source_ref": source_ref,
                        "source_sha256": source_hash,
                        "target_span": {"start": start, "end": end},
                        "derivation_group_id": doc_id,
                        "derivation": "deterministic-sentence-v1",
                        "license": task_metadata.get("license") or "unknown",
                        "task_id": task_id,
                        "language": task_metadata.get("language") or "unknown",
                    },
                    "inputs": {
                        "proposition": proposition,
                        "local_context": {
                            "section_heading": heading,
                            "previous_sentence": previous_text,
                            "source_sentence": proposition,
                            "next_sentence": next_text,
                        },
                        "scope_relations": None,
                    },
                    "annotation": {
                        "suggested_label": suggested,
                        "status": "proposed" if suggested else "unlabeled",
                        "reviewer_kind": "planning_llm_teacher" if suggested else None,
                        "reviewer_id": "historical-disposition-label"
                        if suggested
                        else None,
                        "rationale": None,
                        "evidence_quotes": [],
                        "ambiguity_reason": None,
                    },
                    "slice_tags": _slice_tags(proposition, heading),
                    "split": split,
                }
            )

    if len({row["example_id"] for row in candidates}) != len(candidates):
        raise ValueError("candidate IDs are not unique")
    return documents, candidates, family_splits


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks-dir", type=Path, required=True)
    parser.add_argument("--silver-dataset", type=Path, default=DEFAULT_SILVER)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    docs, candidates, family_splits = _build(args)
    _write_jsonl(args.output_dir / "raw/source_documents.jsonl", docs)
    _write_jsonl(args.output_dir / "data/candidates.jsonl", candidates)
    _write_jsonl(
        args.output_dir / "splits/family-splits.jsonl",
        [
            {"family_id": family_id, "split": split}
            for family_id, split in sorted(family_splits.items())
        ],
    )
    counts = Counter(row["split"] for row in candidates)
    manifest = {
        "schema_version": "routing-candidate-dataset-manifest-v1",
        "created_at": datetime.now(UTC).isoformat(),
        "status": "candidate_data_not_gold",
        "source_documents": len(docs),
        "candidate_count": len(candidates),
        "family_count": len(family_splits),
        "candidate_counts_by_split": dict(sorted(counts.items())),
        "unreviewed_suggestions_are_not_gold": True,
        "input_revision": "routing-input-v1",
        "rubric_revision": "routing-rubric-v1",
        "source_documents_sha256": _sha256(
            (args.output_dir / "raw/source_documents.jsonl").read_bytes()
        ),
        "candidates_sha256": _sha256(
            (args.output_dir / "data/candidates.jsonl").read_bytes()
        ),
        "split_manifest_sha256": _sha256(
            (args.output_dir / "splits/family-splits.jsonl").read_bytes()
        ),
        "provenance": {
            "instruction_root": "external_deepswe_tasks_checkout",
            "historical_silver_dataset": (
                "science/classifications/root_disposition/data/root_disposition.jsonl"
            ),
        },
    }
    _write_json(args.output_dir / "data/manifest.json", manifest)
    print(
        json.dumps(
            {
                "source_documents": len(docs),
                "candidates": len(candidates),
                "families": len(family_splits),
                "counts_by_split": dict(sorted(counts.items())),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
