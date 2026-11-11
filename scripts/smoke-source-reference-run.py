#!/usr/bin/env python3
"""Opt-in paid-provider smoke for the protected local source reference run."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from powdrr_contracts.models import ChatCompletionRequest
from powdrr_local.miniswe_inference import create_miniswe_inference_handler
from powdrr_service.providers.inference import RoutedInferenceClient
from powdrr_service.structrr.reference_definitions import ReferenceDefinition

from powdrr_lift.workrr.coding_agent_validation import ValidationProfile
from powdrr_lift.workrr.source_reference_run import SourceReferenceRun


class Reviewer:
    def __init__(self, model: str) -> None:
        self.model = model
        self.client = RoutedInferenceClient(timeout=120)

    def complete_json(
        self, messages: list[dict[str, str]], *, response_schema: Any = None
    ) -> dict[str, Any]:
        response = self.client.complete(
            ChatCompletionRequest(
                model=self.model,
                messages=tuple(dict(message) for message in messages),
                options={"response_format": {"type": "json_object"}},
            )
        )
        result = json.loads(response.content or "null")
        if not isinstance(result, dict):
            raise ValueError("Reviewer did not return a JSON object")
        return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--model", required=True)
    parser.add_argument("--artifact-root", type=Path, required=True)
    args = parser.parse_args()
    if not args.live or os.environ.get("POWDRR_LIVE_SOURCE_REFERENCE_SMOKE") != "1":
        parser.error("Requires --live and POWDRR_LIVE_SOURCE_REFERENCE_SMOKE=1")
    root = args.artifact_root.resolve()
    root.mkdir(parents=True, exist_ok=False)
    workspace = root / "candidate"
    workspace.mkdir()

    def git(*arguments: str) -> str:
        return subprocess.run(
            ["git", *arguments],
            cwd=workspace,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    git("init")
    git("config", "user.name", "Source reference smoke")
    git("config", "user.email", "smoke@example.invalid")
    (workspace / "value.py").write_text("def twice(value):\n    return value\n")
    git("add", "value.py")
    git("commit", "-m", "smoke baseline")
    design = {
        "reference_definitions": [
            ReferenceDefinition(
                "feature:twice", "feature", "Return twice the numeric input.", "smoke:1"
            ).to_data()
        ],
        "source_reference_placements": [
            {
                "obligation_id": "place:twice",
                "ref": "feature:twice",
                "relationship": "realizes",
                "requirement": "Return twice the numeric input.",
                "owner": {
                    "path": "value.py",
                    "kind": "function",
                    "qualified_name": "twice",
                },
                "cardinality": "exactly_one",
                "verification_refs": ["smoke:behavior"],
            }
        ],
    }
    run = SourceReferenceRun.prepare(
        workspace_root=workspace,
        accepted_design=design,
        base_commit=git("rev-parse", "HEAD"),
        objective="Make twice return twice its input and attach feature:twice. "
        "Use the supported reader and context command to retrieve its definition.",
        allowed_paths=("value.py",),
        profiles={
            "behavior": ValidationProfile(
                "behavior",
                (sys.executable, "-c", "from value import twice; assert twice(3)==6"),
            )
        },
        artifact_root=root / "runs",
        run_id="live-smoke",
        model=args.model,
        review_client=Reviewer(args.model),
    )
    result = run.execute(create_miniswe_inference_handler(run.worker_timeout_seconds))
    print(json.dumps(result.to_data(), indent=2))
    return 0 if result.accepted else 1


if __name__ == "__main__":
    raise SystemExit(main())
