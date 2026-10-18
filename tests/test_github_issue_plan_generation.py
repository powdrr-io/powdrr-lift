from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from powdrr_lift import cli
from powdrr_lift.workrr.github_issue_plan_generation import (
    PLAN_ITEMS_RESPONSE_SCHEMA,
    PlanGenerationError,
    PlanPullRequest,
    generate_execution_plan_yaml,
    load_plan_workflow_instance,
    publish_execution_plan_pull_request,
    write_execution_plan_artifacts,
)


class FakePlanningClient:
    def __init__(self, response: dict[str, Any]) -> None:
        self.response = response
        self.messages: list[dict[str, str]] | None = None
        self.response_schema: dict[str, Any] | None = None

    def complete_json(
        self,
        messages: list[dict[str, str]],
        *,
        response_schema: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.messages = messages
        self.response_schema = response_schema
        return self.response


def _item(item_id: str, *, dependencies: list[str] | None = None) -> dict[str, Any]:
    return {
        "id": item_id,
        "title": f"Implement {item_id}",
        "objective": f"Deliver {item_id} from the plan.",
        "scope": [f"component: {item_id}"],
        "acceptance_criteria": [f"{item_id} meets its documented acceptance criteria."],
        "dependencies": dependencies or [],
    }


def test_generate_execution_plan_yaml_compiles_valid_ordered_plan() -> None:
    client = FakePlanningClient(
        {
            "items": [
                _item("foundation"),
                _item("integration", dependencies=["foundation"]),
            ]
        }
    )

    rendered, plan = generate_execution_plan_yaml(
        source_plan="# Plan\nImplement a foundation, then integration.",
        plan_id="local-attestation-v1",
        client=client,
    )

    assert client.response_schema == PLAN_ITEMS_RESPONSE_SCHEMA
    assert client.messages is not None
    assert "Implement a foundation" in client.messages[1]["content"]
    assert [item.item_id for item in plan.items] == ["foundation", "integration"]
    assert plan.items[1].dependencies == ("foundation",)
    assert plan.items[0].workflow_file == (
        "docs/workflows/local-attestation-v1/local-attestation-v1-foundation.yaml"
    )
    assert plan.items[0].workflow_id == "local-attestation-v1-foundation"
    assert plan.items[1].workflow_file == (
        "docs/workflows/local-attestation-v1/local-attestation-v1-integration.yaml"
    )
    assert plan.items[1].workflow_id == "local-attestation-v1-integration"
    assert (
        "schema: https://powdrr.io/schemas/github-issue-execution-plan-v1" in rendered
    )


@pytest.mark.parametrize(
    "response, message",
    [
        (
            {"items": [_item("first"), _item("second", dependencies=["first"])]},
            "ordered",
        ),
        ({"items": [_item("first", dependencies=["missing"])]}, "unknown dependency"),
        ({"items": []}, "at least one"),
        (
            {"items": [_item("first"), {**_item("second"), "extra": "field"}]},
            "unknown or missing fields",
        ),
    ],
)
def test_generate_execution_plan_yaml_rejects_invalid_model_output(
    response: dict[str, Any], message: str
) -> None:
    items = response.get("items")
    if isinstance(items, list) and len(items) > 1:
        response = {"items": list(reversed(items))}
    client = FakePlanningClient(response)

    with pytest.raises(PlanGenerationError, match=message):
        generate_execution_plan_yaml(
            source_plan="# Plan\nDo the work.",
            plan_id="local-attestation-v1",
            client=client,
        )


def test_generate_execution_plan_yaml_rejects_empty_and_oversized_sources() -> None:
    client = FakePlanningClient({"items": [_item("first")]})

    with pytest.raises(PlanGenerationError, match="must not be empty"):
        generate_execution_plan_yaml(
            source_plan="\n",
            plan_id="local-attestation-v1",
            client=client,
        )
    with pytest.raises(PlanGenerationError, match="exceeds"):
        generate_execution_plan_yaml(
            source_plan="x" * 500_001,
            plan_id="local-attestation-v1",
            client=client,
        )


def test_plan_cli_writes_and_stages_reviewable_plan(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "proposal.md"
    source.write_text("# Proposal\nAdd a foundation and integration.", encoding="utf-8")
    staged: list[Any] = []
    client = FakePlanningClient({"items": [_item("foundation")]})

    monkeypatch.setattr(cli, "resolve_repo_root", lambda _path: tmp_path)
    monkeypatch.setattr(cli, "resolve_workflow_provider", lambda value: value)
    monkeypatch.setattr(
        cli,
        "configured_model_mapping",
        lambda _provider, _model: type(
            "Mapping", (), {"provider": "test", "model": "test-model"}
        )(),
    )
    monkeypatch.setattr(cli, "resolve_provider_credentials", lambda *_args: object())
    monkeypatch.setattr(cli, "build_workflow_client", lambda *_args, **_kwargs: client)
    monkeypatch.setattr(
        cli, "_stage_generated_file", lambda _root, path: staged.append(path)
    )
    publications: list[dict[str, Any]] = []

    def publish_plan(**kwargs: Any) -> PlanPullRequest:
        publications.append(kwargs)
        return PlanPullRequest(
            branch_name="powdrr/plan-proposal-v1-12345678",
            base_branch="main",
            commit_sha="abc123",
            pull_request_url="https://github.com/example/repo/pull/42",
        )

    monkeypatch.setattr(cli, "publish_execution_plan_pull_request", publish_plan)

    status = cli.main(
        [
            "plan",
            "--source",
            str(source),
            "--plan-id",
            "proposal-v1",
            "--verification-command",
            "scripts/verify-all.sh",
            "--repo-root",
            str(tmp_path),
            "--json",
        ]
    )

    output = tmp_path / "docs/plans/proposal-v1-execution-plan.yaml"
    workflow_file = tmp_path / "docs/workflows/proposal-v1/proposal-v1-foundation.yaml"
    assert status == 0
    assert output.is_file()
    assert workflow_file.is_file()
    workflow_definition = workflow_file.read_text(encoding="utf-8")
    assert "template: execute-proposed-pr" in workflow_definition
    assert "proposed-pr-id: proposal-v1-foundation" in workflow_definition
    assert "verification-command: scripts/verify-all.sh" in workflow_definition
    instance = load_plan_workflow_instance(workflow_file)
    assert instance.workflow_id == "proposal-v1-foundation"
    assert instance.template_id == "execute-proposed-pr"
    assert instance.parameters == {
        "proposed-pr-id": "proposal-v1-foundation",
        "verification-command": "scripts/verify-all.sh",
    }
    assert not list((tmp_path / "docs/workflows/proposal-v1").glob("*-task-*.yaml"))
    assert len(staged) == 2
    assert publications[0]["repo_root"] == tmp_path
    assert publications[0]["generated_files"] == tuple(staged)
    assert publications[0]["source_plan_path"] == "proposal.md"
    assert set(staged) == {output, workflow_file}
    response = capsys.readouterr().out
    assert '"workflow_count": 1' in response
    assert '"pull_request_url": "https://github.com/example/repo/pull/42"' in response
    assert '"status": "pull_request_opened"' in response
    assert '"published_issues": false' in response


def test_publish_execution_plan_pull_request_commits_only_generated_files(
    tmp_path: Any,
) -> None:
    _, plan = generate_execution_plan_yaml(
        source_plan="# Plan\nDo the work.",
        plan_id="proposal-v1",
        client=FakePlanningClient({"items": [_item("foundation")]}),
    )
    generated_file = tmp_path / "docs/plans/proposal-v1-execution-plan.yaml"
    generated_file.parent.mkdir(parents=True)
    generated_file.write_text("generated", encoding="utf-8")
    commands: list[list[str]] = []

    def runner(
        command: list[str],
        *,
        cwd: Path,
        capture_output: bool,
        text: bool,
        check: bool,
    ) -> Any:
        del cwd, capture_output, text, check
        commands.append(command)
        if command[:3] == ["git", "branch", "--show-current"]:
            output = "main\n"
        elif command[:3] == ["git", "rev-parse", "--verify"]:
            output = "abc123\n"
        elif command[:3] == ["gh", "pr", "create"]:
            output = "https://github.com/example/repo/pull/42\n"
        else:
            output = ""
        return type(
            "Completed",
            (),
            {"returncode": 0, "stdout": output, "stderr": ""},
        )()

    pull_request = publish_execution_plan_pull_request(
        repo_root=tmp_path,
        plan=plan,
        generated_files=(generated_file,),
        source_plan_path="docs/plans/proposal.md",
        runner=runner,
    )

    assert pull_request.base_branch == "main"
    assert pull_request.commit_sha == "abc123"
    assert pull_request.pull_request_url.endswith("/pull/42")
    commit_command = next(command for command in commands if "commit" in command)
    assert "--only" in commit_command
    assert commit_command[-1] == "docs/plans/proposal-v1-execution-plan.yaml"
    assert "docs/unrelated-staged-file.yaml" not in commit_command
    pr_command = next(
        command for command in commands if command[:3] == ["gh", "pr", "create"]
    )
    assert pr_command[pr_command.index("--base") + 1] == "main"
    assert pr_command[pr_command.index("--head") + 1] == pull_request.branch_name
    assert "docs/plans/proposal.md" in pr_command[pr_command.index("--body") + 1]


def test_publish_execution_plan_pull_request_rejects_detached_head(
    tmp_path: Any,
) -> None:
    _, plan = generate_execution_plan_yaml(
        source_plan="# Plan\nDo the work.",
        plan_id="proposal-v1",
        client=FakePlanningClient({"items": [_item("foundation")]}),
    )
    generated_file = tmp_path / "plan.yaml"
    generated_file.write_text("generated", encoding="utf-8")

    def runner(
        _command: list[str],
        *,
        cwd: Path,
        capture_output: bool,
        text: bool,
        check: bool,
    ) -> Any:
        del cwd, capture_output, text, check
        return type("Completed", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    with pytest.raises(PlanGenerationError, match="detached HEAD"):
        publish_execution_plan_pull_request(
            repo_root=tmp_path,
            plan=plan,
            generated_files=(generated_file,),
            source_plan_path="proposal.md",
            runner=runner,
        )


def test_publish_execution_plan_pr_preserves_other_staged_changes(
    tmp_path: Any,
) -> None:
    import subprocess

    repo = tmp_path / "repo"
    remote = tmp_path / "origin.git"
    repo.mkdir()
    subprocess.run(["git", "init", "--bare", str(remote)], check=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=repo, check=True
    )
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, check=True)
    subprocess.run(
        ["git", "remote", "add", "origin", str(remote)], cwd=repo, check=True
    )
    subprocess.run(
        ["git", "push", "--set-upstream", "origin", "main"], cwd=repo, check=True
    )

    unrelated = repo / "docs/unrelated-staged-file.yaml"
    unrelated.parent.mkdir()
    unrelated.write_text("unrelated: true\n", encoding="utf-8")
    subprocess.run(["git", "add", str(unrelated)], cwd=repo, check=True)
    generated = repo / "docs/plans/generated.yaml"
    generated.parent.mkdir()
    generated.write_text("generated: true\n", encoding="utf-8")
    _, plan = generate_execution_plan_yaml(
        source_plan="# Plan\nDo the work.",
        plan_id="proposal-v1",
        client=FakePlanningClient({"items": [_item("foundation")]}),
    )

    def runner(command: list[str], **kwargs: Any) -> Any:
        if command[:3] == ["gh", "pr", "create"]:
            return subprocess.CompletedProcess(
                command,
                0,
                stdout="https://github.com/example/repo/pull/42\n",
                stderr="",
            )
        return subprocess.run(command, **kwargs)

    pull_request = publish_execution_plan_pull_request(
        repo_root=repo,
        plan=plan,
        generated_files=(generated,),
        source_plan_path="docs/plans/source.md",
        runner=runner,
    )

    committed_paths = subprocess.run(
        ["git", "show", "--pretty=format:", "--name-only", "HEAD"],
        cwd=repo,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.splitlines()
    staged_paths = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=repo,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.splitlines()
    assert committed_paths == ["docs/plans/generated.yaml"]
    assert staged_paths == ["docs/unrelated-staged-file.yaml"]
    assert pull_request.pull_request_url.endswith("/pull/42")
    remote_branch = subprocess.run(
        [
            "git",
            "--git-dir",
            str(remote),
            "show-ref",
            "--verify",
            f"refs/heads/{pull_request.branch_name}",
        ],
        check=True,
        text=True,
        capture_output=True,
    )
    assert remote_branch.returncode == 0


def test_generate_execution_plan_yaml_rejects_colliding_workflow_ids() -> None:
    client = FakePlanningClient({"items": [_item("area/first"), _item("area-first")]})

    with pytest.raises(PlanGenerationError, match="unique workflow ids"):
        generate_execution_plan_yaml(
            source_plan="# Plan\nDo the work.",
            plan_id="local-attestation-v1",
            client=client,
        )


def test_plan_artifact_writer_preserves_existing_workflow_files(tmp_path: Any) -> None:
    rendered, plan = generate_execution_plan_yaml(
        source_plan="# Plan\nDo the work.",
        plan_id="local-attestation-v1",
        client=FakePlanningClient({"items": [_item("foundation")]}),
    )
    assert plan.items[0].workflow_file is not None
    workflow_file = tmp_path / plan.items[0].workflow_file
    workflow_file.parent.mkdir(parents=True)
    sentinel = workflow_file
    sentinel.write_text("preserve", encoding="utf-8")
    output_path = tmp_path / "docs/plans/local-attestation-v1-execution-plan.yaml"

    with pytest.raises(FileExistsError, match="already exists"):
        write_execution_plan_artifacts(
            rendered_plan=rendered,
            plan=plan,
            output_path=output_path,
            repo_root=tmp_path,
            verification_command="pytest -q",
        )

    assert sentinel.read_text(encoding="utf-8") == "preserve"
    assert not output_path.exists()
