"""Local service adapter used during the staged migration."""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

import yaml
from powdrr_contracts.models import (
    Artifact,
    ChatCompletionRequest,
    ChatCompletionResponse,
    OperationInput,
    OperationReference,
    OperationRequest,
    OperationUpdate,
    ServiceError,
    artifact_manifest_digest,
)

from powdrr_service.providers.inference import (
    DeepInfraInferenceClient,
    InferenceClient,
)


class LocalPowdrrService:
    """Reconstruct public artifact state from data supplied by the agent."""

    def __init__(
        self,
        *,
        planning_issue_store: Any | None = None,
        inference_client: InferenceClient | None = None,
        quality_review_clients: tuple[Any, Any] | None = None,
        probe_repair_clients: tuple[Any, Any] | None = None,
    ) -> None:
        self._planning_issue_store = planning_issue_store
        self._inference_client = inference_client or DeepInfraInferenceClient()
        self._quality_review_clients = quality_review_clients
        self._probe_repair_clients = probe_repair_clients

    def start(self, request: OperationRequest) -> OperationUpdate:
        if request.operation_kind == "review_feedback":
            return self._review_feedback(request)
        if request.operation_kind == "verification_health":
            return self._verification_health(request)
        if request.operation_kind == "plan":
            return self._planning_session(request)
        if request.operation_kind == "replan":
            return self._prepare_plan_repair(request)
        if request.operation_kind == "bootstrap":
            return self._bootstrap_structrr(request)
        if request.operation_kind == "compile_structrr_diff":
            return self._compile_structrr_diff(request)
        if request.operation_kind != "inspect_artifacts":
            return _failed(
                request.request_id,
                "unsupported_operation",
                "Operation is not migrated yet.",
            )
        if request.repository is None:
            return _failed(
                request.request_id,
                "invalid_request",
                "Repository snapshot is required.",
            )
        areas = request.inputs.get("areas", ["workflows"])
        if areas != ["workflows"]:
            return _failed(
                request.request_id,
                "unsupported_projection",
                "This boundary slice supports workflow inspection.",
            )
        try:
            projection = self._inspect_workflows(request)
        except (ValueError, OSError, subprocess.SubprocessError) as exc:
            return _failed(request.request_id, "artifact_inspection_failed", str(exc))
        payload = json.dumps(projection, sort_keys=True, separators=(",", ":")).encode()
        return OperationUpdate(
            operation_id=request.request_id,
            revision=1,
            status="succeeded",
            progress=({"phase": "artifact_state_reconstructed", "area": "workflows"},),
            output_artifacts=(
                Artifact(
                    artifact_id="artifact-state/workflows.json",
                    media_type="application/vnd.powdrr.artifact-state+json",
                    data=payload,
                ),
            ),
        )

    def _review_feedback(self, request: OperationRequest) -> OperationUpdate:
        try:
            from powdrr_service.review_feedback import (
                ReviewFeedbackError,
                build_review_feedback_ledger,
            )

            evidence = request.inputs.get("review_evidence")
            if not isinstance(evidence, dict):
                raise ReviewFeedbackError("review_evidence must be an object")
            ledger = build_review_feedback_ledger(evidence)
        except ReviewFeedbackError as exc:
            return _failed(request.request_id, "invalid_review_evidence", str(exc))

        payload = json.dumps(ledger, sort_keys=True, separators=(",", ":")).encode()
        phase = (
            "review_threads_prepared"
            if ledger["status"] == "ready"
            else "review_threads_empty"
        )
        return OperationUpdate(
            operation_id=request.request_id,
            revision=1,
            status="succeeded",
            progress=({"phase": phase},),
            output_artifacts=(
                Artifact(
                    artifact_id="review/review-feedback-ledger.json",
                    media_type="application/vnd.powdrr.review-feedback+json",
                    data=payload,
                ),
            ),
        )

    def _verification_health(self, request: OperationRequest) -> OperationUpdate:
        verification = request.inputs.get("verification")
        if verification == "behavior_probe_repair":
            return self._behavior_probe_repair(request)
        from powdrr_service.verification.execute_pr_test_quality import (
            review_execute_pr_test_quality,
        )

        evidence = request.inputs.get("suite_evidence")
        provider = request.inputs.get("provider")
        model = request.inputs.get("model")
        if verification != "execute_pr_test_quality":
            return _failed(
                request.request_id,
                "unsupported_verification",
                "verification_health does not support this verification profile.",
            )
        if not isinstance(evidence, dict):
            return _failed(
                request.request_id,
                "invalid_review_evidence",
                "suite_evidence must be an object.",
            )
        if not isinstance(provider, str) or not provider:
            return _failed(
                request.request_id,
                "invalid_review_configuration",
                "provider must be a non-empty service provider name.",
            )
        if not isinstance(model, str) or not model:
            return _failed(
                request.request_id,
                "invalid_review_configuration",
                "model must be a non-empty model name.",
            )
        retries = request.inputs.get("max_timeout_retries", 8)
        backoff = request.inputs.get("timeout_backoff_seconds", 10.0)
        suggest_tests = request.inputs.get("suggest_tests", True)
        if (
            not isinstance(retries, int)
            or isinstance(retries, bool)
            or retries < 0
            or not isinstance(backoff, (int, float))
            or isinstance(backoff, bool)
            or backoff < 0
            or not isinstance(suggest_tests, bool)
        ):
            return _failed(
                request.request_id,
                "invalid_review_configuration",
                "review retry settings or suggest_tests are invalid.",
            )

        try:
            if self._quality_review_clients is None:
                from powdrr_service.decision_classifier import (
                    JevSemanticClassifierClient,
                )
                from powdrr_service.providers.clients import build_provider_client
                from powdrr_service.providers.config import (
                    ProviderConfigurationError,
                    default_llm_mappings,
                    provider_definition,
                )
                from powdrr_service.providers.credentials import (
                    resolve_provider_credentials,
                )
                from powdrr_service.providers.selection import (
                    initial_model_for_provider,
                    resolve_provider,
                )

                base_url = request.inputs.get("base_url")
                if base_url is not None and not isinstance(base_url, str):
                    raise ValueError("base_url must be a string")
                provider_prefix, separator, unprefixed_model = model.partition("/")
                if provider == "auto" and separator:
                    try:
                        provider_definition(provider_prefix)
                    except ProviderConfigurationError:
                        pass
                    else:
                        provider, model = provider_prefix, unprefixed_model
                provider = resolve_provider(provider, model)
                if model.startswith(f"{provider}/"):
                    model = model.removeprefix(f"{provider}/")
                if model == "fast_iteration":
                    mapping = default_llm_mappings(provider).get("fast_iteration")
                    if mapping is None:
                        raise ValueError(
                            f"No fast_iteration model is configured for {provider!r}"
                        )
                    provider, model = mapping.provider, mapping.model
                    if provider == "local":
                        raise ValueError(
                            "Local quality-review model requires an explicit "
                            "model path."
                        )
                model = initial_model_for_provider(provider, model)
                credentials = resolve_provider_credentials(
                    provider,
                    base_url_override=base_url,
                )
                llm_client = build_provider_client(
                    provider=provider,
                    model=model,
                    api_key=credentials.api_key,
                    base_url=credentials.base_url,
                    progress_stream=None,
                )
                jev_client = JevSemanticClassifierClient(llm_client, fail_closed=True)
            else:
                jev_client, llm_client = self._quality_review_clients
            report = review_execute_pr_test_quality(
                evidence,
                jev_client=jev_client,
                llm_client=llm_client,
                max_timeout_retries=retries,
                timeout_backoff_seconds=float(backoff),
                suggest_tests=suggest_tests,
            )
        except Exception as exc:  # Quality review is advisory, including outages.
            report = {
                "provider": "jev",
                "outcome": "advisory",
                "assessments": [],
                "suggestions": [],
                "notes": [f"Execute-PR test quality review was unavailable: {exc}"],
            }

        payload = json.dumps(report, sort_keys=True, separators=(",", ":")).encode()
        return OperationUpdate(
            operation_id=request.request_id,
            revision=1,
            status="succeeded",
            progress=(
                {"phase": "verification_review_completed", "profile": verification},
            ),
            output_artifacts=(
                Artifact(
                    artifact_id="verification/execute-pr-test-quality.json",
                    media_type="application/vnd.powdrr.verification-health+json",
                    data=payload,
                ),
            ),
        )

    def _behavior_probe_repair(self, request: OperationRequest) -> OperationUpdate:
        from powdrr_service.verification.probe_repair import repair_behavior_probes

        inputs = request.inputs
        instruction = inputs.get("instruction")
        scenarios = inputs.get("scenarios")
        faithfulness_evidence = inputs.get("faithfulness_evidence", {})
        ledger_clauses = inputs.get("ledger_clauses", [])
        repository_contract = inputs.get("repository_contract", {})
        provider = inputs.get("provider")
        model = inputs.get("model")
        if not isinstance(instruction, str) or not isinstance(scenarios, list):
            return _failed(
                request.request_id,
                "invalid_probe_repair_input",
                "instruction and scenarios are required.",
            )
        if not all(isinstance(item, dict) for item in scenarios):
            return _failed(
                request.request_id,
                "invalid_probe_repair_input",
                "scenarios must contain JSON objects.",
            )
        if (
            not isinstance(faithfulness_evidence, dict)
            or not isinstance(ledger_clauses, list)
            or not all(isinstance(item, dict) for item in ledger_clauses)
        ):
            return _failed(
                request.request_id,
                "invalid_probe_repair_input",
                "faithfulness_evidence and ledger_clauses have invalid shapes.",
            )
        if not isinstance(repository_contract, dict):
            return _failed(
                request.request_id,
                "invalid_probe_repair_input",
                "repository_contract must be an object.",
            )
        if (
            not isinstance(provider, str)
            or not provider
            or not isinstance(model, str)
            or not model
        ):
            return _failed(
                request.request_id,
                "invalid_review_configuration",
                "provider and model must be non-empty strings.",
            )

        try:
            if self._probe_repair_clients is None:
                from powdrr_service.decision_classifier import (
                    JevSemanticClassifierClient,
                )
                from powdrr_service.providers.clients import build_provider_client
                from powdrr_service.providers.credentials import (
                    resolve_provider_credentials,
                )
                from powdrr_service.providers.selection import (
                    initial_model_for_provider,
                    resolve_provider,
                )

                base_url = inputs.get("base_url")
                if base_url is not None and not isinstance(base_url, str):
                    raise ValueError("base_url must be a string")
                provider = resolve_provider(provider, model)
                model = initial_model_for_provider(provider, model)
                credentials = resolve_provider_credentials(
                    provider,
                    base_url_override=base_url,
                )
                llm_client = build_provider_client(
                    provider=provider,
                    model=model,
                    api_key=credentials.api_key,
                    base_url=credentials.base_url,
                    progress_stream=None,
                )
                jev_client = JevSemanticClassifierClient(llm_client, fail_closed=True)
            else:
                jev_client, llm_client = self._probe_repair_clients
            revised, report = repair_behavior_probes(
                llm_client,
                instruction=instruction,
                scenarios=scenarios,
                faithfulness_evidence=faithfulness_evidence,
                ledger_clauses=ledger_clauses,
                faithfulness_client=jev_client,
                faithfulness_detail_client=llm_client,
                repository_contract=repository_contract,
            )
            result = {"scenarios": list(revised), "report": report}
        except Exception as exc:
            return _failed(request.request_id, "probe_repair_failed", str(exc))

        payload = json.dumps(result, sort_keys=True, separators=(",", ":")).encode()
        return OperationUpdate(
            operation_id=request.request_id,
            revision=1,
            status="succeeded",
            progress=(
                {
                    "phase": "verification_review_completed",
                    "profile": "behavior_probe_repair",
                },
            ),
            output_artifacts=(
                Artifact(
                    artifact_id="verification/behavior-probe-repair.json",
                    media_type="application/vnd.powdrr.verification-health+json",
                    data=payload,
                ),
            ),
        )

    def _planning_session(self, request: OperationRequest) -> OperationUpdate:
        store = self._planning_issue_store
        if store is None:
            return _failed(
                request.request_id,
                "adapter_unavailable",
                "Planning issue adapter is not configured.",
            )
        try:
            from powdrr_service.planning.service import (
                PlanningServiceError,
                create_planning_session,
                load_planning_session,
            )

            action = request.inputs.get("action")
            repository = _required_string(
                request.inputs.get("repository_id"), "repository_id"
            )
            if repository != store.repository:
                raise PlanningServiceError(
                    "planning issue adapter repository differs from request"
                )
            if action == "create_session":
                session = create_planning_session(
                    store,
                    plan_id=_required_string(request.inputs.get("plan_id"), "plan_id"),
                    base_commit=_required_string(
                        request.inputs.get("base_commit"), "base_commit"
                    ),
                    source_path=_required_string(
                        request.inputs.get("source_path"), "source_path"
                    ),
                    source_blob_sha=_required_string(
                        request.inputs.get("source_blob_sha"), "source_blob_sha"
                    ),
                    verification_command=_required_string(
                        request.inputs.get("verification_command"),
                        "verification_command",
                    ),
                )
            elif action == "load_session":
                issue_number = request.inputs.get("issue_number")
                if not isinstance(issue_number, int) or isinstance(issue_number, bool):
                    raise ValueError("issue_number must be an integer")
                session = load_planning_session(
                    store,
                    issue_number,
                    expected_repository=repository,
                )
            else:
                raise ValueError("plan operation action must create or load a session")
        except (
            OSError,
            ValueError,
            subprocess.SubprocessError,
            PlanningServiceError,
        ) as exc:
            return _failed(request.request_id, "planning_session_failed", str(exc))
        payload = json.dumps(
            session.to_data(), sort_keys=True, separators=(",", ":")
        ).encode()
        phase = (
            "planning_session_created"
            if action == "create_session"
            else "planning_session_loaded"
        )
        return OperationUpdate(
            operation_id=request.request_id,
            revision=1,
            status="succeeded",
            progress=({"phase": phase},),
            output_artifacts=(
                Artifact(
                    artifact_id="planning/session.json",
                    media_type="application/vnd.powdrr.planning-session+json",
                    data=payload,
                ),
            ),
        )

    def _prepare_plan_repair(self, request: OperationRequest) -> OperationUpdate:
        repository = request.repository
        if repository is None:
            return _failed(
                request.request_id,
                "invalid_request",
                "Repository snapshot is required.",
            )
        try:
            plan_path = _safe_relative_path(request.inputs.get("plan_path"))
            base_commit = _required_string(
                request.inputs.get("base_commit", repository.head_sha),
                "base_commit",
            )
            request_artifact_id = _required_string(
                request.inputs.get("repair_request_artifact"),
                "repair_request_artifact",
            )
            request_artifact = next(
                (
                    artifact
                    for artifact in request.artifacts
                    if artifact.artifact_id == request_artifact_id
                    and artifact.media_type
                    == "application/vnd.powdrr.plan-repair-request+json"
                ),
                None,
            )
            if request_artifact is None:
                raise ValueError("plan repair request artifact is missing")
            repair_request = json.loads(request_artifact.data)
            if not isinstance(repair_request, dict):
                raise ValueError("plan repair request must be a JSON object")
            with tempfile.TemporaryDirectory(
                prefix="powdrr-service-plan-repair-"
            ) as temp:
                root = Path(temp).resolve()
                _write_snapshot(root, request)
                _initialize_snapshot_git(root)
                from powdrr_service.planning.repair_service import (
                    apply_repair_request,
                    load_repair_draft,
                    prepare_repair_publication,
                )

                draft = load_repair_draft(root, root / plan_path)
                repaired = apply_repair_request(
                    draft,
                    repair_request,
                    base_commit=base_commit,
                )
                source_plan_path = draft.plan.source_document
                if not source_plan_path:
                    raise ValueError(
                        "reviewed plan does not reference its source document"
                    )
                latest_repair = repaired.manifest.get("latest_repair")
                if not isinstance(latest_repair, dict):
                    raise ValueError("repaired plan has no latest repair manifest")
                discovery_value = repair_request.get("discovery")
                if not isinstance(discovery_value, dict):
                    raise ValueError("repair discovery must be an object")
                bound_discovery = {
                    **discovery_value,
                    "id": latest_repair.get("repair_id"),
                }
                publication = prepare_repair_publication(
                    draft=draft,
                    repair=repaired,
                    plan_path=plan_path,
                    source_plan_path=source_plan_path,
                    discovery=bound_discovery,
                )
                publication["affected_item_ids"] = list(
                    latest_repair.get("affected_item_ids", [])
                )
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            return _failed(request.request_id, "plan_repair_failed", str(exc))
        payload = json.dumps(publication, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
        return OperationUpdate(
            operation_id=request.request_id,
            revision=1,
            status="succeeded",
            progress=({"phase": "plan_repair_validated"},),
            output_artifacts=(
                Artifact(
                    artifact_id="plan-repair/prepared-publication.json",
                    media_type="application/vnd.powdrr.plan-repair-publication+json",
                    data=payload,
                ),
            ),
        )

    def _inspect_workflows(self, request: OperationRequest) -> dict[str, Any]:
        repository = request.repository
        assert repository is not None
        with tempfile.TemporaryDirectory(prefix="powdrr-service-snapshot-") as temp:
            root = Path(temp).resolve()
            _write_snapshot(root, request, path_prefix="docs/workflows/")
            _initialize_snapshot_git(root)
            commit = _git(root, "rev-parse", "HEAD").strip()
            # Importing the legacy implementation is a temporary PR 2 migration
            # edge; it receives a service-owned snapshot directory, never the
            # agent's repository path or a callback into it.
            from powdrr_lift.artifact_state import build_artifact_state

            result = build_artifact_state(
                root,
                branch="snapshot",
                sha=commit,
                repository=repository.repository_id,
                include_github=False,
                areas=["workflows"],
            )
        result["branch"] = repository.branch
        result["sha"] = repository.head_sha
        return result

    def _bootstrap_structrr(self, request: OperationRequest) -> OperationUpdate:
        repository = request.repository
        if repository is None:
            return _failed(
                request.request_id,
                "invalid_request",
                "Repository snapshot is required.",
            )
        try:
            output_path = _safe_relative_path(request.inputs.get("output_path"))
            taxonomy_path = _safe_relative_path(
                request.inputs.get(
                    "taxonomy_path", "software_development_entity_taxonomy.md"
                )
            )
            change_id = _required_string(
                request.inputs.get("change_id", "bootstrap"), "change_id"
            )
            title = request.inputs.get("title")
            if title is not None and not isinstance(title, str):
                raise ValueError("title must be a string or null")
            repository_name = _required_string(
                request.inputs.get("repository_name", repository.repository_id),
                "repository_name",
            )
            with tempfile.TemporaryDirectory(
                prefix="powdrr-service-bootstrap-"
            ) as temp:
                root = Path(temp).resolve()
                _write_snapshot(root, request)
                _initialize_snapshot_git(root)
                from powdrr_lift.structrr.bootstrap import bootstrap_structrr

                result = bootstrap_structrr(
                    root,
                    output_path=output_path,
                    change_id=change_id,
                    title=title or f"Bootstrap Structrr for {repository_name}",
                    taxonomy_path=taxonomy_path,
                    submission_base=repository.head_sha,
                    source_revision=repository.head_sha,
                    repository_name=repository_name,
                )
                if not result.validation.successful:
                    issues = "; ".join(
                        f"{issue.code}: {issue.message}"
                        for issue in result.validation.issues
                    )
                    return _failed(
                        request.request_id,
                        "bootstrap_validation_failed",
                        issues or "Structrr bootstrap validation failed.",
                    )
                outputs = [
                    Artifact(
                        artifact_id=output_path,
                        media_type="application/yaml",
                        data=result.output_path.read_bytes(),
                    )
                ]
                if result.report_path is not None:
                    outputs.append(
                        Artifact(
                            artifact_id=result.report_path.relative_to(root).as_posix(),
                            media_type="text/markdown; charset=utf-8",
                            data=result.report_path.read_bytes(),
                        )
                    )
        except (ValueError, OSError, subprocess.SubprocessError) as exc:
            return _failed(request.request_id, "bootstrap_failed", str(exc))
        return OperationUpdate(
            operation_id=request.request_id,
            revision=1,
            status="succeeded",
            progress=({"phase": "structrr_bootstrap_validated"},),
            output_artifacts=tuple(outputs),
        )

    def _compile_structrr_diff(self, request: OperationRequest) -> OperationUpdate:
        try:
            documents: dict[str, dict[str, Any]] = {}
            for artifact in request.artifacts:
                if artifact.artifact_id not in {
                    "baseline/structrr.yaml",
                    "candidate/structrr.yaml",
                }:
                    raise ValueError(
                        "actual diff received an unexpected artifact reference"
                    )
                raw = yaml.safe_load(artifact.data)
                if not isinstance(raw, dict):
                    raise ValueError(
                        f"{artifact.artifact_id} must contain a YAML object"
                    )
                documents[artifact.artifact_id] = raw
            baseline = documents.get("baseline/structrr.yaml")
            candidate = documents.get("candidate/structrr.yaml")
            if baseline is None or candidate is None:
                raise ValueError(
                    "actual diff requires baseline and candidate snapshots"
                )
            baseline_manifest = baseline.get("source_manifest")
            candidate_manifest = candidate.get("source_manifest")
            if not isinstance(baseline_manifest, dict) or not isinstance(
                candidate_manifest, dict
            ):
                raise ValueError("both snapshots must include a source manifest")
            from powdrr_lift.structrr.actual_diff import compile_actual_structrr_diff
            from powdrr_lift.structrr.source_manifest import SourceManifest

            actual = compile_actual_structrr_diff(
                baseline,
                candidate,
                SourceManifest.from_data(baseline_manifest),
                SourceManifest.from_data(candidate_manifest),
            )
        except (ValueError, yaml.YAMLError) as exc:
            return _failed(request.request_id, "structrr_diff_failed", str(exc))
        payload = json.dumps(actual.to_data(), sort_keys=True).encode()
        return OperationUpdate(
            operation_id=request.request_id,
            revision=1,
            status="succeeded",
            progress=({"phase": "structrr_actual_diff_compiled"},),
            output_artifacts=(
                Artifact(
                    artifact_id="structrr/actual-diff.json",
                    media_type="application/vnd.powdrr.structrr-actual-diff+json",
                    data=payload,
                ),
            ),
        )

    def advance(self, operation_input: OperationInput) -> OperationUpdate:
        return _failed(
            operation_input.operation_id,
            "unsupported_operation",
            "No resumable operations are migrated yet.",
        )

    def inspect(self, reference: OperationReference) -> OperationUpdate:
        return _failed(
            reference.operation_id, "not_found", "Local operations are not persisted."
        )

    def cancel(self, reference: OperationReference) -> OperationUpdate:
        return _failed(
            reference.operation_id,
            "unsupported_operation",
            "Read-only inspection cannot be cancelled.",
        )

    def infer(self, request: ChatCompletionRequest) -> ChatCompletionResponse:
        return self._inference_client.complete(request)


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise ValueError(
            result.stderr.strip() or "service snapshot git operation failed"
        )
    return result.stdout


def _write_snapshot(
    root: Path, request: OperationRequest, *, path_prefix: str | None = None
) -> None:
    repository = request.repository
    if repository is None:
        raise ValueError("Repository snapshot is required.")
    source_artifacts = tuple(
        artifact
        for artifact in request.artifacts
        if artifact.media_type == "application/vnd.powdrr.source-file"
    )
    if len({item.artifact_id for item in source_artifacts}) != len(source_artifacts):
        raise ValueError("repository snapshot contains duplicate artifact paths")
    if {item.artifact_id for item in source_artifacts} != set(
        repository.source_artifact_refs
    ):
        raise ValueError("repository snapshot references do not match supplied files")
    if artifact_manifest_digest(source_artifacts) != repository.manifest_digest:
        raise ValueError(
            "repository snapshot manifest digest does not match supplied files"
        )
    pending_symlinks: list[tuple[Path, str]] = []
    root_resolved = root.resolve()
    for artifact in source_artifacts:
        relative = PurePosixPath(artifact.artifact_id)
        if (
            relative.is_absolute()
            or not relative.parts
            or ".." in relative.parts
            or "\\" in artifact.artifact_id
            or relative.parts[0] == ".git"
        ):
            raise ValueError(f"unsafe repository artifact path: {artifact.artifact_id}")
        if path_prefix is not None and not artifact.artifact_id.startswith(path_prefix):
            raise ValueError("workflow projection received a non-workflow artifact")
        destination = root.joinpath(*relative.parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            mode = artifact.attributes.get("git_mode", "100644")
            if mode == "120000":
                target = artifact.data.decode("utf-8", errors="strict")
                candidate = (destination.parent / target).resolve(strict=False)
                candidate.relative_to(root_resolved)
                pending_symlinks.append((destination, target))
            elif mode in {"100644", "100755"}:
                destination.write_bytes(artifact.data)
                destination.chmod(0o755 if mode == "100755" else 0o644)
            else:
                raise ValueError(f"unsupported Git file mode {mode!r}")
        except (UnicodeDecodeError, ValueError) as exc:
            raise ValueError(
                f"unsafe repository artifact {artifact.artifact_id}"
            ) from exc
    for destination, target in pending_symlinks:
        destination.symlink_to(target)
    if not source_artifacts:
        (root / ".powdrr-empty-snapshot").write_text("", encoding="utf-8")


def _initialize_snapshot_git(root: Path) -> None:
    _git(root, "init", "-b", "snapshot")
    _git(root, "config", "user.name", "Powdrr local service")
    _git(root, "config", "user.email", "service@localhost")
    _git(root, "add", "--all")
    _git(root, "commit", "-m", "reconstructed agent evidence snapshot")


def _safe_relative_path(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("artifact path must be a non-empty repository-relative string")
    path = PurePosixPath(value)
    if not path.parts or path.is_absolute() or ".." in path.parts or "\\" in value:
        raise ValueError(f"unsafe repository-relative path: {value}")
    return path.as_posix()


def _required_string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _failed(operation_id: str, category: str, message: str) -> OperationUpdate:
    return OperationUpdate(
        operation_id=operation_id,
        revision=0,
        status="failed",
        failure=ServiceError(
            category=category,
            message=message,
            retryable=False,
            correlation_id=operation_id,
        ),
    )
