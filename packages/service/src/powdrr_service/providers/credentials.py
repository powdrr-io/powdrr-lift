"""Service-owned provider credential discovery and provenance."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from powdrr_service.providers.config import (
    LLM_PROVIDERS,
    ProviderConfigurationError,
    provider_definition,
)


class ProviderCredentialsError(ProviderConfigurationError):
    """Required credentials for a configured provider are unavailable."""


@dataclass(frozen=True, slots=True)
class ProviderCredentials:
    provider: str
    api_key: str
    source: str
    base_url: str
    base_url_source: str


def resolve_provider_credentials(
    provider: str,
    api_key_override: str | None = None,
    base_url_override: str | None = None,
) -> ProviderCredentials:
    definition = provider_definition(provider)
    if api_key_override:
        api_key, source = api_key_override, "--api-key"
    elif definition.client_kind == "local":
        api_key, source = "local", "local"
    else:
        api_key = ""
        source = ""
        for env_name in definition.api_key_env_names:
            value = os.environ.get(env_name)
            if value:
                api_key, source = value, env_name
                break
        if not api_key and provider == "openai":
            access_token = _resolve_codex_access_token()
            if access_token is not None:
                api_key, source = access_token, _codex_auth_path_description()
        if not api_key:
            if provider == "openai":
                raise ProviderCredentialsError(
                    "No OpenAI credentials found. Set OPENAI_API_KEY, CODEX_API_KEY, "
                    "or sign in with Codex so ~/.codex/auth.json is available."
                )
            credential_names = " or ".join(definition.api_key_env_names)
            raise ProviderCredentialsError(
                f"No {definition.display_name} credentials found. "
                f"Set {credential_names}, or pass --api-key."
            )

    if base_url_override:
        base_url, base_url_source = base_url_override, "--base-url"
    elif definition.client_kind == "local":
        base_url, base_url_source = "local", "local"
    else:
        base_url, base_url_source = definition.default_base_url, "default"
        for env_name in definition.base_url_env_names:
            value = os.environ.get(env_name)
            if value:
                base_url, base_url_source = value, env_name
                break
    return ProviderCredentials(
        provider=provider,
        api_key=api_key,
        source=source,
        base_url=base_url,
        base_url_source=base_url_source,
    )


def _resolve_codex_access_token() -> str | None:
    auth_path = _resolve_codex_auth_path()
    if not auth_path.exists():
        return None
    try:
        raw_auth = json.loads(auth_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(raw_auth, dict):
        return None
    tokens = raw_auth.get("tokens")
    if not isinstance(tokens, dict):
        return None
    access_token = tokens.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        return None
    expiry = tokens.get("expiry")
    if isinstance(expiry, str):
        try:
            expiry_dt = datetime.fromisoformat(expiry)
        except ValueError:
            return access_token
        if expiry_dt.tzinfo is None:
            expiry_dt = expiry_dt.replace(tzinfo=UTC)
        if expiry_dt <= datetime.now(UTC):
            return None
    return access_token


def _resolve_codex_auth_path() -> Path:
    codex_home = os.environ.get("CODEX_HOME")
    if codex_home is not None:
        return Path(codex_home).expanduser() / "auth.json"
    return Path.home() / ".codex" / "auth.json"


def _codex_auth_path_description() -> str:
    return str(_resolve_codex_auth_path())


def provider_has_credentials(provider: str) -> bool:
    definition = provider_definition(provider)
    if provider == "openai" and _resolve_codex_access_token() is not None:
        return True
    return any(os.environ.get(env_name) for env_name in definition.api_key_env_names)


def auto_provider_candidates() -> tuple[str, ...]:
    candidates: list[tuple[int, str]] = []
    for name, definition in LLM_PROVIDERS.items():
        if definition.auto_priority is None or not provider_has_credentials(name):
            continue
        candidates.append((definition.auto_priority, name))
    return tuple(name for _, name in sorted(candidates))


def available_provider_names() -> tuple[str, ...]:
    candidates: list[tuple[int, str]] = []
    for name, definition in LLM_PROVIDERS.items():
        if not definition.api_key_env_names and name != "openai":
            continue
        if not provider_has_credentials(name):
            continue
        priority = definition.auto_priority
        candidates.append((priority if priority is not None else 100, name))
    return tuple(name for _, name in sorted(candidates))
