"""Run the production instruction ledger, atomicity, and routing before matching."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import yaml

from powdrr_lift.core.instruction_ledger import (
    InstructionLedger,
    apply_atomicity_decisions,
    compile_instruction_ledger,
)
from powdrr_lift.workrr.jev_classifier import JevSemanticClassifierClient
from powdrr_lift.workrr.procedrr import WorkrrProcedrrClient
from powdrr_lift.workrr.semantic_contract_compiler import (
    bind_source_semantic_decisions,
    prepare_source_semantic_decisions,
)

from . import PROMPT_VERSION
from .cli import DEFAULT_MODEL, _client
from .common import HERE, Recorder, digest, generation_input, load_json, write_json
from .generation import generate, load_catalog, save_generation

DESIGN_FLOW = HERE.parents[2] / "docs/procedrr/skill-definitions/design-interview.yaml"
MAX_PARALLEL = 8


class JevPromptAdapter:
    """Present recorded science calls in the production Jev parser's format."""

    def __init__(self, client: Any) -> None:
        self.client = client
        self._local = threading.local()
        self._handler = _JevEventHandler(self._local)
        self._logger = logging.getLogger("powdrr_lift.workrr.jev_classifier")
        self._logger.addHandler(self._handler)

    def complete_json(
        self, messages: list[dict[str, str]], *, response_schema: Any = None
    ) -> dict[str, Any]:
        if len(messages) != 2:
            raise ValueError("Jev adapter expected one system and one user message")
        payload = json.loads(messages[1]["content"])
        if not isinstance(payload, dict):
            raise ValueError("Jev classifier payload must be an object")
        content = (
            messages[0]["content"]
            + "\n\nContext:\n"
            + json.dumps(payload, ensure_ascii=False)
        )
        self._local.events = []
        result = self.client.complete_json(
            [
                {"role": "system", "content": "Return only the classifier result."},
                {"role": "user", "content": content},
            ],
            response_schema=response_schema,
        )
        events = self._local.events
        if not any("JEV_RESULT_ACCEPTED" in event for event in events):
            raise RuntimeError("JEV did not accept this classifier request")
        if any(
            token in event
            for event in events
            for token in ("JEV_FALLBACK", "JEV_FAIL_CLOSED", "JEV_CALL_SKIPPED")
        ):
            raise RuntimeError(
                "JEV fallback or skip detected; refusing model substitution"
            )
        return result

    def close(self) -> None:
        self._logger.removeHandler(self._handler)


class _JevEventHandler(logging.Handler):
    def __init__(self, local: threading.local) -> None:
        super().__init__(level=logging.WARNING)
        self.local = local

    def emit(self, record: logging.LogRecord) -> None:
        events = getattr(self.local, "events", None)
        if events is not None:
            events.append(record.getMessage())


def _find_judges(value: Any) -> list[dict[str, Any]]:
    found = []
    if isinstance(value, dict):
        judge = value.get("judge")
        if isinstance(judge, dict):
            found.append(judge)
        for child in value.values():
            found.extend(_find_judges(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(_find_judges(child))
    return found


def load_production_judges(path: Path = DESIGN_FLOW) -> dict[str, dict[str, Any]]:
    flow = yaml.safe_load(path.read_text(encoding="utf-8"))
    judges = _find_judges(flow)
    selected = {
        "atomicity": next(
            row
            for row in judges
            if str(row.get("question", "")).startswith(
                "Does this one instruction clause contain more than one"
            )
        ),
        "split": next(
            row
            for row in judges
            if str(row.get("question", "")).startswith(
                "How can this clause be decomposed"
            )
        ),
        "routing": next(
            row
            for row in judges
            if str(row.get("question", "")).startswith(
                "Decide how the pipeline should route this exact"
            )
        ),
    }
    return selected


def _line_source_ids(spans: list[dict[str, Any]], start: int, end: int) -> list[str]:
    return [row["id"] for row in spans if row["start"] < end and start < row["end"]]


def _judge_prompt(judge: dict[str, Any]) -> str:
    instructions = judge.get("instructions", [])
    return f"Question:\n{judge['question']}\n\nInstructions:\n" + "\n".join(
        f"- {item}" for item in instructions
    )


def _route_inventory(
    original_task: dict[str, Any],
    ledger: InstructionLedger,
    route_decisions: list[dict[str, Any]],
) -> dict[str, Any]:
    spans = original_task["source_spans"]
    items: list[dict[str, Any]] = []
    context_items = []
    excluded_items = []
    unresolved_items = []
    for clause, decision in zip(ledger.clauses, route_decisions, strict=True):
        route = decision["route"]
        source_ids = _line_source_ids(
            spans, clause.source_span[0], clause.source_span[1]
        )
        if not source_ids:
            raise ValueError(
                f"routed clause has no original source span: {clause.clause_id}"
            )
        item = {
            "id": f"r{len(items) + 1:03}",
            "clause_id": clause.clause_id,
            "parent_clause_id": clause.parent_clause_id,
            "text": clause.text,
            "kind": (
                "requirement"
                if route in {"include", "include_prohibition"}
                else "context"
                if route == "context"
                else "process"
                if route == "exclude"
                else "unresolved"
            ),
            "route": route,
            "polarity": (
                "prohibited"
                if route == "include_prohibition"
                else "positive_product_behavior"
                if route == "include"
                else "descriptive"
                if route in {"context", "exclude"}
                else "unresolved"
            ),
            "validation_group_id": clause.validation_group_id,
            "validation_relation": clause.validation_relation,
            "source_ids": source_ids,
            "source_span": {
                "start": clause.source_span[0],
                "end": clause.source_span[1],
            },
            "route_decision": {
                key: value
                for key, value in decision["decision"].items()
                if key != "created_at"
            },
        }
        items.append(item)
        if route == "context":
            context_items.append(item["id"])
        elif route == "exclude":
            excluded_items.append(item["id"])
        elif route == "unclear":
            unresolved_items.append(item["id"])
    return {
        "schema_version": "production-routed-inventory-v1",
        "ledger_sha256": digest(ledger.to_data()),
        "items": items,
        "context_item_ids": context_items,
        "excluded_item_ids": excluded_items,
        "unresolved_item_ids": unresolved_items,
    }


def analyze_instruction(
    task: dict[str, Any],
    *,
    jev_recorder: Recorder,
    split_recorder: Recorder,
    judges: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Use production sentence capture, JEV atomicity/routing, and planning splits."""
    ledger = compile_instruction_ledger(task["task_id"], task["instruction"])
    atomic_judge = judges["atomicity"]
    split_judge = judges["split"]
    split_decisions: dict[str, dict[str, Any]] = {}
    atomic_records = []
    full_source = ledger.source.text
    for clause in ledger.clauses:
        raw_clause = clause.to_data()

        def validate_atomicity(value: dict[str, Any]) -> dict[str, Any]:
            if not isinstance(value.get("multiple"), bool):
                raise ValueError("multiple must be boolean")
            return value

        atomic = jev_recorder.call(
            f"atomicity-{clause.clause_id}",
            _judge_prompt(atomic_judge),
            {"atomicity_clause": raw_clause},
            atomic_judge["output"]["schema"],
            validate_atomicity,
        )
        split_decisions[clause.clause_id] = {"multiple": atomic["multiple"]}
        record = {"clause_id": clause.clause_id, "multiple": atomic["multiple"]}
        if atomic["multiple"]:
            split_context = {"atomicity_split_request": {"clause": raw_clause}}
            split_result = split_recorder.call(
                f"split-{clause.clause_id}",
                _judge_prompt(split_judge),
                split_context,
                split_judge["output"]["schema"],
                lambda value: value,
            )
            split_decisions[clause.clause_id].update(split_result)
            record["split"] = split_result
        atomic_records.append(record)
    split_ledger = apply_atomicity_decisions(ledger, split_decisions)

    flow_route_instructions = list(judges["routing"]["instructions"])
    route_records: list[dict[str, Any] | None] = [None] * len(split_ledger.clauses)

    def classify(index: int, clause: Any) -> tuple[int, dict[str, Any]]:
        request_plan = prepare_source_semantic_decisions(
            clause.to_data(), source_text=full_source
        )
        pending = request_plan["pending_specs"]
        if len(pending) != 1:
            raise ValueError("production router did not produce one root decision")
        request = dict(pending[0])
        request["instructions"] = flow_route_instructions + request["instructions"]
        request["question"] = judges["routing"]["question"]
        response_schema = dict(judges["routing"]["output"]["schema"])
        value_schema = dict(response_schema["properties"]["value"])
        value_schema["enum"] = [*request["allowed_values"], None]
        response_schema["properties"] = {
            **response_schema["properties"],
            "value": value_schema,
        }
        context = {
            "root_decision_request": request,
            "atomic_instruction_ledger": {
                "source_text": full_source,
                "clauses": [item.to_data() for item in split_ledger.clauses],
            },
        }

        def validate(result: dict[str, Any]) -> dict[str, Any]:
            if result.get("status") not in {"resolved", "unresolved"}:
                raise ValueError("router status is invalid")
            if result.get("status") == "resolved":
                if result.get("value") not in request["allowed_values"]:
                    raise ValueError("router returned a value outside its allowed set")
                if result.get("reason_code") is not None:
                    raise ValueError("resolved route has a reason code")
            elif result.get("reason_code") not in {
                "source_ambiguous",
                "source_underspecified",
                "no_candidate",
                "multiple_candidates",
                "repository_evidence_missing",
                "unsupported_concept",
                "classifier_abstained",
                "invalid_response",
                "conflicting_evidence",
            }:
                raise ValueError("unresolved route has an invalid reason code")
            return result

        result = jev_recorder.call(
            f"routing-{clause.clause_id}",
            _judge_prompt(judges["routing"]),
            context,
            response_schema,
            validate,
        )
        bound = bind_source_semantic_decisions(
            resolved_decisions=request_plan["resolved_decisions"],
            pending_specs=pending,
            provider_results=[result],
        )
        decision = bound[0].to_data()
        return index, {
            "clause_id": clause.clause_id,
            "route": decision["result"]["value"] or "unclear",
            "decision": decision,
        }

    with ThreadPoolExecutor(max_workers=MAX_PARALLEL) as pool:
        futures = [
            pool.submit(classify, index, clause)
            for index, clause in enumerate(split_ledger.clauses)
        ]
        for future in futures:
            index, row = future.result()
            route_records[index] = row
    resolved_routes = [row for row in route_records if row is not None]
    inventory = _route_inventory(task, split_ledger, resolved_routes)
    return {
        "task_id": task["task_id"],
        "instruction_sha256": digest(task["instruction"]),
        "production_flow_sha256": hashlib.sha256(DESIGN_FLOW.read_bytes()).hexdigest(),
        "ledger": ledger.to_data(),
        "atomicity": atomic_records,
        "split_ledger": split_ledger.to_data(),
        "split_diagnostics": [row.to_data() for row in split_ledger.split_diagnostics],
        "routes": resolved_routes,
        "inventory": inventory,
        "routing_counts": {
            route: sum(item["route"] == route for item in resolved_routes)
            for route in (
                "include",
                "include_prohibition",
                "context",
                "exclude",
                "unclear",
            )
        },
        "clause_count_before_split": len(ledger.clauses),
        "clause_count_after_split": len(split_ledger.clauses),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs-dir", type=Path, default=HERE / "data/inputs")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--tasks", nargs="+", required=True)
    parser.add_argument("--planning-provider", default="deepinfra")
    parser.add_argument("--planning-model", default=DEFAULT_MODEL)
    parser.add_argument("--matching-model", default=DEFAULT_MODEL)
    parser.add_argument("--batch-size", type=int, default=3)
    parser.add_argument("--repairs", type=int, default=1)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args(argv)
    if not any(
        os.environ.get(name)
        for name in ("TYPESAFEAI_API_KEY", "TYPESAFE_API_KEY", "SYSTEM_ONE_API_KEY")
    ):
        raise RuntimeError("JEV API key is required; router fallback is disabled")
    judges = load_production_judges()
    planning_client = _client(args.planning_provider, args.planning_model, 120, 12288)
    skills_dir = DESIGN_FLOW.parent
    jev_client = JevPromptAdapter(
        WorkrrProcedrrClient(
            JevSemanticClassifierClient(planning_client, fail_closed=True),
            skills_dir=skills_dir,
        )
    )
    split_client = WorkrrProcedrrClient(planning_client, skills_dir=skills_dir)
    matching_client = _client(args.planning_provider, args.matching_model, 120, 12288)
    catalog = load_catalog(HERE / "catalog.json")
    failures: list[dict[str, str]] = []
    task_ids = args.tasks
    manifest = {
        "provider": args.planning_provider,
        "planning_model": args.planning_model,
        "matching_model": args.matching_model,
        "prompt_version": PROMPT_VERSION,
        "router_provider": "typesafe.ai",
        "router_model": os.environ.get("SYSTEM_ONE_MODEL", "jev-latest"),
        "splitter_provider": args.planning_provider,
        "splitter_model": args.planning_model,
        "catalog_sha256": digest(catalog),
        "production_flow_sha256": hashlib.sha256(DESIGN_FLOW.read_bytes()).hexdigest(),
        "batch_size": args.batch_size,
        "repairs": args.repairs,
        "tasks": task_ids,
        "arms": ["templates"],
        "analysis_pipeline": [
            "production instruction ledger sentence capture",
            "production Jev atomicity classifier",
            "production planning model atomicity splitter",
            "production Jev instruction router",
            "acceptance template selection and binding",
        ],
        "failures": failures,
    }
    write_json(args.output_dir / "run.json", {**manifest, "status": "running"})
    for task_id in task_ids:
        task = generation_input(args.inputs_dir / f"{task_id}.json")
        task_dir = args.output_dir / task_id
        task_dir.mkdir(parents=True, exist_ok=True)
        jev_recorder = Recorder(
            jev_client,
            task_dir / "instruction-analysis/jev-calls",
            provider="typesafe.ai",
            model=manifest["router_model"],
            resume=args.resume,
            repairs=args.repairs,
        )
        split_recorder = Recorder(
            split_client,
            task_dir / "instruction-analysis/split-calls",
            provider=args.planning_provider,
            model=args.planning_model,
            resume=args.resume,
            repairs=args.repairs,
        )
        stage = "production_analysis"
        try:
            analysis_path = task_dir / "instruction-analysis/analysis.json"
            if args.resume and analysis_path.exists():
                analysis = load_json(analysis_path)
                if analysis.get("instruction_sha256") != digest(task["instruction"]):
                    raise ValueError("saved instruction analysis is stale")
                if (
                    analysis.get("production_flow_sha256")
                    != manifest["production_flow_sha256"]
                ):
                    raise ValueError("saved production analysis flow is stale")
            else:
                analysis = analyze_instruction(
                    task,
                    jev_recorder=jev_recorder,
                    split_recorder=split_recorder,
                    judges=judges,
                )
                write_json(analysis_path, analysis)
            manifest.setdefault("task_routing_counts", {})[task_id] = analysis[
                "routing_counts"
            ]
            stage = "template_match"
            inv = analysis["inventory"]
            generation_recorder = Recorder(
                matching_client,
                task_dir / "templates/calls",
                provider=args.planning_provider,
                model=args.matching_model,
                resume=args.resume,
                repairs=args.repairs,
            )
            result = generate(
                task,
                inv,
                catalog,
                generation_recorder,
                "templates",
                args.batch_size,
            )
            result["instruction_analysis_sha256"] = digest(analysis)
            result["routing_counts"] = analysis["routing_counts"]
            result["unresolved_route_items"] = [
                row for row in inv["items"] if row["route"] == "unclear"
            ]
            save_generation(task_dir / "templates", result)
        except Exception as exc:  # Preserve task failure, continue the cohort.
            failures.append({"task_id": task_id, "stage": stage, "error": str(exc)})
        write_json(args.output_dir / "run.json", {**manifest, "status": "running"})
    manifest["status"] = "completed" if not failures else "completed_with_failures"
    jev_client.close()
    write_json(args.output_dir / "run.json", manifest)
    completed = len(task_ids) - len(failures)
    print(
        f"Analyzed and matched {completed}/{len(task_ids)} tasks; "
        f"{len(failures)} failed"
    )
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())
