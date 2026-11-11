"""Local execution and artifact transport for service-owned story proof policy."""

from __future__ import annotations

import json
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from uuid import uuid4

from powdrr_agent.execution.execute_pr_tests import ValidationProfile
from powdrr_agent.execution.story_test_collection import (
    capture_story_execution_context,
    collect_story_tests,
    run_story_tests,
)
from powdrr_service.verification.story_execution import (
    compile_story_execution_receipts,
    story_coverage_report,
)


def assertion_line_catalog(manifest: Mapping[str, Any]) -> dict[str, list[int]]:
    lines: dict[str, set[int]] = {}
    for link in manifest.get("links", []):
        lines.setdefault(link["path"], set()).update(
            assertion["start_line"] for assertion in link["assertions"]
        )
    return {path: sorted(values) for path, values in lines.items()}


def execute_story_validation(
    root: Path,
    *,
    snapshot: Mapping[str, Any],
    authoring: Mapping[str, Any],
    profile: ValidationProfile,
    repository: str,
    artifact_root: Path,
    timeout_seconds: float,
    excluded_paths: Sequence[str] = (),
) -> dict[str, Any]:
    """Capture actual observations, delegate proof decisions, retain every run."""
    manifest = authoring["story_test_links"]
    paths = tuple(authoring["test_sources"])
    collection = collect_story_tests(
        profile, root=root, paths=paths, timeout_seconds=timeout_seconds
    )
    runtime = collection.get("runtime", {"status": "unavailable"})
    before = capture_story_execution_context(
        root,
        repository=repository,
        command=profile.command,
        excluded_paths=excluded_paths,
        runtime=runtime,
    )
    if collection.get("status") == "collected":
        result = run_story_tests(
            profile,
            root=root,
            paths=paths,
            assertion_lines=assertion_line_catalog(manifest),
            timeout_seconds=timeout_seconds,
        )
    else:
        result = {
            "passed": False,
            "returncode": None,
            "command": list(profile.command),
            "stdout": "",
            "stderr": "",
            "error": collection.get("error"),
            "story_execution": {"status": "unavailable"},
        }
    observed = result.get("story_execution", {})
    after = capture_story_execution_context(
        root,
        repository=repository,
        command=profile.command,
        excluded_paths=excluded_paths,
        runtime=observed.get("runtime_after", runtime),
    )
    if observed.get("runtime") != runtime or observed.get("runtime") != observed.get(
        "runtime_after"
    ):
        result["coverage_evidence_error"] = (
            "execution runtime differs from the collected environment"
        )
    run_id = f"{time.time_ns()}-{uuid4().hex}"
    destination = artifact_root / run_id
    destination.mkdir(parents=True, exist_ok=False)
    execution_path = destination / "execution.json"
    execution_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    receipts = compile_story_execution_receipts(
        snapshot=snapshot,
        manifest=manifest,
        review=authoring.get("accepted_story_review", {}),
        review_evidence=authoring.get("story_review_evidence", {}),
        context=before,
        context_after=after,
        execution=result,
        artifact_refs=(str(execution_path),),
    )
    selected = sorted(
        {ref["story_id"] for link in manifest["links"] for ref in link["story_refs"]}
    )
    coverage = story_coverage_report(
        snapshot,
        context=after,
        manifest=manifest,
        receipts=receipts,
        story_ids=selected,
    )
    payload = {
        "snapshot": dict(snapshot),
        "context": after,
        "manifest": dict(manifest),
        "receipts": [receipt.to_data() for receipt in receipts],
        "story_ids": selected,
    }
    (destination / "receipts.json").write_text(
        json.dumps(payload["receipts"], indent=2, sort_keys=True) + "\n"
    )
    (destination / "coverage.json").write_text(
        json.dumps(coverage, indent=2, sort_keys=True) + "\n"
    )
    (destination / "verification-health-input.json").write_text(
        json.dumps({"story_coverage": payload}, indent=2, sort_keys=True) + "\n"
    )
    result.update(
        story_receipts=payload["receipts"],
        story_coverage=coverage,
        execution_context=after,
        story_evidence_path=str(destination),
    )
    return result
