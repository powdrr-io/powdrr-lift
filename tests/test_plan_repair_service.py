from __future__ import annotations

import argparse
import json
import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from powdrr_contracts.models import (
    Artifact,
    OperationRequest,
    RepositorySnapshot,
    artifact_manifest_digest,
)
from powdrr_local import JsonServiceBridge, LocalPowdrrService
from powdrr_local.planning.repair_publication import (
    RepairPublicationError,
    publish_repair_pull_request,
)
from powdrr_service.planning.repair import RepairDraft, item_content_digest
from powdrr_service.planning.repair_service import (
    PlanRepairServiceError,
    apply_repair_request,
    load_repair_draft,
    prepare_repair_publication,
)

from powdrr_lift.workrr.github_issue_queue import PlannedWorkItem, ReviewedExecutionPlan


def _item(item_id: str, title: str | None = None) -> PlannedWorkItem:
    return PlannedWorkItem(
        item_id=item_id,
        title=title or f"Implement {item_id}",
        objective=f"Deliver {item_id}.",
        scope=(f"Scope for {item_id}.",),
        acceptance_criteria=(f"{item_id} works.",),
        dependencies=(),
        workflow_directory=None,
        workflow_id=f"plan-{item_id}",
        workflow_file=f"docs/workflows/plan/{item_id}.yaml",
        implementation_steps=(f"Implement {item_id}.",),
        allowed_paths=("src/",),
    )


def _draft() -> RepairDraft:
    items = (_item("item-01"), _item("item-02"))
    manifest: dict[str, Any] = {
        "schema": "https://powdrr.io/schemas/planning-manifest-v1",
        "source": {"path": "docs/plan.md"},
        "base": {"commit_sha": "a" * 40},
        "requirements": [
            {"id": "req-01", "statement": "Behavior", "source_refs": ["anchor-01"]},
            {
                "id": "req-02",
                "statement": "Other behavior",
                "source_refs": ["anchor-02"],
            },
        ],
        "coverage": [
            {"requirement_id": "req-01", "status": "missing"},
            {"requirement_id": "req-02", "status": "missing"},
        ],
        "units": [
            {"id": "unit-01", "requirement_ids": ["req-01"]},
            {"id": "unit-02", "requirement_ids": ["req-02"]},
        ],
        "item_unit_ids": {"item-01": ["unit-01"], "item-02": ["unit-02"]},
        "validations": [],
        "evidence": [],
        "setups": [],
        "structrr": {"proposal_path": "docs/plans/plan-structrr-proposal.json"},
        "latest_repair": None,
    }
    return RepairDraft(
        ReviewedExecutionPlan("plan", items, "docs/plan.md", manifest),
        manifest,
        {
            "docs/plans/plan-execution-plan.yaml": "original plan file",
            "docs/plans/plan-structrr-proposal.json": "original proposal file",
            "docs/workflows/plan/item-01.yaml": item_content_digest(items[0]),
            "docs/workflows/plan/item-02.yaml": item_content_digest(items[1]),
        },
    )


def _compile(plan: ReviewedExecutionPlan, _manifest: Any) -> dict[str, str]:
    return {
        "docs/plans/plan-execution-plan.yaml": "recompiled plan",
        "docs/plans/plan-structrr-proposal.json": "original proposal file",
        **{item.workflow_file or "": item_content_digest(item) for item in plan.items},
    }


def _request() -> dict[str, Any]:
    return {
        "discovery": {
            "kind": "scope_defect",
            "summary": "The reviewed title names the wrong deliverable.",
            "requirement_ids": [],
            "item_ids": ["item-01"],
            "evidence_ids": ["evidence-01"],
            "requested_outcome": (
                "Correct the item title without changing its allocation."
            ),
        },
        "subjects": [{"subject_kind": "item", "reason": "Wrong title."}],
        "operations": [
            {
                "operation": "update_item",
                "item_id": "item-01",
                "reason": "Evidence proves the title is wrong.",
            }
        ],
        "replacement_items": {
            "item-01": {
                "title": "Implement the correct behavior",
                "objective": "Deliver item-01.",
                "scope": ["Scope for item-01."],
                "acceptance_criteria": ["item-01 works."],
            }
        },
        "replacement_units": [],
        "replacement_requirements": {},
    }


def test_shared_repair_service_binds_cas_and_preserves_unrelated_item() -> None:
    draft = _draft()
    repaired = apply_repair_request(draft, _request(), compile_candidate=_compile)
    old = {item.item_id: item_content_digest(item) for item in draft.plan.items}
    new = {item.item_id: item_content_digest(item) for item in repaired.plan.items}
    assert new["item-01"] != old["item-01"]
    assert new["item-02"] == old["item-02"]
    assert (
        repaired.artifacts["docs/workflows/plan/item-02.yaml"]
        == draft.artifacts["docs/workflows/plan/item-02.yaml"]
    )
    assert repaired.manifest["latest_repair"]["affected_item_ids"] == ["item-01"]
    assert repaired.manifest["latest_repair"]["repair_id"].startswith("repair-")


def test_shared_repair_service_rejects_model_supplied_cas_digest() -> None:
    request = _request()
    request["operations"][0]["expected_digest"] = "sha256:" + "0" * 64
    with pytest.raises(PlanRepairServiceError, match="cannot supply compare-and-swap"):
        apply_repair_request(_draft(), request, compile_candidate=_compile)


def test_shared_repair_service_rejects_stale_bound_plan_reference() -> None:
    request = _request()
    request["discovery"]["item_ids"] = ["unknown-item"]
    with pytest.raises(PlanRepairServiceError, match="unknown item"):
        apply_repair_request(_draft(), request, compile_candidate=_compile)


def test_shared_repair_service_compiles_real_reviewed_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from powdrr_service.planning.compiler import compile_plan
    from test_planning_compiler import _fixture

    from powdrr_lift import cli

    plan, manifest, proposal = _fixture()
    plan = replace(plan, planning=manifest.to_data())
    compiled = compile_plan(
        plan=plan,
        planning_manifest=manifest,
        repository_snapshot={
            "source": manifest.data["source"],
            "base": manifest.data["base"],
        },
        structrr_proposal=proposal,
        verification_command="pytest -q",
        plan_path="docs/plans/bounded-plan-execution-plan.yaml",
    )
    draft = RepairDraft(compiled.plan, manifest.to_data(), compiled.files)
    for path, content in compiled.files.items():
        artifact = tmp_path / path
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text(content, encoding="utf-8")
    loaded = load_repair_draft(
        tmp_path, tmp_path / "docs/plans/bounded-plan-execution-plan.yaml"
    )
    assert loaded.plan == draft.plan
    draft = loaded
    request = _request()
    request["discovery"]["item_ids"] = ["item-01"]
    request["replacement_items"] = {
        "item-01": {
            "title": "Implement the corrected requirement",
            "objective": "Add the required behavior.",
            "scope": ["Implement the behavior."],
            "acceptance_criteria": ["The behavior is covered."],
        }
    }

    repaired = apply_repair_request(draft, request)

    assert repaired.plan.items[0].title == "Implement the corrected requirement"
    assert repaired.artifacts[compiled.plan_path] != compiled.files[compiled.plan_path]
    workflow = repaired.artifacts["docs/workflows/bounded-plan/item-01.yaml"]
    assert "Implement the corrected requirement" in workflow

    source_artifacts = tuple(
        Artifact(path, "application/vnd.powdrr.source-file", content.encode("utf-8"))
        for path, content in compiled.files.items()
    )
    request_artifact = Artifact(
        "plan-repair/request.json",
        "application/vnd.powdrr.plan-repair-request+json",
        json.dumps(request).encode("utf-8"),
    )
    manifest_digest = artifact_manifest_digest(source_artifacts)
    operation = OperationRequest(
        request_id="replan-e2e",
        operation_kind="replan",
        inputs={
            "plan_path": compiled.plan_path,
            "base_commit": "c" * 40,
            "repair_request_artifact": request_artifact.artifact_id,
        },
        repository=RepositorySnapshot(
            repository_id="owner/repo",
            branch="main",
            base_sha=None,
            head_sha="c" * 40,
            manifest_digest=manifest_digest,
            worktree_digest=manifest_digest,
            source_artifact_refs=tuple(
                artifact.artifact_id for artifact in source_artifacts
            ),
        ),
        artifacts=(*source_artifacts, request_artifact),
    )

    update = JsonServiceBridge(LocalPowdrrService()).start(operation)

    assert update.status == "succeeded"
    assert update.operation_id == "replan-e2e"
    assert update.progress == ({"phase": "plan_repair_validated"},)
    assert len(update.output_artifacts) == 1
    publication = json.loads(update.output_artifacts[0].data)
    assert publication["plan_id"] == "bounded-plan"
    assert publication["affected_item_ids"] == ["item-01"]
    assert publication["changed"] == [
        "docs/plans/bounded-plan-execution-plan.yaml",
        "docs/workflows/bounded-plan/item-01.yaml",
    ]

    request_path = tmp_path / "repair-request.json"
    request_path.write_text(json.dumps(request), encoding="utf-8")
    subprocess.run(["git", "init", "-b", "main", str(tmp_path)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "add",
            ".",
        ],
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-m",
            "fixture",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    published: dict[str, Any] = {}

    def publish(*, repo_root: Path, publication: Any) -> tuple[str, tuple[str, ...]]:
        published["repo_root"] = repo_root
        published["publication"] = publication
        return "https://github.com/owner/repo/pull/1", tuple(publication["changed"])

    monkeypatch.setattr(cli, "_discover_github_repository", lambda _root: "owner/repo")
    monkeypatch.setattr(cli, "publish_repair_pull_request", publish)
    result = cli._run_plan_repair(
        argparse.Namespace(
            repo_root=tmp_path,
            plan=Path(compiled.plan_path),
            request=request_path,
            json=True,
        )
    )

    cli_output = json.loads(capsys.readouterr().out)
    assert result == 0
    assert cli_output["plan_id"] == "bounded-plan"
    assert cli_output["affected_item_ids"] == ["item-01"]
    assert cli_output["pull_request_url"] == "https://github.com/owner/repo/pull/1"
    assert published["repo_root"] == tmp_path
    assert published["publication"]["changed"] == publication["changed"]


def test_repair_publisher_branches_from_fresh_main_and_limits_commit_paths(
    tmp_path: Path,
) -> None:
    draft = _draft()
    repaired = apply_repair_request(draft, _request(), compile_candidate=_compile)
    calls: list[list[str]] = []

    def runner(arguments: list[str], **_kwargs: Any) -> SimpleNamespace:
        calls.append(arguments)
        stdout = (
            "https://github.com/example/repo/pull/123"
            if arguments[:3] == ["gh", "pr", "create"]
            else "a" * 40
        )
        if arguments[:3] == ["git", "status", "--porcelain"]:
            stdout = ""
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    publication = prepare_repair_publication(
        draft=draft,
        repair=repaired,
        plan_path="docs/plans/plan-execution-plan.yaml",
        source_plan_path="docs/plan.md",
        discovery={
            "id": repaired.manifest["latest_repair"]["repair_id"],
            "summary": "Correct the reviewed item title.",
        },
    )
    url, changed = publish_repair_pull_request(
        repo_root=tmp_path, publication=publication, runner=runner
    )

    assert url == "https://github.com/example/repo/pull/123"
    assert changed == (
        "docs/plans/plan-execution-plan.yaml",
        "docs/workflows/plan/item-01.yaml",
    )
    assert ["git", "fetch", "origin", "main"] in calls
    assert ["git", "switch", "--detach", "origin/main"] in calls
    create = next(call for call in calls if call[:3] == ["gh", "pr", "create"])
    assert create[create.index("--base") + 1] == "main"
    assert create[create.index("--title") + 1].startswith("Correct plan:")
    body = create[create.index("--body") + 1]
    assert "<!-- powdrr-plan-repair:v1 -->" in body
    assert '"repair_id":' in body


def test_repair_publisher_rejects_paths_before_running_git(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def runner(arguments: list[str], **_kwargs: Any) -> SimpleNamespace:
        calls.append(arguments)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    with pytest.raises(RepairPublicationError, match="path is invalid"):
        publish_repair_pull_request(
            repo_root=tmp_path,
            publication={
                "plan_id": "plan",
                "repair_id": "repair",
                "summary": "Fix plan",
                "body": "body",
                "changed": ["../outside.txt"],
                "artifacts": {"../outside.txt": "content"},
            },
            runner=runner,
        )

    assert calls == []


def test_repair_publisher_rejects_symlink_escape_before_running_git(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "docs").symlink_to(outside, target_is_directory=True)
    calls: list[list[str]] = []

    def runner(arguments: list[str], **_kwargs: Any) -> SimpleNamespace:
        calls.append(arguments)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    with pytest.raises(RepairPublicationError, match="escapes repository"):
        publish_repair_pull_request(
            repo_root=root,
            publication={
                "plan_id": "plan",
                "repair_id": "repair",
                "summary": "Fix plan",
                "body": "body",
                "changed": ["docs/outside.txt"],
                "artifacts": {"docs/outside.txt": "content"},
            },
            runner=runner,
        )

    assert calls == []
