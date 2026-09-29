from __future__ import annotations

import json
from typing import Any

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


def test_jev_provider_failure_falls_back(monkeypatch: Any) -> None:
    fallback = _Fallback()

    def fail(*_: Any) -> Any:
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(jev_classifier, "_call_jev", fail)

    result = JevSemanticClassifierClient(fallback, api_key="key").complete_json(
        _messages("polarity"), response_schema=_decision_schema()
    )

    assert result["value"] == "absent"
    assert fallback.calls == 1
