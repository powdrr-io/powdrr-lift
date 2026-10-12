"""Replay only failed enum mappings, capture raw Jev answers, normalize abstention."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
from typing import Any

from powdrr_lift.workrr.jev_classifier import (
    JEV_DEFAULT_BASE_URL,
    _call_jev,
    _classifier_request,
    _format_classifier_result,
)
from science.deepswe.acceptance_templates.common import Recorder, write_json

HERE = Path(__file__).resolve().parent


class ObservedJev:
    """Same request builder/endpoint; preserve provider output before enum mapping."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def complete_json(
        self, messages: list[dict[str, str]], *, response_schema: Any = None
    ) -> dict[str, Any]:
        wrapped = [
            {"role": "system", "content": "Return only the classifier result."},
            {
                "role": "user",
                "content": messages[0]["content"]
                + "\n\nContext:\n"
                + messages[1]["content"],
            },
        ]
        request = _classifier_request(wrapped, response_schema)
        if request is None:
            raise ValueError("unsupported diagnostic request")
        api_key = next(
            (
                os.environ[k]
                for k in (
                    "TYPESAFEAI_API_KEY",
                    "TYPESAFE_API_KEY",
                    "SYSTEM_ONE_API_KEY",
                )
                if os.environ.get(k)
            ),
            None,
        )
        if not api_key:
            raise ValueError("Typesafe.ai key missing")
        base = os.environ.get("SYSTEM_ONE_BASE_URL")
        endpoint = f"{base.rstrip('/')}/systemone" if base else JEV_DEFAULT_BASE_URL
        raw = _call_jev(
            request, api_key, endpoint, os.environ.get("SYSTEM_ONE_MODEL", "jev-latest")
        )
        write_json(
            self.path,
            {
                "response": raw,
                "normalization": (
                    "Provider unresolved maps to experiment unknown; no model "
                    "substitution."
                ),
            },
        )
        if raw["answer"].get("choice") == "unresolved":
            return {"choice": "unknown"}
        return _format_classifier_result(
            raw["answer"].get("choice"), request, response_schema
        )


def main() -> None:
    module = importlib.util.spec_from_file_location(
        "decision_experiment", HERE / "run_experiment.py"
    )
    if module is None or module.loader is None:
        raise ValueError("missing runner")
    runner = importlib.util.module_from_spec(module)
    module.loader.exec_module(runner)
    repaired = []
    for target in sorted((HERE / "results/current").glob("*.json")):
        result = json.loads(target.read_text())
        if "error" not in result:
            continue
        case_id = result["id"]
        old = runner.load(
            HERE / "calls/decisions/current" / case_id / "jev/choose-0.json"
        )
        flash = runner.load(
            HERE / "calls/decisions/current" / case_id / "flash/choose-0.json"
        )
        assert old["status"] == "failed" and flash["status"] == "completed"
        messages = old["request"]["messages"]
        path = HERE / "diagnostics" / f"{case_id}-provider.json"
        rec = Recorder(
            ObservedJev(path),
            HERE / "calls/diagnostics" / case_id,
            provider="typesafe.ai",
            model=old["request"]["model"],
            resume=True,
            repairs=0,
        )
        answer = rec.call(
            "observe-abstention",
            messages[0]["content"],
            json.loads(messages[1]["content"]),
            old["request"]["schema"],
            lambda row: row,
        )
        result.update(
            flash=flash["response"]["choice"],
            jev=answer["choice"],
            consensus=answer["choice"]
            if answer["choice"] == flash["response"]["choice"]
            else "unknown",
            initial_mapping_error=result.pop("error"),
            diagnostic=str(path.relative_to(HERE)),
            calls=[flash["receipt"], old["receipt"], *rec.calls],
        )
        # These four cases abstain; do not synthesize a missing decision.
        assert result["consensus"] == "unknown"
        result["rendered"] = None
        write_json(target, result)
        repaired.append(
            {
                "id": case_id,
                "provider_choice": runner.load(path)["response"]["answer"]["choice"],
                "experiment_choice": answer["choice"],
            }
        )
    if repaired or not (HERE / "diagnostics/summary.json").exists():
        write_json(
            HERE / "diagnostics/summary.json",
            {
                "replays": repaired,
                "production_issue": (
                    "_call_jev offers unresolved; _format_classifier_result "
                    "rejects it for single output named choice, causing "
                    "fail-closed ValueError."
                ),
                "caveat": (
                    "Initial raw provider bodies were not captured. Two replays "
                    "confirmed unresolved; two returned the declared unknown "
                    "label. The exact original provider choice cannot be proved "
                    "for the latter two."
                ),
                "scope": "Diagnostic normalization only; production client unchanged.",
            },
        )
    cases = runner.load(HERE / "cases.json")["cases"]
    results = [
        runner.load(path) for path in sorted((HERE / "results").glob("*/*.json"))
    ]
    runner.finish(cases, results)


if __name__ == "__main__":
    main()
