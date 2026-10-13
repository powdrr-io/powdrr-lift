"""Draft source-grounded evaluation labels without exposing them to generation."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from .common import (
    IDS,
    TEXT,
    Recorder,
    array_schema,
    checked_ids,
    checked_rows,
    choice_schema,
    digest,
    object_schema,
    require_text,
)

REFERENCE_SCHEMA = object_schema(
    validations=array_schema(
        object_schema(
            behavior=TEXT,
            source_ids=IDS,
            template_groups=array_schema(IDS),
            recoverability=choice_schema("instruction", "patch_only", "uncertain"),
            rationale=TEXT,
        )
    )
)

REFERENCE_PROMPT = """Draft an independent evaluation reference inventory before
viewing generated acceptance criteria. Enumerate the instruction's required
observable predicates, conditions, branches, scope and timing. Optional patch
evidence is offline corroboration only. The generator will never see it.
If a patch demands a behavior that cannot be recovered from the instruction,
mark patch_only. If the source cannot settle the outcome, mark uncertain and
explain. Only instruction-recoverable validations enter recall scoring. Do not
invent requirements from incidental implementation choices or ordinary best
practices. Context/current limitations and branch/commit process are not product
obligations. Preserve permitted alternative implementations. Split independently
verifiable outcomes; do not predict a test count. Cite exact source IDs for
instruction-recoverable labels. For template_groups, each group contains
alternative suitable template IDs; multiple groups mean multiple patterns are
needed. Use an empty groups list for a real catalog miss. Return JSON only."""


def draft_reference(
    task: dict[str, Any],
    catalog: dict[str, Any],
    recorder: Recorder,
    tasks_dir: Path | None = None,
) -> dict[str, Any]:
    """Patches enter this label-drafting path only, never generation_input()."""
    evidence = {}
    hashes = {}
    if tasks_dir is not None:
        for relative in ("tests/test.patch", "solution/solution.patch"):
            path = tasks_dir / task["task_id"] / relative
            if path.exists():
                text = path.read_text()
                evidence[relative] = text
                hashes[relative] = hashlib.sha256(text.encode()).hexdigest()
    tids = {card["id"] for card in catalog["templates"]}
    sources = {span["id"] for span in task["source_spans"]}

    def validate(raw: dict[str, Any]) -> list[dict[str, Any]]:
        rows = checked_rows(raw, "validations")
        if not rows:
            raise ValueError("empty reference draft")
        for row in rows:
            require_text(row, "behavior")
            require_text(row, "rationale")
            ids = checked_ids(row.get("source_ids"), sources, "reference sources")
            if row.get("recoverability") not in {
                "instruction",
                "patch_only",
                "uncertain",
            }:
                raise ValueError("invalid source recoverability")
            if row["recoverability"] == "instruction" and not ids:
                raise ValueError("instruction-derived reference needs source evidence")
            groups = row.get("template_groups")
            if not isinstance(groups, list):
                raise ValueError("template groups must be arrays of alternatives")
            for group in groups:
                if not checked_ids(group, tids, "reference template group"):
                    raise ValueError("empty alternative group")
        return rows

    rows = recorder.call(
        "draft-reference",
        REFERENCE_PROMPT,
        {
            **task,
            "offline_patch_evidence": evidence,
            "templates": [
                {key: card[key] for key in ("id", "name", "applies_when")}
                for card in catalog["templates"]
            ],
        },
        REFERENCE_SCHEMA,
        validate,
    )
    scored = [
        {**row, "id": f"v{i:03}", "basis": "instruction"}
        for i, row in enumerate(rows, 1)
        if row["recoverability"] == "instruction"
    ]
    return {
        "task_id": task["task_id"],
        "instruction_sha256": digest(task["instruction"]),
        "label_status": "model_draft",
        "author": {"provider": recorder.provider, "model": recorder.model},
        "validations": scored,
        "excluded_labels": [
            row for row in rows if row["recoverability"] != "instruction"
        ],
        "offline_evidence_sha256": hashes,
        "calls": recorder.calls,
    }
