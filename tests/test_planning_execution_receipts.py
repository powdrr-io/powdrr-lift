from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from powdrr_service.decision_obligation import content_fingerprint
from powdrr_service.planning.evidence import EvidenceError
from powdrr_service.verification.requirement_execution import (
    RequirementExecutionReceipt,
    requirement_coverage_digest,
)

from powdrr_lift.cli import build_parser
from powdrr_lift.workrr.planning.execution_receipts import (
    MANIFEST_SCHEMA,
    RECEIPT_ARTIFACT_NAME,
    discover_requirement_execution_receipts,
    load_requirement_execution_receipts,
    produce_requirement_execution_receipt_manifest,
    write_requirement_execution_receipt_manifest,
)


def _manifest_data(commit: str = "a" * 40) -> dict[str, Any]:
    requirement_digest = requirement_coverage_digest(
        requirement_id="req-0001",
        statement="Implement expected behavior.",
        kind="functional",
        source_refs=("anchor-0001",),
        source_group_ids=(),
        source_validation_groups=(),
        source_logic=(),
    )
    receipt = RequirementExecutionReceipt.observed(
        requirement_id="req-0001",
        requirement_digest=requirement_digest,
        source_commit=commit,
        candidate_commit=commit,
        oracle_id="oracle:sample",
        oracle_digest="sha256:" + "b" * 64,
        command_digest="sha256:" + "c" * 64,
        outcome="passed",
        run_id="github:run/456",
        artifact_refs=("artifact:run/456/result.json",),
    ).to_data()
    excerpt = (
        json.dumps(receipt, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        + "\n"
    )
    evidence = {
        "id": "evidence-0001",
        "base_commit": commit,
        "path": "artifacts/requirement-receipt.json",
        "start_line": 1,
        "end_line": 1,
        "content_sha256": hashlib.sha256(excerpt.encode()).hexdigest(),
        "kind": "execution_receipt",
        "symbol": None,
    }
    return {
        "schema_version": MANIFEST_SCHEMA,
        "base_commit": commit,
        "receipts": [{"evidence": evidence, "receipt": receipt}],
    }


def test_receipt_manifest_loads_only_at_its_pinned_commit(tmp_path: Path) -> None:
    path = tmp_path / "receipts.json"
    path.write_text(json.dumps(_manifest_data()), encoding="utf-8")

    evidence_by_requirement, excerpts = load_requirement_execution_receipts(
        path, pinned_commit="a" * 40
    )

    evidence = evidence_by_requirement["req-0001"][0]
    assert evidence.kind == "execution_receipt"
    assert evidence.id in excerpts
    assert json.loads(excerpts[evidence.id])["requirement_id"] == "req-0001"

    with pytest.raises(EvidenceError, match="different base commit"):
        load_requirement_execution_receipts(path, pinned_commit="d" * 40)


def test_receipt_manifest_rejects_excerpt_digest_mismatch(tmp_path: Path) -> None:
    manifest = _manifest_data()
    receipt_record = manifest["receipts"][0]
    assert isinstance(receipt_record, dict)
    evidence = receipt_record["evidence"]
    assert isinstance(evidence, dict)
    evidence["content_sha256"] = "e" * 64
    path = tmp_path / "receipts.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(EvidenceError, match="digest does not match"):
        load_requirement_execution_receipts(path, pinned_commit="a" * 40)


def test_receipt_manifest_producer_writes_named_artifact_and_loads_it(
    tmp_path: Path,
) -> None:
    source = _manifest_data()
    source_record = source["receipts"][0]
    assert isinstance(source_record, dict)
    receipt = RequirementExecutionReceipt.from_data(source_record["receipt"])

    manifest_path = write_requirement_execution_receipt_manifest(
        tmp_path, pinned_commit="a" * 40, receipts=(receipt,)
    )

    assert manifest_path.parts[-2:] == (RECEIPT_ARTIFACT_NAME, "manifest.json")
    evidence_by_requirement, excerpts = load_requirement_execution_receipts(
        manifest_path, pinned_commit="a" * 40
    )
    assert set(evidence_by_requirement) == {"req-0001"}
    evidence = evidence_by_requirement["req-0001"][0]
    assert json.loads(excerpts[evidence.id])["outcome"] == "passed"
    receipt_file = (
        tmp_path
        / RECEIPT_ARTIFACT_NAME
        / "receipts"
        / (receipt.to_data()["receipt_id"].removeprefix("receipt:") + ".json")
    )
    assert json.loads(receipt_file.read_text(encoding="utf-8")) == receipt.to_data()


def test_receipt_manifest_producer_rejects_unpassed_receipts(tmp_path: Path) -> None:
    requirement_digest = requirement_coverage_digest(
        requirement_id="req-0001",
        statement="Implement expected behavior.",
        kind="functional",
        source_refs=("anchor-0001",),
        source_group_ids=(),
        source_validation_groups=(),
        source_logic=(),
    )
    receipt = RequirementExecutionReceipt.observed(
        requirement_id="req-0001",
        requirement_digest=requirement_digest,
        source_commit="a" * 40,
        candidate_commit="a" * 40,
        oracle_id="oracle:sample",
        oracle_digest="sha256:" + "b" * 64,
        command_digest="sha256:" + "c" * 64,
        outcome="failed",
        run_id="github:run/456",
        artifact_refs=("artifact:run/456/result.json",),
    )

    with pytest.raises(EvidenceError, match="only passed"):
        write_requirement_execution_receipt_manifest(
            tmp_path, pinned_commit="a" * 40, receipts=(receipt,)
        )


def test_verification_run_producer_requires_explicit_requirement_binding(
    tmp_path: Path,
) -> None:
    commit = "a" * 40
    obligation = {
        "obligation_id": "obligation:one",
        "contract_id": "contract:one",
        "provider": "pytest",
        "selector": "tests/test_example.py::test_behavior",
        "profile": "python-tests",
        "contract_fingerprint": "sha256:" + "b" * 64,
        "verifier_fingerprint": "sha256:" + "c" * 64,
    }
    evidence = {
        "obligation_id": obligation["obligation_id"],
        "contract_id": obligation["contract_id"],
        "provider": obligation["provider"],
        "selector": obligation["selector"],
        "profile": obligation["profile"],
        "contract_fingerprint": obligation["contract_fingerprint"],
        "verifier_fingerprint": obligation["verifier_fingerprint"],
        "candidate_tree": "tree:observed",
        "command_fingerprint": "sha256:" + "d" * 64,
        "status": "passed",
        "artifact_refs": ["artifact:run/456/test-output.txt"],
    }
    requirement_digest = requirement_coverage_digest(
        requirement_id="req-0001",
        statement="The behavior is observable.",
        kind="functional",
        source_refs=("anchor-0001",),
        source_group_ids=(),
        source_validation_groups=(),
        source_logic=(),
    )

    manifest_path = produce_requirement_execution_receipt_manifest(
        tmp_path,
        pinned_commit=commit,
        candidate_tree="tree:observed",
        run_id="github:run/456",
        evidence_records=(evidence,),
        obligations=(obligation,),
        requirement_bindings={
            obligation["obligation_id"]: (
                {
                    "requirement_id": "req-0001",
                    "requirement_digest": requirement_digest,
                },
            )
        },
        evidence_artifact_refs={
            obligation["obligation_id"]: "artifact:run/456/evidence.json"
        },
    )

    evidence_by_requirement, excerpts = load_requirement_execution_receipts(
        manifest_path, pinned_commit=commit
    )
    receipt_evidence = evidence_by_requirement["req-0001"][0]
    receipt = json.loads(excerpts[receipt_evidence.id])
    assert receipt["candidate_commit"] == commit
    assert receipt["requirement_digest"] == requirement_digest
    assert receipt["oracle_id"].startswith("oracle:")


def test_verification_run_producer_rejects_candidate_tree_mismatch(
    tmp_path: Path,
) -> None:
    obligation = {
        "obligation_id": "obligation:one",
        "contract_id": "contract:one",
        "provider": "pytest",
        "selector": "tests/test_example.py::test_behavior",
        "profile": "python-tests",
        "contract_fingerprint": "sha256:" + "b" * 64,
        "verifier_fingerprint": "sha256:" + "c" * 64,
    }
    evidence = {
        "obligation_id": obligation["obligation_id"],
        "contract_id": obligation["contract_id"],
        "provider": obligation["provider"],
        "selector": obligation["selector"],
        "profile": obligation["profile"],
        "contract_fingerprint": obligation["contract_fingerprint"],
        "verifier_fingerprint": obligation["verifier_fingerprint"],
        "candidate_tree": "tree:other",
        "command_fingerprint": "sha256:" + "d" * 64,
        "status": "passed",
        "artifact_refs": [],
    }

    with pytest.raises(EvidenceError, match="different candidate tree"):
        produce_requirement_execution_receipt_manifest(
            tmp_path,
            pinned_commit="a" * 40,
            candidate_tree="tree:expected",
            run_id="github:run/456",
            evidence_records=(evidence,),
            obligations=(obligation,),
            requirement_bindings={
                obligation["obligation_id"]: (
                    {
                        "requirement_id": "req-0001",
                        "requirement_digest": "sha256:" + "e" * 64,
                    },
                )
            },
            evidence_artifact_refs={
                obligation["obligation_id"]: "artifact:run/456/evidence.json"
            },
        )


def test_publish_execution_receipts_cli_builds_from_committed_bindings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Powdrr",
            "-c",
            "user.email=powdrr@example.com",
            "commit",
            "--allow-empty",
            "-qm",
            "fixture",
        ],
        cwd=repo,
        check=True,
    )
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    monkeypatch.setattr("powdrr_lift.cli.resolve_repo_root", lambda _path: repo)
    monkeypatch.setattr("powdrr_lift.cli.subprocess.run", subprocess.run)
    obligation = {
        "obligation_id": "obligation:one",
        "contract_id": "contract:one",
        "provider": "pytest",
        "selector": "tests/test_example.py::test_behavior",
        "profile": "python-tests",
        "contract_fingerprint": "sha256:" + "b" * 64,
        "verifier_fingerprint": "sha256:" + "c" * 64,
    }
    evidence = {
        "obligation_id": obligation["obligation_id"],
        "contract_id": obligation["contract_id"],
        "provider": obligation["provider"],
        "selector": obligation["selector"],
        "profile": obligation["profile"],
        "contract_fingerprint": obligation["contract_fingerprint"],
        "verifier_fingerprint": obligation["verifier_fingerprint"],
        "candidate_tree": "",
        "command_fingerprint": "sha256:" + "d" * 64,
        "status": "passed",
        "artifact_refs": ["artifact:run/456/test-output.txt"],
    }
    binding = {
        "requirement_id": "req-0001",
        "requirement_digest": "sha256:" + "e" * 64,
    }
    plan_path = repo / "docs" / "structrr" / "diffs" / "demo" / "structrr-diff.yaml"
    plan_path.parent.mkdir(parents=True)
    plan_path.write_text(
        "verification:\n  required_test_cases:\n"
        "    - id: AC-01\n      selectors:\n"
        "        - tests/test_example.py::test_behavior\n"
        "      requirement_bindings:\n"
        "        - requirement_id: req-0001\n"
        f"          requirement_digest: {binding['requirement_digest']}\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "--", str(plan_path)], cwd=repo, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Powdrr",
            "-c",
            "user.email=powdrr@example.com",
            "commit",
            "-qm",
            "add bindings",
        ],
        cwd=repo,
        check=True,
    )
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    candidate_tree = content_fingerprint(
        {
            "head": head,
            "diff": subprocess.run(
                ["git", "diff", "--binary"],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
            ).stdout,
            "status": subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
            ).stdout,
        }
    )
    evidence["candidate_tree"] = candidate_tree
    evidence_root = tmp_path / "verification-evidence"
    evidence_dir = evidence_root / "obligation:one"
    evidence_dir.mkdir(parents=True)
    evidence_path = evidence_dir / "evidence.json"
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
    (evidence_root / "verification-run.json").write_text(
        json.dumps(
            {
                "schema_version": "verification-evidence-run-v1",
                "candidate_tree": candidate_tree,
                "obligations": [obligation],
                "evidence_files": [
                    {
                        "obligation_id": obligation["obligation_id"],
                        "path": "obligation:one/evidence.json",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    output_dir = tmp_path / "output"

    args = build_parser().parse_args(
        [
            "publish-execution-receipts",
            "--verification-artifact-dir",
            str(evidence_root),
            "--output",
            str(output_dir),
            "--pinned-commit",
            head,
            "--run-id",
            "456",
            "--repo-root",
            str(repo),
        ]
    )

    assert args.func(args) == 0
    manifest_path = output_dir / RECEIPT_ARTIFACT_NAME / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["base_commit"] == head
    assert manifest["receipts"][0]["receipt"]["run_id"] == "github:run/456"


def test_discovery_downloads_only_successful_artifacts_for_exact_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    commit = "a" * 40
    calls: list[list[str]] = []

    def run(command: list[str], **_kwargs: Any) -> Any:
        calls.append(command)
        if command[1:3] == ["run", "list"]:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    [
                        {
                            "databaseId": 1,
                            "headSha": "f" * 40,
                            "status": "completed",
                            "conclusion": "success",
                        },
                        {
                            "databaseId": 2,
                            "headSha": commit,
                            "status": "completed",
                            "conclusion": "failure",
                        },
                        {
                            "databaseId": 3,
                            "headSha": commit,
                            "status": "completed",
                            "conclusion": "success",
                        },
                    ]
                ),
                stderr="",
            )
        destination = Path(command[command.index("--dir") + 1])
        destination.mkdir(parents=True, exist_ok=True)
        (destination / "receipts.json").write_text(
            json.dumps(_manifest_data(commit)), encoding="utf-8"
        )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(
        "powdrr_lift.workrr.planning.execution_receipts.subprocess.run", run
    )
    evidence_by_requirement, _excerpts = discover_requirement_execution_receipts(
        "owner/repo", pinned_commit=commit
    )

    assert list(evidence_by_requirement) == ["req-0001"]
    assert calls[0][calls[0].index("--commit") + 1] == commit
    assert calls[1][3] == "3"
    assert calls[1][calls[1].index("--name") + 1] == RECEIPT_ARTIFACT_NAME


def test_discovery_returns_no_evidence_when_runs_have_no_receipt_artifact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commit = "a" * 40

    def run(command: list[str], **_kwargs: Any) -> Any:
        if command[1:3] == ["run", "list"]:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    [
                        {
                            "databaseId": 4,
                            "headSha": commit,
                            "status": "completed",
                            "conclusion": "success",
                        }
                    ]
                ),
                stderr="",
            )
        return SimpleNamespace(
            returncode=1, stdout="", stderr="no artifacts found for run"
        )

    monkeypatch.setattr(
        "powdrr_lift.workrr.planning.execution_receipts.subprocess.run", run
    )

    assert discover_requirement_execution_receipts(
        "owner/repo", pinned_commit=commit
    ) == ({}, {})
