"""Verify saved experiment artifacts without model calls or repository tests."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tarfile
from collections import Counter
from pathlib import Path
from typing import Any

from run_experiment import ARMS, metrics

from science.deepswe.acceptance_templates.common import digest as object_digest

HERE = Path(__file__).resolve().parent


def read(name: str) -> dict[str, Any]:
    return json.loads((HERE / name).read_text())


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def main() -> None:
    manifest = read("manifest.json")
    assert object_digest(read("cases.json")) == manifest["cases_sha256"]
    assert digest((HERE / "oracle.json").read_bytes()) == manifest["oracle_sha256"]
    cases = {c["id"]: c for c in read("cases.json")["cases"]}
    labels = {c["id"]: c for c in read("oracle.json")["labels"]}
    rows = [
        json.loads(p.read_text()) for p in sorted((HERE / "results").rglob("*.json"))
    ]
    assert len(rows) == len(cases) * len(ARMS) == 66
    assert len({(r["arm"], r["id"]) for r in rows}) == len(rows)
    for split in ("development", "evaluation", "all"):
        for arm in ARMS:
            selected = [
                r
                for r in rows
                if r["arm"] == arm
                and (split == "all" or cases[r["id"]]["split"] == split)
            ]
            for model in ("flash", "jev", "consensus"):
                assert (
                    metrics(selected, labels, model)
                    == read("scores.json")["scores"][split][arm][model]
                )
    for task, info in manifest["tasks"].items():
        sha = subprocess.check_output(
            ["git", "-C", info["base"], "rev-parse", "HEAD"], text=True
        ).strip()
        assert sha == info["sha"]
        source = read(f"sources/{task}.json")
        for excerpt in source["original_code"]:
            lines = (Path(info["base"]) / excerpt["file"]).read_text().splitlines()
            assert excerpt["text"] == "\n".join(
                lines[excerpt["start"] - 1 : excerpt["end"]]
            )
        task_root = Path.home() / "code/powdrr-deep-swe/tasks" / task
        for name, sha in manifest["task_hashes"][task].items():
            assert digest((task_root / name).read_bytes()) == sha
        product = [
            r
            for r in read(f"routing/{task}.json")["inventory"]["items"]
            if r["route"] in {"include", "include_prohibition"}
        ]
        for arm in ARMS:
            prompt = (HERE / f"prompts/{arm}/{task}.md").read_text()
            assert prompt.count("\n# ") == 5
            for item in product:
                assert item["text"] in prompt
            for row in rows:
                if row["arm"] == arm and row["task_id"] == task:
                    expected = (
                        cases[row["id"]]["options"][row["consensus"]]
                        if row["consensus"] != "unknown"
                        else None
                    )
                    assert row["rendered"] == expected
                    if expected:
                        assert expected in prompt
    index = read("call-checkpoints.json")["checkpoints"]
    with tarfile.open(HERE / "call-checkpoints.tar.gz") as archive:
        assert set(archive.getnames()) == {c["file"] for c in index}
        for checkpoint in index:
            member = archive.extractfile(checkpoint["file"])
            assert member is not None
            raw = member.read()
            assert digest(raw) == checkpoint["sha256"]
            saved = json.loads(raw)
            assert saved["request_sha256"] == checkpoint["request_sha256"]
            if checkpoint["file"].startswith("calls/decisions/"):
                payload = json.loads(saved["request"]["messages"][1]["content"])
                assert (
                    not {"expected", "evidence", "solution", "patch", "labels"}
                    & payload.keys()
                )
    secrets = [
        v.encode()
        for k, v in os.environ.items()
        if len(v) >= 10
        and any(s in k.upper() for s in ("TOKEN", "API_KEY", "SECRET", "PASSWORD"))
    ]
    for path in HERE.rglob("*"):
        if path.is_file() and path.suffix in {".json", ".md", ".py"}:
            assert not any(secret in path.read_bytes() for secret in secrets), (
                "Credential scan failed"
            )
    audited = {
        arm: {
            model: metrics(
                [
                    r
                    for r in rows
                    if r["arm"] == arm
                    and cases[r["id"]]["split"] == "evaluation"
                    and r["id"] != "g-extensions-lifetime"
                ],
                labels,
                model,
            )
            for model in ("flash", "jev", "consensus")
        }
        for arm in ARMS
    }
    (HERE / "source-audit.json").write_text(
        json.dumps(
            {
                "excluded_case": "g-extensions-lifetime",
                "reason": (
                    "Literal None guarantee lacks helper-versus-wire observation "
                    "boundary; WebSocket adds control metadata to extensions."
                ),
                "evidence": ["solution.patch:575-606", "solution.patch:635-662"],
                "frozen_cases_and_labels_unchanged": True,
                "positive_evaluation_decisions": 10,
                "scores": audited,
            },
            indent=2,
        )
        + "\n"
    )
    summary = {
        "model_calls": len(index),
        "providers": dict(Counter(c["receipt"]["provider"] for c in index)),
        "statuses": dict(Counter(c["status"] for c in index)),
        "summed_provider_seconds": round(
            sum(c["receipt"]["elapsed_seconds"] for c in index), 3
        ),
        "token_usage": "Unavailable; provider receipts contain empty usage objects.",
        "paired_decisions": len(rows),
        "prompts": 9,
        "verification": [
            "frozen hashes",
            "original source commits/excerpts",
            "task artifact hashes",
            "score recomputation",
            "source/label packet boundary",
            "literal rendering",
            "checkpoint archive hashes",
            "environment credential scan",
        ],
        "tests_run": False,
        "coding_agents_run": False,
    }
    (HERE / "run-summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
