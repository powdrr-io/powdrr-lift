from __future__ import annotations

import hashlib
import subprocess
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from powdrr_lift import cli
from powdrr_lift.workrr import jev_classifier
from powdrr_lift.workrr.github_issue_plan_generation import (
    PLAN_COVERAGE_AUDIT_RESPONSE_SCHEMA,
    PLAN_ITEMS_RESPONSE_SCHEMA,
    PlanGenerationError,
    PlanPullRequest,
    _apply_plan_coverage_assessments,
    _ensure_ci_bootstrap,
    _plan_item_refinement_issues,
    _validated_plan_coverage_assessments,
    _validated_plan_coverage_assessments_with_repair,
    generate_execution_plan_yaml,
    generate_multi_pr_structrr_proposal,
    inspect_repository_ci,
    inspect_repository_code,
    load_plan_workflow_instance,
    publish_execution_plan_pull_request,
    write_execution_plan_artifacts,
)
from powdrr_lift.workrr.github_issue_queue import (
    PlannedWorkItem,
    QueueIssue,
    ReviewedExecutionPlan,
)
from powdrr_lift.workrr.multipr_structrr import compile_multi_pr_proposal
from powdrr_lift.workrr.plan_sizing import PlanSizingError, size_plan_items


@pytest.fixture(autouse=True)
def _disable_live_semantic_providers(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("TYPESAFEAI_API_KEY", "TYPESAFE_API_KEY", "SYSTEM_ONE_API_KEY"):
        monkeypatch.delenv(name, raising=False)


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
        if response_schema == PLAN_COVERAGE_AUDIT_RESPONSE_SCHEMA:
            return {
                "assessments": [
                    {
                        "item_id": str(item["id"]),
                        "status": "missing",
                        "scope_assessments": [
                            {
                                "requirement_id": f"{item['id']}:scope-{index:02d}",
                                "status": "missing",
                                "existing_evidence": [],
                                "remaining_requirement": "",
                            }
                            for index, _scope in enumerate(
                                item.get("scope", []), start=1
                            )
                        ],
                        "remaining_work": "",
                        "remaining_item": {
                            "title": "",
                            "objective": "",
                            "scope": [],
                            "allowed_paths": [],
                            "implementation_steps": [],
                            "acceptance_criteria": [],
                        },
                    }
                    for item in self.response.get("items", [])
                ]
            }
        return self.response


class SequencedPlanningClient:
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = responses
        self.messages: list[list[dict[str, str]]] = []

    def complete_json(
        self,
        messages: list[dict[str, str]],
        *,
        response_schema: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        del response_schema
        self.messages.append(messages)
        return self.responses.pop(0)


def _item(item_id: str, *, dependencies: list[str] | None = None) -> dict[str, Any]:
    return {
        "id": item_id,
        "title": f"Implement {item_id}",
        "objective": f"Deliver {item_id} from the plan.",
        "scope": [f"component: {item_id}"],
        "allowed_paths": ["."],
        "implementation_steps": [
            f"Implement the smallest behavior for {item_id}.",
            f"Add focused tests proving {item_id} works.",
        ],
        "acceptance_criteria": [f"{item_id} meets its documented acceptance criteria."],
        "dependencies": dependencies or [],
    }


def _empty_structrr_proposal(plan: Any) -> Any:
    return compile_multi_pr_proposal(
        plan_id=plan.plan_id,
        baseline_snapshot={
            "entities": [],
            "entity_relationships": [],
            "source_subjects": [],
            "active_intent": [],
            "human-decisions": [],
            "invariants": [],
        },
        items=[
            {
                "id": item.item_id,
                "acceptance_criteria": list(item.acceptance_criteria),
                "allowed_paths": list(item.allowed_paths),
            }
            for item in plan.items
        ],
        proposed_operations=[],
        source_ref=f"plan:{plan.plan_id}",
    )


def test_plan_refinement_detects_cross_runtime_implementation_only() -> None:
    cross_runtime_item = {
        "id": "protocol",
        "scope": ["Implement Python parsers and Swift parsers for the protocol."],
        "implementation_steps": [],
    }
    parity_only_item = {
        "id": "swift-parity",
        "scope": ["Implement Swift parsers for the protocol."],
        "implementation_steps": [
            "Add tests comparing Swift parser output with Python fixtures."
        ],
    }

    assert _plan_item_refinement_issues([cross_runtime_item]) == [
        "protocol combines implementation runtimes: python, swift"
    ]
    assert _plan_item_refinement_issues([parity_only_item]) == []


def test_plan_sizing_groups_cohesive_items_to_target_work_size() -> None:
    first = _item("codec-model")
    first["scope"] = ["component: codec", "Define codec model"]
    first["allowed_paths"] = ["src/codec/model.py"]
    first["implementation_steps"] = ["Define the typed codec model."]
    second = _item("codec-parser", dependencies=["codec-model"])
    second["scope"] = ["component: codec", "Parse codec input"]
    second["allowed_paths"] = ["src/codec/parser.py"]
    second["implementation_steps"] = [
        "Parse codec input into the typed model.",
        "Add focused parser tests.",
    ]

    sized = size_plan_items([first, second])

    assert len(sized) == 1
    assert sized[0]["id"] == "codec-model"
    assert sized[0]["title"] == "Implement codec-model; Implement codec-parser"
    assert len(sized[0]["implementation_steps"]) == 3
    assert sized[0]["dependencies"] == []
    assert size_plan_items([first, second]) == sized
    assert sized[0]["allowed_paths"] == [
        "src/codec/model.py",
        "src/codec/parser.py",
    ]


def test_plan_sizing_keeps_manual_prerequisite_on_its_own_work_item() -> None:
    prerequisite_item = _item("credential-setup")
    prerequisite_item["scope"] = ["component: repository", "Use CI credential"]
    prerequisite_item["allowed_paths"] = ["src/ci/client.py"]
    prerequisite_item["implementation_steps"] = ["Implement CI credential use."]
    prerequisite_item["manual_prerequisite"] = {
        "title": "Install the CI credential",
        "instructions": ["Add the repository secret."],
        "verification": "Confirm the secret is configured.",
        "trigger": "after_implementation_before_validation",
        "unblocks": "Running the workflow that uses the credential.",
    }
    followup = _item("credential-tests", dependencies=["credential-setup"])
    followup["scope"] = ["component: repository", "Test credential workflow"]
    followup["allowed_paths"] = ["tests/ci/test_client.py"]
    followup["implementation_steps"] = ["Add credential workflow tests."]

    sized = size_plan_items([prerequisite_item, followup])

    assert [item["id"] for item in sized] == ["credential-setup", "credential-tests"]
    assert sized[0]["manual_prerequisite"] == prerequisite_item["manual_prerequisite"]
    assert sized[1]["dependencies"] == ["credential-setup"]


def test_plan_sizing_keeps_incompatible_or_unrelated_items_separate() -> None:
    python_item = _item("python-codec")
    python_item["scope"] = ["component: codec", "Implement Python codec"]
    python_item["allowed_paths"] = ["src/codec/python.py"]
    swift_item = _item("swift-codec", dependencies=["python-codec"])
    swift_item["scope"] = ["component: codec", "Implement Swift codec"]
    swift_item["allowed_paths"] = ["src/codec/swift.swift"]
    swift_item["implementation_steps"] = ["Implement Swift codec behavior."]

    sized = size_plan_items([python_item, swift_item])

    assert [item["id"] for item in sized] == ["python-codec", "swift-codec"]
    assert sized[1]["dependencies"] == ["python-codec"]

    first_component = _item("first-component")
    first_component["scope"] = ["component: first"]
    first_component["allowed_paths"] = ["src/first/feature.py"]
    first_component["implementation_steps"] = ["Implement first component behavior."]
    second_component = _item("second-component")
    second_component["scope"] = ["component: second"]
    second_component["allowed_paths"] = ["src/second/feature.py"]
    second_component["implementation_steps"] = ["Implement second component behavior."]
    unrelated = size_plan_items([first_component, second_component])

    assert [item["id"] for item in unrelated] == [
        "first-component",
        "second-component",
    ]


def test_plan_sizing_splits_mixed_runtime_items_before_optimizing() -> None:
    mixed = {
        "id": "PR-03",
        "title": "Implement portable protocol parsers",
        "objective": "Provide compatible parser implementations.",
        "scope": [
            "component: protocol",
            "Implement the Python parser.",
            "Implement the Swift parser.",
        ],
        "allowed_paths": [
            "src/protocol/parser.py",
            "Sources/Protocol/Parser.swift",
        ],
        "implementation_steps": [
            "Implement the Python parser module.",
            "Add tests for the Python parser.",
            "Implement the Swift parser module.",
            "Add tests for the Swift parser.",
        ],
        "acceptance_criteria": [
            "The Python parser rejects duplicate keys.",
            "The Swift parser rejects duplicate keys.",
            "Both parsers reject unsupported fields.",
        ],
        "dependencies": ["PR-01"],
    }
    downstream = _item("PR-04", dependencies=["PR-03"])
    downstream["scope"] = ["component: docs", "Document parser behavior"]
    downstream["allowed_paths"] = ["docs/protocol.md"]
    downstream["implementation_steps"] = ["Document parser configuration."]

    sized = size_plan_items([mixed, downstream])

    assert [item["id"] for item in sized] == ["PR-03", "PR-03-swift", "PR-04"]
    assert [item["implementation_steps"] for item in sized[:2]] == [
        [
            "Implement the Python parser module.",
            "Add tests for the Python parser.",
        ],
        [
            "Implement the Swift parser module.",
            "Add tests for the Swift parser.",
        ],
    ]
    assert [item["dependencies"] for item in sized] == [
        ["PR-01"],
        ["PR-01"],
        ["PR-03", "PR-03-swift"],
    ]
    assert all(len(_plan_item_refinement_issues([item])) == 0 for item in sized)


def test_plan_generation_splits_mixed_runtime_response_before_refinement() -> None:
    foundation = _item("PR-01")
    foundation["scope"] = ["component: setup", "Establish shared protocol fixtures"]
    foundation["allowed_paths"] = ["fixtures/protocol.json"]
    foundation["implementation_steps"] = ["Add the shared protocol fixture."]
    mixed = {
        "id": "PR-03",
        "title": "Implement portable protocol parsers",
        "objective": "Provide compatible parser implementations.",
        "scope": [
            "component: protocol",
            "Implement the Python parser.",
            "Implement the Swift parser.",
        ],
        "allowed_paths": [
            "src/protocol/parser.py",
            "Sources/Protocol/Parser.swift",
        ],
        "implementation_steps": [
            "Implement the Python parser module.",
            "Add tests for the Python parser.",
            "Implement the Swift parser module.",
            "Add tests for the Swift parser.",
        ],
        "acceptance_criteria": [
            "The Python parser rejects duplicate keys.",
            "The Swift parser rejects duplicate keys.",
        ],
        "dependencies": ["PR-01"],
    }
    downstream = _item("PR-04", dependencies=["PR-03"])
    downstream["scope"] = ["component: docs", "Document parser behavior"]
    downstream["allowed_paths"] = ["docs/protocol.md"]
    downstream["implementation_steps"] = ["Document parser configuration."]
    client = FakePlanningClient({"items": [foundation, mixed, downstream]})

    _rendered, plan = generate_execution_plan_yaml(
        source_plan="# Plan\nImplement portable parsers and document them.",
        plan_id="runtime-split-v1",
        client=client,
        verification_command="make test",
        repository_ci_context="Recognized CI runs make test.",
    )

    assert client.messages is not None and len(client.messages) == 2
    assert [item.item_id for item in plan.items] == [
        "PR-01",
        "PR-03",
        "PR-03-swift",
        "PR-04",
    ]
    assert plan.items[2].dependencies == ("PR-01",)
    assert plan.items[3].dependencies == ("PR-03", "PR-03-swift")


def test_final_planning_correction_is_split_before_validation() -> None:
    unsplittable = _item("PR-03")
    unsplittable["scope"] = [
        "component: protocol",
        "Implement the Python and Swift parser modules.",
    ]
    unsplittable["allowed_paths"] = [
        "src/protocol/parser.py",
        "Sources/Protocol/Parser.swift",
    ]
    unsplittable["implementation_steps"] = [
        "Implement the Python parser module.",
        "Add tests for the Python parser.",
        "Implement the Swift parser module.",
        "Add tests for the Swift parser.",
    ]
    corrected = dict(unsplittable)
    corrected["scope"] = [
        "component: protocol",
        "Implement the Python parser.",
        "Implement the Swift parser.",
    ]
    client = SequencedPlanningClient(
        [{"items": [unsplittable]}] * 6 + [{"items": [corrected]}]
    )

    _, plan = generate_execution_plan_yaml(
        source_plan="# Plan\nImplement Python and Swift parsers.",
        plan_id="final-runtime-split-v1",
        client=client,
        repository_ci_context="Recognized CI runs make test.",
    )

    assert [item.item_id for item in plan.items] == ["PR-03", "PR-03-swift"]
    assert len(client.messages) == 7


def test_plan_sizing_rejects_ambiguous_mixed_runtime_steps() -> None:
    mixed = {
        "id": "protocol",
        "title": "Implement protocol parsers",
        "objective": "Provide compatible parser implementations.",
        "scope": [
            "component: protocol",
            "Implement the Python parser.",
            "Implement the Swift parser.",
        ],
        "allowed_paths": ["src/protocol.py", "Sources/Protocol.swift"],
        "implementation_steps": ["Implement the Python and Swift parser modules."],
        "acceptance_criteria": ["Both parsers reject unsupported fields."],
        "dependencies": [],
    }

    with pytest.raises(PlanSizingError, match="each implementation step"):
        size_plan_items([mixed])


def test_plan_sizing_never_merges_ci_bootstrap_with_product_work() -> None:
    bootstrap = {
        **_item("ci-bootstrap"),
        "title": "Establish CI validation",
        "scope": ["component: repository", "Add CI workflow"],
        "allowed_paths": [".github/workflows/verify.yml"],
        "implementation_steps": ["Run verification in pull request CI."],
    }
    product = _item("feature", dependencies=["ci-bootstrap"])
    product["scope"] = ["component: repository", "Add the feature"]
    product["allowed_paths"] = [".github/workflows/verify.yml"]
    product["implementation_steps"] = ["Implement the feature."]

    sized = size_plan_items([bootstrap, product])

    assert [item["id"] for item in sized] == ["ci-bootstrap", "feature"]
    assert sized[1]["dependencies"] == ["ci-bootstrap"]


def test_plan_requires_ci_bootstrap_and_dependency_when_repository_has_no_ci() -> None:
    ci_item = _item("ci-bootstrap")
    ci_item["title"] = "Set up CI validation"
    ci_item["implementation_steps"] = [
        "Add a GitHub Actions continuous integration workflow.",
        "Run scripts/verify-all.sh in CI.",
    ]
    product_item = _item("feature", dependencies=["ci-bootstrap"])
    assert (
        _plan_item_refinement_issues(
            [ci_item, product_item],
            require_ci_bootstrap=True,
            verification_command="scripts/verify-all.sh",
        )
        == []
    )
    assert _plan_item_refinement_issues(
        [_item("foundation"), product_item], require_ci_bootstrap=True
    ) == [
        "foundation must establish continuous integration before the product work",
        "feature must depend on the initial CI item foundation",
    ]


def test_planner_inserts_ci_bootstrap_when_model_omits_the_initial_gate() -> None:
    items = [
        _item("verifier"),
        _item("cli", dependencies=["verifier"]),
    ]

    repaired = _ensure_ci_bootstrap(items, "bash attestation/scripts/verify-all.sh")

    assert repaired[0]["id"] == "ci-bootstrap"
    assert repaired[0]["allowed_paths"] == [".github/workflows/verify.yml"]
    assert "bash attestation/scripts/verify-all.sh" in str(repaired[0])
    assert repaired[1]["dependencies"] == ["ci-bootstrap"]
    assert repaired[2]["dependencies"] == ["verifier", "ci-bootstrap"]
    assert (
        _plan_item_refinement_issues(
            repaired,
            require_ci_bootstrap=True,
            verification_command="bash attestation/scripts/verify-all.sh",
        )
        == []
    )


def test_planner_repairs_ci_item_missing_exact_verification_command() -> None:
    ci_item = _item("ci-bootstrap")
    ci_item["title"] = "Set up continuous integration"
    ci_item["implementation_steps"] = ["Add a GitHub Actions workflow."]
    ci_item["allowed_paths"] = [".github/workflows/verify.yml"]
    feature_item = _item("verifier")

    repaired = _ensure_ci_bootstrap(
        [ci_item, feature_item], "bash attestation/scripts/verify-all.sh"
    )

    assert len(repaired) == 2
    assert "bash attestation/scripts/verify-all.sh" in str(repaired[0])
    assert repaired[1]["dependencies"] == ["ci-bootstrap"]
    assert (
        _plan_item_refinement_issues(
            repaired,
            require_ci_bootstrap=True,
            verification_command="bash attestation/scripts/verify-all.sh",
        )
        == []
    )


def test_inspect_repository_ci_reports_existing_workflow_and_missing_ci(
    tmp_path: Path,
) -> None:
    assert inspect_repository_ci(tmp_path).startswith(
        "No recognized CI configuration files were found."
    )
    workflow = tmp_path / ".github/workflows/verify.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text("name: Verify\nrun: scripts/verify-all.sh\n", encoding="utf-8")
    context = inspect_repository_ci(tmp_path)
    assert "Recognized CI configuration files" in context
    assert ".github/workflows/verify.yml" in context
    assert "scripts/verify-all.sh" in context


def test_inspect_repository_code_follows_up_with_symbol_and_test_lookups(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    source = tmp_path / "src/attestation/verifier.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "def verify_signature(payload):\n"
        "    return verify_ed25519_signature(payload)\n",
        encoding="utf-8",
    )
    test = tmp_path / "tests/test_verifier.py"
    test.parent.mkdir(parents=True)
    test.write_text(
        "def test_ed25519_signature_rejects_invalid_payload():\n"
        "    assert not verify_signature(b'invalid')\n",
        encoding="utf-8",
    )
    unrelated = tmp_path / "src/catalog/colors.py"
    unrelated.parent.mkdir(parents=True)
    unrelated.write_text("def blue():\n    return 'blue'\n", encoding="utf-8")

    lookups: list[str] = []

    def lookup(
        queries: list[str], *, worktree_root: Path, limit: int
    ) -> dict[str, list[dict[str, Any]]]:
        assert worktree_root == tmp_path
        assert limit == 20
        lookups.extend(queries)
        return {
            query: [
                {
                    "name": query,
                    "path": (
                        "src/attestation/verifier.py"
                        if query == "verify_signature"
                        else "tests/test_verifier.py"
                    ),
                    "range": {"start_line": 1, "end_line": 2},
                }
            ]
            for query in queries
        }

    monkeypatch.setattr(
        "powdrr_lift.workrr.github_issue_plan_generation.find_workspace_symbols",
        lookup,
    )
    client = SequencedPlanningClient(
        [
            {"queries": ["verify_signature"], "done": False},
            {"queries": ["test_verify_signature"], "done": True},
        ]
    )
    context = inspect_repository_code(
        tmp_path,
        "Implement Ed25519 signature verification for attestation payloads.",
        client,
    )

    assert lookups == ["verify_signature", "test_verify_signature"]
    assert len(client.messages) == 2
    assert "verify_signature" in client.messages[1][1]["content"]
    assert "src/attestation/verifier.py" in context
    assert "tests/test_verifier.py" in context
    assert "verify_ed25519_signature" in context
    assert "assert not verify_signature(b'invalid')" in context
    assert "Analyzed checkout: commit unborn HEAD" in context
    assert "working tree has changes: yes" in context
    assert "working-tree status ??; not in analyzed commit" in context


def test_planner_repairs_missing_ci_bootstrap_and_blocks_later_items() -> None:
    original = {
        "items": [
            _item("foundation"),
            _item("integration", dependencies=["foundation"]),
        ]
    }
    ci_item = _item("ci-bootstrap")
    ci_item["title"] = "Set up continuous integration"
    ci_item["objective"] = "Run repository verification in CI before feature work."
    ci_item["allowed_paths"] = [".github/workflows/verify.yml"]
    ci_item["implementation_steps"] = [
        "Add .github/workflows/verify.yml.",
        "Run scripts/verify-all.sh in the CI workflow.",
    ]
    repaired = {
        "items": [
            ci_item,
            _item("foundation", dependencies=["ci-bootstrap"]),
            _item("integration", dependencies=["foundation", "ci-bootstrap"]),
        ]
    }
    client = SequencedPlanningClient([original, original, repaired])

    _, plan = generate_execution_plan_yaml(
        source_plan="# Plan\nImplement a foundation, then integration.",
        plan_id="ci-first-v1",
        client=client,
        verification_command="scripts/verify-all.sh",
        repository_ci_context=("No recognized CI configuration files were found."),
        repository_code_context=(
            "Existing source and tests include a partial signature verifier."
        ),
    )

    assert len(client.messages) == 3
    assert [item.item_id for item in plan.items] == [
        "ci-bootstrap",
        "foundation",
        "integration",
    ]
    assert plan.items[0].dependencies == ()
    assert plan.items[1].dependencies == ("ci-bootstrap",)
    assert plan.items[2].dependencies == ("foundation", "ci-bootstrap")
    repair_prompt = client.messages[2][1]["content"]
    assert "continuous integration before the product work" in repair_prompt
    assert "scripts/verify-all.sh" in repair_prompt
    assert "partial signature verifier" in repair_prompt


def test_plan_generation_repairs_persistent_missing_ci_gate() -> None:
    uncorrected = {
        "items": [
            _item("verifier"),
            _item("cli", dependencies=["verifier"]),
        ]
    }
    client = SequencedPlanningClient([uncorrected] * 8)

    _, plan = generate_execution_plan_yaml(
        source_plan="# Plan\nImplement the verifier and CLI.",
        plan_id="validation-gate-v1",
        client=client,
        verification_command="bash attestation/scripts/verify-all.sh",
        repository_ci_context="No recognized CI configuration files were found.",
    )

    assert [item.item_id for item in plan.items] == [
        "ci-bootstrap",
        "verifier",
        "cli",
    ]
    assert plan.items[0].dependencies == ()
    assert plan.items[1].dependencies == ("ci-bootstrap",)
    assert plan.items[2].dependencies == ("verifier", "ci-bootstrap")
    assert "bash attestation/scripts/verify-all.sh" in str(plan.items[0])


def test_generate_execution_plan_yaml_compiles_without_jev(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_if_called(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("plan generation must not call Jev")

    monkeypatch.setenv("TYPESAFEAI_API_KEY", "test-key")
    monkeypatch.setattr(jev_classifier, "_call_jev", fail_if_called)
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
        verification_command="scripts/verify-all.sh",
        repository_ci_context=(
            "Recognized CI configuration files:\n"
            "--- .github/workflows/verify.yml ---\n"
            "run: scripts/verify-all.sh"
        ),
        repository_code_context=(
            "Existing repository evidence: src/attestation/verifier.py defines "
            "verify_signature and tests cover invalid payloads."
        ),
    )

    assert client.response_schema == PLAN_ITEMS_RESPONSE_SCHEMA
    assert client.messages is not None
    assert "Implement a foundation" in client.messages[1]["content"]
    assert [item.item_id for item in plan.items] == ["foundation", "integration"]
    assert plan.items[1].dependencies == ("foundation",)
    assert plan.items[0].allowed_paths == (".",)
    assert plan.items[0].implementation_steps == (
        "Implement the smallest behavior for foundation.",
        "Add focused tests proving foundation works.",
    )
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
    assert "implementation_steps:" in rendered
    assert "allowed_paths:" in rendered
    assert "Implement the smallest behavior for foundation." in rendered
    assert "implementation step" in client.messages[0]["content"]
    assert "filesystem paths" in client.messages[0]["content"]
    assert "runs the supplied verification command" in client.messages[0]["content"]
    assert "remove already implemented work" in client.messages[0]["content"]
    assert (
        "Test files do not establish a passing result" in client.messages[0]["content"]
    )
    assert "scripts/verify-all.sh" in client.messages[1]["content"]
    assert "verify_signature" in client.messages[1]["content"]


def test_generate_execution_plan_yaml_uses_deterministic_sizing() -> None:
    first = _item("codec-model")
    first["scope"] = ["component: codec", "Define codec model"]
    first["allowed_paths"] = ["src/codec/model.py"]
    first["implementation_steps"] = ["Define the typed codec model."]
    second = _item("codec-parser", dependencies=["codec-model"])
    second["scope"] = ["component: codec", "Parse codec input"]
    second["allowed_paths"] = ["src/codec/parser.py"]
    second["implementation_steps"] = [
        "Parse codec input into the typed model.",
        "Add focused parser tests.",
    ]
    client = FakePlanningClient({"items": [first, second]})

    _rendered, plan = generate_execution_plan_yaml(
        source_plan="# Plan\nDefine and parse a codec.",
        plan_id="codec-v1",
        client=client,
    )

    assert [item.item_id for item in plan.items] == ["codec-model"]
    assert len(plan.items[0].implementation_steps) == 3
    assert plan.items[0].dependencies == ()


def test_plan_generation_reconciles_draft_with_targeted_repository_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    implementation = tmp_path / "src/schema.py"
    implementation.parent.mkdir(parents=True)
    implementation.write_text("def parse_policy(): pass\n", encoding="utf-8")
    test_file = tmp_path / "tests/test_parsers.py"
    test_file.parent.mkdir(parents=True)
    test_file.write_text(
        "def test_parse_policy():\n    assert parse_policy() is None\n",
        encoding="utf-8",
    )
    verifier = tmp_path / "src/verifier.py"
    verifier.write_text("def verify_signature(): pass\n", encoding="utf-8")
    verifier_test = tmp_path / "tests/test_verifier.py"
    verifier_test.write_text(
        "def test_verify_signature():\n    assert verify_signature()\n",
        encoding="utf-8",
    )
    portable_item = _item("portable-verifier", dependencies=["schema-parsers"])
    portable_item["scope"].append("Add the missing stable rejection reason API.")
    draft = {
        "items": [
            _item("schema-parsers"),
            portable_item,
        ]
    }
    reconciled = {"items": [_item("portable-verifier")]}
    audit = {
        "assessments": [
            {
                "item_id": "schema-parsers",
                "status": "complete",
                "scope_assessments": [
                    {
                        "requirement_id": "schema-parsers:scope-01",
                        "status": "complete",
                        "existing_evidence": [
                            {
                                "path": "src/schema.py",
                                "symbol": "parse_policy",
                                "kind": "implementation",
                                "rationale": "The parser implements the contract.",
                            },
                            {
                                "path": "tests/test_parsers.py",
                                "symbol": "test_parse_policy",
                                "kind": "test",
                                "rationale": "The parser behavior has a focused test.",
                            },
                        ],
                        "remaining_requirement": "",
                    }
                ],
                "remaining_work": "",
                "remaining_item": {
                    "title": "",
                    "objective": "",
                    "scope": [],
                    "allowed_paths": [],
                    "implementation_steps": [],
                    "acceptance_criteria": [],
                },
            },
            {
                "item_id": "portable-verifier",
                "status": "partial",
                "scope_assessments": [
                    {
                        "requirement_id": "portable-verifier:scope-01",
                        "status": "partial",
                        "existing_evidence": [
                            {
                                "path": "src/verifier.py",
                                "symbol": "verify_signature",
                                "kind": "implementation",
                                "rationale": "A signature helper already exists.",
                            },
                            {
                                "path": "tests/test_verifier.py",
                                "symbol": "test_verify_signature",
                                "kind": "test",
                                "rationale": "The current helper has a focused test.",
                            },
                        ],
                        "remaining_requirement": (
                            "Add the missing authorization decision API."
                        ),
                    },
                    {
                        "requirement_id": "portable-verifier:scope-02",
                        "status": "missing",
                        "existing_evidence": [
                            {
                                "path": "src/verifier.py",
                                "symbol": "verify_signature",
                                "kind": "implementation",
                                "rationale": (
                                    "This helper verifies signatures but does not "
                                    "provide stable rejection reasons."
                                ),
                            },
                            {
                                "path": "tests/test_verifier.py",
                                "symbol": "test_verify_signature",
                                "kind": "test",
                                "rationale": (
                                    "This test does not exercise authorization "
                                    "rejection reasons."
                                ),
                            },
                        ],
                        "remaining_requirement": (
                            "Add the missing stable rejection reason API."
                        ),
                    },
                ],
                "remaining_work": "Add the missing authorization decision API.",
                "remaining_item": {
                    "title": "Complete verifier authorization decisions",
                    "objective": "Add only the missing authorization decision API.",
                    "scope": ["Implement stable authorization rejection decisions."],
                    "allowed_paths": ["src/verifier.py"],
                    "implementation_steps": [
                        "Implement authorization decisions for verifier inputs."
                    ],
                    "acceptance_criteria": [
                        "Each authorization failure returns a stable reason."
                    ],
                },
            },
        ]
    }
    client = SequencedPlanningClient([draft, draft, audit, reconciled])
    inspected_contexts: list[str] = []

    def inspect_draft(_repo_root: Path, source_plan: str, _client: Any) -> str:
        inspected_contexts.append(source_plan)
        return (
            "Current checkout evidence: attestation/verifier/parsers.py implements "
            "strict policy parsing; tests/test_parsers.py covers duplicate keys."
        )

    monkeypatch.setattr(
        "powdrr_lift.workrr.github_issue_plan_generation.inspect_repository_code",
        inspect_draft,
    )
    _, plan = generate_execution_plan_yaml(
        source_plan="# Plan\nImplement strict parsers and a portable verifier.",
        plan_id="current-state-reconciliation-v1",
        client=client,
        repository_ci_context=(
            "Recognized CI configuration files:\n"
            "--- .github/workflows/verify.yml ---\n"
            "run: bash attestation/scripts/verify-all.sh"
        ),
        repository_code_context="Initial evidence mentions the verifier package.",
        repository_root=tmp_path,
    )

    assert len(client.messages) == 4
    assert len(inspected_contexts) == 1
    assert "schema-parsers" in inspected_contexts[0]
    audit_prompt = client.messages[2][1]["content"]
    assert "portable-verifier:scope-02" in audit_prompt
    audit_instructions = client.messages[2][0]["content"]
    assert "Assess every supplied scope requirement separately" in audit_instructions
    reconciliation_prompt = client.messages[3][1]["content"]
    assert "Current checkout evidence" in reconciliation_prompt
    assert "schema-parsers" in reconciliation_prompt
    assert [item.item_id for item in plan.items] == ["portable-verifier"]
    assert plan.items[0].title == "Complete verifier authorization decisions"
    assert plan.items[0].scope == (
        "Add the missing authorization decision API.",
        "Add the missing stable rejection reason API.",
    )


def test_incomplete_coverage_audit_preserves_unassessed_items(tmp_path: Path) -> None:
    audit = {
        "assessments": [
            {
                "item_id": "reviewed",
                "status": "missing",
                "scope_assessments": [
                    {
                        "requirement_id": "reviewed:scope-01",
                        "status": "missing",
                        "existing_evidence": [],
                        "remaining_requirement": "",
                    }
                ],
                "remaining_work": "",
                "remaining_item": {
                    "title": "",
                    "objective": "",
                    "scope": [],
                    "allowed_paths": [],
                    "implementation_steps": [],
                    "acceptance_criteria": [],
                },
            }
        ]
    }

    assessments = _validated_plan_coverage_assessments(
        audit,
        [_item("reviewed"), _item("unassessed")],
        repository_root=tmp_path,
    )

    assert assessments["reviewed"]["status"] == "missing"
    assert assessments["unassessed"]["status"] == "missing"
    assert assessments["unassessed"]["scope_assessments"] == [
        {
            "requirement_id": "unassessed:scope-01",
            "status": "missing",
            "existing_evidence": [],
            "remaining_requirement": "",
        }
    ]


def test_coverage_audit_repairs_a_fabricated_repository_path(
    tmp_path: Path,
) -> None:
    item = _item("parser")
    invalid_audit = {
        "assessments": [
            {
                "item_id": "parser",
                "status": "complete",
                "scope_assessments": [
                    {
                        "requirement_id": "parser:scope-01",
                        "status": "complete",
                        "existing_evidence": [
                            {
                                "path": ".github/workflows/ci.yml",
                                "symbol": "ci",
                                "kind": "implementation",
                                "rationale": "The CI config implements the parser.",
                            },
                            {
                                "path": ".github/workflows/ci.yml",
                                "symbol": "ci",
                                "kind": "test",
                                "rationale": "The CI config tests the parser.",
                            },
                        ],
                        "remaining_requirement": "",
                    }
                ],
                "remaining_work": "",
                "remaining_item": {
                    "title": "",
                    "objective": "",
                    "scope": [],
                    "allowed_paths": [],
                    "implementation_steps": [],
                    "acceptance_criteria": [],
                },
            }
        ]
    }
    corrected_audit = {
        "assessments": [
            {
                "item_id": "parser",
                "status": "missing",
                "scope_assessments": [
                    {
                        "requirement_id": "parser:scope-01",
                        "status": "missing",
                        "existing_evidence": [],
                        "remaining_requirement": "",
                    }
                ],
                "remaining_work": "",
                "remaining_item": {
                    "title": "",
                    "objective": "",
                    "scope": [],
                    "allowed_paths": [],
                    "implementation_steps": [],
                    "acceptance_criteria": [],
                },
            }
        ]
    }
    client = SequencedPlanningClient([corrected_audit])

    assessments = _validated_plan_coverage_assessments_with_repair(
        invalid_audit,
        [item],
        repository_root=tmp_path,
        client=client,
        source_plan="# Plan\nImplement a parser.",
        repository_code_context="The repository has no matching parser code.",
        draft_code_context="No parser symbols were found.",
    )

    assert assessments["parser"]["status"] == "missing"
    assert len(client.messages) == 1
    repair_request = client.messages[0][1]["content"]
    assert ".github/workflows/ci.yml" in repair_request
    assert "verified evidence" in repair_request


def test_missing_coverage_assessment_can_retain_the_full_draft_item() -> None:
    remaining_item = {
        "title": "Implement missing component",
        "objective": "Implement the component from the source plan.",
        "scope": ["Implement component behavior."],
        "allowed_paths": ["src/component.py"],
        "implementation_steps": ["Implement component behavior."],
        "acceptance_criteria": ["Component behavior is verified."],
    }
    item = _item("missing")
    assessments = {
        "missing": {
            "status": "missing",
            "remaining_item": remaining_item,
        }
    }

    result = _apply_plan_coverage_assessments([item], assessments)

    assert result[0]["title"] == "Implement missing component"
    assert result[0]["scope"] == ["Implement component behavior."]


def test_structrr_proposal_normalizes_content_identity_from_subject_id() -> None:
    plan = ReviewedExecutionPlan(
        plan_id="attestation-current-state",
        items=(
            PlannedWorkItem(
                item_id="schema-contracts",
                title="Define protocol schemas",
                objective="Add the attestation schema contracts.",
                scope=("Add JSON schemas for policy records.",),
                acceptance_criteria=("The schemas validate all fixtures.",),
                dependencies=(),
                workflow_directory=None,
                workflow_id="attestation-current-state-schema-contracts",
                allowed_paths=("attestation/schemas",),
            ),
        ),
    )
    baseline: dict[str, Any] = {
        "entities": [],
        "entity_relationships": [],
        "files": [],
        "source_subjects": [],
        "source_bindings": [],
        "active_intent": {},
        "human-decisions": [],
        "invariants": [],
    }
    invalid_operation = {
        "operation_id": "add-schema-package",
        "item_id": "schema-contracts",
        "section": "entities",
        "subject_id": "entity:attestation-schemas",
        "action": "added",
        "content": {"id": "wrong-identity", "type": "Module"},
    }
    client = SequencedPlanningClient([{"operations": [invalid_operation]}])

    proposal = generate_multi_pr_structrr_proposal(
        plan=plan,
        baseline_snapshot=baseline,
        source_plan="# Attestation plan",
        client=client,
    )

    assert len(client.messages) == 1
    assert proposal.item_proposals["schema-contracts"].operations[0].subject_id == (
        "entity:attestation-schemas"
    )
    assert proposal.item_proposals["schema-contracts"].operations[0].content["id"] == (
        "entity:attestation-schemas"
    )


def test_structrr_repair_changes_a_subject_added_by_an_earlier_item() -> None:
    plan = ReviewedExecutionPlan(
        plan_id="duplicate-file-plan",
        items=(
            PlannedWorkItem(
                item_id="schema",
                title="Add policy schema",
                objective="Define the policy schema.",
                scope=("Add the policy schema.",),
                acceptance_criteria=("The schema is valid.",),
                dependencies=(),
                workflow_directory=None,
                workflow_id="duplicate-file-plan-schema",
                allowed_paths=("attestation/schemas",),
            ),
            PlannedWorkItem(
                item_id="schema-tests",
                title="Test policy schema",
                objective="Add schema validation tests.",
                scope=("Test the policy schema.",),
                acceptance_criteria=("Invalid policy data is rejected.",),
                dependencies=("schema",),
                workflow_directory=None,
                workflow_id="duplicate-file-plan-schema-tests",
                allowed_paths=("attestation/schemas", "tests"),
            ),
        ),
    )
    baseline: dict[str, Any] = {
        "entities": [],
        "entity_relationships": [],
        "files": [],
        "source_subjects": [],
        "source_bindings": [],
        "active_intent": {},
        "human-decisions": [],
        "invariants": [],
    }
    file_key = "file:attestation/schemas/policy-v1.schema.json"
    initial = {
        "operations": [
            {
                "operation_id": "add-policy-schema",
                "item_id": "schema",
                "section": "files",
                "subject_id": file_key,
                "action": "added",
                "content": {
                    "stable_key": file_key,
                    "path": "attestation/schemas/policy-v1.schema.json",
                },
            },
            {
                "operation_id": "add-policy-schema-again",
                "item_id": "schema-tests",
                "section": "files",
                "subject_id": file_key,
                "action": "added",
                "content": {
                    "stable_key": file_key,
                    "path": "attestation/schemas/policy-v1.schema.json",
                },
            },
        ]
    }
    repaired = {
        "operations": [
            initial["operations"][0],
            {
                **initial["operations"][1],
                "action": "changed",
                "content": {
                    "stable_key": file_key,
                    "path": "attestation/schemas/policy-v1.schema.json",
                    "validation_tests": ["tests/test_policy_schema.py"],
                },
            },
        ]
    }
    client = SequencedPlanningClient([initial, repaired])

    proposal = generate_multi_pr_structrr_proposal(
        plan=plan,
        baseline_snapshot=baseline,
        source_plan="# Policy schema plan",
        client=client,
    )

    assert len(client.messages) == 2
    assert "later item" in client.messages[1][0]["content"]
    assert proposal.item_proposals["schema-tests"].operations[0].action == "change"


def test_plan_can_append_a_final_review_dependent_on_all_implementation_items() -> None:
    client = FakePlanningClient({"items": [_item("foundation")]})

    rendered, plan = generate_execution_plan_yaml(
        source_plan="# Plan\nImplement a foundation.",
        plan_id="feature-v1",
        client=client,
        verification_command="make test",
        include_final_review=True,
    )

    assert [item.item_id for item in plan.items] == [
        "foundation",
        "final-integration-review",
    ]
    final = plan.items[-1]
    assert final.dependencies == ("foundation",)
    assert "docs/plans/feature-v1-final-structrr-review.json" in final.allowed_paths
    assert "final-integration-review" in rendered


def test_final_review_can_cover_paths_from_multiple_implementation_items() -> None:
    first = _item("foundation")
    first["allowed_paths"] = [f"src/foundation/{index}.py" for index in range(15)]
    second = _item("integration", dependencies=["foundation"])
    second["allowed_paths"] = [f"src/integration/{index}.py" for index in range(15)]

    _rendered, plan = generate_execution_plan_yaml(
        source_plan="# Plan\nImplement foundation and integration.",
        plan_id="multi-path-v1",
        client=FakePlanningClient({"items": [first, second]}),
        include_final_review=True,
    )

    final = plan.items[-1]
    assert final.item_id == "final-integration-review"
    assert len(final.allowed_paths) == 31


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda item: item.pop("implementation_steps"), "complete planning contract"),
        (
            lambda item: item.update(
                scope=[f"component {index}" for index in range(6)]
            ),
            "between 1 and 5 scope entries",
        ),
        (
            lambda item: item.update(
                implementation_steps=[f"step {index}" for index in range(9)]
            ),
            "between 1 and 4 non-empty string implementation steps",
        ),
        (
            lambda item: item.update(
                implementation_steps=[
                    {"Configure the workflow command": "bash verify.sh"}
                ]
            ),
            "non-empty string implementation steps",
        ),
    ],
)
def test_generate_execution_plan_yaml_rejects_oversized_or_incomplete_items(
    mutate: Any, message: str
) -> None:
    item = _item("foundation")
    mutate(item)

    with pytest.raises(PlanGenerationError, match=message):
        generate_execution_plan_yaml(
            source_plan="# Plan\nDo the work.",
            plan_id="local-attestation-v1",
            client=FakePlanningClient({"items": [item]}),
        )


def test_generated_plan_records_source_document() -> None:
    rendered, plan = generate_execution_plan_yaml(
        source_plan="# Plan\nDo the work.",
        plan_id="local-attestation-v1",
        client=FakePlanningClient({"items": [_item("foundation")]}),
        source_document="docs/plans/proposal.md",
    )

    assert "source_document: docs/plans/proposal.md" in rendered
    assert plan.source_document == "docs/plans/proposal.md"


def test_execute_pr_artifact_writer_requires_structrr_proposal(tmp_path: Path) -> None:
    rendered, plan = generate_execution_plan_yaml(
        source_plan="# Plan\nImplement the feature.",
        plan_id="structrr-required-v1",
        client=FakePlanningClient({"items": [_item("foundation")]}),
    )

    with pytest.raises(PlanGenerationError, match="require a generated Structrr"):
        write_execution_plan_artifacts(
            rendered_plan=rendered,
            plan=plan,
            output_path=tmp_path / "docs/plans/structrr-required-v1.yaml",
            repo_root=tmp_path,
            verification_command="scripts/verify-all.sh",
        )


def test_generated_plan_resolves_procedrr_process_workflow(tmp_path: Path) -> None:
    from powdrr_lift.process.templates import instantiate_procedrr_process_workflow

    colon_step = (
        "Configure the workflow command: bash attestation/scripts/verify-all.sh."
    )
    item = _item("foundation")
    item["implementation_steps"][0] = colon_step
    rendered, plan = generate_execution_plan_yaml(
        source_plan="# Plan\nImplement the feature.",
        plan_id="procedrr-process-v1",
        client=FakePlanningClient({"items": [item]}),
    )
    assert plan.items[0].workflow_file is not None
    write_execution_plan_artifacts(
        rendered_plan=rendered,
        plan=plan,
        output_path=tmp_path / "docs/plans/procedrr-process-v1.yaml",
        repo_root=tmp_path,
        verification_command="scripts/verify-all.sh",
        structrr_proposal=_empty_structrr_proposal(plan),
    )
    workflow_relative_file = plan.items[0].workflow_file
    assert workflow_relative_file is not None
    workflow_file = tmp_path / workflow_relative_file
    instance = load_plan_workflow_instance(workflow_file)

    workflow_dir, tasks = instantiate_procedrr_process_workflow(
        process_id=instance.process_id,
        process_directory=cli._bundled_procedrr_process_directory(),
        input_state=instance.inputs,
        work_item_name=instance.workflow_id,
        workflow_instance_name=instance.workflow_id,
        output_root=tmp_path / "workflows",
    )

    assert instance.process_id == "execute-proposed-pr-workflow"
    assert workflow_dir.name == instance.workflow_id
    assert len(tasks) == 1
    assert tasks[0].process_id == instance.process_id
    assert tasks[0].input_state["proposed_pr_id"] == instance.workflow_id
    assert tasks[0].input_state["proposed_pr_context"]["verification_command"] == (
        "scripts/verify-all.sh"
    )
    assert tasks[0].input_state["proposed_pr_context"]["implementation_steps"] == [
        colon_step,
        "Add focused tests proving foundation works.",
    ]


def test_execute_proposed_pr_process_runs_coding_agent_before_completion() -> None:
    import yaml

    process_path = (
        Path(__file__).parents[1]
        / "docs"
        / "procedrr"
        / "processes"
        / "execute-proposed-pr.yaml"
    )
    process = yaml.safe_load(process_path.read_text(encoding="utf-8"))
    coding_step = process["steps"][0]["operation"]["parameters"]

    assert coding_step["tool"] == "coding-agent"
    assert len(process["steps"]) == 2
    assert process["steps"][-1] == {"terminal": "succeeded"}


def test_issue_worker_runs_plan_task_from_durable_runtime_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from powdrr_lift.workrr.git import load_workflow_git_state

    rendered, plan = generate_execution_plan_yaml(
        source_plan="# Plan\nImplement the feature.",
        plan_id="procedrr-worker-v1",
        client=FakePlanningClient({"items": [_item("foundation")]}),
    )
    write_execution_plan_artifacts(
        rendered_plan=rendered,
        plan=plan,
        output_path=tmp_path / "docs/plans/procedrr-worker-v1.yaml",
        repo_root=tmp_path,
        verification_command="scripts/verify-all.sh",
        structrr_proposal=_empty_structrr_proposal(plan),
    )
    workflow_relative_file = plan.items[0].workflow_file
    assert workflow_relative_file is not None
    workflow_file = tmp_path / workflow_relative_file
    integration_worktree = tmp_path / "integration-worktree"
    integration_worktree.mkdir()
    captured: dict[str, Any] = {}

    monkeypatch.setattr(cli, "resolve_repo_root", lambda _path: tmp_path)
    monkeypatch.setattr(
        cli,
        "create_workflow_worktree",
        lambda *_args, **kwargs: (
            integration_worktree,
            kwargs["branch_name"],
        ),
    )

    def run_workflow_task(config: Any, **_kwargs: Any) -> int:
        captured["workflow_dir"] = config.workflow_dir
        return 0

    def run_worker_once(**kwargs: Any) -> SimpleNamespace:
        kwargs["run_task"](
            workflow_file,
            plan.items[0].workflow_id,
            "worker",
            plan.items[0],
            plan,
            (),
            lambda *_args: None,
        )
        return SimpleNamespace(
            outcome="awaiting_review",
            issue_number=23,
            item_id=plan.items[0].item_id,
            detail=None,
        )

    monkeypatch.setattr(cli, "run_workflow_task", run_workflow_task)
    monkeypatch.setattr(cli, "run_worker_once", run_worker_once)

    status = cli._run_github_issue_worker(
        Namespace(
            repo_root=tmp_path,
            repository="owner/repo",
            worker_id="worker",
            provider="auto",
            api_key=None,
            base_url=None,
            max_roundtrips=1,
            context_compaction_threshold=0.75,
            verbose=False,
        )
    )

    runtime_directory = Path(captured["workflow_dir"])
    assert status == 0
    assert runtime_directory.is_relative_to(integration_worktree)
    assert runtime_directory != workflow_file.parent
    workflow_instance = cli.load_plan_workflow_instance(workflow_file)
    state = load_workflow_git_state(runtime_directory, plan.items[0].workflow_id)
    assert state is not None
    attempt_id = state.integration_branch.rsplit("-attempt-", maxsplit=1)[1]
    instance_id = cli._plan_workflow_instance_id(
        plan.items[0].workflow_id,
        workflow_instance.inputs,
        "worker",
        attempt_id=attempt_id,
    )
    assert runtime_directory.name == cli.slugify_workflow_id(instance_id)
    task_files = sorted(runtime_directory.glob("*.yaml"))
    assert len(task_files) == 2
    assert task_files[0].stem.startswith(
        f"{cli.slugify_workflow_id(plan.items[0].workflow_id)}-task-"
    )
    assert (
        state.workflow_relative_directory
        == runtime_directory.relative_to(integration_worktree).as_posix()
    )
    assert state.integration_branch.startswith(
        f"powdrr/{cli.slugify_workflow_id(plan.items[0].workflow_id)}-attempt-"
    )


def test_plan_workflow_instance_id_tracks_input_changes() -> None:
    workflow_id = "proposal-v1-foundation"

    original = cli._plan_workflow_instance_id(
        workflow_id, {"objective": "first"}, "worker-a"
    )

    assert (
        cli._plan_workflow_instance_id(workflow_id, {"objective": "first"}, "worker-a")
        == original
    )
    assert (
        cli._plan_workflow_instance_id(
            workflow_id, {"objective": "revised"}, "worker-a"
        )
        != original
    )
    assert (
        cli._plan_workflow_instance_id(workflow_id, {"objective": "first"}, "worker-b")
        != original
    )


def test_plan_worker_process_catalog_does_not_depend_on_target_repo(
    tmp_path: Path,
) -> None:
    from procedrr import ProcessCatalog

    process_directory = cli._bundled_procedrr_process_directory()
    target_process_directory = tmp_path / "docs" / "procedrr" / "processes"

    assert not target_process_directory.exists()
    assert ProcessCatalog.load(process_directory).get("execute-proposed-pr-workflow")


def test_generated_plan_rejects_unknown_procedrr_process(tmp_path: Path) -> None:
    from powdrr_lift.process.templates import instantiate_procedrr_process_workflow

    with pytest.raises(ValueError, match="unknown Procedrr process"):
        instantiate_procedrr_process_workflow(
            process_id="missing-process",
            process_directory=(
                Path(__file__).resolve().parents[1] / "docs/procedrr/processes"
            ),
            input_state={},
            work_item_name="proposal-v1-foundation",
            output_root=tmp_path / "workflows",
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


def test_plan_cli_creates_tracking_issue_and_publishes_compiled_artifacts(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from powdrr_lift.workrr.planning.checkpoints import InMemoryCheckpointStore
    from powdrr_lift.workrr.planning.source import PinnedSourceDocument

    source = tmp_path / "proposal.md"
    source.write_text("# Proposal\nAdd a foundation.", encoding="utf-8")
    source_text = source.read_text(encoding="utf-8")
    base_commit = "a" * 40
    staged: list[Any] = []
    store = InMemoryCheckpointStore(session_id="test-planning-session")
    created_issues: list[QueueIssue] = []
    comments: list[str] = []

    class FakeIssueClient:
        repository = "example/repo"

        def ensure_planning_label(self) -> None:
            pass

        def create_issue(self, *, body: str, labels: Any, title: str) -> QueueIssue:
            issue = QueueIssue(
                42, title, body, "open", tuple(labels), "https://example/issues/42"
            )
            created_issues.append(issue)
            return issue

        def add_comment(self, _issue: QueueIssue, body: str) -> None:
            comments.append(body)

    issue_client = FakeIssueClient()
    compiled_files = {
        "docs/plans/proposal-v1-execution-plan.yaml": "plan: compiled\n",
        "docs/plans/proposal-v1-structrr-proposal.json": "{}\n",
        "docs/workflows/proposal-v1/proposal-v1-foundation.yaml": (
            "workflow: compiled\n"
        ),
    }
    machine_values: list[Any] = []
    compile_calls: list[dict[str, Any]] = []

    class FakeMachine:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            machine_values.append((args, kwargs))

        def compile_accepted_plan(self, accepted: Any, **kwargs: Any) -> Any:
            compile_calls.append({"accepted": accepted, **kwargs})
            return SimpleNamespace(
                plan=SimpleNamespace(
                    plan_id="proposal-v1",
                    items=[SimpleNamespace(item_id="proposal-v1-foundation")],
                ),
                files=compiled_files,
            )

    monkeypatch.setattr(cli, "resolve_repo_root", lambda _path: tmp_path)
    monkeypatch.setattr(
        cli, "_discover_github_repository", lambda _root: "example/repo"
    )
    monkeypatch.setattr(cli, "_git_head", lambda _root: base_commit)
    monkeypatch.setattr(cli, "_require_clean_planning_checkout", lambda *_args: None)
    monkeypatch.setattr(cli, "_planning_repository_paths", lambda *_args: {})
    monkeypatch.setattr(
        cli,
        "read_pinned_source",
        lambda *_args, **_kwargs: PinnedSourceDocument(
            repository="example/repo",
            commit_sha=base_commit,
            path="proposal.md",
            blob_sha="b" * 40,
            content_sha256=hashlib.sha256(source_text.encode()).hexdigest(),
            text=source_text,
        ),
    )
    monkeypatch.setattr(cli, "resolve_workflow_provider", lambda value: value)
    monkeypatch.setattr(
        cli,
        "configured_model_mapping",
        lambda _provider, _model: type(
            "Mapping", (), {"provider": "test", "model": "test-model"}
        )(),
    )
    monkeypatch.setattr(cli, "resolve_provider_credentials", lambda *_args: object())
    monkeypatch.setattr(
        cli, "build_workflow_client", lambda *_args, **_kwargs: object()
    )
    monkeypatch.setattr(cli, "GitHubIssueClient", lambda _repository: issue_client)
    monkeypatch.setattr(cli, "SourceLedgerQuestionMachine", FakeMachine)
    monkeypatch.setattr(
        cli, "load_entity_taxonomy", lambda *_args: SimpleNamespace(entity_types=())
    )
    monkeypatch.setattr(
        cli,
        "bootstrap_structrr",
        lambda *_args, **_kwargs: SimpleNamespace(
            document={"entities": [], "entity_relationships": [], "files": []},
            validation=SimpleNamespace(successful=True, issues=[]),
        ),
    )
    monkeypatch.setattr(
        cli,
        "LocalCheckpointStore",
        lambda *_args, **_kwargs: store,
    )
    process_calls: list[dict[str, Any]] = []

    def run_process(**kwargs: Any) -> Any:
        process_calls.append(kwargs)
        return SimpleNamespace(terminal_status=SimpleNamespace(value="succeeded"))

    monkeypatch.setattr(cli, "run_planning_procedrr", run_process)
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
    structrr_proposal_file = tmp_path / "docs/plans/proposal-v1-structrr-proposal.json"
    assert status == 0
    assert output.is_file()
    assert workflow_file.is_file()
    assert structrr_proposal_file.is_file()
    assert len(staged) == 3
    assert publications[0]["repo_root"] == tmp_path
    assert publications[0]["generated_files"] == tuple(staged)
    assert publications[0]["source_plan_path"] == "proposal.md"
    assert created_issues[0].labels == ("powdrr:planning",)
    assert created_issues[0].state == "open"
    assert "planning-session-v1" in created_issues[0].body
    assert not hasattr(issue_client, "create_work_item_issue")
    assert len(machine_values) == 1
    assert machine_values[0][1]["verification_command"] == "scripts/verify-all.sh"
    assert process_calls[0]["session"].issue.number == 42
    assert compile_calls[0]["plan_id"] == "proposal-v1"
    assert compile_calls[0]["plan_path"] == "docs/plans/proposal-v1-execution-plan.yaml"
    assert "scripts/verify-all.sh" in process_calls[0]["session"].verification_command
    assert comments == [
        "Planning completed and the review PR is open: "
        "https://github.com/example/repo/pull/42"
    ]
    assert set(staged) == {
        output,
        workflow_file,
        structrr_proposal_file,
    }
    response = capsys.readouterr().out
    assert '"workflow_count": 1' in response
    assert '"session_issue_url": "https://example/issues/42"' in response
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
    source_plan = tmp_path / "docs/plans/proposal.md"
    source_plan.write_text(
        "# Attestation Implementation Plan\n\nPlan details.\n", encoding="utf-8"
    )
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
    assert pr_command[pr_command.index("--title") + 1] == "Review plan: Attestation"
    pr_body = pr_command[pr_command.index("--body") + 1]
    assert "This pull request contains the generated implementation plan" in pr_body
    assert "- Implementation plan: `docs/plans/proposal-v1-execution-plan.yaml`" in (
        pr_body
    )
    assert "docs/plans/proposal.md" in pr_body


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
            structrr_proposal=_empty_structrr_proposal(plan),
        )

    assert sentinel.read_text(encoding="utf-8") == "preserve"
    assert not output_path.exists()
