"""One-task experiment; generation never loads the solution or evaluation oracle."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from powdrr_lift.workrr.jev_classifier import JevSemanticClassifierClient
from powdrr_lift.workrr.procedrr import WorkrrProcedrrClient
from science.deepswe.acceptance_templates.cli import DEFAULT_MODEL, _client
from science.deepswe.acceptance_templates.common import (
    IDS,
    TEXT,
    Recorder,
    array_schema,
    choice_schema,
    object_schema,
    source_spans,
    write_json,
)
from science.deepswe.acceptance_templates.production_analysis import (
    DESIGN_FLOW,
    JevPromptAdapter,
    analyze_instruction,
    load_production_judges,
)

HERE = Path(__file__).resolve().parent
BASE_SHA = "9c434858b2b522002f8c5eb4a554fa8836a7cf3c"

# Existing modules selected from operation names in the instruction. No aspect
# implementation, solution/test patch, oracle answers, or prior prompt is input.
BASE_FILES = [
    "index.ts",
    "entity/entity-methods-patch.ts",
    "entity/types.ts",
    "trait/trait.ts",
    "trait/types.ts",
    "query/query-result.ts",
    "query/query.ts",
    "query/types.ts",
    "query/modifier.ts",
    "query/modifiers/added.ts",
    "query/modifiers/changed.ts",
    "query/modifiers/removed.ts",
    "query/modifiers/not.ts",
    "query/modifiers/or.ts",
    "query/utils/check-query.ts",
    "query/utils/check-query-tracking.ts",
    "query/utils/tracking-cursor.ts",
    "query/utils/create-query-hash.ts",
    "world/world.ts",
    "world/types.ts",
    "utils/shallow-equal.ts",
    "storage/accessors.ts",
    "storage/schema.ts",
    "storage/types.ts",
]

GROUPS = [
    (
        "construction",
        ["createAspect", "public exports", "id/traits/schema", "nested aspects"],
        [
            "index.ts",
            "trait/types.ts",
            "trait/trait.ts",
            "storage/schema.ts",
            "storage/types.ts",
        ],
    ),
    (
        "entity",
        ["has", "get", "set", "add", "remove"],
        [
            "entity/entity-methods-patch.ts",
            "entity/types.ts",
            "trait/trait.ts",
            "trait/types.ts",
            "storage/accessors.ts",
        ],
    ),
    (
        "query-data",
        ["direct query", "readEach", "updateEach", "query lifecycle and callback data"],
        [
            "query/query-result.ts",
            "query/types.ts",
            "query/utils/create-query-hash.ts",
            "storage/accessors.ts",
            "utils/shallow-equal.ts",
            "world/types.ts",
        ],
    ),
    (
        "modifiers",
        [
            "Not",
            "Or",
            "Changed",
            "Added",
            "Removed",
            "modifier composition and tracking",
        ],
        [
            "query/query.ts",
            "query/types.ts",
            "query/modifier.ts",
            "query/modifiers/added.ts",
            "query/modifiers/changed.ts",
            "query/modifiers/removed.ts",
            "query/modifiers/not.ts",
            "query/modifiers/or.ts",
            "query/utils/check-query.ts",
            "query/utils/check-query-tracking.ts",
            "query/utils/tracking-cursor.ts",
        ],
    ),
    (
        "subscriptions",
        ["onAdd", "onRemove", "onChange", "unsubscribe", "world reset"],
        [
            "world/world.ts",
            "world/types.ts",
            "trait/trait.ts",
            "query/modifiers/changed.ts",
        ],
    ),
]

ANALYSIS_PROMPT = """Analyze the supplied routed product requirements into
implementation decisions that remove ambiguity for a coding agent. Use only
the supplied instruction and, when present, original repository code. You have
no target solution or validation patch. Context and excluded process statements
must not become obligations. Preserve include_prohibition polarity.

For each operation resolve: inputs and accepted forms; preconditions and missing
or invalid state; output shape and identity/copy/mutability; writes and omitted
or unknown fields; equality and change detection; notification unit and timing;
composition/grouping; lifecycle, existing options, callback positions and public
API compatibility. Apply only dimensions relevant to that operation. Use full
instruction context and related operations. Do not repeat a decision under
multiple headings or require a particular architecture.

Keep one behavioral decision per item. instruction means explicitly required
or necessarily implied by source. repository means an existing contract you
can justify inheriting, with an exact code quotation. assumption means a chosen
default for a policy not settled by those sources; name the alternatives and
explain the choice. unresolved means the sources do not justify choosing; name
the alternatives. Do not disguise a preference as evidence. An existing
single-trait operation does not automatically establish a new composite
operation's precondition or grouping rule. Do not reject reasonable defaults
just because details are missing, but label them honestly. Do not write the
final prompt or acceptance criteria yet. No arbitrary item count or brevity
target. Aim for all decisions necessary for clear behavior with limited
repetition. The behavior field MUST contain a complete sentence describing
what the operation does, including conditions and observable outcomes. It must
never contain an evidence-category label. The basis field alone contains that
label; rationale explains the evidence and is not a substitute for behavior.
Return the requested JSON only."""

ANALYSIS_PROMPT += """
Analyze ONLY the supplied focus_operations. Other operations are context, not
an invitation to enumerate another inventory. Behavior and rationale should
each be one precise sentence; quote only the relevant few lines of code.
Include concrete decisions that affect observable behavior, not speculative
combinations or descriptions of unrelated existing APIs. Finish after the
focused operations are covered; do not repeat or pad the decision list."""

EVIDENCE = object_schema(file=TEXT, quote=TEXT)
ANALYSIS_SCHEMA = object_schema(
    objective=TEXT,
    decisions=array_schema(
        object_schema(
            operation=TEXT,
            behavior={
                "type": "string",
                ("description"): (
                    "A concrete behavioral rule about the operation, conditions and "
                    "outcome; NOT an evidence category."
                ),
            },
            basis=choice_schema(
                "instruction", "repository", "assumption", "unresolved"
            ),
            source_ids=IDS,
            evidence=array_schema(EVIDENCE),
            alternatives=IDS,
            rationale=TEXT,
        )
    ),
)

PROMPT_SCHEMA = object_schema(
    objective=TEXT,
    requirements=array_schema(object_schema(decision_ids=IDS, text=TEXT)),
    constraints=array_schema(object_schema(decision_ids=IDS, text=TEXT)),
    relevant_context=array_schema(object_schema(decision_ids=IDS, text=TEXT)),
    acceptance_criteria=array_schema(object_schema(decision_ids=IDS, text=TEXT)),
    verification=IDS,
)


def load(name: str) -> dict[str, Any]:
    return json.loads((HERE / name).read_text())


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def prepare(task_dir: Path, base_dir: Path) -> None:
    head = (base_dir / ".git/HEAD").read_text().strip()
    if head != BASE_SHA:
        raise ValueError("base checkout is not detached at the task's original commit")
    instruction = (task_dir / "instruction.md").read_text()
    write_json(
        HERE / "input.json",
        {
            "task_id": "koota-composite-trait-aspects",
            "instruction": instruction,
            "source_spans": source_spans(instruction),
        },
    )
    files = []
    for relative in BASE_FILES:
        path = base_dir / "packages/core/src" / relative
        files.append(
            {
                "file": "packages/core/src/" + relative,
                "sha256": sha(path),
                "text": path.read_text(),
            }
        )
    write_json(
        HERE / "repository-evidence.json", {"base_commit": BASE_SHA, "files": files}
    )
    write_json(
        HERE / "manifest.json",
        {
            "task_id": "koota-composite-trait-aspects",
            "model": DEFAULT_MODEL,
            "router_provider": "typesafe.ai",
            "router_model": os.environ.get("SYSTEM_ONE_MODEL", "jev-latest"),
            "base_commit": BASE_SHA,
            "oracle_sha256_before_analysis": sha(HERE / "oracle.json"),
            "input_sha256": sha(HERE / "input.json"),
            "repository_evidence_sha256": sha(HERE / "repository-evidence.json"),
            "solution_sha256": sha(task_dir / "solution/solution.patch"),
            "validation_sha256": sha(task_dir / "tests/test.patch"),
            "runner_sha256": sha(Path(__file__)),
            ("boundary"): (
                "Model payloads contain instruction, production routed inventory and "
                "base code only. Oracle, solution, validation patch, prior prompts "
                "and review results are withheld."
            ),
        },
    )


def recorder(arm: str, client: Any = None) -> Recorder:
    return Recorder(
        client or _client("deepinfra", DEFAULT_MODEL, 240, 16384),
        HERE / "calls" / arm,
        provider="deepinfra",
        model=DEFAULT_MODEL,
        resume=True,
        repairs=1,
    )


def analyze() -> None:
    task = load("input.json")
    analysis_path = HERE / "instruction-analysis.json"
    if not analysis_path.exists():
        if not any(
            os.environ.get(k)
            for k in ("TYPESAFEAI_API_KEY", "TYPESAFE_API_KEY", "SYSTEM_ONE_API_KEY")
        ):
            raise ValueError("Typesafe.ai key is missing; no classifier substitution")
        planning = _client("deepinfra", DEFAULT_MODEL, 240, 12288)
        adapter = JevPromptAdapter(
            WorkrrProcedrrClient(
                JevSemanticClassifierClient(planning, fail_closed=True),
                skills_dir=DESIGN_FLOW.parent,
            )
        )
        jev = Recorder(
            adapter,
            HERE / "calls/jev",
            provider="typesafe.ai",
            model=os.environ.get("SYSTEM_ONE_MODEL", "jev-latest"),
            resume=True,
            repairs=1,
        )
        splitter = recorder(
            "splitter", WorkrrProcedrrClient(planning, skills_dir=DESIGN_FLOW.parent)
        )
        try:
            result = analyze_instruction(
                task,
                jev_recorder=jev,
                split_recorder=splitter,
                judges=load_production_judges(),
            )
            write_json(analysis_path, result)
        finally:
            adapter.close()
    routing = load("instruction-analysis.json")
    inv = routing["inventory"]
    files = load("repository-evidence.json")["files"]
    allowed_sources = {r["id"] for r in task["source_spans"]}
    routed_sources = {r["id"]: r["source_ids"] for r in inv["items"]}

    def run_group(
        job: tuple[str, str, list[str], list[str]],
    ) -> tuple[str, str, dict[str, Any], list[dict[str, Any]]]:
        arm, group, operations, paths = job
        provided = (
            [f for f in files if f["file"].removeprefix("packages/core/src/") in paths]
            if arm == "repository-grounded"
            else []
        )
        by_file = {r["file"]: r["text"] for r in provided}

        def validate(raw: dict[str, Any]) -> dict[str, Any]:
            if not raw.get("objective") or not raw.get("decisions"):
                raise ValueError("empty objective or decisions")
            for index, row in enumerate(raw["decisions"], 1):
                sources = []
                for sid in row["source_ids"]:
                    if sid in allowed_sources:
                        sources.append(sid)
                    elif sid in routed_sources:
                        sources.extend(routed_sources[sid])
                    else:
                        raise ValueError("unknown source anchor")
                row["raw_source_ids"] = row["source_ids"]
                row["source_ids"] = list(dict.fromkeys(sources))
                if len(row["behavior"].split()) < 5:
                    raise ValueError("invalid rule or source anchor")
                verified_evidence = []
                rejected_evidence = []
                for evidence in row["evidence"]:
                    evidence_text = (
                        task["instruction"]
                        if evidence["file"] == "instruction"
                        else by_file.get(evidence["file"], "")
                    )
                    quote = evidence["quote"]
                    normalized_text = " ".join(evidence_text.split())
                    if (
                        evidence["file"] == "instruction"
                        and " ".join(quote.split()) not in normalized_text
                        and " ".join(quote.rstrip(".").split()) in normalized_text
                    ):
                        evidence["model_quote"] = quote
                        evidence["quote"] = quote.rstrip(".")
                        evidence["alignment"] = (
                            "recovered source clause by removing model-added "
                            "terminal period"
                        )
                    if (
                        not evidence_text
                        or " ".join(evidence["quote"].split()) not in normalized_text
                    ):
                        rejected_evidence.append(
                            {
                                **evidence,
                                ("reason"): (
                                    "quotation not present verbatim in supplied source"
                                ),
                            }
                        )
                    else:
                        verified_evidence.append(evidence)
                row["evidence"] = verified_evidence
                row["rejected_evidence"] = rejected_evidence
                if row["basis"] == "repository" and not any(
                    e["file"] in by_file for e in row["evidence"]
                ):
                    raise ValueError("repository decision lacks evidence")
                if (
                    row["basis"] in {"assumption", "unresolved"}
                    and not row["alternatives"]
                ):
                    raise ValueError("uncertain policy lacks alternatives")
                row["rule"] = row.pop("behavior")
                row["id"] = f"a{index:03}"
            return raw

        rec = recorder(arm, _client("deepinfra", DEFAULT_MODEL, 180, 4096))
        result = rec.call(
            f"decisions-v2-{group}",
            ANALYSIS_PROMPT,
            {
                **task,
                "routed_inventory": inv,
                "focus_operations": operations,
                "repository_files": provided,
            },
            ANALYSIS_SCHEMA,
            validate,
        )
        write_json(HERE / arm / f"{group}.json", result)
        return arm, group, result, rec.calls

    jobs = [
        (arm, group, operations, paths)
        for arm in ("instruction-only", "repository-grounded")
        for group, operations, paths in GROUPS
    ]
    results = []
    failures = []
    with ThreadPoolExecutor(max_workers=3) as pool:
        pending = {pool.submit(run_group, job): job for job in jobs}
        for future in as_completed(pending):
            try:
                results.append(future.result())
            except RuntimeError as exc:
                job = pending[future]
                failures.append({"arm": job[0], "group": job[1], "error": str(exc)})
    write_json(HERE / "analysis-failures.json", failures)
    results.sort(
        key=lambda r: (r[0], next(i for i, g in enumerate(GROUPS) if g[0] == r[1]))
    )
    for arm in ("instruction-only", "repository-grounded"):
        decisions: list[dict[str, Any]] = []
        receipts = []
        for result_arm, group, result, calls in results:
            if result_arm != arm:
                continue
            receipts.extend(calls)
            for row in result["decisions"]:
                row["id"] = f"a{len(decisions) + 1:03}"
                row["group"] = group
                decisions.append(row)
        write_json(
            HERE / arm / "analysis.json",
            {"objective": results[0][2]["objective"], "decisions": decisions},
        )
        write_json(HERE / arm / "receipts.json", receipts)
        print(f"{arm}: {len(decisions)} decisions", flush=True)


def generate_prompt(arm: str) -> None:
    analysis = load(f"{arm}/analysis.json")
    ids = {row["id"] for row in analysis["decisions"]}

    def validate(raw: dict[str, Any]) -> dict[str, Any]:
        covered = set()
        for section in (
            "requirements",
            "constraints",
            "relevant_context",
            "acceptance_criteria",
        ):
            for row in raw[section]:
                if not set(row["decision_ids"]) <= ids or not row["text"].strip():
                    raise ValueError("unknown decision or empty text")
                if section != "acceptance_criteria":
                    covered.update(row["decision_ids"])
        fallbacks = []
        for decision in analysis["decisions"]:
            if decision["id"] in covered:
                continue
            basis = decision["basis"]
            section = (
                "relevant_context"
                if basis == "unresolved"
                else "constraints"
                if basis == "repository"
                else "requirements"
            )
            prefix = (
                "Unresolved choice: "
                if basis == "unresolved"
                else "Chosen default: "
                if basis == "assumption"
                else ""
            )
            text = prefix + decision["rule"]
            if basis == "unresolved" and decision["alternatives"]:
                text += " Alternatives: " + "; ".join(decision["alternatives"]) + "."
            raw[section].append({"decision_ids": [decision["id"]], "text": text})
            fallbacks.append(
                {
                    "decision_id": decision["id"],
                    "section": section,
                    ("reason"): (
                        "model omitted decision; materialized from existing analysis "
                        "without another model call"
                    ),
                }
            )
        raw["assembly_fallbacks"] = fallbacks
        return raw

    rec = recorder(arm)
    raw = rec.call(
        "six-sections",
        """Render the supplied operation analysis
into the six requested sections. Requirements state concrete behavior,
constraints preserve compatibility, context explains relevant existing behavior
and unresolved choices, acceptance criteria give discriminating observable
conditions/examples. Resolve no new policies, and add no decisions absent from
the analysis. Label assumptions as chosen defaults and unresolved decisions as
open choices; do not disguise either as established requirements. Group related
acceptance scenarios to reduce repetition, while preserving every behavioral
decision. Objective describes intended behavior. No arbitrary length target.
Verification should use relevant repository checks and report results. Powdrr
owns branching/committing; do not instruct the coding worker to do those.
Respect resolution_priority when supplied: focused policy statements supersede
earlier broad statements for the same topic; cite the superseded IDs on the
merged statement rather than repeating contradictory rules. If the focused
policy is unresolved, present it as an open choice, not a mandatory rule. Do
not block prompt generation or demand a user response to those choices.
Return requested JSON only.""",
        {"instruction": load("input.json")["instruction"], "analysis": analysis},
        PROMPT_SCHEMA,
        validate,
    )
    write_json(HERE / arm / "prompt-sections.json", raw)
    parts = ["# Objective", "", raw["objective"], ""]
    for key, title in (
        ("requirements", "Requirements"),
        ("constraints", "Constraints"),
        ("relevant_context", "Relevant context"),
        ("acceptance_criteria", "Acceptance criteria"),
    ):
        parts += [f"# {title}", ""]
        parts += [f"{i}. {row['text']}" for i, row in enumerate(raw[key], 1)] or [
            "None specified."
        ]
        parts.append("")
    parts += ["# Verification and completion", ""]
    parts += [f"- {text}" for text in raw["verification"]]
    (HERE / arm / "prompt.md").write_text("\n".join(parts) + "\n")
    write_json(HERE / arm / "prompt-receipts.json", rec.calls)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=["prepare", "analyze", "prompt"])
    parser.add_argument("--task-dir", type=Path)
    parser.add_argument("--base-dir", type=Path)
    parser.add_argument("--arm", default="repository-grounded")
    args = parser.parse_args()
    if args.phase == "prepare":
        prepare(args.task_dir, args.base_dir)
    elif args.phase == "analyze":
        analyze()
    else:
        generate_prompt(args.arm)
