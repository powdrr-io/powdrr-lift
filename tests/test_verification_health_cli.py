from __future__ import annotations

import json
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from powdrr_lift.cli import main


def test_verification_health_cli_reports_json_and_blocks_enforce(
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "health.yaml"
    input_path.write_text(
        "intents:\n"
        "  - clause_id: intent.new\n"
        "    status: active\n"
        "    new: true\n"
        "contracts: []\n",
        encoding="utf-8",
    )
    output = StringIO()

    with redirect_stdout(output):
        result = main(
            [
                "verification-health",
                "--input",
                str(input_path),
                "--mode",
                "enforce",
            ]
        )

    assert result == 1
    report = json.loads(output.getvalue())
    assert report["passed"] is False
    assert report["findings"][0]["kind"] == "intent_without_contract"


def test_verification_diff_cli_reports_verifier_changes(tmp_path: Path) -> None:
    base = tmp_path / "base.yaml"
    candidate = tmp_path / "candidate.yaml"
    base.write_text(
        "obligation_id: obligation\nstatus: passed\n"
        "contract_fingerprint: old\nselector: old\nverifier_fingerprint: old\n",
        encoding="utf-8",
    )
    candidate.write_text(
        "obligation_id: obligation\nstatus: failed\n"
        "contract_fingerprint: new\nselector: new\nverifier_fingerprint: new\n",
        encoding="utf-8",
    )
    output = StringIO()

    with redirect_stdout(output):
        result = main(
            [
                "verification-diff",
                "--base",
                str(base),
                "--candidate",
                str(candidate),
            ]
        )

    assert result == 1
    report = json.loads(output.getvalue())
    assert report["result"]["status"] == "new_regression"
    assert len(report["verifier_changes"]) == 3
