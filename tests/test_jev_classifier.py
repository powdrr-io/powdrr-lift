from __future__ import annotations

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
    import json

    return [
        {"role": "system", "content": "Return JSON."},
        {"role": "user", "content": "Context:\n" + json.dumps({"request": request})},
    ]


def test_jev_resolves_high_confidence_supported_decision(monkeypatch: Any) -> None:
    fallback = _Fallback()
    monkeypatch.setattr(
        jev_classifier,
        "_call_jev",
        lambda *_: {"choice": "present", "confidence": 0.99},
    )

    result = JevSemanticClassifierClient(fallback, api_key="key").complete_json(
        _messages("has_exception"), response_schema={}
    )

    assert result == {"status": "resolved", "value": "present", "reason_code": None}
    assert fallback.calls == 0


def test_jev_low_confidence_falls_back(monkeypatch: Any) -> None:
    fallback = _Fallback()
    monkeypatch.setattr(
        jev_classifier,
        "_call_jev",
        lambda *_: {"choice": "present", "confidence": 0.72},
    )

    result = JevSemanticClassifierClient(fallback, api_key="key").complete_json(
        _messages("temporal_scope"), response_schema={}
    )

    assert result["value"] == "absent"
    assert fallback.calls == 1


def test_jev_only_handles_qualified_classifier_kinds() -> None:
    fallback = _Fallback()

    result = JevSemanticClassifierClient(fallback, api_key="key").complete_json(
        _messages("polarity"), response_schema={}
    )

    assert result["value"] == "absent"
    assert fallback.calls == 1
