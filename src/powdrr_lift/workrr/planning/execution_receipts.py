"""Load externally observed requirement receipts for a pinned plan run."""

from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
from collections.abc import Mapping
from pathlib import Path

from powdrr_service.planning.evidence import Evidence, EvidenceError
from powdrr_service.verification.requirement_execution import (
    RequirementExecutionReceipt,
)

MANIFEST_SCHEMA = "requirement-execution-evidence-manifest-v1"
RECEIPT_ARTIFACT_NAME = "powdrr-requirement-execution-receipts"


def load_requirement_execution_receipts(
    path: Path,
    *,
    pinned_commit: str,
) -> tuple[dict[str, tuple[Evidence, ...]], dict[str, str]]:
    """Load a strict receipt manifest and bind entries to this pinned revision."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise EvidenceError(
            f"could not read execution receipt manifest: {error}"
        ) from error
    if not isinstance(raw, Mapping) or set(raw) != {
        "schema_version",
        "base_commit",
        "receipts",
    }:
        raise EvidenceError("execution receipt manifest fields are invalid")
    if raw.get("schema_version") != MANIFEST_SCHEMA:
        raise EvidenceError("unsupported execution receipt manifest schema")
    if raw.get("base_commit") != pinned_commit:
        raise EvidenceError("execution receipt manifest uses a different base commit")
    records = raw.get("receipts")
    if not isinstance(records, list):
        raise EvidenceError("execution receipt manifest receipts must be a list")

    evidence_by_requirement: dict[str, tuple[Evidence, ...]] = {}
    excerpts_by_evidence: dict[str, str] = {}
    seen_evidence_ids: set[str] = set()
    for record in records:
        if not isinstance(record, Mapping) or set(record) != {"evidence", "receipt"}:
            raise EvidenceError("execution receipt manifest entry is invalid")
        evidence_raw = record.get("evidence")
        receipt_raw = record.get("receipt")
        if not isinstance(evidence_raw, Mapping) or not isinstance(
            receipt_raw, Mapping
        ):
            raise EvidenceError("execution receipt manifest entry is incomplete")
        try:
            evidence = Evidence.from_mapping(evidence_raw)
            receipt = RequirementExecutionReceipt.from_data(receipt_raw)
        except (EvidenceError, ValueError, TypeError) as error:
            raise EvidenceError(f"invalid execution receipt entry: {error}") from error
        if evidence.kind != "execution_receipt":
            raise EvidenceError("receipt manifest evidence must be execution_receipt")
        if evidence.base_commit != pinned_commit:
            raise EvidenceError("receipt evidence uses a different base commit")
        if evidence.start_line != 1 or evidence.end_line != 1:
            raise EvidenceError("receipt evidence must bind one canonical JSON line")
        receipt_data = receipt.to_data()
        if (
            receipt_data["source_commit"] != pinned_commit
            or receipt_data["candidate_commit"] != pinned_commit
        ):
            raise EvidenceError("receipt does not match the pinned source candidate")
        excerpt = (
            json.dumps(
                receipt_data,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            )
            + "\n"
        )
        if (
            hashlib.sha256(excerpt.encode("utf-8")).hexdigest()
            != evidence.content_sha256
        ):
            raise EvidenceError("receipt evidence digest does not match its content")
        requirement_id = str(receipt_data["requirement_id"])
        if requirement_id in evidence_by_requirement:
            raise EvidenceError(
                f"multiple execution receipts supplied for {requirement_id}"
            )
        if evidence.id in seen_evidence_ids:
            raise EvidenceError("execution receipt evidence IDs must be unique")
        seen_evidence_ids.add(evidence.id)
        evidence_by_requirement[requirement_id] = (evidence,)
        excerpts_by_evidence[evidence.id] = excerpt
    return evidence_by_requirement, excerpts_by_evidence


def discover_requirement_execution_receipts(
    repository: str,
    *,
    pinned_commit: str,
) -> tuple[dict[str, tuple[Evidence, ...]], dict[str, str]]:
    """Find the latest successful GitHub Actions receipt artifact for a commit."""
    try:
        result = subprocess.run(
            [
                "gh",
                "run",
                "list",
                "--repo",
                repository,
                "--commit",
                pinned_commit,
                "--json",
                "databaseId,headSha,status,conclusion",
                "--limit",
                "100",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise EvidenceError(f"could not list GitHub workflow runs: {error}") from error
    if result.returncode != 0:
        raise EvidenceError(
            "could not list GitHub workflow runs for the pinned commit: "
            + result.stderr.strip()[-500:]
        )
    try:
        runs = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise EvidenceError("GitHub returned invalid workflow run data") from error
    if not isinstance(runs, list):
        raise EvidenceError("GitHub workflow run data must be a list")
    eligible_runs = [
        run
        for run in runs
        if isinstance(run, Mapping)
        and run.get("headSha") == pinned_commit
        and run.get("status") == "completed"
        and run.get("conclusion") == "success"
        and isinstance(run.get("databaseId"), int)
        and not isinstance(run.get("databaseId"), bool)
    ][:25]

    for run in eligible_runs:
        run_id = run["databaseId"]
        with tempfile.TemporaryDirectory(prefix="powdrr-receipt-artifact-") as folder:
            try:
                download = subprocess.run(
                    [
                        "gh",
                        "run",
                        "download",
                        str(run_id),
                        "--repo",
                        repository,
                        "--name",
                        RECEIPT_ARTIFACT_NAME,
                        "--dir",
                        folder,
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
            except (OSError, subprocess.TimeoutExpired) as error:
                raise EvidenceError(
                    f"could not download receipt artifact from run {run_id}: {error}"
                ) from error
            if download.returncode != 0:
                details = download.stderr.strip()
                if (
                    "no artifact" in details.casefold()
                    or "no artifacts" in details.casefold()
                ):
                    continue
                raise EvidenceError(
                    f"could not download receipt artifact from workflow run {run_id}: "
                    + details[-500:]
                )
            manifests = []
            for manifest_path in Path(folder).rglob("*.json"):
                try:
                    candidate = json.loads(manifest_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if (
                    isinstance(candidate, Mapping)
                    and candidate.get("schema_version") == MANIFEST_SCHEMA
                ):
                    manifests.append(manifest_path)
            if len(manifests) != 1:
                raise EvidenceError(
                    "receipt artifact must contain exactly one recognized JSON manifest"
                )
            return load_requirement_execution_receipts(
                manifests[0], pinned_commit=pinned_commit
            )
    return {}, {}


__all__ = [
    "MANIFEST_SCHEMA",
    "RECEIPT_ARTIFACT_NAME",
    "discover_requirement_execution_receipts",
    "load_requirement_execution_receipts",
]
