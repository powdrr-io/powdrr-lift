from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest
from powdrr_service.providers.errors import (
    ProviderClientError,
    ProviderHTTPError,
    ProviderResponseError,
)

from powdrr_lift.errors import PowdrrExecutionError, ProviderExecutionError
from powdrr_lift.workrr.llm import WorkflowLLMHTTPError
from powdrr_lift.workrr.provider_runtime import WorkflowProviderClientAdapter


class _FailingProviderClient:
    def __init__(self, error: RuntimeError) -> None:
        self.error = error

    def complete_json(
        self,
        messages: list[dict[str, str]],
        *,
        response_schema: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        del messages, response_schema
        raise self.error


def test_provider_http_errors_keep_workflow_http_status() -> None:
    client = WorkflowProviderClientAdapter(
        _FailingProviderClient(ProviderHTTPError("DeepInfra", 429, "rate limited"))
    )

    with pytest.raises(WorkflowLLMHTTPError) as raised:
        client.complete_json([{"role": "user", "content": "hello"}])

    assert raised.value.status_code == 429


def test_provider_response_errors_keep_agent_correction_classification() -> None:
    client = WorkflowProviderClientAdapter(
        _FailingProviderClient(
            ProviderResponseError("invalid JSON", error_code="invalid_json_response")
        )
    )

    with pytest.raises(PowdrrExecutionError) as raised:
        client.complete_json([{"role": "user", "content": "hello"}])

    assert raised.value.error_code == "invalid_json_response"


def test_provider_transport_errors_keep_noncorrectable_classification() -> None:
    client = WorkflowProviderClientAdapter(
        _FailingProviderClient(ProviderClientError("connection dropped"))
    )

    with pytest.raises(ProviderExecutionError, match="connection dropped"):
        client.complete_json([{"role": "user", "content": "hello"}])
