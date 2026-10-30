from __future__ import annotations

import ast
import base64
import hashlib
import json
from pathlib import Path

import pytest
from powdrr_contracts.models import (
    Artifact,
    ContractError,
    OperationRequest,
    OperationUpdate,
    RepositorySnapshot,
    json_round_trip,
)


def test_operation_request_round_trips_binary_artifacts_and_snapshot() -> None:
    artifact = Artifact("docs/workflows/example.yaml", "text/yaml", b"workflow: \xff")
    request = OperationRequest(
        request_id="request-1",
        operation_kind="inspect_artifacts",
        inputs={"areas": ["workflows"]},
        repository=RepositorySnapshot(
            repository_id="owner/repo",
            branch="main",
            base_sha=None,
            head_sha="a" * 40,
            manifest_digest="b" * 64,
            worktree_digest="b" * 64,
            source_artifact_refs=(artifact.artifact_id,),
        ),
        artifacts=(artifact,),
    )

    decoded = OperationRequest.from_dict(request.to_dict())

    assert decoded == request
    assert decoded.artifacts[0].data == b"workflow: \xff"
    assert "repo_root" not in decoded.to_dict()


def test_from_dict_does_not_alias_caller_owned_nested_values() -> None:
    payload = OperationRequest(
        request_id="alias-1",
        operation_kind="plan",
        inputs={"items": ["original"]},
    ).to_dict()

    decoded = OperationRequest.from_dict(payload)
    payload["inputs"]["items"].append("mutated-after-parse")

    assert decoded.inputs == {"items": ["original"]}


def test_contract_rejects_unsupported_version_and_artifact_digest_mismatch() -> None:
    with pytest.raises(
        ContractError, match="unsupported operation_request schema_version"
    ):
        OperationRequest.from_dict(
            {"schema_version": 2, "request_id": "r", "operation_kind": "plan"}
        )
    encoded = base64.b64encode(b"expected").decode()
    with pytest.raises(ContractError, match="does not match"):
        Artifact.from_dict(
            {
                "artifact_id": "x",
                "media_type": "text/plain",
                "schema_version": 1,
                "encoding": "base64",
                "data": encoded,
                "sha256": hashlib.sha256(b"different").hexdigest(),
            }
        )


def test_json_copy_does_not_alias_nested_caller_values() -> None:
    original = {"nested": {"values": ["agent-owned"]}}

    copied = json_round_trip(original)
    copied["nested"]["values"].append("service-owned")

    assert original["nested"]["values"] == ["agent-owned"]


def test_operation_update_round_trip_preserves_distinct_empty_and_failure_states() -> (
    None
):
    empty = OperationUpdate("op-empty", 1, "succeeded")
    failed = OperationUpdate.from_dict(
        {
            "schema_version": 1,
            "operation_id": "op-failed",
            "revision": 0,
            "status": "failed",
            "failure": {
                "schema_version": 1,
                "category": "probe_generation_failed",
                "message": "generation failed",
                "retryable": False,
                "correlation_id": "request-1",
            },
        }
    )

    assert OperationUpdate.from_dict(empty.to_dict()).status == "succeeded"
    assert failed.status == "failed"
    assert failed.failure is not None
    assert failed.failure.category == "probe_generation_failed"


def test_registry_names_supported_product_operation_families() -> None:
    from powdrr_local.registry import OPERATION_KINDS

    assert {
        "design",
        "plan",
        "replan",
        "implement",
        "review_feedback",
        "bootstrap",
        "inspect_artifacts",
        "evaluate_prompt",
        "verification_diff",
    } <= OPERATION_KINDS


def test_contract_package_imports_without_application_dependencies() -> None:
    root = Path(__file__).parents[1] / "packages/contracts/src/powdrr_contracts"
    allowed = {
        "__future__",
        "base64",
        "binascii",
        "hashlib",
        "json",
        "math",
        "re",
        "dataclasses",
        "typing",
    }
    for source in root.glob("*.py"):
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module.split(".")[0]]
            else:
                continue
            assert set(names) <= allowed | {"powdrr_contracts"}, (source, names)


def test_package_import_graph_respects_agent_and_service_ownership() -> None:
    repository = Path(__file__).parents[1]
    boundaries = {
        "agent": (
            "packages/agent/src/powdrr_agent",
            {"powdrr_service", "powdrr_local"},
        ),
        "service": (
            "packages/service/src/powdrr_service",
            {"powdrr_agent", "powdrr_local"},
        ),
    }
    for side, (relative_root, forbidden) in boundaries.items():
        for source in (repository / relative_root).glob("*.py"):
            tree = ast.parse(source.read_text(encoding="utf-8"))
            imports: set[str] = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imports.update(alias.name.split(".")[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imports.add(node.module.split(".")[0])
            assert not imports & forbidden, (side, source, imports & forbidden)


def test_legacy_service_imports_are_explicitly_owned() -> None:
    repository = Path(__file__).parents[1]
    allowlist = json.loads(
        (
            repository / "docs/architecture/agent-service-legacy-import-allowlist.json"
        ).read_text(encoding="utf-8")
    )
    expected = {entry["import"] for entry in allowlist["entries"]}
    actual: set[str] = set()
    package = repository / "packages/service/src/powdrr_service"
    for source in package.rglob("*.py"):
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                for alias in node.names:
                    if node.module.startswith("powdrr_lift"):
                        actual.add(f"{node.module}.{alias.name}")
    assert actual == expected
    assert all(entry["owner_pr"] > 0 for entry in allowlist["entries"])


def test_legacy_planning_imports_reexport_service_owned_implementations() -> None:
    from powdrr_service.planning.assembly import PlannedWrite
    from powdrr_service.planning.compiler import compile_plan

    from powdrr_lift.workrr.planning.assembly import PlannedWrite as LegacyPlannedWrite
    from powdrr_lift.workrr.planning.compiler import compile_plan as legacy_compile_plan

    assert LegacyPlannedWrite is PlannedWrite
    assert legacy_compile_plan is compile_plan


def test_legacy_planning_driver_adapts_the_service_owned_machine() -> None:
    from powdrr_service.planning.driver import (
        SourceLedgerQuestionMachine as ServiceQuestionMachine,
    )

    from powdrr_lift.workrr.planning.driver import (
        SourceLedgerQuestionMachine as LegacyQuestionMachine,
    )

    assert issubclass(LegacyQuestionMachine, ServiceQuestionMachine)


def test_legacy_plan_sizing_reexports_service_owned_policy() -> None:
    from powdrr_service.planning import plan_sizing as service_plan_sizing

    from powdrr_lift.workrr import plan_sizing as legacy_plan_sizing

    assert legacy_plan_sizing.__all__ == service_plan_sizing.__all__
    for name in service_plan_sizing.__all__:
        assert getattr(legacy_plan_sizing, name) is getattr(service_plan_sizing, name)


def test_legacy_plan_record_names_reexport_service_owned_models() -> None:
    from powdrr_service.planning.plan_models import (
        PlannedWorkItem,
        ReviewedExecutionPlan,
    )

    from powdrr_lift.workrr.github_issue_queue import (
        PlannedWorkItem as LegacyPlannedWorkItem,
    )
    from powdrr_lift.workrr.github_issue_queue import (
        ReviewedExecutionPlan as LegacyReviewedExecutionPlan,
    )

    assert LegacyPlannedWorkItem is PlannedWorkItem
    assert LegacyReviewedExecutionPlan is ReviewedExecutionPlan


def test_local_checkpoint_persistence_is_owned_by_local_adapter() -> None:
    from powdrr_local.planning.checkpoints import (
        GitHubCommentCheckpointStore,
        LocalCheckpointStore,
        LocalDiagnosticCache,
    )
    from powdrr_service.planning import checkpoints as service_checkpoints

    from powdrr_lift.workrr.planning.checkpoints import (
        GitHubCommentCheckpointStore as LegacyGitHubCheckpointStore,
    )
    from powdrr_lift.workrr.planning.checkpoints import (
        LocalCheckpointStore as LegacyLocalCheckpointStore,
    )
    from powdrr_lift.workrr.planning.checkpoints import (
        LocalDiagnosticCache as LegacyLocalDiagnosticCache,
    )

    assert not hasattr(service_checkpoints, "LocalCheckpointStore")
    assert not hasattr(service_checkpoints, "LocalDiagnosticCache")
    assert not hasattr(service_checkpoints, "GitHubCommentCheckpointStore")
    assert LegacyLocalCheckpointStore is LocalCheckpointStore
    assert LegacyLocalDiagnosticCache is LocalDiagnosticCache
    assert LegacyGitHubCheckpointStore is GitHubCommentCheckpointStore


def test_planning_service_uses_issue_store_protocol_not_github_client() -> None:
    source = (
        Path(__file__).parents[1]
        / "packages/service/src/powdrr_service/planning/service.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)

    assert all(
        not (
            isinstance(node, ast.ImportFrom)
            and node.module == "powdrr_lift.workrr.github_issue_queue"
        )
        for node in ast.walk(tree)
    )


def test_plan_repair_publication_is_owned_by_local_adapter() -> None:
    from powdrr_local.planning.repair_publication import publish_repair_pull_request
    from powdrr_service.planning import repair_service

    assert callable(publish_repair_pull_request)
    assert not hasattr(repair_service, "publish_repair_pull_request")
