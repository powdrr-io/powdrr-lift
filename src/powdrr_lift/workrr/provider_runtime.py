"""Provider/client services shared by workflow execution adapters.

The workflow agents decide what to ask a provider and when to switch models.
This module owns the mechanics of resolving credentials, constructing clients,
and optionally recording exchanges, so those mechanics are not part of an
interactive agent implementation.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO

from powdrr_service.providers.clients import (
    ProviderClient,
)
from powdrr_service.providers.clients import (
    build_provider_client as _build_service_provider_client,
)
from powdrr_service.providers.config import LLMModelLimits, provider_definition
from powdrr_service.providers.credentials import (
    ProviderCredentials,
    resolve_provider_credentials,
)
from powdrr_service.providers.errors import (
    ProviderClientError,
    ProviderHTTPError,
    ProviderResponseError,
)
from powdrr_service.providers.selection import (
    provider_model_limits,
    resolve_provider_roles,
)

from powdrr_lift.errors import PowdrrExecutionError, ProviderExecutionError
from powdrr_lift.workrr.exchanges import ExchangeRecordingClient
from powdrr_lift.workrr.llm import (
    WorkflowLLMClient,
    WorkflowLLMHTTPError,
    complete_json,
)
from powdrr_lift.workrr.local_model import (
    resolve_local_model_context,
    resolve_local_model_path,
)

ENABLE_LLM_EXCHANGE_LOGGING = False
_DEFAULT_LOCAL_MODEL_CONTEXT = 24576


class WorkflowProviderClientAdapter:
    """Translate service provider errors into the workflow error contract."""

    def __init__(self, client: ProviderClient) -> None:
        self._client = client

    def complete_json(
        self,
        messages: list[dict[str, str]],
        *,
        response_schema: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            return self._client.complete_json(
                messages,
                response_schema=response_schema,
            )
        except ProviderHTTPError as error:
            raise WorkflowLLMHTTPError(
                error.provider,
                error.status_code,
                str(error).partition(": ")[2],
            ) from error
        except ProviderResponseError as error:
            raise PowdrrExecutionError(
                str(error),
                error_code=error.error_code,
            ) from error
        except ProviderClientError as error:
            raise ProviderExecutionError(str(error)) from error

    def __getattr__(self, name: str) -> object:
        return getattr(self._client, name)


def build_provider_client(
    *,
    provider: str,
    model: str,
    api_key: str,
    base_url: str,
    local_model_path: Path | None = None,
    local_context: int = _DEFAULT_LOCAL_MODEL_CONTEXT,
    progress_stream: TextIO | None = None,
    timeout: float = 120.0,
) -> WorkflowLLMClient:
    client = _build_service_provider_client(
        provider=provider,
        model=model,
        api_key=api_key,
        base_url=base_url,
        local_model_path=local_model_path,
        local_context=local_context,
        progress_stream=progress_stream,
        timeout=timeout,
    )
    return WorkflowProviderClientAdapter(client)


def build_workflow_client(
    credentials: ProviderCredentials,
    *,
    model: str,
    model_cache_dir: Path,
    progress_stream: TextIO | None = None,
    timeout: float = 120.0,
) -> WorkflowLLMClient:
    """Compose a service provider client with local workflow error semantics."""
    provider = provider_definition(credentials.provider)
    return build_provider_client(
        provider=credentials.provider,
        model=model,
        api_key=credentials.api_key,
        base_url=credentials.base_url,
        local_model_path=(
            resolve_local_model_path(model_cache_dir)
            if provider.client_kind == "local"
            else None
        ),
        local_context=resolve_local_model_context(),
        timeout=timeout,
        progress_stream=progress_stream,
    )


def model_limits_for(provider: str, model: str) -> LLMModelLimits:
    return provider_model_limits(
        provider,
        model,
        local_context=resolve_local_model_context(),
    )


def resolve_workflow_provider(
    provider: str = "auto",
    *,
    normal_provider: str | None = None,
) -> str:
    """Resolve the normal provider using workflow provider policy."""
    return resolve_provider_roles(
        provider,
        normal_provider=normal_provider,
    ).normal


def maybe_record_llm_exchanges(
    client: WorkflowLLMClient,
    repo_root: Path,
) -> WorkflowLLMClient:
    """Apply exchange recording only while diagnostic logging is enabled."""
    if not ENABLE_LLM_EXCHANGE_LOGGING:
        return client
    return ExchangeRecordingClient(client, repo_root, request_json=complete_json)


@dataclass(slots=True)
class WorkflowClientRegistry:
    """Cache provider clients by provider/model within one workflow run."""

    api_key: str | None
    base_url: str | None
    project_root: Path
    progress_stream: TextIO
    clients: dict[tuple[str, str], WorkflowLLMClient] = field(default_factory=dict)

    def credentials_for(self, provider: str) -> ProviderCredentials:
        return resolve_provider_credentials(provider, self.api_key, self.base_url)

    def client_for(
        self,
        model: str,
        provider: str,
        *,
        credentials: ProviderCredentials | None = None,
    ) -> WorkflowLLMClient:
        key = (provider, model)
        client = self.clients.get(key)
        if client is None:
            client = build_workflow_client(
                credentials or self.credentials_for(provider),
                model=model,
                model_cache_dir=self.project_root / ".powdrr" / "models",
                progress_stream=self.progress_stream,
            )
            client = maybe_record_llm_exchanges(client, self.project_root)
            self.clients[key] = client
        return client
