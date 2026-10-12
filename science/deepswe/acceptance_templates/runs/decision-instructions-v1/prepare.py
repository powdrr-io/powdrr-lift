"""Freeze development/evaluation cases, original sources, labels and routing."""

from __future__ import annotations

import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from powdrr_lift.workrr.jev_classifier import JevSemanticClassifierClient
from powdrr_lift.workrr.procedrr import WorkrrProcedrrClient
from science.deepswe.acceptance_templates.cli import DEFAULT_MODEL, _client
from science.deepswe.acceptance_templates.common import (
    Recorder,
    digest,
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
TASKS = Path("/Users/gregory/code/powdrr-deep-swe/tasks")
TASK_INFO: dict[str, dict[str, Any]] = {
    "koota-composite-trait-aspects": {
        "split": "development",
        "base": "/private/tmp/koota-aspect-analysis-base-20261011",
        "sha": "9c434858b2b522002f8c5eb4a554fa8836a7cf3c",
        "excerpts": [
            ("packages/core/src/trait/types.ts", 13, 18),
            ("packages/core/src/trait/trait.ts", 47, 93),
            ("packages/core/src/trait/trait.ts", 403, 423),
            ("packages/core/src/query/query.ts", 444, 469),
            ("packages/core/src/query/query-result.ts", 22, 51),
            ("packages/core/src/query/query-result.ts", 245, 282),
            ("packages/core/src/world/world.ts", 315, 373),
        ],
    },
    "gql-incremental-graphql-delivery": {
        "split": "evaluation",
        "base": "/private/tmp/decision-gql-base-20261011",
        "sha": "f07c89f8f065010a36b4263eded209b2b1d37063",
        "excerpts": [
            ("gql/client.py", 1395, 1450),
            ("gql/transport/websockets_protocol.py", 286, 314),
        ],
    },
    "bandit-structured-nosec-directives": {
        "split": "evaluation",
        "base": "/private/tmp/decision-bandit-base-20261011",
        "sha": "b46fa3a2723635aa29cc012538df4867ac2ac006",
        "excerpts": [
            ("bandit/core/manager.py", 478, 504),
            ("bandit/core/tester.py", 78, 97),
            ("bandit/core/tester.py", 127, 154),
            ("bandit/core/utils.py", 219, 227),
            ("bandit/core/utils.py", 393, 401),
        ],
    },
}


def main() -> None:
    cases: list[dict[str, Any]] = []
    labels: list[dict[str, Any]] = []

    def add(
        case_id: str,
        task: str,
        kind: str,
        current: str,
        revised: str,
        options: dict[str, str],
        expected: str,
        evidence: str,
    ) -> None:
        # Label information is written separately and excluded from model inputs.
        options = {
            key: options[key]
            for key in sorted(options, key=lambda k: digest(case_id + k))
        }
        cases.append(
            {
                "id": case_id,
                "task_id": task,
                "split": TASK_INFO[task]["split"],
                "kind": kind,
                "current_question": current,
                "revised_question": revised,
                "options": options,
            }
        )
        labels.append(
            {"id": case_id, "expected": expected, "evidence": evidence, "kind": kind}
        )

    k = "koota-composite-trait-aspects"
    add(
        "k-assignment",
        k,
        "inferred",
        (
            "Should exposed id, traits and schema properties prevent "
            "reassignment, following the existing public metadata pattern?"
        ),
        (
            "Which property-assignment policy best preserves the aspect's "
            "constructed identity and public metadata contract, considering "
            "corresponding existing APIs?"
        ),
        {
            "protected": (
                "The aspect's id, traits and schema properties cannot be reassigned."
            ),
            "writable": (
                "The aspect's id, traits and schema properties allow reassignment."
            ),
            "unknown": "The sources do not settle property reassignment.",
        },
        "protected",
        "solution.patch:88-107 non-writable property descriptors",
    )
    add(
        "k-traits-container",
        k,
        "inferred",
        (
            "Does the instruction or an established existing contract require "
            "protecting the exposed traits array against adding, removing or "
            "replacing entries?"
        ),
        (
            "Which traits-array mutation policy best preserves the constructed "
            "aspect's constituent definition and its relationship with field "
            "ownership and query membership? Consider whether a reconfiguration "
            "capability is requested."
        ),
        {
            "protected": (
                "The exposed traits array cannot have entries added, removed or "
                "replaced after creation."
            ),
            "mutable": (
                "The exposed traits array allows entries to be added, removed "
                "or replaced after creation."
            ),
            "unknown": "The sources do not settle traits-array mutation.",
        },
        "protected",
        "solution.patch:95-100 Object.freeze([...traits])",
    )
    add(
        "k-schema-container",
        k,
        "inferred",
        (
            "Does the instruction or an established existing contract require "
            "protecting the merged schema's top-level fields against mutation?"
        ),
        (
            "Which top-level schema mutation policy keeps the aspect's declared "
            "fields consistent with field ownership used by set and add? "
            "Consider the metadata's role separately from nested values."
        ),
        {
            "protected": (
                "The merged schema's top-level fields cannot be added, removed "
                "or reassigned after creation."
            ),
            "mutable": (
                "The merged schema permits adding, removing or reassigning "
                "top-level fields after creation."
            ),
            "unknown": "The sources do not settle top-level schema mutation.",
        },
        "protected",
        "solution.patch:102-107 Object.freeze({...schema})",
    )
    add(
        "k-nested-freeze",
        k,
        "inferred",
        (
            "Should creating an aspect recursively freeze nested schema values, "
            "or does the instruction not settle this?"
        ),
        (
            "What recursive-freezing policy preserves existing constituent "
            "schema values without introducing a side effect on nested objects "
            "supplied by callers? Distinguish container protection from "
            "recursive mutation protection."
        ),
        {
            "preserve": (
                "Aspect creation does not recursively freeze previously mutable "
                "nested schema values."
            ),
            "recursive": "Aspect creation recursively freezes nested schema values.",
            "unknown": "The sources do not settle recursive schema freezing.",
        },
        "preserve",
        "solution.patch:103 shallow copy/freeze, no recursive walk",
    )
    add(
        "k-nested-reference",
        k,
        "inferred",
        (
            "Does the instruction explicitly require retaining nested schema "
            "value identity instead of copying nested objects?"
        ),
        (
            "Are merged schema entries references to existing constituent field "
            "definitions, or independently cloned definitions? Choose the "
            "policy most consistent with composing existing traits without "
            "changing their definitions."
        ),
        {
            "shared": (
                "Nested merged schema values retain their constituent schema "
                "references."
            ),
            "cloned": (
                "Nested merged schema values are independent deep copies of "
                "constituent schema values."
            ),
            "unknown": "The sources do not settle nested schema identity.",
        },
        "shared",
        "solution.patch:72 schema[key]=traitSchema[key], then {...schema}",
    )
    add(
        "k-callback-slot",
        k,
        "explicit",
        (
            "Does nested-aspect flattening replace readEach's merged data with "
            "separate constituent callback slots?"
        ),
        (
            "Which operation does nested-aspect flattening govern, and which "
            "callback data shape preserves readEach's separate promise to "
            "deliver merged aspect data?"
        ),
        {
            "merged": (
                "A directly supplied nested aspect contributes one merged "
                "readEach callback object."
            ),
            "expanded": (
                "A directly supplied nested aspect contributes separate "
                "readEach callback objects for each constituent."
            ),
            "unknown": "The sources do not settle nested callback grouping.",
        },
        "merged",
        "solution.patch:468-476 per-aspect slot mapping; 511-519 state[i]=merged",
    )
    add(
        "k-set-equality",
        k,
        "inherited",
        (
            "Should the new aspect setter inherit equal-value notifications "
            "from regular-trait set, or introduce an inequality guard like "
            "updateEach?"
        ),
        (
            "Which operation corresponds to distributing aspect set fields to "
            "their owners? Follow that operation's actual notification guard, "
            "keeping setter behavior separate from mutation-callback detection."
        ),
        {
            "notify": (
                "A supplied equal-value aspect set field still triggers its "
                "constituent notification when triggerChanged=true."
            ),
            "suppress": (
                "A supplied equal-value aspect set field suppresses its "
                "constituent notification even when triggerChanged=true."
            ),
            "unknown": "The sources do not settle equal-value aspect notifications.",
        },
        "notify",
        (
            "solution.patch:1095-1096 setTraitForTrait forwarding; base "
            "trait/trait.ts:406-423 no equality guard"
        ),
    )
    add(
        "k-callback-subject",
        k,
        "inherited",
        (
            "Does the instruction explicitly specify whether an aspect "
            "subscription callback receives the affected entity or merged data "
            "as its first argument?"
        ),
        (
            "What is the first argument in existing world subscription "
            "callbacks, and does the aspect feature request replace that "
            "callback subject? Choose the compatible first argument."
        ),
        {
            "entity": (
                "Aspect subscription callbacks receive the affected entity as "
                "their first argument."
            ),
            "data": (
                "Aspect subscription callbacks receive merged aspect data as "
                "their first argument."
            ),
            "unknown": "The sources do not settle the callback's first argument.",
        },
        "entity",
        (
            "solution.patch:1177,1218,1246 callback(entity,undefined); exact "
            "arity deliberately not tested here"
        ),
    )
    add(
        "k-unsupported-cap",
        k,
        "unsupported",
        (
            "Does this task require an application-level maximum of 32 or 256 "
            "aspect constituents?"
        ),
        (
            "Is a fixed maximum of 32 or 256 constituents part of the requested "
            "contract or necessary for its behavior, beyond the stated minimum?"
        ),
        {
            "cap32": "Aspect creation rejects more than 32 constituents.",
            "cap256": "Aspect creation rejects more than 256 constituents.",
            "unknown": (
                "The task establishes no application-level maximum constituent count."
            ),
        },
        "unknown",
        (
            "Instruction specifies minimum only; solution.patch constructor has "
            "no upper-limit guard"
        ),
    )

    g = "gql-incremental-graphql-delivery"
    add(
        "g-result-snapshot",
        g,
        "inferred",
        (
            "Does the instruction explicitly require previous yielded data "
            "objects to remain unchanged when later payloads arrive?"
        ),
        (
            "Which data-lifetime policy preserves each yielded result as a "
            "coherent result for the payload that produced it, while "
            "accumulating later deliveries? Consider nested deferred and "
            "streamed updates."
        ),
        {
            "snapshot": (
                "Later payload processing leaves previously yielded result data "
                "unchanged, including nested objects and lists."
            ),
            "live": (
                "Previously yielded result data remains a live view that later "
                "payload processing mutates."
            ),
            "unknown": "The sources do not settle previously yielded data lifetime.",
        },
        "snapshot",
        "solution.patch:329,360,366 deep-copy accumulated result data",
    )
    add(
        "g-stream-position",
        g,
        "inferred",
        (
            "Does insertion start index mean stream items splice into the list "
            "and shift existing elements, or overwrite addressed positions?"
        ),
        (
            "Which interpretation of a stream start index preserves path-based "
            "addressing while concurrent deferred and streamed fields are "
            "merged: filling addressed positions or shifting existing positions?"
        ),
        {
            "positions": (
                "For list [A,B,C] and streamed [X] at start index 1, the result "
                "is [A,X,C]."
            ),
            "splice": (
                "For list [A,B,C] and streamed [X] at start index 1, the result "
                "is [A,X,B,C]."
            ),
            "unknown": "The sources do not settle stream index semantics.",
        },
        "positions",
        (
            "solution.patch:280-284 target_list[start_index+i]=item, padding "
            "gaps with None"
        ),
    )
    add(
        "g-extensions-lifetime",
        g,
        "explicit",
        (
            "When a later payload omits extensions, should earlier payload "
            "extensions remain in its yielded result?"
        ),
        (
            "Which fields have an accumulation contract and which are "
            "payload-local? Apply the stated extensions scope when a payload "
            "has no extensions field."
        ),
        {
            "payload_local": (
                "A payload without extensions yields extensions=None rather "
                "than retaining earlier extensions."
            ),
            "retain": (
                "A payload without extensions yields the previous payload's extensions."
            ),
            "unknown": "The sources do not settle omitted extensions.",
        },
        "payload_local",
        (
            "Instruction payload-specific extensions; solution.patch:355 "
            "response.get(extensions)"
        ),
    )
    add(
        "g-progress-only",
        g,
        "explicit",
        (
            "Does the instruction require yielding results for hasNext-only "
            "payloads and empty incremental arrays, or may they be skipped?"
        ),
        (
            "Is yielding governed by receiving a payload or by that payload "
            "changing data? Apply the explicit progress-payload requirement."
        ),
        {
            "yield": (
                "hasNext-only payloads and empty incremental arrays still yield "
                "results with the accumulated data."
            ),
            "skip": (
                "hasNext-only payloads and empty incremental arrays are skipped "
                "when they do not change data."
            ),
            "unknown": "The sources do not settle progress-only yields.",
        },
        "yield",
        (
            "Instruction explicit; solution.patch:366-383 constructs result for "
            "every incremental/progress payload"
        ),
    )
    add(
        "g-error-continuation",
        g,
        "explicit",
        (
            "Should the incremental generator retain existing session "
            "subscribe's TransportQueryError behavior for result errors?"
        ),
        (
            "Does the new operation's explicit error-continuation requirement "
            "override the corresponding existing subscription error behavior? "
            "Choose the observable incremental behavior."
        ),
        {
            "continue": (
                "Result errors are delivered on incremental results without "
                "halting subsequent items."
            ),
            "raise": (
                "Result errors raise TransportQueryError and terminate "
                "incremental delivery."
            ),
            "unknown": "The sources do not settle incremental error continuation.",
        },
        "continue",
        (
            "Instruction errors must not halt subsequent items; "
            "solution.patch:93 unconditional yield and 373-383 result.errors"
        ),
    )
    add(
        "g-errors-lifetime",
        g,
        "inferred",
        (
            "Does the instruction explicitly specify whether errors accumulate "
            "across result payloads?"
        ),
        (
            "Which result fields are explicitly accumulated, and what is the "
            "existing result scope for errors? Choose the errors policy that "
            "preserves per-delivery result meaning without introducing extra "
            "accumulation."
        ),
        {
            "payload_local": (
                "Each result contains only current payload and current "
                "incremental-item errors; earlier payload errors are not "
                "carried forward."
            ),
            "accumulate": "Each result accumulates errors from all previous payloads.",
            "unknown": "The sources do not settle error accumulation.",
        },
        "payload_local",
        "solution.patch:354 and 367-377 collects only current response/item errors",
    )
    add(
        "g-unsupported-cap",
        g,
        "unsupported",
        "Does the task require a maximum of 1024 or 4096 accumulated result items?",
        (
            "Is either numeric result-size ceiling requested or necessary for "
            "incremental delivery's stated contract?"
        ),
        {
            "cap1024": "Incremental results reject more than 1024 accumulated items.",
            "cap4096": "Incremental results reject more than 4096 accumulated items.",
            "unknown": "The task establishes no fixed accumulated-item ceiling.",
        },
        "unknown",
        "Instruction and solution contain no such application-level result-size cap",
    )

    b = "bandit-structured-nosec-directives"
    add(
        "b-none-selection",
        b,
        "explicit",
        (
            "Should none resolve to an empty legacy nosec set and therefore "
            "blanket suppression?"
        ),
        (
            "Does the new selector token none mean absence of a selector or "
            "selection of no tests? Preserve that semantic distinction when "
            "adapting legacy sentinel representation."
        ),
        {
            "no_effect": (
                "A structured directive with selector none applies no suppression."
            ),
            "blanket": (
                "A structured directive with selector none suppresses every test."
            ),
            "unknown": "The sources do not settle selector none.",
        },
        "no_effect",
        "Instruction none has no effect; solution.patch:343-344 returns None",
    )
    add(
        "b-unrecognized-selection",
        b,
        "inferred",
        (
            "When a nonempty new selector resolves no valid test IDs, should "
            "legacy empty-set blanket suppression be inherited?"
        ),
        (
            "Is a supplied nonempty selector that selects no enabled tests "
            "equivalent to an omitted selector? Choose the policy that "
            "preserves selection meaning while adapting the legacy "
            "representation."
        ),
        {
            "no_effect": (
                "A supplied nonempty structured selector resolving no known "
                "tests applies no suppression."
            ),
            "blanket": (
                "A supplied nonempty structured selector resolving no known "
                "tests suppresses all tests."
            ),
            "unknown": (
                "The sources do not settle nonempty selectors resolving no tests."
            ),
        },
        "no_effect",
        (
            "solution.patch:347-348 empty resolved selection->None;366 empty "
            "fallback->None"
        ),
    )
    add(
        "b-blanket-combination",
        b,
        "explicit",
        (
            "Should combining blanket and specific suppressions convert the "
            "empty blanket sentinel into only the specific test set?"
        ),
        (
            "What must dominate when two applicable suppressions combine? "
            "Distinguish the semantic blanket selection from its empty-set "
            "representation."
        ),
        {
            "blanket": (
                "Combining a blanket suppression and a specific suppression "
                "remains blanket suppression and counts as nosec."
            ),
            "specific": (
                "Combining a blanket suppression and a specific suppression "
                "suppresses only the specific tests and counts as skipped_tests."
            ),
            "unknown": "The sources do not settle blanket-plus-specific combination.",
        },
        "blanket",
        (
            "Instruction blanket dominates; solution.patch:719-720 returns "
            "set() when either applicable set empty"
        ),
    )
    add(
        "b-compound-header",
        b,
        "inferred",
        (
            "Does statement-wide suppression on a function-definition header "
            "extend across its full AST span, including independent statements "
            "in its body?"
        ),
        (
            "Which findings belong to the suppressed logical statement and "
            "which belong to separate nested statements? Apply statement-wide "
            "scope without turning header suppression into a new whole-suite "
            "region."
        ),
        {
            "header_only": (
                "Suppression on a compound statement header does not "
                "automatically suppress independent statements in its body."
            ),
            "whole_suite": (
                "Suppression on a compound statement header automatically "
                "suppresses all independent statements in its body."
            ),
            "unknown": "The sources do not settle compound-header scope.",
        },
        "header_only",
        "solution.patch:739-754 restricts compound statements to header line",
    )
    add(
        "b-nested-begin-line",
        b,
        "inferred",
        (
            "Is a nested nosec-begin directive line exempt from every active "
            "suppression because begin lines are not suppressed?"
        ),
        (
            "Does excluding a begin directive's own line apply only to that "
            "newly opened region or also cancel an already active outer region? "
            "Preserve the scope of the exclusion and combination rules."
        ),
        {
            "outer_applies": (
                "A nested begin directive's line can still be suppressed by an "
                "already active outer region; only the new region starts on the "
                "following line."
            ),
            "all_exempt": (
                "A nested begin directive's line is exempt from all active "
                "outer-region suppressions."
            ),
            "unknown": "The sources do not settle nested begin-line coverage.",
        },
        "outer_applies",
        (
            "solution.patch:normalize_begin event starts next line;212-230 "
            "region map applies existing stack on nested begin line"
        ),
    )
    add(
        "b-unsupported-cap",
        b,
        "unsupported",
        (
            "Does this task require a maximum nesting depth of 32 or 256 "
            "suppression regions?"
        ),
        (
            "Is either fixed nesting-depth ceiling requested or necessary for "
            "the region suppression contract?"
        ),
        {
            "cap32": "Region suppression rejects nesting deeper than 32 regions.",
            "cap256": "Region suppression rejects nesting deeper than 256 regions.",
            "unknown": "The task establishes no fixed region-nesting depth ceiling.",
        },
        "unknown",
        "Instruction and solution region stack establish no fixed nesting cap",
    )

    if (HERE / "cases.json").exists():
        if json.loads((HERE / "cases.json").read_text()) != {"cases": cases}:
            raise ValueError("frozen case definitions changed")
    else:
        write_json(HERE / "cases.json", {"cases": cases})
        write_json(
            HERE / "oracle.json",
            {
                "labels": labels,
                "authority": (
                    "Solution patch is the target; unsupported controls have no "
                    "requested/specified obligation."
                ),
                "reviewer": (
                    "Agent-authored, not human adjudication; known task and "
                    "manually designed questions."
                ),
            },
        )
    for task_id, info in TASK_INFO.items():
        instruction = (TASKS / task_id / "instruction.md").read_text()
        code = []
        for name, start, end in info["excerpts"]:
            path = Path(info["base"]) / name
            text = "\n".join(path.read_text().splitlines()[start - 1 : end])
            code.append({"file": name, "start": start, "end": end, "text": text})
        write_json(
            HERE / "sources" / f"{task_id}.json",
            {
                "task_id": task_id,
                "instruction": instruction,
                "source_spans": source_spans(instruction),
                "original_code": code,
            },
        )
    write_json(
        HERE / "manifest.json",
        {
            "cases_sha256": digest({"cases": cases}),
            "oracle_sha256": hashlib.sha256(
                (HERE / "oracle.json").read_bytes()
            ).hexdigest(),
            "tasks": TASK_INFO,
            "task_hashes": {
                task: {
                    name: hashlib.sha256((TASKS / task / name).read_bytes()).hexdigest()
                    for name in (
                        "instruction.md",
                        "solution/solution.patch",
                        "tests/test.patch",
                    )
                }
                for task in TASK_INFO
            },
            "boundary": (
                "Classifier inputs contain original sources/questions/choices "
                "only; labels and patch contents excluded."
            ),
            "limitation": (
                "Manual question discovery, retrieval and choice definitions. "
                "Evaluation repositories separated from development examples, "
                "but author knows their solutions; not an untouched holdout."
            ),
        },
    )

    def route(task_id: str) -> None:
        target = HERE / "routing" / f"{task_id}.json"
        if target.exists():
            return
        if task_id == k:
            prior = HERE.parent / "operation-analysis-v1/instruction-analysis.json"
            write_json(target, json.loads(prior.read_text()))
            return
        planning = _client("deepinfra", DEFAULT_MODEL, 120, 4096)
        adapter = JevPromptAdapter(
            WorkrrProcedrrClient(
                JevSemanticClassifierClient(planning, fail_closed=True),
                skills_dir=DESIGN_FLOW.parent,
            )
        )
        jev = Recorder(
            adapter,
            HERE / "calls/routing" / task_id / "jev",
            provider="typesafe.ai",
            model=os.environ.get("SYSTEM_ONE_MODEL", "jev-latest"),
            resume=True,
            repairs=0,
        )
        splitter = Recorder(
            WorkrrProcedrrClient(planning, skills_dir=DESIGN_FLOW.parent),
            HERE / "calls/routing" / task_id / "flash",
            provider="deepinfra",
            model=DEFAULT_MODEL,
            resume=True,
            repairs=0,
        )
        try:
            result = analyze_instruction(
                json.loads((HERE / "sources" / f"{task_id}.json").read_text()),
                jev_recorder=jev,
                split_recorder=splitter,
                judges=load_production_judges(),
            )
            write_json(target, result)
            print("routed", task_id, flush=True)
        finally:
            adapter.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(route, TASK_INFO))


if __name__ == "__main__":
    main()
