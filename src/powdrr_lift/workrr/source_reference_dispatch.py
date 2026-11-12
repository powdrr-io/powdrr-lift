"""Dispatch reviewed source-reference tasks without entering legacy recovery."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml
from powdrr_agent.execution.execute_pr_tests import (
    fingerprint_test_files,
    pytest_command_for_files,
)
from powdrr_local.miniswe_inference import create_miniswe_inference_handler
from powdrr_service.planning.plan_models import source_reference_design_file

from powdrr_lift.errors import PowdrrExecutionError
from powdrr_lift.workrr.coding_agent import _remove_provider_credentials
from powdrr_lift.workrr.coding_agent_validation import ValidationProfile
from powdrr_lift.workrr.llm import WorkflowLLMClient, complete_json
from powdrr_lift.workrr.source_reference_run import SourceReferenceRun
from powdrr_lift.workrr.story_execution_operations import execute_story_validation

DESIGN_SCHEMA = "structrr-source-reference-task-v1"
MAX_DESIGN_BYTES = 1_000_000


def _git(root: Path, *arguments: str, data: bytes | None = None) -> bytes:
    result = subprocess.run(
        ["git", *arguments], cwd=root, input=data, capture_output=True, check=False
    )
    if result.returncode:
        raise PowdrrExecutionError(result.stderr.decode(errors="replace").strip())
    return result.stdout


def load_reviewed_source_design(
    repo_root: Path, proposed: Mapping[str, Any], *, base_commit: str
) -> tuple[str, dict[str, Any]]:
    """Read accepted intent from a regular committed blob, never candidate files."""
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
        or set(document) != {"schema_version", "plan_id", "item_id", "design"}
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
    return path, document["design"]


class _ReferenceReviewer:
    def __init__(self, client: WorkflowLLMClient) -> None:
        self.client = client

    def complete_json(
        self, messages: list[dict[str, str]], *, response_schema: Any = None
    ) -> dict[str, Any]:
        return complete_json(self.client, messages, response_schema=response_schema)


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
    review_client: WorkflowLLMClient,
    story_context: Mapping[str, Any] | None = None,
    story_validation_bundle: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Run once in a clean candidate, then transfer only an accepted patch."""
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
    except FileExistsError as error:
        raise PowdrrExecutionError(
            f"Source task already dispatched; inspect {root}. "
            "Accept a new design revision before another implementation attempt."
        ) from error
    candidate = root / "candidate"
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
    try:
        if _git(repo_root, "rev-parse", "HEAD").decode().strip() != base_commit:
            raise ValueError("Task checkout differs from the reviewed source base")
        _git(repo_root, "worktree", "add", "--detach", str(candidate), base_commit)
        run = SourceReferenceRun.prepare(
            workspace_root=candidate,
            accepted_design=design,
            base_commit=base_commit,
            objective=objective,
            allowed_paths=allowed_paths,
            profiles=profiles,
            artifact_root=root / "runs",
            run_id="implementation",
            model=model,
            worker_timeout_seconds=worker_timeout_seconds,
            validation_timeout_seconds=validation_timeout_seconds,
            review_client=_ReferenceReviewer(review_client),
            readonly_support_files=test_sources,
        )
        outcome = run.execute(create_miniswe_inference_handler(worker_timeout_seconds))
        data = outcome.to_data()
        attempt = data["attempt"] or {}
        result.update(
            attempt_id=attempt.get("attempt_id"),
            status=attempt.get("status", "failed"),
            validation=data["validation"],
            source_reference_outcome=data["outcome"],
            source_reference_terminal=str(run.private_root / "terminal.json"),
            attempts=[data],
            error=data["error"],
        )
        if not outcome.accepted:
            return result
        run.verify_candidate()
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
            if (
                not story_result.get("passed")
                or story_result.get("coverage_evidence_error")
                or not isinstance(coverage, Mapping)
                or coverage.get("verified") is not True
            ):
                result.update(
                    status="story_validation_failed",
                    story_validation=story_result,
                    error=(
                        "Candidate did not establish the pinned story contract: "
                        + str(
                            story_result.get("coverage_evidence_error")
                            or story_result.get("error")
                            or coverage.get("stories")
                        )
                    ),
                )
                return result
            result["story_validation"] = story_result
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
        if _git(repo_root, "rev-parse", "HEAD").decode().strip() != base_commit:
            raise ValueError("Task checkout moved while the source worker ran")
        if _git(
            repo_root, "status", "--porcelain", "--untracked-files=all", "--", *changed
        ):
            raise ValueError(
                "Task checkout changed on candidate paths; patch not applied"
            )
        _git(repo_root, "apply", "--check", "--binary", "-", data=patch)
        _git(repo_root, "apply", "--binary", "-", data=patch)
        result.update(passed=True, changed_paths=list(changed))
    except Exception as error:
        result["error"] = str(error)
        result["status"] = "failed"
    finally:
        (root / "dispatch.json").write_text(json.dumps(result, indent=2) + "\n")
    # Preserve the candidate and all evidence for diagnosis and human review.
    return result
