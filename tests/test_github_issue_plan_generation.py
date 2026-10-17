from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from powdrr_lift import cli
from powdrr_lift.process.tasks import load_workflow_task
from powdrr_lift.workrr.git import load_workflow_git_state
from powdrr_lift.workrr.github_issue_plan_generation import (
    PLAN_ITEMS_RESPONSE_SCHEMA,
    PlanGenerationError,
    generate_execution_plan_yaml,
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
    assert plan.items[0].workflow_directory == (
        "docs/workflows/local-attestation-v1/local-attestation-v1-foundation"
    )
    assert plan.items[0].workflow_id == "local-attestation-v1-foundation"
    assert plan.items[1].workflow_directory == (
        "docs/workflows/local-attestation-v1/local-attestation-v1-integration"
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
    workflow_directory = tmp_path / "docs/workflows/proposal-v1/proposal-v1-foundation"
    assert status == 0
    assert output.is_file()
    assert workflow_directory.is_dir()
    assert len(tuple(workflow_directory.glob("*.yaml"))) == 14
    first_task = load_workflow_task(
        workflow_directory / "proposal-v1-foundation-task-001.yaml"
    )
    assert first_task.pre_step is None
    assert first_task.input_state["structured_pr_context"] == {
        "plan_id": "proposal-v1",
        "item_id": "foundation",
        "title": "Implement foundation",
        "objective": "Deliver foundation from the plan.",
        "scope": ["component: foundation"],
        "acceptance_criteria": ["foundation meets its documented acceptance criteria."],
        "dependencies": [],
        "source_plan": "proposal.md",
    }
    workflow_state = load_workflow_git_state(
        workflow_directory, "proposal-v1-foundation"
    )
    assert workflow_state is not None
    assert workflow_state.integration_branch == "powdrr/proposal-v1-foundation"
    assert workflow_state.workflow_relative_directory == (
        "docs/workflows/proposal-v1/proposal-v1-foundation"
    )
    proposal_document = (
        tmp_path / "docs/proposals/proposal-v1-foundation/plan-item.yaml"
    )
    assert proposal_document.is_file()
    assert len(staged) == 16
    assert set(staged) == {
        output,
        proposal_document,
        *workflow_directory.glob("*.yaml"),
    }
    response = capsys.readouterr().out
    assert '"workflow_count": 1' in response
    assert '"published_issues": false' in response


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
    workflow_directory = tmp_path / plan.items[0].workflow_directory
    workflow_directory.mkdir(parents=True)
    sentinel = workflow_directory / "keep.yaml"
    sentinel.write_text("preserve", encoding="utf-8")
    output_path = tmp_path / "docs/plans/local-attestation-v1-execution-plan.yaml"
    template_path = (
        Path(__file__).resolve().parents[1] / "templates" / "execute-proposed-pr.yaml"
    )

    with pytest.raises(FileExistsError, match="already exists"):
        write_execution_plan_artifacts(
            rendered_plan=rendered,
            plan=plan,
            output_path=output_path,
            repo_root=tmp_path,
            source_plan_path="proposal.md",
            template_path=template_path,
            verification_command="pytest -q",
        )

    assert sentinel.read_text(encoding="utf-8") == "preserve"
    assert not output_path.exists()
