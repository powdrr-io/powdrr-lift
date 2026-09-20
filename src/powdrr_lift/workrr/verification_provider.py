"""Provider-neutral verification discovery and execution boundaries."""

from __future__ import annotations

import hashlib
import importlib.metadata
import os
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET
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
    command: tuple[str, ...] = ()
    timeout_seconds: float = 600.0


@dataclass(frozen=True, slots=True)
class ProviderInventory:
    provider: str
    profile: str
    selectors: tuple[str, ...]
    provider_version: str | None = None
    collection_error: str | None = None
    command: tuple[str, ...] = ()

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
            "command": list(self.command),
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
    stdout: str = ""
    stderr: str = ""
    duration_ms: int | None = None
    provider_version: str | None = None


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
                command=tuple(profile.command),
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
                tuple(profile.command),
            )
        finally:
            os.chdir(previous_directory)
        return ProviderInventory(
            self.name,
            profile.name,
            tuple(sorted(set(selectors))),
            _package_version("pytest"),
            None if exit_code == 0 else f"pytest collection exited with {exit_code}",
            tuple(profile.command),
        )

    def execute(self, request: VerificationProviderRequest) -> ProviderExecutionResult:
        command = request.command or ("python", "-m", "pytest")
        command = (*command, request.selector)
        junit_path: str | None = None
        started = time.monotonic()
        try:
            with tempfile.NamedTemporaryFile(
                prefix="powdrr-pytest-", suffix=".xml", dir=request.root, delete=False
            ) as artifact:
                junit_path = artifact.name
            completed = subprocess.run(
                [*command, f"--junitxml={junit_path}"],
                cwd=request.root,
                capture_output=True,
                text=True,
                timeout=request.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            return ProviderExecutionResult(
                self.name,
                request.selector,
                request.profile,
                "timed_out",
                returncode=124,
                error="pytest execution timed out",
                stdout=_output_text(error.stdout),
                stderr=_output_text(error.stderr),
                duration_ms=_duration_ms(started),
                provider_version=_package_version("pytest"),
            )
        except OSError as error:
            return ProviderExecutionResult(
                self.name,
                request.selector,
                request.profile,
                "errored",
                error=f"could not start pytest: {error}",
                duration_ms=_duration_ms(started),
                provider_version=_package_version("pytest"),
            )
        status = _pytest_junit_status(junit_path, completed.returncode)
        if junit_path is not None:
            try:
                os.unlink(junit_path)
            except OSError:
                pass
        return ProviderExecutionResult(
            self.name,
            request.selector,
            request.profile,
            status,
            returncode=completed.returncode,
            error=None
            if status == "passed"
            else f"pytest exited with {completed.returncode}",
            stdout=completed.stdout,
            stderr=completed.stderr,
            duration_ms=_duration_ms(started),
            provider_version=_package_version("pytest"),
        )

    def normalize(self, result: ProviderExecutionResult) -> dict[str, Any]:
        return {
            "provider": result.provider,
            "selector": result.selector,
            "profile": result.profile,
            "status": result.status,
            "returncode": result.returncode,
            "error": result.error,
            "duration_ms": result.duration_ms,
            "provider_version": result.provider_version,
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


def _duration_ms(started: float) -> int:
    return max(0, round((time.monotonic() - started) * 1000))


def _output_text(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return str(value or "")


def _pytest_junit_status(path: str | None, returncode: int) -> str:
    if path is None:
        return "errored"
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError):
        return "errored" if returncode else "passed"
    cases = list(root.iter("testcase"))
    if not cases:
        return "not_collected" if returncode == 5 else "errored"
    case = cases[0]
    if case.find("failure") is not None:
        return "failed"
    if case.find("error") is not None:
        return "errored"
    skipped = case.find("skipped")
    if skipped is not None:
        message = str(skipped.attrib.get("message", "")).lower()
        return "xfailed" if "xfail" in message else "skipped"
    return "passed" if returncode == 0 else "failed"


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
