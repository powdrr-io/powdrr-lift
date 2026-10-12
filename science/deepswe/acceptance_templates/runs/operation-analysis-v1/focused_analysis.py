"""Generic semantic dimensions, asked one at a time; no solution/oracle input."""

import importlib.util
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from science.deepswe.acceptance_templates.cli import DEFAULT_MODEL, _client
from science.deepswe.acceptance_templates.common import (
    IDS,
    TEXT,
    Recorder,
    array_schema,
    choice_schema,
    object_schema,
    write_json,
)

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location(
    "operation_runner", HERE / "run_experiment.py"
)
if spec is None or spec.loader is None:
    raise RuntimeError("could not load experiment runner")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)

# These questions instantiate the generic dimensions from the original
# analysis request for API names in the instruction. No target answer is given.
# Question discovery itself is manual and is not a blinded/automated result.
QUESTIONS = [
    (
        "entity",
        "set precondition",
        (
            "What should entity.set(aspect, data) do if only some constituent traits "
            "are present? Distinguish what existing code establishes from a chosen "
            "new composite policy."
        ),
    ),
    (
        "entity",
        "set notifications",
        (
            "For a constituent receiving recognized fields through aspect set, does "
            "writing the same value as before still notify? What behavior should the "
            "existing triggerChanged argument have?"
        ),
    ),
    (
        "entity",
        "unknown and omitted fields",
        (
            "What should aspect set and configured add do with unknown fields and "
            "omitted fields? Establish which existing field-accessor contract "
            "applies."
        ),
    ),
    (
        "entity",
        "read copy semantics",
        (
            "What should merged aspect get/readEach data mean for top-level "
            "assignments and nested object/array references? Specify whether each "
            "writes through or is copied, and why."
        ),
    ),
    (
        "query-data",
        "callback slots",
        (
            "How should directly supplied aspects, ordinary data traits, ordinary "
            "tags and Not modifiers map to readEach/updateEach callback data slots? "
            "Include a tag-only aspect."
        ),
    ),
    (
        "query-data",
        "update equality",
        (
            "For aspect updateEach, what comparison should determine a changed "
            "constituent? Address equal primitive values versus identical or "
            "separately allocated nested references."
        ),
    ),
    (
        "query-data",
        "notification options",
        (
            "What should updateEach changeDetection options do for aspect queries? "
            "Separate committing writes from suppressing/detecting notifications."
        ),
    ),
    (
        "query-data",
        "callback destruction",
        (
            "If an updateEach callback destroys its entity, should aspect writes or "
            "notifications be committed for that entity? What original behavior "
            "should be inherited?"
        ),
    ),
    (
        "query-data",
        "notification timing",
        (
            "At what point relative to callback iteration should aspect updateEach "
            "dispatch constituent change notifications? What original behavior "
            "applies?"
        ),
    ),
    (
        "subscriptions",
        "subscription notification unit",
        (
            "What unit of event should aspect onChange use when one aspect operation "
            "changes more than one constituent: constituent notifications or one "
            "notification for the whole operation? State what is supported versus "
            "chosen."
        ),
    ),
    (
        "construction",
        "flattening identity",
        (
            "How should repeated occurrences of the same trait through direct/nested "
            "aspect inputs affect the flattened traits list, its order and the "
            "minimum constituent count? Separate overlap of distinct trait fields "
            "from duplicate trait identity."
        ),
    ),
    (
        "construction",
        "public metadata",
        (
            "What public type and mutability contract should aspect id, traits and "
            "schema have? Distinguish readonly property assignment, top-level "
            "container freezing and nested value freezing."
        ),
    ),
    (
        "modifiers",
        "Changed completeness",
        (
            "Should Changed(aspect) match an entity with a changed constituent but "
            "another constituent missing? State the matching predicate and evidence."
        ),
    ),
    (
        "modifiers",
        "Or grouping",
        (
            "When an aspect is passed to Or alongside a regular trait, what is the "
            "exact logical predicate over constituent presence? State whether "
            "expansion preserves a grouped conjunction or creates separate "
            "alternatives, and its evidence or default status."
        ),
    ),
    (
        "subscriptions",
        "unsubscribe",
        (
            "What should aspect subscription methods return, and what must invoking "
            "that return value detach? Establish whether repeated unsubscribe calls "
            "have an effect."
        ),
    ),
    (
        "subscriptions",
        "reset lifecycle",
        (
            "What should world.reset do to aspect query results, subsequent "
            "constituent registration and subscription lifetimes? Preserve the "
            "original world's reset conventions."
        ),
    ),
]

SCHEMA = object_schema(
    behavior={
        "type": "string",
        ("description"): (
            "Concrete chosen behavior answering the question, never a category label."
        ),
    },
    basis=choice_schema("instruction", "repository", "assumption", "unresolved"),
    evidence=array_schema(object_schema(file=TEXT, quote=TEXT)),
    alternatives=IDS,
    rationale=TEXT,
)

PROMPT = """Resolve exactly one behavioral decision for the requested feature.
Use only the original instruction and supplied original repository excerpts.
Read the actual counterpart code rather than restating the instruction.
State the behavior a coding agent should implement in one concrete sentence.
Preserve the scope and conditions of the instruction; do not invent extra API
arguments or restrictions. Existing code may settle inherited behavior but
does not prove an arbitrary new composite policy. instruction means expressly
required or necessarily implied; repository means an inherited contract with
exact code evidence; assumption is an explicit chosen default with alternatives;
unresolved means there is no justified choice. Give no more than the necessary
few lines of evidence, one short rationale, and alternatives only if uncertain.
Return the requested JSON only. The behavior field contains behavior, not the
evidence category. Do not enumerate additional decisions or acceptance criteria."""


def main() -> None:
    task = runner.load("input.json")
    source = runner.load("repository-evidence.json")["files"]
    path_groups = {g[0]: g[2] for g in runner.GROUPS}

    def run(
        job: tuple[int, tuple[str, str, str]],
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        index, (group, operation, question) = job
        files = [
            f
            for f in source
            if f["file"].removeprefix("packages/core/src/") in path_groups[group]
        ]
        rec = Recorder(
            _client("deepinfra", DEFAULT_MODEL, 120, 2048),
            HERE / "calls/focused",
            provider="deepinfra",
            model=DEFAULT_MODEL,
            resume=True,
            repairs=1,
        )

        def validate(raw: dict[str, Any]) -> dict[str, Any]:
            if len(raw["behavior"].split()) < 5:
                raise ValueError("behavior is not a concrete statement")
            by_file = {f["file"]: f["text"] for f in files}
            by_file["instruction"] = task["instruction"]
            good, rejected = [], []
            for e in raw["evidence"]:
                if e["file"] in by_file and " ".join(e["quote"].split()) in " ".join(
                    by_file[e["file"]].split()
                ):
                    good.append(e)
                else:
                    rejected.append(e)
            raw["evidence"], raw["rejected_evidence"] = good, rejected
            if raw["basis"] == "repository" and not any(
                e["file"] != "instruction" for e in good
            ):
                raw["basis"] = "unresolved"
                raw["rationale"] += (
                    " Repository evidence could not be verified; original "
                    "evidence is retained as rejected."
                )
            return raw

        raw = rec.call(
            f"question-{index:02}",
            PROMPT,
            {
                "instruction": task["instruction"],
                "question": question,
                "repository_files": files,
            },
            SCHEMA,
            validate,
        )
        result = {
            "id": f"f{index:03}",
            "operation": operation,
            "rule": raw.pop("behavior"),
            "question": question,
            "group": group,
            "source_ids": [],
            **raw,
        }
        write_json(HERE / "focused" / f"question-{index:02}.json", result)
        return result, rec.calls

    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(run, enumerate(QUESTIONS, 1)))
    write_json(HERE / "focused/decisions.json", {"decisions": [r[0] for r in results]})
    write_json(HERE / "focused/receipts.json", [c for r in results for c in r[1]])
    baseline = runner.load("repository-grounded/analysis.json")
    combined = []
    for row in baseline["decisions"]:
        row = dict(row)
        row["id"] = "g" + row["id"][1:]
        combined.append(row)
    combined += [r[0] for r in results]
    write_json(
        HERE / "focused/analysis.json",
        {
            ("objective"): (
                "Add composite trait aspects with unified entity, query and "
                "subscription operations."
            ),
            ("resolution_priority"): (
                "Focused decisions override earlier broad statements about the same "
                "policy. Assumptions remain labeled; unresolved choices are not "
                "promoted to established obligations."
            ),
            "decisions": combined,
        },
    )


if __name__ == "__main__":
    main()
