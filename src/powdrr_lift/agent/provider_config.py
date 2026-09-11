"""Provider-independent model and provider configuration types."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True, slots=True)
class LLMModelLimits:
    context_window: int
    max_output_tokens: int


@dataclass(frozen=True, slots=True)
class LLMModelMapping:
    model: str
    provider: str
    backup_model: LLMModelMapping | None = None
    long_context_backup_model: LLMModelMapping | None = None


LLMProviderRole = Literal["normal", "adversarial"]


@dataclass(frozen=True, slots=True)
class LLMProviderRoles:
    normal: str
    adversarial: str | None = None

    def provider_for(self, role: LLMProviderRole) -> str:
        if role == "adversarial" and self.adversarial is not None:
            return self.adversarial
        return self.normal


@dataclass(frozen=True, slots=True)
class LLMProviderDefinition:
    name: str
    display_name: str
    llm_mappings: Mapping[str, LLMModelMapping]
    model_limits: Mapping[str, LLMModelLimits]
    api_key_env_names: tuple[str, ...] = ()
    base_url_env_names: tuple[str, ...] = ()
    default_base_url: str = "https://api.openai.com/v1"
    client_kind: str = "openai"
    forced_model: str | None = None
    auto_priority: int | None = None
