from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import pytest

from powdrr_lift.workrr import jev_classifier
from powdrr_lift.workrr.jev_classifier import JevSemanticClassifierClient


class _Fallback:
    def __init__(self) -> None:
        self.calls = 0

    def complete_json(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        self.calls += 1
        return {"status": "resolved", "value": "absent", "reason_code": None}


def _messages(kind: str) -> list[dict[str, str]]:
    request = {
        "spec": {"decision_kind": kind},
        "question": "Classify this proposition.",
        "instructions": ["Use only source evidence."],
        "allowed_values": ["absent", "present"],
        "subject_text": "The operation returns a report.",
    }
    return [
        {"role": "system", "content": "Return JSON."},
        {"role": "user", "content": "Context:\n" + json.dumps({"request": request})},
    ]


def _routing_messages() -> list[dict[str, str]]:
    request = {
        "spec": {"decision_kind": "routing"},
        "question": "Route this exact statement.",
        "instructions": ["Use only the target and local source context."],
        "allowed_values": [
            "context",
            "exclude",
            "include",
            "include_prohibition",
            "unclear",
        ],
        "criteria": {
            "context": "A current fact without desired behavior.",
            "exclude": "A process instruction.",
            "include": "Desired product behavior.",
            "include_prohibition": "An explicit product prohibition.",
            "unclear": "The source does not resolve the distinction.",
        },
        "subject_text": (
            "Local source context:\n"
            "Previous sentence: Existing behavior is synchronous.\n\n"
            "Proposition to classify:\nThe library must preserve ordering."
        ),
        "provider_state": {
            "proposition": "The library must preserve ordering.",
            "local_context": "Existing behavior is synchronous.",
            "scope_relations": {"semantic_relations": []},
        },
    }
    return [
        {"role": "system", "content": "Return JSON."},
        {
            "role": "user",
            "content": "Question:\nRoute.\n\nContext:\n"
            + json.dumps(
                {
                    "routing_request": request,
                    "atomic_instruction_ledger": {
                        "source_text": (
                            "Existing behavior is synchronous. "
                            "The library must preserve ordering."
                        ),
                        "clauses": [
                            {
                                "clause_id": "instruction-001",
                                "ordinal": 1,
                                "text": "The library must preserve ordering.",
                                "source_span": {"start": 40, "end": 75},
                                "unexpected_metadata": "drop this",
                            }
                        ],
                    },
                }
            ),
        },
    ]


def _decision_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {"status": {}, "value": {}, "reason_code": {}},
    }


def test_jev_uses_valid_choice_regardless_of_confidence(monkeypatch: Any) -> None:
    fallback = _Fallback()
    monkeypatch.setattr(
        jev_classifier,
        "_call_jev",
        lambda *_: {"choice": "present", "confidence": 0.99},
    )

    result = JevSemanticClassifierClient(fallback, api_key="key").complete_json(
        _messages("has_exception"), response_schema=_decision_schema()
    )

    assert result == {"status": "resolved", "value": "present", "reason_code": None}
    assert fallback.calls == 0


def test_jev_logs_correlated_successful_call(
    monkeypatch: Any, caplog: pytest.LogCaptureFixture
) -> None:
    fallback = _Fallback()
    monkeypatch.setattr(jev_classifier, "_call_jev", lambda *_: {"choice": "present"})
    caplog.set_level("WARNING", logger="powdrr_lift.workrr.jev_classifier")

    JevSemanticClassifierClient(fallback, api_key="key").complete_json(
        _messages("has_exception"), response_schema=_decision_schema()
    )

    events = [
        record.getMessage()
        for record in caplog.records
        if record.name == "powdrr_lift.workrr.jev_classifier"
    ]
    assert [event.split()[0] for event in events] == [
        "JEV_REQUEST_STARTED",
        "JEV_RESPONSE_RECEIVED",
        "JEV_RESULT_ACCEPTED",
    ]
    request_ids = {event.split("request_id=", 1)[1].split()[0] for event in events}
    assert len(request_ids) == 1


def test_routing_request_uses_target_and_bounded_context_only(
    monkeypatch: Any,
) -> None:
    fallback = _Fallback()
    captured: dict[str, Any] = {}

    def classify(request: Mapping[str, Any], *_: Any) -> dict[str, str]:
        captured.update(request)
        return {"choice": "include"}

    monkeypatch.setattr(jev_classifier, "_call_jev", classify)
    result = JevSemanticClassifierClient(fallback, api_key="key").complete_json(
        _routing_messages(), response_schema=_decision_schema()
    )

    assert result == {"status": "resolved", "value": "include", "reason_code": None}
    assert captured["state"] == {
        "proposition": "The library must preserve ordering.",
        "local_context": "Existing behavior is synchronous.",
        "scope_relations": {"semantic_relations": []},
        "instruction_ledger": {
            "source_text": (
                "Existing behavior is synchronous. The library must preserve ordering."
            ),
            "clauses": [
                {
                    "clause_id": "instruction-001",
                    "ordinal": 1,
                    "text": "The library must preserve ordering.",
                    "source_span": {"start": 40, "end": 75},
                }
            ],
        },
    }
    assert "request" not in captured["state"]
    assert "context" not in captured["state"]
    assert captured["criteria"]["context"] == (
        "A current fact without desired behavior."
    )
    assert fallback.calls == 0


def test_jev_smoke_cases_build_structured_routing_inputs() -> None:
    from science.classifications.routing.evaluate_jev_smoke import (
        DEFAULT_CASES,
        DEFAULT_DOCUMENTS,
        _load_cases,
        _load_documents,
        _provider_request,
    )

    cases = _load_cases(DEFAULT_CASES)
    documents = _load_documents(DEFAULT_DOCUMENTS)
    assert len(cases) == 32
    assert {case["label"] for case in cases} == {
        "context",
        "include",
        "include_prohibition",
        "exclude",
        "unclear",
    }
    state = _provider_request(cases[0], "described", documents)["state"]
    assert state["proposition"] == cases[0]["proposition"]
    assert state["local_context"] == cases[0]["local_context"]
    assert "label" not in state
    assert state["instruction_ledger"]["source_text"]
    baseline_state = _provider_request(cases[0], "baseline", documents)["state"]
    assert "request" in baseline_state
    assert "criteria" not in baseline_state["request"]
    state_machine = next(
        case for case in cases if case["id"] == "statemachine-opening-context"
    )
    full_state = _provider_request(state_machine, "described", documents)["state"]
    assert (
        full_state["instruction_ledger"]["source_text"]
        == documents["state-machine-state-data-scoping"]
    )
    assert "Data survives pickle." in full_state["instruction_ledger"]["source_text"]
    assert "label" not in full_state


def test_jev_http_payload_includes_routing_criteria_and_model(
    monkeypatch: Any,
) -> None:
    captured: dict[str, Any] = {}

    class _Response:
        def __enter__(self) -> _Response:
            return self

        def __exit__(self, *_: Any) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps(
                {
                    "model": "jev-test-revision",
                    "answers": {"decision": {"choice": "context", "confidence": 0.94}},
                    "usage": {"input_tokens": 250, "output_tokens": 30},
                }
            ).encode()

    def open_request(request: Any, timeout: int) -> _Response:
        captured["body"] = json.loads(request.data)
        captured["timeout"] = timeout
        return _Response()

    monkeypatch.setattr(jev_classifier, "urlopen", open_request)
    request = jev_classifier._classifier_request(
        _routing_messages(), _decision_schema()
    )
    assert request is not None
    response = jev_classifier._call_jev(
        request, "test-key", "https://example.test", "jev-test"
    )

    body = captured["body"]
    criteria = body["questions"]["decision"]["criteria"]
    assert body["model"] == "jev-test"
    assert criteria["context"] == "A current fact without desired behavior."
    assert criteria["include"] == "Desired product behavior."
    assert criteria["include_prohibition"] == "An explicit product prohibition."
    assert criteria["exclude"] == "A process instruction."
    assert criteria["unclear"] == "The source does not resolve the distinction."
    assert criteria["unresolved"]
    assert body["state"] == request["state"]
    assert response["model"] == "jev-test-revision"
    assert response["usage"] == {"input_tokens": 250, "output_tokens": 30}


def test_jev_model_can_be_pinned_per_client(monkeypatch: Any) -> None:
    fallback = _Fallback()
    captured: list[str] = []

    def classify(_: Mapping[str, Any], *args: Any) -> dict[str, str]:
        captured.append(args[-1])
        return {"choice": "present"}

    monkeypatch.setattr(jev_classifier, "_call_jev", classify)
    JevSemanticClassifierClient(
        fallback, api_key="key", model="jev-pinned"
    ).complete_json(_messages("has_exception"), response_schema=_decision_schema())

    assert captured == ["jev-pinned"]


def test_jev_routes_other_semantic_classifier_kinds(monkeypatch: Any) -> None:
    fallback = _Fallback()
    monkeypatch.setattr(
        jev_classifier,
        "_call_jev",
        lambda *_: {"choice": "present", "confidence": 0.72},
    )

    result = JevSemanticClassifierClient(fallback, api_key="key").complete_json(
        _messages("polarity"), response_schema=_decision_schema()
    )

    assert result == {"status": "resolved", "value": "present", "reason_code": None}
    assert fallback.calls == 0


def test_jev_maps_boolean_atomicity_classifier(monkeypatch: Any) -> None:
    fallback = _Fallback()
    monkeypatch.setattr(jev_classifier, "_call_jev", lambda *_: {"choice": "true"})
    messages = [
        {"role": "system", "content": "Return JSON."},
        {
            "role": "user",
            "content": (
                "Instructions:\n- Return true when there are multiple requirements."
                "\n\nQuestion:\nAre there multiple requirements?"
                '\n\nContext:\n{"clause": {"text": "Return a value and '
                'emit an event."}}'
            ),
        },
    ]

    result = JevSemanticClassifierClient(fallback, api_key="key").complete_json(
        messages,
        response_schema={
            "type": "object",
            "required": ["multiple"],
            "properties": {"multiple": {"type": "boolean"}},
        },
    )

    assert result == {"multiple": True}
    assert fallback.calls == 0


def test_jev_handles_field_entailment_classifiers(monkeypatch: Any) -> None:
    fallback = _Fallback()
    monkeypatch.setattr(
        jev_classifier, "_call_jev", lambda *_: {"choice": "contradicted"}
    )
    field_request = {
        "spec": {"field": "temporal_scope"},
        "question": "Is this candidate field entailed by the source?",
        "instructions": ["Evaluate only the candidate field."],
        "allowed_values": ["entailed", "contradicted", "not_stated"],
        "source_text": "The API currently accepts strings.",
        "candidate_field": "temporal_scope",
        "candidate_value": "future",
    }
    messages = [
        {"role": "system", "content": "Return JSON."},
        {
            "role": "user",
            "content": "Context:\n"
            + json.dumps({"field_entailment_request": field_request}),
        },
    ]

    result = JevSemanticClassifierClient(fallback, api_key="key").complete_json(
        messages, response_schema=_decision_schema()
    )

    assert result == {
        "status": "resolved",
        "value": "contradicted",
        "reason_code": None,
    }
    assert fallback.calls == 0


def test_jev_abstention_falls_back(monkeypatch: Any) -> None:
    fallback = _Fallback()
    monkeypatch.setattr(
        jev_classifier, "_call_jev", lambda *_: {"choice": "unresolved"}
    )

    result = JevSemanticClassifierClient(fallback, api_key="key").complete_json(
        _messages("polarity"), response_schema=_decision_schema()
    )

    assert result == {
        "status": "unresolved",
        "value": None,
        "reason_code": "classifier_abstained",
    }
    assert fallback.calls == 0


def test_jev_provider_failure_falls_back(
    monkeypatch: Any, caplog: pytest.LogCaptureFixture
) -> None:
    fallback = _Fallback()

    def fail(*_: Any) -> Any:
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(jev_classifier, "_call_jev", fail)

    result = JevSemanticClassifierClient(fallback, api_key="key").complete_json(
        _messages("polarity"), response_schema=_decision_schema()
    )

    assert result["value"] == "absent"
    assert fallback.calls == 1
    assert "JEV_REQUEST_STARTED" in caplog.text
    assert "JEV_FALLBACK" in caplog.text


@pytest.mark.parametrize("answer", [None, "unresolved", "invalid"])
def test_jev_enum_gate_retains_planning_fallback(
    monkeypatch: Any, answer: str | None
) -> None:
    class GateFallback(_Fallback):
        def complete_json(self, messages: list[dict[str, str]]) -> dict[str, Any]:
            self.calls += 1
            return {"decision": "skip"}

    fallback = GateFallback()
    observed: list[dict[str, Any]] = []

    def fail(request: dict[str, Any], *_: Any) -> Any:
        observed.append(request)
        if answer is None:
            raise RuntimeError("provider unavailable")
        return {"choice": answer}

    monkeypatch.setattr(jev_classifier, "_call_jev", fail)
    result = JevSemanticClassifierClient(fallback, api_key="key").complete_json(
        [
            {"role": "system", "content": "Return JSON."},
            {
                "role": "user",
                "content": (
                    "Question:\nResearch?\n\nContext:\n"
                    '{"feature_description": "Local behavior."}'
                ),
            },
        ],
        response_schema={
            "type": "object",
            "required": ["decision"],
            "properties": {
                "decision": {"type": "string", "enum": ["research", "skip"]}
            },
        },
    )
    assert observed[0]["allowed_values"] == ["research", "skip"]
    assert fallback.calls == 1
    assert result == {"decision": "skip"}
