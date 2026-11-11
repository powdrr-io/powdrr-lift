from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml
from powdrr_agent.execution.story_test_collection import collect_story_tests
from powdrr_local import LocalPowdrrService
from powdrr_service.planning.story_allocation import pin_story, story_planning_context
from powdrr_service.structrr.story_coverage import (
    StoryCoverageError,
    declared_case_coverage,
    expand_story_cases,
    parse_story_case_refs,
    story_case_obligations,
    story_case_ref,
    validate_story_case_refs,
)
from powdrr_service.structrr.user_stories import (
    StoryEditRequest,
    allocate_story_edit,
    apply_story_edits,
    parse_story_catalog,
    story_contract_digest,
)
from powdrr_service.structrr.verification_contract import VerificationContract
from powdrr_service.verification.execute_pr_test_quality import (
    review_execute_pr_test_quality,
)
from powdrr_service.verification.story_test_authoring import (
    prepare_story_test_obligations,
)
from powdrr_service.verification.story_test_links import (
    compile_story_test_links,
    require_story_test_quality,
)

from powdrr_lift.workrr import task_agent
from powdrr_lift.workrr.coding_agent import ImplementationRequest
from powdrr_lift.workrr.coding_agent_validation import ValidationProfile

STORY = "story:session-resume"
PROFILE = ValidationProfile(name="verify", command=(sys.executable, "-m", "pytest"))


def confirm(graph: dict[str, Any]) -> dict[str, Any]:
    graph["user_stories"][0]["confirmation"] = {
        "contract_digest": story_contract_digest(parse_story_catalog(graph), STORY),
        "decision_ref": "review:fixture:1",
        "authority": "reviewed_source",
    }
    return graph


@pytest.fixture
def graph() -> dict[str, Any]:
    return confirm(
        yaml.safe_load(
            (
                Path(__file__).parent / "fixtures/user_stories/session-resume.yaml"
            ).read_text()
        )
    )


def loop_graph(graph: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(graph)
    result["user_stories"][0]["preconditions"].append(
        "The user may retry an unavailable checkpoint zero, one, or multiple times."
    )
    result["user_stories"][0]["identity_state"].update(
        next_step=6, next_transition=6, next_observation=4
    )
    result["user_story_steps"].append(
        {
            "id": STORY + ":step:5",
            "story_id": STORY,
            "kind": "user_action",
            "description": "Retry the unavailable checkpoint on request.",
            "action": {
                "actor": "Maintainer",
                "operation": "Retry checkpoint",
                "input": "Existing session ID",
            },
            "source_refs": ["review:fixture:1"],
            "expected_observations": [
                {
                    "id": STORY + ":observation:3",
                    "description": "The retry count is visible before retry.",
                    "boundary": "Resume command response",
                }
            ],
        }
    )
    result["user_story_transitions"].extend(
        [
            {
                "id": STORY + ":transition:4",
                "story_id": STORY,
                "from_step_id": STORY + ":step:2",
                "to_step_id": STORY + ":step:5",
                "condition": {
                    "kind": "predicate",
                    "description": "Checkpoint unavailable; retry requested.",
                },
                "source_refs": ["review:fixture:1"],
            },
            {
                "id": STORY + ":transition:5",
                "story_id": STORY,
                "from_step_id": STORY + ":step:5",
                "to_step_id": STORY + ":step:2",
                "condition": {"kind": "always", "description": "Retry was requested."},
                "source_refs": ["review:fixture:1"],
            },
        ]
    )
    result["user_story_cases"] = []
    return confirm(result)


def add_candidates(graph: dict[str, Any]) -> dict[str, Any]:
    catalog = parse_story_catalog(graph)
    expansion = expand_story_cases(catalog, STORY)
    assert expansion.status == "graph_coverage_complete"
    for index, candidate in enumerate(expansion.candidates):
        request = StoryEditRequest(
            "user_story_cases",
            "add",
            STORY,
            {
                "description": f"Confirmed path to {candidate.step_ids[-1]}",
                "initial_conditions": list(catalog.user_stories[0].preconditions),
                "transition_ids": list(candidate.transition_ids),
                "expected_observations": list(candidate.observation_ids),
                "source_refs": ["review:fixture:1"],
            },
        )
        edit = allocate_story_edit(catalog, request, f"case:{index}")
        catalog = apply_story_edits(catalog, (edit,), catalog.fingerprint)
    return catalog.to_data()


def test_candidate_walks_are_finite_stable_and_never_hide_budget_exhaustion(
    graph: dict[str, Any],
) -> None:
    loop = loop_graph(graph)
    catalog = parse_story_catalog(loop)
    limited = expand_story_cases(catalog, STORY, edge_budget=1)
    assert limited.status == "case_expansion_incomplete"
    assert limited.edge_expansions == 1 and limited.uncovered_targets
    full = expand_story_cases(catalog, STORY)
    assert full.status == "graph_coverage_complete"
    assert full.edge_expansions <= 1000
    assert {
        min(2, candidate.transition_ids.count(STORY + ":transition:5"))
        for candidate in full.candidates
    } == {0, 1, 2}
    reversed_graph = copy.deepcopy(loop)
    reversed_graph["user_story_transitions"].reverse()
    assert expand_story_cases(parse_story_catalog(reversed_graph), STORY) == full
    refined = add_candidates(loop)
    assert not declared_case_coverage(parse_story_catalog(refined), STORY)
    assert (
        refined["user_stories"][0]["confirmation"]
        == loop["user_stories"][0]["confirmation"]
    )
    for case in refined["user_story_cases"]:
        ref = story_case_ref(parse_story_catalog(refined), case["id"])
        assert len(ref.step_ids) == len(ref.transition_ids) + 1


def test_missing_cases_return_candidates_to_normal_design_workflow(
    graph: dict[str, Any],
) -> None:
    graph["user_story_cases"].pop()
    pin = pin_story(graph, STORY)
    proposed: dict[str, Any] = {
        "story_refs": [pin.to_data()],
        "story_context": story_planning_context(graph, (pin,)),
        "story_validation": True,
    }
    with pytest.raises(StoryCoverageError, match="story-design-interview") as caught:
        prepare_story_test_obligations(proposed)
    assert STORY + ":transition:3" in str(caught.value)
    assert "candidates" in str(caught.value)
    proposed["story_validation"] = False
    assert prepare_story_test_obligations(proposed) == []


def linked_source(obligations: list[dict[str, Any]]) -> str:
    lines = ["from app import resume", ""]
    requests = {}
    for index, obligation in enumerate(obligations):
        name = f"test_case_{index}"
        ref = obligation["story_refs"][0]
        available = ref["step_ids"][-1] == STORY + ":step:3"
        expected = "published" if available else "unavailable"
        retries = ref["transition_ids"].count(STORY + ":transition:5")
        lines.extend([f"# Covers: AC-01 {name}", f"def {name}():"])
        argument = f"{available!r}, retries={retries}" if retries else repr(available)
        lines.append(f"    result = resume({argument})")
        lines.append(f"    assert result['outcome'] == {expected!r}")
        outcome_line = len(lines)
        lines.append("    assert result['accepted'] == ['kept']")
        state_line = len(lines)
        observations = {}
        for identity in ref["observation_ids"]:
            if identity == STORY + ":observation:3":
                lines.append(f"    assert result['retries'] == {retries}")
                observations[identity] = [len(lines)]
            else:
                observations[identity] = [outcome_line, state_line]
        lines.append("")
        requests[name] = [
            {
                "obligation_id": obligation["obligation_id"],
                "observations": observations,
                "description": "Preserve decisions; report outcome and retry count.",
                "boundary": "app.resume",
                "substitutions": [],
            }
        ]
    return "\n".join(lines) + "\n__powdrr_story_links__ = " + repr(requests) + "\n"


def test_manifest_requires_collected_tests_and_executable_observation_assertions(
    graph: dict[str, Any], tmp_path: Path
) -> None:
    obligations = story_case_obligations(
        parse_story_catalog(graph), [case["id"] for case in graph["user_story_cases"]]
    )
    source = linked_source(obligations)
    (tmp_path / "tests").mkdir()
    (tmp_path / "app.py").write_text(
        "def resume(available):\n    return {'outcome': 'lost', 'accepted': []}\n"
    )
    path = "tests/test_resume.py"
    (tmp_path / path).write_text(source)
    collected = collect_story_tests(
        PROFILE, root=tmp_path, paths=(path,), timeout_seconds=30
    )
    assert collected["status"] == "collected"
    manifest = compile_story_test_links(
        {path: source}, obligations, collected["selectors"]
    )
    assert {link["selector"] for link in manifest["links"]} == set(
        collected["selectors"]
    )
    assert all(
        link["test_source_digest"].startswith("sha256:") for link in manifest["links"]
    )
    with pytest.raises(StoryCoverageError, match="actual pytest collection"):
        compile_story_test_links({path: source}, obligations, [])
    for replacement, match in (
        (source.replace("'test_case_0':", "'test_fiction':"), "unknown or empty"),
        (source.replace("'SC-", "'UNKNOWN-", 1), "unknown story obligation"),
        (
            source.replace("assert result[", "# assert result[", 1),
            "executable assertion",
        ),
        (
            source.replace("assert result['outcome'] == 'published'", "assert True"),
            "constant-only",
        ),
    ):
        with pytest.raises(StoryCoverageError, match=match):
            compile_story_test_links(
                {path: replacement}, obligations, collected["selectors"]
            )
    with pytest.raises(StoryCoverageError, match="lack collected assertion links"):
        compile_story_test_links(
            {path: source.split("__powdrr_story_links__")[0]},
            obligations,
            collected["selectors"],
        )
    incomplete = source.replace(
        "'observations': {'"
        + obligations[0]["story_refs"][0]["observation_ids"][0]
        + "'",
        "'observations': {'story:unknown:observation:1'",
        1,
    )
    with pytest.raises(StoryCoverageError, match="unknown observation"):
        compile_story_test_links(
            {path: incomplete}, obligations, collected["selectors"]
        )
    broken = tmp_path / "tests/test_broken.py"
    broken.write_text(
        "from absent_dependency import value\ndef test_broken():\n    assert value\n"
    )
    assert (
        collect_story_tests(
            PROFILE, root=tmp_path, paths=("tests/test_broken.py",), timeout_seconds=30
        )["status"]
        == "unavailable"
    )


def test_case_refs_survive_contracts_and_reject_stale_or_disconnected_walks(
    graph: dict[str, Any],
) -> None:
    catalog = parse_story_catalog(graph)
    ref = story_case_ref(catalog, graph["user_story_cases"][0]["id"])
    assert parse_story_case_refs([ref.to_data()]) == (ref,)
    contract = VerificationContract.from_mapping(
        {
            "id": "case-1",
            "description": "Resume a readable session",
            "story_refs": [ref.to_data()],
        }
    )
    assert VerificationContract.from_mapping(contract.to_data()).story_refs == (ref,)
    changed = copy.deepcopy(graph)
    changed["user_story_cases"][0]["input_examples"] = [{"available": True}]
    with pytest.raises(StoryCoverageError, match="stale or altered"):
        validate_story_case_refs((ref,), parse_story_catalog(changed))
    broken = copy.deepcopy(graph)
    broken["user_story_cases"][0]["transition_ids"].reverse()
    with pytest.raises(ValueError, match="case"):
        story_case_ref(parse_story_catalog(broken), ref.case_id)


class NoShortcoming:
    def complete_json(
        self, messages: list[dict[str, str]], *, response_schema: Any = None
    ) -> dict[str, Any]:
        return {"shortcoming_present": False}


class NoExplanation:
    def complete_json(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError("a negative decision needs no generative explanation")


def test_mocked_target_and_unavailable_story_review_cannot_count_as_adequate(
    graph: dict[str, Any],
) -> None:
    obligations = story_case_obligations(
        parse_story_catalog(graph), [case["id"] for case in graph["user_story_cases"]]
    )
    source = linked_source(obligations)
    manifest = compile_story_test_links(
        {"tests/test_resume.py": source},
        obligations,
        ["tests/test_resume.py::test_case_0", "tests/test_resume.py::test_case_1"],
    )
    evidence = {
        "test_sources": {"tests/test_resume.py": source},
        "test_obligations": obligations,
        "story_test_links": manifest,
    }
    report = review_execute_pr_test_quality(
        evidence, decision_client=NoShortcoming(), llm_client=NoExplanation()
    )
    require_story_test_quality(report, manifest)
    with pytest.raises(StoryCoverageError, match="unavailable"):
        require_story_test_quality({"assessments": []}, manifest)
    for assessment in report["assessments"]:
        if assessment["check_id"] == "mocked_target" and any(
            identity.startswith("SC-") for identity in assessment["criterion_ids"]
        ):
            assessment["decision_reports_shortcoming"] = True
            break
    with pytest.raises(StoryCoverageError, match="mocked_target"):
        require_story_test_quality(report, manifest)


@pytest.mark.parametrize("with_retry", [False, True])
def test_existing_miniswe_author_path_produces_pinned_collected_manifest(
    graph: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    with_retry: bool,
) -> None:
    if with_retry:
        graph = add_candidates(loop_graph(graph))
    repository = tmp_path / "repository"
    repository.mkdir()

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=repository, check=True, capture_output=True, text=True
        ).stdout.strip()

    git("init", "-q")
    git("config", "user.name", "Story Test")
    git("config", "user.email", "story@example.invalid")
    baseline_source = (
        "def resume(available, retries=0):\n"
        "    return {'outcome': 'lost', 'accepted': [], 'retries': -1}\n"
    )
    (repository / "app.py").write_text(baseline_source)
    (repository / "tests").mkdir()
    (repository / "tests/test_existing.py").write_text(
        "def test_existing():\n    assert 1 == 1\n"
    )
    git("add", ".")
    git("commit", "-qm", "fixture")
    base = git("rev-parse", "HEAD")
    ref = pin_story(graph, STORY)
    proposed: dict[str, Any] = {
        "title": "Resume sessions",
        "objective": "Resume without losing accepted decisions",
        "acceptance_criteria": [
            "Resume returns the specified outcome while preserving accepted decisions."
        ],
        "story_refs": [ref.to_data()],
        "story_context": story_planning_context(graph, (ref,)),
        "story_validation": True,
    }
    obligations = prepare_story_test_obligations(proposed)
    source = linked_source(obligations)
    prompts = []

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
            prompts.append(request.prompt)
            assert worktree_root != repository
            path = worktree_root / request.allowed_paths[0]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(source)
            subprocess.run(
                ["git", "add", "-f", request.allowed_paths[0]],
                cwd=worktree_root,
                check=True,
                capture_output=True,
            )
            return subprocess.CompletedProcess(
                ["scripted-miniswe"], 0, "authored\n", ""
            )

    monkeypatch.setattr(task_agent, "MiniSWEAgentProvider", ScriptedMiniSWE)
    request = ImplementationRequest(
        request_id="story-test",
        objective=proposed["objective"],
        prompt="",
        base_commit=base,
        plan_fingerprint="plan",
        allowed_paths=("app.py",),
        acceptance_criteria=tuple(proposed["acceptance_criteria"]),
        validation_profiles=("verify",),
    )
    sources, artifact = task_agent._author_execute_pr_tests(
        repository,
        base_commit=base,
        request=request,
        proposed=proposed,
        workflow_id="story-pr",
        config=task_agent.WorkflowTaskAgentConfig(
            workflow_dir=tmp_path / "workflow",
            repo_root=repository,
            coding_agent_validation_timeout_seconds=30,
        ),
        store_root=tmp_path / "run",
        validation_profile=PROFILE,
        quality_review_service=LocalPowdrrService(
            quality_review_clients=(NoShortcoming(), NoExplanation())
        ),
    )
    assert artifact["baseline_result"]["passed"] is False
    assert len(artifact["story_test_links"]["links"]) == len(obligations)
    assert set(obligation["obligation_id"] for obligation in obligations) <= set(
        artifact["coverage_references"]
    )
    assert all(
        "SC-" in prompt and "__powdrr_story_links__" in prompt for prompt in prompts
    )
    persisted = json.loads((tmp_path / "run/integration-tests/suite.json").read_text())
    assert persisted["story_test_links"] == artifact["story_test_links"]
    assert "app.py" not in sources
    assert (repository / "app.py").read_text() == baseline_source
    if with_retry:
        assert any("retries=2" in source for source in sources.values())
        assert any(
            assertion["observation_id"] == STORY + ":observation:3"
            for link in artifact["story_test_links"]["links"]
            for assertion in link["assertions"]
        )


def test_many_to_many_assertion_links_use_actual_pytest_selectors(
    graph: dict[str, Any], tmp_path: Path
) -> None:
    obligations = story_case_obligations(
        parse_story_catalog(graph), [case["id"] for case in graph["user_story_cases"]]
    )
    source = (
        "from app import resume\ndef test_both():\n"
        "    left, right = resume(True), resume(False)\n"
        "    assert left['outcome'] == 'published'\n"
        "    assert right['outcome'] == 'unavailable'\n"
        "def test_again():\n"
        "    assert resume(True)['accepted'] == ['kept']\n"
    )

    def request(index: int, line: int) -> dict[str, Any]:
        return {
            "obligation_id": obligations[index]["obligation_id"],
            "observations": {
                obligations[index]["story_refs"][0]["observation_ids"][0]: [line]
            },
            "description": "Establish the specified public response observation.",
            "boundary": "app.resume",
            "substitutions": [],
        }

    source += "__powdrr_story_links__ = " + repr(
        {"test_both": [request(0, 4), request(1, 5)], "test_again": [request(0, 7)]}
    )
    (tmp_path / "app.py").write_text(
        "def resume(available):\n"
        "    return {'outcome': 'published' if available else 'unavailable', "
        "'accepted': ['kept']}\n"
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_paths.py").write_text(source)
    collected = collect_story_tests(
        PROFILE, root=tmp_path, paths=("tests/test_paths.py",), timeout_seconds=30
    )
    manifest = compile_story_test_links(
        {"tests/test_paths.py": source}, obligations, collected["selectors"]
    )
    assert len(manifest["links"]) == 3
    assert (
        sum(link["selector"].endswith("::test_both") for link in manifest["links"]) == 2
    )
    assert (
        sum(
            link["obligation_id"] == obligations[0]["obligation_id"]
            for link in manifest["links"]
        )
        == 2
    )


def test_mocked_module_wiring_is_available_to_required_semantic_review(
    graph: dict[str, Any],
) -> None:
    obligations = story_case_obligations(
        parse_story_catalog(graph), [case["id"] for case in graph["user_story_cases"]]
    )
    source = linked_source(obligations).replace(
        "from app import resume",
        "from unittest.mock import Mock; resume = Mock(return_value={})",
    )
    manifest = compile_story_test_links(
        {"tests/test_mocked.py": source},
        obligations,
        ["tests/test_mocked.py::test_case_0", "tests/test_mocked.py::test_case_1"],
    )

    class MockConcern:
        def complete_json(
            self, messages: list[dict[str, str]], *, response_schema: Any = None
        ) -> dict[str, Any]:
            text = messages[-1]["content"]
            if "Does this test mock the behavior or wiring" in text:
                assert "test_module_source" in text and "unittest.mock" in text
                return {"shortcoming_present": True}
            return {"shortcoming_present": False}

    class Explanation:
        def complete_json(
            self, messages: Any, *, response_schema: Any = None
        ) -> dict[str, Any]:
            return {
                "shortcoming_present": True,
                "explanation": "The target is replaced by Mock at module scope.",
            }

    report = review_execute_pr_test_quality(
        {
            "test_sources": {"tests/test_mocked.py": source},
            "test_obligations": obligations,
            "story_test_links": manifest,
        },
        decision_client=MockConcern(),
        llm_client=Explanation(),
        suggest_tests=False,
    )
    with pytest.raises(StoryCoverageError, match="mocked_target"):
        require_story_test_quality(report, manifest)


def test_story_refs_survive_existing_scenario_and_obligation_contracts(
    graph: dict[str, Any],
) -> None:
    from powdrr_service.behavior_contract import compile_behavior_scenarios
    from powdrr_service.structrr.verification_obligations import VerificationObligation
    from powdrr_service.verification.story_test_authoring import (
        require_supported_story_author,
    )
    from test_behavior_contract import _scenario

    ref = story_case_ref(parse_story_catalog(graph), graph["user_story_cases"][0]["id"])
    raw = {**_scenario(), "story_refs": [ref.to_data()]}
    scenario = compile_behavior_scenarios((raw,))[0]
    assert compile_behavior_scenarios((scenario.to_data(),))[0].story_refs == (ref,)
    obligation = VerificationObligation(
        "obligation-1",
        "contract-1",
        "sha256:" + "1" * 64,
        ("intent-1",),
        "pytest",
        "tests/test_paths.py::test_both",
        "verify",
        "pass",
        {},
        "Required",
        (),
        "sha256:" + "2" * 64,
        "sha256:" + "3" * 64,
        (ref,),
    )
    assert VerificationObligation.from_data(obligation.to_data()).story_refs == (ref,)
    with pytest.raises(StoryCoverageError, match="unsupported_story_test_author"):
        require_supported_story_author(
            ("go", "test", "./..."), [{"story_refs": [ref.to_data()]}]
        )
