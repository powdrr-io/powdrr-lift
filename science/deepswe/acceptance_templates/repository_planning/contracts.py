"""Science operations for cited contracts, requirement coverage and prompt rendering."""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml
from powdrr_service.procedrr import parse_and_validate
from powdrr_service.procedrr_evaluator import Evaluator

from powdrr_lift.workrr import WorkflowLLMClient
from science.deepswe.acceptance_templates.common import digest, load_json, write_json
from science.deepswe.acceptance_templates.repository_planning.prepare import inspect
from science.deepswe.acceptance_templates.repository_planning.run import (
    BASELINE,
    Operations,
    RecordedClient,
)
from science.deepswe.acceptance_templates.repository_planning.symbols import terms

from .contracts_workflow import definition


class ContractClient(RecordedClient):
    @staticmethod
    def fallback(schema: Mapping[str, Any]) -> dict[str, Any]:
        for key in ("facts", "criteria"):
            if key in schema["properties"]:
                return {key: []}
        if "behavior" in schema["properties"]:
            return {"behavior": ""}
        return RecordedClient.fallback(schema)


def citations(values: list[str], sources: list[dict[str, Any]]) -> dict[str, Any]:
    """Repair exact references and unambiguous file/line references without an LLM."""
    resolved: dict[str, dict[str, Any]] = {}
    unlinked = []
    for value in values:
        if value.casefold().strip() == "instruction":
            continue
        matches = [s for s in sources if value == s["reference"]]
        if not matches:
            for source in sources:
                if source["path"] not in value:
                    continue
                suffix = value.split(source["path"], 1)[1]
                numbers = [int(n) for n in re.findall(r"\d+", suffix)]
                if numbers and all(
                    source["start"] <= n <= source["end"] for n in numbers[:2]
                ):
                    matches.append(source)
        if not matches:
            unlinked.append(value)
        for source in matches:
            resolved[source["reference"]] = {
                k: source[k]
                for k in ("reference", "path", "start", "end", "file_sha256")
            }
    return {"resolved": list(resolved.values()), "unlinked": unlinked}


def evaluate_recorded(
    document: dict[str, Any],
    operations: ContractOperations,
    flash: WorkflowLLMClient,
    jev: WorkflowLLMClient,
    inputs: dict[str, Any],
) -> dict[str, Any]:
    """Preserve partial decisions and original requirements after a local failure."""
    events = []

    def record(event: Any) -> None:
        row = {"kind": event.kind, "path": event.path, "data": dict(event.data)}
        events.append(row)
        with (operations.output / "procedrr-events.jsonl").open("a") as stream:
            stream.write(json.dumps(row) + "\n")

    try:
        result = Evaluator(
            flash,
            operations,
            judge_clients={"flash": flash, "jev": jev},
            event_sink=record,
        ).evaluate(document, inputs)
        usage: dict[str, Any] = {
            "provider_calls": result.llm_activations,
            "run_error": None,
        }
    except Exception as exc:
        operations("internal", {"command": ["render_prompt"]})
        usage = {"provider_calls": None, "run_error": str(exc)}
    final = inspect(operations.container, {"operation": "status"})
    if final != operations.environment["initial_status"]:
        raise ValueError("Task repository changed during planning")
    write_json(operations.output / "events.json", events)
    calls = [load_json(p) for p in (operations.output / "calls").rglob("call.json")]
    usage.update(
        recorded_calls=len(calls),
        failures=sum(r["status"] == "failed" for r in calls),
        final_status=final,
    )
    write_json(operations.output / "execution.json", usage)
    return usage


class ContractOperations(Operations):
    def __init__(self, setup: Path, output: Path, packet: dict[str, Any]) -> None:
        super().__init__(setup, output, packet)
        self.facts: list[dict[str, Any]] = []
        self.coverage: list[dict[str, Any]] = []
        self.superseded: dict[str, str] = {}

    def __call__(self, tool: str, parameters: Mapping[str, Any]) -> Any:
        command = parameters["command"][0]
        if command == "bind_contract_facts":
            batch = parameters["batch"]
            allowed = {r["id"] for r in batch["requirements"]}
            facts = []
            for number, proposal in enumerate(parameters["proposal"]["facts"], 1):
                fact = {**proposal, "id": f"{batch['id']}-fact-{number}"}
                fact["citation_resolution"] = citations(
                    proposal["citations"], parameters["evidence"]["sources"]
                )
                fact["scope_warnings"] = sorted(set(proposal["applies_to"]) - allowed)
                fact["applies_to"] = [
                    r for r in proposal["applies_to"] if r in allowed
                ] or sorted(allowed)
                if not fact["statement"].strip():
                    continue
                facts.append(fact)
                write_json(self.output / "facts/proposed" / f"{fact['id']}.json", fact)
            return facts
        if command == "record_contract_fact":
            row = {**parameters["fact"], "review": parameters["review"]}
            self.facts.append(row)
            write_json(self.output / "facts/reviewed" / f"{row['id']}.json", row)
            return row
        if command == "requirement_contracts":
            requirement = parameters["requirement"]
            # Outcome generation receives scoped facts, without implementation bodies.
            return {
                "facts": [
                    {
                        k: f[k]
                        for k in (
                            "category",
                            "statement",
                            "applies_to",
                            "citation_resolution",
                        )
                    }
                    for f in self.facts
                    if f["review"]["choice"] == "supported"
                    and requirement["id"] in f["applies_to"]
                ]
            }
        if command == "bind_contract_criteria":
            rows = []
            for number, proposal in enumerate(parameters["proposal"]["criteria"], 1):
                row = {
                    **proposal,
                    "id": f"{parameters['requirement']['id']}-criterion-{number}",
                }
                if not all(
                    row[k].strip() for k in ("given", "when", "then", "rejects")
                ):
                    write_json(
                        self.output / "invalid-criteria" / f"{row['id']}.json", row
                    )
                    continue
                rows.append(row)
                write_json(self.output / "criteria/proposed" / f"{row['id']}.json", row)
            return rows
        if command == "check_examples":
            query = parameters["criterion"]["query"].strip()
            if not query:
                return {
                    "valid": True,
                    "query": None,
                    "note": "No query; premise review checks other inputs.",
                }
            result = inspect(
                self.container, {"operation": "parse_query", "query": query}
            )
            return {"valid": not result.get("error"), "query": query, "result": result}
        if command == "record_contract_criterion":
            row = {
                k: parameters[k]
                for k in (
                    "requirement",
                    "behavior",
                    "criterion",
                    "premise",
                    "review",
                    "example_check",
                )
            }
            criterion = row["criterion"]
            given = criterion["given"] + (
                f" Query: `{criterion['query']}`." if criterion["query"].strip() else ""
            )
            row["rendered"] = (
                f"Given {given} When {criterion['when']} Then {criterion['then']}"
                if row["premise"]["choice"] == row["review"]["choice"] == "supported"
                and row["example_check"]["valid"]
                else None
            )
            self.records.append(row)
            write_json(
                self.output / "criteria/reviewed" / f"{criterion['id']}.json", row
            )
            return row
        if command == "record_requirement_coverage":
            records = [r.get("result", r) for r in parameters["records"]]
            row = {
                "requirement": parameters["requirement"],
                "behavior": parameters["behavior"],
                "emitted_criteria": [
                    r["criterion"]["id"] for r in records if r["rendered"]
                ],
                "rejected_criteria": [
                    r["criterion"]["id"] for r in records if not r["rendered"]
                ],
            }
            row["status"] = "clarified" if row["emitted_criteria"] else "unresolved"
            self.coverage.append(row)
            write_json(
                self.output / "coverage" / f"{row['requirement']['id']}.json", row
            )
            return row
        if command == "prepare_overlap_pairs":
            rows = [r for r in self.records if r["rendered"]]
            pairs: list[dict[str, Any]] = []
            for i, left in enumerate(rows):
                for right in rows[i + 1 :]:
                    a, b = (
                        terms(left["criterion"]["then"]),
                        terms(right["criterion"]["then"]),
                    )
                    similarity = len(a & b) / max(1, len(a | b))
                    if (
                        similarity >= 0.45
                        or left["requirement"]["id"] == right["requirement"]["id"]
                    ):
                        pairs.append(
                            {"left": left, "right": right, "similarity": similarity}
                        )
            pairs.sort(key=lambda p: -p["similarity"])
            write_json(
                self.output / "overlap-candidates.json",
                {
                    "pairs": pairs[:30],
                    "total": len(pairs),
                    "truncated": len(pairs) > 30,
                },
            )
            return pairs[:30]
        if command == "record_overlap":
            pair, decision = parameters["pair"], parameters["decision"]
            left, right = (
                pair["left"]["criterion"]["id"],
                pair["right"]["criterion"]["id"],
            )
            if left not in self.superseded and right not in self.superseded:
                if decision["choice"] == "left_covers_right":
                    self.superseded[right] = left
                elif decision["choice"] == "right_covers_left":
                    self.superseded[left] = right
            row = {"left": left, "right": right, "decision": decision}
            write_json(self.output / "overlap-reviews" / f"{left}--{right}.json", row)
            return row
        if command == "merge_exact_criteria":
            groups: dict[str, list[str]] = {}
            for row in self.records:
                if row["rendered"]:
                    groups.setdefault(" ".join(row["rendered"].split()), []).append(
                        row["criterion"]["id"]
                    )
            result = {
                "groups": list(groups.values()),
                "removed": sum(len(g) - 1 for g in groups.values()),
                "method": "Exact text deduplication plus reviewed semantic coverage",
                "semantic_superseded": self.superseded,
            }
            write_json(self.output / "deduplication.json", result)
            for row in self.coverage:
                targets = []
                for identifier in row["emitted_criteria"]:
                    while identifier in self.superseded:
                        identifier = self.superseded[identifier]
                    targets.append(identifier)
                row["effective_criteria"] = list(dict.fromkeys(targets))
            write_json(self.output / "coverage.json", {"requirements": self.coverage})
            return result
        if command == "render_prompt":
            requirements = [
                r
                for r in self.packet["routed_clauses"]
                if r["route"] in {"include", "include_prohibition"}
            ]
            lines = [
                "# Objective",
                "",
                self.packet["instruction"].split("\n\n", 1)[0],
                "",
                "# Requirements",
                "",
            ]
            lines += [f"{i}. {r['text']}" for i, r in enumerate(requirements, 1)]
            lines += [
                "",
                "# Constraints",
                "",
                "- Powdrr manages branching and commits.",
            ]
            for category, heading in (
                ("compatibility", None),
                ("owner", "# Relevant context"),
            ):
                if heading:
                    lines += ["", heading, ""]
                seen = set()
                for fact in self.facts:
                    if (
                        fact["review"]["choice"] != "supported"
                        or fact["category"] != category
                        or fact["statement"] in seen
                    ):
                        continue
                    seen.add(fact["statement"])
                    anchors = ", ".join(
                        f"{s['path']}:{s['start']}"
                        for s in fact["citation_resolution"]["resolved"]
                    )
                    lines.append(
                        f"- {fact['statement']}"
                        + (f" (Source: {anchors}.)" if anchors else "")
                    )
            lines += ["", "# Acceptance criteria", ""]
            seen_criteria = set()
            for row in self.records:
                if (
                    row["rendered"]
                    and row["criterion"]["id"] not in self.superseded
                    and row["rendered"] not in seen_criteria
                ):
                    seen_criteria.add(row["rendered"])
                    lines.append(f"- {row['rendered']}")
            lines += [
                "",
                (
                    "Original requirements remain completion conditions when "
                    "clarification is unresolved."
                ),
                "",
                "# Verification and completion",
                "",
                (
                    "- Add or update validations for the requirements and "
                    "observable conditions above. Report checks performed, "
                    "failures, compatibility changes, and unresolved behavior."
                ),
                "",
            ]
            (self.output / "prompt.md").write_text("\n".join(lines))
            return {"path": "prompt.md", "criteria": len(seen_criteria)}
        return super().__call__(tool, parameters)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--setup", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    packet = load_json(BASELINE / "input.json")
    if packet["instruction"] != (args.setup / "instruction.md").read_text():
        raise ValueError("Instruction differs from frozen routing")
    requirements = [
        r
        for r in packet["routed_clauses"]
        if r["route"] in {"include", "include_prohibition"}
    ]
    batches = [
        {"id": f"batch-{i // 4 + 1:02}", "requirements": requirements[i : i + 4]}
        for i in range(0, len(requirements), 4)
    ]
    workflow = definition()
    source = yaml.safe_dump(workflow, sort_keys=False)
    parse_and_validate(source)
    identity = {
        "workflow": digest(workflow),
        "input": digest(packet),
        "environment": load_json(args.setup / "environment.json"),
        "oracle_access": False,
    }
    target = args.output / "definition.json"
    if target.exists() and load_json(target) != identity:
        raise ValueError("Changed experiment; use a fresh output")
    write_json(target, identity)
    write_json(args.output / "input.json", packet)
    (args.output / "pipeline.yaml").write_text(source)
    operations = ContractOperations(args.setup, args.output, packet)
    flash: WorkflowLLMClient = ContractClient(args.output, "deepinfra")
    jev: WorkflowLLMClient = ContractClient(args.output, "typesafe.ai")

    def event_sink(event: Any) -> None:
        with (args.output / "procedrr-events.jsonl").open("a") as stream:
            stream.write(
                json.dumps(
                    {"kind": event.kind, "path": event.path, "data": dict(event.data)}
                )
                + "\n"
            )

    error = None
    try:
        result = Evaluator(
            flash,
            operations,
            judge_clients={"flash": flash, "jev": jev},
            event_sink=event_sink,
        ).evaluate(
            workflow,
            {
                "instruction": packet["instruction"],
                "batches": batches,
                "requirements": requirements,
            },
        )
        usage = {
            "llm_activations": result.llm_activations,
            "operations": result.tool_calls,
        }
    except Exception as exc:
        error, usage = str(exc), {}
        operations("internal", {"command": ["render_prompt"]})
    final = inspect(operations.container, {"operation": "status"})
    if final != operations.environment["initial_status"]:
        raise ValueError("Task repository changed during planning")
    calls = [load_json(p) for p in (args.output / "calls").rglob("call.json")]
    write_json(
        args.output / "summary.json",
        {
            **usage,
            "run_error": error,
            "requirements": len(requirements),
            "covered": sum(r["status"] == "clarified" for r in operations.coverage),
            "criteria": len(operations.records),
            "emitted": sum(bool(r["rendered"]) for r in operations.records),
            "provider_calls": len(calls),
            "failures": sum(r["status"] == "failed" for r in calls),
            "final_status": final,
            "tests_run": False,
            "coding_agents_run": False,
        },
    )
    print(json.dumps(load_json(args.output / "summary.json")), flush=True)


if __name__ == "__main__":
    main()
