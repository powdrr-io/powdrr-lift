"""Render bounded decisions directly; no LLM contract or scenario synthesis."""

from __future__ import annotations

import hashlib
import json
import os
import tarfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from jsonschema import ValidationError, validate

from powdrr_lift.workrr.jev_classifier import JevSemanticClassifierClient
from science.deepswe.acceptance_templates.cli import DEFAULT_MODEL, _client
from science.deepswe.acceptance_templates.common import (
    Recorder,
    choice_schema,
    digest,
    object_schema,
    write_json,
)
from science.deepswe.acceptance_templates.production_analysis import JevPromptAdapter

HERE = Path(__file__).resolve().parent
PREVIOUS = HERE.parent / "narrow-questions-v1"

# Manual development questions. Their selected meanings and scenarios are frozen
# before calls. This is a rendering experiment, not automatic question discovery.
SPECS: list[dict[str, Any]] = [
    {
        "id": "callback-slot",
        "case": "callback-grouping",
        "reuse": True,
        "question": (
            "Does nested-aspect flattening replace readEach's merged aspect data "
            "with separate constituent callback objects?"
        ),
        "options": {
            "merged": {
                "meaning": "Keep one merged callback object for the supplied aspect.",
                "statement": (
                    "A directly supplied aspect contributes one merged readEach "
                    "callback data object, including when constructed from "
                    "nested aspects."
                ),
                "scenario": (
                    "Given data traits A={x:1}, B={y:2}, C={z:3}, a complete entity "
                    "and Outer=createAspect(createAspect(A,B),C), readEach on a "
                    "query with Outer as its only parameter supplies state with "
                    "one element: {x:1,y:2,z:3}."
                ),
            },
            "expanded": {
                "meaning": "Expand constituents into separate callback data objects.",
                "statement": (
                    "A nested aspect contributes a separate readEach callback "
                    "data object for each constituent trait."
                ),
                "scenario": (
                    "Given data traits A={x:1}, B={y:2}, C={z:3}, a complete entity "
                    "and Outer=createAspect(createAspect(A,B),C), readEach on a "
                    "query with Outer as its only parameter supplies three state "
                    "elements: {x:1}, {y:2}, and {z:3}."
                ),
            },
            "unknown": {"meaning": "The sources do not settle callback grouping."},
        },
    },
    {
        "id": "aspect-set-equality",
        "case": "set-notifications",
        "question": (
            "For the new aspect set operation, should a supplied equal-value field "
            "with triggerChanged=true retain existing regular-trait set notification "
            "behavior, or introduce an inequality guard like updateEach?"
        ),
        "options": {
            "retain_set": {
                ("meaning"): (
                    "Retain regular-trait setter notifications for equal writes."
                ),
                "statement": (
                    "Setting a supplied constituent field to its existing value "
                    "still triggers that constituent's change notification when "
                    "triggerChanged=true."
                ),
                "scenario": (
                    "Given an entity with every constituent of aspect(A,B), A.x=1, "
                    "and an A change subscriber registered after setup, setting "
                    "the aspect with {x:1} and triggerChanged=true invokes that "
                    "subscriber once and leaves A.x equal to 1."
                ),
            },
            "inequality_guard": {
                ("meaning"): (
                    "Introduce an inequality guard for aspect setter notifications."
                ),
                "statement": (
                    "Setting a supplied constituent field to its existing value "
                    "does not trigger that constituent's change notification."
                ),
                "scenario": (
                    "Given an entity with every constituent of aspect(A,B), A.x=1, "
                    "and an A change subscriber registered after setup, setting "
                    "the aspect with {x:1} and triggerChanged=true invokes that "
                    "subscriber zero times and leaves A.x equal to 1."
                ),
            },
            "unknown": {
                "meaning": "The sources do not settle notification inheritance."
            },
        },
    },
    {
        "id": "metadata-assignment",
        "case": "immutability",
        "question": (
            "Should the new aspect's exposed id, traits and schema properties "
            "prevent reassignment, following the existing public metadata pattern?"
        ),
        "options": {
            "protected": {
                "meaning": "Expose non-reassignable metadata properties.",
                ("statement"): (
                    "The aspect's id, traits and schema properties cannot be "
                    "reassigned."
                ),
                "scenario": (
                    "Given a created aspect and saved values of its id, traits "
                    "and schema, attempts to assign replacement values do not "
                    "change those property values; assignment may throw in strict mode."
                ),
            },
            "writable": {
                "meaning": "Expose reassignable metadata properties.",
                ("statement"): (
                    "The aspect's id, traits and schema properties can be reassigned."
                ),
                "scenario": (
                    "Given a created aspect, assigning replacement id, traits "
                    "and schema values succeeds and property reads return the "
                    "replacements."
                ),
            },
            "unknown": {"meaning": "The sources do not settle metadata reassignment."},
        },
    },
    {
        "id": "traits-container",
        "case": "immutability",
        "question": (
            "Does the instruction or an established existing contract require "
            "protecting the new aspect's exposed traits array against adding, "
            "removing or replacing its entries after construction? Decide array "
            "mutation separately from property reassignment and nested freezing."
        ),
        "options": {
            "protected": {
                ("meaning"): (
                    "Protect the exposed traits array against structural mutation."
                ),
                "statement": (
                    "The exposed traits array cannot have entries added, removed "
                    "or replaced after aspect creation."
                ),
                "scenario": (
                    "Given an aspect created from A and B, attempts to push C, "
                    "remove an entry or replace traits[0] leave the exposed "
                    "array's contents and order unchanged; mutation may throw."
                ),
            },
            "mutable": {
                "meaning": "The exposed traits array permits structural mutation.",
                ("statement"): (
                    "The exposed traits array allows entries to be added, removed or "
                    "replaced."
                ),
                "scenario": (
                    "Given an aspect created from A and B, pushing C into its "
                    "exposed traits array succeeds and changes its length to three."
                ),
            },
            "unknown": {
                "meaning": "The sources do not settle traits-array protection."
            },
        },
    },
    {
        "id": "schema-container",
        "case": "immutability",
        "question": (
            "Does the instruction or an established existing contract require "
            "protecting the new aspect's exposed merged schema against adding, "
            "removing or replacing its top-level fields? Decide top-level schema "
            "mutation separately from nested-value freezing."
        ),
        "options": {
            "protected": {
                ("meaning"): (
                    "Protect the merged schema's top-level fields from mutation."
                ),
                "statement": (
                    "Top-level fields of the exposed merged schema cannot be "
                    "added, removed or reassigned after aspect creation."
                ),
                "scenario": (
                    "Given an aspect with schema fields x and y, attempts to add "
                    "schema.z, delete schema.x or replace schema.x leave the "
                    "exposed schema's top-level fields and values unchanged; "
                    "mutation may throw."
                ),
            },
            "mutable": {
                "meaning": "The merged schema's top-level fields permit mutation.",
                ("statement"): (
                    "The exposed merged schema permits adding, removing or "
                    "reassigning fields."
                ),
                "scenario": (
                    "Given an aspect with schema fields x and y, assigning a new "
                    "schema.z succeeds and the exposed schema contains z."
                ),
            },
            "unknown": {
                "meaning": "The sources do not settle top-level schema protection."
            },
        },
    },
    {
        "id": "nested-freezing",
        "case": "immutability",
        "question": (
            "Should creating an aspect recursively freeze nested schema values, "
            "or preserve the existing nested values without recursively freezing "
            "them? Distinguish recursive freezing from shallow container protection."
        ),
        "options": {
            "preserve_nested": {
                ("meaning"): (
                    "Preserve nested schema references without recursive freezing."
                ),
                "statement": (
                    "Aspect creation retains nested schema value references and "
                    "does not recursively freeze those values."
                ),
                "scenario": (
                    "Given a constituent schema field whose value is a mutable "
                    "object N, creating an aspect leaves that merged schema field "
                    "equal to N by identity and does not freeze N."
                ),
            },
            "recursive": {
                ("meaning"): (
                    "Recursively freeze nested schema values at aspect creation."
                ),
                ("statement"): (
                    "Aspect creation recursively freezes nested schema values."
                ),
                "scenario": (
                    "Given a constituent schema field whose value is a mutable "
                    "object N, creating an aspect makes the corresponding nested "
                    "schema value frozen."
                ),
            },
            "unknown": {
                "meaning": "The sources do not settle recursive schema freezing."
            },
        },
    },
]


def load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f"expected an object: {path}")
    return value


def render(row: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
    """Render only a consensus choice's frozen meaning, never model explanation."""
    flash_choice = row["flash"]["choice"]
    jev_choice = row["jev"]["choice"]
    if flash_choice != jev_choice or flash_choice == "unknown":
        return {
            **row,
            "status": "unresolved",
            "statement": None,
            "scenario": None,
        }
    option = spec["options"][flash_choice]
    return {
        **row,
        "status": "rendered",
        "statement": option["statement"],
        "scenario": option["scenario"],
    }


def run(spec: dict[str, Any]) -> dict[str, Any]:
    case = spec["case"]
    original = load(PREVIOUS / case / "input.json")
    decision_id = spec["id"]
    # Classification sees choices, not the prewritten render/scenario strings.
    payload = {
        "sources": original["sources"],
        "question": spec["question"],
        "options": {key: value["meaning"] for key, value in spec["options"].items()},
    }
    write_json(HERE / "inputs" / f"{decision_id}.json", payload)
    schema = object_schema(choice=choice_schema(*spec["options"]))

    def checked(value: dict[str, Any]) -> dict[str, Any]:
        try:
            validate(value, schema)
        except ValidationError as exc:
            raise ValueError(exc.message) from None
        return value

    if spec.get("reuse"):
        prior = load(PREVIOUS / "atomic" / f"{case}.json")
        row = {
            "id": decision_id,
            "flash": {"choice": prior["observation"]["choice"]},
            "jev": prior["independent_jev"],
            "provenance": {
                "type": "saved_decisions",
                "artifact": str(
                    (PREVIOUS / "atomic" / f"{case}.json").relative_to(HERE.parent)
                ),
                "sha256": digest(prior),
            },
            "calls": [],
        }
        checked(row["flash"])
        checked(row["jev"])
        result = render(row, spec)
        write_json(HERE / "decisions" / f"{decision_id}.json", result)
        return result

    flash = Recorder(
        _client("deepinfra", DEFAULT_MODEL, 120, 256),
        HERE / "calls" / decision_id / "flash",
        provider="deepinfra",
        model=DEFAULT_MODEL,
        resume=True,
        repairs=0,
    )
    adapter = JevPromptAdapter(
        JevSemanticClassifierClient(flash.client, fail_closed=True)
    )
    jev = Recorder(
        adapter,
        HERE / "calls" / decision_id / "jev",
        provider="typesafe.ai",
        model=os.environ.get("SYSTEM_ONE_MODEL", "jev-latest"),
        resume=True,
        repairs=0,
    )
    try:
        answer = flash.call(
            "choose",
            "Choose the most faithful behavior for exactly the supplied question "
            "using the original instruction and original code. Preserve ordinary "
            "instruction meanings and established behavior of the corresponding "
            "operation. Do not infer recursive freezing from readonly properties "
            "or shallow freezing. An analogous API is not proof of a new policy; "
            "choose unknown if the available sources do not settle the choice. "
            "Return only the selected choice, with no prose synthesis.",
            payload,
            schema,
            checked,
        )
        opinion = jev.call(
            "independent-choice",
            "Question:\n" + spec["question"] + "\nChoose the most faithful option "
            "from the original sources. Preserve established corresponding "
            "operation behavior. Choose unknown when the sources do not settle "
            "a new policy. Do not treat shallow freezing as recursive freezing.",
            payload,
            schema,
            checked,
        )
        row = {
            "id": decision_id,
            "flash": answer,
            "jev": opinion,
            "provenance": {"type": "independent_choices"},
            "calls": flash.calls + jev.calls,
        }
        result = render(row, spec)
        if result["status"] == "rendered":
            # Check meaning preservation, not truth or coverage of the solution.
            review_schema = object_schema(
                verdict=choice_schema("preserved", "adds_behavior", "insufficient")
            )

            def checked_review(value: dict[str, Any]) -> dict[str, Any]:
                validate(value, review_schema)
                return value

            result["scenario_review"] = jev.call(
                "scenario-preservation",
                "Question:\nDoes the concrete scenario faithfully exercise only "
                "the selected behavior? Choose preserved if it has a concrete "
                "setup/action/result without adding another behavior; adds_behavior "
                "if it adds obligations; insufficient if it does not exercise "
                "the behavior. This checks rendering, not source support.",
                {
                    "question": spec["question"],
                    "selected_meaning": spec["options"][answer["choice"]]["meaning"],
                    "statement": result["statement"],
                    "scenario": result["scenario"],
                },
                review_schema,
                checked_review,
            )
            result["calls"] = flash.calls + jev.calls
            if result["scenario_review"]["verdict"] != "preserved":
                result["rejected_scenario"] = result.pop("scenario")
                result["scenario"] = None
        write_json(HERE / "decisions" / f"{decision_id}.json", result)
        print(
            decision_id,
            answer["choice"],
            opinion["choice"],
            result["status"],
            flush=True,
        )
        return result
    finally:
        adapter.close()


def ablation() -> None:
    """Reuse the original decisions without smuggling in a feature transfer."""
    statements = []
    for case in ("immutability", "callback-grouping", "set-notifications"):
        prior = load(PREVIOUS / "atomic" / f"{case}.json")
        choice = prior["observation"]["choice"]
        statements.append(
            {
                "case": case,
                "question": prior["question"]["question"],
                "choice": choice,
                "statement": prior["question"]["options"][choice],
                "scope": "feature" if case == "callback-grouping" else "existing_code",
                "input_sha256": digest(prior),
            }
        )
    write_json(HERE / "frozen-choice-ablation.json", {"statements": statements})


def main() -> None:
    if not (HERE / "manifest.json").exists():
        write_json(
            HERE / "manifest.json",
            {
                "specs": SPECS,
                "specs_sha256": digest(SPECS),
                ("boundary"): (
                    "Original instruction/code only. No oracle/patches in "
                    "classification."
                ),
                ("limitation"): (
                    "Manual questions, choices and renderings; known development task."
                ),
            },
        )
    else:
        if load(HERE / "manifest.json")["specs_sha256"] != digest(SPECS):
            raise ValueError("frozen experiment definitions changed")
    ablation()

    def collect(spec: dict[str, Any]) -> dict[str, Any]:
        try:
            return run(spec)
        except (RuntimeError, ValueError) as exc:
            result = {"id": spec["id"], "status": "failed", "error": str(exc)}
            write_json(HERE / "decisions" / f"{spec['id']}.json", result)
            return result

    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(collect, SPECS))
    write_json(HERE / "results.json", {"decisions": results})
    # No model explanation or provider-generated prose enters this document.
    lines = ["# Focused prompt fragment", "", "## Requirements", ""]
    rendered = [row for row in results if row["status"] == "rendered"]
    for index, row in enumerate(rendered, 1):
        lines.append(f"{index}. {row['statement']}")
    lines += ["", "## Relevant context", ""]
    context_labels = {
        "immutability": "Existing createQuery freeze behavior",
        "set-notifications": "Existing setTraitForTrait notification guard",
    }
    for row in load(HERE / "frozen-choice-ablation.json")["statements"]:
        if row["scope"] == "existing_code":
            meaning = row["statement"].partition(": ")[2]
            lines.append(f"- {context_labels[row['case']]}: {meaning}")
    lines += ["", "## Acceptance criteria", ""]
    for row in rendered:
        if row.get("scenario"):
            lines.append(f"- {row['scenario']}")
    (HERE / "prompt-fragment.md").write_text("\n".join(lines) + "\n")
    write_json(
        HERE / "unresolved.json",
        {
            "decisions": [row for row in results if row["status"] != "rendered"],
            ("original_requirement_retained"): (
                "Each aspect exposes `id`, `traits`, and `schema`."
            ),
        },
    )
    checkpoints = []
    paths = sorted((HERE / "calls").glob("**/*.json"))
    with tarfile.open(HERE / "call-checkpoints.tar.gz", "w:gz") as archive:
        for path in paths:
            record = load(path)
            assert record["status"] != "running"
            archive.add(path, arcname=str(path.relative_to(HERE)))
            checkpoints.append(
                {
                    "file": str(path.relative_to(HERE)),
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "request_sha256": record["request_sha256"],
                    "receipt": record["receipt"],
                }
            )
    write_json(HERE / "call-checkpoints.json", {"checkpoints": checkpoints})


if __name__ == "__main__":
    main()
