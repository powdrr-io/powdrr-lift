"""Construct setup and observation independently for each frozen requirement."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

import yaml

from powdrr_lift.workrr import WorkflowLLMClient
from procedrr import parse_and_validate
from science.deepswe.acceptance_templates.common import (
    load_json,
    object_schema,
    write_json,
)
from science.deepswe.acceptance_templates.repository_planning.refine import (
    RepairClient,
    ReviewOperations,
)

from .contracts import evaluate_recorded
from .contracts_workflow import (
    CHOICE,
    TEXT,
    judge,
    loop,
    op,
    ref,
)


class AtomicClient(RepairClient):
    @staticmethod
    def fallback(schema: Any) -> dict[str, Any]:
        if all(
            p.get("type") == "string" and not p.get("enum")
            for p in schema["properties"].values()
        ):
            return {k: "" for k in schema["properties"]}
        return RepairClient.fallback(schema)


class AtomicOperations(ReviewOperations):
    def __init__(
        self,
        setup: Path,
        output: Path,
        packet: dict[str, Any],
        generation: Path,
        evidence: Path,
    ) -> None:
        super().__init__(setup, output, packet, generation)
        self.evidence = evidence

    def __call__(self, tool: str, parameters: Any) -> Any:
        command = parameters["command"][0]
        if command == "focused_sources":
            requirement = parameters["requirement"]
            packet = load_json(self.evidence / "packets" / f"{requirement['id']}.json")
            dependencies = load_json(self.generation / "dependency-evidence.json")
            # A routed clause is often a narrow split child. Resolve shared API
            # facts from the full instruction so examples for that child can use
            # any directive explicitly introduced by the task.
            instruction = str(self.packet.get("instruction", ""))
            relevant_directives = {
                name: value
                for name, value in dependencies["directives"].items()
                if _directive_is_named(name, instruction)
            }
            text = requirement["text"].casefold()
            result_interface = (
                dependencies["types"]["ExecutionResult"]
                if "result" in text
                and any(
                    attr in text
                    for attr in (".data", ".errors", ".extensions", ".has_next")
                )
                else None
            )
            wire_interfaces = (
                {
                    k: v
                    for k, v in dependencies["types"].items()
                    if k.startswith("Formatted")
                }
                if any(
                    term in text
                    for term in ("http", "websocket", "multipart", "protocol")
                )
                else {}
            )
            return {
                "repository": packet["evidence"],
                "retrieval_record": {
                    k: packet[k]
                    for k in (
                        "query_terms",
                        "matching_symbols",
                        "truncated_candidates",
                        "ranked_candidates",
                        "selected",
                    )
                },
                "result_interface": result_interface,
                "wire_interfaces": wire_interfaces,
                "directives": relevant_directives,
                "directive_context_source": "full_instruction",
                "scope": (
                    "Existing APIs and dependency formats are evidence of "
                    "conventions. They do not mandate an implementation class for a "
                    "newly requested structural interface."
                ),
            }
        if command == "assemble_atomic_criterion":
            requirement = parameters["requirement"]
            return {
                **parameters["setup"],
                **parameters["observation"],
                **parameters["counterexample"],
                "id": f"{requirement['id']}-atomic",
            }
        if command == "record_atomic":
            row = {
                "requirement": parameters["requirement"],
                "behavior": {},
                "criterion": parameters["criterion"],
                "premise": parameters["premise"],
                "review": parameters["review"],
                "example_check": parameters["syntax"],
            }
            if not all(
                row["criterion"][k].strip()
                for k in ("given", "when", "then", "rejects")
            ):
                row["premise"] = {"choice": "unknown"}
            return super().__call__(
                tool, {"command": ["record_contract_criterion"], **row}
            )
        return super().__call__(tool, parameters)


def _directive_is_named(name: str, instruction: str) -> bool:
    """Match a dependency directive explicitly named anywhere in the task."""
    identifier = name.removeprefix("@").casefold()
    return bool(
        identifier
        and re.search(
            rf"(?<![\w])@{re.escape(identifier)}(?![\w])",
            instruction,
            flags=re.IGNORECASE,
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--setup", type=Path, required=True)
    parser.add_argument("--generation", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    packet = load_json(args.generation / "input.json")
    requirements = [
        r
        for r in packet["routed_clauses"]
        if r["route"] in {"include", "include_prohibition"}
    ]
    evidence_summary = load_json(args.evidence / "summary.json")
    if evidence_summary["requirements"] != len(requirements):
        raise ValueError("Evidence packets do not cover every routed requirement")
    operations = AtomicOperations(
        args.setup, args.output, packet, args.generation, args.evidence
    )
    operations.facts = [
        load_json(p)
        for p in sorted((args.generation / "facts/reviewed").glob("*.json"))
    ]
    body = [
        op("focused_sources", "sources", requirement=ref("requirement")),
        judge(
            "setup",
            "What complete inputs and one operation isolate this requirement?",
            (
                "Construct a valid normal-use scenario for the active requirement. "
                "The original instruction governs meaning and grammar; the routed "
                "clause is an index. Specify actual initial state and every input "
                "payload with concrete values, fields and flags. Name HTTP or "
                "WebSocket when relevant. Choose a state that exposes a plausible "
                "error through collision, retained data, scope or lifecycle timing "
                "when implicated. One operation, precise observation point. query "
                "is a complete valid GraphQL document only when needed; otherwise "
                "empty. Use captured directive locations and argument names. No "
                "expected outcomes. A requested new API need not exist yet. Do not "
                "invent guarantees for malformed inputs or absent containers."
            ),
            ["instruction", "requirement", "sources"],
            object_schema(given=TEXT, when=TEXT, query=TEXT),
        ),
        judge(
            "observation",
            (
                "What required observation follows from exactly these inputs and "
                "this operation?"
            ),
            (
                "Derive the post-implementation observation from scratch. Do not "
                "assess current implementation completeness. Assert only the active "
                "requirement and necessary interactions. Use exact values from "
                "setup; do not invent example values or defaults. Preserve state "
                "outside the addressed update unless relocation is explicitly "
                "requested. Distinguish addressed updates from positional "
                "insertion. Interpret type/value support separately from "
                "constructing absent structure. Existing concrete return types do "
                "not prescribe the class for a new structural interface. Optional "
                "API arguments do not require a particular AST representation when "
                "equivalent encodings express the same behavior. State exact "
                "observation timing and conditions without may/or alternatives; "
                "omit unrelated assertions. Reference input values symbolically "
                "only when the entire relationship is unambiguous and verifiable."
            ),
            ["instruction", "requirement", "setup", "sources"],
            object_schema(then=TEXT),
        ),
        judge(
            "counterexample",
            (
                "Which plausible incorrect implementation would fail this observed "
                "condition?"
            ),
            (
                "Name a specific wrong behavior discriminated by this setup and "
                "observation. Do not assume a correct output beyond the supplied "
                "observation. Do not add implementation prescriptions or guarantees."
            ),
            ["requirement", "setup", "observation"],
            object_schema(rejects=TEXT),
        ),
        op(
            "assemble_atomic_criterion",
            "criterion",
            requirement=ref("requirement"),
            setup=ref("setup"),
            observation=ref("observation"),
            counterexample=ref("counterexample"),
        ),
        op("quality_query", "syntax", criterion=ref("criterion")),
        judge(
            "premise",
            (
                "Are the inputs fully specified and valid for checking the proposed "
                "observation?"
            ),
            (
                "Check concrete values, payload envelopes, prior state, directive "
                "locations, argument names, receivers and observation layer. Source "
                "is existing evidence, not a veto on requested new APIs. "
                "supported=complete valid setup; contradicted=invalid input or "
                "invented value; unknown=insufficient specification."
            ),
            ["instruction", "requirement", "criterion", "syntax", "sources"],
            CHOICE,
            "jev",
        ),
        judge(
            "review",
            (
                "Does every asserted observation follow from the instruction and "
                "correctly scoped source evidence?"
            ),
            (
                "Judge independently; the proposal and earlier analyses are "
                "hypotheses. Missing implementation never blocks an explicit "
                "requirement. Check observation time, update scope, absence "
                "defaults and payload-local metadata. Reject additional "
                "concrete-class identity requirements for a structural interface "
                "and stronger-than-requested representation guarantees. A targeted "
                "update should leave other addresses unchanged unless relocation is "
                "requested. Dependency formats describe scoped wire/API "
                "conventions, not mandatory implementation types. supported=all "
                "assertions justified; contradicted=any conflicting or unsupported "
                "assertion; unknown=underdetermined."
            ),
            ["instruction", "requirement", "criterion", "sources"],
            CHOICE,
            "jev",
        ),
        op(
            "record_atomic",
            "record",
            requirement=ref("requirement"),
            criterion=ref("criterion"),
            premise=ref("premise"),
            review=ref("review"),
            syntax=ref("syntax"),
        ),
    ]
    document = {
        "version": 1,
        "name": "atomic-acceptance-observations",
        "inputs": [
            {"name": "instruction", "type": "string", "required": True},
            {"name": "requirements", "type": "array", "required": True},
        ],
        "limits": {
            "llm_activations": 200,
            "tool_calls": 200,
            "context_chars": 70000,
            "context_value_chars": 50000,
        },
        "steps": [
            loop("requirements", "requirement", "records", "record", body, 40),
            op("merge_exact_criteria", "merge_result"),
            op("render_prompt", "prompt"),
            {"terminal": "succeeded"},
        ],
    }
    source = yaml.safe_dump(document, sort_keys=False)
    parse_and_validate(source)
    (args.output / "pipeline.yaml").write_text(source)
    write_json(args.output / "input.json", packet)
    write_json(
        args.output / "selection.json",
        {
            "selection": (
                "All 23 frozen requirements; no prior generated observation or oracle "
                "in generation context"
            ),
            "oracle_access": False,
        },
    )
    flash: WorkflowLLMClient = AtomicClient(args.output, "deepinfra")
    jev: WorkflowLLMClient = AtomicClient(args.output, "typesafe.ai")
    usage = evaluate_recorded(
        document,
        operations,
        flash,
        jev,
        {"instruction": packet["instruction"], "requirements": requirements},
    )
    write_json(
        args.output / "summary.json",
        {
            "requirements": len(requirements),
            "emitted": sum(bool(r["rendered"]) for r in operations.records),
            **usage,
            "tests_run": False,
            "coding_agents_run": False,
        },
    )
    print(json.dumps(load_json(args.output / "summary.json")), flush=True)


if __name__ == "__main__":
    main()
