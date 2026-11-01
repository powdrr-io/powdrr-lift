"""Reusable application boundary for creating a reviewable execution plan."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any, TextIO

from powdrr_contracts.models import OperationRequest
from powdrr_local import JsonServiceBridge, LocalPowdrrService
from powdrr_local.planning.checkpoints import (
    GitHubCommentCheckpointStore,
    LocalCheckpointStore,
)
from powdrr_local.planning.sessions import GitHubPlanningIssueStore
from powdrr_local.planning.source import read_pinned_source
from powdrr_service.planning.assembly import PathFact
from powdrr_service.planning.compiler import PlanningCompilationError
from powdrr_service.planning.driver import SourceLedgerQuestionMachine
from powdrr_service.planning.plan_sizing import PATH_RUNTIME_SUFFIXES
from powdrr_service.planning.service import (
    PlanningOptions,
    PlanningServiceError,
    PlanningSession,
    run_planning_procedrr,
)
from powdrr_service.planning.source import segment_markdown_source
from powdrr_service.planning.workflow_ids import slugify_workflow_id
from powdrr_service.providers.credentials import resolve_provider_credentials

from powdrr_lift.core import resolve_repo_root
from powdrr_lift.core.entity_taxonomy import load_entity_taxonomy
from powdrr_lift.errors import PowdrrExecutionError
from powdrr_lift.structrr.bootstrap import bootstrap_structrr
from powdrr_lift.workrr.credential_check import configured_model_mapping
from powdrr_lift.workrr.github_issue_plan_generation import (
    PlanPullRequest,
    publish_execution_plan_pull_request,
)
from powdrr_lift.workrr.github_issue_queue import (
    GitHubIssueClient,
    GitHubIssueQueueError,
)
from powdrr_lift.workrr.planning.evidence import (
    read_evidence_excerpt,
    search_requirements_evidence,
)
from powdrr_lift.workrr.provider_runtime import (
    build_workflow_client,
    resolve_workflow_provider,
)


@dataclass(frozen=True, slots=True)
class PlanningSessionConfig:
    """Inputs required to create or resume one planning session."""

    repo_root: Path | None
    resume: bool = False
    repository: str | None = None
    session_issue: int | None = None
    source: Path | None = None
    plan_id: str | None = None
    verification_command: str = ""
    output: Path | None = None
    provider: str = "auto"
    model: str | None = None
    api_key: str | None = None
    base_url: str | None = None
    concurrency: int = 1


@dataclass(frozen=True, slots=True)
class PlanningSessionResult:
    """Review PR details returned to CLI, web, or another application adapter."""

    plan_id: str
    session_issue_url: str
    output_path: str
    item_ids: tuple[str, ...]
    generated_files: tuple[str, ...]
    workflow_count: int
    pull_request: PlanPullRequest


class PlanningSessionExecutionError(PowdrrExecutionError):
    """Planning failed after attempting terminal checkpoint/status write-back."""


def run_planning_session(
    config: PlanningSessionConfig,
    *,
    progress_stream: TextIO | None = None,
    stage_generated_file: Callable[[Path, Path], None] | None = None,
) -> PlanningSessionResult:
    """Create a reviewable plan through the existing service and provider path.

    This function owns planning execution and external publication effects. It
    returns structured data and leaves presentation to the invoking adapter.
    """
    repo_root = resolve_repo_root(config.repo_root).resolve()
    stream = progress_stream or sys.stderr
    stage_file = stage_generated_file or _stage_generated_file
    github = (
        GitHubIssueClient(config.repository)
        if config.resume and config.repository is not None
        else None
    )
    session: PlanningSession | None = None
    checkpoint_store: LocalCheckpointStore | None = None
    issue_checkpoint_store: GitHubCommentCheckpointStore | None = None
    plan_id = config.plan_id or ""
    output_path: Path | None = None
    try:
        if config.resume:
            if (
                github is None
                or config.repository is None
                or config.session_issue is None
            ):
                raise ValueError("resume requires a repository and planning issue")
            session = _planning_session_operation(
                GitHubPlanningIssueStore(github),
                action="load_session",
                repository_id=config.repository,
                issue_number=config.session_issue,
            )
            plan_id = session.plan_id
            source_relative = session.source_path
            verification_command = session.verification_command
        else:
            if config.source is None or not config.plan_id:
                raise ValueError("planning requires a source path and plan ID")
            source_path = _repository_file_path(config.source, repo_root, "Source plan")
            if not source_path.is_file():
                raise ValueError(f"Source plan does not exist: {source_path}")
            source_relative = source_path.relative_to(repo_root).as_posix()
            verification_command = config.verification_command
            repository_name = _discover_github_repository(repo_root)
            github = GitHubIssueClient(repository_name)

        output_path = (
            _repository_file_path(config.output, repo_root, "Output plan")
            if not config.resume and config.output is not None
            else repo_root
            / "docs"
            / "plans"
            / f"{slugify_workflow_id(plan_id)}-execution-plan.yaml"
        )
        if output_path == repo_root / source_relative:
            raise ValueError("Output plan must be different from the source plan")
        if output_path.exists():
            raise FileExistsError(
                f"Output plan already exists: {output_path}; choose another path "
                "so the reviewed plan is not overwritten."
            )

        base_commit = (
            session.base_commit if session is not None else _git_head(repo_root)
        )
        _require_clean_planning_checkout(repo_root, base_commit)
        assert github is not None
        pinned_source = read_pinned_source(
            str(repo_root),
            base_commit,
            source_relative,
            repository_id=github.repository,
        )
        if session is None:
            session = _planning_session_operation(
                GitHubPlanningIssueStore(github),
                action="create_session",
                repository_id=github.repository,
                plan_id=plan_id,
                base_commit=base_commit,
                source_path=source_relative,
                source_blob_sha=pinned_source.blob_sha,
                verification_command=verification_command,
            )
        elif pinned_source.blob_sha != session.source_blob_sha:
            raise PlanningServiceError(
                "pinned source blob differs from the planning issue metadata"
            )

        structrr_baseline = _bootstrap_planning_structrr_baseline(
            repo_root, session.session_id
        )
        planning_provider = resolve_workflow_provider(config.provider)
        planning_mapping = configured_model_mapping(planning_provider, config.model)
        credentials = resolve_provider_credentials(
            planning_mapping.provider,
            config.api_key,
            config.base_url,
        )
        planning_client = build_workflow_client(
            credentials,
            model=planning_mapping.model,
            model_cache_dir=repo_root / ".powdrr" / "models",
            progress_stream=stream,
        )
        set_output_limit = getattr(
            planning_client, "set_structured_output_token_limit", None
        )
        if callable(set_output_limit):
            set_output_limit(8192)

        source = segment_markdown_source(pinned_source)
        taxonomy = load_entity_taxonomy(repo_root, _structrr_taxonomy_path(repo_root))
        machine = SourceLedgerQuestionMachine(
            source,
            evidence_search=lambda requirements: search_requirements_evidence(
                repo_root,
                base_commit,
                requirements,
            ),
            evidence_excerpt_reader=lambda evidence: read_evidence_excerpt(
                repo_root, evidence
            ),
            verification_command=verification_command,
            repository_paths=_planning_repository_paths(
                repo_root, base_commit, pinned_source.text
            ),
            plan_id=plan_id,
            structrr_baseline=structrr_baseline,
            taxonomy_entity_types=taxonomy.entity_types,
        )
        checkpoint_store = LocalCheckpointStore(
            repo_root, session_id=session.session_id
        )
        issue_checkpoint_store = GitHubCommentCheckpointStore(
            github, session.issue.number, session_id=session.session_id
        )
        if config.resume and not checkpoint_store.path.exists():
            checkpoint_store.import_legacy_comments(
                github.list_checkpoint_comments(
                    session.issue.number, session_id=session.session_id
                )
            )
        journal = checkpoint_store.load()
        if journal.events:
            issue_checkpoint_store.publish_archive(journal.events)
        process_directory = (
            Path(__file__).resolve().parents[2] / "docs" / "procedrr" / "processes"
        )
        result = run_planning_procedrr(
            process_directory=process_directory,
            session=session,
            state_machine=machine,
            checkpoint_store=checkpoint_store,
            client=planning_client,
            options=PlanningOptions(
                model=planning_mapping.model,
                max_concurrent_questions=(
                    1 if planning_mapping.provider == "local" else config.concurrency
                ),
                use_provider_schema=planning_mapping.provider != "deepinfra-cheap",
            ),
            stderr=stream,
        )
        if result.terminal_status.value != "succeeded":
            journal = checkpoint_store.load()
            if journal.events:
                issue_checkpoint_store.publish_archive(journal.events)
            raise PlanningServiceError(
                f"Procedrr planning process ended {result.terminal_status.value}"
            )
        journal = checkpoint_store.load()
        if journal.events:
            issue_checkpoint_store.publish_archive(journal.events)
        accepted_answers = _accepted_planning_answers(journal.events)
        relative_output = output_path.relative_to(repo_root).as_posix()
        plan_slug = slugify_workflow_id(plan_id)
        proposal_relative = (
            Path("docs") / "plans" / f"{plan_slug}-structrr-proposal.json"
        ).as_posix()
        compiled = machine.compile_accepted_plan(
            accepted_answers,
            plan_id=plan_id,
            plan_path=relative_output,
            proposal_path=proposal_relative,
        )
        generated_files = _write_compiled_plan_files(repo_root, compiled.files)
        for generated_file in generated_files:
            stage_file(repo_root, generated_file)
        pull_request = publish_execution_plan_pull_request(
            repo_root=repo_root,
            plan=compiled.plan,
            generated_files=generated_files,
            source_plan_path=source_relative,
        )
        try:
            github.add_issue_comment(
                session.issue.number,
                "Planning completed and the review PR is open: "
                f"{pull_request.pull_request_url}",
            )
        except GitHubIssueQueueError as update_error:
            print(
                f"Could not write the completed planning status to GitHub: "
                f"{update_error}",
                file=stream,
            )
        return PlanningSessionResult(
            plan_id=plan_id,
            session_issue_url=session.issue.url,
            output_path=relative_output,
            item_ids=tuple(item.item_id for item in compiled.plan.items),
            generated_files=tuple(
                path.relative_to(repo_root).as_posix() for path in generated_files
            ),
            workflow_count=len(compiled.plan.items),
            pull_request=pull_request,
        )
    except Exception as error:
        if checkpoint_store is not None and issue_checkpoint_store is not None:
            try:
                journal = checkpoint_store.load()
                if journal.events:
                    issue_checkpoint_store.publish_archive(journal.events)
            except Exception as archive_error:
                print(
                    f"Could not publish the planning checkpoint archive: "
                    f"{archive_error}",
                    file=stream,
                )
        if session is not None and github is not None:
            try:
                github.add_issue_comment(
                    session.issue.number,
                    "Planning stopped after a terminal error. "
                    f"Resume with `powdrr plan-resume --session-issue "
                    f"{session.issue.number}`. Error: {error}",
                )
            except GitHubIssueQueueError as update_error:
                print(
                    f"Could not write the terminal planning status to GitHub: "
                    f"{update_error}",
                    file=stream,
                )
        raise PlanningSessionExecutionError(
            f"Could not create execution plan: {error}"
        ) from error


def _planning_session_operation(
    issue_store: GitHubPlanningIssueStore,
    *,
    action: str,
    repository_id: str,
    **parameters: Any,
) -> PlanningSession:
    request = OperationRequest(
        request_id=f"planning-session-{uuid.uuid4().hex}",
        operation_kind="plan",
        inputs={"action": action, "repository_id": repository_id, **parameters},
    )
    update = JsonServiceBridge(
        LocalPowdrrService(planning_issue_store=issue_store)
    ).start(request)
    if update.status != "succeeded":
        message = (
            update.failure.message
            if update.failure is not None
            else "Service did not return a planning session."
        )
        raise PlanningServiceError(message)
    artifact = next(
        (
            item
            for item in update.output_artifacts
            if item.artifact_id == "planning/session.json"
            and item.media_type == "application/vnd.powdrr.planning-session+json"
        ),
        None,
    )
    if artifact is None:
        raise PlanningServiceError("Service response omitted planning session data.")
    try:
        data = json.loads(artifact.data)
    except json.JSONDecodeError as error:
        raise PlanningServiceError(
            "Service returned malformed planning session data."
        ) from error
    return PlanningSession.from_data(data)


def _accepted_planning_answers(events: Sequence[Any]) -> dict[str, dict[str, Any]]:
    accepted: dict[str, dict[str, Any]] = {}
    for event in events:
        question = event.payload.get("question")
        if not isinstance(question, Mapping):
            continue
        question_id = question.get("question_id")
        if not isinstance(question_id, str):
            continue
        if event.event_type == "answer_accepted":
            answer = event.payload.get("accepted_answer")
            if isinstance(answer, Mapping):
                accepted[question_id] = dict(answer)
        elif event.event_type == "needs_answer":
            accepted.pop(question_id, None)
    return accepted


def _bootstrap_planning_structrr_baseline(
    repo_root: Path, session_id: str
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="powdrr-lift-plan-structrr-") as temp_dir:
        result = bootstrap_structrr(
            repo_root,
            output_path=Path(temp_dir) / f"{session_id}.yaml",
            taxonomy_path=_structrr_taxonomy_path(repo_root),
        )
        if not result.validation.successful:
            details = "; ".join(
                f"{issue.code}: {issue.message}" for issue in result.validation.issues
            )
            raise PlanningServiceError(
                f"Structrr bootstrap failed before planning: {details}"
            )
        return result.document


def _discover_github_repository(repo_root: Path) -> str:
    result = subprocess.run(
        ["gh", "repo", "view", "--json", "nameWithOwner"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise GitHubIssueQueueError(
            "could not determine the current GitHub repository: "
            + result.stderr.strip()
        )
    value = json.loads(result.stdout)
    repository = value.get("nameWithOwner") if isinstance(value, Mapping) else None
    if not isinstance(repository, str) or not repository:
        raise GitHubIssueQueueError("gh repo view did not return nameWithOwner")
    return repository


def _git_head(repo_root: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD^{commit}"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise ValueError("planning requires a committed base revision")
    return result.stdout.strip()


def _require_clean_planning_checkout(repo_root: Path, base_commit: str) -> None:
    if _git_head(repo_root) != base_commit:
        raise PlanningServiceError(
            "resume requires a checkout at the session's pinned base commit"
        )
    result = subprocess.run(
        ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    )
    if result.stdout:
        raise PlanningServiceError(
            "planning requires a clean checkout at the pinned base commit"
        )


def _planning_repository_paths(
    repo_root: Path, base_commit: str, source_text: str
) -> Mapping[str, PathFact]:
    listing = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", base_commit],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    tracked = set(listing)
    cited_paths = set(
        re.findall(
            r"(?<![A-Za-z0-9_./-])(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+\.[A-Za-z0-9]+",
            source_text,
        )
    )
    return {
        path: PathFact(path, PATH_RUNTIME_SUFFIXES.get(Path(path).suffix.lower()))
        for path in sorted(tracked & cited_paths)
    }


def _write_compiled_plan_files(
    repo_root: Path, files: Mapping[str, str]
) -> tuple[Path, ...]:
    destinations = tuple((repo_root / relative).resolve() for relative in files)
    for destination in destinations:
        try:
            destination.relative_to(repo_root.resolve())
        except ValueError as error:
            raise PlanningCompilationError(
                "compiled artifact destination escaped the repository"
            ) from error
        if destination.exists():
            raise FileExistsError(
                "compiled artifact already exists: "
                f"{destination.relative_to(repo_root)}"
            )
    created: list[Path] = []
    try:
        for (_relative, content), destination in zip(
            files.items(), destinations, strict=True
        ):
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(content, encoding="utf-8")
            created.append(destination)
    except OSError:
        for path in created:
            path.unlink(missing_ok=True)
        raise
    return destinations


def _repository_file_path(path: Path, repo_root: Path, label: str) -> Path:
    candidate = path if path.is_absolute() else repo_root / path
    resolved = candidate.resolve()
    try:
        resolved.relative_to(repo_root)
    except ValueError as error:
        raise ValueError(f"{label} path must be inside the repository") from error
    return resolved


def _structrr_taxonomy_path(repo_root: Path) -> Path:
    candidate = repo_root / "software_development_entity_taxonomy.md"
    if candidate.is_file():
        return candidate
    packaged_resource = files("powdrr_service.resources").joinpath(
        "software_development_entity_taxonomy.md"
    )
    packaged_path = Path(str(packaged_resource))
    if packaged_path.is_file():
        return packaged_path
    package_data_path = (
        Path(__file__).resolve().parents[2] / "software_development_entity_taxonomy.md"
    )
    source_tree_path = (
        Path(__file__).resolve().parents[3] / "software_development_entity_taxonomy.md"
    )
    return next(
        (path for path in (package_data_path, source_tree_path) if path.is_file()),
        package_data_path,
    )


def _stage_generated_file(repo_root: Path, output_path: Path) -> None:
    relative_path = output_path.resolve().relative_to(repo_root.resolve())
    subprocess.run(
        ["git", "add", "--", str(relative_path)],
        cwd=repo_root,
        check=True,
    )


__all__ = [
    "PlanningSessionConfig",
    "PlanningSessionExecutionError",
    "PlanningSessionResult",
    "run_planning_session",
]
