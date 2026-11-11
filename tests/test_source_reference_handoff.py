from __future__ import annotations

import copy
import json
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from powdrr_service.structrr.design import DesignHandoff, DesiredState
from powdrr_service.structrr.proposal import compile_proposal_revision
from powdrr_service.structrr.reference_catalog import compile_reference_catalog
from powdrr_service.structrr.reference_definitions import ReferenceDefinition
from powdrr_service.structrr.source_reference_handoff import (
    SECTIONS,
    WORKER_INSTRUCTION,
    CompiledSourceReferenceHandoff,
    SourceReferencePrompt,
    compile_source_reference_handoff,
)

from powdrr_lift.workrr.coding_agent import (
    CodingAgentStatus,
    ImplementationRequest,
    run_coding_agent,
)


def placement(**changes: Any) -> dict[str, Any]:
    return {
        "obligation_id": "placement:retry",
        "ref": "Opaque:retry",
        "relationship": "preserves",
        "requirement": "Keep the original key.",
        "owner": {
            "path": "payments/retry.py",
            "kind": "function",
            "qualified_name": "retry",
        },
        "cardinality": "exactly_one",
        "verification_refs": ["case:retry"],
        **changes,
    }


def document(
    *placements: dict[str, Any],
    definitions: list[ReferenceDefinition] | None = None,
    preservation: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "reference_definitions": [
            d.to_data()
            for d in (
                definitions
                or [
                    ReferenceDefinition(
                        "Opaque:retry", "invariant", "Keep the original key.", "spec:1"
                    )
                ]
            )
        ],
        "source_reference_placements": list(placements or [placement()]),
        "source_reference_preservations": preservation or [],
    }


def compile_document(
    doc: dict[str, Any], **kwargs: Any
) -> CompiledSourceReferenceHandoff:
    catalog = compile_reference_catalog(doc, base_commit="a" * 40)
    return compile_source_reference_handoff(
        doc,
        catalog,
        objective="Implement retry safety.",
        allowed_paths=("payments", "tests"),
        **kwargs,
    )


def test_atomic_prompt_manifest_parity_privacy_and_stability() -> None:
    private = {"oracle": "SECRET_PRIVATE_ORACLE", "path": "/private/manifest.json"}
    original = document()
    handoff = compile_document(original, private_validation_data=private)
    assert (
        compile_document(original, private_validation_data=private).to_data()
        == handoff.to_data()
    )
    assert (
        CompiledSourceReferenceHandoff.from_data(
            json.loads(json.dumps(handoff.to_data()))
        ).to_data()
        == handoff.to_data()
    )
    worker = handoff.worker_prompt.to_data()
    manifest = handoff.validation_manifest
    assert manifest["contract"] == worker["contract"]
    assert manifest["contract_fingerprint"] == worker["contract_fingerprint"]
    assert manifest["prompt_fingerprint"] == worker["fingerprint"]
    assert len(manifest["contract"]["obligations"]) == 1
    assert [
        line[3:] for line in worker["prompt"].splitlines() if line.startswith("## ")
    ] == list(SECTIONS)
    assert WORKER_INSTRUCTION in worker["prompt"]
    assert "Keep the original key." in worker["prompt"]
    assert worker["prompt"].count("Keep the original key.") == 1
    assert worker["source_reference_mode"] == "enforce"
    request = ImplementationRequest.from_source_reference_handoff(
        handoff, request_id="request:1"
    )
    assert "SECRET_PRIVATE_ORACLE" not in request.to_json()
    assert "/private/manifest.json" not in request.to_json()
    assert "validation_manifest" not in request.to_json()
    revised_private = compile_document(
        original, private_validation_data={"oracle": "different private inputs"}
    )
    assert revised_private.worker_prompt.prompt == handoff.worker_prompt.prompt
    assert (
        revised_private.worker_prompt.to_data()["fingerprint"] != worker["fingerprint"]
    )
    with pytest.raises(ValueError, match="private validation manifest"):
        handoff.require_matches(revised_private.worker_prompt)
    assert ImplementationRequest.from_data(request.to_data()) == request
    private["oracle"] = "changed"
    original["source_reference_placements"][0]["requirement"] = "changed"
    assert (
        handoff.validation_manifest["private_validation_data"]["oracle"]
        == "SECRET_PRIVATE_ORACLE"
    )


@pytest.mark.parametrize(
    "kind,relationship",
    [
        ("feature", "implements"),
        ("decision", "realizes"),
        ("invariant", "preserves"),
        ("assumption", "relies_on"),
    ],
)
def test_relationships_follow_canonical_kind_not_id_prefix(
    kind: str, relationship: str
) -> None:
    definition = ReferenceDefinition(
        "Opaque:retry", kind, "Keep the original key.", "spec:1"
    )
    handoff = compile_document(
        document(placement(relationship=relationship), definitions=[definition])
    )
    assert handoff.worker_prompt.to_data()["contract"]["definitions"][0]["kind"] == kind


def test_checks_requires_explicit_test_ownership() -> None:
    owner = {
        "path": "tests/test_retry.py",
        "kind": "function",
        "qualified_name": "test_retry",
        "role": "test",
    }
    handoff = compile_document(document(placement(relationship="checks", owner=owner)))
    assert (
        handoff.validation_manifest["contract"]["obligations"][0]["owner"]["role"]
        == "test"
    )
    with pytest.raises(ValueError, match="test ownership"):
        compile_document(document(placement(relationship="checks")))


@pytest.mark.parametrize(
    "changes",
    [
        {"ref": "missing"},
        {"ref": "opaque:retry"},
        {"relationship": "implements"},
        {"requirement": "A paraphrase"},
        {"cardinality": "anywhere"},
        {"verification_refs": []},
        {"obligation_id": "invalid id"},
    ],
)
def test_invalid_or_unresolved_placements_fail_compilation(
    changes: dict[str, Any],
) -> None:
    with pytest.raises(ValueError):
        compile_document(document(placement(**changes)))


@pytest.mark.parametrize(
    "path",
    [
        "/payments/a.py",
        "../a.py",
        "C:/a.py",
        "payments\\a.py",
        "elsewhere/a.py",
        "payments/*.py",
        "payments/a.txt",
    ],
)
def test_owner_paths_are_safe_literal_and_within_scope(path: str) -> None:
    item = placement()
    item["owner"]["path"] = path
    with pytest.raises(ValueError):
        compile_document(document(item))


def test_semantic_owner_binding_remains_an_independent_review_obligation() -> None:
    owner = {
        "binding": "requires_semantic_review",
        "kind": "function",
        "allowed_paths": ["payments"],
        "responsibility": "Own retry scheduling.",
    }
    item = placement(owner=owner, cardinality="one_or_more")
    handoff = compile_document(document(item))
    bound = handoff.validation_manifest["contract"]["obligations"][0]
    assert bound["owner"]["binding"] == "requires_semantic_review"
    assert "independent review" in handoff.worker_prompt.prompt
    assert "passing binding" in handoff.worker_prompt.prompt
    assert "qualified_name" not in bound["owner"]
    with pytest.raises(ValueError, match="one_or_more"):
        compile_document(document(placement(owner=owner)))


def test_multiple_owners_keep_distinct_obligations_and_duplicate_ids_fail() -> None:
    second = placement(obligation_id="placement:helper")
    second["owner"]["qualified_name"] = "helper"
    handoff = compile_document(document(placement(), second))
    assert len(handoff.validation_manifest["contract"]["obligations"]) == 2
    with pytest.raises(ValueError, match="duplicate"):
        compile_document(document(placement(), placement()))


def preservation(**changes: Any) -> dict[str, Any]:
    return {
        "id": "preserve:old",
        "ref": "Opaque:retry",
        "owner": {
            "path": "payments/old.py",
            "kind": "function",
            "qualified_name": "old",
        },
        "disposition": "preserve",
        "reason": "Keep baseline behavior.",
        "successor": None,
        "destination": None,
        **changes,
    }


def test_preservation_relocation_removal_and_canonical_supersession() -> None:
    rules = [
        preservation(),
        preservation(
            id="move:1",
            owner={
                "path": "payments/move.py",
                "kind": "function",
                "qualified_name": "move",
            },
            disposition="relocate",
            destination=placement()["owner"],
        ),
    ]
    handoff = compile_document(document(preservation=rules))
    assert len(handoff.validation_manifest["contract"]["preservation"]) == 2
    assert "baseline_reference_rules" in handoff.worker_prompt.prompt
    removal = preservation(disposition="remove", reason="Accepted retirement.")
    assert compile_document(document(preservation=[removal]))
    old = ReferenceDefinition(
        "old",
        "invariant",
        "Old wording.",
        "spec:old",
        lifecycle="superseded",
        successor="Opaque:retry",
    )
    new = ReferenceDefinition(
        "Opaque:retry", "invariant", "Keep the original key.", "spec:1"
    )
    rule = preservation(ref="old", disposition="supersede", successor="Opaque:retry")
    assert compile_document(document(definitions=[old, new], preservation=[rule]))
    with pytest.raises(ValueError, match="conflicting"):
        compile_document(
            document(
                preservation=[
                    preservation(),
                    preservation(id="other", disposition="remove"),
                ]
            )
        )
    with pytest.raises(ValueError, match="conflicting"):
        compile_document(
            document(
                preservation=[
                    preservation(),
                    preservation(
                        id="same-physical-owner",
                        disposition="remove",
                        owner={**preservation()["owner"], "role": "test"},
                    ),
                ]
            )
        )
    with pytest.raises(ValueError, match="required reference"):
        compile_document(
            document(
                preservation=[
                    preservation(owner=placement()["owner"], disposition="remove")
                ]
            )
        )


def test_frozen_catalog_and_inactive_reference_guards() -> None:
    doc = document()
    catalog = compile_reference_catalog(doc, base_commit="a" * 40)
    doc["source_reference_placements"][0]["requirement"] = "Changed after freeze."
    with pytest.raises(ValueError, match="do not match"):
        compile_source_reference_handoff(
            doc, catalog, objective="Do it", allowed_paths=("payments",)
        )
    inactive = ReferenceDefinition(
        "Opaque:retry",
        "invariant",
        "Keep the original key.",
        "spec:1",
        lifecycle="retired",
    )
    with pytest.raises(ValueError, match="active"):
        compile_document(document(definitions=[inactive]))


def test_tampering_cross_artifact_mismatch_and_prompt_bounds() -> None:
    handoff = compile_document(document())
    data = handoff.to_data()
    data["validation_manifest"]["contract"]["obligations"][0]["owner"][
        "qualified_name"
    ] = "unrelated"
    with pytest.raises(ValueError, match="mismatch"):
        CompiledSourceReferenceHandoff.from_data(data)
    prompt = handoff.worker_prompt.to_data()
    prompt["prompt"] += "extra instruction"
    with pytest.raises(ValueError, match="mismatch"):
        SourceReferencePrompt.from_data(prompt)
    other = placement()
    other["owner"]["qualified_name"] = "another"
    with pytest.raises(ValueError, match="private validation manifest"):
        handoff.require_matches(compile_document(document(other)).worker_prompt)
    huge = ReferenceDefinition("Opaque:retry", "invariant", "X" * 64000, "spec:1")
    with pytest.raises(ValueError, match="bound"):
        compile_document(
            document(placement(requirement=huge.statement), definitions=[huge])
        )


@pytest.mark.parametrize(
    "version", ["implementation-request-v1", "implementation-request-v2"]
)
def test_legacy_request_reads_keep_references_disabled(version: str) -> None:
    data = ImplementationRequest("r", "o", "p", "base", "plan", (), (), ()).to_data()
    data["schema_version"] = version
    data.pop("source_reference_mode")
    data.pop("source_reference_prompt")
    restored = ImplementationRequest.from_data(data)
    assert restored.source_reference_mode == "off"
    assert restored.source_reference_prompt is None
    assert restored.to_data() == data
    data["source_reference_mode"] = "off"
    with pytest.raises(ValueError, match="legacy"):
        ImplementationRequest.from_data(data)


def test_request_cannot_downgrade_mutate_repair_or_execute_before_rollout() -> None:
    request = ImplementationRequest.from_source_reference_handoff(
        compile_document(document()), request_id="r"
    )
    for changes in (
        {"source_reference_mode": "observe"},
        {"schema_version": "implementation-request-v2"},
        {"prompt": "weakened"},
        {"base_commit": "b" * 40},
        {"allowed_paths": (".",)},
    ):
        with pytest.raises(ValueError):
            replace(request, **changes)
    with pytest.raises(ValueError, match="new design revision"):
        request.repair_prompt({"finding": "x"})
    with pytest.raises(ValueError, match="not integrated"):
        request.require_supported_execution()
    changed = request.to_data()
    changed["schema_version"] = "implementation-request-v999"
    with pytest.raises(ValueError):
        ImplementationRequest.from_data(changed)


def test_desired_state_keeps_placement_identity_and_detached_data() -> None:
    doc = document()
    before = copy.deepcopy(doc)
    state = DesiredState.from_snapshot(doc)
    assert doc == before
    assert (
        state.document["source_reference_placements"][0]["obligation_id"]
        == "placement:retry"
    )
    assert DesiredState.from_data(state.to_data()) == state


def test_design_projection_keeps_placement_identity_and_cannot_use_legacy_handoff() -> (
    None
):
    baseline = document()
    baseline["source_reference_placements"] = []
    proposal = compile_proposal_revision(
        "design:1",
        baseline,
        {"source_reference_placements": [{**placement(), "action": "added"}]},
        acceptance_criteria=(),
        must_preserve=(),
        non_goals=(),
        allowed_paths=("payments",),
        source_refs=("spec:1",),
    )
    handoff = DesignHandoff.compile(baseline, proposal, base_commit="a" * 40)
    desired = handoff.to_data()["desired"]["document"]
    assert desired["source_reference_placements"] == [placement()]
    assert compile_document(desired)
    with pytest.raises(ValueError, match="source reference compiler"):
        ImplementationRequest.from_structrr_handoff(handoff)


def test_preservation_only_handoff_and_unavailable_local_check_are_explicit() -> None:
    doc = document(preservation=[preservation()])
    doc["source_reference_placements"] = []
    handoff = compile_document(doc)
    assert handoff.validation_manifest["contract"]["obligations"] == []
    assert "unavailable in this build" in handoff.worker_prompt.prompt
    assert handoff.worker_prompt.to_data()["source_reference_mode"] == "enforce"
    with pytest.raises(ValueError, match="empty"):
        compile_document(document(), reference_check_command=())
    with pytest.raises(ValueError, match="empty"):
        compile_document(document(), verification_commands=((),))


@pytest.mark.parametrize("lifecycle", ["accepted", "proposed", "deprecated"])
def test_only_active_catalog_entries_can_be_required_placements(lifecycle: str) -> None:
    definition = ReferenceDefinition(
        "Opaque:retry",
        "invariant",
        "Keep the original key.",
        "spec:1",
        lifecycle=lifecycle,
    )
    with pytest.raises(ValueError, match="active"):
        compile_document(document(definitions=[definition]))


def test_preflight_blocks_reference_requests_before_any_provider_invocation(
    tmp_path: Path,
) -> None:
    class Provider:
        provider_name = "test"

        def run(
            self,
            request: ImplementationRequest,
            *,
            worktree_root: Path,
            attempt_id: str,
        ) -> subprocess.CompletedProcess[str]:
            raise AssertionError("reference validation is not integrated yet")

    request = ImplementationRequest.from_source_reference_handoff(
        compile_document(document()), request_id="r"
    )
    attempt = run_coding_agent(
        Provider(), request, worktree_root=tmp_path, attempt_id="a"
    )
    assert attempt.status == CodingAgentStatus.POLICY_DENIED
    assert "not integrated" in str(attempt.error)
