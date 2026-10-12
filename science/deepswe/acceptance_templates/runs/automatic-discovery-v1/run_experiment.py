"""Discover bounded behavioral decisions and answer them without oracle inputs."""

from __future__ import annotations

import hashlib
import os
import tarfile
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from typing import Any

from jsonschema import validate

from powdrr_lift.workrr.jev_classifier import (
    JEV_DEFAULT_BASE_URL,
    _call_jev,
    _classifier_request,
    _format_classifier_result,
)
from science.deepswe.acceptance_templates.cli import DEFAULT_MODEL, _client
from science.deepswe.acceptance_templates.common import (
    TEXT,
    Recorder,
    array_schema,
    choice_schema,
    digest,
    load_json,
    object_schema,
    write_json,
)

HERE = Path(__file__).resolve().parent
PREVIOUS = HERE.parent / "decision-instructions-v1"
TASKS = (
    "koota-composite-trait-aspects",
    "gql-incremental-graphql-delivery",
    "bandit-structured-nosec-directives",
)
DISCOVERY = (
    "Discover the behavioral decisions a coding agent needs to resolve to implement "
    "these requirements consistently. Use only the original instruction, routed "
    "product clauses and original code. For each requested operation identify its "
    "object, triggering condition, affected scope and observable outcome. Ask small "
    "questions about explicit contracts and necessary implications: corresponding "
    "existing-operation behavior, object/reference lifetime, nested versus outer "
    "scope, conditions/exceptions, ordering, and interaction between requirements. "
    "Use only relevant dimensions; do not create a checklist of unrequested "
    "features. Keep different operations separate. An explicit new behavior "
    "overrides compatible inherited behavior. Distinguish external inputs from "
    "internal representations and specify where an outcome is observed. Do not "
    "assume a wire payload and a processed result have identical fields. Each "
    "question must address one behavior distinction, anchored to product clause "
    "IDs. Supply two plausible, mutually exclusive observable alternatives a "
    "coding agent might implement. Alternative prose must be usable literally "
    "as an acceptance criterion. Preserve all applicability conditions; avoid "
    "unrequested guarantees, numeric limits and implementation prescriptions. "
    "Use concrete before/after examples when they clarify the distinction. Do "
    "not decide which alternative is correct. Do not repeat the same behavior "
    "in multiple questions. Context explains requirements but is not itself "
    "an obligation. Return decisions and a per-product-clause account of which "
    "questions cover it, or why no further clarification is needed. No hidden "
    "solution, tests or target choices are available."
)
SELECT = (
    "Choose the behavior that best satisfies the requested feature's meaning, "
    "purpose and relationships with existing operations. Identify the operation, "
    "object, condition and observation scope. Inspect actual corresponding code "
    "behavior. Preserve compatible behavior; explicit new behavior overrides it. "
    "Compare alternatives by observable consequences and consistency with other "
    "requirements. Infer necessary implications without requiring literal "
    "repetition. Do not transfer a rule from a different operation or observation "
    "boundary. Choose unknown for unsupported guarantees or genuinely unresolved "
    "choices. Return only the selected choice."
)
SCHEMA = object_schema(
    decisions=array_schema(
        object_schema(
            id=TEXT,
            requirement_ids=array_schema(TEXT),
            operation=TEXT,
            object=TEXT,
            condition=TEXT,
            observation_scope=TEXT,
            question=TEXT,
            alternatives=object_schema(a=TEXT, b=TEXT),
        )
    ),
    coverage=array_schema(
        object_schema(
            requirement_id=TEXT,
            decision_ids=array_schema(TEXT),
            explanation=TEXT,
        )
    ),
)


class ObservedJev:
    """Use production request construction; record and normalize abstention."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def complete_json(
        self, messages: list[dict[str, str]], *, response_schema: Any = None
    ) -> dict[str, Any]:
        wrapped = [
            {"role": "system", "content": "Return only the classifier result."},
            {
                "role": "user",
                "content": messages[0]["content"]
                + "\n\nContext:\n"
                + messages[1]["content"],
            },
        ]
        request = _classifier_request(wrapped, response_schema)
        if request is None:
            raise ValueError("unsupported Jev request")
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
            raise ValueError("Typesafe.ai key missing")
        base = os.environ.get("SYSTEM_ONE_BASE_URL")
        endpoint = f"{base.rstrip('/')}/systemone" if base else JEV_DEFAULT_BASE_URL
        raw = _call_jev(
            request, key, endpoint, os.environ.get("SYSTEM_ONE_MODEL", "jev-latest")
        )
        write_json(self.path, {"request": request, "response": raw})
        answer = raw["answer"].get("choice")
        if answer == "unresolved":
            return {"choice": "unknown"}
        return _format_classifier_result(answer, request, response_schema)


def source_packet(task: str) -> dict[str, Any]:
    source = load_json(PREVIOUS / "sources" / f"{task}.json")
    routed = load_json(PREVIOUS / "routing" / f"{task}.json")
    return {
        "task_id": task,
        "instruction": source["instruction"],
        "original_code": source["original_code"],
        "routed_clauses": [
            {k: row[k] for k in ("id", "text", "route", "source_ids")}
            for row in routed["inventory"]["items"]
        ],
    }


def checked_discovery(
    response: dict[str, Any], packet: dict[str, Any]
) -> dict[str, Any]:
    validate(response, SCHEMA)
    products = {
        r["id"]
        for r in packet["routed_clauses"]
        if r["route"] in {"include", "include_prohibition"}
    }
    response = deepcopy(response)
    # The raw checkpoint retains non-product bookkeeping. Removing those rows
    # changes neither a behavioral question nor an alternative.
    response["coverage"] = [
        row for row in response["coverage"] if row["requirement_id"] in products
    ]
    rows = response["decisions"]
    ids = {r["id"] for r in rows}
    if len(ids) != len(rows):
        raise ValueError("discovery requires unique decision IDs")
    for row in rows:
        if not row["requirement_ids"] or not set(row["requirement_ids"]) <= products:
            raise ValueError("decision must anchor to product clauses")
        if any(
            not str(v).strip()
            for k, v in row.items()
            if k not in {"requirement_ids", "alternatives"}
        ):
            raise ValueError("empty decision fields")
        if not all(v.strip() for v in row["alternatives"].values()):
            raise ValueError("empty alternative")
    coverage = response["coverage"]
    if (
        len(coverage) != len(products)
        or {r["requirement_id"] for r in coverage} != products
    ):
        raise ValueError("coverage must account for every product clause once")
    for row in coverage:
        expected = {
            r["id"] for r in rows if row["requirement_id"] in r["requirement_ids"]
        }
        # Coverage is bookkeeping, not another semantic decision. Derive links
        # from the questions' declared product anchors and retain raw mismatches.
        row["decision_ids"] = sorted(expected)
        if not row["explanation"].strip():
            raise ValueError("coverage explanation is empty")
    return response


def discover(task: str) -> tuple[str, dict[str, Any]]:
    packet = source_packet(task)
    write_json(HERE / "inputs" / f"{task}.json", packet)
    recorder = Recorder(
        _client("deepinfra", DEFAULT_MODEL, 120, 16000),
        HERE / "calls/discovery" / task,
        provider="deepinfra",
        model=DEFAULT_MODEL,
        resume=True,
        repairs=0,
    )
    try:
        response = recorder.call(
            "discover",
            DISCOVERY,
            packet,
            SCHEMA,
            lambda r: checked_discovery(r, packet),
        )
        write_json(HERE / "discovery" / f"{task}.json", response)
        raw = load_json(HERE / "calls/discovery" / task / "discover-0.json")
        kept = {r["requirement_id"] for r in response["coverage"]}
        write_json(
            HERE / "normalization" / f"{task}.json",
            {
                "raw_checkpoint_status": raw["status"],
                "discarded_non_product_coverage_ids": [
                    r["requirement_id"]
                    for r in raw["response"]["coverage"]
                    if r["requirement_id"] not in kept
                ],
                "questions_and_alternatives_changed": False,
                "coverage_link_mismatches": [
                    {
                        "requirement_id": r["requirement_id"],
                        "raw": r["decision_ids"],
                        "derived": next(
                            v["decision_ids"]
                            for v in response["coverage"]
                            if v["requirement_id"] == r["requirement_id"]
                        ),
                    }
                    for r in raw["response"]["coverage"]
                    if r["requirement_id"] in kept
                    and set(r["decision_ids"])
                    != set(
                        next(
                            v["decision_ids"]
                            for v in response["coverage"]
                            if v["requirement_id"] == r["requirement_id"]
                        )
                    )
                ],
            },
        )
        print(task, "discovered", len(response["decisions"]), flush=True)
        return task, response
    except RuntimeError as exc:
        failed = load_json(HERE / "calls/discovery" / task / "discover-0.json")
        if "bounded output limit" in failed.get("error_detail", ""):
            return task, discover_scoped(task)
        write_json(HERE / "discovery" / f"{task}.json", {"failed": str(exc)})
        return task, {"failed": str(exc)}


def discover_scoped(task: str) -> dict[str, Any]:
    """Output-size recovery with smaller subjects, not a repeat of the same call."""
    packet = source_packet(task)
    products = [
        r
        for r in packet["routed_clauses"]
        if r["route"] in {"include", "include_prohibition"}
    ]
    prompt = DISCOVERY + (
        "\nOnly discover questions anchored to active_requirement_ids. Other "
        "requirements are dependency context. Produce at most two questions "
        "per active clause, and avoid inventing questions merely to fill a quota. "
        "Coverage must contain only active clauses. Keep each prose field "
        "under 50 words."
    )
    recovery: dict[str, Any] = {
        "reason": "Bulk response exceeded provider output character guard.",
        "batch_size": 4,
        "maximum_questions_per_batch": 8,
        "prompt": prompt,
        "batches": [],
    }
    combined: dict[str, Any] = {"decisions": [], "coverage": []}
    for offset in range(0, len(products), 4):
        active = products[offset : offset + 4]
        ids = [r["id"] for r in active]
        number = offset // 4 + 1
        batch = f"batch-{number:02}"
        schema = deepcopy(SCHEMA)
        schema["properties"]["decisions"]["maxItems"] = 8
        schema["properties"]["decisions"]["items"]["properties"]["requirement_ids"][
            "items"
        ] = choice_schema(*ids)
        schema["properties"]["coverage"]["items"]["properties"]["requirement_id"] = (
            choice_schema(*ids)
        )
        rec = Recorder(
            _client("deepinfra", DEFAULT_MODEL, 120, 6000),
            HERE / "calls/scoped-discovery" / task / batch,
            provider="deepinfra",
            model=DEFAULT_MODEL,
            resume=True,
            repairs=0,
        )

        def checked(
            row: dict[str, Any],
            schema: dict[str, Any] = schema,
            active: list[dict[str, Any]] = active,
        ) -> dict[str, Any]:
            validate(row, schema)
            return checked_discovery(row, {**packet, "routed_clauses": active})

        try:
            response = rec.call(
                "discover",
                prompt,
                {**packet, "active_requirement_ids": ids},
                schema,
                checked,
            )
        except RuntimeError as exc:
            recovery["batches"].append(
                {"id": batch, "failed": str(exc), "requirement_ids": ids}
            )
            continue
        mapping = {r["id"]: f"{batch}-{r['id']}" for r in response["decisions"]}
        for row in response["decisions"]:
            row["id"] = mapping[row["id"]]
        for row in response["coverage"]:
            row["decision_ids"] = [mapping[value] for value in row["decision_ids"]]
        combined["decisions"].extend(response["decisions"])
        combined["coverage"].extend(response["coverage"])
        recovery["batches"].append(
            {
                "id": batch,
                "requirement_ids": ids,
                "decisions": len(response["decisions"]),
            }
        )
    write_json(HERE / "recovery" / f"{task}.json", recovery)
    write_json(HERE / "discovery" / f"{task}.json", combined)
    print(task, "scoped discovery", len(combined["decisions"]), flush=True)
    return combined


def decide(job: tuple[str, dict[str, Any]]) -> dict[str, Any]:
    task, question = job
    packet = source_packet(task)
    # Stable randomized alternative order, independent of eventual evaluation labels.
    options = {
        k: question["alternatives"][k]
        for k in sorted(("a", "b"), key=lambda k: digest([task, question["id"], k]))
    }
    options["unknown"] = (
        "The available sources do not settle this behavioral distinction."
    )
    packet["decision"] = {k: v for k, v in question.items() if k != "alternatives"}
    packet["options"] = options
    schema = object_schema(choice=choice_schema(*options))
    answers: dict[str, Any] = {"task_id": task, "id": question["id"]}

    def checked_answer(row: dict[str, Any]) -> dict[str, Any]:
        validate(row, schema)
        return row

    for model in ("flash", "jev"):
        directory = HERE / "calls/decisions" / task / question["id"] / model
        client = (
            _client("deepinfra", DEFAULT_MODEL, 120, 256)
            if model == "flash"
            else ObservedJev(directory / "provider.json")
        )
        recorder = Recorder(
            client,
            directory,
            provider="deepinfra" if model == "flash" else "typesafe.ai",
            model=DEFAULT_MODEL
            if model == "flash"
            else os.environ.get("SYSTEM_ONE_MODEL", "jev-latest"),
            resume=True,
            repairs=0,
        )
        try:
            response = recorder.call(
                "choose",
                "Question:\n" + question["question"] + "\n\nInstructions:\n" + SELECT,
                packet,
                schema,
                checked_answer,
            )
            answers[model] = response["choice"]
        except RuntimeError as exc:
            answers[model] = "failed"
            answers[f"{model}_error"] = str(exc)
    same = answers["flash"] == answers["jev"] and answers["flash"] not in {
        "unknown",
        "failed",
    }
    answers["consensus"] = answers["flash"] if same else "unknown"
    answers["rendered"] = options[answers["consensus"]] if same else None
    write_json(HERE / "results" / task / f"{question['id']}.json", answers)
    print(task, question["id"], answers["flash"], answers["jev"], flush=True)
    return answers


def assemble(
    discovered: list[tuple[str, dict[str, Any]]], results: list[dict[str, Any]]
) -> None:
    for task, _response in discovered:
        packet = source_packet(task)
        products = [
            r
            for r in packet["routed_clauses"]
            if r["route"] in {"include", "include_prohibition"}
        ]
        lines = [
            "# Objective",
            "",
            "Implement the requested feature using the existing repository APIs.",
            "",
            "# Requirements",
            "",
        ]
        lines += [f"{i}. {r['text']}" for i, r in enumerate(products, 1)]
        lines += [
            "",
            "# Constraints",
            "",
            "- Powdrr manages branching and commits.",
            "",
            "# Relevant context",
            "",
        ]
        lines += [
            "- Original code paths: "
            + ", ".join(sorted({r["file"] for r in packet["original_code"]}))
            + "."
        ]
        lines += ["", "# Acceptance criteria", ""]
        lines += [
            f"- {r['rendered']}"
            for r in results
            if r["task_id"] == task and r["rendered"]
        ]
        lines += [
            "",
            "The numbered requirements remain completion conditions "
            "when clarification is unresolved.",
            "",
            "# Verification and completion",
            "",
            "- Add or update relevant validations. Run applicable repository "
            "checks and report checks performed, failures and remaining ambiguity.",
            "",
        ]
        path = HERE / "prompts" / f"{task}.md"
        path.parent.mkdir(exist_ok=True)
        path.write_text("\n".join(lines))
    calls = []
    with tarfile.open(HERE / "call-checkpoints.tar.gz", "w:gz") as archive:
        for path in sorted((HERE / "calls").rglob("*.json")):
            archive.add(path, arcname=str(path.relative_to(HERE)))
            row = load_json(path)
            calls.append(
                {
                    "file": str(path.relative_to(HERE)),
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    **(
                        {
                            "receipt": row["receipt"],
                            "status": row["status"],
                            "request_sha256": row["request_sha256"],
                        }
                        if "receipt" in row
                        else {"raw_provider_capture": True}
                    ),
                }
            )
    write_json(HERE / "call-checkpoints.json", {"checkpoints": calls})
    receipts = [c for c in calls if "receipt" in c]
    write_json(
        HERE / "run-summary.json",
        {
            "discovered": {task: len(r.get("decisions", [])) for task, r in discovered},
            "paired_decisions": len(results),
            "provider_calls": len(receipts),
            "providers": dict(Counter(c["receipt"]["provider"] for c in receipts)),
            "statuses": dict(Counter(c["status"] for c in receipts)),
            "summed_provider_seconds": round(
                sum(c["receipt"]["elapsed_seconds"] for c in receipts), 3
            ),
            "tests_run": False,
            "coding_agents_run": False,
        },
    )


def main() -> None:
    definition = {
        "discovery": DISCOVERY,
        "selection": SELECT,
        "schema": SCHEMA,
        "model": DEFAULT_MODEL,
        "jev_model": os.environ.get("SYSTEM_ONE_MODEL", "jev-latest"),
        "input_hashes": {task: digest(source_packet(task)) for task in TASKS},
        "code_excerpt_retrieval": (
            "Reuse manually retrieved original-code excerpts; "
            "question/choice/scope discovery is automatic."
        ),
        "baseline": (
            "Previous revised decisions, audited excluding g-extensions-lifetime."
        ),
        "evaluation": (
            "Post-call source audit of generated choices and selected meanings "
            "plus mapping to frozen previous decision targets."
        ),
        "oracle_access_during_generation": False,
    }
    frozen = HERE / "experiment-definition.json"
    if frozen.exists() and load_json(frozen) != definition:
        raise ValueError("experiment definition changed; use a new output directory")
    write_json(frozen, definition)
    with ThreadPoolExecutor(max_workers=3) as pool:
        discovered = list(pool.map(discover, TASKS))
    jobs = [
        (task, row)
        for task, response in discovered
        for row in response.get("decisions", [])
    ]
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(decide, jobs))
    assemble(discovered, results)


if __name__ == "__main__":
    main()
