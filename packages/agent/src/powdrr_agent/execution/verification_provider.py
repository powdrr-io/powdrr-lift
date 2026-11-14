"""Provider-neutral verification discovery and execution boundaries."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from powdrr_contracts.verification import VALIDATION_PROVIDER_INVENTORY_SCHEMA_VERSION

VERIFICATION_PROVIDER_INVENTORY_SCHEMA_VERSION = (
    VALIDATION_PROVIDER_INVENTORY_SCHEMA_VERSION
)


class DiscoveredValidationProfile(Protocol):
    """Provider-neutral profile fields consumed by local verification adapters."""

    @property
    def name(self) -> str: ...

    @property
    def command(self) -> Sequence[str]: ...

    @property
    def provider(self) -> str: ...


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
    cwd: str = "."
    environment_id: str | None = None
    execution_kind: str = "argv"
    shell: str | None = None
    script: str | None = None
    applicability: Mapping[str, Any] | None = None
    provider_version: str | None = None


@dataclass(frozen=True, slots=True)
class ProviderInventory:
    provider: str
    profile: str
    selectors: tuple[str, ...]
    provider_version: str | None = None
    collection_error: str | None = None
    command: tuple[str, ...] = ()
    cwd: str = "."
    environment_id: str | None = None
    execution_kind: str = "argv"
    shell: str | None = None
    script: str | None = None
    applicability: Mapping[str, Any] | None = None

    @property
    def fingerprint(self) -> str:
        payload = json.dumps(
            {
                "schema_version": VERIFICATION_PROVIDER_INVENTORY_SCHEMA_VERSION,
                "provider": self.provider,
                "profile": self.profile,
                "provider_version": self.provider_version,
                "selectors": self.selectors,
                "collection_error": self.collection_error,
                "command": self.command,
                "cwd": self.cwd,
                "environment_id": self.environment_id,
                "execution_kind": self.execution_kind,
                "shell": self.shell,
                "script": self.script,
                "applicability": self.applicability,
            },
            sort_keys=True,
            separators=(",", ":"),
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
            "cwd": self.cwd,
            "environment_id": self.environment_id,
            "execution_kind": self.execution_kind,
            "shell": self.shell,
            "script": self.script,
            "applicability": (
                dict(self.applicability) if self.applicability is not None else None
            ),
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
        context = _profile_context(profile)
        execution_error = _profile_execution_error(context)
        if execution_error:
            return ProviderInventory(
                self.name,
                profile.name,
                (),
                collection_error=execution_error,
                command=tuple(profile.command),
                **context,
            )
        cwd, cwd_error = _execution_directory(root, context["cwd"])
        if cwd_error:
            return ProviderInventory(
                self.name,
                profile.name,
                (),
                collection_error=cwd_error,
                command=tuple(profile.command),
                **context,
            )
        with tempfile.TemporaryDirectory(prefix="powdrr-pytest-inventory-") as temp:
            plugin_dir = Path(temp)
            plugin_path = plugin_dir / "_powdrr_inventory_plugin.py"
            output_path = plugin_dir / "inventory.json"
            plugin_path.write_text(
                "import json, os\n"
                "import pytest\n"
                "def pytest_collection_finish(session):\n"
                "    payload = {'selectors': [item.nodeid for item in session.items], "
                "'pytest_version': pytest.__version__}\n"
                "    with open(os.environ['POWDRR_PYTEST_INVENTORY_OUTPUT'], "
                "'w', encoding='utf-8') as stream:\n"
                "        json.dump(payload, stream)\n",
                encoding="utf-8",
            )
            environment = _target_environment(plugin_dir, output_path)
            command = (
                *profile.command,
                "--collect-only",
                "-q",
                "-p",
                "_powdrr_inventory_plugin",
            )
            try:
                completed = subprocess.run(
                    command,
                    cwd=cwd,
                    env=environment,
                    capture_output=True,
                    text=True,
                    timeout=600,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as error:
                return ProviderInventory(
                    self.name,
                    profile.name,
                    (),
                    collection_error=f"pytest collection failed: {error}",
                    command=tuple(profile.command),
                    **context,
                )
            try:
                payload = json.loads(output_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                payload = None
        if not isinstance(payload, Mapping) or not isinstance(
            payload.get("selectors"), list
        ):
            return ProviderInventory(
                self.name,
                profile.name,
                (),
                collection_error=(
                    f"pytest collection exited with {completed.returncode}"
                    if completed.returncode
                    else "pytest collection did not produce its inventory"
                ),
                command=tuple(profile.command),
                **context,
            )
        selectors = tuple(
            sorted(
                {
                    item
                    for item in payload["selectors"]
                    if isinstance(item, str) and item
                }
            )
        )
        return ProviderInventory(
            self.name,
            profile.name,
            selectors,
            payload.get("pytest_version")
            if isinstance(payload.get("pytest_version"), str)
            else None,
            None
            if completed.returncode == 0
            else f"pytest collection exited with {completed.returncode}",
            tuple(profile.command),
            **context,
        )

    def execute(self, request: VerificationProviderRequest) -> ProviderExecutionResult:
        context = {
            "cwd": request.cwd,
            "environment_id": request.environment_id,
            "execution_kind": request.execution_kind,
            "shell": request.shell,
            "script": request.script,
            "applicability": request.applicability,
        }
        execution_error = _profile_execution_error(context)
        if execution_error:
            return ProviderExecutionResult(
                self.name,
                request.selector,
                request.profile,
                "errored",
                error=execution_error,
            )
        cwd, cwd_error = _execution_directory(request.root, request.cwd)
        if cwd_error:
            return ProviderExecutionResult(
                self.name,
                request.selector,
                request.profile,
                "errored",
                error=cwd_error,
            )
        command = request.command or ("python", "-m", "pytest")
        command = (*command, request.selector)
        junit_path: str | None = None
        started = time.monotonic()
        try:
            with tempfile.NamedTemporaryFile(
                prefix="powdrr-pytest-",
                suffix=".xml",
                dir=request.root,
                delete=False,
            ) as artifact:
                junit_path = artifact.name
            completed = subprocess.run(
                [*command, f"--junitxml={junit_path}"],
                cwd=cwd,
                env=_target_environment(),
                capture_output=True,
                text=True,
                timeout=request.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            if junit_path is not None:
                try:
                    os.unlink(junit_path)
                except OSError:
                    pass
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
                provider_version=request.provider_version,
            )
        except OSError as error:
            if junit_path is not None:
                try:
                    os.unlink(junit_path)
                except OSError:
                    pass
            return ProviderExecutionResult(
                self.name,
                request.selector,
                request.profile,
                "errored",
                error=f"could not start pytest: {error}",
                duration_ms=_duration_ms(started),
                provider_version=request.provider_version,
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
            provider_version=request.provider_version,
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


class StestrVerificationProvider:
    """Collect and run exact unittest identities through stestr."""

    name = "stestr"

    def detect(
        self,
        root: Path,
        profiles: Sequence[DiscoveredValidationProfile],
    ) -> bool:
        del root
        return any(
            profile.provider == self.name
            or _command_mentions(profile.command, self.name)
            for profile in profiles
        )

    def inventory(
        self, root: Path, profile: DiscoveredValidationProfile
    ) -> ProviderInventory:
        context = _profile_context(profile)
        execution_error = _profile_execution_error(context)
        if execution_error:
            return ProviderInventory(
                self.name,
                profile.name,
                (),
                collection_error=execution_error,
                command=tuple(profile.command),
                **context,
            )
        cwd, cwd_error = _execution_directory(root, context["cwd"])
        if cwd_error:
            return ProviderInventory(
                self.name,
                profile.name,
                (),
                collection_error=cwd_error,
                command=tuple(profile.command),
                **context,
            )
        command = _stestr_command(profile.command, "list")
        try:
            completed = subprocess.run(
                command,
                cwd=cwd,
                env=_target_environment(),
                capture_output=True,
                text=True,
                timeout=600,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            return ProviderInventory(
                self.name,
                profile.name,
                (),
                None,
                f"stestr collection failed: {error}",
                command,
                **context,
            )
        version = _stestr_version(profile.command, cwd)
        selectors = tuple(
            sorted(
                {line.strip() for line in completed.stdout.splitlines() if line.strip()}
            )
        )
        return ProviderInventory(
            self.name,
            profile.name,
            selectors,
            version,
            None
            if completed.returncode == 0
            else f"stestr list exited with {completed.returncode}",
            command,
            **context,
        )

    def execute(self, request: VerificationProviderRequest) -> ProviderExecutionResult:
        context = {
            "cwd": request.cwd,
            "environment_id": request.environment_id,
            "execution_kind": request.execution_kind,
            "shell": request.shell,
            "script": request.script,
            "applicability": request.applicability,
        }
        execution_error = _profile_execution_error(context)
        if execution_error:
            return ProviderExecutionResult(
                self.name,
                request.selector,
                request.profile,
                "errored",
                error=execution_error,
            )
        cwd, cwd_error = _execution_directory(request.root, request.cwd)
        if cwd_error:
            return ProviderExecutionResult(
                self.name,
                request.selector,
                request.profile,
                "errored",
                error=cwd_error,
            )
        command = (*_stestr_command(request.command, "run"), request.selector)
        started = time.monotonic()
        try:
            completed = subprocess.run(
                command,
                cwd=cwd,
                env=_target_environment(),
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
                124,
                "stestr execution timed out",
                _output_text(error.stdout),
                _output_text(error.stderr),
                _duration_ms(started),
                request.provider_version,
            )
        except OSError as error:
            return ProviderExecutionResult(
                self.name,
                request.selector,
                request.profile,
                "errored",
                error=f"could not start stestr: {error}",
                duration_ms=_duration_ms(started),
                provider_version=request.provider_version,
            )
        return ProviderExecutionResult(
            self.name,
            request.selector,
            request.profile,
            "passed" if completed.returncode == 0 else "failed",
            completed.returncode,
            None
            if completed.returncode == 0
            else f"stestr exited with {completed.returncode}",
            completed.stdout,
            completed.stderr,
            _duration_ms(started),
            request.provider_version,
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
    return VerificationProviderRegistry(
        (PytestVerificationProvider(), StestrVerificationProvider())
    )


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


def _profile_context(profile: DiscoveredValidationProfile) -> dict[str, Any]:
    applicability = getattr(profile, "applicability", None)
    return {
        "cwd": getattr(profile, "cwd", "."),
        "environment_id": getattr(profile, "environment_id", None),
        "execution_kind": getattr(profile, "execution_kind", "argv") or "argv",
        "shell": getattr(profile, "shell", None),
        "script": getattr(profile, "script", None),
        "applicability": dict(applicability)
        if isinstance(applicability, Mapping)
        else None,
    }


def _profile_execution_error(context: Mapping[str, Any]) -> str | None:
    if (
        context.get("execution_kind") != "argv"
        or context.get("shell")
        or context.get("script")
    ):
        return (
            "provider execution requires an argv profile "
            "without shell or script semantics"
        )
    return None


def _execution_directory(root: Path, relative_cwd: str) -> tuple[Path, str | None]:
    relative_path = Path(relative_cwd)
    if not relative_cwd or relative_path.is_absolute() or ".." in relative_path.parts:
        return root, "validation profile cwd must be repository-relative"
    resolved_root = root.resolve()
    resolved_cwd = (resolved_root / relative_path).resolve()
    if not resolved_cwd.is_relative_to(resolved_root):
        return resolved_root, "validation profile cwd escapes the repository"
    if not resolved_cwd.is_dir():
        return resolved_root, f"validation profile cwd does not exist: {relative_cwd}"
    return resolved_cwd, None


def _target_environment(
    plugin_dir: Path | None = None, output_path: Path | None = None
) -> dict[str, str]:
    environment = os.environ.copy()
    environment.pop("VIRTUAL_ENV", None)
    if plugin_dir is not None:
        existing = environment.get("PYTHONPATH")
        environment["PYTHONPATH"] = (
            str(plugin_dir) if not existing else str(plugin_dir) + os.pathsep + existing
        )
    if output_path is not None:
        environment["POWDRR_PYTEST_INVENTORY_OUTPUT"] = str(output_path)
    return environment


def _stestr_command(command: Sequence[str], action: str) -> tuple[str, ...]:
    try:
        index = next(
            index
            for index, part in enumerate(command)
            if Path(part).name.casefold() == "stestr"
        )
    except StopIteration:
        return ("stestr", action)
    tail = list(command[index + 1 :])
    if tail and tail[0] in {"run", "list"}:
        tail.pop(0)
    if action == "list":
        tail = []
    return (*command[: index + 1], action, *tail)


def _stestr_version(command: Sequence[str], cwd: Path) -> str | None:
    try:
        index = next(
            index
            for index, part in enumerate(command)
            if Path(part).name.casefold() == "stestr"
        )
    except StopIteration:
        version_command = ("stestr", "--version")
    else:
        version_command = (*command[: index + 1], "--version")
    try:
        completed = subprocess.run(
            version_command,
            cwd=cwd,
            env=_target_environment(),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    return next(
        (line.strip() for line in completed.stdout.splitlines() if line.strip()), None
    )


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
    "StestrVerificationProvider",
    "VERIFICATION_PROVIDER_INVENTORY_SCHEMA_VERSION",
    "VerificationProvider",
    "VerificationProviderRegistry",
    "VerificationProviderRequest",
    "default_verification_provider_registry",
    "discover_verification_inventory",
    "match_selector",
]
