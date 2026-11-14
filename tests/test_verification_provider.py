from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from powdrr_agent.execution.verification_provider import (
    DiscoveredValidationProfile as ProviderValidationProfile,
)
from powdrr_agent.execution.verification_provider import (
    ProviderExecutionResult,
    ProviderInventory,
    PytestVerificationProvider,
    StestrVerificationProvider,
    VerificationProviderRegistry,
    VerificationProviderRequest,
    default_verification_provider_registry,
    match_selector,
)
from powdrr_service.structrr.validation import DiscoveredValidationProfile


def test_pytest_inventory_contains_exact_node_ids(tmp_path: Path) -> None:
    component = tmp_path / "packages" / "api"
    tests = component / "tests"
    tests.mkdir(parents=True)
    (tests / "test_sample.py").write_text(
        "def test_first():\n    assert True\n\n"
        "class TestGroup:\n    def test_second(self):\n        assert True\n",
        encoding="utf-8",
    )
    profile = DiscoveredValidationProfile(
        "pytest",
        ("python", "-m", "pytest", "-q", "tests"),
        "test",
        cwd="packages/api",
        environment_id="environment:python:packages/api",
        applicability={"local": True, "evaluation": "applicable"},
    )
    previous_directory = Path.cwd()

    inventory = PytestVerificationProvider().inventory(tmp_path, profile)

    assert inventory.collection_error is None
    assert inventory.selectors == (
        "tests/test_sample.py::TestGroup::test_second",
        "tests/test_sample.py::test_first",
    )
    assert inventory.fingerprint.startswith("sha256:")
    assert inventory.cwd == "packages/api"
    assert inventory.environment_id == "environment:python:packages/api"
    assert inventory.applicability == {"local": True, "evaluation": "applicable"}
    assert Path.cwd() == previous_directory


def test_pytest_inventory_marks_shell_profiles_unresolved(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    profile = DiscoveredValidationProfile(
        "pytest",
        ("pytest", "tests"),
        "workflow",
        shell="bash",
        execution_kind="shell",
    )

    inventory = PytestVerificationProvider().inventory(tmp_path, profile)

    assert inventory.selectors == ()
    assert inventory.collection_error is not None
    assert "argv profile" in inventory.collection_error
    assert inventory.execution_kind == "shell"


def test_registry_is_framework_neutral_and_dispatches_fake_provider(
    tmp_path: Path,
) -> None:
    class FakeProvider:
        name = "fake"

        def detect(
            self, root: Path, profiles: Sequence[ProviderValidationProfile]
        ) -> bool:
            return root == tmp_path

        def inventory(
            self, root: Path, profile: ProviderValidationProfile
        ) -> ProviderInventory:
            raise AssertionError("inventory is not part of this test")

        def execute(
            self, request: VerificationProviderRequest
        ) -> ProviderExecutionResult:
            return ProviderExecutionResult(
                request.provider, request.selector, request.profile, "passed"
            )

        def normalize(self, result: ProviderExecutionResult) -> dict[str, Any]:
            return {"status": result.status}

    registry = VerificationProviderRegistry((FakeProvider(),))
    profiles = (DiscoveredValidationProfile("fake", ("fake",), "test"),)

    assert [provider.name for provider in registry.detected(tmp_path, profiles)] == [
        "fake"
    ]
    assert default_verification_provider_registry().provider("pytest") is not None


def test_pytest_provider_executes_exact_selector(
    tmp_path: Path,
) -> None:
    component = tmp_path / "component"
    tests = component / "tests"
    tests.mkdir(parents=True)
    (tests / "test_sample.py").write_text(
        "def test_exact():\n    assert True\n", encoding="utf-8"
    )
    result = PytestVerificationProvider().execute(
        VerificationProviderRequest(
            "pytest",
            "tests/test_sample.py::test_exact",
            "pytest",
            tmp_path,
            command=("python", "-m", "pytest", "-q"),
            cwd="component",
            environment_id="environment:python:component",
        )
    )

    assert result.status == "passed"
    assert result.returncode == 0


def test_pytest_provider_reports_failed_subprocess_result(tmp_path: Path) -> None:
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_failure.py").write_text(
        "def test_failure():\n    assert False\n", encoding="utf-8"
    )

    result = PytestVerificationProvider().execute(
        VerificationProviderRequest(
            "pytest",
            "tests/test_failure.py::test_failure",
            "pytest",
            tmp_path,
            command=("python", "-m", "pytest", "-q"),
        )
    )

    assert result.status == "failed"
    assert result.returncode != 0
    assert "pytest exited with" in (result.error or "")


def test_pytest_provider_records_timeout_and_removes_temporary_report(
    tmp_path: Path, monkeypatch: Any
) -> None:
    (tmp_path / "tests").mkdir()
    junit_paths: list[Path] = []

    def timeout(
        command: Sequence[str], **_kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        junit_paths.append(
            Path(
                next(
                    arg.split("=", 1)[1]
                    for arg in command
                    if arg.startswith("--junitxml=")
                )
            )
        )
        raise subprocess.TimeoutExpired(command, timeout=1)

    monkeypatch.setattr(
        "powdrr_agent.execution.verification_provider.subprocess.run", timeout
    )
    result = PytestVerificationProvider().execute(
        VerificationProviderRequest(
            "pytest", "tests/test_slow.py::test_slow", "pytest", tmp_path
        )
    )

    assert result.status == "timed_out"
    assert result.returncode == 124
    assert junit_paths and not junit_paths[0].exists()


def test_pytest_provider_does_not_execute_declared_shell_profile(
    tmp_path: Path,
) -> None:
    request = VerificationProviderRequest(
        "pytest",
        "tests/test_sample.py::test_first",
        "pytest",
        tmp_path,
        execution_kind="shell",
        shell="bash",
        script="pytest tests",
    )

    result = PytestVerificationProvider().execute(request)

    assert result.status == "errored"
    assert "argv profile" in (result.error or "")


def test_stestr_provider_collects_and_executes_exact_selector(
    tmp_path: Path, monkeypatch: Any
) -> None:
    profile = DiscoveredValidationProfile(
        "stestr", ("stestr", "run", "--concurrency", "1"), "test"
    )
    calls: list[tuple[str, ...]] = []

    def fake_run(
        command: Sequence[str], **_kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        calls.append(tuple(command))
        output = {
            "list": "tests.test_one.TestOne.test_first\n",
            "--version": "stestr 4.1.0\n",
        }.get(command[1], "ok\n")
        return subprocess.CompletedProcess(command, 0, output, "")

    monkeypatch.setattr(
        "powdrr_agent.execution.verification_provider.subprocess.run", fake_run
    )
    provider = StestrVerificationProvider()

    inventory = provider.inventory(tmp_path, profile)
    result = provider.execute(
        VerificationProviderRequest(
            "stestr",
            "tests.test_one.TestOne.test_first",
            "stestr",
            tmp_path,
            command=profile.command,
        )
    )

    assert inventory.selectors == ("tests.test_one.TestOne.test_first",)
    assert inventory.provider_version == "stestr 4.1.0"
    assert calls[0] == ("stestr", "list")
    assert calls[1] == ("stestr", "--version")
    assert calls[2] == (
        "stestr",
        "run",
        "--concurrency",
        "1",
        "tests.test_one.TestOne.test_first",
    )
    assert result.status == "passed"


def test_match_selector_requires_exact_provider_profile_and_node_id() -> None:
    collected = ProviderInventory(
        "pytest", "pytest", ("tests/test_sample.py::test_first",)
    )

    assert (
        match_selector(
            collected,
            provider="pytest",
            profile="pytest",
            selector="tests/test_sample.py::test_first",
        ).status
        == "collected"
    )
    assert (
        match_selector(
            collected,
            provider="pytest",
            profile="pytest",
            selector="tests/test_sample.py::test_missing",
        ).status
        == "not_collected"
    )
