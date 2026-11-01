from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from powdrr_service.providers.credentials import (
    ProviderCredentialsError,
    resolve_provider_credentials,
)


def test_provider_credentials_use_explicit_overrides_before_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPINFRA_API_TOKEN", "environment-token")
    monkeypatch.setenv("DEEPINFRA_BASE_URL", "https://env.example/v1")

    credentials = resolve_provider_credentials(
        "deepinfra", "explicit-token", "https://override.example/v1"
    )

    assert credentials.api_key == "explicit-token"
    assert credentials.source == "--api-key"
    assert credentials.base_url == "https://override.example/v1"
    assert credentials.base_url_source == "--base-url"


def test_provider_credentials_record_selected_environment_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPINFRA_API_TOKEN", "deepinfra-token")
    monkeypatch.setenv("DEEPINFRA_BASE_URL", "https://custom.example/v1")

    credentials = resolve_provider_credentials("deepinfra")

    assert credentials.api_key == "deepinfra-token"
    assert credentials.source == "DEEPINFRA_API_TOKEN"
    assert credentials.base_url == "https://custom.example/v1"
    assert credentials.base_url_source == "DEEPINFRA_BASE_URL"


def test_openai_credentials_can_resolve_a_codex_access_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    auth_path = codex_home / "auth.json"
    auth_path.write_text(
        json.dumps(
            {
                "tokens": {
                    "access_token": "codex-access-token",
                    "expiry": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("CODEX_API_KEY", raising=False)

    credentials = resolve_provider_credentials("openai")

    assert credentials.api_key == "codex-access-token"
    assert credentials.source == str(auth_path)


def test_missing_provider_credentials_raise_service_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DEEPINFRA_API_TOKEN", raising=False)
    monkeypatch.delenv("DEEPINFRA_API_KEY", raising=False)

    with pytest.raises(ProviderCredentialsError, match="No DeepInfra credentials"):
        resolve_provider_credentials("deepinfra")
