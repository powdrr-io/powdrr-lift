"""Retrieve and preserve source evidence independently for each requirement."""

from __future__ import annotations

import argparse
import gzip
import json
import re
from pathlib import Path
from typing import Any

from science.deepswe.acceptance_templates.common import load_json, write_json
from science.deepswe.acceptance_templates.repository_planning.prepare import inspect
from science.deepswe.acceptance_templates.repository_planning.symbols import retrieve


def _ranked(retrieval: dict[str, Any], limit: int, kind: str) -> list[dict[str, Any]]:
    return [
        row
        for row in retrieval["candidates"]
        if row["reason"] == "lexical" and row["symbol"]["test"] == (kind == "test")
    ][:limit]


def _anchors(
    retrieval: dict[str, Any], requirement: str, candidates: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    terms = set(retrieval["query_terms"])
    exact = []
    for candidate in candidates:
        symbol = candidate["symbol"]
        components = [
            component.lower()
            for identifier in symbol["qualified_name"].split(".")
            for component in re.findall(
                r"[A-Z]+(?=[A-Z][a-z]|$)|[A-Z]?[a-z]+|[0-9]+", identifier
            )
        ]
        # Avoid treating common prose words (such as "payload") as exact API
        # references. A name is an anchor only when all its identifier words
        # occur in the active requirement, or its full CamelCase name does.
        if len(components) > 1 and (
            set(components) <= terms or "".join(components) in terms
        ):
            exact.append(candidate)
    return exact


def build(
    container: str,
    packet: dict[str, Any],
    output: Path,
    max_production: int = 7,
    max_tests: int = 2,
    max_source_chars: int = 3500,
) -> dict[str, Any]:
    """Index once, rank each requirement against all symbols, and read evidence."""
    if output.exists():
        raise ValueError("Use a fresh output directory for evidence packets")
    output.mkdir(parents=True)
    index = inspect(container, {"operation": "index"})
    if index.get("error"):
        raise ValueError(index)
    if index["parse_errors"]:
        raise ValueError(f"Symbol index has parse errors: {index['parse_errors']}")
    (output / "symbol-index.json.gz").write_bytes(
        gzip.compress(json.dumps(index).encode(), mtime=0)
    )

    routed = [
        row
        for row in packet["routed_clauses"]
        if row["route"] in {"include", "include_prohibition"}
    ]
    manifests = []
    for requirement in routed:
        retrieval = retrieve(index, requirement["text"])
        production = _ranked(retrieval, 10, "production")
        tests = _ranked(retrieval, 3, "test")
        all_production = [
            row for row in retrieval["candidates"] if not row["symbol"]["test"]
        ]
        anchors = _anchors(retrieval, requirement["text"], all_production)
        chosen = list(anchors[:max_production])
        chosen_ids = {row["symbol"]["id"] for row in chosen}
        anchor_classes = {
            row["symbol"]["qualified_name"]
            for row in anchors
            if row["symbol"]["kind"] == "class"
        }
        candidate_by_id = {row["symbol"]["id"]: row for row in retrieval["candidates"]}
        # When prose names a receiver, include its public API surface in source
        # order. Ranking only the lexical top ten can crowd out methods such as
        # ``directives`` because every method inherits the same receiver terms.
        direct_methods = [
            {
                "id": symbol["id"],
                "symbol": symbol,
                "score": candidate_by_id.get(symbol["id"], {}).get("score", 0),
                "matched_terms": candidate_by_id.get(symbol["id"], {}).get(
                    "matched_terms", []
                ),
                "reason": "public member of exact receiver",
            }
            for symbol in sorted(
                (
                    row
                    for row in index["symbols"]
                    if row["container"] in anchor_classes
                    and row["kind"] == "function"
                    and (not row["name"].startswith("_") or row["name"] == "__call__")
                    and not row["test"]
                ),
                key=lambda row: row["start"],
            )
        ]
        chosen.extend(
            row
            for row in direct_methods
            if row["id"] not in chosen_ids and len(chosen) < max_production
        )
        chosen_ids.update(row["symbol"]["id"] for row in chosen)
        chosen.extend(
            row
            for row in production
            if row["symbol"]["id"] not in chosen_ids and len(chosen) < max_production
        )
        chosen.extend(tests[:max_tests])

        evidence = []
        for candidate in chosen:
            symbol = candidate["symbol"]
            end = min(symbol["end"], symbol["start"] + 119)
            request = {
                "operation": "read",
                "path": symbol["path"],
                "start": symbol["start"],
                "end": end,
            }
            response = inspect(container, request)
            if response.get("error"):
                raise ValueError({"requirement": requirement["id"], **response})
            if response["file_sha256"] != symbol["file_sha256"]:
                raise ValueError(f"Source changed since indexing: {symbol['path']}")
            original = response["text"]
            char_limit = (
                max_source_chars + 2500
                if symbol["kind"] == "class"
                else max_source_chars
            )
            excerpt = original[:char_limit]
            evidence.append(
                {
                    "reference": symbol["id"],
                    "path": symbol["path"],
                    "signature": symbol["signature"],
                    "parent": symbol["container"],
                    "kind": symbol["kind"],
                    "test": symbol["test"],
                    "score": candidate["score"],
                    "matched_terms": candidate["matched_terms"],
                    "reason": candidate.get("reason", "lexical"),
                    "start": symbol["start"],
                    "end": response["end"],
                    "file_sha256": response["file_sha256"],
                    "text": excerpt,
                    "truncated_lines": response["end"] < symbol["end"],
                    "truncated_chars": len(original) > char_limit,
                }
            )
        path = output / "packets" / f"{requirement['id']}.json"
        write_json(
            path,
            {
                "requirement": requirement,
                "query_terms": retrieval["query_terms"],
                "matching_symbols": retrieval["total_matching_symbols"],
                "truncated_candidates": retrieval["truncated"],
                "ranked_candidates": [
                    {
                        "reference": row["symbol"]["id"],
                        "score": row["score"],
                        "matched_terms": row["matched_terms"],
                    }
                    for row in [*production, *tests]
                ],
                "selected": [item["reference"] for item in evidence],
                "evidence": evidence,
            },
        )
        manifests.append(
            {
                "requirement_id": requirement["id"],
                "matching_symbols": retrieval["total_matching_symbols"],
                "selected": len(evidence),
                "anchors": [row["symbol"]["id"] for row in anchors],
                "truncated_sources": sum(
                    item["truncated_lines"] or item["truncated_chars"]
                    for item in evidence
                ),
                "top_sources": [item["reference"] for item in evidence[:3]],
            }
        )
    summary = {
        "base": inspect(container, {"operation": "status"}),
        "requirements": len(routed),
        "symbol_count": len(index["symbols"]),
        "per_requirement": manifests,
        "retrieval": (
            "Whole-index lexical retrieval per routed requirement; exact CamelCase "
            "receiver terms are expanded and prioritized."
        ),
        "oracle_access": False,
        "tests_run": False,
        "coding_agents_run": False,
    }
    write_json(output / "summary.json", summary)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--setup", type=Path, required=True)
    parser.add_argument("--generation", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    environment = load_json(args.setup / "environment.json")
    packet = load_json(args.generation / "input.json")
    print(json.dumps(build(environment["container"], packet, args.output)), flush=True)


if __name__ == "__main__":
    main()
