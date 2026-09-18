"""One-shot Workrr orchestration for a planned feature handoff."""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import yaml

from powdrr_lift.change_log_parser import parse_change_log
from powdrr_lift.core.execution_plan import ExecutionPlan, ExecutionUnit
from powdrr_lift.core.spec_context import (
    gather_specification_context,
    render_gather_context_report,
)
from powdrr_lift.errors import PowdrrExecutionError
from powdrr_lift.file_management import manage_worktree_file
from powdrr_lift.structrr.bootstrap import bootstrap_structrr
from powdrr_lift.workrr.coding_agent import (
    CodingAgentAttempt,
    CodingAgentAttemptStore,
    CodingAgentRunner,
    CodingAgentStatus,
    ImplementationRequest,
    OpenCodePermissionPolicy,
    OpenCodeProvider,
)
from powdrr_lift.workrr.coding_agent_validation import (
    ValidationProfile,
    ValidationReport,
    ValidationReportStatus,
    ValidationRunner,
)
from powdrr_lift.workrr.git import integration_branch_name
from powdrr_lift.workrr.procedrr import WorkrrProcedrrClient
from powdrr_lift.workrr.protocol import WorkflowLLMClient
from procedrr import parse_and_validate
from procedrr_evaluator import Evaluator
from procedrr_evaluator.evaluator import ValidationGateError

Runner = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True, slots=True)
class FeatureEndpointConfig:
    feature_description: str
    work_item_name: str
    repo_root: Path
    allowed_paths: tuple[str, ...]
    validation_command: tuple[str, ...]
    base_branch: str = "main"
    opencode_executable: str = "opencode"
    opencode_model: str = "deepinfra/deepseek-ai/DeepSeek-V4-Flash-0731"
    output_root: Path | None = None
    open_pr: bool = True
    planning_client: WorkflowLLMClient | None = None


@dataclass(frozen=True, slots=True)
class FeatureEndpointResult:
    status: str
    branch: str
    worktree: Path
    baseline_path: Path
    plan_path: Path
    request_path: Path
    attempt: CodingAgentAttempt | None
    validation: ValidationReport | None
    review: dict[str, Any]
    pull_request_url: str | None = None
    changelog_path: Path | None = None

    def to_data(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "branch": self.branch,
            "worktree": str(self.worktree),
            "baseline_path": str(self.baseline_path),
            "plan_path": str(self.plan_path),
            "request_path": str(self.request_path),
            "attempt": self.attempt.to_data() if self.attempt else None,
            "validation": self.validation.to_data() if self.validation else None,
            "review": self.review,
            "pull_request_url": self.pull_request_url,
            "changelog_path": str(self.changelog_path) if self.changelog_path else None,
        }


def run_feature_endpoint(
    config: FeatureEndpointConfig,
    *,
    runner: Runner = subprocess.run,
) -> FeatureEndpointResult:
    """Plan, implement, review, and optionally open one feature PR."""
    if not config.feature_description.strip():
        raise ValueError("feature_description must not be empty")
    if not config.allowed_paths:
        raise ValueError("at least one allowed path is required")
    root = config.repo_root.resolve()
    slug = _snake_case_work_item_name(config.work_item_name)
    branch = integration_branch_name(config.work_item_name)
    worktree = root / ".worktrees" / "powdrr" / slug
    output_root = (
        config.output_root or root / ".powdrr" / "feature-runs" / slug
    ).resolve()
    output_root.mkdir(parents=True, exist_ok=True)

    _require_clean_root(root, runner)
    _run(runner, root, ["git", "fetch", "origin", config.base_branch])
    _run(
        runner,
        root,
        [
            "git",
            "worktree",
            "add",
            "-b",
            branch,
            str(worktree),
            f"origin/{config.base_branch}",
        ],
    )
    try:
        return _execute_procedrr_flow(
            config,
            runner=runner,
            worktree=worktree,
            output_root=output_root,
            branch=branch,
        )
    except Exception:
        raise


def _execute_procedrr_flow(
    config: FeatureEndpointConfig,
    *,
    runner: Runner,
    worktree: Path,
    output_root: Path,
    branch: str,
) -> FeatureEndpointResult:
    slug = _snake_case_work_item_name(config.work_item_name)
    state: dict[str, Any] = {}
    flow_path = _validate_procedrr_flow(worktree)
    flow = parse_and_validate(flow_path.read_text(encoding="utf-8"))

    def execute(tool: str, parameters: Mapping[str, Any]) -> Any:
        if tool == "gather_context":
            types = parameters.get("types")
            if not isinstance(types, list) or not all(
                isinstance(item, str) for item in types
            ):
                raise PowdrrExecutionError("gather_context types are malformed")
            report = gather_specification_context(worktree, types=types)
            return json.loads(render_gather_context_report(report))
        if tool == "edit":
            file_path = parameters.get("file_path")
            document = parameters.get("document")
            if not isinstance(file_path, str) or not isinstance(document, Mapping):
                raise PowdrrExecutionError("design interview edit is malformed")
            target = _resolve_flow_path(worktree, file_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
            return dict(document)
        if tool == "yaml_edit":
            return _apply_flow_yaml_edit(worktree, parameters)
        if tool == "file_management":
            operation = parameters.get("operation")
            file_path = parameters.get("file_path")
            destination_path = parameters.get("destination_path")
            if not isinstance(operation, str) or not isinstance(file_path, str):
                raise PowdrrExecutionError(
                    "file_management requires operation and file_path"
                )
            if destination_path is not None and not isinstance(destination_path, str):
                raise PowdrrExecutionError(
                    "file_management destination_path must be a string"
                )
            return manage_worktree_file(
                worktree,
                operation=operation,
                file_path=file_path,
                destination_path=destination_path,
            )
        if tool != "internal":
            raise PowdrrExecutionError(
                f"feature flow requested unsupported tool {tool!r}"
            )
        command = parameters.get("command")
        if not isinstance(command, list) or not command:
            raise PowdrrExecutionError("feature flow operation command is malformed")
        name = command[0]
        if name == "ensure_current_structrr":
            state["baseline_path"] = _ensure_current_baseline(worktree, runner)
            return {"path": str(state["baseline_path"])}
        if name == "build_interview_input":
            document = parameters.get("document")
            if not isinstance(document, Mapping):
                raise PowdrrExecutionError(
                    "build_interview_input requires a document mapping"
                )
            state["interview_input"] = dict(document)
            file_path = parameters.get("file_path")
            if isinstance(file_path, str):
                target = _resolve_flow_path(worktree, file_path)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(
                    json.dumps(document, indent=2) + "\n", encoding="utf-8"
                )
            return dict(document)
        if command[:2] == ["powdrr-lift", "feature-pr-specification"]:
            interview_path = _command_option(command, "--interview-input")
            interview_document = state.get("interview_input")
            if not isinstance(interview_document, Mapping):
                raise PowdrrExecutionError(
                    "feature specification did not receive interview input"
                )
            temporary_interview_path = _resolve_flow_path(worktree, interview_path)
            temporary_interview_path.parent.mkdir(parents=True, exist_ok=True)
            temporary_interview_path.write_text(
                json.dumps(interview_document, indent=2) + "\n", encoding="utf-8"
            )
            _run(runner, worktree, command)
            work_item_name = _command_option(command, "--work-item-name")
            return {
                "path": str(
                    Path("docs")
                    / "proposals"
                    / work_item_name
                    / "feature-pr-specification.yaml"
                )
            }
        if command[:2] == ["powdrr-lift", "evaluate"]:
            result = _run(runner, worktree, command)
            return {
                "returncode": result.returncode,
                "issues": [],
                "stdout": result.stdout,
                "stderr": result.stderr,
            }
        if name == "extract_proposal_issues":
            evaluation = parameters.get("evaluation")
            return (
                list(evaluation.get("issues", []))
                if isinstance(evaluation, Mapping)
                else []
            )
        if name == "aggregate_category_edits":
            decisions = parameters.get("decisions")
            if not isinstance(decisions, Mapping):
                raise PowdrrExecutionError(
                    "aggregate_category_edits requires category decisions"
                )
            return _aggregate_category_edits(decisions)
        if len(command) != 1:
            raise PowdrrExecutionError("feature flow operation command is malformed")
        if name == "plan_structrr_diff":
            baseline = _require_flow_text(parameters, "baseline")
            if baseline != str(state["baseline_path"]):
                raise PowdrrExecutionError(
                    "planning baseline does not match ensured baseline"
                )
            plan_config = replace(
                config,
                work_item_name=_require_flow_text(parameters, "work_item_name"),
                feature_description=_require_flow_text(
                    parameters, "feature_description"
                ),
            )
            state["plan_path"] = _write_structrr_plan(
                worktree,
                plan_config,
                interview_input=parameters.get("interview_input"),
            )
            return {"path": str(state["plan_path"])}
        if name == "commit_design_artifacts":
            _commit(runner, worktree, "Record Structrr feature design")
            return {"committed": True}
        if name == "run_opencode":
            return _run_opencode_phase(
                config,
                runner=runner,
                worktree=worktree,
                output_root=output_root,
                branch=branch,
                slug=slug,
                state=state,
                parameters=parameters,
            )
        if name == "validate_implementation":
            if not isinstance(parameters.get("implementation"), Mapping):
                raise PowdrrExecutionError(
                    "validation did not receive implementation state"
                )
            return _validate_implementation_phase(
                config, worktree=worktree, state=state
            )
        if name == "review_worker_diff":
            if not isinstance(parameters.get("implementation"), Mapping):
                raise PowdrrExecutionError(
                    "review did not receive implementation state"
                )
            if not isinstance(parameters.get("validation"), Mapping):
                raise PowdrrExecutionError("review did not receive validation state")
            review = review_feature_diff(
                worktree,
                state["request"],
                state["attempt"],
                validation=state["validation"],
                runner=runner,
            )
            state["review"] = review
            return review
        if name == "open_pull_request":
            review_value = parameters.get("review")
            if (
                not isinstance(review_value, Mapping)
                or review_value.get("passed") is not True
            ):
                raise PowdrrExecutionError("cannot open a PR before a passing review")
            work_item_name = _require_flow_text(parameters, "work_item_name")
            if Path(_require_flow_text(parameters, "plan")) != state["plan_path"]:
                raise PowdrrExecutionError(
                    "PR input plan does not match the planned diff"
                )
            feature_config = replace(
                config,
                work_item_name=work_item_name,
                feature_description=_require_flow_text(
                    parameters, "feature_description"
                ),
            )
            _commit(runner, worktree, f"Implement {work_item_name}")
            _run(runner, worktree, ["git", "push", "--set-upstream", "origin", branch])
            if not config.open_pr:
                return None
            state["pull_request_url"] = _open_pull_request(
                runner, worktree, feature_config, branch, state["plan_path"]
            )
            return state["pull_request_url"]
        if name == "create_pr_changelog":
            pull_request = _require_flow_text(parameters, "pull_request")
            if not pull_request:
                return None
            if Path(_require_flow_text(parameters, "plan")) != state["plan_path"]:
                raise PowdrrExecutionError(
                    "changelog input plan does not match the planned diff"
                )
            feature_config = replace(
                config,
                work_item_name=_require_flow_text(parameters, "work_item_name"),
                feature_description=_require_flow_text(
                    parameters, "feature_description"
                ),
            )
            state["changelog_path"] = _create_pr_changelog(
                runner,
                worktree,
                branch,
                pull_request,
                feature_config,
                state["plan_path"],
            )
            state["plan_path"] = state["changelog_path"]
            return str(state["changelog_path"])
        if name == "update_pull_request":
            pull_request_value = parameters.get("pull_request")
            changelog = parameters.get("changelog")
            if isinstance(pull_request_value, str) and isinstance(changelog, str):
                _update_pull_request_description(
                    runner,
                    worktree,
                    pull_request_value,
                    config,
                    Path(changelog).relative_to(worktree),
                )
            return pull_request_value
        raise PowdrrExecutionError(f"feature flow requested unknown operation {name!r}")

    if config.planning_client is None:
        raise PowdrrExecutionError(
            "implement-feature requires a planning_client for its "
            "design-interview subprocess"
        )
    try:
        evaluator = Evaluator(
            WorkrrProcedrrClient(
                config.planning_client,
                skills_dir=worktree / "docs" / "procedrr" / "skill-definitions",
            ),
            execute,
            process_directory=worktree / "docs" / "procedrr" / "skill-definitions",
        )
        evaluator.evaluate(
            flow,
            {
                "feature_description": config.feature_description,
                "work_item_name": config.work_item_name,
                "work_item_slug": slug,
            },
        )
    except ValidationGateError:
        return _feature_endpoint_result(state, branch, worktree, "review_failed")
    return _feature_endpoint_result(
        state,
        branch,
        worktree,
        "pr_opened" if state.get("pull_request_url") else "completed",
    )


def _run_opencode_phase(
    config: FeatureEndpointConfig,
    *,
    runner: Runner,
    worktree: Path,
    output_root: Path,
    branch: str,
    slug: str,
    state: dict[str, Any],
    parameters: Mapping[str, Any],
) -> dict[str, Any]:
    del branch
    baseline_path = Path(_require_flow_text(parameters, "baseline"))
    plan_path = Path(_require_flow_text(parameters, "plan"))
    if baseline_path != state["baseline_path"] or plan_path != state["plan_path"]:
        raise PowdrrExecutionError("implementation inputs do not match the plan state")
    feature_description = _require_flow_text(parameters, "feature_description")
    _require_flow_text(parameters, "work_item_name")
    planned_additions, planned_deletions, acceptance_criteria = (
        _load_implementation_plan(plan_path, feature_description)
    )
    procedrr_path = (
        worktree / "docs" / "procedrr" / "skill-definitions" / "implement-feature.yaml"
    )
    base_commit = _git_output(runner, worktree, ["git", "rev-parse", "HEAD"])
    plan = ExecutionPlan(
        plan_id=f"{slug}-execution",
        proposed_pr_fingerprint=f"{slug}-v1",
        units=(
            ExecutionUnit(
                unit_id=f"implement-{slug}",
                objective=feature_description,
                paths=config.allowed_paths,
                validation_profiles=("feature-validation",),
                acceptance_criteria=acceptance_criteria,
                planned_additions=planned_additions,
                planned_deletions=planned_deletions,
            ),
        ),
        allowed_paths=config.allowed_paths,
    )
    request = ImplementationRequest.from_execution_plan(
        plan,
        unit_id=f"implement-{slug}",
        request_id=f"{slug}-implementation",
        base_commit=base_commit,
        context_refs=(
            f"structrr:{baseline_path.relative_to(worktree)}",
            f"structrr-diff:{plan_path.relative_to(worktree)}",
            f"procedrr:{procedrr_path.relative_to(worktree)}",
        ),
        allowed_commands=(" ".join(config.validation_command) + " *",),
    )
    request_path = output_root / "implementation-request.json"
    request_path.write_text(request.to_json(), encoding="utf-8")
    attempt_store = CodingAgentAttemptStore(output_root / "artifacts")
    attempt = CodingAgentRunner(
        provider=OpenCodeProvider(
            executable=config.opencode_executable,
            model=config.opencode_model,
            permission_policy=OpenCodePermissionPolicy(
                (" ".join(config.validation_command) + " *",)
            ),
        ),
        store=attempt_store,
    ).run(request, worktree_root=worktree, attempt_id=f"{slug}-attempt")
    state.update(
        request=request,
        request_path=request_path,
        attempt=attempt,
        attempt_store=attempt_store,
    )
    return {"request_id": request.request_id, "attempt": attempt.to_data()}


def _load_implementation_plan(
    path: Path, feature_description: str
) -> tuple[tuple[dict[str, Any], ...], tuple[dict[str, Any], ...], tuple[str, ...]]:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise PowdrrExecutionError(
            f"could not read Structrr plan {path}: {error}"
        ) from error
    if not isinstance(raw, Mapping):
        raise PowdrrExecutionError(f"Structrr plan {path} must contain a mapping")

    additions: list[dict[str, Any]] = []
    deletions: list[dict[str, Any]] = []
    for section, value in raw.items():
        if not isinstance(section, str) or not isinstance(value, list):
            continue
        for item in value:
            if not isinstance(item, Mapping):
                continue
            action = item.get("action")
            if action not in {"added", "deleted", "removed"}:
                continue
            planned = {"section": section, **dict(item)}
            if action == "added":
                additions.append(planned)
            else:
                deletions.append(planned)

    criteria: list[str] = []
    raw_criteria = raw.get("acceptance_criteria")
    if isinstance(raw_criteria, list):
        for item in raw_criteria:
            if isinstance(item, str) and item.strip():
                criteria.append(item.strip())
            elif isinstance(item, Mapping):
                description = item.get("description", item.get("text"))
                if isinstance(description, str) and description.strip():
                    criteria.append(description.strip())
    if not criteria:
        criteria.append(
            f"The requested feature behavior is implemented: {feature_description}"
        )
    criteria.append("Only the declared implementation paths are changed.")
    return tuple(additions), tuple(deletions), tuple(dict.fromkeys(criteria))


def _validate_implementation_phase(
    config: FeatureEndpointConfig, *, worktree: Path, state: dict[str, Any]
) -> dict[str, Any]:
    validation = ValidationRunner(
        {
            "feature-validation": ValidationProfile(
                "feature-validation", config.validation_command
            )
        }
    ).run(state["request"], state["attempt"], worktree_root=worktree)
    state["attempt_store"].save_validation_report(validation)
    state["validation"] = validation
    return validation.to_data()


def _require_flow_text(parameters: Mapping[str, Any], name: str) -> str:
    value = parameters.get(name)
    if not isinstance(value, str) or not value.strip():
        raise PowdrrExecutionError(
            f"feature flow parameter {name!r} must be a non-empty string"
        )
    return value


def _command_option(command: list[Any], option: str) -> str:
    try:
        index = command.index(option)
        value = command[index + 1]
    except (ValueError, IndexError) as error:
        raise PowdrrExecutionError(f"flow command is missing {option}") from error
    if not isinstance(value, str) or not value.strip():
        raise PowdrrExecutionError(f"flow command option {option} is malformed")
    return value


def _snake_case_work_item_name(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", value.strip().casefold()).strip("_")
    if not slug:
        raise ValueError("The work item name must contain a letter or digit.")
    return slug


def _feature_endpoint_result(
    state: dict[str, Any], branch: str, worktree: Path, status: str
) -> FeatureEndpointResult:
    return FeatureEndpointResult(
        status,
        branch,
        worktree,
        state["baseline_path"],
        state["plan_path"],
        state["request_path"],
        state.get("attempt"),
        state.get("validation"),
        state.get("review", {"passed": False}),
        state.get("pull_request_url"),
        state.get("changelog_path"),
    )


def review_feature_diff(
    worktree: Path,
    request: ImplementationRequest,
    attempt: CodingAgentAttempt,
    validation: ValidationReport,
    *,
    runner: Runner = subprocess.run,
) -> dict[str, Any]:
    """Review the worker result before any commit or PR operation."""
    changed = tuple(
        _git_output(runner, worktree, ["git", "diff", "--name-only"]).splitlines()
    )
    allowed = set(request.allowed_paths)
    out_of_scope = tuple(path for path in changed if path not in allowed)
    diff_check = _run(runner, worktree, ["git", "diff", "--check"])
    passed = (
        attempt.status is CodingAgentStatus.COMPLETED
        and validation.status is ValidationReportStatus.PASSED
        and bool(changed)
        and not out_of_scope
        and diff_check.returncode == 0
    )
    return {
        "passed": passed,
        "changed_paths": list(changed),
        "out_of_scope_paths": list(out_of_scope),
        "diff_check_returncode": diff_check.returncode,
        "worker_status": attempt.status.value,
        "validation_status": validation.status.value,
        "validation_error": validation.error,
    }


def _write_structrr_plan(
    worktree: Path,
    config: FeatureEndpointConfig,
    *,
    interview_input: Any,
) -> Path:
    slug = _snake_case_work_item_name(config.work_item_name)
    proposal = worktree / "docs" / "current" / slug
    proposal.mkdir(parents=True, exist_ok=True)
    path = proposal / "structrr-diff.yaml"
    interview = dict(interview_input) if isinstance(interview_input, Mapping) else {}
    sections = {
        key: _interview_edits(interview.get(f"{key}_edits"))
        for key in (
            "requirements",
            "approach",
            "entities",
            "entity_relationships",
            "invariants",
            "guidance",
            "features",
            "human_decisions",
            "intent",
            "intents",
            "acceptance_criteria",
            "expected_tests",
            "required_test_cases",
            "expected_outcomes",
            "non_goals",
            "risks",
            "decisions",
            "proposed_prs",
            "modules",
            "tools",
        )
    }
    document = {
        "schema": "https://powdrr.io/schema/changelog-v2",
        "change_id": slug,
        "title": config.work_item_name,
        "intent": {
            "problem": "The requested product behavior is not yet available.",
            "goal": config.feature_description,
        },
        "human-decisions": sections["human_decisions"],
        "files": [],
        "entities": sections["entities"]
        or [{"id": slug, "type": "Feature", "action": "added"}],
        "entity_relationships": sections["entity_relationships"],
        "features": sections["features"]
        or [
            {
                "id": slug,
                "description": config.feature_description,
                "action": "added",
            }
        ],
        "invariants": sections["invariants"],
        "guidance": sections["guidance"],
        "acceptance_criteria": _plan_text_items(
            sections["acceptance_criteria"],
            fallback=(
                f"The requested feature behavior is implemented: "
                f"{config.feature_description}"
            ),
            prefix=slug,
        ),
        "proposed_prs": sections["proposed_prs"],
    }
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    parse_change_log(path.read_text(encoding="utf-8"))
    return path


def _interview_edits(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, Mapping):
        return []
    edits: list[dict[str, Any]] = []
    for action, items in (
        ("added", value.get("added")),
        ("deleted", value.get("deleted")),
    ):
        if not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, Mapping):
                edits.append({**item, "action": action})
    return edits


def _plan_text_items(
    items: Sequence[Mapping[str, Any]], *, fallback: str, prefix: str
) -> list[dict[str, str]]:
    values: list[dict[str, str]] = []
    for index, item in enumerate(items, start=1):
        description = item.get("description")
        if isinstance(description, str) and description.strip():
            identifier = item.get("id")
            values.append(
                {
                    "id": (
                        identifier.strip()
                        if isinstance(identifier, str) and identifier.strip()
                        else f"{prefix}-acceptance-{index}"
                    ),
                    "description": description.strip(),
                }
            )
            continue
        if isinstance(item.get("text"), str) and item["text"].strip():
            values.append(
                {
                    "id": f"{prefix}-acceptance-{index}",
                    "description": item["text"].strip(),
                }
            )
    return values or [{"id": f"{prefix}-acceptance-1", "description": fallback}]


def _aggregate_category_edits(decisions: Mapping[str, Any]) -> dict[str, Any]:
    aggregated: dict[str, Any] = {}
    for category, decision in decisions.items():
        added: list[dict[str, Any]] = []
        deleted: list[dict[str, Any]] = []
        if isinstance(decision, Mapping):
            action = decision.get("action")
            item = decision.get("item")
            if action == "add" and isinstance(item, Mapping):
                added.append(dict(item))
            elif action == "delete" and isinstance(item, Mapping):
                deleted.append(dict(item))
        aggregated[str(category)] = {"added": added, "deleted": deleted}
    return aggregated


def _resolve_flow_path(worktree: Path, relative_path: str) -> Path:
    target = (worktree / relative_path).resolve()
    if target != worktree.resolve() and worktree.resolve() not in target.parents:
        raise PowdrrExecutionError(f"flow path escapes worktree: {relative_path}")
    return target


def _apply_flow_yaml_edit(
    worktree: Path, parameters: Mapping[str, Any]
) -> dict[str, Any]:
    edit = parameters.get("edit")
    if not isinstance(edit, Mapping):
        raise PowdrrExecutionError("yaml_edit requires an edit mapping")
    file_path = edit.get("path", parameters.get("file_path"))
    edits = edit.get("edits")
    if not isinstance(file_path, str) or not isinstance(edits, list):
        raise PowdrrExecutionError("yaml_edit requires path and edits")
    target = _resolve_flow_path(worktree, file_path)
    document = yaml.safe_load(target.read_text(encoding="utf-8"))
    if not isinstance(document, Mapping):
        raise PowdrrExecutionError("yaml_edit target must contain a mapping")
    updated = dict(document)
    for item in edits:
        if not isinstance(item, Mapping):
            raise PowdrrExecutionError("yaml_edit entries must be mappings")
        path = item.get("path")
        if (
            not isinstance(path, list)
            or not path
            or not all(isinstance(part, str) for part in path)
        ):
            raise PowdrrExecutionError("yaml_edit entries require a path list")
        cursor: Any = updated
        for part in path[:-1]:
            if not isinstance(cursor, Mapping) or part not in cursor:
                raise PowdrrExecutionError(f"yaml_edit path does not exist: {path}")
            cursor = cursor[part]
        if not isinstance(cursor, dict):
            raise PowdrrExecutionError(f"yaml_edit parent is not a mapping: {path}")
        cursor[path[-1]] = item.get("value")
    target.write_text(yaml.safe_dump(updated, sort_keys=False), encoding="utf-8")
    return {"path": str(target), "edits": len(edits)}


def _validate_procedrr_flow(worktree: Path) -> Path:
    path = (
        worktree / "docs" / "procedrr" / "skill-definitions" / "implement-feature.yaml"
    )
    try:
        parse_and_validate(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise PowdrrExecutionError(
            f"Shared feature Procedrr definition is invalid: {path}: {error}"
        ) from error
    return path


def _ensure_current_baseline(worktree: Path, runner: Runner) -> Path:
    """Reuse the current baseline or bootstrap it once before planning."""
    relative_paths = _git_output(
        runner,
        worktree,
        ["git", "ls-files", "docs/structrr/current/baseline-*.yaml"],
    ).splitlines()
    if not relative_paths:
        baseline = bootstrap_structrr(worktree)
        if not baseline.validation.successful:
            raise PowdrrExecutionError("Structrr bootstrap validation failed.")
        _commit(runner, worktree, "Bootstrap Structrr baseline")
        return baseline.output_path
    ranked: list[tuple[int, str]] = []
    for relative_path in relative_paths:
        timestamp = _git_output(
            runner,
            worktree,
            ["git", "log", "-1", "--format=%ct", "--", relative_path],
        )
        ranked.append((int(timestamp or "0"), relative_path))
    _, selected = max(ranked)
    return worktree / selected


def _require_clean_root(root: Path, runner: Runner) -> None:
    result = _run(runner, root, ["git", "status", "--porcelain"])
    if result.stdout.strip():
        raise PowdrrExecutionError("Feature endpoint requires a clean caller worktree.")


def _commit(runner: Runner, worktree: Path, message: str) -> None:
    _run(runner, worktree, ["git", "add", "docs"])
    _run(runner, worktree, ["git", "commit", "-am", message])


def _create_pr_changelog(
    runner: Runner,
    worktree: Path,
    branch: str,
    pull_request_url: str,
    config: FeatureEndpointConfig,
    plan_path: Path,
) -> Path:
    match = re.search(r"/pull/(\d+)", pull_request_url)
    if match is None:
        raise PowdrrExecutionError(
            f"Could not determine the pull request number from {pull_request_url!r}."
        )
    pr_number = match.group(1)
    path = worktree / "docs" / "changelogs" / f"PR-{pr_number}-changelog.yaml"
    changed_paths = _git_output(
        runner,
        worktree,
        ["git", "diff", "--name-only", f"origin/{config.base_branch}...HEAD"],
    ).splitlines()
    try:
        descriptor = yaml.safe_load(plan_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise PowdrrExecutionError(
            f"could not read provisional Structrr plan {plan_path}: {error}"
        ) from error
    if not isinstance(descriptor, dict):
        raise PowdrrExecutionError("provisional Structrr plan must contain a mapping")
    descriptor["schema"] = "https://powdrr.io/schema/changelog-v2"
    descriptor["change_id"] = f"PR-{pr_number}"
    descriptor["title"] = config.work_item_name
    descriptor["intent"] = {
        "problem": "The requested product behavior is not yet available.",
        "goal": config.feature_description,
    }
    descriptor["files"] = [
        {
            "path": changed_path,
            "type": _changelog_file_type(changed_path),
            "span": {"start_line": 1, "end_line": 1},
            "summary": f"Changed as part of {config.work_item_name}.",
            "rationale": config.feature_description,
        }
        for changed_path in changed_paths
        if changed_path != str(plan_path.relative_to(worktree))
    ]
    descriptor.setdefault("human-decisions", [])
    descriptor.setdefault("entities", [])
    descriptor.setdefault("entity_relationships", [])
    descriptor.setdefault("invariants", [])
    descriptor.setdefault("guidance", [])
    descriptor.setdefault("features", [])
    descriptor.setdefault("proposed_prs", [])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(descriptor, sort_keys=False), encoding="utf-8")
    parse_change_log(path.read_text(encoding="utf-8"))
    plan_path.unlink()
    _commit(runner, worktree, f"Add PR-{pr_number} changelog descriptor")
    _run(runner, worktree, ["git", "push", "origin", branch])
    return path


def _changelog_file_type(path: str) -> str:
    if path.startswith("tests/"):
        return "Test file"
    if path.endswith((".yaml", ".yml", ".json", ".toml")):
        return "Configuration file"
    if path.startswith("docs/"):
        return "Documentation"
    return "Source file"


def _update_pull_request_description(
    runner: Runner,
    worktree: Path,
    pull_request_url: str,
    config: FeatureEndpointConfig,
    changelog_relative_path: Path,
) -> None:
    body = (
        f"## Feature\n\n{config.feature_description}\n\n"
        f"Structrr change and PR changelog: `{changelog_relative_path}`\n"
        "Current feature specification: "
        f"`docs/current/{_snake_case_work_item_name(config.work_item_name)}"
        "/feature-pr-specification.yaml`\n"
        "Implemented by the bounded Workrr/OpenCode handoff and reviewed before commit."
    )
    _run(
        runner,
        worktree,
        ["gh", "pr", "edit", pull_request_url, "--body", body],
    )


def _open_pull_request(
    runner: Runner,
    worktree: Path,
    config: FeatureEndpointConfig,
    branch: str,
    plan_path: Path,
) -> str:
    body = (
        f"## Feature\n\n{config.feature_description}\n\n"
        f"Structrr plan: `{plan_path.relative_to(worktree)}`\n"
        "Implemented by the bounded Workrr/OpenCode handoff and reviewed before commit."
    )
    result = _run(
        runner,
        worktree,
        [
            "gh",
            "pr",
            "create",
            "--base",
            config.base_branch,
            "--head",
            branch,
            "--title",
            config.work_item_name,
            "--body",
            body,
        ],
    )
    return result.stdout.strip().splitlines()[-1]


def _run(
    runner: Runner, cwd: Path, command: list[str]
) -> subprocess.CompletedProcess[str]:
    result = runner(command, cwd=cwd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise PowdrrExecutionError(
            f"Command failed ({result.returncode}): {' '.join(command)}\n"
            f"{result.stderr.strip()}"
        )
    return result


def _git_output(runner: Runner, cwd: Path, command: list[str]) -> str:
    return _run(runner, cwd, command).stdout.strip()


__all__ = [
    "FeatureEndpointConfig",
    "FeatureEndpointResult",
    "review_feature_diff",
    "run_feature_endpoint",
]
