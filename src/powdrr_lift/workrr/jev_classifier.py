"""Jev-backed typed classification for the design-interview workflow."""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping
from typing import Any
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

    def complete_json(
        self,
        messages: list[dict[str, str]],
        *,
        response_schema: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        request_data = _classifier_request(messages, response_schema)
        if not self._api_key or request_data is None:
            return _fallback(self._fallback, messages, response_schema)
        try:
            answer = _call_jev(request_data, self._api_key, self._endpoint)
            choice = answer.get("choice")
            return _format_classifier_result(choice, request_data, response_schema)
        except Exception:  # noqa: BLE001 - Jev is an optional classifier provider.
            LOGGER.warning(
                "Jev request failed; using configured classifier fallback",
                exc_info=True,
            )
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
            return {
                "kind": kind,
                "allowed_values": [item for item in allowed if isinstance(item, str)],
                "question": str(prompt),
                "instructions": [prompt_prefix]
                + (
                    [str(item) for item in instructions]
                    if isinstance(instructions, list)
                    else []
                ),
                "state": {
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
    classifier: Mapping[str, Any], api_key: str, endpoint: str
) -> Mapping[str, Any]:
    labels = classifier["allowed_values"]
    instructions = classifier["instructions"]
    body = {
        "model": JEV_MODEL,
        "state": classifier["state"],
        "questions": {
            "decision": {
                "type": "choice",
                "instructions": str(classifier["question"])
                + "\n\n"
                + "\n".join(f"- {item}" for item in instructions)
                + "\nChoose unresolved if the proposition does not support a label.",
                "criteria": {label: None for label in labels}
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
    return answer
