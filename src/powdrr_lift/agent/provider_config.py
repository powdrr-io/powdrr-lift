"""Provider-independent model and provider configuration types."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from powdrr_lift.errors import PowdrrExecutionError


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


MAX_COMPLETION_TOKENS = 32768
DEFAULT_MODEL = "glm-5.2"
DEFAULT_LLM_TYPE = "high_reasoning"
QWEN_2_5_CODER_MODEL = "Qwen/Qwen2.5-Coder-14B-Instruct"
DEEPINFRA_CHEAP_MODEL = "deepseek-ai/DeepSeek-V4-Flash-0731"
DEEPINFRA_CHEAP_BACKUP_MODEL = "deepseek-ai/DeepSeek-V4-Flash"
OPENROUTER_MODEL = "stealth/ox-alpha"

ALL_LLM_TYPES = (
    "high_reasoning",
    "standard_reasoning",
    "simple_task",
    "fast_iteration",
    "long_context",
    "vision",
)

DEFAULT_MODEL_LIMITS = LLMModelLimits(
    context_window=128_000,
    max_output_tokens=MAX_COMPLETION_TOKENS,
)

ZAI_MODEL_LIMITS: Mapping[str, LLMModelLimits] = {
    "glm-5.2": LLMModelLimits(context_window=200_000, max_output_tokens=131_072),
    "glm-4.7": LLMModelLimits(context_window=200_000, max_output_tokens=131_072),
    "glm-4.7-flashx": LLMModelLimits(context_window=200_000, max_output_tokens=131_072),
    "glm-4.7-flash": LLMModelLimits(context_window=200_000, max_output_tokens=131_072),
    "glm-4.6v": LLMModelLimits(context_window=200_000, max_output_tokens=32_768),
}

DEEPINFRA_MODEL_LIMITS: Mapping[str, LLMModelLimits] = {
    "deepseek-ai/deepseek-v4-pro": LLMModelLimits(
        context_window=1_000_000, max_output_tokens=16_384
    ),
    "deepseek-ai/deepseek-v4-flash": LLMModelLimits(
        context_window=1_000_000, max_output_tokens=16_384
    ),
    "deepseek-ai/deepseek-v4-flash-0731": LLMModelLimits(
        context_window=1_000_000, max_output_tokens=16_384
    ),
    "qwen/qwen3-next-80b-a3b-instruct": LLMModelLimits(
        context_window=128_000, max_output_tokens=16_384
    ),
    "qwen/qwen2.5-vl-32b-instruct": LLMModelLimits(
        context_window=128_000, max_output_tokens=16_384
    ),
}

ZAI_LLM_MAPPINGS: Mapping[str, LLMModelMapping] = {
    "high_reasoning": LLMModelMapping("glm-5.2", provider="zai"),
    "standard_reasoning": LLMModelMapping("glm-4.7", provider="zai"),
    "simple_task": LLMModelMapping(
        QWEN_2_5_CODER_MODEL,
        provider="local",
        backup_model=LLMModelMapping("glm-4.7", provider="zai"),
        long_context_backup_model=LLMModelMapping("glm-5.2", provider="zai"),
    ),
    "fast_iteration": LLMModelMapping(
        QWEN_2_5_CODER_MODEL,
        provider="local",
        long_context_backup_model=LLMModelMapping("glm-4.7-flash", provider="zai"),
    ),
    "long_context": LLMModelMapping("glm-5.2", provider="zai"),
    "vision": LLMModelMapping("glm-4.6v", provider="zai"),
}

DEEPINFRA_LLM_MAPPINGS: Mapping[str, LLMModelMapping] = {
    "high_reasoning": LLMModelMapping(
        "deepseek-ai/DeepSeek-V4-Pro", provider="deepinfra"
    ),
    "standard_reasoning": LLMModelMapping(
        "deepseek-ai/DeepSeek-V4-Flash", provider="deepinfra"
    ),
    "simple_task": LLMModelMapping(
        "Qwen/Qwen3-Next-80B-A3B-Instruct",
        provider="deepinfra",
        long_context_backup_model=LLMModelMapping(
            "deepseek-ai/DeepSeek-V4-Flash", provider="deepinfra"
        ),
    ),
    "fast_iteration": LLMModelMapping(
        "Qwen/Qwen3-Next-80B-A3B-Instruct",
        provider="deepinfra",
        long_context_backup_model=LLMModelMapping(
            "deepseek-ai/DeepSeek-V4-Flash", provider="deepinfra"
        ),
    ),
    "long_context": LLMModelMapping(
        "deepseek-ai/DeepSeek-V4-Flash", provider="deepinfra"
    ),
    "vision": LLMModelMapping("Qwen/Qwen2.5-VL-32B-Instruct", provider="deepinfra"),
}

DEEPINFRA_CHEAP_LLM_MAPPINGS: Mapping[str, LLMModelMapping] = {
    llm_type: LLMModelMapping(
        DEEPINFRA_CHEAP_MODEL,
        provider="deepinfra-cheap",
        backup_model=LLMModelMapping(
            DEEPINFRA_CHEAP_BACKUP_MODEL, provider="deepinfra-cheap"
        ),
    )
    for llm_type in ALL_LLM_TYPES
}

OPENROUTER_LLM_MAPPINGS: Mapping[str, LLMModelMapping] = {
    llm_type: LLMModelMapping(OPENROUTER_MODEL, provider="openrouter")
    for llm_type in ALL_LLM_TYPES
}

LLM_PROVIDERS: Mapping[str, LLMProviderDefinition] = {
    "openai": LLMProviderDefinition(
        name="openai",
        display_name="OpenAI",
        llm_mappings={},
        model_limits={},
        api_key_env_names=("OPENAI_API_KEY", "CODEX_API_KEY"),
        base_url_env_names=("OPENAI_BASE_URL", "CODEX_BASE_URL"),
        auto_priority=40,
    ),
    "anthropic": LLMProviderDefinition(
        name="anthropic",
        display_name="Anthropic",
        llm_mappings={},
        model_limits={},
        api_key_env_names=("ANTHROPIC_API_KEY", "CLAUDE_API_KEY"),
        base_url_env_names=("ANTHROPIC_BASE_URL",),
        default_base_url="https://api.anthropic.com",
        client_kind="anthropic",
        auto_priority=20,
    ),
    "zai": LLMProviderDefinition(
        name="zai",
        display_name="z.ai",
        llm_mappings=ZAI_LLM_MAPPINGS,
        model_limits=ZAI_MODEL_LIMITS,
        api_key_env_names=("ZAI_API_KEY", "GLM_API_KEY"),
        base_url_env_names=("ZAI_BASE_URL",),
        default_base_url="https://api.z.ai/api/paas/v4/",
        auto_priority=30,
    ),
    "openrouter": LLMProviderDefinition(
        name="openrouter",
        display_name="OpenRouter",
        llm_mappings=OPENROUTER_LLM_MAPPINGS,
        model_limits={},
        api_key_env_names=("OPENROUTER_API_KEY",),
        base_url_env_names=("OPENROUTER_BASE_URL",),
        default_base_url="https://openrouter.ai/api/v1",
        auto_priority=10,
    ),
    "deepinfra": LLMProviderDefinition(
        name="deepinfra",
        display_name="DeepInfra",
        llm_mappings=DEEPINFRA_LLM_MAPPINGS,
        model_limits=DEEPINFRA_MODEL_LIMITS,
        api_key_env_names=("DEEPINFRA_API_TOKEN", "DEEPINFRA_API_KEY"),
        base_url_env_names=("DEEPINFRA_BASE_URL",),
        default_base_url="https://api.deepinfra.com/v1/openai",
        auto_priority=None,
    ),
    "deepinfra-cheap": LLMProviderDefinition(
        name="deepinfra-cheap",
        display_name="DeepInfra",
        llm_mappings=DEEPINFRA_CHEAP_LLM_MAPPINGS,
        model_limits=DEEPINFRA_MODEL_LIMITS,
        api_key_env_names=("DEEPINFRA_API_TOKEN", "DEEPINFRA_API_KEY"),
        base_url_env_names=("DEEPINFRA_BASE_URL",),
        default_base_url="https://api.deepinfra.com/v1/openai",
        forced_model=DEEPINFRA_CHEAP_MODEL,
        auto_priority=0,
    ),
    "local": LLMProviderDefinition(
        name="local",
        display_name="local",
        llm_mappings=ZAI_LLM_MAPPINGS,
        model_limits={},
        default_base_url="local",
        client_kind="local",
    ),
}
ALL_PROVIDERS = tuple(LLM_PROVIDERS)


def provider_definition(provider: str) -> LLMProviderDefinition:
    try:
        return LLM_PROVIDERS[provider]
    except KeyError as exc:
        raise PowdrrExecutionError(f"Unsupported LLM provider {provider!r}.") from exc


def default_llm_mappings(provider: str) -> Mapping[str, LLMModelMapping]:
    return provider_definition(provider).llm_mappings


def provider_supports_llm_mappings(provider: str) -> bool:
    return bool(provider_definition(provider).llm_mappings)
