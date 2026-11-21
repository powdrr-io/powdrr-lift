"""Load externally observed requirement receipts for a pinned plan run."""

from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml
from powdrr_service.planning.evidence import Evidence, EvidenceError
from powdrr_service.verification.requirement_execution import (
    RequirementExecutionReceipt,
    receipt_from_verification_evidence,
)

MANIFEST_SCHEMA = "requirement-execution-evidence-manifest-v1"
RECEIPT_ARTIFACT_NAME = "powdrr-requirement-execution-receipts"
VERIFICATION_RUN_SCHEMA = "verification-evidence-run-v1"


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
        if receipt_data["outcome"] != "passed":
            raise EvidenceError(
                "execution receipt manifests may contain only passed receipts"
            )
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


def write_requirement_execution_receipt_manifest(
    directory: Path,
    *,
    pinned_commit: str,
    receipts: Sequence[RequirementExecutionReceipt],
) -> Path:
    """Write the named GitHub artifact payload from explicitly bound receipts.

    The caller obtains each receipt from observed verification evidence and
    supplies its requirement ID and digest to the verifier adapter. This
    function performs only deterministic serialization and structural checks.
    """
    if (
        not isinstance(pinned_commit, str)
        or len(pinned_commit) not in {40, 64}
        or any(character not in "0123456789abcdef" for character in pinned_commit)
    ):
        raise EvidenceError("receipt manifest requires a pinned commit")
    records: list[dict[str, object]] = []
    seen_requirements: set[str] = set()
    for index, receipt in enumerate(
        sorted(receipts, key=lambda item: item.to_data()["requirement_id"]), start=1
    ):
        data = receipt.to_data()
        if data["outcome"] != "passed":
            raise EvidenceError("only passed execution receipts can be published")
        if (
            data["source_commit"] != pinned_commit
            or data["candidate_commit"] != pinned_commit
        ):
            raise EvidenceError("receipt does not match the pinned source candidate")
        requirement_id = str(data["requirement_id"])
        if requirement_id in seen_requirements:
            raise EvidenceError(
                f"multiple execution receipts supplied for {requirement_id}"
            )
        seen_requirements.add(requirement_id)
        excerpt = (
            json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            + "\n"
        )
        evidence_path = (
            Path("artifacts/requirement-execution-receipts/receipts")
            / f"{data['receipt_id'].removeprefix('receipt:')}.json"
        )
        evidence = {
            "id": f"evidence-{index:04d}",
            "base_commit": pinned_commit,
            "path": evidence_path.as_posix(),
            "start_line": 1,
            "end_line": 1,
            "content_sha256": hashlib.sha256(excerpt.encode("utf-8")).hexdigest(),
            "kind": "execution_receipt",
            "symbol": None,
        }
        records.append({"evidence": evidence, "receipt": data})

    artifact_dir = directory / RECEIPT_ARTIFACT_NAME
    receipt_dir = artifact_dir / "receipts"
    receipt_dir.mkdir(parents=True, exist_ok=True)
    for record in records:
        receipt_data = record["receipt"]
        assert isinstance(receipt_data, Mapping)
        receipt_path = (
            receipt_dir / f"{receipt_data['receipt_id'].removeprefix('receipt:')}.json"
        )
        receipt_path.write_text(
            json.dumps(
                receipt_data,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
    manifest_path = artifact_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": MANIFEST_SCHEMA,
                "base_commit": pinned_commit,
                "receipts": records,
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    return manifest_path


def produce_requirement_execution_receipt_manifest(
    directory: Path,
    *,
    pinned_commit: str,
    candidate_tree: str,
    run_id: str,
    evidence_records: Sequence[Mapping[str, object]],
    obligations: Sequence[Mapping[str, object]],
    requirement_bindings: Mapping[str, Sequence[Mapping[str, str]]],
    evidence_artifact_refs: Mapping[str, str],
) -> Path:
    """Build and write receipts from an explicitly bound verifier run.

    This is the producer boundary for CI integrations: the caller supplies the
    exact plan requirement bindings, the verifier's persisted evidence and
    obligations, and the commit/tree/run identities observed by that CI run.
    The function performs no semantic matching or requirement inference.
    """
    obligations_by_id: dict[str, Mapping[str, object]] = {}
    for obligation in obligations:
        obligation_id = obligation.get("obligation_id")
        if not isinstance(obligation_id, str) or not obligation_id:
            raise EvidenceError("verification obligation has no stable ID")
        if obligation_id in obligations_by_id:
            raise EvidenceError("verification obligation IDs must be unique")
        obligations_by_id[obligation_id] = obligation

    evidence_by_id: dict[str, Mapping[str, object]] = {}
    for evidence in evidence_records:
        obligation_id = evidence.get("obligation_id")
        if not isinstance(obligation_id, str) or not obligation_id:
            raise EvidenceError("verification evidence has no obligation ID")
        if obligation_id in evidence_by_id:
            raise EvidenceError("verification evidence obligation IDs must be unique")
        evidence_by_id[obligation_id] = evidence

    receipts: list[RequirementExecutionReceipt] = []
    bound_requirement_ids: set[str] = set()
    for obligation_id, bindings in sorted(requirement_bindings.items()):
        bound_obligation = obligations_by_id.get(obligation_id)
        bound_evidence = evidence_by_id.get(obligation_id)
        artifact_ref = evidence_artifact_refs.get(obligation_id)
        if bound_obligation is None or bound_evidence is None or artifact_ref is None:
            raise EvidenceError(
                f"requirement binding {obligation_id!r} lacks observed evidence, "
                "an obligation, or an evidence artifact reference"
            )
        if not bindings:
            raise EvidenceError(
                "requirement binding must include at least one requirement"
            )
        for binding in bindings:
            requirement_id = binding.get("requirement_id")
            requirement_digest = binding.get("requirement_digest")
            if not isinstance(requirement_id, str) or not requirement_id:
                raise EvidenceError("requirement binding has no stable requirement ID")
            if requirement_id in bound_requirement_ids:
                raise EvidenceError(
                    f"multiple verifier results supplied for {requirement_id}"
                )
            if not isinstance(requirement_digest, str) or not requirement_digest:
                raise EvidenceError(
                    "requirement binding has no typed requirement digest"
                )
            bound_requirement_ids.add(requirement_id)
            try:
                receipt = receipt_from_verification_evidence(
                    evidence=bound_evidence,
                    obligation=bound_obligation,
                    requirement_id=requirement_id,
                    requirement_digest=requirement_digest,
                    source_commit=pinned_commit,
                    candidate_commit=pinned_commit,
                    expected_candidate_tree=candidate_tree,
                    run_id=run_id,
                    evidence_artifact_ref=artifact_ref,
                )
            except (TypeError, ValueError) as error:
                raise EvidenceError(
                    f"could not bind verifier evidence to {requirement_id}: {error}"
                ) from error
            receipts.append(receipt)

    if not receipts:
        raise EvidenceError("no requirement execution receipts were produced")
    return write_requirement_execution_receipt_manifest(
        directory, pinned_commit=pinned_commit, receipts=receipts
    )


def produce_receipts_from_verification_run(
    directory: Path,
    *,
    repository_root: Path,
    verification_artifact_root: Path,
    pinned_commit: str,
    run_id: str,
    expected_candidate_tree: str,
) -> Path:
    """Build receipts from persisted verifier output and committed case bindings.

    Requirement linkage comes only from committed Structrr test-case records;
    observed verifier obligations are joined by exact selector equality.
    Requirements with missing, ambiguous, or non-passing selector evidence are
    omitted so planning can use semantic coverage instead.
    """
    from powdrr_service.verification.requirement_execution import (
        RequirementExecutionReceipt,
    )

    run_path = verification_artifact_root / "verification-run.json"
    try:
        run_data = json.loads(run_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise EvidenceError(
            f"could not read verification run index: {error}"
        ) from error
    if not isinstance(run_data, Mapping) or set(run_data) != {
        "schema_version",
        "candidate_tree",
        "obligations",
        "evidence_files",
    }:
        raise EvidenceError("verification run index fields are invalid")
    if run_data.get("schema_version") != VERIFICATION_RUN_SCHEMA:
        raise EvidenceError("unsupported verification run index schema")
    candidate_tree = run_data.get("candidate_tree")
    if not isinstance(candidate_tree, str) or not candidate_tree:
        raise EvidenceError("verification run index has no candidate tree")
    if candidate_tree != expected_candidate_tree:
        raise EvidenceError("verification run uses a different candidate tree")
    obligations = run_data.get("obligations")
    evidence_files = run_data.get("evidence_files")
    if not isinstance(obligations, list) or not all(
        isinstance(item, Mapping) for item in obligations
    ):
        raise EvidenceError("verification run obligations must be objects")
    if not isinstance(evidence_files, list) or not all(
        isinstance(item, Mapping) for item in evidence_files
    ):
        raise EvidenceError("verification run evidence files must be objects")

    obligations_by_id: dict[str, Mapping[str, object]] = {}
    for obligation in obligations:
        obligation_id = obligation.get("obligation_id")
        if not isinstance(obligation_id, str) or not obligation_id:
            raise EvidenceError("verification obligation has no stable ID")
        if obligation_id in obligations_by_id:
            raise EvidenceError("verification obligation IDs must be unique")
        obligations_by_id[obligation_id] = obligation
    evidence_by_id: dict[str, Mapping[str, object]] = {}
    refs_by_id: dict[str, str] = {}
    for entry in evidence_files:
        obligation_id, relative_path = entry.get("obligation_id"), entry.get("path")
        if (
            not isinstance(obligation_id, str)
            or obligation_id not in obligations_by_id
            or not isinstance(relative_path, str)
            or not relative_path
            or Path(relative_path).is_absolute()
            or ".." in Path(relative_path).parts
        ):
            raise EvidenceError("verification evidence file reference is invalid")
        expected_path = f"{obligation_id.removeprefix('sha256:')}/evidence.json"
        if relative_path != expected_path:
            raise EvidenceError(
                "verification evidence path does not match its obligation"
            )
        if obligation_id in evidence_by_id:
            raise EvidenceError("verification evidence obligation IDs must be unique")
        evidence_path = (verification_artifact_root / relative_path).resolve()
        try:
            evidence_path.relative_to(verification_artifact_root.resolve())
        except ValueError as error:
            raise EvidenceError(
                "verification evidence path escapes its artifact"
            ) from error
        try:
            evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise EvidenceError(
                f"could not read verification evidence: {error}"
            ) from error
        if not isinstance(evidence, Mapping):
            raise EvidenceError("verification evidence must be an object")
        if evidence.get("obligation_id") != obligation_id:
            raise EvidenceError("verification evidence does not match its index")
        if evidence.get("candidate_tree") != candidate_tree:
            raise EvidenceError("verification evidence has a different candidate tree")
        evidence_by_id[obligation_id] = evidence
        refs_by_id[obligation_id] = relative_path
    if set(evidence_by_id) != set(obligations_by_id):
        raise EvidenceError(
            "verification run does not contain evidence for every obligation"
        )

    requirement_cases: dict[str, dict[str, Any]] = {}
    tracked = subprocess.run(
        ["git", "ls-files", "-z", "--", "*structrr-diff.yaml"],
        cwd=repository_root,
        capture_output=True,
        check=True,
    ).stdout
    for raw_path in tracked.split(b"\0"):
        if not raw_path:
            continue
        plan_path = repository_root / raw_path.decode("utf-8")
        try:
            document = yaml.safe_load(plan_path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as error:
            raise EvidenceError(
                f"could not read committed test bindings: {error}"
            ) from error
        if not isinstance(document, Mapping):
            continue
        verification = document.get("verification")
        cases = (
            verification.get("required_test_cases")
            if isinstance(verification, Mapping)
            else None
        )
        if not isinstance(cases, list):
            continue
        for case in cases:
            if not isinstance(case, Mapping):
                raise EvidenceError("committed required test case must be an object")
            selectors = case.get("selectors")
            bindings = case.get("requirement_bindings")
            if bindings is None:
                continue
            if not isinstance(bindings, list):
                raise EvidenceError("committed requirement bindings must be a list")
            if (
                not isinstance(selectors, list)
                or not all(
                    isinstance(selector, str) and selector for selector in selectors
                )
                or not selectors
            ):
                raise EvidenceError("bound committed test case has invalid selectors")
            for binding in bindings:
                if not isinstance(binding, Mapping):
                    raise EvidenceError(
                        "committed requirement binding must be an object"
                    )
                requirement_id = binding.get("requirement_id")
                digest = binding.get("requirement_digest")
                if not isinstance(requirement_id, str) or not requirement_id:
                    raise EvidenceError(
                        "committed requirement binding has no stable ID"
                    )
                if not isinstance(digest, str) or not digest:
                    raise EvidenceError("committed requirement binding has no digest")
                aggregate = requirement_cases.setdefault(
                    requirement_id,
                    {"digest": digest, "selectors": set(), "bindings": []},
                )
                if aggregate["digest"] != digest:
                    raise EvidenceError(
                        "conflicting committed digests for requirement "
                        f"{requirement_id}"
                    )
                aggregate["selectors"].update(selectors)
                aggregate["bindings"].append(
                    {"selectors": sorted(set(selectors)), "case_id": case.get("id")}
                )

    receipts: list[RequirementExecutionReceipt] = []
    for requirement_id, aggregate in sorted(requirement_cases.items()):
        selectors = aggregate["selectors"]
        assert isinstance(selectors, set)
        matched: list[tuple[str, Mapping[str, object], Mapping[str, object]]] = []
        complete = True
        for selector in sorted(selectors):
            matches = [
                obligation_id
                for obligation_id, obligation in obligations_by_id.items()
                if obligation.get("selector") == selector
            ]
            if len(matches) != 1:
                complete = False
                break
            obligation_id = matches[0]
            evidence = evidence_by_id[obligation_id]
            if evidence.get("status") != "passed":
                complete = False
                break
            obligation = obligations_by_id[obligation_id]
            for field in (
                "contract_id",
                "provider",
                "selector",
                "profile",
                "contract_fingerprint",
                "verifier_fingerprint",
            ):
                if evidence.get(field) != obligation.get(field):
                    raise EvidenceError(f"verification evidence has mismatched {field}")
            matched.append((obligation_id, obligation, evidence))
        if not complete or not matched:
            continue
        rows: list[dict[str, Any]] = [
            {
                "obligation": dict(obligation),
                "evidence": dict(evidence),
                "evidence_path": refs_by_id[obligation_id],
            }
            for obligation_id, obligation, evidence in matched
        ]
        rows.sort(key=lambda item: str(item["obligation"].get("selector", "")))
        oracle_payload = {"schema": "powdrr-requirement-oracle-v1", "rows": rows}
        oracle_digest = (
            "sha256:" + hashlib.sha256(_canonical_json(oracle_payload)).hexdigest()
        )
        oracle_id = "oracle:" + oracle_digest.removeprefix("sha256:")
        command_payload = [row["evidence"].get("command_fingerprint") for row in rows]
        if any(
            not isinstance(item, str) or not item.startswith("sha256:")
            for item in command_payload
        ):
            raise EvidenceError(
                "verification evidence has an invalid command fingerprint"
            )
        command_digest = (
            "sha256:" + hashlib.sha256(_canonical_json(command_payload)).hexdigest()
        )
        artifact_refs = [
            f"artifact:run/{run_id}/verification-evidence/{refs_by_id[obligation_id]}"
            for obligation_id, _obligation, _evidence in matched
        ]
        receipts.append(
            RequirementExecutionReceipt.observed(
                requirement_id=requirement_id,
                requirement_digest=str(aggregate["digest"]),
                source_commit=pinned_commit,
                candidate_commit=pinned_commit,
                oracle_id=oracle_id,
                oracle_digest=oracle_digest,
                command_digest=command_digest,
                outcome="passed",
                run_id=f"github:run/{run_id}",
                artifact_refs=artifact_refs,
            )
        )
    return write_requirement_execution_receipt_manifest(
        directory, pinned_commit=pinned_commit, receipts=receipts
    )


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


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
    "produce_requirement_execution_receipt_manifest",
    "produce_receipts_from_verification_run",
    "write_requirement_execution_receipt_manifest",
]
