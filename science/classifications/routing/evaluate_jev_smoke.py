#!/usr/bin/env python3
"""Run an authored development smoke set against the production Jev request."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from powdrr_lift.core.semantic_decision import SemanticDecisionSpec  # noqa: E402
from powdrr_lift.workrr import jev_classifier  # noqa: E402
from powdrr_lift.workrr.semantic_contract_compiler import (  # noqa: E402
    CLASSIFIER_DEFINITIONS,
    SOURCE_CLASSIFIER_REVISION,
    _classifier_request,
)

LABELS = ("context", "include", "include_prohibition", "exclude", "unclear")
DEFAULT_CASES = Path(__file__).with_name("jev_smoke_cases.jsonl")
DEFAULT_DOCUMENTS = Path(__file__).with_name("jev_smoke_documents.json")


def _load_cases(path: Path) -> list[dict[str, Any]]:
    cases = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if not cases:
        raise ValueError("case file is empty")
    seen: set[str] = set()
    for case in cases:
        if (
            not isinstance(case.get("id"), str)
            or case["id"] in seen
            or case.get("label") not in LABELS
            or not isinstance(case.get("proposition"), str)
            or (
                case.get("local_context") is not None
                and not isinstance(case.get("local_context"), str)
            )
        ):
            raise ValueError(f"invalid or duplicate smoke case: {case!r}")
        seen.add(case["id"])
    return cases


def _load_documents(path: Path) -> dict[str, str]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in raw.items()
    ):
        raise ValueError("document fixture must map string IDs to source text")
    return raw


def _provider_request(
    case: dict[str, Any], variant: str, documents: dict[str, str]
) -> dict[str, Any]:
    spec = SemanticDecisionSpec(
        decision_id=f"smoke:{case['id']}:routing",
        decision_kind="routing",
        subject_ref=f"smoke:{case['id']}",
        proposition_text=case["proposition"],
        source_fingerprint=hashlib.sha256(
            case["proposition"].encode("utf-8")
        ).hexdigest(),
        contract_revision="routing-smoke-v1",
        context_text=case["local_context"],
    )
    request = _classifier_request(spec, CLASSIFIER_DEFINITIONS["routing"])
    document_ref = case.get("instruction_text_ref")
    source_text = (
        documents[document_ref]
        if isinstance(document_ref, str)
        else f"{case['local_context']}\n{case['proposition']}"
        if case["local_context"]
        else case["proposition"]
    )
    ledger_clauses = [
        {
            "clause_id": f"smoke:{case['id']}:source-{ordinal}",
            "ordinal": ordinal,
            "text": line.strip(),
        }
        for ordinal, line in enumerate(source_text.splitlines(), start=1)
        if line.strip()
    ]
    context = {
        "routing_request": request,
        "atomic_instruction_ledger": {
            "source_text": source_text,
            "clauses": ledger_clauses,
        },
    }
    messages = [
        {"role": "system", "content": "Classify the source proposition."},
        {
            "role": "user",
            "content": "Question:\n"
            + str(request["question"])
            + "\n\nContext:\n"
            + json.dumps(context, ensure_ascii=False),
        },
    ]
    provider_request = jev_classifier._classifier_request(
        messages,
        {
            "type": "object",
            "properties": {"status": {}, "value": {}, "reason_code": {}},
        },
    )
    if provider_request is None:
        raise ValueError(f"could not build Jev request for {case['id']}")
    if variant == "baseline":
        envelope = json.loads(messages[-1]["content"].split("Context:\n", 1)[1])
        classifier = envelope["routing_request"]
        legacy_classifier = {
            key: value
            for key, value in classifier.items()
            if key not in {"criteria", "provider_state"}
        }
        provider_request = {
            **provider_request,
            "criteria": {},
            "instructions": [messages[-1]["content"].split("Context:\n", 1)[0]]
            + list(classifier["instructions"]),
            "state": {
                "source_text": classifier["subject_text"],
                "request": legacy_classifier,
                "context": {"routing_request": legacy_classifier},
            },
        }
    return provider_request


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--documents", type=Path, default=DEFAULT_DOCUMENTS)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", default=os.environ.get("SYSTEM_ONE_MODEL"))
    parser.add_argument(
        "--variant", choices=("baseline", "described"), default="described"
    )
    parser.add_argument("--allow-live", action="store_true")
    args = parser.parse_args()
    if not args.allow_live:
        parser.error("live Jev calls require --allow-live")
    if args.output.exists():
        parser.error(f"refusing to overwrite report: {args.output}")
    api_key = (
        os.environ.get("TYPESAFEAI_API_KEY")
        or os.environ.get("TYPESAFE_API_KEY")
        or os.environ.get("SYSTEM_ONE_API_KEY")
    )
    if not api_key:
        parser.error("set a TypeSafe API key in the environment")

    cases = _load_cases(args.cases)
    documents = _load_documents(args.documents)
    for case in cases:
        document_ref = case.get("instruction_text_ref")
        if document_ref is not None and document_ref not in documents:
            parser.error(f"missing instruction text document: {document_ref}")
    results = []
    for case in cases:
        request = _provider_request(case, args.variant, documents)
        try:
            response = jev_classifier._call_jev(
                request,
                api_key,
                jev_classifier.JEV_DEFAULT_BASE_URL,
                args.model or jev_classifier.JEV_MODEL,
            )
            answer = response["answer"]
            predicted = answer.get("choice")
            if predicted not in (*LABELS, "unresolved"):
                raise ValueError("Jev returned an unsupported route")
            results.append(
                {
                    "id": case["id"],
                    "family_id": case["family_id"],
                    "expected": case["label"],
                    "predicted": predicted if predicted != "unresolved" else None,
                    "abstained": predicted == "unresolved",
                    "confidence": answer.get("confidence"),
                    "probabilities": answer.get("probabilities"),
                    "model": response.get("model"),
                    "usage": response.get("usage"),
                }
            )
        except Exception as exc:  # noqa: BLE001
            results.append(
                {
                    "id": case["id"],
                    "family_id": case["family_id"],
                    "expected": case["label"],
                    "predicted": None,
                    "error_type": type(exc).__name__,
                }
            )

    confusion: dict[str, Counter[str]] = {label: Counter() for label in LABELS}
    correct = 0
    for result in results:
        predicted = result["predicted"]
        if predicted is not None:
            confusion[result["expected"]][predicted] += 1
            correct += predicted == result["expected"]
    report = {
        "schema_version": "jev-routing-smoke-report-v1",
        "variant": args.variant,
        "classifier_revision": SOURCE_CLASSIFIER_REVISION,
        "evaluation_scope": "authored_development_smoke_only",
        "annotation_status": "single_author_reviewed_not_independent_gold",
        "generated_at": datetime.now(UTC).isoformat(),
        "cases_sha256": hashlib.sha256(args.cases.read_bytes()).hexdigest(),
        "documents_sha256": hashlib.sha256(args.documents.read_bytes()).hexdigest(),
        "implementation_sha256": hashlib.sha256(
            Path(__file__).read_bytes()
            + (ROOT / "src/powdrr_lift/workrr/jev_classifier.py").read_bytes()
            + (
                ROOT / "src/powdrr_lift/workrr/semantic_contract_compiler.py"
            ).read_bytes()
        ).hexdigest(),
        "cases": len(cases),
        "correct": correct,
        "accuracy_with_errors_as_incorrect": correct / len(cases),
        "errors": sum(result["predicted"] is None for result in results),
        "confusion": {expected: dict(row) for expected, row in confusion.items()},
        "requested_model": args.model or jev_classifier.JEV_MODEL,
        "results": results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "cases": report["cases"],
                "correct": correct,
                "accuracy": report["accuracy_with_errors_as_incorrect"],
                "errors": report["errors"],
                "requested_model": report["requested_model"],
                "output": str(args.output),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
