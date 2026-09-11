"""Durable recording of agent/provider exchanges."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from powdrr_lift.agent.protocol import (
    SchemaAwareWorkflowLLMClient,
    WorkflowLLMClient,
)


def normalize_cache_usage(usage: Mapping[str, Any]) -> dict[str, int] | None:
    """Normalize cache counters returned by OpenAI-compatible providers."""

    prompt_tokens = usage.get("prompt_tokens")
    prompt_details = usage.get("prompt_tokens_details")
    if not isinstance(prompt_details, Mapping):
        prompt_details = {}
    cached_tokens = prompt_details.get("cached_tokens")
    if not isinstance(cached_tokens, int):
        cached_tokens = usage.get("prompt_cache_hit_tokens")
    if not isinstance(cached_tokens, int):
        cached_tokens = 0
    cache_miss_tokens = usage.get("prompt_cache_miss_tokens")
    if not isinstance(cache_miss_tokens, int):
        cache_miss_tokens = (
            prompt_tokens - cached_tokens if isinstance(prompt_tokens, int) else 0
        )
    cache_write_tokens = prompt_details.get("cache_write_tokens")
    if not isinstance(cache_write_tokens, int):
        cache_write_tokens = usage.get("cache_write_tokens")
    if not isinstance(cache_write_tokens, int):
        cache_write_tokens = 0
    if not any((cached_tokens, cache_miss_tokens, cache_write_tokens)):
        return None
    return {
        "prompt_tokens": prompt_tokens if isinstance(prompt_tokens, int) else 0,
        "cached_tokens": cached_tokens,
        "cache_miss_tokens": cache_miss_tokens,
        "cache_write_tokens": cache_write_tokens,
    }


def serialize_exchange(
    exchange: Mapping[str, Any],
    *,
    serialized_messages: str,
) -> str:
    """Serialize an exchange without encoding the large input twice."""

    lines = [
        "{",
        f'  "timestamp": {json.dumps(exchange["timestamp"], ensure_ascii=False)},',
        f'  "input": {serialized_messages},',
        '  "output": ' + json.dumps(exchange["output"], indent=2, ensure_ascii=False),
    ]
    if "usage" in exchange:
        lines[-1] += ","
        lines.append(
            '  "usage": ' + json.dumps(exchange["usage"], indent=2, ensure_ascii=False)
        )
    lines.append("}")
    return "\n".join(lines) + "\n"


def serialize_messages(messages: list[dict[str, str]]) -> str:
    return json.dumps(messages, ensure_ascii=False, separators=(",", ":"))


class ExchangeRecordingClient:
    """Record every request and response in the active repository root."""

    def __init__(
        self,
        client: WorkflowLLMClient,
        repo_root: Path,
        *,
        request_json: Callable[..., dict[str, Any]] | None = None,
    ) -> None:
        self._client = client
        self._repo_root = repo_root.expanduser().resolve()
        self._request_json = request_json

    def complete_json(
        self,
        messages: list[dict[str, str]],
        *,
        response_schema: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            if self._request_json is None:
                if response_schema is None:
                    response = self._client.complete_json(messages)
                else:
                    complete = cast(
                        SchemaAwareWorkflowLLMClient, self._client
                    ).complete_json
                    response = complete(messages, response_schema=response_schema)
            else:
                response = self._request_json(
                    self._client, messages, response_schema=response_schema
                )
        except Exception as exc:
            self._write_exchange(
                messages,
                {"error": str(exc), "error_type": type(exc).__name__},
            )
            raise
        self._write_exchange(messages, response)
        return response

    def _write_exchange(self, messages: list[dict[str, str]], output: object) -> None:
        timestamp = datetime.now(UTC)
        timestamp_text = timestamp.strftime("%Y%m%d-%H%M%S-%f")
        output_path = self._repo_root / f"llm-{timestamp_text}.json"
        suffix = 1
        while output_path.exists():
            output_path = self._repo_root / f"llm-{timestamp_text}-{suffix}.json"
            suffix += 1
        exchange: dict[str, Any] = {
            "timestamp": timestamp.isoformat(),
            "output": output,
        }
        usage = getattr(self._client, "last_usage", None)
        if isinstance(usage, Mapping):
            cache_usage = normalize_cache_usage(usage)
            if cache_usage is not None:
                exchange["usage"] = cache_usage
        serialized_messages = getattr(self._client, "last_serialized_messages", None)
        if not isinstance(serialized_messages, str):
            serialized_messages = serialize_messages(messages)
        output_path.write_text(
            serialize_exchange(exchange, serialized_messages=serialized_messages),
            encoding="utf-8",
        )
