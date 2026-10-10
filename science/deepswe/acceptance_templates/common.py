"""Input boundaries, source anchors, schemas, and checkpointed model calls."""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from typing import Any

from powdrr_lift.workrr.llm import complete_json

from . import PROMPT_VERSION, SCHEMA_VERSION

HERE = Path(__file__).resolve().parent


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected an object: {path}")
    return value


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")
    temporary.replace(path)


def source_spans(instruction: str) -> list[dict[str, Any]]:
    """Stable nonempty line anchors; semantic splitting remains a model decision."""
    result: list[dict[str, Any]] = []
    offset = 0
    for line in instruction.splitlines(keepends=True):
        text = line.rstrip("\r\n")
        if text.strip():
            result.append(
                {
                    "id": f"s{len(result) + 1:03}",
                    "text": text,
                    "start": offset,
                    "end": offset + len(text),
                }
            )
        offset += len(line)
    return result


def generation_input(path: Path) -> dict[str, Any]:
    """Construct an allowlisted payload even if an input file has extra fields."""
    raw = load_json(path)
    instruction = raw["instruction"]
    if not isinstance(instruction, str) or not instruction.strip():
        raise ValueError("input requires a nonempty instruction")
    return {
        "task_id": raw["task_id"],
        "instruction": instruction,
        "source_spans": source_spans(instruction),
    }


def object_schema(**properties: Any) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        "required": list(properties),
    }


def array_schema(items: dict[str, Any]) -> dict[str, Any]:
    return {"type": "array", "items": items}


TEXT = {"type": "string"}
IDS = array_schema(TEXT)


def choice_schema(*choices: str) -> dict[str, Any]:
    return {"type": "string", "enum": list(choices)}


def require_text(row: dict[str, Any], key: str) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a nonempty string")
    return value.strip()


def checked_ids(value: Any, allowed: set[str], label: str) -> list[str]:
    if (
        not isinstance(value, list)
        or any(not isinstance(item, str) or item not in allowed for item in value)
        or len(set(value)) != len(value)
    ):
        raise ValueError(f"{label}: unknown or repeated IDs")
    return value


def checked_rows(response: dict[str, Any], field: str) -> list[dict[str, Any]]:
    rows = response.get(field)
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError(f"{field} must be an array of objects")
    return rows


class Recorder:
    """Persist each request before calling, then response, usage, timing, and failure.

    There are no hidden retries. Optional repair attempts are explicit artifacts.
    A checkpoint is reusable only when the full request fingerprint matches.
    """

    def __init__(
        self,
        client: Any,
        directory: Path,
        *,
        provider: str,
        model: str,
        resume: bool = False,
        repairs: int = 1,
    ) -> None:
        self.client = client
        self.directory = directory
        self.provider = provider
        self.model = model
        self.resume = resume
        self.repairs = repairs
        self.calls: list[dict[str, Any]] = []

    def call(
        self,
        stage: str,
        prompt: str,
        payload: dict[str, Any],
        schema: dict[str, Any],
        validator: Callable[[dict[str, Any]], Any],
    ) -> Any:
        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ]
        for attempt in range(self.repairs + 1):
            identity = {
                "prompt_version": PROMPT_VERSION,
                "provider": self.provider,
                "model": self.model,
                "messages": messages,
                "schema": schema,
            }
            fingerprint = digest(identity)
            path = self.directory / f"{stage}-{attempt}.json"
            if self.resume and path.exists():
                prior = load_json(path)
                if prior.get("request_sha256") != fingerprint:
                    raise ValueError(
                        f"stale checkpoint for {stage}; use a new output dir"
                    )
                if prior.get("status") == "completed":
                    value = validator(deepcopy(prior["response"]))
                    self._record_cached(stage, attempt, prior, identity)
                    return value
                if isinstance(prior.get("response"), dict):
                    # Resume a completed schema-repair transcript rather than
                    # calling the failed first attempt again on every resume.
                    try:
                        value = validator(deepcopy(prior["response"]))
                    except ValueError as exc:
                        self.calls.append({**prior["receipt"], "cached": True})
                        if attempt == self.repairs:
                            raise RuntimeError(
                                f"{stage} exhausted its recorded repair budget; "
                                f"see {path}"
                            ) from None
                        detail = prior.get("validation_error", str(exc))
                        messages += [
                            {
                                "role": "assistant",
                                "content": json.dumps(prior["response"]),
                            },
                            {
                                "role": "user",
                                "content": "Correct this JSON without inventing "
                                f"source facts. Local validation failed: {detail}",
                            },
                        ]
                        continue
                    self._record_cached(stage, attempt, prior, identity)
                    return value
                if prior.get("status") == "failed":
                    self.calls.append({**prior["receipt"], "cached": True})
                    if attempt == self.repairs:
                        raise RuntimeError(
                            f"{stage} exhausted its recorded attempt budget; see {path}"
                        ) from None
                    # A transport failure has no response to repair. Explicit
                    # resume consumes the next persisted attempt, preserving
                    # the original failure and the configured total budget.
                    continue
            record: dict[str, Any] = {
                "schema_version": SCHEMA_VERSION,
                "stage": stage,
                "attempt": attempt,
                "request_sha256": fingerprint,
                "request": identity,
                "status": "running",
            }
            write_json(path, record)
            label = "/".join(self.directory.parts[-3:])
            if self.directory.parent.name == "review-calls":
                label = (
                    "/".join(self.directory.parts[-4:-1])
                    + "/"
                    + self.directory.name[:10]
                )
            print(f"{label}: {stage} attempt={attempt}", flush=True)
            started = time.monotonic()
            response = None
            if hasattr(self.client, "last_usage"):
                self.client.last_usage = {}
            try:
                response = complete_json(self.client, messages, response_schema=schema)
                record["response"] = response
                value = validator(deepcopy(response))
            except Exception as exc:
                receipt = self._receipt(stage, attempt, started, "failed")
                record.update(
                    status="failed", error_type=type(exc).__name__, receipt=receipt
                )
                detail = str(exc)
                for key, secret in os.environ.items():
                    if secret and any(
                        word in key.upper()
                        for word in ("TOKEN", "API_KEY", "SECRET", "PASSWORD")
                    ):
                        detail = detail.replace(secret, "[redacted]")
                record["error_detail"] = detail
                if isinstance(exc, ValueError):
                    record["validation_error"] = detail
                write_json(path, record)
                self.calls.append(receipt)
                if response is None or attempt == self.repairs:
                    raise RuntimeError(
                        f"{stage} failed ({type(exc).__name__}); see {path}"
                    ) from None
                messages += [
                    {"role": "assistant", "content": json.dumps(response)},
                    {
                        "role": "user",
                        "content": "Correct this JSON without inventing "
                        f"source facts. Local validation failed: {exc}",
                    },
                ]
                continue
            receipt = self._receipt(stage, attempt, started, "completed")
            record.update(status="completed", receipt=receipt)
            write_json(path, record)
            self.calls.append(receipt)
            return value
        raise AssertionError("unreachable repair loop")

    def _record_cached(
        self, stage: str, attempt: int, prior: dict[str, Any], identity: dict[str, Any]
    ) -> None:
        self.calls.append({**prior["receipt"], "cached": True})
        # Formatting alignment can make a formerly rejected response usable.
        # Later attempts already made for it still count toward actual cost.
        for later_attempt in range(attempt + 1, self.repairs + 1):
            path = self.directory / f"{stage}-{later_attempt}.json"
            if not path.exists():
                continue
            later = load_json(path)
            request = later.get("request", {})
            if (
                request.get("messages", [])[:2] == identity["messages"][:2]
                and request.get("schema") == identity["schema"]
                and request.get("model") == self.model
                and request.get("provider") == self.provider
                and "receipt" in later
            ):
                self.calls.append(
                    {**later["receipt"], "cached": True, "superseded": True}
                )

    def _receipt(
        self, stage: str, attempt: int, started: float, status: str
    ) -> dict[str, Any]:
        usage = getattr(self.client, "last_usage", {})
        return {
            "stage": stage,
            "attempt": attempt,
            "status": status,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "provider": self.provider,
            "model": self.model,
            "usage": dict(usage),
            "cached": False,
        }


def normalized(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()
