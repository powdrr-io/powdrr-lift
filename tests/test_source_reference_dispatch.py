from __future__ import annotations

import json
import shlex
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
import yaml
from powdrr_contracts.models import ChatCompletionRequest, ChatCompletionResponse
from powdrr_service.decision_obligation import content_fingerprint
from powdrr_service.structrr.reference_definitions import ReferenceDefinition
from powdrr_service.structrr.source_run_lineage import build_source_run_lineage

from powdrr_lift.errors import PowdrrExecutionError
from powdrr_lift.workrr import source_reference_dispatch as dispatch
from powdrr_lift.workrr import task_agent
from powdrr_lift.workrr.coding_agent_validation import ValidationProfile
from powdrr_lift.workrr.source_reference_postrun import reconcile_source_task


def test_missing_validation_is_reported_as_structured_failure() -> None:
    result = dispatch._validation_result(
        {"validation": None, "error": "Source completion was invalid"}
    )

    assert result == {
        "status": "not_run",
        "passed": False,
        "error": "Source completion was invalid",
    }


@pytest.mark.parametrize(
    "health",
    [None, {"mode": "observe", "passed": True}, {"mode": "enforce", "passed": False}],
)
def test_story_dispatch_requires_passing_enforced_health(health: Any) -> None:
    result = {
        "passed": True,
        "story_coverage": {"verified": True},
        "verification_health": health,
    }
    assert dispatch._story_validation_is_accepted(result) is False


def test_story_dispatch_accepts_current_enforced_health() -> None:
    result = {
        "passed": True,
        "story_coverage": {"verified": True},
        "verification_health": {"mode": "enforce", "passed": True},
    }
    assert dispatch._story_validation_is_accepted(result) is True


BASELINE = "# structrr: feature:twice\ndef twice(value):\n    return value\n"
CANDIDATE = BASELINE.replace("return value", "return value * 2")


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, capture_output=True, check=True, text=True
    ).stdout.strip()


def fixture(tmp_path: Path) -> tuple[Path, str, dict[str, Any]]:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.invalid")
    (repo / "twice.py").write_text(BASELINE)
    (repo / ".gitignore").write_text(".powdrr/\n")
    document = {
        "schema_version": dispatch.DESIGN_SCHEMA,
        "plan_id": "plan-1",
        "item_id": "item-1",
        "design": {
            "reference_definitions": [
                ReferenceDefinition(
                    "feature:twice", "feature", "Return twice the input.", "spec:1"
                ).to_data()
            ],
            "source_reference_placements": [
                {
                    "obligation_id": "place:twice",
                    "ref": "feature:twice",
                    "relationship": "implements",
                    "requirement": "Return twice the input.",
                    "owner": {
                        "path": "twice.py",
                        "kind": "function",
                        "qualified_name": "twice",
                    },
                    "cardinality": "exactly_one",
                    "verification_refs": ["case:twice"],
                }
            ],
            "required_test_cases": [
                {
                    "id": "case:twice",
                    "behavior_scenario": {
                        "scenario_id": "twice-result",
                        "subject": "twice function",
                        "given": {"value": 3},
                        "when": "twice is called",
                        "then": {"result": 6},
                        "related_requirements": [],
                        "dimensions": {
                            "normal_result": {"result": "twice the input"},
                            "error_behavior": "not_applicable",
                            "continuation": "not_applicable",
                            "unsupported_behavior": "not_applicable",
                            "cancellation_cleanup": "not_applicable",
                            "compatibility": "not_applicable",
                            "negative_boundaries": "not_applicable",
                        },
                        "evidence": ["twice.py::twice"],
                        "validator": "focused twice behavior test",
                        "capability_matrix": [],
                        "faithfulness_ref": {"fingerprint": "sha256:" + "d" * 64},
                    },
                }
            ],
        },
    }
    (repo / "design.yaml").write_text(yaml.safe_dump(document))
    git(repo, "add", ".")
    git(repo, "commit", "-m", "reviewed design")
    proposed = {
        "plan_id": "plan-1",
        "item_id": "item-1",
        "source_reference_design_file": "design.yaml",
        "objective": "Implement twice.",
        "scope": ["Implement twice."],
        "allowed_paths": ["twice.py", "added.py"],
        "implementation_steps": ["Double the input."],
        "acceptance_criteria": ["The result is twice the input."],
        "verification_command": (
            "python -c 'from twice import twice; assert twice(3)==6'"
        ),
    }
    return repo, git(repo, "rev-parse", "HEAD"), proposed


def _write_minimal_lineage(private_root: Path, run_id: str) -> dict[str, Any]:
    private_root.mkdir(parents=True, exist_ok=True)
    record = build_source_run_lineage(
        run_id=run_id,
        lineage_context={"plan_id": "plan-1", "item_id": "item-1"},
        accepted_design={},
        handoff={
            "worker_prompt": {
                "fingerprint": "prompt-integrity",
                "contract": {
                    "base_commit": "a" * 40,
                    "design_fingerprint": "design-integrity",
                    "catalog_digest": "catalog-integrity",
                    "definitions": [],
                    "obligations": [],
                    "preservation": [],
                    "behavior_case_ids": {},
                },
            },
            "validation_manifest": {"fingerprint": "manifest-integrity"},
        },
        terminal={
            "outcome": "accepted",
            "validation": {
                "results": [
                    {
                        "profile": "behavior",
                        "status": "passed",
                        "fingerprint": "sha256:validation-evidence",
                    }
                ]
            },
            "obligation_route_evidence": {
                "candidate_tree": "candidate-tree-id",
                "routes": [],
            },
        },
    )
    (private_root / "lineage.json").write_text(json.dumps(record), encoding="utf-8")
    return record


def test_reviewed_base_may_be_ancestor_of_workflow_checkpoint_checkout(
    tmp_path: Path,
) -> None:
    repo, base_commit, _ = fixture(tmp_path)
    checkpoint = repo / "workflow-checkpoint.txt"
    checkpoint.write_text("persisted workflow state\n")
    git(repo, "add", "workflow-checkpoint.txt")
    git(repo, "commit", "-m", "persist workflow checkpoint")
    checkout_commit = git(repo, "rev-parse", "HEAD")

    assert dispatch._is_base_ancestor_of_checkout(
        repo, base_commit=base_commit, checkout_commit=checkout_commit
    )
    assert not dispatch._is_base_ancestor_of_checkout(
        repo, base_commit=checkout_commit, checkout_commit=base_commit
    )


class Reviewer:
    def complete_json(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        packet = json.loads(messages[-1]["content"])
        return {
            "classification": "satisfied",
            "explanation": "The function doubles its input.",
            "evidence_locations": packet["available_evidence"],
        }


def test_design_is_loaded_from_exact_committed_regular_blob(tmp_path: Path) -> None:
    repo, base, proposed = fixture(tmp_path)
    (repo / "design.yaml").write_text("unreviewed candidate changes")
    path, design = dispatch.load_reviewed_source_design(
        repo, proposed, base_commit=base
    )
    assert path == "design.yaml"
    assert design["source_reference_placements"][0]["ref"] == "feature:twice"
    with pytest.raises(ValueError, match="reviewed plan item"):
        dispatch.load_reviewed_source_design(
            repo, {**proposed, "item_id": "other"}, base_commit=base
        )
    with pytest.raises(ValueError, match="repository-relative"):
        dispatch.load_reviewed_source_design(
            repo,
            {**proposed, "source_reference_design_file": "../design.yaml"},
            base_commit=base,
        )
    (repo / "link.yaml").symlink_to("design.yaml")
    git(repo, "add", "link.yaml")
    git(repo, "commit", "-m", "link")
    with pytest.raises(ValueError, match="regular committed file"):
        dispatch.load_reviewed_source_design(
            repo,
            {**proposed, "source_reference_design_file": "link.yaml"},
            base_commit=git(repo, "rev-parse", "HEAD"),
        )


def test_task_routes_before_legacy_authorship_and_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, base, proposed = fixture(tmp_path)
    calls = []
    monkeypatch.setattr(task_agent, "_workflow_base_commit", lambda *a, **k: base)
    monkeypatch.setattr(
        task_agent, "_preflight_reviewed_file_creation_conflicts", lambda *a, **k: ()
    )
    monkeypatch.setattr(
        task_agent,
        "_issue_worker_structrr_context",
        lambda *a, **k: pytest.fail("legacy dispatch"),
    )
    expected = {"passed": False, "retryable": False, "continuations": 0}

    def execute(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return expected

    monkeypatch.setattr(dispatch, "dispatch_source_reference_task", execute)
    strategy = SimpleNamespace(
        task=SimpleNamespace(
            task_id="implementation",
            input_state={
                "proposed_pr_context": proposed,
                "proposed_pr_id": "workflow-1",
            },
        ),
        workflow=SimpleNamespace(directory=repo / ".powdrr"),
        repo_root=repo,
        config=task_agent.WorkflowTaskAgentConfig(workflow_dir=repo / ".powdrr"),
        client=Reviewer(),
        events=[],
    )
    typed_strategy = cast(task_agent._TaskWorkflowExecutionStrategy, strategy)
    result = task_agent._TaskWorkflowExecutionStrategy._run_coding_agent(typed_strategy)
    assert result is expected
    assert len(calls) == 1
    assert calls[0]["allowed_paths"] == ("twice.py", "added.py")
    assert strategy.events[-1]["result"] is expected
    del proposed["source_reference_design_file"]
    proposed["source_reference_placements"] = []
    with pytest.raises(PowdrrExecutionError, match="reviewed design file"):
        task_agent._TaskWorkflowExecutionStrategy._run_coding_agent(typed_strategy)
    proposed.pop("source_reference_placements")
    with pytest.raises(
        PowdrrExecutionError, match="Legacy issue-worker implementation"
    ):
        task_agent._TaskWorkflowExecutionStrategy._run_coding_agent(typed_strategy)


def test_story_implementation_dispatch_requires_validated_context(
    tmp_path: Path,
) -> None:
    repo, base, proposed = fixture(tmp_path)
    proposed["story_refs"] = [{"story_id": "story:resume"}]
    arguments: dict[str, Any] = {
        "repo_root": repo,
        "proposed": proposed,
        "base_commit": base,
        "workflow_id": "workflow-1",
        "task_id": "implementation",
        "objective": "Implement the pinned behavior.",
        "allowed_paths": ("twice.py",),
        "profile": ValidationProfile("behavior", (sys.executable, "-c", "pass")),
        "code_agent": "minisweagent",
        "model": "test",
        "worker_timeout_seconds": 30,
        "validation_timeout_seconds": 30,
        "review_client": Reviewer(),
    }

    with pytest.raises(PowdrrExecutionError, match="validated pinned story context"):
        dispatch.dispatch_source_reference_task(**arguments)

    with pytest.raises(
        PowdrrExecutionError, match="requires independently authored tests"
    ):
        full_validation: dict[str, Any] = dict(arguments)
        full_validation["proposed"] = {**proposed, "story_validation": True}
        dispatch.dispatch_source_reference_task(
            **full_validation,
            story_context={"story_id": "story:resume"},
        )


def test_story_context_is_bound_into_protected_source_objective(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, base, proposed = fixture(tmp_path)
    proposed["story_refs"] = [{"story_id": "story:resume"}]
    captured: dict[str, Any] = {}

    class Run:
        private_root = tmp_path / "private"
        public_root = tmp_path / "public"
        run_id = "implementation"

        def __init__(self) -> None:
            self.public_root.mkdir()

        def execute(self, _handler: Any) -> Any:
            record = _write_minimal_lineage(self.private_root, self.run_id)
            return SimpleNamespace(
                accepted=True,
                to_data=lambda: {
                    "outcome": "accepted",
                    "attempt": {
                        "attempt_id": "run-1",
                        "status": "completed",
                        "changed_paths": [],
                    },
                    "validation": {"status": "passed"},
                    "error": None,
                    "lineage_record_fingerprint": content_fingerprint(record),
                },
            )

        def verify_candidate(self) -> None:
            return None

        def bind_story_validation(
            self, _story_validation: dict[str, Any]
        ) -> dict[str, Any]:
            return {
                "complete": True,
                "routes": [],
                "reconciliation": {"passed": True, "issues": []},
            }

        def protect_command(self, command: tuple[str, ...]) -> tuple[str, ...]:
            return command

        def validation_environment(self) -> dict[str, str]:
            return {}

    def prepare(**kwargs: Any) -> Run:
        captured.update(kwargs)
        return Run()

    def fake_git(_root: Path, *arguments: str, data: bytes | None = None) -> bytes:
        if arguments[:2] == ("rev-parse", "HEAD"):
            return base.encode()
        if arguments[0] == "rev-parse" and ":" in arguments[1]:
            return b"source-design-blob-id"
        if arguments[0] in {"diff", "ls-files", "status", "apply", "worktree"}:
            return b""
        pytest.fail(f"unexpected git command: {arguments}")

    monkeypatch.setattr(
        dispatch, "load_reviewed_source_design", lambda *_a, **_k: ("design.yaml", {})
    )
    monkeypatch.setattr(dispatch, "_git", fake_git)
    monkeypatch.setattr(dispatch.SourceReferenceRun, "prepare", prepare)
    monkeypatch.setattr(dispatch, "create_miniswe_inference_handler", lambda _t: None)
    monkeypatch.setattr(dispatch.tempfile, "gettempdir", lambda: str(tmp_path / "tmp"))
    monkeypatch.setattr(
        dispatch,
        "execute_story_validation",
        lambda *_a, **kwargs: {
            "passed": True,
            "story_coverage": {"verified": True},
            "verification_health": {"mode": "enforce", "passed": True},
            "coverage_evidence_error": None,
        },
    )
    story_context = {
        "stories": [{"id": "story:resume", "contract_digest": "sha256:abc"}],
        "cases": [{"id": "story:resume:case:1"}],
    }

    result = dispatch.dispatch_source_reference_task(
        repo_root=repo,
        proposed={**proposed, "story_validation": True},
        base_commit=base,
        workflow_id="workflow-1",
        task_id="implementation",
        objective="Implement the pinned behavior.",
        allowed_paths=("twice.py",),
        profile=ValidationProfile("behavior", (sys.executable, "-c", "pass")),
        code_agent="minisweagent",
        model="test",
        worker_timeout_seconds=30,
        validation_timeout_seconds=30,
        review_client=Reviewer(),
        story_context=story_context,
        story_validation_bundle={
            "snapshot": {"user_stories": []},
            "test_sources": {"tests/test_story.py": "def test_story(): pass\n"},
            "authoring": {
                "story_test_links": {"links": [{"selector": "test_story"}]},
                "accepted_story_review": {},
                "story_review_evidence": {},
                "test_sha256": dispatch.fingerprint_test_files(
                    {"tests/test_story.py": "def test_story(): pass\n"}
                ),
            },
        },
    )

    assert result["passed"] is True, result.get("error")
    assert result["obligation_route_evidence"]["complete"] is True
    assert result["obligation_route_evidence_path"].endswith(
        "story-route-evidence.json"
    )
    assert "Pinned customer story contract and selected cases" in captured["objective"]
    assert "story:resume:case:1" in captured["objective"]
    assert captured["readonly_support_files"] == {
        "tests/test_story.py": "def test_story(): pass\n"
    }
    assert "story-validation" in captured["profiles"]
    assert callable(captured["post_worker_reconciler"])


def test_failed_source_invocation_retries_only_from_a_new_reviewed_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, base, proposed = fixture(tmp_path)
    artifact_roots: list[Path] = []
    invocations: list[int] = []

    class Run:
        def __init__(
            self, invocation: int, artifact_root: Path, workspace_root: Path
        ) -> None:
            self.invocation = invocation
            self.run_id = "implementation"
            self.private_root = tmp_path / "private" / str(invocation)
            self.public_root = tmp_path / "public" / str(invocation)
            self.artifact_root = artifact_root
            self.workspace_root = workspace_root

        def execute(self, _handler: Any) -> Any:
            invocations.append(self.invocation)
            accepted = self.invocation == 1
            if accepted:
                record = _write_minimal_lineage(self.private_root, self.run_id)
                (self.workspace_root / "twice.py").write_text(CANDIDATE)
            else:
                record = None
            return SimpleNamespace(
                accepted=accepted,
                to_data=lambda: {
                    "outcome": "accepted" if accepted else "validation_failed",
                    "attempt": {
                        "attempt_id": f"run-{self.invocation}",
                        "status": "completed",
                        "changed_paths": ["twice.py"] if accepted else [],
                    },
                    "validation": {"status": "passed" if accepted else "failed"},
                    "error": None if accepted else "verification failed",
                    "lineage_record_fingerprint": (
                        content_fingerprint(record) if record is not None else None
                    ),
                    "obligation_route_evidence": (
                        {
                            "complete": True,
                            "reconciliation": {"passed": True, "issues": []},
                        }
                        if accepted
                        else None
                    ),
                },
            )

        def verify_candidate(self) -> None:
            return None

    def prepare(**kwargs: Any) -> Run:
        invocation = len(artifact_roots)
        artifact_roots.append(Path(kwargs["artifact_root"]))
        return Run(
            invocation,
            artifact_roots[-1],
            Path(kwargs["workspace_root"]),
        )

    monkeypatch.setattr(dispatch.SourceReferenceRun, "prepare", prepare)
    monkeypatch.setattr(dispatch.tempfile, "gettempdir", lambda: str(tmp_path))
    arguments: dict[str, Any] = {
        "repo_root": repo,
        "proposed": proposed,
        "base_commit": base,
        "workflow_id": "workflow-1",
        "task_id": "implementation",
        "objective": "Double the input.",
        "allowed_paths": ("twice.py",),
        "profile": ValidationProfile("behavior", (sys.executable, "-c", "pass")),
        "code_agent": "minisweagent",
        "model": "test",
        "worker_timeout_seconds": 30,
        "validation_timeout_seconds": 30,
        "review_client": Reviewer(),
    }

    failed = dispatch.dispatch_source_reference_task(**arguments)
    assert failed["passed"] is False
    assert failed["source_reference_outcome"] == "validation_failed"
    with pytest.raises(PowdrrExecutionError, match="already dispatched"):
        dispatch.dispatch_source_reference_task(**arguments)
    assert invocations == [0]

    revised_design = yaml.safe_load((repo / "design.yaml").read_text())
    revised_design["plan_id"] = "plan-2"
    revised_design["item_id"] = "item-2"
    (repo / "design.yaml").write_text(yaml.safe_dump(revised_design))
    git(repo, "add", "design.yaml")
    git(repo, "commit", "-m", "review revised implementation design")
    revised_arguments = {
        **arguments,
        "proposed": {**proposed, "plan_id": "plan-2", "item_id": "item-2"},
        "base_commit": git(repo, "rev-parse", "HEAD"),
    }

    retried = dispatch.dispatch_source_reference_task(**revised_arguments)
    assert retried["passed"] is True, retried.get("error")
    assert len(set(artifact_roots)) == 2
    assert invocations == [0, 1]
    assert Path(failed["source_reference_artifact_root"], "dispatch.json").is_file()
    assert Path(retried["source_reference_artifact_root"], "dispatch.json").is_file()
    with pytest.raises(PowdrrExecutionError, match="already dispatched"):
        dispatch.dispatch_source_reference_task(**revised_arguments)
    assert invocations == [0, 1]


@pytest.mark.parametrize(
    "tamper_kind",
    [
        "missing_link",
        "base_commit",
        "design_fingerprint",
        "bundle_fingerprint",
        "attempt_id",
        "candidate_identity",
        "evidence_fingerprint",
    ],
)
def test_dispatch_blocks_patch_when_stable_lineage_record_is_tampered(
    tamper_kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, base, proposed = fixture(tmp_path)
    candidate_roots: list[Path] = []
    invocation_count = 0

    class Run:
        run_id = "implementation"

        def __init__(self, artifact_root: Path, workspace_root: Path) -> None:
            self.private_root = artifact_root / self.run_id / "private"
            self.private_root.mkdir(parents=True)
            self.workspace_root = workspace_root

        def execute(self, _handler: Any) -> Any:
            nonlocal invocation_count
            invocation_count += 1
            (self.workspace_root / "twice.py").write_text(CANDIDATE)
            original_record = _write_minimal_lineage(self.private_root, self.run_id)
            tampered_record = json.loads(
                (self.private_root / "lineage.json").read_text()
            )
            if tamper_kind == "missing_link":
                tampered_record["links"] = [
                    link
                    for link in tampered_record["links"]
                    if link["relation"] != "invoked"
                ]
            else:
                artifact_kind, attribute = {
                    "base_commit": ("accepted_design", "base_commit"),
                    "design_fingerprint": (
                        "accepted_design",
                        "design_fingerprint",
                    ),
                    "bundle_fingerprint": (
                        "compiled_handoff",
                        "bundle_fingerprint",
                    ),
                    "attempt_id": ("worker_attempt", "attempt_id"),
                    "candidate_identity": ("candidate", "tree_identity"),
                    "evidence_fingerprint": (
                        "validation_evidence",
                        "evidence_fingerprint",
                    ),
                }[tamper_kind]
                artifact = next(
                    item
                    for item in tampered_record["artifacts"]
                    if item["kind"] == artifact_kind
                )
                artifact["attributes"][attribute] = "tampered-value"
            (self.private_root / "lineage.json").write_text(json.dumps(tampered_record))
            return SimpleNamespace(
                accepted=True,
                to_data=lambda: {
                    "outcome": "accepted",
                    "attempt": {
                        "attempt_id": self.run_id,
                        "status": "completed",
                        "changed_paths": ["twice.py"],
                    },
                    "validation": {"status": "passed"},
                    "error": None,
                    "lineage_record_fingerprint": content_fingerprint(original_record),
                    "obligation_route_evidence": {
                        "complete": True,
                        "reconciliation": {"passed": True, "issues": []},
                    },
                },
            )

        def verify_candidate(self) -> None:
            return None

    def prepare(**kwargs: Any) -> Run:
        artifact_root = Path(kwargs["artifact_root"])
        candidate_roots.append(artifact_root)
        return Run(artifact_root, Path(kwargs["workspace_root"]))

    monkeypatch.setattr(dispatch.SourceReferenceRun, "prepare", prepare)
    monkeypatch.setattr(dispatch.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(dispatch, "create_miniswe_inference_handler", lambda _t: None)
    result = dispatch.dispatch_source_reference_task(
        repo_root=repo,
        proposed=proposed,
        base_commit=base,
        workflow_id="workflow-1",
        task_id="implementation",
        objective="Double the input.",
        allowed_paths=("twice.py",),
        profile=ValidationProfile("behavior", (sys.executable, "-c", "pass")),
        code_agent="minisweagent",
        model="test",
        worker_timeout_seconds=30,
        validation_timeout_seconds=30,
        review_client=Reviewer(),
    )

    assert result["passed"] is False
    assert result["status"] == "lineage_failed"
    assert result["lineage_audit"]["passed"] is False
    assert result["error"] == "Source run lineage integrity check failed"
    assert invocation_count == 1
    assert git(repo, "rev-parse", "HEAD") == base
    assert (repo / "twice.py").read_text() == BASELINE
    assert not (repo / "added.py").exists()


@pytest.mark.parametrize(
    (
        "initial_source",
        "has_accepted_placement",
        "response",
        "expected_status",
        "expected_source",
    ),
    [
        (
            "def twice(value):\n    return value\n",
            True,
            {"status": "resolved", "value": "exists", "reason_code": None},
            "passed",
            "# structrr: feature:twice\ndef twice(value):\n    return value\n",
        ),
        (
            "def twice(value):\n    return value\n",
            True,
            {"status": "resolved", "value": "absent", "reason_code": None},
            "blocked",
            "def twice(value):\n    return value\n",
        ),
        (
            "# structrr: feature:twice\ndef twice(value):\n    return value\n",
            False,
            {"status": "resolved", "value": "absent", "reason_code": None},
            "passed",
            "def twice(value):\n    return value\n",
        ),
    ],
)
def test_post_worker_reconciliation_uses_decision_and_changes_only_annotation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    initial_source: str,
    has_accepted_placement: bool,
    response: dict[str, Any],
    expected_status: str,
    expected_source: str,
) -> None:
    monkeypatch.setattr(
        "powdrr_lift.workrr.source_reference_postrun.DecisionProviderFactory.create",
        lambda *_args, **_kwargs: None,
    )
    (tmp_path / "twice.py").write_text(initial_source)
    definition = ReferenceDefinition(
        "feature:twice", "feature", "Return twice the input.", "spec:1"
    )
    placement = {
        "obligation_id": "place:twice",
        "ref": "feature:twice",
        "relationship": "implements",
        "requirement": "Return twice the input.",
        "owner": {
            "path": "twice.py",
            "kind": "function",
            "qualified_name": "twice",
            "binding": "exact",
            "role": "implementation",
        },
        "cardinality": "exactly_one",
    }
    contract = {
        "obligations": [placement] if has_accepted_placement else [],
        "preservation": [],
        "accepted_document_digest": "sha256:" + "a" * 64,
    }
    prompt = SimpleNamespace(to_data=lambda: {"contract": contract})
    run = SimpleNamespace(
        workspace_root=tmp_path,
        private_root=tmp_path / "private",
        handoff=SimpleNamespace(worker_prompt=prompt),
        catalog=SimpleNamespace(
            entries=(SimpleNamespace(reference=definition),),
        ),
        _last_attempt=SimpleNamespace(changed_paths=("twice.py",)),
    )

    class DecisionReviewer:
        def complete_json(
            self, _messages: list[dict[str, str]], *, response_schema: Any = None
        ) -> dict[str, Any]:
            return response

    receipt = reconcile_source_task(run, DecisionReviewer())
    assert receipt["status"] == expected_status
    assert (tmp_path / "twice.py").read_text() == expected_source
    if expected_status == "passed":
        assert (
            receipt["decisions"][0]["attempts"][0]["decision"]["result"]["value"]
            == response["value"]
        )


@pytest.mark.parametrize(
    "scenario",
    [
        "success",
        "behavior_failure",
        "parent_conflict",
        "preexisting_parent_no_progress",
    ],
)
def test_native_dispatch_validates_before_applying_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scenario: str
) -> None:
    if sys.platform == "linux" and not Path("/usr/bin/bwrap").is_file():
        pytest.skip("Native Linux source dispatch requires bubblewrap")
    if sys.platform not in {"darwin", "linux"}:
        pytest.skip("Native source dispatch requires macOS or Linux")
    repo, base, proposed = fixture(tmp_path)
    monkeypatch.setattr(dispatch.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setenv("MSWEA_COST_TRACKING", "ignore_errors")
    runtime = repo / ".powdrr" / "state.json"
    runtime.parent.mkdir()
    runtime.write_text("workflow state")
    preexisting_parent_edit = "pre-existing caller change\n"
    if scenario == "preexisting_parent_no_progress":
        (repo / "twice.py").write_text(preexisting_parent_edit)
    requests = []

    def infer(request: ChatCompletionRequest) -> ChatCompletionResponse:
        requests.append(request)
        candidate = next((tmp_path / "powdrr-source-tasks").glob("*/candidate"))
        if scenario == "parent_conflict":
            (repo / "twice.py").write_text("concurrent parent edit\n")
        if scenario == "preexisting_parent_no_progress":
            assert (candidate / "twice.py").read_text() == BASELINE
        completion = {
            "schema_version": "structrr-reference-completion-v1",
            "reference_owners": [
                {
                    "ref": "feature:twice",
                    "path": "twice.py",
                    "kind": "function",
                    "qualified_name": "twice",
                }
            ],
            "design_gaps": [],
        }
        edit = (
            "from pathlib import Path; Path('twice.py').write_text("
            + repr(CANDIDATE)
            + "); Path('added.py').write_text('VALUE = 1\\n')"
        )
        commands = [
            "pwd; git rev-parse --show-toplevel",
            "python -m powdrr_agent.execution.source_reader read twice.py",
            "python -c " + shlex.quote(edit),
            "printf '%s\\n' COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT "
            + shlex.quote(json.dumps(completion)),
        ]
        if scenario == "preexisting_parent_no_progress":
            commands[2] = "true"
        assert str(candidate) in request.messages[1]["content"]
        call = {
            "id": str(len(requests)),
            "type": "function",
            "function": {
                "name": "bash",
                "arguments": json.dumps({"command": commands[len(requests) - 1]}),
            },
        }
        return ChatCompletionResponse(
            content=None, tool_calls=(call,), finish_reason="tool_calls"
        )

    monkeypatch.setattr(
        dispatch, "create_miniswe_inference_handler", lambda timeout: infer
    )
    parameters: dict[str, Any] = dict(
        repo_root=repo,
        proposed=proposed,
        base_commit=base,
        workflow_id="workflow-1",
        task_id="implementation",
        objective="Double the input.",
        allowed_paths=("twice.py", "added.py"),
        profile=ValidationProfile(
            "behavior",
            (
                sys.executable,
                "-c",
                "from twice import twice; assert twice(3)=="
                + ("7" if scenario == "behavior_failure" else "6"),
            ),
        ),
        code_agent="minisweagent",
        model="test",
        worker_timeout_seconds=30,
        validation_timeout_seconds=30,
        review_client=Reviewer(),
    )
    result = dispatch.dispatch_source_reference_task(**parameters)
    if any(
        message in (result.get("error") or "")
        for message in (
            "sandbox_apply: Operation not permitted",
            "No permissions to create a new namespace",
        )
    ):
        pytest.skip("Outer environment prevents nested native sandbox")
    # This fixture has no protected story receipts, so its behavior route cannot
    # be reconciled and the candidate must not transfer to the task checkout.
    assert result["passed"] is False, result
    assert result["continuations"] == 0
    assert not result["retryable"]
    assert len(requests) == 4, result
    assert runtime.read_text() == "workflow state"
    assert git(repo, "rev-parse", "HEAD") == base
    if scenario == "parent_conflict":
        assert (repo / "twice.py").read_text() == "concurrent parent edit\n"
        assert not (repo / "added.py").exists()
    elif scenario == "preexisting_parent_no_progress":
        assert result["status"] == "submitted_without_progress", result
        assert result["attempts"][0]["attempt"]["changed_paths"] == []
        assert (repo / "twice.py").read_text() == preexisting_parent_edit
        assert not (repo / "added.py").exists()
    else:
        assert (repo / "twice.py").read_text() == BASELINE
        assert not (repo / "added.py").exists()
        if scenario == "success":
            assert result["status"] == "route_evidence_incomplete"
        elif scenario == "behavior_failure":
            assert result["status"] == "validation_failed", result
    with pytest.raises(PowdrrExecutionError, match="already dispatched"):
        dispatch.dispatch_source_reference_task(**parameters)
