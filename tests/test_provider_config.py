from __future__ import annotations

import pytest
from powdrr_service.providers.config import (
    DEFAULT_MODEL,
    LLM_PROVIDERS,
    ProviderConfigurationError,
    default_llm_mappings,
    provider_definition,
)


def test_service_provider_registry_has_resolvable_mapping_targets() -> None:
    for provider_name, definition in LLM_PROVIDERS.items():
        assert provider_definition(provider_name) is definition
        for mapping in definition.llm_mappings.values():
            assert mapping.provider in LLM_PROVIDERS
            if mapping.backup_model is not None:
                assert mapping.backup_model.provider in LLM_PROVIDERS
            if mapping.long_context_backup_model is not None:
                assert mapping.long_context_backup_model.provider in LLM_PROVIDERS

    assert default_llm_mappings("zai")["high_reasoning"].model == DEFAULT_MODEL


def test_service_provider_registry_rejects_unknown_provider() -> None:
    with pytest.raises(
        ProviderConfigurationError, match="Unsupported LLM provider 'missing'"
    ):
        provider_definition("missing")
