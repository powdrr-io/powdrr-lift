from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from powdrr_lift.workrr import task_agent
from powdrr_lift.workrr.coding_agent import ImplementationRequest
from powdrr_lift.workrr.coding_agent_validation import ValidationProfile


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _write(root: Path, relative_path: str, content: str) -> None:
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_execute_pr_test_repair_uses_nested_root_and_clean_baseline(
    tmp_path: Path, monkeypatch: Any
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "-q")
    _git(repository, "config", "user.name", "Powdrr Test")
    _git(repository, "config", "user.email", "powdrr-test@example.invalid")
    _write(repository, "sample.py", "value = 1\n")
    _write(repository, "tests/test_root.py", "def test_root():\n    assert True\n")
    _write(repository, "attestation/tests/conftest.py", "")
    _write(
        repository,
        "attestation/tests/test_existing.py",
        "def test_existing():\n    assert True\n",
    )
    _write(
        repository,
        "attestation/verifier/tests/test_policy.py",
        "def test_policy():\n    assert True\n",
    )
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "baseline")
    base_commit = _git(repository, "rev-parse", "HEAD")

    provider_calls: list[tuple[str, ...]] = []
    expected_test_path = (
        "attestation/tests/test_powdrr_execute_pr_"
        + hashlib.sha256(b"local-pr-01").hexdigest()[:12]
        + ".py"
    )
    test_source = (
        "from sample import value\n\n"
        "# Covers: AC-01 test_expected_value\n"
        "def test_expected_value():\n"
        "    assert value == 2, 'AssertionError: expected value 2'\n"
    )

    jev_calls: list[tuple[list[dict[str, str]], dict[str, Any]]] = []

    class FakeJevClient:
        def __init__(self, _fallback: Any, *, fail_closed: bool) -> None:
            assert fail_closed

        def complete_json(
            self,
            messages: list[dict[str, str]],
            *,
            response_schema: dict[str, Any],
        ) -> dict[str, Any]:
            jev_calls.append((messages, response_schema))
            return {"passed": True}

    class FakeMiniSWEAgentProvider:
        provider_name = "fake-miniswe"

        def __init__(self, **_kwargs: Any) -> None:
            pass

        def run(
            self,
            request: ImplementationRequest,
            *,
            worktree_root: Path,
            attempt_id: str,
        ) -> subprocess.CompletedProcess[str]:
            del attempt_id
            if not provider_calls:
                # Simulate the failed attempt from Gaterr: the agent chooses
                # the wrong repository-level test root.
                _write(
                    worktree_root,
                    "tests/test_wrong_location.py",
                    "def test_wrong_location():\n    assert True\n",
                )
                _git(worktree_root, "add", "-f", "tests/test_wrong_location.py")
            else:
                provider_calls.append(request.allowed_paths)
                status = _git(worktree_root, "status", "--porcelain")
                assert status == "", "repair must start from a clean baseline"
                assert request.allow_existing_changes is False
                _write(worktree_root, request.allowed_paths[0], test_source)
            if not provider_calls:
                provider_calls.append(request.allowed_paths)
            return subprocess.CompletedProcess(["fake-miniswe"], 0, "done\n", "")

    monkeypatch.setattr(task_agent, "MiniSWEAgentProvider", FakeMiniSWEAgentProvider)
    monkeypatch.setattr(task_agent, "JevSemanticClassifierClient", FakeJevClient)
    monkeypatch.setattr(
        task_agent,
        "complete_json",
        lambda *_args, **_kwargs: pytest.fail(
            "the LLM must not be asked for findings when Jev passes"
        ),
    )

    request = ImplementationRequest(
        request_id="nested-test-root",
        objective="Add CI for the attestation project.",
        prompt="",
        base_commit=base_commit,
        plan_fingerprint="plan-fingerprint",
        allowed_paths=(
            ".github/workflows/ci.yml",
            "attestation/scripts/verify-all.sh",
        ),
        acceptance_criteria=("The CI workflow runs the verifier.",),
        validation_profiles=("verify",),
    )
    config = task_agent.WorkflowTaskAgentConfig(
        workflow_dir=tmp_path / "workflow",
        repo_root=repository,
        coding_agent_timeout_seconds=60,
        coding_agent_validation_timeout_seconds=60,
    )

    sources, artifact = task_agent._author_execute_pr_tests(
        repository,
        base_commit=base_commit,
        request=request,
        proposed={
            "title": "Add attestation CI",
            "objective": "Add CI for the attestation project.",
            "scope": ["Run the attestation verifier in CI."],
            "implementation_steps": ["Add the CI workflow."],
            "acceptance_criteria": ["The CI workflow runs the verifier."],
        },
        workflow_id="local-pr-01",
        config=config,
        store_root=tmp_path / "run",
        validation_profile=ValidationProfile(
            name="verify", command=(sys.executable, "-m", "pytest")
        ),
        quality_client=object(),  # type: ignore[arg-type]
    )

    assert provider_calls == [(expected_test_path,), (expected_test_path,)]
    assert tuple(sources) == (expected_test_path,)
    assert artifact["authoring_attempts"][0]["status"] == "policy_denied"
    assert artifact["authoring_attempts"][0]["out_of_scope_paths"] == [
        "tests/test_wrong_location.py"
    ]
    assert artifact["authoring_attempts"][1]["status"] == "completed"
    assert artifact["baseline_result"]["passed"] is False
    assert len(jev_calls) == 1
    assert jev_calls[0][1]["properties"]["passed"]["type"] == "boolean"
    assert (
        "Do these tests provide adequate independent" in jev_calls[0][0][1]["content"]
    )


def test_execute_pr_test_quality_asks_llm_for_findings_after_jev_failure(
    monkeypatch: Any,
) -> None:
    jev_calls: list[tuple[list[dict[str, str]], dict[str, Any]]] = []
    findings_calls: list[tuple[list[dict[str, str]], dict[str, Any]]] = []

    class FakeJevClient:
        def __init__(self, _fallback: Any, *, fail_closed: bool) -> None:
            assert fail_closed

        def complete_json(
            self,
            messages: list[dict[str, str]],
            *,
            response_schema: dict[str, Any],
        ) -> dict[str, Any]:
            jev_calls.append((messages, response_schema))
            return {"passed": False}

    class FakeQualityClient:
        def complete_json(self, _messages: list[dict[str, str]]) -> dict[str, Any]:
            pytest.fail("the patched complete_json helper should be used")

    def request_findings(
        _client: Any,
        messages: list[dict[str, str]],
        *,
        response_schema: dict[str, Any],
    ) -> dict[str, Any]:
        findings_calls.append((messages, response_schema))
        return {
            "findings": [
                {
                    "test_id": "test_ci_workflow",
                    "issue": "Does not exercise the workflow trigger.",
                    "evidence": "The test only checks that the YAML file exists.",
                }
            ]
        }

    monkeypatch.setattr(task_agent, "JevSemanticClassifierClient", FakeJevClient)
    monkeypatch.setattr(task_agent, "complete_json", request_findings)

    with pytest.raises(task_agent.ExecutePrTestError, match="test_ci_workflow"):
        task_agent._review_execute_pr_test_quality(
            FakeQualityClient(),
            {"test_sources": {"tests/test_ci.py": "def test_ci(): ..."}},
        )

    assert len(jev_calls) == 1
    assert len(findings_calls) == 1
    assert findings_calls[0][1]["required"] == ["findings"]
    assert "Jev has already judged inadequate" in findings_calls[0][0][0]["content"]
