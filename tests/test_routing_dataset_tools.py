from __future__ import annotations

import hashlib
import json
import sys
from argparse import Namespace
from pathlib import Path
from typing import Any

import pytest

from science.classifications.routing import finalize_labels
from science.classifications.routing.build_dataset import _build
from science.classifications.routing.input_contract import (
    inference_input_sha256,
    serialize_inference_input,
)
from science.classifications.routing.validate_dataset import validate


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def test_inference_serializer_is_stable_and_only_accepts_contract_fields() -> None:
    inputs = {
        "proposition": "The cache must be isolated.",
        "local_context": {
            "section_heading": "Required behavior",
            "previous_sentence": None,
            "source_sentence": "The cache must be isolated.",
            "next_sentence": None,
        },
        "scope_relations": None,
    }

    serialized = serialize_inference_input(inputs)

    assert json.loads(serialized) == inputs
    assert inference_input_sha256(inputs) == hashlib.sha256(serialized).hexdigest()
    assert inference_input_sha256(inputs) == inference_input_sha256(
        dict(reversed(list(inputs.items())))
    )
    with pytest.raises(ValueError, match="exactly"):
        serialize_inference_input({**inputs, "label": "include"})


def test_builder_preserves_source_spans_and_marks_old_label_as_proposed(
    tmp_path: Path,
) -> None:
    tasks = tmp_path / "tasks"
    task_dir = tasks / "sample-task"
    task_dir.mkdir(parents=True)
    text = (
        "# Required behavior\n"
        "The cache must be isolated. The cache is currently shared.\n"
    )
    (task_dir / "instruction.md").write_text(text, encoding="utf-8")
    (task_dir / "task.toml").write_text(
        '[metadata]\ntask_id = "sample-task"\nlanguage = "python"\n'
        'repository_url = "https://example.test/sample.git"\n',
        encoding="utf-8",
    )
    silver_path = tmp_path / "silver.jsonl"
    _write_jsonl(
        silver_path,
        [
            {
                "source": {
                    "task_id": "sample-task",
                    "source_family_id": "family:deepswe-repository:https://example.test/sample",
                },
                "inputs": {"proposition": "The cache must be isolated."},
                "labels": {"answerable": True, "class": "invariant"},
            }
        ],
    )

    docs, candidates, family_splits = _build(
        Namespace(tasks_dir=tasks, silver_dataset=silver_path)
    )

    required = next(
        row for row in candidates if "must be isolated" in row["inputs"]["proposition"]
    )
    assert required["annotation"]["suggested_label"] == "include"
    assert required["annotation"]["status"] == "proposed"
    span = required["source"]["target_span"]
    assert (
        docs[0]["text"][span["start"] : span["end"]]
        == required["inputs"]["proposition"]
    )
    assert (
        required["inputs"]["local_context"]["section_heading"] == "# Required behavior"
    )
    assert required["split"] == family_splits[required["source"]["family_id"]]

    output = tmp_path / "routing"
    _write_jsonl(output / "raw/source_documents.jsonl", docs)
    _write_jsonl(output / "data/candidates.jsonl", candidates)
    _write_jsonl(
        output / "splits/family-splits.jsonl",
        [{"family_id": key, "split": value} for key, value in family_splits.items()],
    )
    result = validate(
        output / "data",
        output / "raw",
        output / "splits/family-splits.jsonl",
        None,
    )
    assert result["candidate_examples"] == 2
    assert result["gold_rows"] == 0


def test_finalizer_requires_human_adjudication_for_disagreement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    review_id = "review:test"
    candidate: dict[str, Any] = {
        "example_id": "routing:test",
        "input_revision": "routing-input-v1",
        "rubric_revision": "routing-rubric-v1",
        "inputs": {
            "proposition": "The cache is currently shared.",
            "local_context": {
                "section_heading": "Current behavior",
                "previous_sentence": None,
                "source_sentence": "The cache is currently shared.",
                "next_sentence": None,
            },
            "scope_relations": None,
        },
    }
    packet_dir = tmp_path / "packet"
    base_review = {
        "review_id": review_id,
        "input_revision": candidate["input_revision"],
        "rubric_revision": candidate["rubric_revision"],
        **candidate["inputs"],
        "confidence": "high",
        "rationale": "Test fixture route decision.",
        "evidence_quotes": [candidate["inputs"]["proposition"]],
        "ambiguity_reason": None,
    }
    _write_jsonl(
        packet_dir / "selection_key.jsonl",
        [{"review_id": review_id, "example_id": candidate["example_id"]}],
    )
    _write_jsonl(
        packet_dir / "reviewer_a.jsonl",
        [{**base_review, "label": "context"}],
    )
    _write_jsonl(
        packet_dir / "reviewer_b.jsonl",
        [{**base_review, "label": "include"}],
    )
    candidate_path = tmp_path / "candidates.jsonl"
    _write_jsonl(candidate_path, [candidate])
    adjudication_path = tmp_path / "adjudications.jsonl"
    _write_jsonl(
        adjudication_path,
        [
            {
                "review_id": review_id,
                "final_label": "context",
                "ambiguity_reason": None,
                "rationale": "The heading makes this a report of current behavior.",
                "adjudicator_id": "reviewer-c",
            }
        ],
    )
    output_dir = tmp_path / "finalized"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "finalize_labels.py",
            "--packet-dir",
            str(packet_dir),
            "--candidate-file",
            str(candidate_path),
            "--reviewer-a-kind",
            "human",
            "--reviewer-a-id",
            "reviewer-a",
            "--reviewer-b-kind",
            "human",
            "--reviewer-b-id",
            "reviewer-b",
            "--adjudications",
            str(adjudication_path),
            "--output-dir",
            str(output_dir),
        ],
    )

    assert finalize_labels.main() == 0
    gold = json.loads((output_dir / "gold-labels.jsonl").read_text().splitlines()[0])
    events = (output_dir / "annotation-ledger.jsonl").read_text().splitlines()
    assert gold["final_label"] == "context"
    assert gold["status"] == "adjudicated"
    assert len(events) == 3
