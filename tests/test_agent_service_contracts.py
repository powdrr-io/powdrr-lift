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
        "enum",
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


def test_semantic_instruction_policy_is_owned_by_service() -> None:
    repository = Path(__file__).parents[1]
    service = repository / "packages/service/src/powdrr_service"
    moved_modules = (
        service / "planning/semantic_contract_compiler.py",
        service / "planning/classifier_input.py",
        service / "evaluations/semantic_prompt_cases.py",
        service / "evaluations/semantic_prompt_variants.py",
    )
    for source in moved_modules:
        tree = ast.parse(source.read_text(encoding="utf-8"))
        imports: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.add(node.module.split(".")[0])
        assert "powdrr_lift" not in imports, source

    assert (service / "resources/evaluations/semantic-prompt-cases-v1.jsonl").is_file()
    assert not (
        repository / "src/powdrr_lift/workrr/semantic_contract_compiler.py"
    ).exists()
    assert not (repository / "src/powdrr_lift/workrr/semantic_prompt_cases.py").exists()
    assert not (
        repository / "src/powdrr_lift/workrr/semantic_prompt_variants.py"
    ).exists()
    assert not (repository / "src/powdrr_lift/core/classifier_input.py").exists()


def test_changelog_parser_is_service_owned_without_legacy_exports() -> None:
    import powdrr_lift

    repository = Path(__file__).parents[1]
    parser = repository / "packages/service/src/powdrr_service/changelog/parser.py"
    tree = ast.parse(parser.read_text(encoding="utf-8"))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module.split(".")[0])
    assert "powdrr_lift" not in imports
    assert not tuple((repository / "src/powdrr_lift/change_log_parser").glob("*.py"))
    assert not (repository / "src/powdrr_lift/core/schemas.py").exists()
    assert not hasattr(powdrr_lift, "parse_change_log")


def test_changelog_rendering_policy_is_service_owned_without_legacy_export() -> None:
    import powdrr_lift
    import powdrr_lift.change_log_template as agent_template
    import powdrr_lift.core as core

    repository = Path(__file__).parents[1]
    renderer = repository / "packages/service/src/powdrr_service/changelog/template.py"
    tree = ast.parse(renderer.read_text(encoding="utf-8"))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module.split(".")[0])
    assert "powdrr_lift" not in imports
    assert not hasattr(agent_template, "render_change_log_template")
    assert not hasattr(core, "render_change_log_template")
    assert not hasattr(powdrr_lift, "render_change_log_template")


def test_changelog_validation_policy_is_service_owned_without_agent_imports() -> None:
    import powdrr_lift.change_log_validation as agent_validation

    repository = Path(__file__).parents[1]
    validator = (
        repository
        / "packages/service/src/powdrr_service/changelog/validation_policy.py"
    )
    tree = ast.parse(validator.read_text(encoding="utf-8"))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module.split(".")[0])

    assert "powdrr_lift" not in imports
    assert not hasattr(agent_validation, "_validate_v2_file_sections")
    assert not hasattr(agent_validation, "_validate_v2_state_change")


def test_system_specification_validation_policy_is_service_owned() -> None:
    from powdrr_service.specifications.system import (
        build_system_specification_validation_report,
    )

    import powdrr_lift
    import powdrr_lift.core as core
    import powdrr_lift.core.system_specification as agent_system_specification

    repository = Path(__file__).parents[1]
    validator = (
        repository / "packages/service/src/powdrr_service/specifications/system.py"
    )
    tree = ast.parse(validator.read_text(encoding="utf-8"))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module.split(".")[0])

    assert "powdrr_lift" not in imports
    assert not hasattr(agent_system_specification, "_validate_section_item")
    assert not hasattr(agent_system_specification, "_collect_section_items")
    assert not hasattr(
        agent_system_specification, "build_system_specification_validation_report"
    )
    assert core.build_system_specification_validation_report is (
        build_system_specification_validation_report
    )
    assert powdrr_lift.build_system_specification_validation_report is (
        build_system_specification_validation_report
    )


def test_architecture_specification_policy_is_service_owned() -> None:
    from powdrr_service.specifications.architecture import (
        build_architecture_specification_validation_report,
    )

    import powdrr_lift.core.architecture_specification as agent_architecture

    repository = Path(__file__).parents[1]
    service_modules = (
        repository
        / "packages/service/src/powdrr_service/specifications/architecture.py",
        repository / "packages/service/src/powdrr_service/specifications/common.py",
    )
    for service_module in service_modules:
        tree = ast.parse(service_module.read_text(encoding="utf-8"))
        imports: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.add(node.module.split(".")[0])
        assert "powdrr_lift" not in imports

    assert not hasattr(agent_architecture, "_collect_entity_ids")
    assert not hasattr(agent_architecture, "_load_system_reference_ids")
    assert agent_architecture._validate_architecture_policy is (
        build_architecture_specification_validation_report
    )


def test_implementation_specification_policy_is_service_owned() -> None:
    from powdrr_service.specifications.implementation import (
        build_implementation_specification_validation_report,
    )

    import powdrr_lift.core.implementation_specification as agent_implementation

    repository = Path(__file__).parents[1]
    validator = (
        repository
        / "packages/service/src/powdrr_service/specifications/implementation.py"
    )
    tree = ast.parse(validator.read_text(encoding="utf-8"))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module.split(".")[0])

    assert "powdrr_lift" not in imports
    assert not hasattr(agent_implementation, "_collect_section_items")
    assert not hasattr(agent_implementation, "_collect_features")
    assert (
        agent_implementation._implementation_service.build_implementation_specification_validation_report
        is build_implementation_specification_validation_report
    )


def test_proposed_pr_coverage_policy_is_service_owned() -> None:
    from powdrr_service.specifications.proposed_pr_coverage import (
        validate_proposed_pr_coverage_and_effects,
    )
    from powdrr_service.specifications.proposed_pr_validation import (
        build_proposed_pr_specification_validation_report,
    )

    import powdrr_lift.core.pr_specification as agent_pr_specification

    assert not hasattr(agent_pr_specification, "_validate_multi_pr_feature_coverage")
    assert not hasattr(agent_pr_specification, "_validate_v1_effect_equivalence")
    assert (
        build_proposed_pr_specification_validation_report.__globals__[
            "validate_proposed_pr_coverage_and_effects"
        ]
        is validate_proposed_pr_coverage_and_effects
    )


def test_proposed_pr_specification_validation_policy_is_service_owned() -> None:
    from powdrr_service.specifications.proposed_pr_validation import (
        build_proposed_pr_specification_validation_report,
    )

    import powdrr_lift.core.pr_specification as agent_pr_specification

    assert not hasattr(agent_pr_specification, "_collect_detail_items")
    assert not hasattr(agent_pr_specification, "_collect_dependent_pr_ids")
    assert (
        agent_pr_specification._service_validate_pr_spec
        is build_proposed_pr_specification_validation_report
    )


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


def test_structrr_review_and_verification_policy_is_service_owned() -> None:
    repository = Path(__file__).parents[1]
    modules = (
        "proposal.py",
        "actual_diff.py",
        "candidate_comparison.py",
        "source_manifest.py",
        "verification_contract.py",
        "verification_obligations.py",
        "gate_compiler.py",
        "proposal_review.py",
        "intent_lineage.py",
        "invariant_review.py",
    )
    service_root = repository / "packages/service/src/powdrr_service/structrr"
    agent_root = repository / "src/powdrr_lift/structrr"
    for module in modules:
        source = service_root / module
        assert source.is_file()
        assert not (agent_root / module).exists()
        tree = ast.parse(source.read_text(encoding="utf-8"))
        imports = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module is not None
        }
        imports.update(
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        )
        assert not any(name.startswith("powdrr_lift") for name in imports), source

    from powdrr_service.structrr.invariant_review import InvariantReviewReceipt
    from powdrr_service.structrr.proposal import ProposalRevision
    from powdrr_service.structrr.proposal_review import ProposalReviewReceipt
    from powdrr_service.structrr.source_manifest import SourceManifest
    from powdrr_service.structrr.verification_contract import VerificationContract
    from powdrr_service.structrr.verification_obligations import VerificationObligation

    assert ProposalRevision.__module__ == "powdrr_service.structrr.proposal"
    assert SourceManifest.__module__ == "powdrr_service.structrr.source_manifest"
    assert ProposalReviewReceipt.__module__ == "powdrr_service.structrr.proposal_review"
    assert (
        VerificationContract.__module__
        == "powdrr_service.structrr.verification_contract"
    )
    assert (
        VerificationObligation.__module__
        == "powdrr_service.structrr.verification_obligations"
    )
    assert (
        InvariantReviewReceipt.__module__ == "powdrr_service.structrr.invariant_review"
    )


def test_prompt_and_benchmark_evaluation_policy_is_service_owned() -> None:
    repository = Path(__file__).parents[1]
    service_root = repository / "packages/service/src/powdrr_service/evaluations"
    agent_root = repository / "src/powdrr_lift/workrr"
    modules = (
        "deepswe_design_evaluation.py",
        "deepswe_probe_evaluation.py",
        "prompt_benchmark_comparison.py",
    )
    for module in modules:
        source = service_root / module
        assert source.is_file()
        assert not (agent_root / module).exists()
        tree = ast.parse(source.read_text(encoding="utf-8"))
        imports = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module is not None
        }
        imports.update(
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        )
        assert not any(name.startswith("powdrr_lift") for name in imports), source

    from powdrr_service.evaluations.deepswe_design_evaluation import (
        DEFAULT_STATE_DATA_RUBRIC,
    )

    assert DEFAULT_STATE_DATA_RUBRIC.is_file()


def test_service_owned_planning_has_no_agent_reexport_modules() -> None:
    repository = Path(__file__).parents[1]
    legacy_reexports = (
        "src/powdrr_lift/workrr/plan_sizing.py",
        "src/powdrr_lift/workrr/multipr_structrr.py",
        "src/powdrr_lift/workrr/planning/assembly.py",
        "src/powdrr_lift/workrr/planning/compiler.py",
        "src/powdrr_lift/workrr/planning/contracts.py",
        "src/powdrr_lift/workrr/planning/dependencies.py",
        "src/powdrr_lift/workrr/planning/questions.py",
        "src/powdrr_lift/workrr/planning/repair.py",
        "src/powdrr_lift/workrr/planning/repair_service.py",
        "src/powdrr_lift/workrr/planning/service.py",
        "src/powdrr_lift/workrr/planning/structrr.py",
        "src/powdrr_lift/workrr/planning/checkpoints.py",
        "src/powdrr_lift/workrr/planning/driver.py",
        "src/powdrr_lift/workrr/planning/source.py",
    )

    assert all(
        not (repository / relative_path).exists() for relative_path in legacy_reexports
    )


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


def test_legacy_plan_document_helpers_reexport_service_owned_codec() -> None:
    from powdrr_service.planning import plan_document

    from powdrr_lift.workrr import github_issue_queue

    assert (
        github_issue_queue._parse_reviewed_plan_yaml
        is plan_document.parse_reviewed_plan_yaml
    )
    assert (
        github_issue_queue.serialize_reviewed_plan_yaml
        is plan_document.serialize_reviewed_plan_yaml
    )
    assert (
        github_issue_queue.render_plan_repair_metadata
        is plan_document.render_plan_repair_metadata
    )
    assert (
        github_issue_queue.GitHubIssueQueueError is not plan_document.PlanDocumentError
    )


def test_legacy_plan_workflow_parser_uses_service_owned_validation() -> None:
    from powdrr_service.planning import workflow_instance

    from powdrr_lift.workrr import github_issue_plan_generation

    assert (
        github_issue_plan_generation.PLAN_WORKFLOW_SCHEMA
        is workflow_instance.PLAN_WORKFLOW_SCHEMA
    )
    assert (
        github_issue_plan_generation.PLAN_WORKFLOW_PROCESS
        is workflow_instance.PLAN_WORKFLOW_PROCESS
    )
    assert (
        github_issue_plan_generation.PlanWorkflowInstance
        is workflow_instance.PlanWorkflowInstance
    )
    assert (
        github_issue_plan_generation._parse_plan_workflow_instance_yaml
        is workflow_instance.parse_plan_workflow_instance_yaml
    )


def test_local_checkpoint_persistence_is_owned_by_local_adapter() -> None:
    from powdrr_local.planning.checkpoints import (
        GitHubCommentCheckpointStore,
        LocalCheckpointStore,
        LocalDiagnosticCache,
    )
    from powdrr_service.planning import checkpoints as service_checkpoints

    assert not hasattr(service_checkpoints, "LocalCheckpointStore")
    assert not hasattr(service_checkpoints, "LocalDiagnosticCache")
    assert not hasattr(service_checkpoints, "GitHubCommentCheckpointStore")
    assert LocalCheckpointStore.__module__ == "powdrr_local.planning.checkpoints"
    assert LocalDiagnosticCache.__module__ == "powdrr_local.planning.checkpoints"
    assert (
        GitHubCommentCheckpointStore.__module__ == "powdrr_local.planning.checkpoints"
    )


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
