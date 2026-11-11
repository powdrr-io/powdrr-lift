from __future__ import annotations

import copy
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
import test_story_planning
import yaml
from powdrr_agent.artifact_state import collect_artifact_state_snapshot
from powdrr_agent.execution.story_test_collection import (
    capture_story_execution_context,
    collect_story_tests,
    run_story_tests,
)
from powdrr_local import JsonServiceBridge, LocalPowdrrService
from powdrr_service.planning.story_allocation import pin_story, story_planning_context
from powdrr_service.structrr.design import DesignHandoff
from powdrr_service.structrr.story_coverage import (
    StoryCoverageError,
    story_case_obligations,
)
from powdrr_service.structrr.story_interview import StoryInterview, StoryUserResponse
from powdrr_service.structrr.user_stories import StoryEditRequest, parse_story_catalog
from powdrr_service.structrr.verification_health import audit_verification_health
from powdrr_service.verification.execute_pr_test_quality import (
    review_execute_pr_test_quality,
)
from powdrr_service.verification.story_baseline import require_story_baseline
from powdrr_service.verification.story_execution import (
    StoryExecutionReceipt,
    story_coverage_report,
)
from powdrr_service.verification.story_test_links import compile_story_test_links
from test_story_interview import add_graph, confirm, review_all
from test_story_planning import build_story_plan
from test_story_test_authoring import (
    PROFILE,
    NoExplanation,
    NoShortcoming,
)

from powdrr_lift.workrr import task_agent
from powdrr_lift.workrr.coding_agent import ImplementationRequest
from powdrr_lift.workrr.feature_endpoint import (
    FeatureEndpointConfig,
    _validate_feature_story_execution,
)
from powdrr_lift.workrr.llm import PowdrrExecutionError
from powdrr_lift.workrr.story_execution_operations import (
    execute_story_validation,
)


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()


def initialize(root: Path) -> str:
    root.mkdir(exist_ok=True)
    git(root, "init", "-b", "main")
    git(root, "config", "user.email", "fixture@example.test")
    git(root, "config", "user.name", "Fixture")
    git(root, "add", ".")
    git(root, "commit", "--allow-empty", "-m", "baseline")
    return git(root, "rev-parse", "HEAD")


@pytest.fixture
def graph() -> dict[str, Any]:
    document = yaml.safe_load(
        (
            Path(__file__).parent / "fixtures/user_stories/session-resume.yaml"
        ).read_text()
    )
    session = StoryInterview.start(
        "proof",
        "session-resume",
        "Resume interrupted planning without losing accepted decisions.",
        {},
        source_revision="a" * 40,
    )
    add_graph(session, document)
    question = session.ask(
        "recovery",
        "Stop when the checkpoint is unavailable?",
        proposed_assumption="Report unavailability and preserve accepted decisions.",
    )
    session.accept_user_response(
        StoryUserResponse(question["question_id"], question["text"], "yes")
    )
    review_all(session)
    confirm(session)
    proposal = session.compile_proposal()
    return DesignHandoff.compile({}, proposal, base_commit="a" * 40).to_data()[
        "desired"
    ]["document"]


GOOD = """import json
def resume(path, available):
    state = json.loads(path.read_text())
    result = {'outcome': 'published' if available else 'unavailable',
              'accepted': state['accepted']}
    if available:
        state['answers'] = ['completed']
        path.write_text(json.dumps(state))
    return result
"""
BAD = GOOD.replace(
    "        path.write_text(json.dumps(state))",
    "        pass  # planted lost checkpoint write",
)


def persistence_source(obligations: list[dict[str, Any]]) -> str:
    lines = ["import json", "from app import resume", ""]
    links = {}
    for index, obligation in enumerate(obligations):
        ref = obligation["story_refs"][0]
        available = ref["step_ids"][-1].endswith(":step:3")
        name = f"test_case_{index}"
        lines.extend(
            [
                f"# Covers: AC-01 {name}",
                f"def {name}(tmp_path):",
                "    checkpoint = tmp_path / 'checkpoint.json'",
                "    checkpoint.write_text(json.dumps({'accepted': ['kept'], "
                "'answers': []}))",
                f"    result = resume(checkpoint, {available!r})",
                "    assert result['outcome'] == "
                f"{'published' if available else 'unavailable'!r}",
            ]
        )
        start = len(lines)
        lines.append("    assert result['accepted'] == ['kept']")
        lines.append("    saved = json.loads(checkpoint.read_text())")
        lines.append(
            "    assert saved == {'accepted': ['kept'], 'answers': "
            f"{['completed'] if available else []!r}}}"
        )
        end = len(lines)
        links[name] = [
            {
                "obligation_id": obligation["obligation_id"],
                "observations": {
                    identity: [start, start + 1, end]
                    for identity in ref["observation_ids"]
                },
                "description": "Observe command outcome and persisted accepted "
                "decisions and answers.",
                "boundary": "app.resume and persisted checkpoint",
                "substitutions": [],
            }
        ]
        lines.append("")
    return "\n".join(lines) + "\n__powdrr_story_links__ = " + repr(links) + "\n"


def author(
    root: Path,
    base: str,
    graph: dict[str, Any],
    store: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    existing: bool = False,
) -> dict[str, Any]:
    identity = graph["user_stories"][0]["id"]
    ref = pin_story(graph, identity)
    obligations = story_case_obligations(parse_story_catalog(graph), ref.case_ids)
    emitted = persistence_source(obligations)

    class ScriptedMiniSWE:
        provider_name = "scripted-miniswe"

        def __init__(self, **kwargs: Any) -> None:
            pass

        def run(
            self,
            request: ImplementationRequest,
            *,
            worktree_root: Path,
            attempt_id: str,
        ) -> subprocess.CompletedProcess[str]:
            path = worktree_root / request.allowed_paths[0]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(emitted)
            git(worktree_root, "add", "-f", request.allowed_paths[0])
            return subprocess.CompletedProcess(["scripted-miniswe"], 0, "authored", "")

    monkeypatch.setattr(task_agent, "MiniSWEAgentProvider", ScriptedMiniSWE)
    criteria = [
        "Preserve accepted decisions and persist completed answers on "
        "successful resume."
    ]
    request = ImplementationRequest(
        request_id="story-proof",
        objective=criteria[0],
        prompt="",
        base_commit=base,
        plan_fingerprint="plan",
        allowed_paths=("app.py",),
        acceptance_criteria=tuple(criteria),
        validation_profiles=("verify",),
    )
    sources, artifact = task_agent._author_execute_pr_tests(
        root,
        base_commit=base,
        request=request,
        proposed={
            "title": "Resume amendment" if existing else "Resume",
            "objective": criteria[0],
            "acceptance_criteria": criteria,
            "story_refs": [ref.to_data()],
            "story_context": story_planning_context(graph, (ref,)),
            "story_validation": True,
            **({"item_id": "story-integration-validation"} if existing else {}),
        },
        workflow_id="proof",
        config=task_agent.WorkflowTaskAgentConfig(
            workflow_dir=store / "workflow",
            repo_root=root,
            coding_agent_validation_timeout_seconds=30,
        ),
        store_root=store,
        validation_profile=PROFILE,
        quality_review_service=LocalPowdrrService(
            quality_review_clients=(NoShortcoming(), NoExplanation())
        ),
    )
    assert list(sources.values()) == [emitted]  # No manual oracle repair.
    for path, source in sources.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(source)
        git(root, "add", "-f", path)
    return artifact


def execute(
    root: Path, graph: dict[str, Any], artifact: dict[str, Any], store: Path
) -> dict[str, Any]:
    return execute_story_validation(
        root,
        snapshot=graph,
        authoring=artifact,
        profile=PROFILE,
        repository="fixture/repo",
        artifact_root=store,
        report_path="docs/plans/story-validation.json",
        timeout_seconds=30,
    )


def test_prose_to_plan_author_execution_repair_and_approved_amendment(
    graph: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity = graph["user_stories"][0]["id"]
    monkeypatch.setattr(test_story_planning, "STORY", identity)
    compiled, _, _ = build_story_plan(graph, tmp_path)
    first, second, integration = compiled.plan.items
    assert set(integration.dependencies) == {first.item_id, second.item_id}
    root = tmp_path / "repo"
    root.mkdir()
    (root / "app.py").write_text(BAD)
    base = initialize(root)
    artifact = author(root, base, graph, tmp_path / "author", monkeypatch)
    # Focused response-only test misses the customer-visible persistence defect.
    focused = root / "test_focused.py"
    focused.write_text(
        "from app import resume\n"
        "def test_response(tmp_path):\n"
        "    p = tmp_path / 'state.json'\n"
        '    p.write_text(\'{"accepted": ["kept"]}\')\n'
        "    assert resume(p, True)['outcome'] == 'published'\n"
        ""
    )
    assert (
        subprocess.run(
            [*PROFILE.command, str(focused)], cwd=root, capture_output=True
        ).returncode
        == 0
    )
    failed = execute(root, graph, artifact, tmp_path / "runs")
    assert not failed["story_coverage"]["verified"]
    assert (
        json.loads((root / "docs/plans/story-validation.json").read_text())["coverage"][
            "verified"
        ]
        is False
    )
    feature_state = {
        "story_test_authoring": artifact,
        "story_test_sources": artifact["test_sources"],
        "story_validation_profile": PROFILE,
    }
    feature_config = FeatureEndpointConfig(
        feature_description="Resume",
        work_item_name="Resume",
        repo_root=root,
        allowed_paths=("app.py",),
    )
    with pytest.raises(
        PowdrrExecutionError, match="current passing execution receipts"
    ):
        _validate_feature_story_execution(
            feature_config,
            worktree=root,
            output_root=tmp_path / "feature-run",
            state=feature_state,
            snapshot=graph,
        )
    assert feature_state["story_execution"]["story_coverage"]["verified"] is False

    case = failed["story_coverage"]["stories"][0]["cases"][0]
    assert case["status"] == "failed" and case["transition_ids"]
    assert case["observations"][0]["tests"][0]["assertion_lines"]
    assert failed["story_coverage"]["stories"][0]["status"] == "implemented_partially"
    # A permitted implementation repair preserves the independent oracle.
    (root / "app.py").write_text(GOOD)
    passed = execute(root, graph, artifact, tmp_path / "runs")
    assert passed["story_coverage"]["verified"]
    assert (
        json.loads((root / "docs/plans/story-validation.json").read_text())["receipts"]
        == passed["story_receipts"]
    )
    published = root / "docs/structrr/diffs/plan/integration/story-execution.json"
    published.parent.mkdir(parents=True)
    published.write_text(
        json.dumps(
            {
                "context": passed["execution_context"],
                "receipts": passed["story_receipts"],
                "coverage_at_execution": passed["story_coverage"],
            }
        )
    )
    git(root, "add", ".")
    git(root, "commit", "-m", "Record story execution")
    request = collect_artifact_state_snapshot(
        root,
        branch="main",
        sha=git(root, "rev-parse", "HEAD"),
        request_id="story-evidence",
        areas=["story_evidence"],
        include_github=False,
    )
    update = JsonServiceBridge(LocalPowdrrService()).start(request)
    assert update.status == "succeeded"
    inventory = json.loads(update.output_artifacts[0].data)["story_evidence"]
    assert inventory[0]["status"] == "historical"
    assert inventory[0]["receipts"] == passed["story_receipts"]

    assert _validate_feature_story_execution(
        feature_config,
        worktree=root,
        output_root=tmp_path / "feature-run",
        state=feature_state,
        snapshot=graph,
    )["story_coverage"]["verified"]
    unavailable = execute(
        root, graph, {**artifact, "accepted_story_review": {}}, tmp_path / "unreviewed"
    )
    assert {receipt["outcome"] for receipt in unavailable["story_receipts"]} == {
        "blocked"
    }
    assert not unavailable["story_coverage"]["verified"]
    # Historical source bytes and review identity cannot be silently refreshed.
    stale_review = copy.deepcopy(artifact)
    stale_review["story_review_evidence"]["contract"]["objective"] = (
        "different behavior"
    )
    assert not execute(root, graph, stale_review, tmp_path / "stale-review")[
        "story_coverage"
    ]["verified"]

    assert (
        passed["execution_context"]["candidate_sha"]
        != failed["execution_context"]["candidate_sha"]
    )
    payload = json.loads(
        (
            Path(passed["story_evidence_path"]) / "verification-health-input.json"
        ).read_text()
    )
    assert audit_verification_health(story_coverage=payload["story_coverage"]).passed
    assert not audit_verification_health(
        story_coverage=payload["story_coverage"], candidate_tree="d" * 40
    ).passed

    amendment = StoryInterview.start(
        "amend",
        "session-resume",
        "Clarify that completed answers remain persisted in the resumed checkpoint.",
        graph,
        source_revision=base,
        story_mode="amend",
        existing_story_ref=identity,
    )
    amendment.edit(
        StoryEditRequest(
            "user_story_steps",
            "change",
            identity,
            {
                "description": "Complete remaining questions, persist completed "
                "answers, and publish the plan."
            },
            identity + ":step:3",
        ),
        "clarify-persistence",
    )
    review_all(amendment)
    confirm(amendment)
    changed = DesignHandoff.compile(
        graph, amendment.compile_proposal(), base_commit=base
    ).to_data()["desired"]["document"]
    historical = story_coverage_report(
        changed,
        context=passed["execution_context"],
        manifest=artifact["story_test_links"],
        receipts=tuple(
            StoryExecutionReceipt.from_data(raw) for raw in passed["story_receipts"]
        ),
    )
    assert not historical["verified"]
    assert {entry["status"] for entry in historical["receipts"]} == {"stale"}
    approved_proposal = root / "docs/plans/approved-story-amendment.json"
    approved_proposal.parent.mkdir(parents=True, exist_ok=True)
    approved_proposal.write_text(json.dumps(amendment.compile_proposal().to_data()))
    git(root, "add", ".")
    git(root, "commit", "-m", "Approve persisted-answer story amendment")
    refreshed = author(
        root,
        git(root, "rev-parse", "HEAD"),
        changed,
        tmp_path / "refreshed",
        monkeypatch,
        existing=True,
    )
    assert refreshed["baseline_classification"] == "existing_behavior_pass"
    assert execute(root, changed, refreshed, tmp_path / "runs")["story_coverage"][
        "verified"
    ]
    assert (
        json.loads((Path(failed["story_evidence_path"]) / "coverage.json").read_text())[
            "verified"
        ]
        is False
    )


def test_candidate_identity_binds_sources_config_environment_and_preserves_index(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "app.py").write_text("value = 1\n")
    initialize(root)
    (root / ".gitignore").write_text("generated/\n")
    (root / "generated").mkdir()
    (root / "generated/test_case.py").write_text("assert len([]) == 0\n")
    git(root, "add", "-f", "generated/test_case.py")
    original_index = git(root, "write-tree")

    def capture(**kwargs: Any) -> dict[str, Any]:
        return capture_story_execution_context(
            root,
            repository="fixture",
            command=kwargs.get("command", PROFILE.command),
            runtime=kwargs.get("runtime", {"python": "fixture"}),
        )

    before = capture()
    assert git(root, "write-tree") == original_index
    (root / "generated/test_case.py").write_text("assert len([]) == 1\n")
    assert capture()["candidate_sha"] != before["candidate_sha"]
    assert (
        capture(command=("pytest", "-x"))["configuration_digest"]
        != before["configuration_digest"]
    )
    assert (
        capture(runtime={"python": "changed"})["environment_digest"]
        != before["environment_digest"]
    )


@pytest.mark.parametrize(
    ("source", "outcome"),
    [
        (
            "import pytest\n"
            "def test_case_0():\n"
            "    pytest.skip('unavailable')\n"
            "    assert len([]) == 0\n"
            "",
            "not_run",
        ),
        (
            "import pytest\n"
            "@pytest.mark.xfail(reason='defect')\n"
            "def test_case_0():\n"
            "    assert len([]) == 1\n"
            "",
            "not_run",
        ),
        (
            "import pytest\n"
            "@pytest.fixture(autouse=True)\n"
            "def setup():\n"
            "    raise RuntimeError('dependency unavailable')\n"
            "def test_case_0():\n"
            "    assert len([]) == 0\n"
            "",
            "blocked",
        ),
        ("def test_case_0():\n    if False:\n        assert len([]) == 0\n", "not_run"),
    ],
)
def test_unavailable_skipped_and_unexecuted_assertions_never_pass(
    graph: dict[str, Any], tmp_path: Path, source: str, outcome: str
) -> None:
    obligations = story_case_obligations(
        parse_story_catalog(graph), [graph["user_story_cases"][0]["id"]]
    )
    line = next(
        index for index, text in enumerate(source.splitlines(), 1) if "assert " in text
    )
    source += (
        "__powdrr_story_links__ = "
        + repr(
            {
                "test_case_0": [
                    {
                        "obligation_id": obligations[0]["obligation_id"],
                        "observations": {
                            obligations[0]["story_refs"][0]["observation_ids"][0]: [
                                line
                            ]
                        },
                        "description": "Observe required outcome.",
                        "boundary": "public API",
                        "substitutions": [],
                    }
                ]
            }
        )
        + "\n"
    )
    path = "test_case.py"
    (tmp_path / path).write_text(source)
    initialize(tmp_path)
    collection = collect_story_tests(
        PROFILE, root=tmp_path, paths=(path,), timeout_seconds=30
    )
    manifest = compile_story_test_links(
        {path: source}, obligations, collection["selectors"]
    )
    evidence = {
        "contract": {},
        "test_obligations": obligations,
        "test_sources": {path: source},
        "story_test_links": manifest,
    }
    review = review_execute_pr_test_quality(
        evidence, decision_client=NoShortcoming(), llm_client=NoExplanation()
    )
    artifact = {
        "story_test_links": manifest,
        "test_sources": {path: source},
        "accepted_story_review": review,
        "story_review_evidence": evidence,
    }
    result = execute(
        tmp_path, graph, artifact, tmp_path.parent / (tmp_path.name + "-runs")
    )
    assert {receipt["outcome"] for receipt in result["story_receipts"]} == {outcome}
    assert not result["story_coverage"]["verified"]


def test_missing_interface_baseline_requires_actual_declared_absence(
    tmp_path: Path,
) -> None:
    path = "test_missing.py"
    (tmp_path / path).write_text(
        "def test_missing():\n"
        "    import planned_public_module\n"
        "    assert planned_public_module.resume()\n"
        ""
    )
    result = run_story_tests(
        PROFILE,
        root=tmp_path,
        paths=(path,),
        assertion_lines={path: [3]},
        timeout_seconds=30,
    )
    assert (
        require_story_baseline(
            result,
            policy="new_behavior",
            missing_interfaces=[{"module": "planned_public_module", "exists": False}],
        )
        == "missing_interface"
    )
    with pytest.raises(StoryCoverageError, match="environment/import/syntax"):
        require_story_baseline(result, policy="new_behavior")
    (tmp_path / path).write_text(
        "import planned_public_module\ndef test_missing():\n    assert len([]) == 0\n"
    )
    result = run_story_tests(
        PROFILE,
        root=tmp_path,
        paths=(path,),
        assertion_lines={path: [3]},
        timeout_seconds=30,
    )
    with pytest.raises(StoryCoverageError, match="collection"):
        require_story_baseline(
            result,
            policy="new_behavior",
            missing_interfaces=[{"module": "planned_public_module", "exists": False}],
        )
