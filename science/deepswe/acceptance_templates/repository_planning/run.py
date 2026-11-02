"""Execute the declared science Procedrr against the prepared base repository."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml
from jsonschema import validate
from powdrr_service.decision_classifier import (
    JEV_DEFAULT_BASE_URL,
    _call_jev,
    _classifier_request,
    _format_classifier_result,
)
from powdrr_service.procedrr import parse_and_validate
from powdrr_service.procedrr_evaluator import Evaluator

from powdrr_lift.workrr import WorkflowLLMClient
from science.deepswe.acceptance_templates.cli import DEFAULT_MODEL, _client
from science.deepswe.acceptance_templates.common import digest, load_json, write_json
from science.deepswe.acceptance_templates.repository_planning.prepare import inspect
from science.deepswe.acceptance_templates.repository_planning.symbols import retrieve
from science.deepswe.acceptance_templates.repository_planning.workflow import definition

HERE = Path(__file__).resolve().parent
BASELINE = HERE / "baseline"


class DirectJev:
    """Use the production classifier parser on unwrapped Procedrr messages."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def complete_json(
        self, messages: list[dict[str, str]], *, response_schema: Mapping[str, Any]
    ) -> dict[str, Any]:
        request = _classifier_request(messages, response_schema)
        if request is None:
            raise ValueError("Unsupported direct Procedrr classifier request")
        key = next(
            (
                os.environ[k]
                for k in (
                    "TYPESAFEAI_API_KEY",
                    "TYPESAFE_API_KEY",
                    "SYSTEM_ONE_API_KEY",
                )
                if os.environ.get(k)
            ),
            None,
        )
        if not key:
            raise ValueError("Typesafe.ai credential missing")
        base = os.environ.get("SYSTEM_ONE_BASE_URL")
        endpoint = f"{base.rstrip('/')}/systemone" if base else JEV_DEFAULT_BASE_URL
        write_json(self.path, {"request": request, "status": "started"})
        raw = _call_jev(
            request, key, endpoint, os.environ.get("SYSTEM_ONE_MODEL", "jev-latest")
        )
        write_json(
            self.path, {"request": request, "status": "completed", "response": raw}
        )
        choice = raw["answer"].get("choice")
        if choice == "unresolved":
            return {"choice": "unknown"}
        return _format_classifier_result(choice, request, response_schema)


class RecordedClient:
    "Capture exact Procedrr requests; no retries, model substitution, or hidden repair."

    def __init__(self, output: Path, provider: str) -> None:
        self.output = output
        self.provider = provider
        self.count = 0

    def complete_json(
        self,
        messages: list[dict[str, str]],
        *,
        response_schema: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if response_schema is None:
            raise ValueError("Schema required")
        self.count += 1
        identity = {
            "provider": self.provider,
            "model": DEFAULT_MODEL
            if self.provider == "deepinfra"
            else os.environ.get("SYSTEM_ONE_MODEL", "jev-latest"),
            "messages": messages,
            "schema": dict(response_schema),
        }
        directory = self.output / "calls" / self.provider / digest(identity)
        target = directory / "call.json"
        if target.exists():
            saved = load_json(target)
            if saved["request_sha256"] != digest(identity):
                raise ValueError("Checkpoint differs; use a new output")
            if saved["status"] == "completed":
                return saved["response"]
            return self.fallback(response_schema)
        row: dict[str, Any] = {
            "request": identity,
            "request_sha256": digest(identity),
            "status": "running",
        }
        write_json(target, row)
        client = (
            _client("deepinfra", DEFAULT_MODEL, 120, 6000)
            if self.provider == "deepinfra"
            else DirectJev(directory / "provider.json")
        )
        previous = os.environ.get("POWDRR_STREAM_CAPTURE_PATH")
        os.environ["POWDRR_STREAM_CAPTURE_PATH"] = str(directory / "raw.txt")
        started = time.monotonic()
        print(self.provider, self.count, "started", flush=True)
        try:
            result = client.complete_json(messages, response_schema=response_schema)
            validate(result, response_schema)
            row.update(status="completed", response=result)
        except Exception as exc:
            row.update(
                status="failed",
                error_type=type(exc).__name__,
                error=str(exc),
                continuation=(
                    "Explicit unknown/unresolved/empty proposal; original "
                    "requirements retained"
                ),
            )
            result = self.fallback(response_schema)
        finally:
            if previous is None:
                os.environ.pop("POWDRR_STREAM_CAPTURE_PATH", None)
            else:
                os.environ["POWDRR_STREAM_CAPTURE_PATH"] = previous
            row["elapsed_seconds"] = time.monotonic() - started
            write_json(target, row)
        print(self.provider, self.count, row["status"], flush=True)
        return result

    @staticmethod
    def fallback(schema: Mapping[str, Any]) -> dict[str, Any]:
        properties = schema["properties"]
        if "choice" in properties:
            return {"choice": "unknown"}
        if "status" in properties:
            return {
                "status": "unresolved",
                "then": "Provider failed; see recorded call.",
            }
        if "symbols" in properties:
            return {"symbols": []}
        return {"scenarios": []}


class Operations:
    def __init__(self, setup: Path, output: Path, packet: dict[str, Any]) -> None:
        self.setup, self.output, self.packet = setup, output, packet
        self.environment = load_json(setup / "environment.json")
        self.container = self.environment["container"]
        self.records: list[dict[str, Any]] = []

    def __call__(self, tool: str, parameters: Mapping[str, Any]) -> Any:
        if tool != "internal":
            raise ValueError("Only declared science operations are allowed")
        name = parameters["command"][0]
        if name == "extract_symbols":
            target = self.output / "symbol-index.json.gz"
            if target.exists():
                return json.loads(gzip.decompress(target.read_bytes()))
            index = inspect(self.container, {"operation": "index"})
            if "error" in index:
                raise ValueError(index)
            target.write_bytes(gzip.compress(json.dumps(index).encode(), mtime=0))
            return index
        if name == "retrieve_candidates":
            batch = parameters["batch"]
            candidates = retrieve(
                parameters["index"], " ".join(r["text"] for r in batch["requirements"])
            )
            write_json(self.output / "retrieval" / f"{batch['id']}.json", candidates)
            return {
                "batch": batch["id"],
                "candidates": [
                    {
                        "symbol": {
                            k: r["symbol"][k]
                            for k in (
                                "id",
                                "signature",
                                "qualified_name",
                                "path",
                                "kind",
                                "test",
                            )
                        },
                        "docstring": r["symbol"]["docstring"][:1200],
                        "reason": r["reason"],
                        "matched_terms": r["matched_terms"],
                    }
                    for r in candidates["candidates"]
                ],
            }
        if name == "gather_evidence":
            batch = parameters["batch"]
            full = load_json(self.output / "retrieval" / f"{batch['id']}.json")
            by_id = {r["symbol"]["id"]: r["symbol"] for r in full["candidates"]}
            selected = parameters["selection"]["symbols"]
            unknown = set(selected) - set(by_id)
            if unknown:
                write_json(
                    self.output / "invalid-selection" / f"{batch['id']}.json",
                    {"unknown": sorted(unknown)},
                )
            # Keep lexical alternatives even if selector drops a relevant symbol.
            ordering = list(
                dict.fromkeys([s for s in selected if s in by_id] + list(by_id))
            )
            sources = []
            for identifier in ordering:
                symbol = by_id[identifier]
                end = min(
                    symbol["end"],
                    symbol["start"] + (24 if symbol["kind"] == "class" else 119),
                )
                request = {
                    "operation": "read",
                    "path": symbol["path"],
                    "start": symbol["start"],
                    "end": end,
                }
                response = inspect(self.container, request)
                if response.get("error"):
                    raise ValueError(response)
                if response["file_sha256"] != symbol["file_sha256"]:
                    raise ValueError("Source changed since indexing")
                item = {
                    "reference": identifier,
                    "signature": symbol["signature"],
                    "selected": identifier in selected,
                    "truncated_body": end < symbol["end"],
                    **response,
                }
                sources.append(item)
                write_json(
                    self.output / "reads" / batch["id"] / f"{len(sources):02}.json",
                    {"request": request, "response": item},
                )
            return {
                "sources": sources,
                "selection_errors": sorted(unknown),
                "note": (
                    "Original implementation. Class excerpts show headers; long "
                    "bodies explicitly truncated. Syntactic candidates are not "
                    "verified type-resolution edges."
                ),
            }
        if name == "bind_scenarios":
            batch = parameters["batch"]
            allowed = {s["reference"] for s in parameters["evidence"]["sources"]}
            proposals = parameters["proposals"]["scenarios"]
            scenarios = []
            for number, row in enumerate(proposals, 1):
                row = {
                    **row,
                    "id": f"{batch['id']}-scenario-{number}",
                    "requirement_ids": [r["id"] for r in batch["requirements"]],
                }
                if (
                    not all(row[k].strip() for k in ("given", "when", "observe"))
                    or not row["evidence"]
                    or not set(row["evidence"]) <= allowed
                ):
                    write_json(
                        self.output / "invalid-scenarios" / f"{row['id']}.json", row
                    )
                    continue
                scenarios.append(row)
                write_json(self.output / "scenarios" / f"{row['id']}.json", row)
            return scenarios
        if name == "default_answer":
            return {"status": "unresolved", "then": "Not called: premise unverified."}
        if name == "default_review":
            return {"choice": "unknown"}
        if name == "record_criterion":
            scenario = parameters["scenario"]
            row = {
                k: parameters[k] for k in ("scenario", "premise", "answer", "review")
            }
            row["rendered"] = (
                f"Given {scenario['given']} When {scenario['when']} "
                f"Then {parameters['answer']['then']}"
                if parameters["premise"]["choice"] == "supported"
                and parameters["answer"]["status"] == "resolved"
                and parameters["review"]["choice"] == "supported"
                else None
            )
            self.records.append(row)
            write_json(self.output / "results" / f"{scenario['id']}.json", row)
            return row
        if name == "render_prompt":
            original = (BASELINE / "prompt.md").read_text()
            before, after = original.split("# Acceptance criteria\n", 1)
            _, tail = after.split("Original requirements remain", 1)
            paths = sorted(
                {
                    load_json(p)["request"]["path"]
                    for p in (self.output / "reads").rglob("*.json")
                }
            )
            before = (
                before.split("# Relevant context\n", 1)[0]
                + "# Relevant context\n\nInspected original files: "
                + ", ".join(paths)
                + ".\n\n"
            )
            text = (
                before
                + "# Acceptance criteria\n\n"
                + "\n".join(f"- {r['rendered']}" for r in self.records if r["rendered"])
                + "\n\nOriginal requirements remain"
                + tail
            )
            (self.output / "prompt.md").write_text(text)
            return {
                "path": "prompt.md",
                "sha256": hashlib.sha256(text.encode()).hexdigest(),
            }
        raise ValueError(f"Unknown operation {name}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--setup", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    packet = load_json(BASELINE / "input.json")
    packet = {k: packet[k] for k in ("task_id", "instruction", "routed_clauses")}
    if packet["instruction"] != (args.setup / "instruction.md").read_text():
        raise ValueError("Instruction changed from frozen routing input")
    document = definition()
    source = yaml.safe_dump(document, sort_keys=False)
    validated = parse_and_validate(source)
    if isinstance(validated, tuple):
        raise ValueError("Unexpected Procedrr parser result")
    # Preserve the exact declaration; checkpoints cannot resume a changed one.
    identity = {
        "workflow": digest(document),
        "packet": digest(packet),
        "environment": load_json(args.setup / "environment.json"),
    }
    if (args.output / "definition.json").exists() and load_json(
        args.output / "definition.json"
    ) != identity:
        raise ValueError("Changed experiment; use a fresh output")
    write_json(args.output / "definition.json", identity)
    write_json(args.output / "input.json", packet)
    (args.output / "pipeline.yaml").write_text(source)
    clauses = [
        r
        for r in packet["routed_clauses"]
        if r["route"] in {"include", "include_prohibition"}
    ]
    batches = [
        {
            "id": f"batch-{offset // 4 + 1:02}",
            "requirements": clauses[offset : offset + 4],
        }
        for offset in range(0, len(clauses), 4)
    ]
    operations = Operations(args.setup, args.output, packet)
    flash: WorkflowLLMClient = RecordedClient(args.output, "deepinfra")
    jev: WorkflowLLMClient = RecordedClient(args.output, "typesafe.ai")
    event_file = args.output / "procedrr-events.jsonl"

    def event_sink(event: Any) -> None:
        with event_file.open("a") as stream:
            stream.write(
                json.dumps(
                    {"kind": event.kind, "path": event.path, "data": dict(event.data)}
                )
                + "\n"
            )

    evaluator = Evaluator(
        flash,
        operations,
        judge_clients={"flash": flash, "jev": jev},
        event_sink=event_sink,
    )
    error = None
    try:
        result = evaluator.evaluate(
            document, {"instruction": packet["instruction"], "batches": batches}
        )
        usage = {
            "llm_activations": result.llm_activations,
            "operations": result.tool_calls,
        }
    except Exception as exc:
        error = str(exc)
        usage = {}
        operations("internal", {"command": ["render_prompt"]})
    final = inspect(operations.container, {"operation": "status"})
    if final != operations.environment["initial_status"]:
        raise ValueError("Task checkout changed during planning")
    calls = [load_json(p) for p in (args.output / "calls").rglob("call.json")]
    write_json(
        args.output / "summary.json",
        {
            **usage,
            "run_error": error,
            "scenario_count": len(operations.records),
            "emitted": sum(bool(r["rendered"]) for r in operations.records),
            "provider_calls": len(calls),
            "provider_failures": sum(r["status"] == "failed" for r in calls),
            "cached_decisions": sum("cached_from" in r for r in calls),
            "new_decisions": sum("cached_from" not in r for r in calls),
            "final_status": final,
            "tests_run": False,
            "coding_agents_run": False,
        },
    )
    print(json.dumps(load_json(args.output / "summary.json")), flush=True)


if __name__ == "__main__":
    main()
