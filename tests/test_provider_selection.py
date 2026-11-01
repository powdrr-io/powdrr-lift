from __future__ import annotations

from pathlib import Path

import pytest
from powdrr_service.providers.config import (
    DEFAULT_MODEL,
    LLMModelMapping,
    ProviderConfigurationError,
)
from powdrr_service.providers.selection import (
    initial_model_for_provider,
    provider_model_limits,
    resolve_llm_mapping,
    resolve_provider,
    resolve_provider_roles,
)


def test_provider_roles_follow_service_discovery_priority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    for name in (
        "OPENAI_API_KEY",
        "CODEX_API_KEY",
        "ANTHROPIC_API_KEY",
        "CLAUDE_API_KEY",
        "ZAI_API_KEY",
        "GLM_API_KEY",
        "OPENROUTER_API_KEY",
        "DEEPINFRA_API_TOKEN",
        "DEEPINFRA_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "router-token")
    monkeypatch.setenv("ZAI_API_KEY", "zai-token")

    roles = resolve_provider_roles("auto")

    assert roles.normal == "openrouter"
    assert roles.adversarial == "zai"


def test_explicit_mapping_provider_wins_over_provider_override() -> None:
    mapping = LLMModelMapping("glm-5.2", provider="zai")

    assert resolve_provider("openai", "some-model", mapping=mapping) == "zai"


def test_forced_provider_model_wins_over_configured_default() -> None:
    assert initial_model_for_provider("deepinfra-cheap", DEFAULT_MODEL) == (
        "deepseek-ai/DeepSeek-V4-Flash-0731"
    )


def test_provider_model_mapping_reports_service_configuration_errors() -> None:
    with pytest.raises(ProviderConfigurationError, match="not supported"):
        resolve_llm_mapping("high_reasoning", mappings=(), provider="openai")


def test_local_model_limits_use_the_requested_context_size() -> None:
    limits = provider_model_limits("local", "any-model", local_context=8_192)

    assert limits.context_window == 8_192
