from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

_BUILD_DATASET_PATH = (
    Path(__file__).resolve().parents[1]
    / "science/classifications/root_disposition/build_dataset.py"
)
_BUILD_DATASET_SPEC = importlib.util.spec_from_file_location(
    "root_disposition_build_dataset", _BUILD_DATASET_PATH
)
assert _BUILD_DATASET_SPEC is not None
assert _BUILD_DATASET_SPEC.loader is not None
build_dataset = importlib.util.module_from_spec(_BUILD_DATASET_SPEC)
sys.modules[_BUILD_DATASET_SPEC.name] = build_dataset
_BUILD_DATASET_SPEC.loader.exec_module(build_dataset)


def test_underspecified_root_retries_with_local_context_and_records_trace(
    monkeypatch: Any,
) -> None:
    root_responses = iter(
        [
            {"status": "resolved", "value": "feature", "reason_code": None},
            {
                "status": "unresolved",
                "value": None,
                "reason_code": "source_underspecified",
            },
        ]
    )
    calls: list[str] = []

    def fake_call_judge(
        _client: Any,
        *,
        context_name: str,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        calls.append(context_name)
        if context_name == "atomicity_clause":
            return {"multiple": False}
        if context_name == "root_decision_request":
            return next(root_responses)
        assert context_name == "context_assisted_root_decision_request"
        return {"status": "resolved", "value": "feature", "reason_code": None}

    monkeypatch.setattr(build_dataset, "_call_judge", fake_call_judge)
    task = {
        "task_id": "context-retry",
        "task_metadata": {
            "name": "Context retry",
            "repository_url": "https://example.com/project",
        },
        "instruction": (
            "The function processes a dataclass input. "
            "The instruction handles dataclasses."
        ),
    }

    task_run = build_dataset._label_task(
        task,
        client=object(),
        provider="test",
        model="test",
    )

    first, second = task_run["root_dispositions"]
    assert first["context_refinement"]["status"] == "not_triggered"
    assert second["context_refinement"]["needs_context"] is True
    assert second["context_refinement"]["status"] == "resolved_with_context"
    assert second["context_refinement"]["local_context"]["previous_sentence"] == (
        "The function processes a dataclass input."
    )
    assert (
        second["context_refinement"]["initial_teacher_response"]["reason_code"]
        == "source_underspecified"
    )
    assert second["teacher_response"]["value"] == "feature"

    example = build_dataset._example(task_run, second)
    assert example["inputs"]["context_level"] == "containing_and_adjacent_sentences"
    assert example["inputs"]["context_refinement"] == {
        "needs_context": True,
        "status": "resolved_with_context",
        "trigger_reason": "source_underspecified",
    }
    assert calls.count("context_assisted_root_decision_request") == 1
