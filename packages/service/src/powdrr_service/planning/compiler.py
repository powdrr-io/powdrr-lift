"""Validate and compile accepted planning records into reviewed artifacts."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from powdrr_service.planning.evidence import Evidence, EvidenceError
from powdrr_service.planning.multipr_structrr import MultiPRStructrrProposal
from powdrr_service.planning.plan_document import (
    PlanDocumentError,
    parse_reviewed_plan_yaml,
    serialize_reviewed_plan_yaml,
)
from powdrr_service.planning.plan_models import PlannedWorkItem, ReviewedExecutionPlan
from powdrr_service.planning.workflow_ids import slugify_workflow_id
from powdrr_service.planning.workflow_instance import (
    PLAN_WORKFLOW_PROCESS,
    PLAN_WORKFLOW_SCHEMA,
    PlanWorkflowInstanceError,
    parse_plan_workflow_instance_yaml,
)

PLANNING_MANIFEST_SCHEMA = "https://powdrr.io/schemas/planning-manifest-v1"
_SHA1_RE = re.compile(r"^[0-9a-f]{40,64}$")
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_SAFE_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._/-]{0,99}$")
_RUNTIMES = frozenset(
    {
        "python",
        "swift",
        "rust",
        "golang",
        "java",
        "javascript",
        "typescript",
        "kotlin",
        "ruby",
        "php",
        "scala",
        "elixir",
        "erlang",
        "dart",
        "c++",
        "c#",
        "objective-c",
        "neutral",
    }
)


class PlanningCompilationError(ValueError):
    """Accepted planning records cannot be compiled into consistent artifacts."""


@dataclass(frozen=True, slots=True)
class PlanningManifest:
    """Canonical provenance and typed records embedded in a reviewed plan."""

    data: Mapping[str, Any]

    @classmethod
    def from_data(cls, value: Mapping[str, Any]) -> PlanningManifest:
        _validate_manifest(value)
        return cls(dict(value))

    @classmethod
    def create(cls, records: Mapping[str, Any]) -> PlanningManifest:
        """Build a manifest and compute its semantic revision digest in code."""
        if "semantic_revision_digest" in records:
            raise PlanningCompilationError(
                "semantic_revision_digest is computed by the compiler"
            )
        data = {"schema": PLANNING_MANIFEST_SCHEMA, **dict(records)}
        data["semantic_revision_digest"] = _semantic_digest(data)
        _validate_manifest(data)
        return cls(data)

    def to_data(self) -> dict[str, Any]:
        return _canonical(dict(self.data))


@dataclass(frozen=True, slots=True)
class CompiledPlan:
    """Validated plan, proposal, workflow files, and exact relative destinations."""

    plan: ReviewedExecutionPlan
    files: Mapping[str, str]
    destinations: tuple[str, ...]
    plan_path: str
    proposal_path: str
    verification_command: str
    ready_item_ids: tuple[str, ...]
    blocked_item_reasons: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class ValidationReport:
    valid: bool
    errors: tuple[str, ...]
    ready_item_ids: tuple[str, ...]
    blocked_item_reasons: Mapping[str, str]


def compile_plan(
    *,
    plan: ReviewedExecutionPlan,
    planning_manifest: PlanningManifest | Mapping[str, Any],
    repository_snapshot: Mapping[str, Any],
    structrr_proposal: MultiPRStructrrProposal,
    verification_command: str,
    plan_path: str,
) -> CompiledPlan:
    """Compile the queue plan, manifest, proposal, and workflows as one value.

    Nothing is written to disk here. Every cross-artifact gate runs before the
    caller stages the returned file mapping.
    """
    manifest = (
        planning_manifest
        if isinstance(planning_manifest, PlanningManifest)
        else PlanningManifest.from_data(planning_manifest)
    )
    if plan.planning is not None and dict(plan.planning) != manifest.to_data():
        raise PlanningCompilationError("plan carries a different planning manifest")
    _validate_repository_snapshot(repository_snapshot, manifest)
    if not verification_command.strip():
        raise PlanningCompilationError("verification command must not be empty")
    _repository_path(plan_path, "plan_path")
    _validate_plan_record_links(plan, manifest)
    proposal_data = structrr_proposal.to_data()
    proposal_info = manifest.data["structrr"]
    assert isinstance(proposal_info, Mapping)
    if structrr_proposal.plan_id != plan.plan_id:
        raise PlanningCompilationError("Structrr proposal belongs to another plan")
    if set(structrr_proposal.item_proposals) != {item.item_id for item in plan.items}:
        raise PlanningCompilationError(
            "Structrr proposal must contain one item proposal per plan item"
        )
    if proposal_info["baseline_fingerprint"] != proposal_data["baseline_fingerprint"]:
        raise PlanningCompilationError("manifest Structrr baseline fingerprint differs")
    if (
        proposal_data["baseline_fingerprint"]
        != manifest.data["base"]["structrr_fingerprint"]
    ):
        raise PlanningCompilationError(
            "proposal baseline differs from pinned base snapshot"
        )
    if proposal_info["proposal_fingerprint"] != proposal_data["fingerprint"]:
        raise PlanningCompilationError("manifest Structrr proposal fingerprint differs")
    proposal_path = str(proposal_info["proposal_path"])
    if plan.source_document is None:
        raise PlanningCompilationError("compiled plans require a source_document")
    _repository_path(plan.source_document, "source_document")
    if plan.source_document != manifest.data["source"]["path"]:
        raise PlanningCompilationError(
            "plan source_document differs from pinned source"
        )
    if plan_path == plan.source_document or proposal_path == plan.source_document:
        raise PlanningCompilationError(
            "generated artifacts cannot overwrite the source"
        )

    compiled_plan = ReviewedExecutionPlan(
        plan.plan_id,
        plan.items,
        plan.source_document,
        manifest.to_data(),
    )
    plan_yaml = serialize_reviewed_plan_yaml(compiled_plan)
    reparsed = parse_reviewed_plan_yaml(plan_yaml)
    if reparsed != compiled_plan:
        raise PlanningCompilationError("execution plan did not round-trip")

    files: dict[str, str] = {
        plan_path: plan_yaml,
        proposal_path: json.dumps(proposal_data, indent=2, sort_keys=True) + "\n",
    }
    workflow_ids = {item.item_id: item.workflow_id for item in plan.items}
    plan_slug = slugify_workflow_id(plan.plan_id)
    for item in plan.items:
        if item.workflow_file is None:
            raise PlanningCompilationError(
                f"plan item {item.item_id!r} has no workflow_file"
            )
        _repository_path(item.workflow_file, f"item {item.item_id} workflow_file")
        if item.workflow_file in files:
            raise PlanningCompilationError(
                f"duplicate artifact destination {item.workflow_file!r}"
            )
        definition = _workflow_definition(
            plan=compiled_plan,
            item=item,
            workflow_ids=workflow_ids,
            plan_path=plan_path,
            proposal_path=proposal_path,
            verification_command=verification_command,
            plan_slug=plan_slug,
        )
        rendered = yaml.safe_dump(definition, sort_keys=False, allow_unicode=True)
        try:
            parsed = parse_plan_workflow_instance_yaml(rendered)
        except PlanWorkflowInstanceError as error:
            raise PlanningCompilationError(
                f"generated workflow {item.workflow_id!r} failed validation: {error}"
            ) from error
        expected_dependencies = tuple(
            workflow_ids[dependency] for dependency in item.dependencies
        )
        if parsed.dependencies != expected_dependencies:
            raise PlanningCompilationError(
                f"workflow {item.workflow_id!r} lost a plan dependency"
            )
        context = parsed.inputs["proposed_pr_context"]
        assert isinstance(context, Mapping)
        if context.get("manual_prerequisite") != _expected_manual_prerequisite(
            item.manual_prerequisite
        ):
            raise PlanningCompilationError(
                f"workflow {item.workflow_id!r} changed manual prerequisite context"
            )
        files[item.workflow_file] = rendered

    if len(files) != len(set(files)):
        raise PlanningCompilationError("artifact destinations must be unique")
    _reject_overlapping_destinations(tuple(files), protected={plan.source_document})
    ready, blocked = _dependency_readiness(compiled_plan)
    compiled = CompiledPlan(
        compiled_plan,
        files,
        tuple(files),
        plan_path,
        proposal_path,
        verification_command,
        ready,
        blocked,
    )
    report = validate_compiled_plan(compiled, repository_snapshot)
    if not report.valid:
        raise PlanningCompilationError(
            "compiled plan validation failed: " + "; ".join(report.errors)
        )
    return compiled


def validate_compiled_plan(
    compiled: CompiledPlan, repository_snapshot: Mapping[str, Any]
) -> ValidationReport:
    """Recheck the complete artifact set immediately before it is staged."""
    errors: list[str] = []
    if set(compiled.destinations) != set(compiled.files) or len(
        compiled.destinations
    ) != len(set(compiled.destinations)):
        errors.append("compiled destination manifest differs from output files")
    try:
        _repository_path(compiled.plan_path, "plan_path")
        _repository_path(compiled.proposal_path, "proposal_path")
        manifest = PlanningManifest.from_data(compiled.plan.planning or {})
        _validate_repository_snapshot(repository_snapshot, manifest)
        manifest_source = manifest.data["source"]
        assert isinstance(manifest_source, Mapping)
        _reject_overlapping_destinations(
            compiled.destinations, protected={str(manifest_source["path"])}
        )
        plan_yaml = compiled.files[compiled.plan_path]
        parsed_plan = parse_reviewed_plan_yaml(plan_yaml)
        if parsed_plan != compiled.plan:
            errors.append("execution plan file differs from compiled plan record")
        manifest = PlanningManifest.from_data(parsed_plan.planning or {})
        manifest_structrr = manifest.data["structrr"]
        assert isinstance(manifest_structrr, Mapping)
        proposal = json.loads(compiled.files[compiled.proposal_path])
        if (
            not isinstance(proposal, Mapping)
            or proposal.get("plan_id") != compiled.plan.plan_id
            or proposal.get("fingerprint") != manifest_structrr["proposal_fingerprint"]
            or manifest_structrr["proposal_path"] != compiled.proposal_path
            or proposal.get("baseline_fingerprint")
            != manifest.data["base"]["structrr_fingerprint"]
            or not _matches_content_fingerprint(proposal)
        ):
            errors.append("Structrr proposal file differs from planning manifest")
        workflow_ids = {item.item_id: item.workflow_id for item in compiled.plan.items}
        expected_workflow_paths = {
            item.workflow_file for item in compiled.plan.items if item.workflow_file
        }
        actual_workflow_paths = set(compiled.files) - {
            compiled.plan_path,
            compiled.proposal_path,
        }
        if actual_workflow_paths != expected_workflow_paths:
            errors.append("workflow destinations differ from plan workflow references")
        for item in compiled.plan.items:
            if item.workflow_file is None:
                continue
            workflow = parse_plan_workflow_instance_yaml(
                compiled.files[item.workflow_file]
            )
            if workflow.workflow_id != item.workflow_id:
                errors.append(f"workflow {item.workflow_id!r} has a different identity")
                continue
            expected_dependencies = tuple(
                workflow_ids[dependency] for dependency in item.dependencies
            )
            if workflow.dependencies != expected_dependencies:
                errors.append(
                    f"workflow {item.workflow_id!r} has different dependencies"
                )
            context = workflow.inputs["proposed_pr_context"]
            if not isinstance(context, Mapping) or any(
                context.get(field) != expected
                for field, expected in (
                    ("execution_plan_file", compiled.plan_path),
                    ("structrr_proposal_file", compiled.proposal_path),
                    ("item_id", item.item_id),
                    ("title", item.title),
                    ("objective", item.objective),
                    ("scope", list(item.scope)),
                    ("allowed_paths", list(item.allowed_paths)),
                    ("implementation_steps", list(item.implementation_steps)),
                    ("acceptance_criteria", list(item.acceptance_criteria)),
                    ("dependencies", list(item.dependencies)),
                    ("verification_command", compiled.verification_command),
                    ("structrr_item_id", item.item_id),
                    ("require_structrr_proposal", True),
                    ("manual_prerequisite", item.manual_prerequisite),
                )
            ):
                errors.append(f"workflow {item.workflow_id!r} lost plan context")
    except (
        PlanDocumentError,
        KeyError,
        TypeError,
        ValueError,
        PlanWorkflowInstanceError,
    ) as error:
        errors.append(str(error))
    ready, blocked = _dependency_readiness(compiled.plan)
    if ready != compiled.ready_item_ids or blocked != compiled.blocked_item_reasons:
        errors.append("compiled ready/blocked summary differs from dependency graph")
    return ValidationReport(not errors, tuple(errors), ready, blocked)


def _workflow_definition(
    *,
    plan: ReviewedExecutionPlan,
    item: PlannedWorkItem,
    workflow_ids: Mapping[str, str],
    plan_path: str,
    proposal_path: str,
    verification_command: str,
    plan_slug: str,
) -> dict[str, Any]:
    context: dict[str, Any] = {
        "plan_id": plan.plan_id,
        "execution_plan_file": plan_path,
        "item_id": item.item_id,
        "title": item.title,
        "objective": item.objective,
        "scope": list(item.scope),
        "allowed_paths": list(item.allowed_paths),
        "implementation_steps": list(item.implementation_steps),
        "acceptance_criteria": list(item.acceptance_criteria),
        "dependencies": list(item.dependencies),
        "verification_command": verification_command,
        "structrr_proposal_file": proposal_path,
        "structrr_item_id": item.item_id,
        "require_structrr_proposal": True,
    }
    if item.manual_prerequisite is not None:
        context["manual_prerequisite"] = dict(item.manual_prerequisite)
    if item.item_id == "final-integration-review":
        context.update(
            final_integration_review=True,
            final_review_report_path=(
                Path("docs") / "plans" / f"{plan_slug}-final-structrr-review.json"
            ).as_posix(),
        )
    return {
        "schema": PLAN_WORKFLOW_SCHEMA,
        "workflow_id": item.workflow_id,
        "process_id": PLAN_WORKFLOW_PROCESS,
        "dependencies": [workflow_ids[dependency] for dependency in item.dependencies],
        "inputs": {
            "work_item_name": plan.plan_id,
            "proposed_pr_id": item.workflow_id,
            "feature_description": item.objective,
            "proposed_pr_context": context,
        },
    }


def _validate_plan_record_links(
    plan: ReviewedExecutionPlan, manifest: PlanningManifest
) -> None:
    data = manifest.data
    expected_items = {item.item_id for item in plan.items}
    if len(expected_items) != len(plan.items):
        raise PlanningCompilationError("plan item IDs must be unique")
    allocation = data["item_unit_ids"]
    assert isinstance(allocation, Mapping)
    if set(allocation) != expected_items:
        raise PlanningCompilationError("manifest item-to-unit allocation differs")
    unit_ids = {str(unit["id"]) for unit in data["units"]}
    assigned = [unit for refs in allocation.values() for unit in refs]
    if set(assigned) != unit_ids or len(assigned) != len(set(assigned)):
        raise PlanningCompilationError("each work unit must belong to exactly one item")
    requirement_ids = {str(item["id"]) for item in data["requirements"]}
    unit_requirements = {
        requirement_id
        for unit in data["units"]
        for requirement_id in unit["requirement_ids"]
    }
    complete_requirements = {
        item["requirement_id"]
        for item in data["coverage"]
        if item["status"] == "complete"
    }
    if not requirement_ids <= unit_requirements | complete_requirements:
        missing = sorted(requirement_ids - unit_requirements - complete_requirements)
        raise PlanningCompilationError(
            f"requirements lack work allocation or complete evidence: {missing}"
        )
    validation_ids = {str(item["id"]) for item in data["validations"]}
    validation_by_id = {str(item["id"]): item for item in data["validations"]}
    setup_by_id = {str(item["id"]): item for item in data["setups"]}
    setup_owners = {setup["owner_item_id"] for setup in setup_by_id.values()}
    if setup_owners - expected_items:
        raise PlanningCompilationError("human setup owner is absent from the plan")
    allocation_by_item = {
        item_id: set(unit_refs) for item_id, unit_refs in allocation.items()
    }
    item_by_id = {item.item_id: item for item in plan.items}
    if any(set(item.dependencies) - set(item_by_id) for item in plan.items):
        raise PlanningCompilationError("plan dependency references an unknown item")
    positions = {item.item_id: index for index, item in enumerate(plan.items)}
    for item in plan.items:
        if any(
            positions[dependency] >= positions[item.item_id]
            for dependency in item.dependencies
        ):
            raise PlanningCompilationError(
                "plan items must be ordered after dependencies"
            )
    unit_by_id = {str(unit["id"]): unit for unit in data["units"]}
    unit_to_item = {
        str(unit_id): item_id
        for item_id, unit_refs in allocation.items()
        for unit_id in unit_refs
    }
    for item_id, assigned_unit_ids in allocation_by_item.items():
        required_predecessors = {
            unit_to_item[predecessor_id]
            for unit_id in assigned_unit_ids
            for predecessor_id in unit_by_id[unit_id].get("prerequisite_unit_ids", [])
            if predecessor_id in unit_to_item
            and unit_to_item[predecessor_id] != item_id
        }
        if not required_predecessors <= set(item_by_id[item_id].dependencies):
            raise PlanningCompilationError(
                f"item {item_id!r} omits a work-unit dependency"
            )
    criteria_bindings = {
        (binding["item_id"], binding["criterion"]): binding
        for binding in data["validation_coverage"]
    }
    expected_criteria = {
        (item.item_id, criterion)
        for item in plan.items
        for criterion in item.acceptance_criteria
    }
    if set(criteria_bindings) != expected_criteria:
        raise PlanningCompilationError(
            "manifest acceptance criterion validation coverage differs from the plan"
        )
    for item in plan.items:
        units = allocation_by_item[item.item_id]
        unit_validations = {
            validation_id
            for unit in data["units"]
            if unit["id"] in units
            for validation_id in unit["validation_ids"]
        }
        if unit_validations - validation_ids:
            raise PlanningCompilationError(
                f"item {item.item_id!r} references missing validations"
            )
        for criterion in item.acceptance_criteria:
            binding = criteria_bindings.get((item.item_id, criterion))
            if (
                binding is None
                or not set(binding["validation_ids"]) <= unit_validations
            ):
                raise PlanningCompilationError(
                    f"item {item.item_id!r} acceptance criterion lacks "
                    "validation coverage"
                )
        declared = item.manual_prerequisite
        owned_setups = [
            setup
            for setup in setup_by_id.values()
            if setup["owner_item_id"] == item.item_id
        ]
        if declared is None and owned_setups:
            raise PlanningCompilationError(
                f"item {item.item_id!r} omits its human setup instructions"
            )
        if declared is not None:
            expected_validation_ids = tuple(
                dict.fromkeys(
                    validation_id
                    for setup in owned_setups
                    for validation_id in setup["unblocks_validation_ids"]
                )
            )
            expected_prerequisite = None
            if owned_setups:
                expected_prerequisite = {
                    "title": "; ".join(
                        dict.fromkeys(str(setup["title"]) for setup in owned_setups)
                    ),
                    "instructions": [
                        instruction
                        for setup in owned_setups
                        for instruction in setup["instructions"]
                    ],
                    "verification": "; ".join(
                        dict.fromkeys(
                            str(setup["verification"]) for setup in owned_setups
                        )
                    ),
                    "trigger": owned_setups[0]["trigger"],
                    "unblocks": "; ".join(
                        dict.fromkeys(
                            str(validation_by_id[validation_id]["target"])
                            for validation_id in expected_validation_ids
                        )
                    ),
                }
            if (
                expected_prerequisite != declared
                or set(expected_validation_ids) - unit_validations
            ):
                raise PlanningCompilationError(
                    f"item {item.item_id!r} setup context differs from its validations"
                )


def _validate_repository_snapshot(
    snapshot: Mapping[str, Any], manifest: PlanningManifest
) -> None:
    if set(snapshot) != {"source", "base"}:
        raise PlanningCompilationError(
            "repository_snapshot must contain the pinned source and base records"
        )
    if snapshot["source"] != manifest.data["source"]:
        raise PlanningCompilationError(
            "pinned source changed after planning; restart from a fresh snapshot"
        )
    if snapshot["base"] != manifest.data["base"]:
        raise PlanningCompilationError(
            "repository base changed after planning; restart from a fresh snapshot"
        )


def _dependency_readiness(
    plan: ReviewedExecutionPlan,
) -> tuple[tuple[str, ...], Mapping[str, str]]:
    ready = tuple(item.item_id for item in plan.items if not item.dependencies)
    blocked = {
        item.item_id: f"waiting for {', '.join(item.dependencies)}"
        for item in plan.items
        if item.dependencies
    }
    return ready, blocked


def _reject_overlapping_destinations(
    paths: Sequence[str], *, protected: set[str]
) -> None:
    if set(paths) & protected:
        raise PlanningCompilationError("generated artifacts overlap a pinned input")
    path_parts = {path: PurePosixPath(path).parts for path in paths}
    for index, first in enumerate(paths):
        for second in paths[index + 1 :]:
            first_parts, second_parts = path_parts[first], path_parts[second]
            if (
                first_parts == second_parts[: len(first_parts)]
                or second_parts == first_parts[: len(second_parts)]
            ):
                raise PlanningCompilationError(
                    f"artifact destinations overlap: {first!r}, {second!r}"
                )


def _validate_manifest(value: Mapping[str, Any]) -> None:
    expected = {
        "schema",
        "source",
        "base",
        "source_blocks",
        "source_anchors",
        "requirements",
        "coverage",
        "validation_coverage",
        "evidence",
        "units",
        "item_unit_ids",
        "runtime_facts",
        "validations",
        "setups",
        "structrr",
        "semantic_revision_digest",
        "latest_repair",
    }
    _exact(value, expected, "planning")
    if value["schema"] != PLANNING_MANIFEST_SCHEMA:
        raise PlanningCompilationError("unsupported planning manifest schema")
    source = value["source"]
    base = value["base"]
    if not isinstance(source, Mapping) or set(source) != {
        "repository",
        "commit_sha",
        "path",
        "blob_sha",
        "content_sha256",
    }:
        raise PlanningCompilationError("manifest source record is malformed")
    if not isinstance(base, Mapping) or set(base) != {
        "commit_sha",
        "structrr_fingerprint",
    }:
        raise PlanningCompilationError("manifest base record is malformed")
    for record, fields, label in (
        (source, ("commit_sha", "blob_sha"), "source"),
        (base, ("commit_sha",), "base"),
    ):
        for field in fields:
            if not isinstance(record[field], str) or not _SHA1_RE.fullmatch(
                record[field]
            ):
                raise PlanningCompilationError(f"manifest {label}.{field} is invalid")
    if not isinstance(source["repository"], str) or not source["repository"].strip():
        raise PlanningCompilationError("manifest source.repository is invalid")
    if not isinstance(source["path"], str):
        raise PlanningCompilationError("manifest source.path is invalid")
    _repository_path(source["path"], "source.path")
    if (
        not isinstance(source["content_sha256"], str)
        or not re.fullmatch(r"[0-9a-f]{64}", source["content_sha256"])
        or not _is_sha256(base["structrr_fingerprint"])
    ):
        raise PlanningCompilationError("manifest source or baseline digest is invalid")

    blocks = _records(value, "source_blocks", "id")
    anchors = _records(value, "source_anchors", "id")
    requirements = _records(value, "requirements", "id")
    evidence = _records(value, "evidence", "id")
    units = _records(value, "units", "id")
    validations = _records(value, "validations", "id")
    setups = _records(value, "setups", "id")
    _records(value, "coverage", "requirement_id")
    validation_coverage = value["validation_coverage"]
    if not isinstance(validation_coverage, list) or not all(
        isinstance(item, Mapping) for item in validation_coverage
    ):
        raise PlanningCompilationError("validation_coverage must be a list of mappings")
    criterion_keys: set[tuple[str, str]] = set()
    for binding in validation_coverage:
        _exact(
            binding,
            {"item_id", "criterion", "validation_ids", "reason"},
            "validation_coverage record",
        )
        item_id = binding.get("item_id")
        criterion = binding.get("criterion")
        if (
            not isinstance(item_id, str)
            or not _SAFE_ID.fullmatch(item_id)
            or not _nonempty(criterion)
            or not _nonempty(binding.get("reason"))
        ):
            raise PlanningCompilationError("acceptance criterion coverage is malformed")
        key = (item_id, criterion)
        if key in criterion_keys:
            raise PlanningCompilationError(
                "acceptance criterion coverage is duplicated"
            )
        criterion_keys.add(key)
        if set(
            _string_refs(binding.get("validation_ids"), "criterion validations")
        ) - set(validations):
            raise PlanningCompilationError(
                "criterion coverage references unknown validation"
            )
    block_ids, anchor_ids = set(blocks), set(anchors)
    requirement_ids, evidence_ids = set(requirements), set(evidence)
    unit_ids, validation_ids = set(units), set(validations)
    if not blocks or not anchors or not requirements or not units:
        raise PlanningCompilationError(
            "manifest needs source accounting, requirements, and allocated units"
        )
    if any(anchor.get("block_id") not in block_ids for anchor in anchors.values()):
        raise PlanningCompilationError("source anchor references an unknown block")
    for anchor in anchors.values():
        if (
            anchor.get("source_blob_sha") != source["blob_sha"]
            or not _positive_integer(anchor.get("start_line"))
            or not _positive_integer(anchor.get("end_line"))
            or anchor["end_line"] < anchor["start_line"]
            or not isinstance(anchor.get("quote_sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", anchor["quote_sha256"])
        ):
            raise PlanningCompilationError("source anchor metadata is invalid")
    dispositions = {"normative", "context", "duplicate"}
    for block in blocks.values():
        if block.get("disposition") not in dispositions:
            raise PlanningCompilationError("source block disposition is invalid")
        refs = _string_refs(
            block.get("requirement_ids"), "source block requirements", allow_empty=True
        )
        if set(refs) - requirement_ids:
            raise PlanningCompilationError(
                "source block references an unknown requirement"
            )
        if block["disposition"] == "normative" and not refs:
            raise PlanningCompilationError("normative source blocks need requirements")
        if block["disposition"] != "normative" and not _nonempty(block.get("reason")):
            raise PlanningCompilationError("context and duplicate blocks need a reason")

    for requirement in requirements.values():
        if not _nonempty(requirement.get("statement")) or requirement.get(
            "kind"
        ) not in {"behavior", "constraint", "acceptance", "human_setup"}:
            raise PlanningCompilationError("requirement record is malformed")
        if (
            set(_string_refs(requirement.get("source_refs"), "requirement source_refs"))
            - anchor_ids
        ):
            raise PlanningCompilationError(
                "requirement references an unknown source anchor"
            )
        if (
            set(
                _string_refs(
                    requirement.get("related_requirement_ids", []),
                    "related requirements",
                    allow_empty=True,
                )
            )
            - requirement_ids
        ):
            raise PlanningCompilationError(
                "requirement references an unknown related requirement"
            )
    coverage = {item["requirement_id"]: item for item in value["coverage"]}
    if set(coverage) != requirement_ids:
        raise PlanningCompilationError("each requirement needs one coverage record")
    for record in coverage.values():
        if record.get("status") not in {"complete", "partial", "missing"}:
            raise PlanningCompilationError("requirement coverage status is invalid")
        if not _nonempty(record.get("reason")):
            raise PlanningCompilationError("requirement coverage needs a reason")
        coverage_evidence_refs = set(
            _string_refs(
                record.get("evidence_ids"), "coverage evidence_ids", allow_empty=True
            )
        )
        if coverage_evidence_refs - evidence_ids:
            raise PlanningCompilationError("coverage references unknown evidence")
        if record["status"] == "complete" and not coverage_evidence_refs:
            raise PlanningCompilationError("complete coverage requires evidence")
        if record["status"] == "missing" and coverage_evidence_refs:
            raise PlanningCompilationError("missing coverage cannot cite evidence")
        if record["status"] == "partial" and not _nonempty(
            record.get("remaining_statement")
        ):
            raise PlanningCompilationError(
                "partial coverage needs its remaining statement"
            )

    for unit in units.values():
        if (
            set(_string_refs(unit.get("requirement_ids"), "unit requirements"))
            - requirement_ids
        ):
            raise PlanningCompilationError("unit references an unknown requirement")
        if (
            set(_string_refs(unit.get("validation_ids"), "unit validations"))
            - validation_ids
        ):
            raise PlanningCompilationError("unit references an unknown validation")
        prerequisites = set(
            _string_refs(
                unit.get("prerequisite_unit_ids", []),
                "unit prerequisites",
                allow_empty=True,
            )
        )
        if unit["id"] in prerequisites or prerequisites - unit_ids:
            raise PlanningCompilationError("unit prerequisite references are invalid")
        writes = unit.get("planned_writes")
        if not isinstance(writes, list):
            raise PlanningCompilationError("unit planned_writes must be a list")
        for write in writes:
            if (
                not isinstance(write, Mapping)
                or not isinstance(write.get("path"), str)
                or not isinstance(write.get("role"), str)
                or not (
                    write.get("runtime") is None
                    or (
                        isinstance(write.get("runtime"), str)
                        and write["runtime"] in _RUNTIMES
                    )
                )
            ):
                raise PlanningCompilationError("planned write runtime is invalid")
            if write.get("implementation_runtime") is not None:
                raise PlanningCompilationError("runtime must be recorded per write")
    allocation = value["item_unit_ids"]
    if not isinstance(allocation, Mapping) or not allocation:
        raise PlanningCompilationError("item_unit_ids must be a non-empty mapping")
    assigned_units = [
        unit_id
        for item_id, refs in allocation.items()
        if _SAFE_ID.fullmatch(item_id)
        for unit_id in _string_refs(refs, f"item {item_id} unit IDs")
    ]
    if set(assigned_units) != unit_ids or len(assigned_units) != len(
        set(assigned_units)
    ):
        raise PlanningCompilationError(
            "units must be allocated exactly once to plan items"
        )
    runtime_facts_raw = value["runtime_facts"]
    if not isinstance(runtime_facts_raw, list):
        raise PlanningCompilationError("runtime_facts must be a list")
    for fact in runtime_facts_raw:
        if (
            not isinstance(fact, Mapping)
            or set(fact) != {"unit_id", "path", "runtime"}
            or not isinstance(fact["path"], str)
            or fact["unit_id"] not in unit_ids
            or not (
                fact["runtime"] is None
                or (isinstance(fact["runtime"], str) and fact["runtime"] in _RUNTIMES)
            )
        ):
            raise PlanningCompilationError("runtime fact is malformed")
    runtime_facts = {
        (fact["unit_id"], fact["path"], fact["runtime"]) for fact in runtime_facts_raw
    }
    expected_runtime_facts = {
        (unit["id"], write["path"], write["runtime"])
        for unit in units.values()
        for write in unit["planned_writes"]
    }
    if runtime_facts != expected_runtime_facts or len(runtime_facts) != len(
        runtime_facts_raw
    ):
        raise PlanningCompilationError("typed runtime facts differ from planned writes")
    for validation in validations.values():
        if (
            not _nonempty(validation.get("description"))
            or not _nonempty(validation.get("target"))
            or validation.get("kind")
            not in {"local_command", "ci_check", "manual_observation"}
        ):
            raise PlanningCompilationError("validation record is malformed")
        if (
            set(
                _string_refs(
                    validation.get("requirement_ids"), "validation requirements"
                )
            )
            - requirement_ids
        ):
            raise PlanningCompilationError("validation references unknown requirements")
        if (
            set(_string_refs(validation.get("source_refs"), "validation source_refs"))
            - anchor_ids
        ):
            raise PlanningCompilationError(
                "validation references unknown source anchors"
            )
        if (
            set(
                _string_refs(
                    validation.get("evidence_ids"),
                    "validation evidence_ids",
                    allow_empty=True,
                )
            )
            - evidence_ids
        ):
            raise PlanningCompilationError("validation references unknown evidence")
    for setup in setups.values():
        if (
            setup.get("owner") != "human"
            or not _nonempty(setup.get("title"))
            or not _nonempty(setup.get("verification"))
            or setup.get("trigger") != "after_implementation_before_validation"
            or not isinstance(setup.get("instructions"), list)
            or not setup["instructions"]
            or not all(_nonempty(step) for step in setup["instructions"])
        ):
            raise PlanningCompilationError("human setup record is malformed")
        if (
            set(_string_refs(setup.get("requirement_ids"), "setup requirements"))
            - requirement_ids
        ):
            raise PlanningCompilationError("setup references unknown requirements")
        if (
            set(_string_refs(setup.get("source_refs"), "setup source_refs"))
            - anchor_ids
        ):
            raise PlanningCompilationError("setup references unknown source anchors")
        if (
            set(_string_refs(setup.get("unblocks_validation_ids"), "setup validations"))
            - validation_ids
        ):
            raise PlanningCompilationError("setup references unknown validations")
        if not _SAFE_ID.fullmatch(str(setup.get("owner_item_id", ""))):
            raise PlanningCompilationError("setup owner item ID is invalid")

    structrr = value["structrr"]
    if not isinstance(structrr, Mapping) or set(structrr) != {
        "baseline_fingerprint",
        "proposal_path",
        "proposal_fingerprint",
    }:
        raise PlanningCompilationError("manifest Structrr reference is malformed")
    if not _is_sha256(structrr["baseline_fingerprint"]) or not _is_sha256(
        structrr["proposal_fingerprint"]
    ):
        raise PlanningCompilationError("manifest Structrr digests are invalid")
    _repository_path(str(structrr["proposal_path"]), "structrr.proposal_path")
    if value["latest_repair"] is not None and not isinstance(
        value["latest_repair"], Mapping
    ):
        raise PlanningCompilationError("latest_repair must be a mapping or null")
    try:
        for record in evidence.values():
            parsed = Evidence.from_mapping(record)
            if parsed.base_commit != base["commit_sha"]:
                raise PlanningCompilationError(
                    "evidence is pinned to a different base commit"
                )
    except (EvidenceError, TypeError) as error:
        raise PlanningCompilationError(
            f"evidence record is invalid: {error}"
        ) from error
    if value["semantic_revision_digest"] != _semantic_digest(value):
        raise PlanningCompilationError(
            "semantic revision digest does not match records"
        )


def _records(
    value: Mapping[str, Any], key: str, identity: str
) -> dict[str, Mapping[str, Any]]:
    records = value.get(key)
    if not isinstance(records, list) or not all(
        isinstance(item, Mapping) for item in records
    ):
        raise PlanningCompilationError(f"manifest {key} must be a list of mappings")
    result: dict[str, Mapping[str, Any]] = {}
    for record in records:
        item_id = record.get(identity)
        if (
            not isinstance(item_id, str)
            or not _SAFE_ID.fullmatch(item_id)
            or item_id in result
        ):
            raise PlanningCompilationError(
                f"manifest {key} IDs must be unique and valid"
            )
        result[item_id] = record
    return result


def _exact(value: Mapping[str, Any], expected: set[str], field: str) -> None:
    if set(value) != expected:
        raise PlanningCompilationError(f"{field} has unknown or missing fields")


def _string_refs(
    value: Any, field: str, *, allow_empty: bool = False
) -> tuple[str, ...]:
    if not isinstance(value, list) or (not value and not allow_empty):
        raise PlanningCompilationError(f"{field} must be a non-empty string list")
    if not all(isinstance(item, str) and item.strip() for item in value):
        raise PlanningCompilationError(f"{field} must contain non-empty strings")
    if len(value) != len(set(value)):
        raise PlanningCompilationError(f"{field} contains duplicate references")
    return tuple(value)


def _repository_path(value: str, field: str) -> None:
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or ".." in path.parts
        or str(path) != value
        or "\\" in value
    ):
        raise PlanningCompilationError(
            f"{field} must be a normalized repository-relative path"
        )


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and bool(_SHA256_RE.fullmatch(value))


def _nonempty(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _positive_integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _semantic_digest(value: Mapping[str, Any]) -> str:
    records = {
        key: item
        for key, item in value.items()
        if key not in {"semantic_revision_digest", "latest_repair"}
    }
    raw = json.dumps(
        _canonical(records), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return "sha256:" + hashlib.sha256(raw.encode()).hexdigest()


def _matches_content_fingerprint(value: Mapping[str, Any]) -> bool:
    fingerprint = value.get("fingerprint")
    if not _is_sha256(fingerprint):
        return False
    unsigned = {key: item for key, item in value.items() if key != "fingerprint"}
    raw = json.dumps(
        _canonical(unsigned), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return fingerprint == "sha256:" + hashlib.sha256(raw.encode()).hexdigest()


def _canonical(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _canonical(item) for key, item in sorted(value.items())}
    if isinstance(value, tuple):
        return [_canonical(item) for item in value]
    if isinstance(value, list):
        return [_canonical(item) for item in value]
    return value


def _expected_manual_prerequisite(
    value: Mapping[str, Any] | None,
) -> Mapping[str, Any] | None:
    if value is None:
        return None
    return dict(value)


__all__ = [
    "CompiledPlan",
    "PLANNING_MANIFEST_SCHEMA",
    "PlanningCompilationError",
    "PlanningManifest",
    "ValidationReport",
    "compile_plan",
    "serialize_reviewed_plan_yaml",
]
