"""Jev-backed typed classification for the design-interview workflow."""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from powdrr_lift.workrr.protocol import WorkflowLLMClient

LOGGER = logging.getLogger(__name__)
JEV_ENDPOINT = "https://api.typesafe.ai/v1/systemone"
JEV_MODEL = "jev-latest"
JEV_DEFAULT_BASE_URL = "https://api.typesafe.ai/v1/systemone"


class JevSemanticClassifierClient:
    """Use Jev for classifier judges, retaining planning as an error fallback."""

    def __init__(
        self,
        fallback: WorkflowLLMClient,
        *,
        api_key: str | None = None,
        endpoint: str | None = None,
        model: str | None = None,
        fail_closed: bool = False,
    ) -> None:
        self._fallback = fallback
        self._api_key = (
            api_key
            or os.environ.get("TYPESAFEAI_API_KEY")
            or os.environ.get("TYPESAFE_API_KEY")
            or os.environ.get("SYSTEM_ONE_API_KEY")
        )
        configured_base_url = os.environ.get("SYSTEM_ONE_BASE_URL")
        self._endpoint = endpoint or (
            f"{configured_base_url.rstrip('/')}/systemone"
            if configured_base_url
            else JEV_DEFAULT_BASE_URL
        )
        self._model = model or os.environ.get("SYSTEM_ONE_MODEL") or JEV_MODEL
        self._fail_closed = fail_closed

    def complete_json(
        self,
        messages: list[dict[str, str]],
        *,
        response_schema: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        request_id = uuid.uuid4().hex
        endpoint_host = urlsplit(self._endpoint).hostname or "unknown"
        request_data = _classifier_request(messages, response_schema)
        if not self._api_key:
            LOGGER.warning(
                "JEV_CALL_SKIPPED "
                "request_id=%s reason=missing_api_key endpoint_host=%s",
                request_id,
                endpoint_host,
            )
            return _fallback(self._fallback, messages, response_schema)
        if request_data is None:
            LOGGER.warning(
                "JEV_CALL_SKIPPED "
                "request_id=%s reason=unsupported_request endpoint_host=%s",
                request_id,
                endpoint_host,
            )
            return _fallback(self._fallback, messages, response_schema)

        started_at = time.monotonic()
        LOGGER.warning(
            "JEV_REQUEST_STARTED request_id=%s endpoint_host=%s model=%s",
            request_id,
            endpoint_host,
            self._model,
        )
        phase = "request"
        try:
            response = _call_jev(
                request_data, self._api_key, self._endpoint, self._model
            )
            answer = response.get("answer", response)
            returned_model = response.get("model", self._model)
            LOGGER.warning(
                "JEV_RESPONSE_RECEIVED request_id=%s endpoint_host=%s "
                "model=%s duration_ms=%d",
                request_id,
                endpoint_host,
                returned_model,
                round((time.monotonic() - started_at) * 1000),
            )
            phase = "response_mapping"
            choice = answer.get("choice")
            result = _format_classifier_result(choice, request_data, response_schema)
        except Exception as exc:  # noqa: BLE001
            # Jev is an optional classifier provider.
            http_status = getattr(exc, "code", "none")
            log_event = "JEV_FAIL_CLOSED" if self._fail_closed else "JEV_FALLBACK"
            LOGGER.warning(
                "%s request_id=%s endpoint_host=%s phase=%s "
                "error_type=%s http_status=%s duration_ms=%d",
                log_event,
                request_id,
                endpoint_host,
                phase,
                type(exc).__name__,
                http_status,
                round((time.monotonic() - started_at) * 1000),
            )
            return self._fallback_or_false(messages, response_schema)
        LOGGER.warning(
            "JEV_RESULT_ACCEPTED request_id=%s endpoint_host=%s model=%s "
            "duration_ms=%d",
            request_id,
            endpoint_host,
            returned_model,
            round((time.monotonic() - started_at) * 1000),
        )
        return result

    def _fallback_or_false(
        self,
        messages: list[dict[str, str]],
        response_schema: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        if self._fail_closed:
            output_property = _single_output_property(response_schema)
            if output_property and output_property.get("type") == "boolean":
                return {output_property["name"]: False}
            raise ValueError("Jev is unavailable for a fail-closed decision")
        return _fallback(self._fallback, messages, response_schema)


def _fallback(
    client: WorkflowLLMClient,
    messages: list[dict[str, str]],
    response_schema: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if response_schema is None:
        return client.complete_json(messages)
    try:
        return client.complete_json(messages, response_schema=response_schema)  # type: ignore[call-arg]
    except TypeError:
        return client.complete_json(messages)


def _classifier_request(
    messages: list[dict[str, str]], response_schema: Mapping[str, Any] | None
) -> dict[str, Any] | None:
    if len(messages) < 2:
        return None
    content = messages[-1].get("content", "")
    marker = "Context:\n"
    if marker not in content:
        return None
    try:
        context = json.loads(content.split(marker, 1)[1])
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(context, Mapping):
        return None
    prompt_prefix = content.split(marker, 1)[0]
    classifier = next(
        (
            value
            for value in context.values()
            if isinstance(value, Mapping)
            and "allowed_values" in value
            and (
                {"question", "instructions", "subject_text"} <= value.keys()
                or {"question", "instructions", "source_text", "candidate_field"}
                <= value.keys()
            )
        ),
        None,
    )
    output_property = _single_output_property(response_schema)
    if isinstance(classifier, Mapping):
        spec = classifier.get("spec")
        kind = spec.get("decision_kind") if isinstance(spec, Mapping) else "classifier"
        allowed = classifier.get("allowed_values")
        prompt = classifier.get("question")
        instructions = classifier.get("instructions")
        source = classifier.get("subject_text", classifier.get("source_text"))
        if isinstance(allowed, list) and isinstance(source, str):
            criteria = classifier.get("criteria")
            if criteria is not None and (
                not isinstance(criteria, Mapping)
                or set(criteria) != set(allowed)
                or not all(isinstance(value, str) for value in criteria.values())
            ):
                return None
            is_routing = kind == "routing"
            provider_state = classifier.get("provider_state")
            routing_state = (
                dict(provider_state) if isinstance(provider_state, Mapping) else None
            )
            if is_routing and routing_state is None:
                return None
            if is_routing:
                ledger = context.get("atomic_instruction_ledger")
                if isinstance(ledger, Mapping):
                    source_text = ledger.get("source_text")
                    clauses = ledger.get("clauses")
                    ledger_state: dict[str, Any] = {}
                    if isinstance(source_text, str):
                        ledger_state["source_text"] = source_text
                    if isinstance(clauses, list):
                        ledger_state["clauses"] = [
                            {
                                key: clause[key]
                                for key in (
                                    "clause_id",
                                    "ordinal",
                                    "text",
                                    "source_span",
                                    "semantic_relations",
                                    "modifier_attachments",
                                    "boolean_combination",
                                )
                                if key in clause
                            }
                            for clause in clauses
                            if isinstance(clause, Mapping)
                        ]
                    if ledger_state:
                        assert routing_state is not None
                        routing_state["instruction_ledger"] = ledger_state
            return {
                "kind": kind,
                "allowed_values": [item for item in allowed if isinstance(item, str)],
                "criteria": dict(criteria) if isinstance(criteria, Mapping) else {},
                "question": str(prompt),
                "instructions": (
                    [str(item) for item in instructions]
                    if is_routing and isinstance(instructions, list)
                    else [prompt_prefix]
                    + (
                        [str(item) for item in instructions]
                        if isinstance(instructions, list)
                        else []
                    )
                ),
                "state": routing_state
                if is_routing and routing_state is not None
                else {
                    "source_text": source,
                    "request": dict(classifier),
                    "context": context,
                },
                "output_property": output_property,
            }
    if output_property and output_property["type"] == "boolean":
        clause = next(
            (
                value
                for value in context.values()
                if isinstance(value, Mapping) and isinstance(value.get("text"), str)
            ),
            None,
        )
        if clause is None:
            return None
        prompt_prefix, _, context_suffix = content.partition(marker)
        return {
            "kind": "boolean_classifier",
            "allowed_values": ["true", "false"],
            "question": _question_from_prompt(prompt_prefix),
            "instructions": [prompt_prefix],
            "state": {"source_text": clause["text"], "context": context_suffix},
            "output_property": output_property,
        }
    if output_property and output_property.get("type") == "string":
        allowed = output_property.get("enum")
        if (
            isinstance(allowed, list)
            and allowed
            and all(isinstance(value, str) for value in allowed)
        ):
            return {
                "kind": "enum_classifier",
                "allowed_values": allowed,
                "question": _question_from_prompt(prompt_prefix),
                "instructions": [prompt_prefix],
                "state": {"context": context},
                "output_property": output_property,
            }
    return None


def _single_output_property(
    schema: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    if schema is None:
        return None
    properties = schema.get("properties")
    required = schema.get("required")
    if not isinstance(properties, Mapping) or not isinstance(required, list):
        return None
    if len(required) == 1 and required[0] in properties:
        name = required[0]
        property_schema = properties[name]
        if isinstance(property_schema, Mapping):
            return {
                "name": name,
                "type": property_schema.get("type"),
                **property_schema,
            }
    value_schema = properties.get("value")
    if isinstance(value_schema, Mapping) and isinstance(value_schema.get("enum"), list):
        return {"name": "value", **value_schema}
    return None


def _question_from_prompt(prompt: str) -> str:
    question_marker = "Question:\n"
    if question_marker in prompt:
        return prompt.split(question_marker, 1)[1].strip()
    return prompt.strip()


def _format_classifier_result(
    choice: Any,
    classifier: Mapping[str, Any],
    response_schema: Mapping[str, Any] | None,
) -> dict[str, Any]:
    allowed = classifier["allowed_values"]
    output_property = classifier["output_property"]
    if output_property and output_property.get("type") == "boolean":
        if choice in {"true", "false"}:
            return {output_property["name"]: choice == "true"}
        raise ValueError("Jev returned an unsupported boolean classification")
    if choice == "unresolved":
        if output_property and output_property["name"] != "value":
            raise ValueError("Jev abstained on a single-field classification")
        return {
            "status": "unresolved",
            "value": None,
            "reason_code": "classifier_abstained",
        }
    if choice not in allowed:
        raise ValueError("Jev returned an unsupported classifier value")
    if response_schema is not None and {"status", "value", "reason_code"} <= set(
        response_schema.get("properties", {})
    ):
        return {"status": "resolved", "value": choice, "reason_code": None}
    if output_property:
        return {output_property["name"]: choice}
    raise ValueError("Jev classifier output does not match the declared schema")


def _call_jev(
    classifier: Mapping[str, Any], api_key: str, endpoint: str, model: str = JEV_MODEL
) -> Mapping[str, Any]:
    labels = classifier["allowed_values"]
    instructions = classifier["instructions"]
    body = {
        "model": model,
        "state": classifier["state"],
        "questions": {
            "decision": {
                "type": "choice",
                "instructions": str(classifier["question"])
                + "\n\n"
                + "\n".join(f"- {item}" for item in instructions)
                + "\nChoose unresolved if the proposition does not support a label.",
                "criteria": {
                    label: classifier.get("criteria", {}).get(label) for label in labels
                }
                | {
                    "unresolved": "The proposition does not support a defensible label."
                },
            }
        },
    }
    request = Request(
        endpoint,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urlopen(request, timeout=10) as response:
        result = json.loads(response.read().decode("utf-8"))
    answer = result.get("answers", {}).get("decision")
    if not isinstance(answer, Mapping):
        raise ValueError("Jev response has no decision answer")
    return {
        "answer": answer,
        "model": result.get("model", model),
        "usage": result.get("usage"),
    }
