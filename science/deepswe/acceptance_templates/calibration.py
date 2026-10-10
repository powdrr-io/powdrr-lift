"""Run small source/criterion contrasts; keep expected labels outside model input."""

from __future__ import annotations

import argparse
from functools import partial
from pathlib import Path
from typing import Any

from .apply_guards import compatible_catalog
from .atomic_review import ASSERTION_SUPPORT_PROMPT, EXTRACT_PROMPT, review_assertions
from .cli import DEFAULT_JUDGE_MODEL, DEFAULT_MODEL, _client
from .common import HERE, Recorder, digest, load_json, source_spans, write_json
from .evaluation import (
    COVERAGE_PROMPT,
    SUPPORT_PROMPT,
    SUPPORT_SCHEMA,
    coverage_response_schema,
    validate_coverage,
    validate_support,
)
from .generation import bind, load_catalog, render_bindings, validate_bindings
from .guarding import confirm_guards


def inputs(case: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    task = {
        "task_id": "contrast-" + digest(case["instruction"])[:12],
        "instruction": case["instruction"],
        "source_spans": source_spans(case["instruction"]),
    }
    inv = {
        "items": [
            {
                "id": "r001",
                "kind": "requirement",
                "text": case["requirement"],
                "source_ids": [row["id"] for row in task["source_spans"]],
            }
        ]
    }
    return task, inv


def fixture_criteria(
    case: dict[str, Any], task: dict[str, Any]
) -> list[dict[str, Any]]:
    return [
        {
            "id": f"c{i:03}",
            "text": row["text"],
            "requirement_ids": ["r001"],
            "source_ids": [span["id"] for span in task["source_spans"]],
            "template_ids": [],
        }
        for i, row in enumerate(case["criteria"], 1)
    ]


def binding_checks(
    case: dict[str, Any],
    catalog: dict[str, Any],
    recorder: Recorder,
    bindings_from: Path | None = None,
) -> list[dict[str, Any]]:
    task, inv = inputs(case)
    selected = [
        {
            "requirement_id": "r001",
            "template_ids": [r["template_id"] for r in case["bindings"]],
        }
    ]
    if bindings_from:
        saved = load_json(bindings_from / case["id"] / "bindings.json")
        decisions = validate_bindings(
            saved, selected=selected, catalog=catalog, task=task
        )
        decisions = confirm_guards(task, inv, catalog, decisions, recorder)
    else:
        decisions = bind(task, inv, catalog, selected, recorder, 6)
    observed = {row["template_id"]: row for row in decisions}
    checks = [
        {
            "case_id": case["id"],
            "phase": "binding",
            "template_id": row["template_id"],
            "expected": row["expected"],
            "observed": observed[row["template_id"]]["applicability"],
            "passed": row["expected"] == observed[row["template_id"]]["applicability"],
        }
        for row in case["bindings"]
    ]
    write_json(
        recorder.directory.parent / "bindings.json",
        {"decisions": decisions, "criteria": render_bindings(decisions, catalog)},
    )
    return checks


def review_checks(
    case: dict[str, Any], recorder: Recorder, coverage_prompt: str, support_prompt: str
) -> list[dict[str, Any]]:
    task, inv = inputs(case)
    criteria = fixture_criteria(case, task)
    support = recorder.call(
        "support",
        support_prompt,
        {**task, "criteria": criteria},
        SUPPORT_SCHEMA,
        partial(validate_support, criteria=criteria, task=task),
    )
    observed = {row["criterion_id"]: row for row in support}
    checks = [
        {
            "case_id": case["id"],
            "phase": "support",
            "criterion_id": criterion["id"],
            "expected": spec["expected_support"],
            "observed": observed[criterion["id"]]["status"],
            "passed": observed[criterion["id"]]["status"] in spec["expected_support"],
        }
        for criterion, spec in zip(criteria, case["criteria"], strict=True)
    ]
    coverage = []
    refs = [
        {
            "id": "v001",
            "behavior": case["requirement"],
            "source_ids": inv["items"][0]["source_ids"],
        }
    ]
    for check in case.get("coverage_checks", []):
        subset = [criteria[i] for i in check["criterion_indices"]]
        generation = {"inventory": inv, "criteria": subset}
        result = recorder.call(
            f"coverage-constrained-{check['id']}",
            coverage_prompt,
            {
                **task,
                "references": refs,
                "requirements": inv["items"],
                "criteria": subset,
            },
            coverage_response_schema(refs, generation),
            partial(validate_coverage, references=refs, generation=generation),
        )
        coverage.append({"check_id": check["id"], "judgments": result})
        actual = result[0]["criterion_status"]
        checks.append(
            {
                "case_id": case["id"],
                "phase": "coverage",
                "check_id": check["id"],
                "expected": check["expected"],
                "observed": actual,
                "passed": actual == check["expected"],
            }
        )
    write_json(
        recorder.directory.parent / "reviews.json",
        {"support": support, "coverage": coverage},
    )
    return checks


def replay_check(
    case: dict[str, Any], recorder: Recorder, coverage_prompt: str, support_prompt: str
) -> list[dict[str, Any]]:
    payload = case["payload"]
    if case["kind"] == "coverage":
        validator = partial(
            validate_coverage,
            references=payload["references"],
            generation={
                "inventory": {"items": payload["requirements"]},
                "criteria": payload["criteria"],
            },
        )
        prompt = coverage_prompt
        id_field, status_field = "reference_id", "criterion_status"
    else:
        validator = partial(
            validate_support, criteria=payload["criteria"], task=payload
        )
        prompt = support_prompt
        id_field, status_field = "criterion_id", "status"
    result = recorder.call(case["kind"], prompt, payload, case["schema"], validator)
    target = next(row for row in result if row[id_field] == case["target_id"])
    original = next(
        row
        for row in case["original_response"][case["kind"]]
        if row[id_field] == case["target_id"]
    )
    write_json(
        recorder.directory.parent / "review.json",
        {
            "kind": case["kind"],
            "judgments": result,
            "original_request_payload_sha256": digest(payload),
            "source_checkpoint_sha256": case["source_checkpoint_sha256"],
        },
    )
    return [
        {
            "case_id": case["id"],
            "phase": case["kind"],
            "target_id": case["target_id"],
            "expected": case["expected"],
            "observed": target[status_field],
            "original_observed": original[status_field],
            "passed": target[status_field] in case["expected"],
        }
    ]


def atomic_check(case: dict[str, Any], recorder: Recorder) -> list[dict[str, Any]]:
    payload = case["payload"]
    criterion = next(
        row for row in payload["criteria"] if row["id"] == case["target_id"]
    )
    result = review_assertions(payload, criterion, recorder)
    write_json(recorder.directory.parent / "assertions.json", result)
    return [
        {
            "case_id": case["id"],
            "phase": "atomic_support",
            "target_id": case["target_id"],
            "expected": case["expected"],
            "observed": result["derived_status"],
            "passed": result["derived_status"] in case["expected"],
        }
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixtures", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--catalog", type=Path, default=HERE / "catalog.json")
    parser.add_argument("--baseline-catalog", type=Path)
    parser.add_argument(
        "--phase", choices=("binding", "review", "replay", "atomic"), required=True
    )
    parser.add_argument("--provider", default="deepinfra")
    parser.add_argument("--model")
    parser.add_argument("--review-version", choices=("v1", "v2"), default="v2")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument(
        "--bindings-from",
        type=Path,
        help="Reuse saved contrast bindings and confirm their prerequisites",
    )
    parser.add_argument("--repairs", type=int, default=1)
    args = parser.parse_args()
    if args.repairs < 0:
        parser.error("repairs must be nonnegative")
    if args.phase == "atomic" and args.review_version != "v2":
        parser.error("atomic review uses the current rubric")
    fixture_path = args.fixtures or HERE / (
        "data/pilot-review-replays.json"
        if args.phase in {"replay", "atomic"}
        else "data/contrasts.json"
    )
    fixtures = load_json(fixture_path)
    if not fixtures["cases"]:
        raise ValueError("no calibration cases")
    catalog = load_catalog(args.catalog)
    baseline = None
    if args.bindings_from:
        if args.phase != "binding":
            parser.error("bindings-from applies only to binding contrasts")
        baseline = load_json(args.bindings_from / "report.json")
        if baseline["fixtures_sha256"] != digest(fixtures):
            raise ValueError("saved bindings must use the same fixtures")
        if baseline["catalog_sha256"] != digest(catalog):
            if args.baseline_catalog is None:
                raise ValueError("saved bindings require the baseline catalog")
            compatible_catalog(
                catalog, load_catalog(args.baseline_catalog), baseline["catalog_sha256"]
            )
    model = args.model or (
        DEFAULT_MODEL if args.phase == "binding" else DEFAULT_JUDGE_MODEL
    )
    client = _client(args.provider, model, 120, 12288)
    old = load_json(HERE / "data/review-prompts-v1.json")
    cp = COVERAGE_PROMPT if args.review_version == "v2" else old["COVERAGE_PROMPT"]
    sp = SUPPORT_PROMPT if args.review_version == "v2" else old["SUPPORT_PROMPT"]
    checks = []
    failures = []
    calls: list[dict[str, Any]] = []
    for case in fixtures["cases"]:
        if args.phase == "binding" and not case["bindings"]:
            continue
        if args.phase == "atomic" and case["kind"] != "support":
            continue
        recorder = Recorder(
            client,
            args.output_dir / case["id"] / "calls",
            provider=args.provider,
            model=model,
            resume=args.resume,
            repairs=args.repairs,
        )
        try:
            if args.phase == "binding":
                checks.extend(
                    binding_checks(case, catalog, recorder, args.bindings_from)
                )
            elif args.phase == "replay":
                checks.extend(replay_check(case, recorder, cp, sp))
            elif args.phase == "atomic":
                checks.extend(atomic_check(case, recorder))
            else:
                checks.extend(review_checks(case, recorder, cp, sp))
        except RuntimeError as exc:
            failures.append({"case_id": case["id"], "error": str(exc)})
        calls.extend({**call, "case_id": case["id"]} for call in recorder.calls)
    report = {
        "label_status": fixtures["label_status"],
        "limitations": fixtures["scope"],
        "fixtures_sha256": digest(fixtures),
        "catalog_sha256": digest(catalog),
        "baseline_report_sha256": digest(baseline) if baseline else None,
        "phase": args.phase,
        "repairs": args.repairs,
        "provider": args.provider,
        "model": model,
        "review_version": args.review_version if args.phase != "binding" else None,
        "review_prompts_sha256": digest(
            {"extract": EXTRACT_PROMPT, "support": ASSERTION_SUPPORT_PROMPT}
            if args.phase == "atomic"
            else {"coverage": cp, "support": sp}
        )
        if args.phase != "binding"
        else None,
        "passed": sum(row["passed"] for row in checks),
        "check_count": len(checks),
        "eligible_case_count": sum(
            bool(case["bindings"])
            if args.phase == "binding"
            else case["kind"] == "support"
            if args.phase == "atomic"
            else True
            for case in fixtures["cases"]
        ),
        "checks": checks,
        "failures": failures,
        "calls": calls,
    }
    write_json(args.output_dir / "report.json", report)
    print(
        f"{report['passed']}/{report['check_count']} contrasts passed; "
        f"{len(failures)} failed cases"
    )
    return int(bool(failures) or report["passed"] != report["check_count"])


if __name__ == "__main__":
    raise SystemExit(main())
