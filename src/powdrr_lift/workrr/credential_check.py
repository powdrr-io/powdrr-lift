"""Preflight configured model credentials and provider access."""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from powdrr_lift.errors import PowdrrExecutionError
from powdrr_lift.workrr.llm import WorkflowLLMHTTPError
from powdrr_lift.workrr.provider_config import (
    LLMModelMapping,
    default_llm_mappings,
)
from powdrr_lift.workrr.providers import (
    ProviderCredentials,
    build_workflow_client,
    resolve_provider_credentials,
)

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
    except (PowdrrExecutionError, OSError, TimeoutError) as error:
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


def check_typesafeai_access() -> CredentialCheck:
    """Make one authenticated Jev request using the configured TypesafeAI key."""
    api_key = (
        os.environ.get("TYPESAFEAI_API_KEY")
        or os.environ.get("TYPESAFE_API_KEY")
        or os.environ.get("SYSTEM_ONE_API_KEY")
    )
    if not api_key:
        return CredentialCheck(
            role="classifier",
            provider="typesafeai",
            model=os.environ.get("SYSTEM_ONE_MODEL") or "jev-latest",
            status="credentials_missing",
            message=(
                "No TypesafeAI credentials found. Set TYPESAFEAI_API_KEY, "
                "TYPESAFE_API_KEY, or SYSTEM_ONE_API_KEY."
            ),
        )

    model = os.environ.get("SYSTEM_ONE_MODEL") or "jev-latest"
    endpoint = os.environ.get("SYSTEM_ONE_BASE_URL", "https://api.typesafe.ai/v1")
    endpoint = endpoint.rstrip("/") + "/systemone"
    body = {
        "model": model,
        "state": {"source_text": "credential preflight"},
        "questions": {
            "decision": {
                "type": "choice",
                "instructions": "Choose the valid value.",
                "criteria": {
                    "ok": "The credential preflight request is valid.",
                    "unresolved": "The request cannot be classified.",
                },
            }
        },
    }
    request = Request(
        endpoint,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=10) as response:
            response_data = json.loads(response.read().decode("utf-8"))
        if not isinstance(response_data, Mapping) or not isinstance(
            response_data.get("answers", {}).get("decision"), Mapping
        ):
            return CredentialCheck(
                role="classifier",
                provider="typesafeai",
                model=model,
                status="provider_rejected_request",
                credential_source=_typesafeai_credential_source(),
                message="Jev returned a response without a decision answer.",
            )
    except HTTPError as error:
        status = {
            401: "authentication_failed",
            403: "model_access_denied",
            429: "rate_or_quota_limited",
        }.get(error.code, "provider_rejected_request")
        return CredentialCheck(
            role="classifier",
            provider="typesafeai",
            model=model,
            status=status,
            credential_source=_typesafeai_credential_source(),
            message=f"Provider returned HTTP {error.code}.",
        )
    except (OSError, TimeoutError, URLError, ValueError) as error:
        return CredentialCheck(
            role="classifier",
            provider="typesafeai",
            model=model,
            status="unavailable",
            credential_source=_typesafeai_credential_source(),
            message=f"TypesafeAI request failed ({type(error).__name__}).",
        )

    return CredentialCheck(
        role="classifier",
        provider="typesafeai",
        model=model,
        status="passed",
        credential_source=_typesafeai_credential_source(),
    )


def _typesafeai_credential_source() -> str:
    for name in ("TYPESAFEAI_API_KEY", "TYPESAFE_API_KEY", "SYSTEM_ONE_API_KEY"):
        if os.environ.get(name):
            return name
    return "unknown"


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
    "check_model_access",
    "check_typesafeai_access",
    "configured_model_mapping",
]
