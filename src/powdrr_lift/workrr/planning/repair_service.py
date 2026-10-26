"""Shared preparation, compilation, and publication for reviewed plan repairs."""

from __future__ import annotations

import hashlib
import json
import subprocess
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml

from powdrr_lift.workrr.git import slugify_workflow_id
from powdrr_lift.workrr.github_issue_plan_generation import (
    parse_plan_workflow_instance_yaml,
)
from powdrr_lift.workrr.github_issue_queue import (
    PlannedWorkItem,
    ReviewedExecutionPlan,
    parse_reviewed_plan_yaml,
    render_plan_repair_metadata,
    serialize_reviewed_plan_yaml,
)
from powdrr_lift.workrr.multipr_structrr import load_multi_pr_proposal
from powdrr_lift.workrr.planning.repair import (
    PlanDiscovery,
    PlanRepairError,
    RepairDraft,
    RepairSubject,
    apply_plan_repair_patch,
    build_plan_repair_patch,
    expand_repair_impact,
    item_content_digest,
    plan_revision_digest,
)


class PlanRepairServiceError(ValueError):
    """A repair request cannot be safely prepared, compiled, or published."""


def load_repair_draft(repo_root: Path, plan_path: Path) -> RepairDraft:
    """Load the plan and all of its generated artifacts as a pinned repair draft."""
    root = repo_root.resolve()
    resolved_plan = plan_path if plan_path.is_absolute() else root / plan_path
    try:
        relative_plan = resolved_plan.resolve().relative_to(root).as_posix()
        plan_text = resolved_plan.read_text(encoding="utf-8")
        plan = parse_reviewed_plan_yaml(plan_text)
    except (OSError, ValueError) as error:
        raise PlanRepairServiceError(
            f"could not load reviewed plan: {error}"
        ) from error
    if not isinstance(plan.planning, Mapping):
        raise PlanRepairServiceError(
            "plan repair requires the planning manifest introduced by the "
            "bounded planner"
        )
    structrr = plan.planning.get("structrr")
    proposal_path = (
        structrr.get("proposal_path") if isinstance(structrr, Mapping) else None
    )
    if not isinstance(proposal_path, str):
        raise PlanRepairServiceError("plan manifest has no Structrr proposal path")
    artifacts: dict[str, str] = {relative_plan: plan_text}
    paths = [proposal_path]
    paths.extend(item.workflow_file or "" for item in plan.items)
    for relative in paths:
        if not relative or relative in artifacts:
            continue
        candidate = (root / relative).resolve()
        try:
            candidate.relative_to(root)
            artifacts[relative] = candidate.read_text(encoding="utf-8")
        except (OSError, ValueError) as error:
            raise PlanRepairServiceError(
                f"could not load plan artifact {relative!r}: {error}"
            ) from error
    try:
        proposal = load_multi_pr_proposal(root / proposal_path)
    except (OSError, ValueError) as error:
        raise PlanRepairServiceError(
            f"Structrr proposal is invalid: {error}"
        ) from error
    item_ids = {item.item_id for item in plan.items}
    if proposal.plan_id != plan.plan_id or set(proposal.item_proposals) != item_ids:
        raise PlanRepairServiceError(
            "Structrr proposal plan identity or item catalog differs from the plan"
        )
    workflow_ids = {item.item_id: item.workflow_id for item in plan.items}
    for item in plan.items:
        assert item.workflow_file is not None
        try:
            workflow = parse_plan_workflow_instance_yaml(artifacts[item.workflow_file])
        except ValueError as error:
            raise PlanRepairServiceError(
                f"workflow {item.workflow_file!r} is invalid: {error}"
            ) from error
        context = workflow.inputs.get("proposed_pr_context")
        if not isinstance(context, Mapping):
            raise PlanRepairServiceError(
                f"workflow {item.workflow_file!r} has no proposed PR context"
            )
        if workflow.workflow_id != item.workflow_id or workflow.dependencies != tuple(
            workflow_ids[dependency] for dependency in item.dependencies
        ):
            raise PlanRepairServiceError(
                f"workflow {item.workflow_file!r} disagrees on identity or dependencies"
            )
        expected_context = {
            "plan_id": plan.plan_id,
            "item_id": item.item_id,
            "title": item.title,
            "objective": item.objective,
            "scope": list(item.scope),
            "implementation_steps": list(item.implementation_steps),
            "acceptance_criteria": list(item.acceptance_criteria),
            "dependencies": list(item.dependencies),
        }
        if any(context.get(key) != value for key, value in expected_context.items()):
            raise PlanRepairServiceError(
                f"workflow {item.workflow_file!r} context disagrees with plan"
            )
    return RepairDraft(plan, dict(plan.planning), artifacts)


def apply_repair_request(
    draft: RepairDraft,
    request: Mapping[str, Any],
    *,
    base_commit: str | None = None,
    compile_candidate: Any | None = None,
) -> RepairDraft:
    """Apply one model- or user-authored semantic request through the typed engine."""
    required = {
        "discovery",
        "subjects",
        "operations",
        "replacement_items",
        "replacement_units",
        "replacement_requirements",
    }
    if set(request) != required:
        raise PlanRepairServiceError(
            "repair request must contain exactly: " + ", ".join(sorted(required))
        )
    raw_discovery = request["discovery"]
    raw_subjects = request["subjects"]
    raw_operations = request["operations"]
    replacement_items = request["replacement_items"]
    replacement_units = request["replacement_units"]
    replacement_requirements = request["replacement_requirements"]
    if not isinstance(raw_discovery, Mapping):
        raise PlanRepairServiceError("repair discovery must be an object")
    discovery_fields = {
        "kind",
        "summary",
        "requirement_ids",
        "item_ids",
        "evidence_ids",
        "requested_outcome",
    }
    if set(raw_discovery) != discovery_fields:
        raise PlanRepairServiceError(
            "repair discovery must contain exactly: "
            + ", ".join(sorted(discovery_fields))
        )
    base = draft.manifest.get("base")
    pinned_base_commit = base_commit or (
        base.get("commit_sha") if isinstance(base, Mapping) else None
    )
    if not isinstance(pinned_base_commit, str):
        raise PlanRepairServiceError("plan manifest has no pinned base commit")
    discovery_identity = hashlib.sha256(
        json.dumps(raw_discovery, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:16]
    discovery_data = {
        **dict(raw_discovery),
        "id": f"repair-{discovery_identity}",
        "base_plan_digest": plan_revision_digest(draft),
        "base_commit": pinned_base_commit,
    }
    try:
        discovery = PlanDiscovery.from_data(discovery_data)
    except PlanRepairError as error:
        raise PlanRepairServiceError(str(error)) from error
    if discovery.base_plan_digest != plan_revision_digest(draft):
        raise PlanRepairServiceError("repair discovery is bound to a stale plan digest")
    if not isinstance(raw_subjects, list) or not raw_subjects:
        raise PlanRepairServiceError("repair subjects must be a non-empty list")
    try:
        subject_refs = _bind_subjects(raw_discovery, raw_subjects)
        subjects = tuple(RepairSubject(**subject) for subject in subject_refs)
        impact = expand_repair_impact(draft, subjects)
    except (TypeError, PlanRepairError) as error:
        raise PlanRepairServiceError(str(error)) from error
    if not isinstance(raw_operations, list) or not isinstance(
        replacement_items, Mapping
    ):
        raise PlanRepairServiceError(
            "repair operations and replacement items are malformed"
        )
    if not isinstance(replacement_units, list) or not isinstance(
        replacement_requirements, Mapping
    ):
        raise PlanRepairServiceError("replacement planning records are malformed")
    original_items = {item.item_id: item for item in draft.plan.items}
    items = {
        str(item_id): _planned_item(
            item_id,
            raw,
            original_items.get(str(item_id)),
        )
        for item_id, raw in replacement_items.items()
    }
    bound_operations = _bind_operations(draft, raw_operations)
    try:
        patch = build_plan_repair_patch(
            draft,
            repair_id=discovery.id,
            discovery_ids=[discovery.id],
            affected_item_ids=impact.item_ids,
            operations=bound_operations,
            replacement_items=items,
            replacement_units=replacement_units,
            replacement_requirements=replacement_requirements,
        )
    except PlanRepairError as error:
        raise PlanRepairServiceError(str(error)) from error
    compiler = compile_candidate or (
        lambda plan, manifest: _compile_artifacts(draft, plan, manifest)
    )
    try:
        return apply_plan_repair_patch(
            draft,
            patch,
            replacement_items=items,
            replacement_units=replacement_units,
            replacement_requirements=replacement_requirements,
            compile_candidate=compiler,
        )
    except PlanRepairError as error:
        raise PlanRepairServiceError(str(error)) from error


def publish_repair_pull_request(
    *,
    repo_root: Path,
    draft: RepairDraft,
    repair: RepairDraft,
    plan_path: str,
    source_plan_path: str,
    discovery: Mapping[str, Any],
    runner: Any = subprocess.run,
) -> tuple[str, tuple[str, ...]]:
    """Publish a validated repair from a fresh remote main branch."""
    root = repo_root.resolve()
    _run(runner, root, ["git", "fetch", "origin", "main"])
    if _run(runner, root, ["git", "status", "--porcelain"]):
        raise PlanRepairServiceError(
            "plan repair publication requires a clean checkout"
        )
    head = _run(runner, root, ["git", "rev-parse", "HEAD"])
    base = _run(runner, root, ["git", "rev-parse", "origin/main"])
    if head != base:
        raise PlanRepairServiceError(
            "plan repair publication requires HEAD to match fresh origin/main"
        )
    changed = tuple(
        path
        for path, content in repair.artifacts.items()
        if draft.artifacts.get(path) != content
    )
    if not changed:
        raise PlanRepairServiceError("repair does not change any plan artifacts")
    plan = repair.plan
    repair_id = str(discovery.get("id", "repair"))
    branch = (
        f"powdrr/{slugify_workflow_id(plan.plan_id)}-repair-"
        f"{slugify_workflow_id(repair_id)}-{uuid.uuid4().hex[:8]}"
    )
    _run(runner, root, ["git", "switch", "--detach", "origin/main"])
    _run(runner, root, ["git", "switch", "-c", branch])
    for path in changed:
        destination = (root / path).resolve()
        try:
            destination.relative_to(root)
        except ValueError as error:
            raise PlanRepairServiceError(
                f"repair artifact escapes repository: {path}"
            ) from error
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(repair.artifacts[path], encoding="utf-8")
    _run(runner, root, ["git", "add", "--", *changed])
    _run(
        runner,
        root,
        [
            "git",
            "-c",
            "user.name=Powdrr Automation",
            "-c",
            "user.email=powdrr-automation@users.noreply.github.com",
            "commit",
            "--only",
            "-m",
            f"Repair reviewed plan {plan.plan_id}",
            "--",
            *changed,
        ],
    )
    _run(runner, root, ["git", "push", "--set-upstream", "origin", branch])
    summary = str(discovery.get("summary", "Correct the reviewed implementation plan."))
    title_summary = " ".join(summary.split())[:65]
    body = _repair_pr_body(
        plan_id=plan.plan_id,
        source_plan_path=source_plan_path,
        plan_path=plan_path,
        discovery=discovery,
        changed=changed,
        draft=draft,
        repaired=repair,
    )
    url = _run(
        runner,
        root,
        [
            "gh",
            "pr",
            "create",
            "--base",
            "main",
            "--head",
            branch,
            "--title",
            f"Correct plan: {title_summary}",
            "--body",
            body,
        ],
    )
    url = next(
        (line.strip() for line in reversed(url.splitlines()) if "/pull/" in line), ""
    )
    if not url:
        raise PlanRepairServiceError("gh pr create did not return a pull request URL")
    return url, changed


def _compile_artifacts(
    original: RepairDraft,
    plan: ReviewedExecutionPlan,
    manifest: Mapping[str, Any],
) -> Mapping[str, str]:
    """Render changed queue/workflow files; preserve the verified Structrr proposal."""
    if {item.item_id for item in original.plan.items} != {
        item.item_id for item in plan.items
    }:
        raise PlanRepairServiceError(
            "this compiler requires Structrr proposal data for added or retired items"
        )
    plan_path = next(
        (path for path in original.artifacts if path.endswith("-execution-plan.yaml")),
        None,
    )
    if plan_path is None:
        raise PlanRepairServiceError("repair draft has no execution plan artifact")
    rendered_plan = ReviewedExecutionPlan(
        plan.plan_id, plan.items, plan.source_document, dict(manifest)
    )
    artifacts = dict(original.artifacts)
    artifacts[plan_path] = serialize_reviewed_plan_yaml(rendered_plan)
    try:
        if parse_reviewed_plan_yaml(artifacts[plan_path]) != rendered_plan:
            raise PlanRepairServiceError("repaired execution plan did not round-trip")
    except ValueError as error:
        raise PlanRepairServiceError(
            f"repaired execution plan is invalid: {error}"
        ) from error
    workflow_ids = {item.item_id: item.workflow_id for item in plan.items}
    original_items = {item.item_id: item for item in original.plan.items}
    for item in plan.items:
        path = item.workflow_file
        if path is None or path not in original.artifacts:
            raise PlanRepairServiceError(
                f"workflow artifact missing for {item.item_id}"
            )
        previous = original_items.get(item.item_id)
        if previous is not None and item_content_digest(
            previous
        ) == item_content_digest(item):
            continue
        raw = yaml.safe_load(original.artifacts[path])
        if not isinstance(raw, Mapping):
            raise PlanRepairServiceError(f"workflow artifact is malformed: {path}")
        updated = dict(raw)
        updated["dependencies"] = [workflow_ids[dep] for dep in item.dependencies]
        raw_inputs = updated.get("inputs")
        if not isinstance(raw_inputs, Mapping):
            raise PlanRepairServiceError(f"workflow inputs are malformed: {path}")
        inputs = dict(raw_inputs)
        context = inputs.get("proposed_pr_context")
        if not isinstance(context, Mapping):
            raise PlanRepairServiceError(f"workflow context missing: {path}")
        context = dict(context)
        context.update(
            {
                "plan_id": plan.plan_id,
                "item_id": item.item_id,
                "dependencies": list(item.dependencies),
                "title": item.title,
                "objective": item.objective,
                "scope": list(item.scope),
                "implementation_steps": list(item.implementation_steps),
                "acceptance_criteria": list(item.acceptance_criteria),
            }
        )
        inputs["proposed_pr_context"] = context
        updated["inputs"] = inputs
        text = yaml.safe_dump(updated, sort_keys=False, allow_unicode=True)
        try:
            parsed = parse_plan_workflow_instance_yaml(text)
        except ValueError as error:
            raise PlanRepairServiceError(
                f"repaired workflow is invalid: {path}: {error}"
            ) from error
        expected_deps = tuple(workflow_ids[dep] for dep in item.dependencies)
        if (
            parsed.workflow_id != item.workflow_id
            or parsed.dependencies != expected_deps
        ):
            raise PlanRepairServiceError(
                f"repaired workflow disagrees with plan: {path}"
            )
        artifacts[path] = text
    return artifacts


def _planned_item(
    item_id: Any, value: Any, current: PlannedWorkItem | None
) -> PlannedWorkItem:
    if not isinstance(item_id, str) or not isinstance(value, Mapping):
        raise PlanRepairServiceError(
            "replacement item must be an object keyed by item ID"
        )
    allowed = {
        "title",
        "objective",
        "scope",
        "acceptance_criteria",
        "dependencies",
        "workflow_directory",
        "workflow_id",
        "workflow_file",
        "implementation_steps",
        "allowed_paths",
        "manual_prerequisite",
        "inline_adjustments",
    }
    if set(value) - allowed:
        raise PlanRepairServiceError(f"replacement item {item_id!r} has unknown fields")
    required = {"title", "objective", "scope", "acceptance_criteria"}
    if required - set(value):
        raise PlanRepairServiceError(
            f"replacement item {item_id!r} is missing required fields"
        )
    if current is None and not {"dependencies", "workflow_id", "workflow_file"} <= set(
        value
    ):
        raise PlanRepairServiceError(
            f"new replacement item {item_id!r} must declare its dependency and "
            "workflow identity"
        )
    replacement = PlannedWorkItem(
        item_id=item_id,
        title=str(value["title"]),
        objective=str(value["objective"]),
        scope=tuple(value["scope"]),
        acceptance_criteria=tuple(value["acceptance_criteria"]),
        dependencies=tuple(
            value.get("dependencies", current.dependencies if current else ())
        ),
        workflow_directory=value.get(
            "workflow_directory", current.workflow_directory if current else None
        ),
        workflow_id=str(
            value.get("workflow_id", current.workflow_id if current else "")
        ),
        workflow_file=value.get(
            "workflow_file", current.workflow_file if current else None
        ),
        implementation_steps=tuple(
            value.get(
                "implementation_steps", current.implementation_steps if current else ()
            )
        ),
        allowed_paths=tuple(
            value.get("allowed_paths", current.allowed_paths if current else (".",))
        ),
        manual_prerequisite=value.get(
            "manual_prerequisite", current.manual_prerequisite if current else None
        ),
        inline_adjustments=tuple(
            value.get(
                "inline_adjustments", current.inline_adjustments if current else ()
            )
        ),
    )
    if current is not None and any(
        getattr(current, field) != getattr(replacement, field)
        for field in (
            "dependencies",
            "scope",
            "acceptance_criteria",
            "workflow_id",
            "workflow_file",
            "implementation_steps",
            "allowed_paths",
            "manual_prerequisite",
            "inline_adjustments",
        )
    ):
        raise PlanRepairServiceError(
            "this repair compiler supports title, objective, and dependency "
            "changes; scope, acceptance, setup, and structural edits need "
            "a refreshed Structrr proposal"
        )
    return replacement


def _repair_pr_body(
    *,
    plan_id: str,
    source_plan_path: str,
    plan_path: str,
    discovery: Mapping[str, Any],
    changed: Sequence[str],
    draft: RepairDraft,
    repaired: RepairDraft,
) -> str:
    old_items = {item.item_id: item for item in draft.plan.items}
    new_items = {item.item_id: item for item in repaired.plan.items}
    affected = repaired.manifest.get("latest_repair", {}).get("affected_item_ids", [])
    details = []
    for item_id in affected:
        if item_id in old_items and item_id in new_items:
            details.append(
                f"- `{item_id}`: {old_items[item_id].title} → "
                f"{new_items[item_id].title}"
            )
            if old_items[item_id].dependencies != new_items[item_id].dependencies:
                details.append(
                    "  - Dependencies: "
                    f"{list(old_items[item_id].dependencies)} → "
                    f"{list(new_items[item_id].dependencies)}"
                )
            if new_items[item_id].manual_prerequisite is not None:
                details.append(
                    f"  - Human prerequisite: {new_items[item_id].manual_prerequisite}"
                )
    old_ready = _ready_items(draft.plan)
    new_ready = _ready_items(repaired.plan)
    body = "\n".join(
        [
            "## Objective",
            "",
            str(discovery.get("summary", "Correct the reviewed plan.")),
            "",
            "## Scope",
            "",
            f"Source plan: `{source_plan_path}`.",
            f"Reviewed plan: `{plan_path}`.",
            "",
            "## Implementation Details",
            "",
            f"Plan `{plan_id}` was amended after this discovery: "
            f"{discovery.get('summary', '')}",
            "",
            "Changed artifacts:",
            *[f"- `{path}`" for path in changed],
            "",
            "Applied a typed repair with compare-and-swap validation. Unrelated "
            "item identities and workflow artifacts were preserved.",
            *details,
            "",
            "## Acceptance Criteria",
            "",
            "- The repaired execution plan and affected workflows validate and agree.",
            "- Unaffected items preserve their semantic digests and workflow IDs.",
            "",
            "## Dependency and Ready Set",
            "",
            f"Ready before: {list(old_ready)}.",
            f"Ready after: {list(new_ready)}.",
            "",
            "## Validation",
            "",
            "- Typed repair validation completed before publication.",
            "",
            "## Issue Effects",
            "",
            "Published issue pointers are not changed by this PR. Reconcile them "
            "after this amendment merges.",
            "",
            "## Advisory Test-Quality Notes",
            "",
            "None.",
        ]
    )
    latest_repair = repaired.manifest.get("latest_repair")
    if not isinstance(latest_repair, Mapping):
        raise PlanRepairServiceError("repaired plan has no latest repair manifest")
    return f"{body}\n\n" + render_plan_repair_metadata(
        plan_id=plan_id,
        plan_path=plan_path,
        repair=latest_repair,
    )


def _run(runner: Any, root: Path, arguments: list[str]) -> str:
    result = runner(arguments, cwd=root, text=True, capture_output=True, check=False)
    if result.returncode:
        raise PlanRepairServiceError(
            f"command failed ({' '.join(arguments)}): {result.stderr.strip()}"
        )
    return result.stdout.strip()


__all__ = [
    "PlanRepairServiceError",
    "apply_repair_request",
    "load_repair_draft",
    "publish_repair_pull_request",
    "write_repaired_artifacts",
]


def write_repaired_artifacts(
    repo_root: Path, before: RepairDraft, after: RepairDraft
) -> tuple[str, ...]:
    """Write only the validated artifact delta, with path containment checks."""
    root = repo_root.resolve()
    changed = tuple(
        path
        for path, content in after.artifacts.items()
        if before.artifacts.get(path) != content
    )
    if not changed:
        raise PlanRepairServiceError("repair does not change any plan artifacts")
    destinations: list[tuple[Path, str]] = []
    for relative in changed:
        destination = (root / relative).resolve()
        try:
            destination.relative_to(root)
        except ValueError as error:
            raise PlanRepairServiceError(
                f"repair artifact escapes repository: {relative}"
            ) from error
        destinations.append((destination, after.artifacts[relative]))
    staged: list[tuple[Path, Path, str | None]] = []
    replaced: list[tuple[Path, str | None]] = []
    try:
        for destination, content in destinations:
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_name(
                f".{destination.name}.{uuid.uuid4().hex}.tmp"
            )
            previous = (
                destination.read_text(encoding="utf-8")
                if destination.is_file()
                else None
            )
            staged.append((temporary, destination, previous))
            temporary.write_text(content, encoding="utf-8")
        for temporary, destination, previous in staged:
            temporary.replace(destination)
            replaced.append((destination, previous))
    except OSError:
        for destination, previous in replaced:
            if previous is None:
                destination.unlink(missing_ok=True)
            else:
                destination.write_text(previous, encoding="utf-8")
        for temporary, _destination, _previous in staged:
            temporary.unlink(missing_ok=True)
        raise
    return changed


def _bind_operations(
    draft: RepairDraft, operations: Sequence[Any]
) -> tuple[Mapping[str, Any], ...]:
    """Bind compare-and-swap digests and allocations from the loaded revision."""
    by_id = {item.item_id: item for item in draft.plan.items}
    bound: list[Mapping[str, Any]] = []
    for raw in operations:
        if not isinstance(raw, Mapping):
            raise PlanRepairServiceError("repair operation must be an object")
        operation = dict(raw)
        kind = operation.get("operation")
        if {"expected_digest", "expected_edge_digest"} & set(operation):
            raise PlanRepairServiceError(
                "repair request cannot supply compare-and-swap digests"
            )
        if kind == "update_item":
            item_id = operation.get("item_id")
            item = by_id.get(item_id) if isinstance(item_id, str) else None
            if item is None:
                raise PlanRepairServiceError("update_item references an unknown item")
            operation["expected_digest"] = item_content_digest(item)
            if "replacement_unit_ids" in operation:
                raise PlanRepairServiceError(
                    "repair request cannot change item requirement allocation"
                )
            operation["replacement_unit_ids"] = draft.manifest.get(
                "item_unit_ids", {}
            ).get(item_id)
        elif kind == "retire_item":
            item_id = operation.get("item_id")
            item = by_id.get(item_id) if isinstance(item_id, str) else None
            if item is not None:
                operation["expected_digest"] = item_content_digest(item)
        elif kind == "set_dependency":
            before_id = operation.get("before_item_id")
            after_id = operation.get("after_item_id")
            before = by_id.get(before_id) if isinstance(before_id, str) else None
            after = by_id.get(after_id) if isinstance(after_id, str) else None
            if before is None or after is None:
                raise PlanRepairServiceError("dependency references an unknown item")
            from powdrr_lift.workrr.planning.repair import dependency_edge_digest

            operation["expected_edge_digest"] = dependency_edge_digest(before, after)
        elif kind == "update_requirement":
            requirement_id = operation.get("requirement_id")
            records = draft.manifest.get("requirements")
            current = (
                next(
                    (
                        record
                        for record in records
                        if record.get("id") == requirement_id
                    ),
                    None,
                )
                if isinstance(records, list)
                else None
            )
            if current is None:
                raise PlanRepairServiceError(
                    "update_requirement references an unknown requirement"
                )
            operation["expected_digest"] = _canonical_digest(current)
        elif kind == "assign_setup":
            setup_id = operation.get("setup_id")
            records = draft.manifest.get("setups")
            current = (
                next(
                    (record for record in records if record.get("id") == setup_id),
                    None,
                )
                if isinstance(records, list)
                else None
            )
            if current is None:
                raise PlanRepairServiceError("assign_setup references an unknown setup")
            operation["expected_digest"] = _canonical_digest(current)
        bound.append(operation)
    return tuple(bound)


def _bind_subjects(
    discovery: Mapping[str, Any], subjects: Sequence[Any]
) -> tuple[Mapping[str, str], ...]:
    """Bind model-authored subject reasons to IDs supplied by the discovery."""
    item_ids = discovery.get("item_ids")
    requirement_ids = discovery.get("requirement_ids")
    if not isinstance(item_ids, list) or not isinstance(requirement_ids, list):
        raise PlanRepairServiceError("discovery subject references must be lists")
    item_cursor = iter(item_ids)
    requirement_cursor = iter(requirement_ids)
    bound: list[Mapping[str, str]] = []
    for raw in subjects:
        if not isinstance(raw, Mapping) or set(raw) != {"subject_kind", "reason"}:
            raise PlanRepairServiceError(
                "repair subjects contain only subject_kind and reason; code binds IDs"
            )
        kind = raw.get("subject_kind")
        if kind == "item":
            subject_id = next(item_cursor, None)
        elif kind == "requirement":
            subject_id = next(requirement_cursor, None)
        else:
            raise PlanRepairServiceError(
                "this repair entry point accepts item and requirement subjects"
            )
        if not isinstance(subject_id, str):
            raise PlanRepairServiceError(
                f"discovery does not supply an ID for {kind!r} repair subject"
            )
        bound.append(
            {
                "subject_kind": str(kind),
                "subject_id": subject_id,
                "reason": str(raw.get("reason", "")),
            }
        )
    if sum(subject["subject_kind"] == "item" for subject in bound) != len(item_ids):
        raise PlanRepairServiceError(
            "every discovery item ID must be bound to exactly one impact subject"
        )
    if sum(subject["subject_kind"] == "requirement" for subject in bound) != len(
        requirement_ids
    ):
        raise PlanRepairServiceError(
            "every discovery requirement ID must be bound to exactly one impact subject"
        )
    return tuple(bound)


def _canonical_digest(value: Any) -> str:
    import hashlib

    return (
        "sha256:"
        + hashlib.sha256(
            json.dumps(
                value,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()
    )


def _ready_items(plan: ReviewedExecutionPlan) -> tuple[str, ...]:
    return tuple(item.item_id for item in plan.items if not item.dependencies)
