from __future__ import annotations

import os
from pathlib import Path
from typing import cast

import pytest

from powdrr_lift.workrr.protocol import SchemaAwareWorkflowLLMClient
from powdrr_lift.workrr.providers import (
    build_workflow_client,
    resolve_provider_credentials,
)


@pytest.mark.live_provider
def test_live_deepinfra_planning_provider_smoke(tmp_path: Path) -> None:
    """Verify the pinned DeepInfra planning route when explicitly requested."""
    if os.environ.get("POWDRR_LIFT_RUN_LIVE_PROVIDER") != "1":
        pytest.skip("set POWDRR_LIFT_RUN_LIVE_PROVIDER=1 to run the live test")
    api_key = os.environ.get("DEEPINFRA_API_KEY") or os.environ.get(
        "DEEPINFRA_API_TOKEN"
    )
    if not api_key:
        pytest.skip("set DEEPINFRA_API_KEY or DEEPINFRA_API_TOKEN to run the live test")

    credentials = resolve_provider_credentials(
        "deepinfra", api_key, os.environ.get("DEEPINFRA_BASE_URL")
    )
    client = build_workflow_client(
        credentials,
        model="deepseek-ai/DeepSeek-V4-Flash-0731",
        model_cache_dir=tmp_path / "models",
    )
    response = cast(SchemaAwareWorkflowLLMClient, client).complete_json(
        [{"role": "user", "content": "Return the JSON object requested."}],
        response_schema={
            "type": "object",
            "properties": {"ok": {"type": "boolean"}},
            "required": ["ok"],
            "additionalProperties": False,
        },
    )
    assert isinstance(response.get("ok"), bool)
