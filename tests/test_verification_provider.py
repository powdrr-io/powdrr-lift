from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from powdrr_lift.structrr.validation import DiscoveredValidationProfile
from powdrr_lift.workrr.verification_provider import (
    ProviderExecutionResult,
    ProviderInventory,
    PytestVerificationProvider,
    VerificationProviderRegistry,
    VerificationProviderRequest,
    default_verification_provider_registry,
    match_selector,
)


def test_pytest_inventory_contains_exact_node_ids(tmp_path: Path) -> None:
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_sample.py").write_text(
        "def test_first():\n    assert True\n\n"
        "class TestGroup:\n    def test_second(self):\n        assert True\n",
        encoding="utf-8",
    )
    profile = DiscoveredValidationProfile(
        "pytest", ("python", "-m", "pytest", "-q"), "test"
    )

    inventory = PytestVerificationProvider().inventory(tmp_path, profile)

    assert inventory.collection_error is None
    assert inventory.selectors == (
        "tests/test_sample.py::TestGroup::test_second",
        "tests/test_sample.py::test_first",
    )
    assert inventory.fingerprint.startswith("sha256:")


def test_registry_is_framework_neutral_and_dispatches_fake_provider(
    tmp_path: Path,
) -> None:
    class FakeProvider:
        name = "fake"

        def detect(
            self, root: Path, profiles: Sequence[DiscoveredValidationProfile]
        ) -> bool:
            return root == tmp_path

        def inventory(
            self, root: Path, profile: DiscoveredValidationProfile
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


def test_pytest_provider_returns_typed_not_implemented_execution_result(
    tmp_path: Path,
) -> None:
    result = PytestVerificationProvider().execute(
        VerificationProviderRequest(
            "pytest", "tests/test_missing.py::test_x", "pytest", tmp_path
        )
    )

    assert result.status == "not_implemented"
    assert result.error is not None


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
