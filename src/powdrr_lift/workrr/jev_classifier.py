"""Conservative Jev routing for selected semantic source classifiers."""

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
JEV_MIN_CONFIDENCE = 0.98
JEV_DECISION_KINDS = frozenset({"has_exception", "temporal_scope"})


class JevSemanticClassifierClient:
    """Use Jev for high-confidence exception/scope labels; fall back otherwise."""

    def __init__(
        self,
        fallback: WorkflowLLMClient,
        *,
        api_key: str | None = None,
        confidence_threshold: float = JEV_MIN_CONFIDENCE,
    ) -> None:
        self._fallback = fallback
        self._api_key = (
            api_key
            or os.environ.get("TYPESAFEAI_API_KEY")
            or os.environ.get("TYPESAFE_API_KEY")
        )
        self._confidence_threshold = confidence_threshold

    def complete_json(
        self,
        messages: list[dict[str, str]],
        *,
        response_schema: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        request_data = _classifier_request(messages)
        if not self._api_key or request_data is None:
            return _fallback(self._fallback, messages, response_schema)
        kind, classifier = request_data
        if kind not in JEV_DECISION_KINDS:
            return _fallback(self._fallback, messages, response_schema)
        try:
            answer = _call_jev(classifier, self._api_key)
            choice = answer.get("choice")
            confidence = answer.get("confidence")
            allowed = classifier["allowed_values"]
            if (
                choice in allowed
                and isinstance(confidence, (int, float))
                and confidence >= self._confidence_threshold
            ):
                LOGGER.info("Jev resolved %s (confidence %.3f)", kind, confidence)
                return {"status": "resolved", "value": choice, "reason_code": None}
            LOGGER.info(
                "Jev abstained on %s; using configured classifier fallback", kind
            )
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
    messages: list[dict[str, str]],
) -> tuple[str, dict[str, Any]] | None:
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
    classifier = next(
        (
            value
            for value in context.values()
            if isinstance(value, Mapping)
            and {"spec", "question", "instructions", "allowed_values", "subject_text"}
            <= value.keys()
        ),
        None,
    )
    if not isinstance(classifier, Mapping):
        return None
    spec = classifier.get("spec")
    kind = spec.get("decision_kind") if isinstance(spec, Mapping) else None
    allowed = classifier.get("allowed_values")
    if not isinstance(kind, str) or not isinstance(allowed, list):
        return None
    request = dict(classifier)
    request["allowed_values"] = [value for value in allowed if isinstance(value, str)]
    return kind, request


def _call_jev(classifier: Mapping[str, Any], api_key: str) -> Mapping[str, Any]:
    labels = classifier["allowed_values"]
    instructions = classifier["instructions"]
    body = {
        "model": JEV_MODEL,
        "state": {"proposition": classifier["subject_text"]},
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
        JEV_ENDPOINT,
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
