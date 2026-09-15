"""Small live-provider probe adapter used by opt-in integration tests.

The probe deliberately delegates provider construction to Workrr so it cannot
create a second provider or retry policy for Procedrr.
"""

from __future__ import annotations

from pathlib import Path
from typing import TextIO

from powdrr_lift.workrr.protocol import WorkflowLLMClient
from powdrr_lift.workrr.providers import (
    build_workflow_client,
    resolve_provider_credentials,
)


def build_probe_client(
    *,
    provider: str,
    model: str,
    api_key: str | None,
    base_url: str | None,
    repo_root: Path,
    progress_stream: TextIO | None = None,
) -> WorkflowLLMClient:
    """Build the same provider client used by the Workrr execution path."""
    credentials = resolve_provider_credentials(provider, api_key, base_url)
    return build_workflow_client(
        credentials,
        model=model,
        model_cache_dir=repo_root / ".powdrr" / "models",
        progress_stream=progress_stream,
    )
