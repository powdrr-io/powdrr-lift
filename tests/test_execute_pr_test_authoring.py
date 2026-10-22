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
    _git(repository, "remote", "add", "origin", "https://example.invalid/repo.git")
    _git(
        repository,
        "symbolic-ref",
        "refs/remotes/origin/HEAD",
        "refs/remotes/origin/main",
    )

    provider_calls: list[tuple[str, ...]] = []
    author_prompts: list[str] = []
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
            return {"passed": False}

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
            author_prompts.append(request.prompt)
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
        "complete_json_with_timeout_retry",
        lambda *_args, **_kwargs: pytest.fail(
            "the LLM must not be asked when Jev finds no shortcoming"
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
    assert len(author_prompts) == 2
    assert "same workflow/job/step" in author_prompts[0]
    assert "never guess a branch name" in author_prompts[0]
    assert '"name": "main"' in author_prompts[0]
    assert '"source": "refs/remotes/origin/HEAD"' in author_prompts[0]
    assert len(jev_calls) == 6
    assert jev_calls[0][1]["properties"]["passed"]["type"] == "boolean"
    assert (
        "Does this test fail to demonstrate observable behavior"
        in jev_calls[0][0][1]["content"]
    )
    jev_context = jev_calls[0][0][1]["content"]
    assert "repository_default_branch" in jev_context
    assert "refs/remotes/origin/HEAD" in jev_context


def test_repository_default_branch_evidence_uses_remote_head(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "-q")
    _git(repository, "remote", "add", "origin", "https://example.invalid/repo.git")
    _git(
        repository,
        "symbolic-ref",
        "refs/remotes/origin/HEAD",
        "refs/remotes/origin/trunk",
    )

    assert task_agent._repository_default_branch_evidence(repository) == {
        "name": "trunk",
        "source": "refs/remotes/origin/HEAD",
    }


def test_repository_default_branch_evidence_uses_remote_show_fallback(
    tmp_path: Path, monkeypatch: Any
) -> None:
    calls: list[list[str]] = []

    def fake_run(
        command: list[str], **_kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if command[1] == "symbolic-ref":
            return subprocess.CompletedProcess(command, 1, "", "")
        return subprocess.CompletedProcess(
            command, 0, "* remote origin\n  HEAD branch: release\n", ""
        )

    monkeypatch.setattr(task_agent.subprocess, "run", fake_run)

    assert task_agent._repository_default_branch_evidence(tmp_path) == {
        "name": "release",
        "source": "git remote show origin",
    }
    assert calls == [
        ["git", "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"],
        ["git", "remote", "show", "origin"],
    ]


def test_execute_pr_test_quality_asks_individual_questions_and_suggests_a_fix(
    monkeypatch: Any,
) -> None:
    jev_calls: list[list[dict[str, str]]] = []
    llm_calls: list[tuple[list[dict[str, str]], dict[str, Any]]] = []
    jev_results = iter([True, False, False, False, False, False])

    class FakeJevClient:
        def __init__(self, _fallback: Any, *, fail_closed: bool) -> None:
            assert fail_closed

        def complete_json(
            self,
            messages: list[dict[str, str]],
            *,
            response_schema: dict[str, Any],
        ) -> dict[str, Any]:
            jev_calls.append(messages)
            assert response_schema["required"] == ["passed"]
            return {"passed": next(jev_results)}

    def request_llm(
        _client: Any,
        messages: list[dict[str, str]],
        *,
        model: str,
        stderr: Any,
        max_timeout_retries: int,
        timeout_backoff_seconds: float,
        response_schema: dict[str, Any],
    ) -> dict[str, Any]:
        del model, stderr, max_timeout_retries, timeout_backoff_seconds
        llm_calls.append((messages, response_schema))
        if response_schema["required"] == ["passed", "explanation"]:
            return {"passed": True, "explanation": "The assertion is vacuous."}
        return {
            "test_name": "test_rejects_invalid_payload",
            "test_code": "def test_rejects_invalid_payload():\n    ...",
            "rationale": "Exercise the invalid request through the public API.",
        }

    monkeypatch.setattr(task_agent, "JevSemanticClassifierClient", FakeJevClient)
    monkeypatch.setattr(task_agent, "complete_json_with_timeout_retry", request_llm)
    evidence = {
        "contract": {"acceptance_criteria": ["AC-01: Reject invalid payloads."]},
        "test_obligations": [
            {"obligation_id": "AC-01", "source_text": "Reject invalid payloads."}
        ],
        "coverage_references": {"AC-01": ["test_rejects_payload"]},
        "test_sources": {
            "tests/test_api.py": (
                "# Covers: AC-01 test_rejects_payload\n"
                "def test_rejects_payload():\n    assert True\n"
            )
        },
        "baseline_result": {"stdout": "1 failed in 0.01s", "passed": False},
    }

    review = task_agent._review_execute_pr_test_quality(
        object(),  # type: ignore[arg-type]
        evidence,
        config=task_agent.WorkflowTaskAgentConfig(workflow_dir=Path(".")),
    )

    assert len(jev_calls) == 6
    assert len(llm_calls) == 2
    assert jev_calls[0][1]["content"] == llm_calls[0][0][1]["content"]
    assert "tests/test_api.py::test_rejects_payload" in llm_calls[0][0][1]["content"]
    assert "AC-01: Reject invalid payloads." in llm_calls[0][0][1]["content"]
    assert "baseline_result" in llm_calls[1][0][1]["content"]
    assert review["outcome"] == "advisory"
    assert review["suggestions"][0]["test_name"] == "test_rejects_invalid_payload"
    assert review["assessments"][0]["check_id"] == "observable_coverage"


def test_execute_pr_test_quality_accepts_jev_concern_as_advisory_on_disagreement(
    monkeypatch: Any,
) -> None:
    llm_calls = 0

    class FakeJevClient:
        def __init__(self, _fallback: Any, *, fail_closed: bool) -> None:
            assert fail_closed

        def complete_json(
            self,
            _messages: list[dict[str, str]],
            *,
            response_schema: dict[str, Any],
        ) -> dict[str, Any]:
            return {"passed": True}

    def request_llm(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        nonlocal llm_calls
        llm_calls += 1
        return {"passed": False, "explanation": "The assertion checks public behavior."}

    monkeypatch.setattr(task_agent, "JevSemanticClassifierClient", FakeJevClient)
    monkeypatch.setattr(task_agent, "complete_json_with_timeout_retry", request_llm)
    review = task_agent._review_execute_pr_test_quality(
        object(),  # type: ignore[arg-type]
        {
            "test_obligations": [
                {"obligation_id": "AC-01", "source_text": "Run the verifier."}
            ],
            "test_sources": {
                "tests/test_ci.py": (
                    "# Covers: AC-01 test_ci\n"
                    "def test_ci():\n    assert public_verify()\n"
                )
            },
        },
        config=task_agent.WorkflowTaskAgentConfig(workflow_dir=Path(".")),
    )
    assert review["outcome"] == "advisory"
    assert llm_calls == 6
    assert review["suggestions"] == []
    assert any("disagreed" in note for note in review["notes"])


def test_execute_pr_test_quality_review_outage_is_advisory(
    monkeypatch: Any,
) -> None:
    class BrokenJevClient:
        def __init__(self, _fallback: Any, *, fail_closed: bool) -> None:
            assert fail_closed

        def complete_json(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("Jev is unavailable")

    monkeypatch.setattr(task_agent, "JevSemanticClassifierClient", BrokenJevClient)
    review = task_agent._review_execute_pr_test_quality(
        object(),  # type: ignore[arg-type]
        {
            "test_obligations": [
                {"obligation_id": "AC-01", "source_text": "Run the verifier."}
            ],
            "test_sources": {
                "tests/test_ci.py": (
                    "# Covers: AC-01 test_ci\n"
                    "def test_ci():\n    assert public_verify()\n"
                )
            },
        },
        config=task_agent.WorkflowTaskAgentConfig(workflow_dir=Path(".")),
    )
    assert review["outcome"] == "advisory"
    assert len(review["assessments"]) == 6
    assert all(
        item["jev_reports_shortcoming"] is None for item in review["assessments"]
    )


def test_quality_shortcoming_is_repaired_once_then_accepted_with_pr_note(
    tmp_path: Path, monkeypatch: Any
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "-q")
    _git(repository, "config", "user.name", "Powdrr Test")
    _git(repository, "config", "user.email", "powdrr-test@example.invalid")
    _write(repository, "sample.py", "value = 1\n")
    _write(
        repository, "tests/test_existing.py", "def test_existing():\n    assert True\n"
    )
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "baseline")
    base_commit = _git(repository, "rev-parse", "HEAD")
    _git(repository, "remote", "add", "origin", "https://example.invalid/repo.git")
    _git(
        repository,
        "symbolic-ref",
        "refs/remotes/origin/HEAD",
        "refs/remotes/origin/main",
    )
    test_path = (
        "tests/test_powdrr_execute_pr_"
        + hashlib.sha256(b"local-pr-quality-repair").hexdigest()[:12]
        + ".py"
    )
    test_source = (
        "from pathlib import Path\n"
        "import sys\n\n"
        "sys.path.insert(0, str(Path(__file__).resolve().parents[1]))\n"
        "from sample import value\n\n"
        "# Covers: AC-01 test_expected_value\n"
        "def test_expected_value():\n"
        "    assert value == 2\n"
    )
    author_prompts: list[str] = []

    class FakeJevClient:
        def __init__(self, _fallback: Any, *, fail_closed: bool) -> None:
            assert fail_closed
            self.calls = 0

        def complete_json(
            self,
            _messages: list[dict[str, str]],
            *,
            response_schema: dict[str, Any],
        ) -> dict[str, Any]:
            self.calls += 1
            assert response_schema["required"] == ["passed"]
            return {"passed": self.calls == 1}

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
            author_prompts.append(request.prompt)
            if len(author_prompts) == 2:
                assert "test_observes_expected_value" in request.prompt, request.prompt[
                    -1800:
                ]
                assert request.allow_existing_changes is False
            _write(worktree_root, request.allowed_paths[0], test_source)
            return subprocess.CompletedProcess(["fake-miniswe"], 0, "done\n", "")

    llm_calls = 0

    def request_llm(
        _client: Any,
        _messages: list[dict[str, str]],
        *,
        model: str,
        stderr: Any,
        max_timeout_retries: int,
        timeout_backoff_seconds: float,
        response_schema: dict[str, Any],
    ) -> dict[str, Any]:
        del model, stderr, max_timeout_retries, timeout_backoff_seconds
        nonlocal llm_calls
        llm_calls += 1
        if response_schema["required"] == ["passed", "explanation"]:
            return {"passed": True, "explanation": "The test needs stronger evidence."}
        return {
            "test_name": "test_observes_expected_value",
            "test_code": "def test_observes_expected_value():\n    ...",
            "rationale": "Assert the externally visible result.",
        }

    monkeypatch.setattr(task_agent, "MiniSWEAgentProvider", FakeMiniSWEAgentProvider)
    monkeypatch.setattr(task_agent, "JevSemanticClassifierClient", FakeJevClient)
    monkeypatch.setattr(task_agent, "complete_json_with_timeout_retry", request_llm)
    monkeypatch.setattr(
        task_agent,
        "run_generated_pytest",
        lambda *_args, **_kwargs: {
            "passed": False,
            "returncode": 1,
            "stdout": (
                "FAILED tests/test_generated.py::test_expected_value - AssertionError\n"
                "1 failed in 0.01s\n"
            ),
            "stderr": "",
        },
    )
    request = ImplementationRequest(
        request_id="quality-repair",
        objective="Add behavior coverage.",
        prompt="",
        base_commit=base_commit,
        plan_fingerprint="plan-fingerprint",
        allowed_paths=("src/sample.py",),
        acceptance_criteria=("The public value is 2.",),
        validation_profiles=(),
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
            "title": "Check the public value",
            "objective": "Add behavior coverage.",
            "acceptance_criteria": ["The public value is 2."],
        },
        workflow_id="local-pr-quality-repair",
        config=config,
        store_root=tmp_path / "run",
        validation_profile=ValidationProfile(
            name="verify", command=(sys.executable, "-m", "pytest")
        ),
        quality_client=object(),  # type: ignore[arg-type]
    )

    assert tuple(sources) == (test_path,)
    assert len(author_prompts) == 2
    assert llm_calls == 3
    assert artifact["quality_review"]["outcome"] == "advisory"
    assert len(artifact["quality_review"]["reviews"]) == 2
    assert "after a targeted repair attempt" in artifact["quality_review"]["notes"][0]
    pr_notes = task_agent._render_execute_pr_test_quality_pr_notes(
        artifact["quality_review"]["notes"]
    )
    assert "Test-quality review is advisory" in " ".join(pr_notes)
    assert "consider adding more coverage" in " ".join(pr_notes)
