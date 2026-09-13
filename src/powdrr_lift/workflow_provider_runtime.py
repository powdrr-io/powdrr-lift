"""Provider/client services shared by workflow execution adapters.

The workflow agents decide what to ask a provider and when to switch models.
This module owns the mechanics of resolving credentials, constructing clients,
and optionally recording exchanges, so those mechanics are not part of an
interactive agent implementation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO

from powdrr_lift.workflow_llm import WorkflowLLMClient, complete_json
from powdrr_lift.workrr.exchanges import ExchangeRecordingClient
from powdrr_lift.workrr.provider_config import LLMModelLimits
from powdrr_lift.workrr.providers import (
    ProviderCredentials,
    build_workflow_client,
    provider_model_limits,
    resolve_local_model_context,
    resolve_provider_credentials,
    resolve_provider_roles,
)

ENABLE_LLM_EXCHANGE_LOGGING = False


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
