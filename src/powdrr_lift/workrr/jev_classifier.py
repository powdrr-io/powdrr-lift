"""Typed decision API classification for the design-interview workflow."""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from collections.abc import Mapping
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from powdrr_lift.workrr.protocol import WorkflowLLMClient

LOGGER = logging.getLogger(__name__)
JEV_ENDPOINT = "https://api.typesafe.ai/v1/systemone"
JEV_MODEL = "jev-latest"
JEV_DEFAULT_BASE_URL = "https://api.typesafe.ai/v1/systemone"
SUBQ_MODEL = "subq-snap-preview"
SUBQ_DEFAULT_BASE_URL = "https://api.subq.ai/v1/systemone"


@dataclass(slots=True)
class _PendingDecision:
    result_index: int
    request_id: str
    request_data: Mapping[str, Any]
    messages: list[dict[str, str]]
    response_schema: Mapping[str, Any] | None
    started_at: float
    future: Future[dict[str, Any]]


class _DecisionApiSemanticClassifierClient:
    """Common typed-decision API adapter with a planning fallback."""

    provider = "DECISION_API"
    default_model = ""
    default_endpoint = ""

    def __init__(
        self,
        fallback: WorkflowLLMClient,
        *,
        api_key: str | None = None,
        endpoint: str | None = None,
        model: str | None = None,
        fail_closed: bool = False,
        _api_key: str | None = None,
        _endpoint_env: str = "",
        _model_env: str = "",
    ) -> None:
        self._fallback = fallback
        self._api_key = api_key or _api_key
        configured_base_url = os.environ.get(_endpoint_env) if _endpoint_env else None
        self._endpoint = endpoint or (
            f"{configured_base_url.rstrip('/')}/systemone"
            if configured_base_url
            else self.default_endpoint
        )
        self._model = (
            model
            or (os.environ.get(_model_env) if _model_env else None)
            or self.default_model
        )
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
                "%s_CALL_SKIPPED request_id=%s reason=missing_api_key endpoint_host=%s",
                self.provider,
                request_id,
                endpoint_host,
            )
            return _fallback(self._fallback, messages, response_schema)
        if request_data is None:
            LOGGER.warning(
                "%s_CALL_SKIPPED "
                "request_id=%s reason=unsupported_request endpoint_host=%s",
                self.provider,
                request_id,
                endpoint_host,
            )
            return _fallback(self._fallback, messages, response_schema)

        started_at = time.monotonic()
        LOGGER.warning(
            "%s_REQUEST_STARTED request_id=%s endpoint_host=%s model=%s",
            self.provider,
            request_id,
            endpoint_host,
            self._model,
        )
        phase = "request"
        try:
            response = self._call(request_data, self._api_key, self._endpoint)
            answer = response.get("answer", response)
            returned_model = response.get("model", self._model)
            LOGGER.warning(
                "%s_RESPONSE_RECEIVED request_id=%s endpoint_host=%s "
                "model=%s duration_ms=%d",
                self.provider,
                request_id,
                endpoint_host,
                returned_model,
                round((time.monotonic() - started_at) * 1000),
            )
            phase = "response_mapping"
            choice = answer.get("choice")
            result = _format_classifier_result(choice, request_data, response_schema)
        except Exception as exc:  # noqa: BLE001
            # Decision APIs are optional classifier providers.
            http_status = getattr(exc, "code", "none")
            log_event = (
                f"{self.provider}_FAIL_CLOSED"
                if self._fail_closed
                else f"{self.provider}_FALLBACK"
            )
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
            "%s_RESULT_ACCEPTED request_id=%s endpoint_host=%s model=%s duration_ms=%d",
            self.provider,
            request_id,
            endpoint_host,
            returned_model,
            round((time.monotonic() - started_at) * 1000),
        )
        return result

    def _execute_batch(self, batch: list[_PendingDecision]) -> None:
        batch_id = uuid.uuid4().hex
        endpoint_host = urlsplit(self._endpoint).hostname or "unknown"
        started_at = min(item.started_at for item in batch)
        response: Mapping[str, Any] | None = None
        phase = "request"
        try:
            if len(batch) > 1:
                LOGGER.warning(
                    "%s_BATCH_REQUEST_STARTED batch_id=%s batch_size=%d "
                    "endpoint_host=%s model=%s",
                    self.provider,
                    batch_id,
                    len(batch),
                    endpoint_host,
                    self._model,
                )
            if len(batch) == 1:
                item = batch[0]
                single_response = self._call(
                    item.request_data, self._api_key or "", self._endpoint
                )
                answer = single_response.get("answer", single_response)
                response = {
                    "answers": {item.request_id: answer},
                    "model": single_response.get("model", self._model),
                }
            else:
                response = self._call_batch(batch, self._api_key or "", self._endpoint)
            phase = "response_mapping"
            returned_model = response.get("model", self._model)
            answers = response.get("answers")
            if not isinstance(answers, Mapping):
                raise ValueError("Decision API batch response has no answers object")
            LOGGER.warning(
                "%s_BATCH_RESPONSE_RECEIVED batch_id=%s batch_size=%d "
                "endpoint_host=%s model=%s duration_ms=%d",
                self.provider,
                batch_id,
                len(batch),
                endpoint_host,
                returned_model,
                round((time.monotonic() - started_at) * 1000),
            )
            for item in batch:
                item_started = item.started_at
                try:
                    answer = answers.get(item.request_id)
                    if not isinstance(answer, Mapping):
                        raise ValueError(
                            "Decision API batch omitted this decision's answer"
                        )
                    result = _format_classifier_result(
                        answer.get("choice"),
                        item.request_data,
                        item.response_schema,
                    )
                except Exception as exc:  # noqa: BLE001
                    self._log_batch_fallback(
                        item,
                        batch_id,
                        endpoint_host,
                        phase="response_mapping",
                        error=exc,
                        started_at=item_started,
                    )
                    self._complete_fallback(item)
                    continue
                LOGGER.warning(
                    "%s_RESPONSE_RECEIVED request_id=%s batch_id=%s "
                    "endpoint_host=%s model=%s duration_ms=%d",
                    self.provider,
                    item.request_id,
                    batch_id,
                    endpoint_host,
                    returned_model,
                    round((time.monotonic() - item_started) * 1000),
                )
                LOGGER.warning(
                    "%s_RESULT_ACCEPTED request_id=%s batch_id=%s "
                    "endpoint_host=%s model=%s duration_ms=%d",
                    self.provider,
                    item.request_id,
                    batch_id,
                    endpoint_host,
                    returned_model,
                    round((time.monotonic() - item_started) * 1000),
                )
                item.future.set_result(result)
        except Exception as exc:  # noqa: BLE001
            for item in batch:
                self._log_batch_fallback(
                    item,
                    batch_id,
                    endpoint_host,
                    phase=phase,
                    error=exc,
                    started_at=item.started_at,
                )
                self._complete_fallback(item)

    def _complete_fallback(self, item: _PendingDecision) -> None:
        try:
            result = self._fallback_or_false(item.messages, item.response_schema)
        except Exception as exc:  # noqa: BLE001
            item.future.set_exception(exc)
        else:
            item.future.set_result(result)

    def _log_batch_fallback(
        self,
        item: _PendingDecision,
        batch_id: str,
        endpoint_host: str,
        *,
        phase: str,
        error: Exception,
        started_at: float,
    ) -> None:
        LOGGER.warning(
            "%s_FALLBACK request_id=%s batch_id=%s endpoint_host=%s "
            "phase=%s error_type=%s http_status=%s duration_ms=%d",
            self.provider,
            item.request_id,
            batch_id,
            endpoint_host,
            phase,
            type(error).__name__,
            getattr(error, "code", "none"),
            round((time.monotonic() - started_at) * 1000),
        )

    def _fallback_or_false(
        self,
        messages: list[dict[str, str]],
        response_schema: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        if self._fail_closed:
            output_property = _single_output_property(response_schema)
            if output_property and output_property.get("type") == "boolean":
                return {output_property["name"]: False}
            raise ValueError(
                f"{self.provider} is unavailable for a fail-closed decision"
            )
        return _fallback(self._fallback, messages, response_schema)

    def _call(
        self, request_data: Mapping[str, Any], api_key: str, endpoint: str
    ) -> Mapping[str, Any]:
        raise NotImplementedError

    def _call_batch(
        self,
        batch: list[_PendingDecision],
        api_key: str,
        endpoint: str,
    ) -> Mapping[str, Any]:
        raise NotImplementedError


class JevSemanticClassifierClient(_DecisionApiSemanticClassifierClient):
    """Use Jev for classifier judges, retaining planning as an error fallback."""

    provider = "JEV"
    default_model = JEV_MODEL
    default_endpoint = JEV_DEFAULT_BASE_URL

    def __init__(
        self,
        fallback: WorkflowLLMClient,
        *,
        api_key: str | None = None,
        endpoint: str | None = None,
        model: str | None = None,
        fail_closed: bool = False,
    ) -> None:
        super().__init__(
            fallback,
            api_key=api_key,
            endpoint=endpoint,
            model=model,
            fail_closed=fail_closed,
            _api_key=(
                os.environ.get("TYPESAFEAI_API_KEY")
                or os.environ.get("TYPESAFE_API_KEY")
                or os.environ.get("SYSTEM_ONE_API_KEY")
            ),
            _endpoint_env="SYSTEM_ONE_BASE_URL",
            _model_env="SYSTEM_ONE_MODEL",
        )

    def complete_json_batch(
        self,
        requests: list[tuple[list[dict[str, str]], Mapping[str, Any]]],
    ) -> list[dict[str, Any]]:
        """Complete independent classifier requests in explicit JEV batches."""
        results: list[dict[str, Any] | None] = [None] * len(requests)
        pending: list[_PendingDecision] = []
        for index, (messages, response_schema) in enumerate(requests):
            request_id = uuid.uuid4().hex
            request_data = _classifier_request(messages, response_schema)
            if not self._api_key or request_data is None:
                results[index] = _fallback(self._fallback, messages, response_schema)
                continue
            pending.append(
                _PendingDecision(
                    result_index=index,
                    request_id=request_id,
                    request_data=request_data,
                    messages=messages,
                    response_schema=response_schema,
                    started_at=time.monotonic(),
                    future=Future(),
                )
            )

        for offset in range(0, len(pending), 8):
            batch = pending[offset : offset + 8]
            self._execute_batch(batch)
            for item in batch:
                results[item.result_index] = item.future.result()
        if any(result is None for result in results):
            raise RuntimeError("JEV batch did not produce every requested result")
        return [result for result in results if result is not None]

    def _call(
        self, request_data: Mapping[str, Any], api_key: str, endpoint: str
    ) -> Mapping[str, Any]:
        return _call_jev(request_data, api_key, endpoint, self._model)

    def _call_batch(
        self,
        batch: list[_PendingDecision],
        api_key: str,
        endpoint: str,
    ) -> Mapping[str, Any]:
        return _call_jev_batch(batch, api_key, endpoint, self._model)


class SubQSemanticClassifierClient(_DecisionApiSemanticClassifierClient):
    """Use SubQ Decisions for classifier judges with planning as fallback."""

    provider = "SUBQ"
    default_model = SUBQ_MODEL
    default_endpoint = SUBQ_DEFAULT_BASE_URL

    def __init__(
        self,
        fallback: WorkflowLLMClient,
        *,
        api_key: str | None = None,
        endpoint: str | None = None,
        model: str | None = None,
        fail_closed: bool = False,
    ) -> None:
        super().__init__(
            fallback,
            api_key=api_key,
            endpoint=endpoint,
            model=model,
            fail_closed=fail_closed,
            _api_key=os.environ.get("SUBQ_API_KEY"),
            _endpoint_env="SUBQ_BASE_URL",
            _model_env="SUBQ_MODEL",
        )

    def _call(
        self, request_data: Mapping[str, Any], api_key: str, endpoint: str
    ) -> Mapping[str, Any]:
        return _call_subq(request_data, api_key, endpoint, self._model)


def preferred_semantic_classifier_client(
    fallback: WorkflowLLMClient, *, fail_closed: bool = False
) -> _DecisionApiSemanticClassifierClient | None:
    """Choose Jev when configured, then SubQ, without requiring credentials."""
    if (
        os.environ.get("TYPESAFEAI_API_KEY")
        or os.environ.get("TYPESAFE_API_KEY")
        or os.environ.get("SYSTEM_ONE_API_KEY")
    ):
        return JevSemanticClassifierClient(fallback, fail_closed=fail_closed)
    if os.environ.get("SUBQ_API_KEY"):
        return SubQSemanticClassifierClient(fallback, fail_closed=fail_closed)
    return None


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
        raise ValueError("Decision API returned an unsupported boolean classification")
    if choice == "unresolved":
        if output_property and output_property["name"] != "value":
            raise ValueError("Decision API abstained on a single-field classification")
        return {
            "status": "unresolved",
            "value": None,
            "reason_code": "classifier_abstained",
        }
    if choice not in allowed:
        raise ValueError("Decision API returned an unsupported classifier value")
    if response_schema is not None and {"status", "value", "reason_code"} <= set(
        response_schema.get("properties", {})
    ):
        return {"status": "resolved", "value": choice, "reason_code": None}
    if output_property:
        return {output_property["name"]: choice}
    raise ValueError("Decision API output does not match the declared schema")


def _call_jev(
    classifier: Mapping[str, Any], api_key: str, endpoint: str, model: str = JEV_MODEL
) -> Mapping[str, Any]:
    return _call_decision_api(classifier, api_key, endpoint, model)


def _call_jev_batch(
    batch: list[_PendingDecision],
    api_key: str,
    endpoint: str,
    model: str = JEV_MODEL,
) -> Mapping[str, Any]:
    items = {item.request_id: item.request_data for item in batch}
    state = {item_id: item_data["state"] for item_id, item_data in items.items()}
    questions = {
        item_id: _decision_question(item_data, item_id=item_id)
        for item_id, item_data in items.items()
    }
    body = {
        "model": model,
        "state": {"items": state},
        "questions": questions,
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
    answers = result.get("answers")
    if not isinstance(answers, Mapping):
        raise ValueError("Decision API batch response has no answers object")
    return {
        "answers": answers,
        "model": result.get("model", model),
        "usage": result.get("usage"),
    }


def _call_subq(
    classifier: Mapping[str, Any], api_key: str, endpoint: str, model: str
) -> Mapping[str, Any]:
    """Call SubQ's Decisions endpoint and normalize its typed answer."""
    return _call_decision_api(classifier, api_key, endpoint, model)


def _call_decision_api(
    classifier: Mapping[str, Any], api_key: str, endpoint: str, model: str
) -> Mapping[str, Any]:
    body = {
        "model": model,
        "state": classifier["state"],
        "questions": {"decision": _decision_question(classifier)},
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
        raise ValueError("Decision API response has no decision answer")
    return {
        "answer": answer,
        "model": result.get("model", model),
        "usage": result.get("usage"),
    }


def _decision_question(
    classifier: Mapping[str, Any], *, item_id: str | None = None
) -> dict[str, Any]:
    labels = classifier["allowed_values"]
    instructions = list(classifier["instructions"])
    if item_id is not None:
        instructions.insert(
            0,
            f"Evaluate only input item {item_id} in state.items.{item_id}.",
        )
    return {
        "type": "choice",
        "instructions": str(classifier["question"])
        + "\n\n"
        + "\n".join(f"- {item}" for item in instructions)
        + "\nChoose unresolved if the proposition does not support a label.",
        "criteria": {
            label: classifier.get("criteria", {}).get(label) for label in labels
        }
        | {"unresolved": "The proposition does not support a defensible label."},
    }
