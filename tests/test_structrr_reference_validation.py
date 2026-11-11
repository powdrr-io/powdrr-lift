from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from powdrr_local.structrr_context import StructrrContextBridge
from powdrr_service.structrr.reference_catalog import (
    ReferenceCatalog,
    compile_reference_catalog,
)
from powdrr_service.structrr.reference_definitions import ReferenceDefinition
from powdrr_service.structrr.reference_validation import (
    ReferenceValidationReport,
    validate_source_references,
    validation_input_identity,
)
from powdrr_service.structrr.source_observation import SourceObservationSession
from powdrr_service.structrr.source_reader import SourceReader
from powdrr_service.structrr.source_reference_handoff import (
    CompiledSourceReferenceHandoff,
    compile_source_reference_handoff,
)

from powdrr_lift.workrr.coding_agent import (
    CodingAgentAttempt,
    CodingAgentStatus,
    ImplementationRequest,
)
from powdrr_lift.workrr.coding_agent_validation import (
    ValidationProfile,
    ValidationReportStatus,
    ValidationRunner,
)
from powdrr_lift.workrr.source_reference_validation import (
    SourceReferenceValidationContext,
)

SOURCE = b"# structrr: retry\ndef retry():\n    return 1\n"
BASE = "a" * 40


def inputs(
    *,
    base: str = BASE,
    owner: dict[str, Any] | None = None,
    cardinality: str = "exactly_one",
    rules: list[dict[str, Any]] | None = None,
    definitions: list[ReferenceDefinition] | None = None,
    relationship: str = "preserves",
) -> tuple[CompiledSourceReferenceHandoff, ReferenceCatalog]:
    doc = {
        "reference_definitions": [
            d.to_data()
            for d in (
                definitions
                or [
                    ReferenceDefinition(
                        "retry", "invariant", "Keep retry identity.", "spec:1"
                    )
                ]
            )
        ],
        "source_reference_placements": [
            {
                "obligation_id": "place:retry",
                "ref": "retry",
                "relationship": relationship,
                "requirement": "Keep retry identity.",
                "owner": owner
                or {"path": "retry.py", "kind": "function", "qualified_name": "retry"},
                "cardinality": cardinality,
                "verification_refs": ["case:1"],
            }
        ],
        "source_reference_preservations": rules or [],
    }
    catalog = compile_reference_catalog(doc, base_commit=base)
    return compile_source_reference_handoff(
        doc,
        catalog,
        objective="Preserve retry identity.",
        allowed_paths=(".",),
        private_validation_data={"oracle": "PRIVATE ORACLE"},
    ), catalog


def validate(
    candidate: bytes,
    *,
    baseline: bytes = SOURCE,
    handoff: CompiledSourceReferenceHandoff | None = None,
    catalog: ReferenceCatalog | None = None,
    **kwargs: Any,
) -> ReferenceValidationReport:
    if handoff is None or catalog is None:
        handoff, catalog = inputs()
    return validate_source_references(
        handoff,
        catalog,
        baseline={"retry.py": baseline},
        candidate={"retry.py": candidate},
        changed_paths=("retry.py",),
        recorded_inputs=validation_input_identity(handoff),
        **kwargs,
    )


def codes(report: ReferenceValidationReport) -> set[str]:
    return {f["code"] for f in report.to_data()["findings"]}


class Judge:
    def __init__(
        self, classification: str = "satisfied", *, forged: bool = False
    ) -> None:
        self.classification = classification
        self.forged = forged
        self.requests: list[dict[str, Any]] = []

    def complete_json(
        self, messages: list[dict[str, str]], *, response_schema: Any = None
    ) -> dict[str, Any]:
        packet = json.loads(messages[-1]["content"])
        self.requests.append(packet)
        return {
            "classification": self.classification,
            "explanation": "Reviewed actual responsibility.",
            "evidence_locations": ["forged:1"]
            if self.forged
            else packet["available_evidence"],
        }


def test_direct_placement_passes_without_claims_and_is_not_behavioral_evidence() -> (
    None
):
    report = validate(SOURCE.replace(b"return 1", b"return 2"))
    assert report.status == "passed"
    assert "missing_owner_claim" in codes(report)
    assert not report.to_data()["behavioral_evidence_satisfied"]
    assert report.to_data()["scan_complete"]
    assert (
        ReferenceValidationReport.from_data(report.to_data()).to_data()
        == report.to_data()
    )
    assert "PRIVATE ORACLE" not in json.dumps(report.to_data())
    changed = report.to_data()
    changed["status"] = "failed"
    with pytest.raises(ValueError):
        ReferenceValidationReport.from_data(changed)


@pytest.mark.parametrize(
    "source",
    [
        b"# structrr[module]: retry\ndef retry(): return 1\n",
        b"def retry(): return 1\n# structrr: retry\ndef unrelated(): pass\n",
        b"# structrr: retry\nclass Unrelated: pass\ndef retry(): pass\n",
    ],
)
def test_inherited_or_unrelated_id_stuffing_fails(source: bytes) -> None:
    report = validate(
        source,
        claims=(
            {
                "ref": "retry",
                "path": "retry.py",
                "kind": "function",
                "qualified_name": "retry",
            },
        ),
    )
    assert report.status == "failed"
    assert {"missing_required_placement", "wrong_owner"} <= codes(report)


@pytest.mark.parametrize(
    "source,code",
    [
        (SOURCE + b"# structrr: missing\ndef other(): pass\n", "unresolved_reference"),
        (b"# structrr: bad ID\ndef retry(): pass\n", "malformed_annotation"),
        (b"# structrr: retry\n", "dangling_annotation"),
        (b"# structrr: retry\ndef retry(:\n", "scope_resolution_failed"),
        (
            b"# structrr[module]: retry\n# structrr[module]: retry\n"
            b"def retry(): pass\n",
            "duplicate_annotation",
        ),
    ],
)
def test_invalid_candidate_annotations_fail(source: bytes, code: str) -> None:
    report = validate(source)
    assert report.status == "failed"
    assert code in codes(report)


def test_duplicate_owners_and_annotations_do_not_satisfy_cardinality() -> None:
    assert "ambiguous_reference" in codes(validate(SOURCE + SOURCE))
    handoff, catalog = inputs(cardinality="one_or_more")
    report = validate(
        b"# structrr: retry\n# structrr: retry\ndef retry(): pass\n",
        handoff=handoff,
        catalog=catalog,
    )
    assert report.status == "failed"
    assert "duplicate_annotation" in codes(report)


def test_inactive_reference_and_stale_recorded_inputs_fail() -> None:
    defs = [
        ReferenceDefinition("retry", "invariant", "Keep retry identity.", "spec:1"),
        ReferenceDefinition(
            "old", "decision", "Old meaning.", "spec:old", lifecycle="retired"
        ),
    ]
    handoff, catalog = inputs(definitions=defs)
    assert "inactive_reference" in codes(
        validate(
            SOURCE + b"# structrr: old\ndef old(): pass\n",
            handoff=handoff,
            catalog=catalog,
        )
    )
    recorded = validation_input_identity(handoff) | {"base_commit": "b" * 40}
    result = validate_source_references(
        handoff,
        catalog,
        baseline={"retry.py": SOURCE},
        candidate={"retry.py": SOURCE},
        changed_paths=(),
        recorded_inputs=recorded,
    )
    assert "stale_catalog" in codes(result)


def rule(disposition: str = "preserve", **changes: Any) -> dict[str, Any]:
    return {
        "id": "preserve:old",
        "ref": "retry",
        "owner": {"path": "old.py", "kind": "function", "qualified_name": "old"},
        "disposition": disposition,
        "reason": "Accepted source change.",
        "successor": None,
        "destination": None,
        **changes,
    }


def test_removal_needs_accepted_authorization_and_relocation_needs_review() -> None:
    old = b"# structrr: retry\ndef old(): return 1\n"
    for rules, expected in (
        ([], "failed"),
        ([rule("remove")], "passed"),
        ([rule()], "failed"),
    ):
        handoff, catalog = inputs(rules=rules)
        report = validate_source_references(
            handoff,
            catalog,
            baseline={"retry.py": SOURCE, "old.py": old},
            candidate={"retry.py": SOURCE, "old.py": None},
            changed_paths=("old.py",),
            recorded_inputs=validation_input_identity(handoff),
        )
        assert report.status == expected
    handoff, catalog = inputs(
        rules=[
            rule(
                "relocate",
                destination={
                    "path": "new.py",
                    "kind": "function",
                    "qualified_name": "new",
                },
            )
        ]
    )
    args: dict[str, Any] = {
        "baseline": {"retry.py": SOURCE, "old.py": old, "new.py": None},
        "candidate": {
            "retry.py": SOURCE,
            "old.py": None,
            "new.py": old.replace(b"def old", b"def new"),
        },
        "changed_paths": ("old.py", "new.py"),
        "recorded_inputs": validation_input_identity(handoff),
    }
    assert validate_source_references(handoff, catalog, **args).status == "needs_review"
    judge = Judge()
    assert (
        validate_source_references(handoff, catalog, review_client=judge, **args).status
        == "passed"
    )
    assert (
        judge.requests[0]["context"]["baseline_bindings"][0]["source"]
        == "def old(): return 1\n"
    )


def test_flexible_owners_and_suspicious_extra_placements_need_fresh_review() -> None:
    owner = {
        "binding": "requires_semantic_review",
        "kind": "function",
        "allowed_paths": ["."],
        "responsibility": "Own retry identity.",
    }
    handoff, catalog = inputs(owner=owner, cardinality="one_or_more")
    candidate = SOURCE + b"def caller(): return retry()\n"
    assert (
        validate(candidate, handoff=handoff, catalog=catalog).status == "needs_review"
    )
    judge = Judge()
    assert (
        validate(
            candidate, handoff=handoff, catalog=catalog, review_client=judge
        ).status
        == "passed"
    )
    assert judge.requests[0]["context"]["callers"]
    assert (
        judge.requests[0]["context"]["candidate_bindings"][0]["source"]
        == "def retry():\n    return 1\n"
    )
    assert (
        validate(
            candidate, handoff=handoff, catalog=catalog, review_client=Judge("violated")
        ).status
        == "failed"
    )
    assert (
        validate(
            candidate,
            handoff=handoff,
            catalog=catalog,
            review_client=Judge(forged=True),
        ).status
        == "needs_review"
    )
    assert (
        validate(SOURCE + b"# structrr: retry\ndef unrelated(): pass\n").status
        == "needs_review"
    )


def test_unchanged_legacy_findings_are_diagnostic_and_scan_gaps_block() -> None:
    handoff, catalog = inputs()
    legacy = SOURCE + b"# structrr: missing\ndef old(): pass\n"
    result = validate_source_references(
        handoff,
        catalog,
        baseline={"retry.py": legacy},
        candidate={"retry.py": legacy},
        changed_paths=(),
        recorded_inputs=validation_input_identity(handoff),
    )
    assert result.status == "passed"
    assert any(
        f["code"] == "unresolved_reference" and f["severity"] == "warning"
        for f in result.to_data()["findings"]
    )
    result = validate_source_references(
        handoff,
        catalog,
        baseline={},
        candidate={},
        changed_paths=("lost.py",),
        recorded_inputs=validation_input_identity(handoff),
    )
    assert result.status == "failed" and not result.to_data()["scan_complete"]


def test_test_ownership_requires_snapshot_bound_collection_evidence() -> None:
    handoff, catalog = inputs(
        owner={
            "path": "retry.py",
            "kind": "function",
            "qualified_name": "retry",
            "role": "test",
        },
        relationship="checks",
    )
    report = validate(SOURCE, handoff=handoff, catalog=catalog)
    assert report.status == "needs_review"
    evidence = {
        "snapshot_digest": report.to_data()["snapshot_digest"],
        "owners": [{"path": "retry.py", "kind": "function", "qualified_name": "retry"}],
    }
    assert (
        validate(
            SOURCE, handoff=handoff, catalog=catalog, test_collection_evidence=evidence
        ).status
        == "passed"
    )
    assert (
        validate(
            SOURCE.replace(b"return 1", b"return 2"),
            handoff=handoff,
            catalog=catalog,
            test_collection_evidence=evidence,
        ).status
        == "needs_review"
    )


def test_incidental_legacy_reference_does_not_force_unrelated_owner_backfill() -> None:
    legacy = SOURCE + b"# structrr: missing\ndef legacy(): return 9\n"
    assert (
        validate(legacy.replace(b"return 1", b"return 2"), baseline=legacy).status
        == "passed"
    )
    assert (
        validate(legacy.replace(b"return 9", b"return 8"), baseline=legacy).status
        == "failed"
    )


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()


def repository(
    tmp_path: Path,
) -> tuple[CompiledSourceReferenceHandoff, ReferenceCatalog]:
    git(tmp_path, "init")
    git(tmp_path, "config", "user.email", "test@example.com")
    git(tmp_path, "config", "user.name", "Test")
    (tmp_path / "retry.py").write_bytes(SOURCE)
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-m", "baseline")
    return inputs(base=git(tmp_path, "rev-parse", "HEAD"))


def context(
    handoff: CompiledSourceReferenceHandoff, catalog: ReferenceCatalog, **kwargs: Any
) -> SourceReferenceValidationContext:
    return SourceReferenceValidationContext(
        handoff, catalog, recorded_inputs=validation_input_identity(handoff), **kwargs
    )


def test_git_acquisition_includes_commits_index_untracked_deletes_and_renames(
    tmp_path: Path,
) -> None:
    handoff, catalog = repository(tmp_path)
    (tmp_path / "retry.py").write_bytes(SOURCE.replace(b"1", b"2"))
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-m", "worker commit")
    (tmp_path / "staged.py").write_text("value = 1\n")
    git(tmp_path, "add", "staged.py")
    (tmp_path / "untracked.py").write_text("value = 2\n")
    report = context(handoff, catalog).check(tmp_path)
    assert report.status == "passed"
    assert set(report.to_data()["changed_paths"]) == {
        "retry.py",
        "staged.py",
        "untracked.py",
    }
    git(tmp_path, "mv", "retry.py", "renamed.py")
    report = context(handoff, catalog).check(tmp_path)
    assert report.status == "failed"
    assert {"retry.py", "renamed.py"} <= set(report.to_data()["changed_paths"])
    assert "missing_preserved_reference" in codes(report)


def test_escaping_links_oversized_sources_and_concurrent_mutation_fail(
    tmp_path: Path,
) -> None:
    handoff, catalog = repository(tmp_path)
    (tmp_path / "escape.py").symlink_to(Path(__file__))
    assert "incomplete_context" in codes(context(handoff, catalog).check(tmp_path))
    (tmp_path / "escape.py").unlink()
    (tmp_path / "big.py").write_bytes(b"#" * (1024 * 1024 + 1))
    assert context(handoff, catalog).check(tmp_path).status == "failed"
    (tmp_path / "big.py").unlink()
    semantic, semantic_catalog = inputs(
        base=catalog.base_commit,
        owner={
            "binding": "requires_semantic_review",
            "allowed_paths": ["."],
            "kind": "function",
            "responsibility": "Retry identity.",
        },
        cardinality="one_or_more",
    )

    class MutatingJudge(Judge):
        def complete_json(
            self, messages: list[dict[str, str]], *, response_schema: Any = None
        ) -> dict[str, Any]:
            (tmp_path / "retry.py").write_bytes(SOURCE.replace(b"1", b"2"))
            return super().complete_json(messages, response_schema=response_schema)

    report = context(semantic, semantic_catalog, review_client=MutatingJudge()).check(
        tmp_path
    )
    assert report.status == "failed"
    assert any("changed during" in f["observed"] for f in report.to_data()["findings"])


def test_safe_git_acquisition_does_not_run_external_diff_or_fsmonitor(
    tmp_path: Path,
) -> None:
    handoff, catalog = repository(tmp_path)
    hook = tmp_path / "hook"
    hook.write_text("#!/bin/sh\ntouch SHOULD_NOT_EXIST\n")
    hook.chmod(0o755)
    git(tmp_path, "config", "diff.external", str(hook))
    git(tmp_path, "config", "core.fsmonitor", str(hook))
    (tmp_path / "retry.py").write_bytes(SOURCE.replace(b"1", b"2"))
    assert context(handoff, catalog).check(tmp_path).status == "passed"
    assert not (tmp_path / "SHOULD_NOT_EXIST").exists()


def test_reference_report_is_separate_from_behavior_and_missing_context_blocks(
    tmp_path: Path,
) -> None:
    handoff, catalog = repository(tmp_path)
    request = ImplementationRequest.from_source_reference_handoff(
        handoff, request_id="r", validation_profiles=("behavior",)
    )
    request = replace(request, allowed_commands=(f"{sys.executable} *",))
    attempt = CodingAgentAttempt("a", "r", "fake", CodingAgentStatus.COMPLETED, 0)
    profiles = {
        "behavior": ValidationProfile(
            "behavior", (sys.executable, "-c", "raise SystemExit(1)")
        )
    }
    result = ValidationRunner(
        profiles, reference_context=context(handoff, catalog)
    ).run(request, attempt, worktree_root=tmp_path)
    assert result.status == ValidationReportStatus.FAILED
    assert result.results[0].profile == "structrr-references"
    assert result.results[0].status.value == "passed"
    assert result.results[1].status.value == "failed"
    assert (
        ValidationRunner(profiles).run(request, attempt, worktree_root=tmp_path).status
        == ValidationReportStatus.BLOCKED
    )
    no_behavior = replace(request, validation_profiles=())
    assert (
        ValidationRunner({}, reference_context=context(handoff, catalog))
        .run(no_behavior, attempt, worktree_root=tmp_path)
        .status
        == ValidationReportStatus.BLOCKED
    )
    (tmp_path / "retry.py").write_text("def retry(): return 1\n")
    passing_behavior = {
        "behavior": ValidationProfile("behavior", (sys.executable, "-c", "pass"))
    }
    result = ValidationRunner(
        passing_behavior, reference_context=context(handoff, catalog)
    ).run(request, attempt, worktree_root=tmp_path)
    assert result.status == ValidationReportStatus.FAILED
    assert result.results[0].status.value == "failed"
    assert result.results[1].status.value == "passed"


def test_local_check_cli_and_isolated_proxy_use_only_trusted_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from powdrr_lift.structrr.source_reader import main

    handoff, catalog = repository(tmp_path)
    for name, value in (
        ("HANDOFF", handoff.to_data()),
        ("CATALOG", catalog.to_data()),
        ("VALIDATION_INPUTS", validation_input_identity(handoff)),
    ):
        path = tmp_path / f"{name}.json"
        path.write_text(json.dumps(value))
        monkeypatch.setenv("POWDRR_STRUCTRR_" + name, str(path))
    monkeypatch.setenv("POWDRR_STRUCTRR_WORKSPACE", str(tmp_path))
    assert main(["check"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "passed"
    trusted = context(handoff, catalog)
    with StructrrContextBridge(
        SourceObservationSession(SourceReader(tmp_path, catalog)),
        tmp_path / "events",
        "enforce",
        reference_check=lambda: trusted.check(tmp_path),
    ) as bridge:
        result = subprocess.run(
            [sys.executable, "-m", "powdrr_agent.execution.source_reader", "check"],
            env=os.environ | {"POWDRR_STRUCTRR_ENDPOINT": bridge.endpoint},
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert (
            json.loads(result.stdout)["schema_version"]
            == "structrr-reference-validation-v1"
        )
        assert "PRIVATE ORACLE" not in result.stdout


def test_explicit_supersession_keeps_canonical_successor_without_redirecting() -> None:
    definitions = [
        ReferenceDefinition("retry", "invariant", "Keep retry identity.", "spec:1"),
        ReferenceDefinition(
            "old",
            "invariant",
            "Previous meaning.",
            "spec:old",
            lifecycle="superseded",
            successor="retry",
        ),
    ]
    accepted = rule(
        "supersede",
        ref="old",
        owner={"path": "retry.py", "kind": "function", "qualified_name": "retry"},
        successor="retry",
    )
    handoff, catalog = inputs(rules=[accepted], definitions=definitions)
    baseline = SOURCE.replace(b"structrr: retry", b"structrr: old")
    assert (
        validate(SOURCE, baseline=baseline, handoff=handoff, catalog=catalog).status
        == "passed"
    )
    assert (
        validate(baseline, baseline=baseline, handoff=handoff, catalog=catalog).status
        == "failed"
    )


def test_ambiguous_baseline_owner_cannot_silently_lose_one_binding() -> None:
    report = validate(SOURCE, baseline=SOURCE + SOURCE)
    assert report.status == "failed"
    assert "ambiguous_reference" in codes(report)


def test_review_errors_partial_evidence_and_review_budget_never_pass() -> None:
    class PartialJudge(Judge):
        def complete_json(
            self, messages: list[dict[str, str]], *, response_schema: Any = None
        ) -> dict[str, Any]:
            result = super().complete_json(messages, response_schema=response_schema)
            result["evidence_locations"] = result["evidence_locations"][:1]
            return result

    owner = {
        "binding": "requires_semantic_review",
        "kind": "function",
        "allowed_paths": ["."],
        "responsibility": "Own retry identity.",
    }
    handoff, catalog = inputs(owner=owner, cardinality="one_or_more")
    assert (
        validate(
            SOURCE + b"# structrr: retry\ndef other(): return 2\n",
            handoff=handoff,
            catalog=catalog,
            review_client=PartialJudge(),
        ).status
        == "needs_review"
    )

    class UnavailableJudge(Judge):
        def complete_json(
            self, messages: list[dict[str, str]], *, response_schema: Any = None
        ) -> dict[str, Any]:
            raise RuntimeError("Review unavailable")

    assert (
        validate(
            SOURCE, handoff=handoff, catalog=catalog, review_client=UnavailableJudge()
        ).status
        == "needs_review"
    )
    many = SOURCE + b"".join(
        f"# structrr: retry\ndef helper_{i}(): return {i}\n".encode() for i in range(51)
    )
    judge = Judge()
    report = validate(many, review_client=judge)
    assert len(judge.requests) == 50
    assert report.status == "needs_review"
    assert any("50-review" in f["observed"] for f in report.to_data()["findings"])
    oversized = SOURCE.replace(b"return 1", b"# " + b"x" * 50000 + b"\n    return 1")
    bounded_judge = Judge()
    assert (
        validate(
            oversized, handoff=handoff, catalog=catalog, review_client=bounded_judge
        ).status
        == "needs_review"
    )
    assert not bounded_judge.requests
