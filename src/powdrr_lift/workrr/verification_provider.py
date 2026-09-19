"""Provider-neutral verification discovery and execution boundaries."""

from __future__ import annotations

import hashlib
import importlib.metadata
import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from powdrr_lift.structrr.validation import (
    VALIDATION_PROVIDER_INVENTORY_SCHEMA_VERSION,
    DiscoveredValidationProfile,
)

VERIFICATION_PROVIDER_INVENTORY_SCHEMA_VERSION = (
    VALIDATION_PROVIDER_INVENTORY_SCHEMA_VERSION
)


class VerificationProvider(Protocol):
    """The Workrr boundary for repository-native verification providers."""

    name: str

    def detect(
        self,
        root: Path,
        profiles: Sequence[DiscoveredValidationProfile],
    ) -> bool: ...

    def inventory(
        self, root: Path, profile: DiscoveredValidationProfile
    ) -> ProviderInventory: ...

    def execute(
        self, request: VerificationProviderRequest
    ) -> ProviderExecutionResult: ...

    def normalize(self, result: ProviderExecutionResult) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class VerificationProviderRequest:
    provider: str
    selector: str
    profile: str
    root: Path


@dataclass(frozen=True, slots=True)
class ProviderInventory:
    provider: str
    profile: str
    selectors: tuple[str, ...]
    provider_version: str | None = None
    collection_error: str | None = None

    @property
    def fingerprint(self) -> str:
        payload = "\n".join(
            (
                VERIFICATION_PROVIDER_INVENTORY_SCHEMA_VERSION,
                self.provider,
                self.profile,
                self.provider_version or "unknown",
                *self.selectors,
                self.collection_error or "",
            )
        )
        return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def to_data(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "profile": self.profile,
            "selectors": list(self.selectors),
            "provider_version": self.provider_version,
            "collection_error": self.collection_error,
            "fingerprint": self.fingerprint,
        }


@dataclass(frozen=True, slots=True)
class ProviderExecutionResult:
    provider: str
    selector: str
    profile: str
    status: str
    returncode: int | None = None
    error: str | None = None


class VerificationProviderRegistry:
    """Detect and dispatch registered providers without framework branching."""

    def __init__(self, providers: Sequence[VerificationProvider] = ()) -> None:
        self._providers = {provider.name: provider for provider in providers}

    def register(self, provider: VerificationProvider) -> None:
        if not provider.name.strip():
            raise ValueError("verification provider name must not be empty")
        self._providers[provider.name] = provider

    def detected(
        self,
        root: str | Path,
        profiles: Sequence[DiscoveredValidationProfile],
    ) -> tuple[VerificationProvider, ...]:
        root_path = Path(root).resolve()
        return tuple(
            provider
            for provider in self._providers.values()
            if provider.detect(root_path, profiles)
        )

    def inventory(
        self,
        root: str | Path,
        profiles: Sequence[DiscoveredValidationProfile],
    ) -> tuple[ProviderInventory, ...]:
        inventories: list[ProviderInventory] = []
        for provider in self.detected(root, profiles):
            for profile in profiles:
                if provider.detect(Path(root).resolve(), (profile,)):
                    inventories.append(
                        provider.inventory(Path(root).resolve(), profile)
                    )
        return tuple(inventories)

    def provider(self, name: str) -> VerificationProvider | None:
        return self._providers.get(name)


class PytestVerificationProvider:
    """Collect exact pytest node IDs through pytest's collection hook."""

    name = "pytest"

    def detect(
        self,
        root: Path,
        profiles: Sequence[DiscoveredValidationProfile],
    ) -> bool:
        return (
            any(
                profile.name == self.name
                or _command_mentions(profile.command, "pytest")
                for profile in profiles
            )
            and (root / "tests").is_dir()
        )

    def inventory(
        self, root: Path, profile: DiscoveredValidationProfile
    ) -> ProviderInventory:
        try:
            import pytest
        except ImportError:
            return ProviderInventory(
                self.name,
                profile.name,
                (),
                collection_error="pytest is not installed",
            )

        selectors: list[str] = []

        class Collector:
            def pytest_collection_finish(self, session: Any) -> None:
                selectors.extend(item.nodeid for item in session.items)

        arguments = _pytest_arguments(profile.command, root)
        previous_directory = Path.cwd()
        try:
            os.chdir(root)
            exit_code = pytest.main(
                ["--collect-only", *arguments], plugins=[Collector()]
            )
        except (OSError, RuntimeError) as error:
            return ProviderInventory(
                self.name,
                profile.name,
                tuple(sorted(set(selectors))),
                _package_version("pytest"),
                str(error),
            )
        finally:
            os.chdir(previous_directory)
        return ProviderInventory(
            self.name,
            profile.name,
            tuple(sorted(set(selectors))),
            _package_version("pytest"),
            None if exit_code == 0 else f"pytest collection exited with {exit_code}",
        )

    def execute(self, request: VerificationProviderRequest) -> ProviderExecutionResult:
        return ProviderExecutionResult(
            self.name,
            request.selector,
            request.profile,
            "not_implemented",
            error="pytest execution is provided by the Phase 5 evidence runner",
        )

    def normalize(self, result: ProviderExecutionResult) -> dict[str, Any]:
        return {
            "provider": result.provider,
            "selector": result.selector,
            "profile": result.profile,
            "status": result.status,
            "returncode": result.returncode,
            "error": result.error,
        }


def default_verification_provider_registry() -> VerificationProviderRegistry:
    return VerificationProviderRegistry((PytestVerificationProvider(),))


def discover_verification_inventory(
    root: str | Path,
    profiles: Sequence[DiscoveredValidationProfile],
) -> tuple[dict[str, Any], ...]:
    """Return bootstrap-safe provider inventory records."""
    registry = default_verification_provider_registry()
    return tuple(
        {
            **inventory.to_data(),
            "schema_version": VERIFICATION_PROVIDER_INVENTORY_SCHEMA_VERSION,
        }
        for inventory in registry.inventory(root, profiles)
    )


def match_selector(
    inventory: ProviderInventory,
    *,
    provider: str,
    profile: str,
    selector: str,
) -> ProviderExecutionResult:
    """Match a contract to exact collected identity without fuzzy names."""
    if inventory.provider != provider or inventory.profile != profile:
        return ProviderExecutionResult(
            provider,
            selector,
            profile,
            "not_collected",
            error="provider/profile was not discovered",
        )
    if inventory.collection_error:
        return ProviderExecutionResult(
            provider,
            selector,
            profile,
            "not_collected",
            error=inventory.collection_error,
        )
    if selector not in inventory.selectors:
        return ProviderExecutionResult(
            provider,
            selector,
            profile,
            "not_collected",
            error="selector was not collected",
        )
    return ProviderExecutionResult(provider, selector, profile, "collected")


def _command_mentions(command: Sequence[str], value: str) -> bool:
    return any(Path(part).name == value or part == value for part in command)


def _pytest_arguments(command: Sequence[str], root: Path) -> list[str]:
    try:
        index = next(
            index for index, part in enumerate(command) if Path(part).name == "pytest"
        )
    except StopIteration:
        return [str(root / "tests")]
    arguments = list(command[index + 1 :])
    return [
        str(root / argument) if argument == "." else argument for argument in arguments
    ]


def _package_version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


__all__ = [
    "ProviderExecutionResult",
    "ProviderInventory",
    "PytestVerificationProvider",
    "VERIFICATION_PROVIDER_INVENTORY_SCHEMA_VERSION",
    "VerificationProvider",
    "VerificationProviderRegistry",
    "VerificationProviderRequest",
    "default_verification_provider_registry",
    "discover_verification_inventory",
    "match_selector",
]
