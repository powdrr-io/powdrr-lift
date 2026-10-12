"""Compare template retrieval strategies and audit catalog expressibility."""

from __future__ import annotations

import argparse
import json
import math
import os
import urllib.request
from collections import defaultdict
from pathlib import Path
from typing import Any

from powdrr_lift.workrr.jev_classifier import JevSemanticClassifierClient
from powdrr_lift.workrr.providers import resolve_provider_credentials

from . import PROMPT_VERSION
from .cli import DEFAULT_JUDGE_MODEL, _client
from .common import (
    Recorder,
    choice_schema,
    digest,
    generation_input,
    load_json,
    object_schema,
    write_json,
)
from .generation import load_catalog
from .production_analysis import JevPromptAdapter

AUDIT_PROMPT = "\n".join(
    [
        "You are checking whether a fixed acceptance-criteria template catalog "
        "can express a reference behavior.",
        "The reference validation is the target meaning. It may describe "
        "several independently testable behaviors. Assume it can be split "
        "into any number of separate criteria, and a card may be instantiated "
        "repeatedly with different symbols, fields, conditions, or values.",
        "A template may express only part of the behavior; combine cards and "
        "repeated instances as needed, and return alternative template groups. "
        "A group is the set of distinct cards that must be available; repeated "
        "uses of the same card are represented once.",
        "Every instance must be fillable without changing card text or adding "
        "an assertion, and all prerequisites must be supported by the "
        "reference behavior. A near-miss template does not count. Equivalent "
        "wording is acceptable. Return every plausible sufficient template "
        "group.",
        "Use no_fit only when no combination of cards and instances can "
        "express the validation; use unclear when the template semantics or "
        "target are insufficient to decide. Explain the missing construct for "
        "no_fit. Judge expressibility, not whether the current matcher selected "
        "or rendered it. Return only the requested JSON.",
    ]
)

SHORTLIST_PROMPT = (
    "Choose the single catalog template that is the best fit for this "
    "acceptance validation. A template is a reusable form, not a keyword "
    "match. Respect its applicability and prerequisites. Choose NONE if no "
    "template can express the validation without changing its meaning. This "
    "is a retrieval decision: return the best candidate even when you are not "
    "certain, unless none plausibly fits."
)

AUDIT_SCHEMA = object_schema(
    judgments={
        "type": "array",
        "items": object_schema(
            reference_id={"type": "string"},
            status=choice_schema("expressible", "no_fit", "unclear"),
            template_groups={
                "type": "array",
                "items": {"type": "array", "items": {"type": "string"}},
            },
            rationale={"type": "string"},
            missing_construct={"type": "string"},
        ),
    }
)


def _embedding_request(texts: list[str], model: str) -> list[list[float]]:
    credentials = resolve_provider_credentials("deepinfra")
    payload = json.dumps(
        {"model": model, "input": texts, "encoding_format": "float"}
    ).encode()
    request = urllib.request.Request(
        f"{credentials.base_url.rstrip('/')}/embeddings",
        data=payload,
        headers={
            "Authorization": f"Bearer {credentials.api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        body = json.loads(response.read())
    rows = sorted(body["data"], key=lambda row: row["index"])
    vectors = [row["embedding"] for row in rows]
    if len(vectors) != len(texts):
        raise ValueError("embedding endpoint returned a different item count")
    return vectors


def _embed(texts: list[str], model: str) -> list[list[float]]:
    result = []
    for offset in range(0, len(texts), 96):
        result.extend(_embedding_request(texts[offset : offset + 96], model))
    return result


def _cosine(left: list[float], right: list[float]) -> float:
    numerator = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    return numerator / (left_norm * right_norm) if left_norm and right_norm else 0.0


def _template_text(card: dict[str, Any]) -> str:
    prerequisites = " ".join(
        item.get("description", item.get("question", ""))
        for item in card.get("prerequisites", [])
    )
    return "\n".join(
        part
        for part in (
            card.get("name", ""),
            card.get("applies_when", ""),
            prerequisites,
            card.get("required_sentence", ""),
            card.get("slot_guidance", ""),
            " ".join(card.get("optional_sentences", [])),
        )
        if part
    )


def _validate_audit(
    raw: dict[str, Any], expected: list[dict[str, Any]], valid_ids: set[str]
) -> list[dict[str, Any]]:
    judgments = raw.get("judgments")
    if not isinstance(judgments, list):
        raise ValueError("audit response must contain judgments")
    expected_ids = {row["id"] for row in expected}
    result = []
    seen = set()
    for row in judgments:
        if not isinstance(row, dict) or row.get("reference_id") not in expected_ids:
            raise ValueError("audit response has an unknown reference")
        rid = row["reference_id"]
        groups = row.get("template_groups")
        if (
            rid in seen
            or not isinstance(groups, list)
            or any(
                not isinstance(group, list)
                or not group
                or any(item not in valid_ids for item in group)
                or len(set(group)) != len(group)
                for group in groups
            )
        ):
            raise ValueError("audit response has duplicate or invalid IDs")
        if row.get("status") not in {"expressible", "no_fit", "unclear"}:
            raise ValueError("audit response has an invalid status")
        if (row["status"] == "expressible") != bool(groups):
            raise ValueError("expressible judgments require candidates only")
        if not isinstance(row.get("rationale"), str) or not isinstance(
            row.get("missing_construct"), str
        ):
            raise ValueError("audit response requires rationale and gap")
        seen.add(rid)
        result.append(row)
    if seen != expected_ids:
        raise ValueError("audit response omitted references")
    return result


def _task_rows(
    task_id: str,
    inputs_dir: Path,
    baseline_dir: Path,
    references_dir: Path,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    task = generation_input(inputs_dir / f"{task_id}.json")
    base = baseline_dir / task_id
    analysis = load_json(base / "instruction-analysis/analysis.json")
    generation = load_json(base / "templates/generation.json")
    review = load_json(base / "templates/review.json")
    reference = load_json(references_dir / f"{task_id}.json")
    if generation.get("instruction_analysis_sha256") != digest(analysis):
        raise ValueError(f"baseline analysis fingerprint mismatch for {task_id}")
    return task, analysis, generation, {**reference, "review": review}


def _audit_catalog(
    task_id: str,
    task: dict[str, Any],
    reference: dict[str, Any],
    catalog: dict[str, Any],
    recorder: Recorder,
    batch_size: int,
) -> list[dict[str, Any]]:
    refs = reference["validations"]
    cards = catalog["templates"]
    valid_ids = {card["id"] for card in cards}
    judgments = []
    for offset in range(0, len(refs), batch_size):
        batch = refs[offset : offset + batch_size]

        def validate(
            raw: dict[str, Any], expected_batch: list[dict[str, Any]] = batch
        ) -> list[dict[str, Any]]:
            return _validate_audit(raw, expected_batch, valid_ids)

        response = recorder.call(
            f"catalog-audit-v3-{offset // batch_size:03}",
            AUDIT_PROMPT,
            {
                **task,
                "reference_validations": batch,
                "full_catalog": cards,
            },
            AUDIT_SCHEMA,
            validate,
        )
        judgments.extend(response)
    return judgments


def _jev_shortlist(
    task_id: str,
    refs: list[dict[str, Any]],
    requirement_by_ref: dict[str, str],
    requirements: dict[str, dict[str, Any]],
    cards: list[dict[str, Any]],
    recorder: Recorder,
) -> dict[str, str]:
    card_ids = [card["id"] for card in cards]
    criteria = {card["id"]: f"{card['name']}: {card['applies_when']}" for card in cards}
    result = {}
    for ref in refs:
        rid = ref["id"]
        requirement = requirements[requirement_by_ref[rid]]
        allowed = [*card_ids, "NONE"]

        def validate(
            raw: dict[str, Any], allowed_values: list[str] = allowed
        ) -> dict[str, Any]:
            if raw.get("template_id") not in allowed_values:
                raise ValueError("JEV returned an unknown template")
            return raw

        output = recorder.call(
            f"jev-{rid}",
            SHORTLIST_PROMPT,
            {
                "classifier": {
                    "question": "Which one catalog template is the best fit?",
                    "instructions": [
                        "Select the closest reusable behavior pattern, not the "
                        "closest keyword.",
                        "Return NONE when no card can express the validation.",
                    ],
                    "subject_text": (
                        f"Requirement: {requirement['text']}\n"
                        f"Reference validation: {ref['behavior']}"
                    ),
                    "allowed_values": allowed,
                    "criteria": {**criteria, "NONE": "No catalog template fits."},
                }
            },
            object_schema(template_id=choice_schema(*allowed)),
            validate,
        )
        result[rid] = output["template_id"]
    return result


def _linked_requirements(
    references: list[dict[str, Any]], inventory: list[dict[str, Any]]
) -> dict[str, list[str]]:
    result = {}
    for ref in references:
        source_ids = set(ref.get("source_ids", []))
        matches = [
            row["id"]
            for row in inventory
            if source_ids.intersection(row.get("source_ids", []))
        ]
        result[ref["id"]] = matches
    return result


def _metrics(
    references: list[dict[str, Any]],
    audits: dict[str, dict[str, Any]],
    candidates: dict[str, set[str]],
) -> dict[str, Any]:
    eligible = [
        row for row in references if audits[row["id"]]["status"] == "expressible"
    ]
    hits = sum(
        any(
            set(group).issubset(candidates.get(row["id"], set()))
            for group in audits[row["id"]]["template_groups"]
        )
        for row in eligible
    )
    candidate_count = sum(len(candidates.get(row["id"], set())) for row in eligible)
    useful_ids = {
        row["id"]: set().union(
            *(set(group) for group in audits[row["id"]]["template_groups"])
        )
        for row in eligible
    }
    correct_pairs = sum(
        len(useful_ids[row["id"]] & candidates.get(row["id"], set()))
        for row in eligible
    )
    return {
        "eligible_references": len(eligible),
        "candidate_recall": hits / len(eligible) if eligible else None,
        "complete_group_recall": hits / len(eligible) if eligible else None,
        "mean_candidates": candidate_count / len(eligible) if eligible else None,
        "candidate_precision_proxy": correct_pairs / candidate_count
        if candidate_count
        else None,
        "hits": hits,
    }


def run(args: argparse.Namespace) -> dict[str, Any]:
    catalog = load_catalog(args.catalog)
    cards = catalog["templates"]
    card_by_id = {card["id"]: card for card in cards}
    all_rows = []
    model = _client(args.provider, args.model, 120, 12288)
    for task_id in args.tasks:
        task, analysis, generation, reference = _task_rows(
            task_id, args.inputs_dir, args.baseline_dir, args.references_dir
        )
        refs = reference["validations"]
        inv = analysis["inventory"]["items"]
        reqs = {row["id"]: row for row in inv}
        reqs_for_refs = _linked_requirements(refs, inv)
        if any(not reqs_for_refs[row["id"]] for row in refs):
            raise ValueError(f"a reference has no instruction-ledger link in {task_id}")

        task_dir = args.output_dir / task_id
        audit_recorder = Recorder(
            model,
            task_dir / "catalog-audit/calls",
            provider=args.provider,
            model=args.model,
            resume=args.resume,
            repairs=args.repairs,
        )
        audits_list = _audit_catalog(
            task_id, task, reference, catalog, audit_recorder, args.audit_batch_size
        )
        audits = {row["reference_id"]: row for row in audits_list}
        write_json(task_dir / "catalog-audit/audit.json", {"judgments": audits_list})

        referenced_req_ids = sorted(
            {req_id for rows in reqs_for_refs.values() for req_id in rows}
        )
        req_texts = [reqs[req_id]["text"] for req_id in referenced_req_ids]
        doc_texts = [_template_text(card) for card in cards]
        scores_path = task_dir / "embeddings/scores.json"
        scores_by_req = None
        if args.resume and scores_path.exists():
            saved_scores = load_json(scores_path)
            if saved_scores.get("model") == args.embedding_model and saved_scores.get(
                "template_text_sha256"
            ) == digest(doc_texts):
                scores_by_req = saved_scores.get("scores_by_requirement")
        if scores_by_req is None:
            vectors = _embed([*doc_texts, *req_texts], args.embedding_model)
            template_vectors = vectors[: len(cards)]
            req_vectors = dict(
                zip(referenced_req_ids, vectors[len(cards) :], strict=True)
            )
            scores_by_req = {
                req_id: {
                    card["id"]: _cosine(req_vectors[req_id], template_vectors[index])
                    for index, card in enumerate(cards)
                }
                for req_id in referenced_req_ids
            }
            write_json(
                scores_path,
                {
                    "model": args.embedding_model,
                    "template_text_sha256": digest(doc_texts),
                    "scores_by_requirement": scores_by_req,
                },
            )

        jev_client = JevPromptAdapter(
            JevSemanticClassifierClient(model, fail_closed=True)
        )
        jev_recorder = Recorder(
            jev_client,
            task_dir / "jev-shortlist/calls",
            provider="typesafe.ai",
            model=os.environ.get("SYSTEM_ONE_MODEL", "jev-latest"),
            resume=args.resume,
            repairs=args.repairs,
        )
        primary_req_by_ref = {ref["id"]: reqs_for_refs[ref["id"]][0] for ref in refs}
        jev_result = _jev_shortlist(
            task_id, refs, primary_req_by_ref, reqs, cards, jev_recorder
        )
        jev_client.close()

        selections = {
            row["requirement_id"]: set(row.get("template_ids", []))
            for row in generation.get("selections", [])
        }
        llm_candidates = {
            ref["id"]: set().union(
                *(selections.get(req_id, set()) for req_id in reqs_for_refs[ref["id"]])
            )
            for ref in refs
        }
        jev_candidates = {
            ref["id"]: ({jev_result[ref["id"]]} - {"NONE"}) for ref in refs
        }
        ref_scores = {
            ref["id"]: {
                card_id: max(
                    scores_by_req[req_id][card_id]
                    for req_id in reqs_for_refs[ref["id"]]
                )
                for card_id in card_by_id
            }
            for ref in refs
        }

        reference_mapping = {
            row["id"]: {
                "status": "expressible" if row.get("template_groups") else "no_fit",
                "template_groups": row.get("template_groups", []),
            }
            for row in refs
        }
        candidate_sets = {"llm_full_catalog": llm_candidates}
        for threshold in args.thresholds:
            embedded = {
                ref["id"]: {
                    card_id
                    for card_id, score in ref_scores[ref["id"]].items()
                    if score >= threshold
                }
                for ref in refs
            }
            hybrid = {
                ref["id"]: embedded[ref["id"]]
                | llm_candidates[ref["id"]]
                | jev_candidates[ref["id"]]
                for ref in refs
            }
            candidate_sets[f"embedding>={threshold:.2f}"] = embedded
            candidate_sets[f"llm+jev+embedding>={threshold:.2f}"] = hybrid
        candidate_sets["jev_top1"] = jev_candidates
        method_results = {
            method: {
                "catalog_audit_oracle": _metrics(refs, audits, candidate_set),
                "reference_mapping_oracle": _metrics(
                    refs, reference_mapping, candidate_set
                ),
            }
            for method, candidate_set in candidate_sets.items()
        }

        review = reference["review"]
        coverage = {row["reference_id"]: row for row in review.get("coverage", [])}
        selected_pairs = defaultdict(set)
        for row in generation.get("selections", []):
            selected_pairs[row["requirement_id"]].update(row.get("template_ids", []))
        diagnosis = []
        for ref in refs:
            audit = audits[ref["id"]]
            linked = reqs_for_refs[ref["id"]]
            retrieved = set().union(*(selected_pairs[req_id] for req_id in linked))
            cover = coverage.get(ref["id"], {})
            status = audit["status"]
            annotated_groups = ref.get("template_groups", [])
            audit_groups = audit["template_groups"]
            linked_kinds = {reqs[req_id].get("kind") for req_id in linked}
            linked_routes = {reqs[req_id].get("route") for req_id in linked}
            if not annotated_groups and status == "no_fit":
                cause = "catalog_gap_candidate"
            elif not annotated_groups or status == "unclear":
                cause = "needs_catalog_adjudication"
            elif "requirement" not in linked_kinds:
                cause = "upstream_route_miss"
            elif not any(set(group).issubset(retrieved) for group in annotated_groups):
                cause = "retrieval_miss"
            elif cover.get("criterion_status") != "full":
                cause = "binding_or_rendering_miss"
            else:
                cause = "covered"
            diagnosis.append(
                {
                    "reference_id": ref["id"],
                    "behavior": ref["behavior"],
                    "catalog_status": status,
                    "oracle_template_groups": audit_groups,
                    "reference_template_groups": annotated_groups,
                    "audit_reference_group_agreement": any(
                        set(left) == set(right)
                        for left in audit_groups
                        for right in annotated_groups
                    ),
                    "catalog_audit_conflict": status == "no_fit"
                    and bool(annotated_groups),
                    "retrieved_template_ids": sorted(retrieved),
                    "source_item_kinds": sorted(str(value) for value in linked_kinds),
                    "source_routes": sorted(str(value) for value in linked_routes),
                    "criterion_status": cover.get("criterion_status", "missing"),
                    "cause": cause,
                    "catalog_rationale": audit["rationale"],
                    "missing_construct": audit["missing_construct"],
                }
            )
        output = {
            "task_id": task_id,
            "audit_call_count": len(audit_recorder.calls),
            "jev_call_count": len(jev_recorder.calls),
            "catalog_status_counts": {
                status: sum(row["status"] == status for row in audits_list)
                for status in ("expressible", "no_fit", "unclear")
            },
            "retrieval_metrics": method_results,
            "diagnosis": diagnosis,
        }
        write_json(task_dir / "retrieval-results.json", output)
        all_rows.append(output)

    aggregate_methods: dict[str, Any] = {}
    method_names = sorted(
        {name for task_result in all_rows for name in task_result["retrieval_metrics"]}
    )
    for method in method_names:
        aggregate_methods[method] = {}
        for oracle_name in ("catalog_audit_oracle", "reference_mapping_oracle"):
            parts = [
                row["retrieval_metrics"][method][oracle_name]
                for row in all_rows
                if method in row["retrieval_metrics"]
            ]
            count = sum(part["eligible_references"] for part in parts)
            hits = sum(part["hits"] for part in parts)
            candidate_total = sum(
                (part["mean_candidates"] or 0) * part["eligible_references"]
                for part in parts
            )
            aggregate_methods[method][oracle_name] = {
                "eligible_references": count,
                "hits": hits,
                "complete_group_recall": hits / count if count else None,
                "mean_candidates": candidate_total / count if count else None,
            }
    aggregate_statuses = {
        status: sum(row["catalog_status_counts"][status] for row in all_rows)
        for status in ("expressible", "no_fit", "unclear")
    }
    diagnosis_counts: dict[str, int] = {}
    for task_result in all_rows:
        for row in task_result["diagnosis"]:
            diagnosis_counts[row["cause"]] = diagnosis_counts.get(row["cause"], 0) + 1
    report = {
        "schema_version": "catalog-retrieval-experiment-v1",
        "prompt_version": PROMPT_VERSION,
        "baseline_run": str(args.baseline_dir),
        "catalog_sha256": digest(catalog),
        "audit_model": {"provider": args.provider, "model": args.model},
        "embedding_model": args.embedding_model,
        "jev_model": os.environ.get("SYSTEM_ONE_MODEL", "jev-latest"),
        "thresholds": args.thresholds,
        "catalog_audit_status_counts": aggregate_statuses,
        "pipeline_diagnosis_counts": diagnosis_counts,
        "retrieval_metrics": aggregate_methods,
        "tasks": all_rows,
    }
    write_json(args.output_dir / "report.json", report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--inputs-dir", type=Path, default=Path(__file__).parent / "data/inputs"
    )
    parser.add_argument(
        "--references-dir", type=Path, default=Path(__file__).parent / "data/references"
    )
    parser.add_argument(
        "--baseline-dir",
        type=Path,
        default=Path(__file__).parent / "runs/production-routed-v1",
    )
    parser.add_argument(
        "--catalog", type=Path, default=Path(__file__).parent / "catalog.json"
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--tasks", nargs="+", required=True)
    parser.add_argument("--provider", default="deepinfra")
    parser.add_argument("--model", default=DEFAULT_JUDGE_MODEL)
    parser.add_argument("--embedding-model", default="BAAI/bge-m3")
    parser.add_argument(
        "--thresholds", nargs="+", type=float, default=[0.35, 0.45, 0.55, 0.65]
    )
    parser.add_argument("--audit-batch-size", type=int, default=3)
    parser.add_argument("--repairs", type=int, default=2)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args(argv)
    if not args.thresholds or any(not -1 <= value <= 1 for value in args.thresholds):
        parser.error("thresholds must be cosine similarities from -1 to 1")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    report = run(args)
    print(json.dumps({"tasks": len(report["tasks"]), "output": str(args.output_dir)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
