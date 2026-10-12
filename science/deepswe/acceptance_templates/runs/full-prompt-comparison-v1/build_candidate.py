"""Assemble a complete candidate without reading oracle or solution artifacts."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
RUNS = HERE.parent
ANALYSIS = RUNS / "operation-analysis-v1"
DECISIONS = RUNS / "deterministic-decisions-v1"
BASE = Path("/private/tmp/koota-aspect-analysis-base-20261011")


def read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError("expected an object")
    return value


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    task = read(ANALYSIS / "input.json")
    inventory = read(ANALYSIS / "instruction-analysis.json")["inventory"]["items"]
    selected = read(DECISIONS / "results.json")["decisions"]
    product = [row for row in inventory if row["route"] == "include"]
    excluded = [row for row in inventory if row["route"] == "exclude"]
    sections: list[tuple[str, list[str]]] = []
    mappings: list[dict[str, Any]] = []
    sections.append(
        (
            "Objective",
            [
                "Add composite trait aspects with unified entity, query and "
                "subscription operations. " + task["source_spans"][0]["text"]
            ],
        )
    )
    requirements = []
    for index, row in enumerate(product, 1):
        requirements.append(f"{index}. {row['text']}")
        mappings.append(
            {
                "section": "Requirements",
                "number": index,
                "text": row["text"],
                "source": "instruction-analysis.json",
                "source_id": row["id"],
                "original_source_ids": row["source_ids"],
            }
        )
    rendered = [row for row in selected if row["status"] == "rendered"]
    for row in rendered:
        number = len(requirements) + 1
        requirements.append(f"{number}. {row['statement']}")
        mappings.append(
            {
                "section": "Requirements",
                "number": number,
                "text": row["statement"],
                "source": "deterministic-decisions-v1/results.json",
                "source_id": row["id"],
            }
        )
    sections.append(("Requirements", requirements))
    # Execution boundaries are controller context, not newly inferred task policy.
    sections.append(
        (
            "Constraints",
            [
                "- Powdrr manages branching and commits for this task. "
                "Implement the requested changes in the supplied checkout."
            ],
        )
    )
    context = [
        "- Relevant existing core modules: `src/trait/trait.ts`, "
        "`src/trait/types.ts`, `src/query/query-result.ts`, "
        "`src/query/types.ts`, `src/query/query.ts`, `src/world/world.ts` "
        "and `src/index.ts` under `packages/core`.",
        "- Existing regular-trait `setTraitForTrait` writes through `ctx.set` "
        "and calls `setChanged` when `triggerChanged` is true, without an "
        "equality guard.",
        "- Existing `createQuery` freezes its query object; that freeze does "
        "not freeze the referenced parameters array.",
    ]
    sections.append(("Relevant context", context))
    criteria: list[str] = []
    for row in rendered:
        if row.get("scenario"):
            number = len(criteria) + 1
            criteria.append(f"{number}. {row['scenario']}")
            mappings.append(
                {
                    "section": "Acceptance criteria",
                    "number": number,
                    "text": row["scenario"],
                    "source": "deterministic-decisions-v1/results.json",
                    "source_id": row["id"],
                }
            )
    criteria.append(
        "\nThe remaining numbered requirements are also required completion "
        "conditions; the examples above cover only the resolved clarifications."
    )
    sections.append(("Acceptance criteria", criteria))
    package = json.loads((BASE / "packages/core/package.json").read_text())
    assert package["name"] == "@koota/core"
    assert package["scripts"]["test"] == "vitest"
    assert package["scripts"]["lint"] == "oxlint"
    sections.append(
        (
            "Verification and completion",
            [
                "- Add or update focused tests for the numbered requirements "
                "and acceptance scenarios. Run the core tests once without "
                "watching: `pnpm --filter @koota/core test --run`.",
                "- Run the existing core lint command: "
                "`pnpm --filter @koota/core lint`.",
                "- Report changed files, checks actually performed, failures "
                "and any remaining requirement ambiguity when finished.",
            ],
        )
    )
    lines = []
    for title, content in sections:
        lines.extend([f"# {title}", "", *content, ""])
    (HERE / "candidate-prompt.md").write_text("\n".join(lines))
    provenance = {
        "generation": "Deterministic assembly; no model calls or oracle inputs.",
        "source_product_clauses": len(product),
        "preserved_product_clause_ids": [row["id"] for row in product],
        "excluded_controller_clause_ids": [row["id"] for row in excluded],
        "clarification_ids": [row["id"] for row in rendered],
        "unresolved": [row["id"] for row in selected if row["status"] != "rendered"],
        "mappings": mappings,
        "inputs": {
            str(path): sha(path)
            for path in (
                ANALYSIS / "input.json",
                ANALYSIS / "instruction-analysis.json",
                DECISIONS / "results.json",
                BASE / "packages/core/package.json",
            )
        },
        "other_scaffolding": {
            "objective": "Original task rationale and named API operations.",
            "constraints": "User-established Powdrr controller boundary.",
            "context": "Previously verified original-code observations and paths.",
            "verification": (
                "User-requested six-section format and original package scripts."
            ),
        },
    }
    (HERE / "candidate-provenance.json").write_text(
        json.dumps(provenance, indent=2) + "\n"
    )
    # Rendering control: retain the same wider analysis used by the old LLM
    # formatter. Selection is based on its saved basis, never oracle judgments.
    analysis_rows = read(ANALYSIS / "focused/analysis.json")["decisions"]
    declared = [
        row for row in analysis_rows if row["basis"] in {"instruction", "repository"}
    ]
    control_requirements = [f"{i}. {row['text']}" for i, row in enumerate(product, 1)]
    for row in declared:
        control_requirements.append(f"{len(control_requirements) + 1}. {row['rule']}")
    control_sections = []
    for title, content in sections:
        if title == "Requirements":
            content = control_requirements
        elif title == "Acceptance criteria":
            content = [
                "Every numbered requirement is a required completion condition. "
                "Validate its stated membership, data, notification or public API "
                "behavior with focused tests. No additional scenario is synthesized."
            ]
        control_sections.extend([f"# {title}", "", *content, ""])
    (HERE / "same-analysis-prompt.md").write_text("\n".join(control_sections))
    (HERE / "same-analysis-provenance.json").write_text(
        json.dumps(
            {
                "analysis_input": str(ANALYSIS / "focused/analysis.json"),
                "analysis_sha256": sha(ANALYSIS / "focused/analysis.json"),
                "included_decision_ids": [row["id"] for row in declared],
                "withheld_decision_ids": [
                    row["id"] for row in analysis_rows if row not in declared
                ],
                "selection": (
                    "Saved instruction/repository basis only; no gold filtering."
                ),
                "limitation": (
                    "Source attribution may be wrong. Clauses and declared rules "
                    "overlap; no semantic deduplication or criterion synthesis."
                ),
            },
            indent=2,
        )
        + "\n"
    )


if __name__ == "__main__":
    main()
