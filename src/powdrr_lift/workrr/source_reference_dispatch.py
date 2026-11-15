"""Dispatch reviewed source-reference tasks without entering legacy recovery."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any

import yaml
from powdrr_agent.execution.execute_pr_tests import (
    fingerprint_test_files,
    pytest_command_for_files,
)
from powdrr_local.miniswe_inference import create_miniswe_inference_handler
from powdrr_service.decision_obligation import content_fingerprint
from powdrr_service.planning.plan_models import source_reference_design_file
from powdrr_service.structrr.execution_preparation import (
    parse_command_spec,
    validate_execution_preparation,
)
from powdrr_service.structrr.source_run_lineage import (
    audit_source_run_lineage,
    bind_source_run_patch,
    build_source_run_lineage,
)

from powdrr_lift.errors import PowdrrExecutionError
from powdrr_lift.workrr.coding_agent import (
    DEFAULT_CODING_AGENT_RECOVERY_ATTEMPTS,
    _remove_provider_credentials,
)
from powdrr_lift.workrr.coding_agent_validation import ValidationProfile
from powdrr_lift.workrr.llm import WorkflowLLMClient, complete_json
from powdrr_lift.workrr.source_reference_postrun import reconcile_source_task
from powdrr_lift.workrr.source_reference_run import SourceReferenceRun
from powdrr_lift.workrr.story_execution_operations import execute_story_validation

DESIGN_SCHEMA = "structrr-source-reference-task-v1"
MAX_DESIGN_BYTES = 1_000_000
CORRECTION_SCHEMA = "source-reference-correction-coordinator-v1"


def _write_coordinator(root: Path, value: Mapping[str, Any]) -> None:
    path = root / "coordinator.json"
    temporary = root / "coordinator.json.tmp"
    temporary.write_text(
        json.dumps(dict(value), indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _capture_candidate_files(
    candidate: Path, paths: Sequence[str], *, excluded: set[str]
) -> dict[str, str | None]:
    captured: dict[str, str | None] = {}
    for relative in sorted(set(paths) - excluded):
        parsed = PurePosixPath(relative)
        if (
            "\\" in relative
            or parsed.is_absolute()
            or not parsed.parts
            or any(part in {"", ".", ".."} for part in parsed.parts)
        ):
            raise ValueError("Correction candidate contains an invalid path")
        target = candidate.joinpath(*parsed.parts)
        if any(
            parent.is_symlink()
            for parent in (target, *target.parents)
            if parent != candidate.parent and parent.is_relative_to(candidate)
        ):
            raise ValueError("Correction candidate contains a symlink")
        if target.is_file():
            content = target.read_text(encoding="utf-8")
            if len(content.encode("utf-8")) > MAX_DESIGN_BYTES:
                raise ValueError("Correction candidate file exceeds the size limit")
            captured[relative] = content
        else:
            captured[relative] = None
    return captured


def _correction_failure_summary(data: Mapping[str, Any]) -> dict[str, Any]:
    validation = data.get("validation")
    failures: list[dict[str, Any]] = []
    if isinstance(validation, Mapping) and isinstance(validation.get("results"), list):
        for result in validation["results"]:
            if not isinstance(result, Mapping) or result.get("status") == "passed":
                continue
            failures.append(
                {
                    "profile": result.get("profile"),
                    "status": result.get("status"),
                    "returncode": result.get("returncode"),
                    "test_id": result.get("test_id"),
                    "classification": (
                        "environment_failure"
                        if result.get("status") in {"blocked", "pending", "timed_out"}
                        else "test_failure"
                    ),
                }
            )
    diff_check = data.get("diff_check")
    return {
        "outcome": data.get("outcome"),
        "error": str(data.get("error") or "")[:500],
        "failed_checks": failures,
        "diff_check": (
            {
                "passed": diff_check.get("passed"),
                "returncode": diff_check.get("returncode"),
            }
            if isinstance(diff_check, Mapping)
            else None
        ),
    }


def _git(root: Path, *arguments: str, data: bytes | None = None) -> bytes:
    result = subprocess.run(
        ["git", *arguments], cwd=root, input=data, capture_output=True, check=False
    )
    if result.returncode:
        raise PowdrrExecutionError(result.stderr.decode(errors="replace").strip())
    return result.stdout


def _is_base_ancestor_of_checkout(
    repo_root: Path, *, base_commit: str, checkout_commit: str
) -> bool:
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", base_commit, checkout_commit],
        cwd=repo_root,
        capture_output=True,
        check=False,
    )
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    raise PowdrrExecutionError(result.stderr.decode(errors="replace").strip())


def load_reviewed_source_design(
    repo_root: Path, proposed: Mapping[str, Any], *, base_commit: str
) -> tuple[str, dict[str, Any]]:
    """Read accepted intent from a regular committed blob, never candidate files."""
    path, design, _ = load_reviewed_source_design_contract(
        repo_root, proposed, base_commit=base_commit
    )
    return path, design


def load_reviewed_source_design_contract(
    repo_root: Path, proposed: Mapping[str, Any], *, base_commit: str
) -> tuple[str, dict[str, Any], dict[str, Any] | None]:
    """Read reviewed design and its static preparation contract from the base."""
    path = source_reference_design_file(proposed.get("source_reference_design_file"))
    if not re.fullmatch(r"[a-f0-9]{40}", base_commit):
        raise ValueError("Source task requires an exact Git base commit")
    tree = _git(repo_root, "ls-tree", "-z", base_commit, "--", path).split(b"\0")
    entries = [entry for entry in tree if entry]
    if len(entries) != 1 or entries[0].split(b" ", 1)[0] not in {b"100644", b"100755"}:
        raise ValueError("Source design must be a regular committed file at the base")
    blob = f"{base_commit}:{path}"
    if int(_git(repo_root, "cat-file", "-s", blob)) > MAX_DESIGN_BYTES:
        raise ValueError("Source design exceeds the committed input byte limit")
    document = yaml.safe_load(_git(repo_root, "show", blob))
    if (
        not isinstance(document, dict)
        or not {"schema_version", "plan_id", "item_id", "design"}.issubset(document)
        or set(document)
        - {
            "schema_version",
            "plan_id",
            "item_id",
            "design",
            "preparation",
            "status",
            "amendment_requirement",
        }
        or document["schema_version"] != DESIGN_SCHEMA
        or not isinstance(document["design"], dict)
        or any(
            not isinstance(document[key], str)
            or not document[key].strip()
            or document[key] != proposed.get(key)
            for key in ("plan_id", "item_id")
        )
    ):
        raise ValueError(
            "Committed source design does not match the reviewed plan item"
        )
    preparation = document.get("preparation")
    if preparation is not None and not isinstance(preparation, dict):
        raise ValueError("Committed source design preparation is malformed")
    return path, document["design"], preparation


class _ReferenceReviewer:
    def __init__(self, client: WorkflowLLMClient) -> None:
        self.client = client

    def complete_json(
        self, messages: list[dict[str, str]], *, response_schema: Any = None
    ) -> dict[str, Any]:
        return complete_json(self.client, messages, response_schema=response_schema)


def _story_validation_is_accepted(result: Mapping[str, Any]) -> bool:
    coverage = result.get("story_coverage")
    health = result.get("verification_health")
    return (
        result.get("passed") is True
        and not result.get("coverage_evidence_error")
        and isinstance(coverage, Mapping)
        and coverage.get("verified") is True
        and isinstance(health, Mapping)
        and health.get("mode") == "enforce"
        and health.get("passed") is True
    )


def _validation_result(data: Mapping[str, Any]) -> dict[str, Any]:
    validation = data.get("validation")
    if isinstance(validation, Mapping):
        return dict(validation)
    return {
        "status": "not_run",
        "passed": False,
        "error": data.get("error") or "source reference validation did not complete",
    }


def dispatch_source_reference_task(
    *,
    repo_root: Path,
    proposed: Mapping[str, Any],
    base_commit: str,
    workflow_id: str,
    task_id: str,
    objective: str,
    allowed_paths: tuple[str, ...],
    profile: ValidationProfile,
    code_agent: str,
    model: str | None,
    worker_timeout_seconds: float,
    validation_timeout_seconds: float,
    max_correction_attempts: int = DEFAULT_CODING_AGENT_RECOVERY_ATTEMPTS,
    review_client: WorkflowLLMClient,
    story_context: Mapping[str, Any] | None = None,
    story_validation_bundle: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Correct failed candidates in linked attempts and transfer only a pass."""
    if max_correction_attempts < 1:
        raise ValueError("max_correction_attempts must be positive")
    if proposed.get("source_reference_mode", "enforce") != "enforce":
        raise PowdrrExecutionError(
            "Source reference tasks cannot downgrade enforce mode"
        )
    if code_agent != "minisweagent" or not model:
        raise PowdrrExecutionError("Source reference tasks require miniSWE and a model")
    story_validation = proposed.get("story_validation") is True
    if story_validation and not proposed.get("story_refs"):
        raise PowdrrExecutionError(
            "Story validation requires reviewed story references"
        )
    if story_validation and story_validation_bundle is None:
        raise PowdrrExecutionError(
            "Full story validation requires independently authored tests and a "
            "pinned story execution bundle"
        )
    if not story_validation and story_validation_bundle is not None:
        raise PowdrrExecutionError(
            "A story execution bundle requires a reviewed story-validation item"
        )
    if proposed.get("story_refs") and not isinstance(story_context, Mapping):
        raise PowdrrExecutionError(
            "Source reference implementation items with story references require "
            "validated pinned story context"
        )
    if not proposed.get("story_refs") and story_context is not None:
        raise PowdrrExecutionError(
            "Pinned story context requires reviewed story references"
        )
    design_path, design = load_reviewed_source_design(
        repo_root, proposed, base_commit=base_commit
    )
    preparation: dict[str, Any] | None = None
    if (
        proposed.get("execution_preparation_schema")
        == "source-reference-preparation-v1"
    ):
        design_path, design, preparation = load_reviewed_source_design_contract(
            repo_root, proposed, base_commit=base_commit
        )
        if preparation is None:
            raise PowdrrExecutionError(
                "Reviewed source design has no shared execution preparation contract"
            )
        try:
            validate_execution_preparation(
                preparation,
                design,
                planning_base_commit=str(preparation.get("planning_base_commit", "")),
                objective=str(proposed.get("objective", objective)),
                allowed_paths=allowed_paths,
                verification_commands=(
                    parse_command_spec(
                        str(proposed.get("verification_command", "python -m pytest"))
                    ),
                ),
                dependency_preconditions=tuple(
                    {"dependency_item_id": dependency, "status": "unresolved"}
                    for dependency in proposed.get("dependencies", ())
                ),
            )
        except (TypeError, ValueError) as error:
            raise PowdrrExecutionError(str(error)) from error
    design_blob_id = (
        _git(repo_root, "rev-parse", f"{base_commit}:{design_path}").decode().strip()
    )
    if story_context is not None:
        objective = (
            objective + "\n\nPinned customer story contract and selected cases "
            "(planned coverage, not verification):\n"
            + json.dumps(dict(story_context), indent=2, sort_keys=True)
            + "\nPreserve these exact references. Report discoveries as "
            "implementation_defect, environment_blocked, or "
            "design_amendment_needed. A behavior amendment returns to "
            "story-design-interview and a reviewed Structrr proposal; "
            "the coding worker cannot approve it."
        )
        if story_validation_bundle is not None:
            objective += (
                "\nThe independently authored story tests are present in the "
                "workspace as read-only files. Run them as needed, preserve their "
                "bytes, and do not weaken or replace their assertions."
            )
    identity = hashlib.sha256(
        json.dumps(
            [
                str(repo_root.resolve()),
                workflow_id,
                task_id,
                base_commit,
                design_path,
                dict(story_context) if story_context is not None else None,
            ],
            sort_keys=True,
        ).encode()
    ).hexdigest()
    root = Path(tempfile.gettempdir()) / "powdrr-source-tasks" / identity
    root.parent.mkdir(parents=True, exist_ok=True)
    try:
        root.mkdir(mode=0o700)
    except FileExistsError:
        dispatch_record = root / "dispatch.json"
        if dispatch_record.is_file():
            saved = json.loads(dispatch_record.read_text(encoding="utf-8"))
            if isinstance(saved, dict):
                return saved
        coordinator_record = root / "coordinator.json"
        state = (
            json.loads(coordinator_record.read_text(encoding="utf-8"))
            if coordinator_record.is_file()
            else {}
        )
        attempt_id = state.get("current_attempt_id")
        attempt_number = state.get("next_attempt")
        recovered_terminal = None
        if isinstance(attempt_id, str) and isinstance(attempt_number, int):
            terminal_path = (
                root
                / "attempts"
                / f"{attempt_number:02d}"
                / "runs"
                / attempt_id
                / "private"
                / "terminal.json"
            )
            if terminal_path.is_file():
                terminal = json.loads(terminal_path.read_text(encoding="utf-8"))
                if isinstance(terminal, dict):
                    recovered_terminal = {
                        "path": str(terminal_path),
                        "fingerprint": terminal.get("fingerprint"),
                        "outcome": terminal.get("outcome"),
                        "lineage_record_id": terminal.get("lineage_record_id"),
                    }
        interrupted = {
            "passed": False,
            "retryable": False,
            "status": "interrupted_attempt_requires_reconciliation",
            "error": (
                "A source attempt was already claimed for this immutable task. "
                "No duplicate invocation was started; reconcile the retained "
                "attempt artifacts before continuing."
            ),
            "source_reference_artifact_root": str(root),
            "coordinator": state,
            "attempts": state.get("attempts", []),
            "recovered_terminal": recovered_terminal,
        }
        temporary = root / "dispatch.json.tmp"
        temporary.write_text(
            json.dumps(interrupted, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, dispatch_record)
        return interrupted
    profiles: dict[str, ValidationProfile] = {profile.name: profile}
    test_sources: dict[str, str] = {}
    story_snapshot: Mapping[str, Any] | None = None
    story_authoring: Mapping[str, Any] | None = None
    story_profile: ValidationProfile | None = None
    if story_validation_bundle is not None:
        raw_sources = story_validation_bundle.get("test_sources")
        raw_snapshot = story_validation_bundle.get("snapshot")
        raw_authoring = story_validation_bundle.get("authoring")
        if (
            not isinstance(raw_sources, Mapping)
            or not raw_sources
            or any(
                not isinstance(path, str) or not isinstance(source, str)
                for path, source in raw_sources.items()
            )
            or not isinstance(raw_snapshot, Mapping)
            or not isinstance(raw_authoring, Mapping)
            or not isinstance(raw_authoring.get("story_test_links"), Mapping)
            or not raw_authoring["story_test_links"].get("links")
            or not isinstance(raw_authoring.get("accepted_story_review"), Mapping)
            or not isinstance(raw_authoring.get("story_review_evidence"), Mapping)
        ):
            raise PowdrrExecutionError("Story validation bundle is incomplete")
        test_sources = dict(raw_sources)
        if raw_authoring.get("test_sha256") != fingerprint_test_files(test_sources):
            raise PowdrrExecutionError(
                "Story validation test sources differ from reviewed authoring"
            )
        story_snapshot = raw_snapshot
        story_authoring = raw_authoring
        story_profile = ValidationProfile(
            name="story-validation",
            command=pytest_command_for_files(profile, tuple(sorted(test_sources))),
        )
        if story_profile.name == profile.name:
            raise PowdrrExecutionError("Story validation profile name collides")
        profiles[story_profile.name] = story_profile
    result: dict[str, Any] = {
        "passed": False,
        "retryable": False,
        "changed_paths": [],
        "attempts": [],
        "continuations": 0,
        "source_reference_artifact_root": str(root),
        "source_reference_mode": "enforce",
    }
    checkout_commit = _git(repo_root, "rev-parse", "HEAD").decode().strip()
    try:
        if not _is_base_ancestor_of_checkout(
            repo_root, base_commit=base_commit, checkout_commit=checkout_commit
        ):
            raise ValueError(
                "Reviewed source base is not an ancestor of the task checkout"
            )
        attempt_records: list[dict[str, Any]] = []
        seed_candidate_files: dict[str, str | None] = {}
        correction_context: dict[str, Any] | None = None
        previous_failure_fingerprint: str | None = None
        coordinator: dict[str, Any] = {
            "schema_version": CORRECTION_SCHEMA,
            "task_identity": identity,
            "base_commit": base_commit,
            "plan_id": proposed["plan_id"],
            "item_id": proposed["item_id"],
            "accepted_design_blob_id": design_blob_id,
            "attempt_budget": max_correction_attempts,
            "attempts": [],
            "status": "preparing",
        }
        _write_coordinator(root, coordinator)
        for attempt_number in range(1, max_correction_attempts + 1):
            attempt_id = f"implementation-attempt-{attempt_number:02d}"
            attempt_root = root / "attempts" / f"{attempt_number:02d}"
            attempt_root.mkdir(parents=True, exist_ok=False)
            candidate = attempt_root / "candidate"
            coordinator.update(
                status="invocation_pending",
                current_attempt_id=attempt_id,
                next_attempt=attempt_number,
            )
            _write_coordinator(root, coordinator)
            _git(repo_root, "worktree", "add", "--detach", str(candidate), base_commit)
            run = SourceReferenceRun.prepare(
                workspace_root=candidate,
                accepted_design=design,
                base_commit=base_commit,
                objective=objective,
                allowed_paths=allowed_paths,
                profiles=profiles,
                artifact_root=attempt_root / "runs",
                run_id=attempt_id,
                lineage_context={
                    "workflow_id": workflow_id,
                    "task_id": task_id,
                    "plan_id": proposed["plan_id"],
                    "item_id": proposed["item_id"],
                    "attempt_number": attempt_number,
                    "correction_of": (
                        attempt_records[-1].get("attempt_id")
                        if attempt_records
                        else None
                    ),
                    "design_path": design_path,
                    "design_blob_id": design_blob_id,
                },
                model=model,
                worker_timeout_seconds=worker_timeout_seconds,
                validation_timeout_seconds=validation_timeout_seconds,
                accepted_preparation=(
                    preparation
                    if proposed.get("execution_preparation_schema")
                    == "source-reference-preparation-v1"
                    else None
                ),
                seed_candidate_files=seed_candidate_files,
                correction_context=correction_context,
                review_client=_ReferenceReviewer(review_client),
                readonly_support_files=test_sources,
                post_worker_reconciler=lambda current_run: reconcile_source_task(
                    current_run, _ReferenceReviewer(review_client)
                ),
            )
            coordinator["status"] = "invocation_running"
            _write_coordinator(root, coordinator)
            outcome = run.execute(
                create_miniswe_inference_handler(worker_timeout_seconds)
            )
            data = outcome.to_data()
            attempt = data["attempt"] or {}
            attempt_record = {
                "attempt_id": attempt_id,
                "attempt_number": attempt_number,
                "source_reference_outcome": data["outcome"],
                "attempt_status": attempt.get("status", "failed"),
                "terminal_path": str(run.private_root / "terminal.json"),
                "candidate_path": str(candidate),
                "error": data.get("error"),
                "data": data,
            }
            attempt_records.append(attempt_record)
            coordinator["attempts"] = [
                {key: value for key, value in item.items() if key != "data"}
                for item in attempt_records
            ]
            coordinator["status"] = "attempt_completed"
            _write_coordinator(root, coordinator)
            if outcome.accepted:
                break
            recoverable = (
                data.get("outcome") == "validation_failed"
                and attempt.get("status") == "completed"
            )
            if not recoverable:
                if data.get("outcome") == "submitted_without_progress":
                    coordinator.update(
                        status="no_progress",
                        stopped_attempt_id=attempt_id,
                        reason=(
                            "worker submitted without attributable candidate changes"
                        ),
                    )
                    _write_coordinator(root, coordinator)
                break
            try:
                run.verify_candidate()
                seed_candidate_files = _capture_candidate_files(
                    candidate,
                    tuple(attempt.get("changed_paths", ())),
                    excluded=set(test_sources),
                )
            except (TypeError, ValueError, OSError) as error:
                coordinator.update(
                    status="blocked_by_candidate_policy", error=str(error)
                )
                _write_coordinator(root, coordinator)
                break
            failure_evidence = _correction_failure_summary(data)
            failure_evidence["attempt_id"] = attempt_id
            failure_fingerprint = content_fingerprint(
                {
                    "failure": {
                        key: value
                        for key, value in failure_evidence.items()
                        if key != "attempt_id"
                    },
                    "candidate": seed_candidate_files,
                }
            )
            if failure_fingerprint == previous_failure_fingerprint:
                coordinator.update(
                    status="no_progress",
                    failure_fingerprint=failure_fingerprint,
                )
                _write_coordinator(root, coordinator)
                break
            previous_failure_fingerprint = failure_fingerprint
            if attempt_number >= max_correction_attempts:
                coordinator.update(
                    status="attempt_budget_exhausted",
                    failure_fingerprint=failure_fingerprint,
                )
                _write_coordinator(root, coordinator)
                break
            correction_context = {
                "schema_version": CORRECTION_SCHEMA,
                "previous_attempt_id": attempt_id,
                "failure_fingerprint": failure_fingerprint,
                "failed_checks": _correction_failure_summary(data),
                "permitted_candidate_files": sorted(seed_candidate_files),
                "required_correction": (
                    "Repair the exact candidate validation failures while preserving "
                    "the accepted source design, independent tests, original base, "
                    "and all protected files."
                ),
            }
            coordinator.update(
                status="correction_pending",
                correction_of=attempt_id,
                failure_fingerprint=failure_fingerprint,
            )
            _write_coordinator(root, coordinator)

        if not attempt_records:
            raise PowdrrExecutionError("Source correction coordinator made no attempt")
        final_record = attempt_records[-1]
        data = final_record["data"]
        attempt = data["attempt"] or {}
        result.update(
            attempt_id=final_record["attempt_id"],
            status=(
                "accepted" if data["outcome"] == "accepted" else coordinator["status"]
            ),
            attempt_status=attempt.get("status", "failed"),
            validation=_validation_result(data),
            diff_check=data.get("diff_check"),
            source_reference_outcome=data["outcome"],
            source_reference_terminal=final_record["terminal_path"],
            lineage_record_id=data.get("lineage_record_id"),
            lineage_audit=data.get("lineage_audit"),
            attempts=[item["data"] for item in attempt_records],
            continuations=max(0, len(attempt_records) - 1),
            attempt_budget=max_correction_attempts,
            correction_coordinator=str(root / "coordinator.json"),
            error=data.get("error"),
        )
        if data.get("outcome") != "accepted":
            return result
        coordinator.update(
            status="accepted", accepted_attempt_id=final_record["attempt_id"]
        )
        _write_coordinator(root, coordinator)
        run.verify_candidate()
        route_evidence: Any = data.get("obligation_route_evidence")
        if story_validation_bundle is not None:
            assert story_profile is not None
            assert story_snapshot is not None
            assert story_authoring is not None
            story_artifacts = run.public_root / "story-validation"
            story_artifacts.mkdir()
            story_environment = os.environ.copy()
            _remove_provider_credentials(story_environment)
            for key in tuple(story_environment):
                if key.startswith("POWDRR_STRUCTRR_"):
                    story_environment.pop(key)
            story_result = execute_story_validation(
                candidate,
                snapshot=story_snapshot,
                authoring={**story_authoring, "test_sources": test_sources},
                profile=profile,
                repository=str(repo_root.resolve()),
                artifact_root=run.private_root / "story-executions",
                timeout_seconds=validation_timeout_seconds,
                command_wrapper=run.protect_command,
                environment_overrides=story_environment
                | run.validation_environment()
                | {
                    "PYTHONDONTWRITEBYTECODE": "1",
                    "PYTEST_ADDOPTS": "-p no:cacheprovider",
                },
                artifact_directory=story_artifacts,
            )
            run.verify_candidate()
            coverage = story_result.get("story_coverage", {})
            health = story_result.get("verification_health", {})
            if not _story_validation_is_accepted(story_result):
                health_findings = (
                    health.get("findings") if isinstance(health, Mapping) else None
                )
                story_cases = (
                    coverage.get("stories") if isinstance(coverage, Mapping) else None
                )
                result.update(
                    status="story_validation_failed",
                    story_validation=story_result,
                    error=(
                        "Candidate did not establish the pinned story contract: "
                        + str(
                            story_result.get("coverage_evidence_error")
                            or story_result.get("error")
                            or health_findings
                            or story_cases
                        )
                    ),
                )
                return result
            route_evidence = run.bind_story_validation(story_result)
            result["obligation_route_evidence"] = route_evidence
            result["obligation_route_evidence_path"] = str(
                run.private_root / "story-route-evidence.json"
            )
            result["story_validation"] = story_result
        result["obligation_route_evidence"] = route_evidence
        reconciliation = (
            route_evidence.get("reconciliation")
            if isinstance(route_evidence, Mapping)
            else None
        )
        if (
            not isinstance(route_evidence, Mapping)
            or route_evidence.get("complete") is not True
            or not isinstance(reconciliation, Mapping)
            or reconciliation.get("passed") is not True
        ):
            issues = (
                reconciliation.get("issues", [])
                if isinstance(reconciliation, Mapping)
                else ["route evidence is missing or incomplete"]
            )
            result.update(
                status="route_evidence_incomplete",
                error=f"Candidate route evidence is incomplete: {issues}",
            )
            return result
        lineage_path = run.private_root / "lineage.json"
        lineage = json.loads(lineage_path.read_text(encoding="utf-8"))
        if data.get("lineage_record_fingerprint") != content_fingerprint(lineage):
            lineage_audit = {
                "schema_version": "structrr-source-run-lineage-audit-v1",
                "run_id": run.run_id,
                "passed": False,
                "findings": [
                    {
                        "code": "lineage_record_tampered",
                        "subject_id": f"source-run:{run.run_id}",
                        "message": (
                            "Stored lineage record differs from the terminal record"
                        ),
                    }
                ],
            }
            result.update(
                status="lineage_failed",
                lineage_audit=lineage_audit,
                error="Source run lineage integrity check failed",
            )
            return result
        if hasattr(run, "accepted_design") and hasattr(run, "handoff"):
            lineage = build_source_run_lineage(
                run_id=run.run_id,
                lineage_context=run.lineage_context,
                accepted_design=run.accepted_design,
                handoff=run.handoff.to_data(),
                terminal={**data, "obligation_route_evidence": route_evidence},
            )
            run_audit = audit_source_run_lineage(lineage)
            if not run_audit["passed"]:
                result.update(
                    status="lineage_failed",
                    lineage_audit=run_audit,
                    error="Source run lineage is incomplete or invalid",
                )
                return result
        changed = tuple(sorted(set(attempt["changed_paths"]) | set(test_sources)))
        # The reviewed intent and task metadata are never implementation output.
        reserved = {
            design_path,
            proposed.get("execution_plan_file"),
            proposed.get("structrr_proposal_file"),
        }
        if any(path in reserved or path.startswith(".powdrr/") for path in changed):
            raise ValueError(
                "Source candidate changed reviewed intent or runtime state"
            )
        patch = _git(
            candidate,
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--binary",
            "--full-index",
            base_commit,
        )
        for raw_path in _git(
            candidate, "ls-files", "--others", "--exclude-standard", "-z"
        ).split(b"\0"):
            if not raw_path:
                continue
            untracked = subprocess.run(
                [
                    "git",
                    "diff",
                    "--no-index",
                    "--no-ext-diff",
                    "--no-textconv",
                    "--binary",
                    "--full-index",
                    "--",
                    "/dev/null",
                    raw_path.decode(),
                ],
                cwd=candidate,
                capture_output=True,
                check=False,
            )
            if untracked.returncode != 1:
                raise ValueError("Could not export validated untracked source")
            patch += untracked.stdout
        run.verify_candidate()
        (root / "accepted.patch").write_bytes(patch)
        if _git(repo_root, "rev-parse", "HEAD").decode().strip() != checkout_commit:
            raise ValueError("Task checkout moved while the source worker ran")
        if _git(
            repo_root, "status", "--porcelain", "--untracked-files=all", "--", *changed
        ):
            raise ValueError(
                "Task checkout changed on candidate paths; patch not applied"
            )
        _git(repo_root, "apply", "--check", "--binary", "-", data=patch)
        lineage = bind_source_run_patch(
            lineage, patch_sha256=hashlib.sha256(patch).hexdigest()
        )
        lineage_audit = audit_source_run_lineage(lineage)
        (run.private_root / "dispatch-lineage.json").write_text(
            json.dumps(lineage, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        (run.private_root / "dispatch-lineage-audit.json").write_text(
            json.dumps(lineage_audit, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        result.update(
            lineage_record_id=f"source-run:{run.run_id}",
            lineage_audit=lineage_audit,
            lineage_dispatch_record=str(run.private_root / "dispatch-lineage.json"),
            lineage_dispatch_fingerprint=content_fingerprint(lineage),
        )
        if not lineage_audit["passed"]:
            result.update(
                status="lineage_failed",
                error="Source patch lineage is incomplete or invalid",
            )
            return result
        _git(repo_root, "apply", "--binary", "-", data=patch)
        result.update(passed=True, changed_paths=list(changed))
    except KeyboardInterrupt:
        result.update(
            status="cancelled",
            error="Source correction was cancelled by the user.",
            retryable=False,
        )
        if "coordinator" in locals():
            coordinator.update(status="cancelled")
            _write_coordinator(root, coordinator)
    except Exception as error:
        result["error"] = str(error)
        result["status"] = "failed"
    finally:
        (root / "dispatch.json").write_text(json.dumps(result, indent=2) + "\n")
    # Preserve the candidate and all evidence for diagnosis and human review.
    return result
