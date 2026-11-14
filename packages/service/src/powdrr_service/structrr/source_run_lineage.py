"""Stable-ID lineage records for protected source-reference implementation runs."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

SOURCE_RUN_LINEAGE_SCHEMA = "structrr-source-run-lineage-v1"


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _node(
    artifact_id: str,
    kind: str,
    *,
    status: str = "recorded",
    attributes: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "artifact_id": artifact_id,
        "kind": kind,
        "status": status,
        "attributes": dict(attributes or {}),
    }


def _link(source_id: str, relation: str, target_id: str) -> dict[str, str]:
    return {"source_id": source_id, "relation": relation, "target_id": target_id}


def build_source_run_lineage(
    *,
    run_id: str,
    lineage_context: Mapping[str, Any],
    accepted_design: Mapping[str, Any],
    handoff: Mapping[str, Any],
    terminal: Mapping[str, Any],
) -> dict[str, Any]:
    """Build a run graph whose joins use domain IDs, not content fingerprints."""
    if not _text(run_id):
        raise ValueError("source lineage requires a stable run ID")
    prompt = handoff.get("worker_prompt")
    manifest = handoff.get("validation_manifest")
    if not isinstance(prompt, Mapping) or not isinstance(manifest, Mapping):
        raise ValueError("source lineage requires a compiled handoff bundle")
    contract = prompt.get("contract")
    if not isinstance(contract, Mapping):
        raise ValueError("source lineage handoff has no canonical contract")

    plan_id = lineage_context.get("plan_id")
    item_id = lineage_context.get("item_id")
    base_commit = contract.get("base_commit")
    design_revision_id = (
        f"design:{plan_id}:{item_id}"
        if _text(plan_id) and _text(item_id)
        else f"design:{run_id}"
    )
    run_artifact_id = f"source-run:{run_id}"
    handoff_id = f"source-handoff:{run_id}"
    contract_id = f"source-contract:{run_id}"
    prompt_id = f"worker-prompt:{run_id}"
    manifest_id = f"validation-manifest:{run_id}"
    catalog_id = f"reference-catalog:{base_commit}"
    attempt_id = f"worker-attempt:{run_id}"
    candidate_id = f"candidate:{run_id}"
    nodes: list[dict[str, Any]] = [
        _node(
            run_artifact_id,
            "source_run",
            attributes={
                "run_id": run_id,
                "workflow_id": lineage_context.get("workflow_id"),
                "task_id": lineage_context.get("task_id"),
            },
        ),
        _node(
            design_revision_id,
            "accepted_design",
            attributes={
                "plan_id": plan_id,
                "item_id": item_id,
                "design_path": lineage_context.get("design_path"),
                "base_commit": base_commit,
                "design_blob_id": lineage_context.get("design_blob_id"),
                "design_fingerprint": contract.get("design_fingerprint"),
            },
        ),
        _node(
            handoff_id,
            "compiled_handoff",
            attributes={
                "bundle_fingerprint": handoff.get("fingerprint"),
            },
        ),
        _node(
            contract_id,
            "source_contract",
            attributes={
                "contract_fingerprint": prompt.get("contract_fingerprint"),
                "catalog_id": catalog_id,
            },
        ),
        _node(
            prompt_id,
            "worker_prompt",
            attributes={"prompt_fingerprint": prompt.get("fingerprint")},
        ),
        _node(
            manifest_id,
            "validation_manifest",
            attributes={"manifest_fingerprint": manifest.get("fingerprint")},
        ),
        _node(
            catalog_id,
            "reference_catalog",
            attributes={"catalog_fingerprint": contract.get("catalog_digest")},
        ),
        _node(
            attempt_id,
            "worker_attempt",
            status=str(terminal.get("outcome", "failed")),
            attributes={"attempt_id": run_id},
        ),
    ]
    links = [
        _link(run_artifact_id, "governed_by", design_revision_id),
        _link(run_artifact_id, "compiled_as", handoff_id),
        _link(run_artifact_id, "invoked", attempt_id),
        _link(handoff_id, "compiled_from", design_revision_id),
        _link(handoff_id, "contains", contract_id),
        _link(handoff_id, "contains", prompt_id),
        _link(handoff_id, "contains", manifest_id),
        _link(contract_id, "uses", catalog_id),
    ]

    definitions = {
        item.get("id"): item
        for item in contract.get("definitions", [])
        if isinstance(item, Mapping) and _text(item.get("id"))
    }
    private_manifest = manifest.get("private_validation_data")
    raw_evidence_contracts = (
        private_manifest.get("obligation_evidence_contracts", [])
        if isinstance(private_manifest, Mapping)
        else []
    )
    evidence_contracts = {
        item.get("obligation_id"): item
        for item in raw_evidence_contracts
        if isinstance(item, Mapping) and _text(item.get("obligation_id"))
    }
    obligations: dict[str, Mapping[str, Any]] = {}
    for collection, kind in (
        ("obligations", "placement"),
        ("preservation", "preservation"),
    ):
        rows = contract.get(collection, [])
        if not isinstance(rows, list):
            continue
        for item in rows:
            if not isinstance(item, Mapping):
                continue
            obligation_key = item.get("obligation_id", item.get("id"))
            reference_id = item.get("ref")
            if not _text(obligation_key) or not _text(reference_id):
                continue
            obligation_id = f"structrr-obligation:{obligation_key}"
            reference_artifact_id = f"structrr-reference:{reference_id}"
            obligations[obligation_id] = item
            nodes.append(
                _node(
                    obligation_id,
                    kind,
                    attributes={
                        "source_id": obligation_key,
                        "reference_id": reference_id,
                        "verification_refs": list(item.get("verification_refs", [])),
                        "required_routes": list(
                            evidence_contracts.get(obligation_key, {}).get(
                                "review_routes", []
                            )
                        ),
                    },
                )
            )
            links.extend(
                (
                    _link(design_revision_id, "defines", obligation_id),
                    _link(handoff_id, "carries", obligation_id),
                )
            )
            if reference_id not in definitions:
                nodes.append(_node(reference_artifact_id, "structrr_reference"))
            else:
                nodes.append(
                    _node(
                        reference_artifact_id,
                        "structrr_reference",
                        attributes={"kind": definitions[reference_id].get("kind")},
                    )
                )
            links.append(_link(obligation_id, "refers_to", reference_artifact_id))

    case_ids: set[str] = set()
    case_rows = accepted_design.get("required_test_cases", [])
    if isinstance(case_rows, list):
        for case in case_rows:
            if not isinstance(case, Mapping) or not _text(case.get("id")):
                continue
            case_key = str(case["id"])
            case_id = f"verification-case:{case_key}"
            case_ids.add(case_key)
            scenario = case.get("behavior_scenario")
            scenario_key = (
                scenario.get("scenario_id") if isinstance(scenario, Mapping) else None
            )
            nodes.append(
                _node(
                    case_id,
                    "verification_case",
                    attributes={"source_id": case_key, "scenario_id": scenario_key},
                )
            )
            links.append(_link(design_revision_id, "requires_case", case_id))
            if _text(scenario_key):
                scenario_id = f"behavior-scenario:{scenario_key}"
                nodes.append(_node(scenario_id, "behavior_scenario"))
                links.append(_link(case_id, "instantiates", scenario_id))

    scenario_case_ids = contract.get("behavior_case_ids", {})
    for obligation_id, obligation in obligations.items():
        for reference in obligation.get("verification_refs", []):
            resolved_cases: Sequence[str]
            if reference in case_ids:
                resolved_cases = (str(reference),)
            elif isinstance(scenario_case_ids, Mapping):
                raw = scenario_case_ids.get(reference, ())
                resolved_cases = raw if isinstance(raw, list) else ()
            else:
                resolved_cases = ()
            for case_key in resolved_cases:
                links.append(
                    _link(obligation_id, "verified_by", f"verification-case:{case_key}")
                )

    candidate_tree = terminal.get("candidate_tree") or terminal.get(
        "candidate_fingerprint"
    )
    if candidate_tree is None:
        candidate_tree = (
            terminal.get("obligation_route_evidence", {}).get("candidate_tree")
            if isinstance(terminal.get("obligation_route_evidence"), Mapping)
            else None
        )
    candidate_status = "available" if _text(candidate_tree) else "missing"
    nodes.append(
        _node(
            candidate_id,
            "candidate",
            status=candidate_status,
            attributes={"tree_identity": candidate_tree},
        )
    )
    links.append(_link(attempt_id, "produced", candidate_id))

    validation = terminal.get("validation")
    if isinstance(validation, Mapping):
        for result in validation.get("results", []):
            if not isinstance(result, Mapping) or not _text(result.get("profile")):
                continue
            profile = str(result["profile"])
            evidence_id = f"validation-evidence:{run_id}:{profile}"
            status = str(result.get("status", "unknown"))
            nodes.append(
                _node(
                    evidence_id,
                    "validation_evidence",
                    status=status,
                    attributes={
                        "profile": profile,
                        "test_id": result.get("test_id"),
                        "candidate_tree": candidate_tree,
                    },
                )
            )
            links.append(_link(candidate_id, "validated_by", evidence_id))

    route_evidence = terminal.get("obligation_route_evidence")
    if isinstance(route_evidence, Mapping):
        for route in route_evidence.get("routes", []):
            if not isinstance(route, Mapping) or not _text(route.get("obligation_id")):
                continue
            obligation_id = f"structrr-obligation:{route['obligation_id']}"
            route_name = str(route.get("route", "unknown"))
            evidence_id = (
                f"route-evidence:{run_id}:{route['obligation_id']}:{route_name}"
            )
            nodes.append(
                _node(
                    evidence_id,
                    "obligation_evidence",
                    status=str(route.get("status", "unknown")),
                    attributes={
                        "obligation_id": route["obligation_id"],
                        "route": route_name,
                        "candidate_tree": route.get("candidate_tree"),
                        "evidence_paths": list(route.get("evidence_paths", [])),
                        "case_ids": list(route.get("case_ids", [])),
                    },
                )
            )
            links.extend(
                (
                    _link(obligation_id, "supported_by", evidence_id),
                    _link(candidate_id, "reviewed_by", evidence_id),
                )
            )
            for case_key in route.get("case_ids", []):
                case_artifact_id = f"verification-case:{case_key}"
                links.append(_link(case_artifact_id, "supported_by", evidence_id))
            for receipt_id in route.get("evidence_receipts", []):
                receipt_artifact_id = f"story-receipt:{run_id}:{receipt_id}"
                nodes.append(
                    _node(
                        receipt_artifact_id,
                        "story_receipt",
                        status=str(route.get("status", "unknown")),
                        attributes={
                            "receipt_id": receipt_id,
                            "candidate_tree": route.get("story_candidate_tree"),
                            "obligation_id": route.get("obligation_id"),
                        },
                    )
                )
                links.append(_link(evidence_id, "contains", receipt_artifact_id))
                links.append(_link(obligation_id, "supported_by", receipt_artifact_id))
                links.append(_link(candidate_id, "reviewed_by", receipt_artifact_id))
                for case_key in route.get("case_ids", []):
                    links.append(
                        _link(
                            f"verification-case:{case_key}",
                            "verified_by",
                            receipt_artifact_id,
                        )
                    )

    unique_nodes: dict[str, dict[str, Any]] = {}
    for node in nodes:
        artifact_id = node["artifact_id"]
        existing = unique_nodes.get(artifact_id)
        if existing is not None and existing != node:
            raise ValueError(f"Conflicting source lineage artifact ID: {artifact_id}")
        unique_nodes[artifact_id] = node
    unique_links = {
        (link["source_id"], link["relation"], link["target_id"]): link for link in links
    }
    return {
        "schema_version": SOURCE_RUN_LINEAGE_SCHEMA,
        "run_id": run_id,
        "transfer_required": False,
        "artifacts": [unique_nodes[key] for key in sorted(unique_nodes)],
        "links": [unique_links[key] for key in sorted(unique_links)],
    }


def bind_source_run_patch(
    record: Mapping[str, Any], *, patch_sha256: str
) -> dict[str, Any]:
    """Add a transfer decision keyed by the run and patch's stable artifact ID."""
    if not _text(patch_sha256):
        raise ValueError("patch transfer lineage requires a patch digest")
    bound = {
        **dict(record),
        "artifacts": [dict(item) for item in record.get("artifacts", [])],
        "links": [dict(item) for item in record.get("links", [])],
        "transfer_required": True,
    }
    run_id = str(bound.get("run_id", ""))
    candidate_id = f"candidate:{run_id}"
    patch_id = f"patch:{run_id}"
    transfer_id = f"transfer-decision:{run_id}"
    bound["artifacts"].extend(
        (
            _node(patch_id, "patch", attributes={"patch_sha256": patch_sha256}),
            _node(transfer_id, "transfer_decision", status="eligible"),
        )
    )
    bound["links"].extend(
        (
            _link(candidate_id, "exported_as", patch_id),
            _link(transfer_id, "authorizes_transfer", patch_id),
        )
    )
    return bound


def audit_source_run_lineage(record: Mapping[str, Any]) -> dict[str, Any]:
    """Check stable-ID uniqueness, link resolution, and required run edges."""
    findings: list[dict[str, str]] = []

    def finding(code: str, subject_id: str, message: str) -> None:
        findings.append({"code": code, "subject_id": subject_id, "message": message})

    if record.get("schema_version") != SOURCE_RUN_LINEAGE_SCHEMA:
        finding("unsupported_schema", "schema_version", "Unsupported lineage schema")
    run_id = record.get("run_id")
    if not _text(run_id):
        finding("missing_run_id", "run_id", "A stable source run ID is required")
        run_id = ""
    raw_artifacts = record.get("artifacts")
    artifacts = raw_artifacts if isinstance(raw_artifacts, list) else []
    by_id: dict[str, Mapping[str, Any]] = {}
    for artifact in artifacts:
        if not isinstance(artifact, Mapping) or not _text(artifact.get("artifact_id")):
            finding(
                "invalid_artifact_id",
                "artifacts",
                "Artifact IDs must be non-empty strings",
            )
            continue
        artifact_id = str(artifact["artifact_id"])
        if artifact_id in by_id:
            finding(
                "duplicate_artifact_id",
                artifact_id,
                "Artifact ID occurs more than once",
            )
            continue
        by_id[artifact_id] = artifact
        if not _text(artifact.get("kind")) or not isinstance(
            artifact.get("attributes"), Mapping
        ):
            finding(
                "invalid_artifact",
                artifact_id,
                "Artifact kind and attributes are required",
            )

    raw_links = record.get("links")
    links = raw_links if isinstance(raw_links, list) else []
    seen_links: set[tuple[str, str, str]] = set()
    link_set: set[tuple[str, str, str]] = set()
    for link in links:
        if not isinstance(link, Mapping):
            finding("invalid_link", "links", "Lineage links must be objects")
            continue
        source = link.get("source_id")
        relation = link.get("relation")
        target = link.get("target_id")
        if not all(_text(value) for value in (source, relation, target)):
            finding(
                "invalid_link",
                "links",
                "Links require stable source, relation, and target IDs",
            )
            continue
        key = (str(source), str(relation), str(target))
        if key in seen_links:
            finding(
                "duplicate_link", "|".join(key), "Lineage link occurs more than once"
            )
            continue
        seen_links.add(key)
        link_set.add(key)
        if key[0] not in by_id or key[2] not in by_id:
            finding(
                "unresolved_link",
                "|".join(key),
                "Lineage link references an unknown artifact ID",
            )

    run_artifact_id = f"source-run:{run_id}"
    required_kinds = {
        "accepted_design",
        "compiled_handoff",
        "source_contract",
        "worker_prompt",
        "validation_manifest",
        "reference_catalog",
        "worker_attempt",
        "candidate",
    }
    expected_run_artifacts = {
        "source_run": run_artifact_id,
        "compiled_handoff": f"source-handoff:{run_id}",
        "source_contract": f"source-contract:{run_id}",
        "worker_prompt": f"worker-prompt:{run_id}",
        "validation_manifest": f"validation-manifest:{run_id}",
        "worker_attempt": f"worker-attempt:{run_id}",
        "candidate": f"candidate:{run_id}",
    }
    for kind, expected_id in expected_run_artifacts.items():
        matches = [item for item in by_id.values() if item.get("kind") == kind]
        if len(matches) != 1:
            finding(
                "cross_run_or_duplicate_artifact",
                str(expected_id),
                f"Run must have exactly one {kind!r} artifact",
            )
        elif matches[0].get("artifact_id") != expected_id:
            finding(
                "cross_run_artifact",
                str(matches[0].get("artifact_id")),
                f"{kind!r} artifact ID belongs to a different run",
            )
    design_artifacts = [
        item for item in by_id.values() if item.get("kind") == "accepted_design"
    ]
    if len(design_artifacts) == 1:
        design = design_artifacts[0]
        attributes = design.get("attributes", {})
        plan_id = attributes.get("plan_id")
        item_id = attributes.get("item_id")
        expected_design_id = (
            f"design:{plan_id}:{item_id}"
            if _text(plan_id) and _text(item_id)
            else f"design:{run_id}"
        )
        if design.get("artifact_id") != expected_design_id:
            finding(
                "design_id_mismatch",
                str(design.get("artifact_id")),
                "Accepted design ID must be based on its stable plan and item IDs",
            )
    for artifact in by_id.values():
        artifact_id = str(artifact.get("artifact_id"))
        kind = str(artifact.get("kind"))
        run_scoped_prefixes = {
            "validation_evidence": "validation-evidence",
            "obligation_evidence": "route-evidence",
            "story_receipt": "story-receipt",
            "patch": "patch",
            "transfer_decision": "transfer-decision",
        }
        prefix = run_scoped_prefixes.get(kind)
        expected_scoped_id = (
            artifact_id == f"{prefix}:{run_id}"
            if kind in {"patch", "transfer_decision"}
            else artifact_id.startswith(f"{prefix}:{run_id}:")
            if prefix is not None
            else True
        )
        if prefix is not None and not expected_scoped_id:
            finding(
                "cross_run_artifact",
                artifact_id,
                "Run-scoped artifact ID belongs to a different run",
            )
        if (
            kind == "worker_attempt"
            and artifact.get("attributes", {}).get("attempt_id") != run_id
        ):
            finding(
                "attempt_id_mismatch",
                artifact_id,
                "Attempt stable ID differs from the reserved run attempt ID",
            )
    present_kinds = {str(item.get("kind")) for item in by_id.values()}
    for kind in sorted(required_kinds - present_kinds):
        finding(
            "missing_artifact_kind", kind, f"Required artifact kind {kind!r} is missing"
        )
    design_id = next(
        (
            str(item.get("artifact_id"))
            for item in by_id.values()
            if item.get("kind") == "accepted_design"
        ),
        "",
    )
    required_links = (
        (run_artifact_id, "governed_by", design_id),
        (run_artifact_id, "compiled_as", f"source-handoff:{run_id}"),
        (run_artifact_id, "invoked", f"worker-attempt:{run_id}"),
        (
            f"worker-attempt:{run_id}",
            "produced",
            f"candidate:{run_id}",
        ),
    )
    for source, relation, target in required_links:
        if (source, relation, target) not in link_set:
            finding(
                "missing_lineage_link",
                source,
                f"Required {relation!r} link to {target!r} is missing",
            )
    handoff_id = f"source-handoff:{run_id}"
    contract_id = f"source-contract:{run_id}"
    prompt_id = f"worker-prompt:{run_id}"
    manifest_id = f"validation-manifest:{run_id}"
    catalog = by_id.get(contract_id)
    catalog_id = (
        str(catalog.get("attributes", {}).get("catalog_id"))
        if isinstance(catalog, Mapping)
        else ""
    )
    for source, relation, target in (
        (
            run_artifact_id,
            "governed_by",
            design_id,
        ),
        (handoff_id, "contains", contract_id),
        (handoff_id, "contains", prompt_id),
        (handoff_id, "contains", manifest_id),
        (contract_id, "uses", catalog_id),
    ):
        if (source, relation, target) not in link_set:
            finding(
                "missing_lineage_link",
                source,
                f"Required {relation!r} link to {target!r} is missing",
            )
    for artifact in by_id.values():
        artifact_id = str(artifact.get("artifact_id"))
        kind = artifact.get("kind")
        if kind == "worker_attempt" and not any(
            source == artifact_id and relation == "produced"
            for source, relation, _ in link_set
        ):
            finding(
                "missing_lineage_link",
                artifact_id,
                "Worker attempt has no candidate link",
            )
        if kind == "worker_attempt" and artifact.get("status") != "accepted":
            finding(
                "attempt_not_accepted",
                artifact_id,
                "Worker attempt did not reach accepted status",
            )
        if kind == "candidate" and artifact.get("status") != "available":
            finding(
                "candidate_missing", artifact_id, "Candidate identity is unavailable"
            )
        if kind in {"placement", "preservation"} and not any(
            source == artifact_id and relation == "verified_by"
            for source, relation, _ in link_set
        ):
            finding(
                "obligation_without_case",
                artifact_id,
                "Obligation has no verification case link",
            )
        if kind == "validation_evidence" and artifact.get("status") != "passed":
            finding(
                "validation_evidence_failed",
                artifact_id,
                "Validation evidence did not pass",
            )
        if (
            record.get("transfer_required") is True
            and kind == "obligation_evidence"
            and artifact.get("status") != "passed"
        ):
            finding(
                "obligation_evidence_failed",
                artifact_id,
                "Obligation evidence did not pass",
            )
        if (
            record.get("transfer_required") is True
            and kind == "story_receipt"
            and artifact.get("status") != "passed"
        ):
            finding("story_receipt_failed", artifact_id, "Story receipt did not pass")

    if record.get("transfer_required") is True:
        patch_nodes = [item for item in by_id.values() if item.get("kind") == "patch"]
        transfer_nodes = [
            item for item in by_id.values() if item.get("kind") == "transfer_decision"
        ]
        if len(patch_nodes) != 1 or len(transfer_nodes) != 1:
            finding(
                "missing_transfer_artifact",
                str(run_id),
                "Transfer requires one patch and one decision artifact",
            )
        elif transfer_nodes[0].get("status") != "eligible":
            finding(
                "transfer_not_eligible",
                str(transfer_nodes[0].get("artifact_id")),
                "Patch transfer is not eligible",
            )
        elif not _text(patch_nodes[0].get("attributes", {}).get("patch_sha256")):
            finding(
                "patch_identity_missing",
                str(patch_nodes[0].get("artifact_id")),
                "Patch bytes have no recorded identity",
            )
        elif (
            f"candidate:{run_id}",
            "exported_as",
            str(patch_nodes[0].get("artifact_id")),
        ) not in link_set:
            finding(
                "missing_patch_link",
                str(run_id),
                "Candidate is not linked to the recorded patch",
            )
        elif not any(
            source == str(transfer_nodes[0].get("artifact_id"))
            and relation == "authorizes_transfer"
            and target == str(patch_nodes[0].get("artifact_id"))
            for source, relation, target in link_set
        ):
            finding(
                "missing_transfer_link",
                str(run_id),
                "Transfer decision does not authorize the recorded patch",
            )
        for obligation in by_id.values():
            if obligation.get("kind") not in {"placement", "preservation"}:
                continue
            attributes = obligation.get("attributes", {})
            required_routes = attributes.get("required_routes", [])
            for route_name in required_routes:
                matches = [
                    evidence
                    for evidence in by_id.values()
                    if evidence.get("kind") == "obligation_evidence"
                    and evidence.get("attributes", {}).get("obligation_id")
                    == attributes.get("source_id")
                    and evidence.get("attributes", {}).get("route") == route_name
                ]
                if len(matches) != 1:
                    finding(
                        "required_route_missing",
                        str(attributes.get("source_id")),
                        (
                            f"Required evidence route {route_name!r} is missing "
                            "or duplicated"
                        ),
                    )
                elif matches[0].get("status") != "passed":
                    finding(
                        "required_route_failed",
                        str(matches[0].get("artifact_id")),
                        f"Required evidence route {route_name!r} did not pass",
                    )

    findings.sort(key=lambda item: (item["code"], item["subject_id"], item["message"]))
    return {
        "schema_version": "structrr-source-run-lineage-audit-v1",
        "run_id": str(run_id),
        "passed": not findings,
        "findings": findings,
    }


__all__ = [
    "SOURCE_RUN_LINEAGE_SCHEMA",
    "audit_source_run_lineage",
    "bind_source_run_patch",
    "build_source_run_lineage",
]
