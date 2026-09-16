from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from powdrr_lift.python_error_classifier import (
    classification_metrics,
    load_jsonl,
    split_records,
)


def test_generate_python_error_dataset_captures_real_failures(tmp_path: Path) -> None:
    output = tmp_path / "errors.jsonl"
    subprocess.run(
        [
            sys.executable,
            "scripts/generate_python_error_dataset.py",
            "--output",
            str(output),
            "--repetitions",
            "1",
        ],
        check=True,
    )
    records = load_jsonl(output)
    assert len(records) == 20
    assert {record["label"] for record in records} >= {
        "missing_parameter",
        "unexpected_keyword",
        "type_mismatch",
        "syntax_error",
    }
    assert all(record["returncode"] != 0 for record in records)
    assert all(
        str(record["text"]).startswith(str(record["exception_type"]))
        for record in records
    )


def test_error_records_split_deterministically_and_stratified() -> None:
    records = [
        {"id": f"{label}-{index}", "text": label, "label": label}
        for label in ("type_mismatch", "missing_parameter")
        for index in range(5)
    ]
    first = split_records(records, seed=7)
    assert first == split_records(records, seed=7)
    assert {record["label"] for record in first["test"]} == {
        "type_mismatch",
        "missing_parameter",
    }
    assert not set(record["id"] for record in first["train"]) & set(
        record["id"] for record in first["test"]
    )


def test_classification_metrics_reports_macro_f1() -> None:
    metrics = classification_metrics([0, 0, 1, 1], [0, 1, 1, 1], ["a", "b"])
    assert metrics["accuracy"] == 0.75
    assert metrics["per_label"]["b"]["recall"] == 1.0
    assert 0 < metrics["macro_f1"] < 1
