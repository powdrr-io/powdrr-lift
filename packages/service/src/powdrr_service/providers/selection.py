"""Service-owned provider and model selection policy."""

from __future__ import annotations

from collections.abc import Sequence

from powdrr_service.providers.config import (
    DEFAULT_LLM_TYPE,
    DEFAULT_MODEL,
    DEFAULT_MODEL_LIMITS,
    MAX_COMPLETION_TOKENS,
    LLMModelLimits,
    LLMModelMapping,
    LLMProviderRoles,
    ProviderConfigurationError,
    default_llm_mappings,
    provider_definition,
    provider_supports_llm_mappings,
)
from powdrr_service.providers.credentials import auto_provider_candidates


def resolve_provider_roles(
    provider: str,
    *,
    normal_provider: str | None = None,
    adversarial_provider: str | None = None,
) -> LLMProviderRoles:
    if normal_provider is not None:
        normal = normal_provider
    elif provider == "auto":
        candidates = auto_provider_candidates()
        normal = candidates[0] if candidates else "openai"
    else:
        normal = provider
    provider_definition(normal)

    adversarial = adversarial_provider
    if adversarial is None and provider == "auto":
        candidates = auto_provider_candidates()
        adversarial = next(
            (candidate for candidate in candidates if candidate != normal),
            None,
        )
    if adversarial is not None:
        provider_definition(adversarial)
    return LLMProviderRoles(normal=normal, adversarial=adversarial)


def resolve_provider(
    provider_override: str,
    model: str,
    *,
    mapping: LLMModelMapping | None = None,
) -> str:
    """Resolve a provider for a model, honoring explicit mapping ownership."""
    if mapping is not None:
        provider_definition(mapping.provider)
        return mapping.provider
    if provider_override != "auto":
        provider_definition(provider_override)
        return provider_override
    candidates = auto_provider_candidates()
    if candidates:
        return candidates[0]
    if model.startswith("claude-"):
        return "anthropic"
    return "openai"


def initial_model_for_provider(provider: str, configured_model: str) -> str:
    """Resolve the first request model using the selected provider's mapping."""
    definition = provider_definition(provider)
    if definition.forced_model is not None:
        return definition.forced_model
    if configured_model != DEFAULT_MODEL:
        return configured_model
    mapping = definition.llm_mappings.get(DEFAULT_LLM_TYPE)
    return mapping.model if mapping is not None else configured_model


def resolve_llm_model(
    llm_type: str | None,
    *,
    fallback_model: str,
    mappings: Sequence[tuple[str, LLMModelMapping]],
    provider: str = "zai",
) -> str:
    if llm_type is None or not provider_supports_llm_mappings(provider):
        return fallback_model
    resolved_mapping = resolve_llm_mapping(
        llm_type,
        mappings=mappings,
        provider=provider,
    )
    return resolved_mapping.model if resolved_mapping is not None else fallback_model


def resolve_llm_mapping(
    llm_type: str | None,
    *,
    mappings: Sequence[tuple[str, LLMModelMapping]],
    provider: str,
) -> LLMModelMapping | None:
    if llm_type is None:
        return None
    if not provider_supports_llm_mappings(provider):
        raise ProviderConfigurationError(
            f"LLM mappings are not supported for provider {provider!r}."
        )
    normalized_llm_type = llm_type.strip().lower().replace("-", "_")
    mapping = dict(default_llm_mappings(provider))
    mapping.update(
        {key.strip().lower().replace("-", "_"): value for key, value in mappings}
    )
    resolved_mapping = mapping.get(normalized_llm_type)
    if resolved_mapping is None:
        raise ProviderConfigurationError(
            f"No LLM mapping is configured for llm_type {llm_type!r} "
            f"with provider {provider!r}."
        )
    return resolved_mapping


def backup_model_for(
    model: str,
    model_mappings: Sequence[tuple[str, LLMModelMapping]],
) -> LLMModelMapping | None:
    normalized_model = model.casefold()
    for _, mapping in model_mappings:
        if mapping.model.casefold() == normalized_model:
            return mapping.backup_model
    return None


def long_context_backup_for(
    model: str,
    model_mappings: Sequence[tuple[str, LLMModelMapping]],
) -> LLMModelMapping | None:
    normalized_model = model.casefold()
    for _, mapping in model_mappings:
        if mapping.model.casefold() == normalized_model:
            return mapping.long_context_backup_model
    return None


def provider_model_limits(
    provider: str,
    model: str,
    *,
    local_context: int = 24_576,
) -> LLMModelLimits:
    definition = provider_definition(provider)
    if definition.client_kind == "local":
        return LLMModelLimits(
            context_window=local_context,
            max_output_tokens=MAX_COMPLETION_TOKENS,
        )
    return definition.model_limits.get(model.casefold(), DEFAULT_MODEL_LIMITS)
