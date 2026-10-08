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


def test_fail_closed_boolean_decision_does_not_use_planning_fallback(
    monkeypatch: Any,
) -> None:
    fallback = _Fallback()
    monkeypatch.delenv("TYPESAFEAI_API_KEY", raising=False)
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.delenv("SYSTEM_ONE_API_KEY", raising=False)

    client = JevSemanticClassifierClient(fallback, fail_closed=True)
    result = client.complete_json(
        [
            {"role": "system", "content": "Return JSON."},
            {
                "role": "user",
                "content": "Question:\nAre these sentences equivalent?\n\nContext:\n{}",
            },
        ],
        response_schema={
            "type": "object",
            "required": ["equivalent"],
            "properties": {"equivalent": {"type": "boolean"}},
        },
    )

    assert result == {"equivalent": False}
    assert fallback.calls == 0


def test_fail_closed_jev_provider_error_rejects_without_planning_fallback(
    monkeypatch: Any,
) -> None:
    fallback = _Fallback()

    def fail(*_: Any) -> Any:
        raise OSError("Jev unavailable")

    monkeypatch.setattr(jev_classifier, "_call_jev", fail)
    messages = [
        {"role": "system", "content": "Return JSON."},
        {
            "role": "user",
            "content": (
                "Question:\nAre these sentences equivalent?\n\nContext:\n"
                '{"request": {"text": "A.", "reconstructed_sentence": "B."}}'
            ),
        },
    ]

    result = JevSemanticClassifierClient(
        fallback, api_key="key", fail_closed=True
    ).complete_json(
        messages,
        response_schema={
            "type": "object",
            "required": ["equivalent"],
            "properties": {"equivalent": {"type": "boolean"}},
        },
    )

    assert result == {"equivalent": False}
    assert fallback.calls == 0


def test_jev_receives_original_and_reconstructed_sentences_for_equivalence(
    monkeypatch: Any,
) -> None:
    fallback = _Fallback()
    captured: dict[str, Any] = {}

    def classify(request: Mapping[str, Any], *_: Any) -> dict[str, str]:
        captured.update(request)
        return {"choice": "true"}

    monkeypatch.setattr(jev_classifier, "_call_jev", classify)
    original = "The request succeeds if A and (B or C)."
    reconstructed = "The request succeeds if A and either B or C."
    messages = [
        {"role": "system", "content": "Return JSON."},
        {
            "role": "user",
            "content": (
                "Question:\nAre these sentences equivalent?"
                "\n\nContext:\n"
                + json.dumps(
                    {
                        "atomicity_reconstruction_request": {
                            "text": original,
                            "reconstructed_sentence": reconstructed,
                            "valid": True,
                        }
                    }
                )
            ),
        },
    ]

    result = JevSemanticClassifierClient(fallback, api_key="key").complete_json(
        messages,
        response_schema={
            "type": "object",
            "required": ["equivalent"],
            "properties": {"equivalent": {"type": "boolean"}},
        },
    )

    assert result == {"equivalent": True}
    assert captured["state"]["source_text"] == original
    assert reconstructed in captured["state"]["context"]


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
