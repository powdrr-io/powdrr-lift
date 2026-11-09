"""Preflight configured model credentials and provider access."""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError

from powdrr_service.decision_classifier import (
    DecisionProviderFactory,
)
from powdrr_service.providers.config import (
    LLMModelMapping,
    ProviderConfigurationError,
    default_llm_mappings,
)
from powdrr_service.providers.credentials import (
    ProviderCredentials,
    resolve_provider_credentials,
)

from powdrr_lift.errors import PowdrrExecutionError
from powdrr_lift.workrr.llm import WorkflowLLMHTTPError
from powdrr_lift.workrr.provider_runtime import build_workflow_client

CredentialResolver = Callable[..., ProviderCredentials]
ClientBuilder = Callable[..., Any]


@dataclass(frozen=True, slots=True)
class CredentialCheck:
    role: str
    provider: str
    model: str
    status: str
    credential_source: str | None = None
    message: str | None = None

    def to_data(self) -> dict[str, str]:
        data = {
            "role": self.role,
            "provider": self.provider,
            "model": self.model,
            "status": self.status,
        }
        if self.credential_source:
            data["credential_source"] = self.credential_source
        if self.message:
            data["message"] = self.message
        return data


def check_model_access(
    *,
    role: str,
    provider: str,
    mapping: LLMModelMapping,
    repo_root: Path,
    api_key: str | None = None,
    base_url: str | None = None,
    credential_resolver: CredentialResolver = resolve_provider_credentials,
    client_builder: ClientBuilder = build_workflow_client,
) -> CredentialCheck:
    """Make one minimal authenticated completion using the selected model."""
    model = mapping.model
    try:
        credentials = credential_resolver(provider, api_key, base_url)
        client = client_builder(
            credentials,
            model=model,
            model_cache_dir=repo_root / ".powdrr" / "models",
            progress_stream=None,
            timeout=10.0,
        )
        client.complete_json(
            [
                {
                    "role": "user",
                    "content": 'Reply with the JSON object {"ok":true}.',
                }
            ]
        )
    except WorkflowLLMHTTPError as error:
        status = {
            401: "authentication_failed",
            403: "model_access_denied",
            429: "rate_or_quota_limited",
        }.get(error.status_code, "provider_rejected_request")
        return CredentialCheck(
            role=role,
            provider=provider,
            model=model,
            status=status,
            message=f"Provider returned HTTP {error.status_code}.",
        )
    except (
        PowdrrExecutionError,
        ProviderConfigurationError,
        OSError,
        TimeoutError,
    ) as error:
        message = _safe_error_message(error)
        return CredentialCheck(
            role=role,
            provider=provider,
            model=model,
            status=(
                "credentials_missing"
                if message.startswith("No ") and " credentials found." in message
                else "unavailable"
            ),
            message=message,
        )

    return CredentialCheck(
        role=role,
        provider=provider,
        model=model,
        status="passed",
        credential_source=credentials.source,
    )


def check_coding_executable(executable: str, *, role: str) -> CredentialCheck:
    """Check that the selected coding-agent executable can be found."""
    resolved = shutil.which(executable)
    return CredentialCheck(
        role=role,
        provider="local-executable",
        model=executable,
        status="passed" if resolved else "executable_missing",
        message=None if resolved else f"Could not find {executable!r} on PATH.",
    )


def check_decision_provider_access() -> CredentialCheck:
    """Check whichever decision provider the startup factory selected."""
    try:
        factory = DecisionProviderFactory()
    except RuntimeError as error:
        return CredentialCheck(
            role="decision-provider",
            provider="decision-provider",
            model="configured",
            status="credentials_missing",
            message=str(error),
        )
    provider = factory.create(_CredentialCheckFallback(), fail_closed=True)
    if provider is None:
        return CredentialCheck(
            role="decision-provider",
            provider="decision-provider",
            model="configured",
            status="credentials_missing",
            message=(
                "Configure a decision provider with POWDRR_DECISION_PROVIDER and "
                "its credentials, or configure one supported provider key."
            ),
        )

    try:
        messages = [
            {"role": "system", "content": ""},
            {
                "role": "user",
                "content": (
                    "Return true to confirm authenticated decision-provider access."
                    '\n\nContext:\n{"decision_provider_check":{"text":'
                    '"authenticated decision-provider access check"}}'
                ),
            },
        ]
        result = provider.complete_json(
            messages, response_schema=DECISION_PROVIDER_RESPONSE_SCHEMA
        )
        if not isinstance(result.get("decision"), bool):
            raise ValueError("Decision provider returned an invalid health result.")
    except HTTPError as error:
        status = {
            401: "authentication_failed",
            403: "access_denied",
            429: "rate_or_quota_limited",
        }.get(error.code, "provider_rejected_request")
        return CredentialCheck(
            role="decision-provider",
            provider=factory.provider_name or "decision-provider",
            model=provider.model,
            status=status,
            credential_source=factory.credential_source,
            message=f"Decision provider returned HTTP {error.code}.",
        )
    except (OSError, TimeoutError, URLError, ValueError, RuntimeError) as error:
        return CredentialCheck(
            role="decision-provider",
            provider=factory.provider_name or "decision-provider",
            model=provider.model,
            status="unavailable",
            credential_source=factory.credential_source,
            message=(
                f"Decision-provider access check failed: {_safe_error_message(error)}"
            ),
        )

    return CredentialCheck(
        role="decision-provider",
        provider=factory.provider_name or "decision-provider",
        model=provider.model,
        status="passed",
        credential_source=factory.credential_source,
    )


class _CredentialCheckFallback:
    def complete_json(self, _messages: list[dict[str, str]]) -> dict[str, Any]:
        raise RuntimeError("Decision-provider health check cannot use an LLM fallback")


DECISION_PROVIDER_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {"decision": {"type": "boolean"}},
    "required": ["decision"],
    "additionalProperties": False,
}


def configured_model_mapping(provider: str, model: str | None) -> LLMModelMapping:
    """Resolve an explicit model or the provider's standard reasoning model."""
    mappings: Mapping[str, LLMModelMapping] = default_llm_mappings(provider)
    if model:
        return LLMModelMapping(model=model, provider=provider)
    try:
        return mappings["standard_reasoning"]
    except KeyError as error:
        raise PowdrrExecutionError(
            f"Provider {provider!r} has no default standard reasoning model; "
            "supply an explicit model."
        ) from error


def _safe_error_message(error: Exception) -> str:
    """Avoid including credential values in diagnostics from provider clients."""
    message = str(error)
    for key_name in (
        "OPENAI_API_KEY",
        "CODEX_API_KEY",
        "ANTHROPIC_API_KEY",
        "ZAI_API_KEY",
        "GLM_API_KEY",
        "OPENROUTER_API_KEY",
        "DEEPINFRA_API_TOKEN",
        "DEEPINFRA_API_KEY",
        "SUBQ_API_KEY",
        "TYPESAFEAI_API_KEY",
        "TYPESAFE_API_KEY",
        "SYSTEM_ONE_API_KEY",
    ):
        # The error itself should not include these variables, but strip any
        # accidental echo from a custom endpoint or provider response.
        secret = os.environ.get(key_name)
        if secret:
            message = message.replace(secret, "[redacted]")
    return message[:500]


__all__ = [
    "CredentialCheck",
    "check_coding_executable",
    "check_decision_provider_access",
    "check_model_access",
    "configured_model_mapping",
]
