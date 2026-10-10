"""Shared requirement inventory, two generation arms, and deterministic rendering."""

from __future__ import annotations

import re
import shutil
from functools import partial
from pathlib import Path
from typing import Any

from . import PROMPT_VERSION, SCHEMA_VERSION
from .build_catalog import placeholders
from .common import (
    IDS,
    TEXT,
    Recorder,
    array_schema,
    checked_ids,
    checked_rows,
    choice_schema,
    digest,
    load_json,
    normalized,
    object_schema,
    require_text,
    write_json,
)

QUALITY = """Use only the supplied instruction and source spans. Describe observable,
unambiguous, verifiable outcomes. Preserve conditions, exceptions, quantifiers,
polarity, timing, identity versus equality, and permitted alternatives. Do not
invent missing defaults, copy depth, errors, interfaces, performance thresholds,
or exactly-one rules from not-both rules. Concrete examples can illustrate a
general rule but cannot introduce mandates. Keep distinct required outcomes.
No coding-agent branch/commit instructions: Powdrr owns repository orchestration.
Return only the requested JSON. No arbitrary count target or padding."""

INVENTORY_PROMPT = (
    QUALITY
    + """
Analyze the full instruction into atomic product requirements and context/process
statements. A requested final behavior can be written in the present tense.
A statement of the existing limitation is context. Classify every source span;
a span may support several items. Preserve relationships and exceptions using
the complete instruction context. Each requirement should have one decidable
behavior, with its scope/conditions attached. Name APIs exactly. Context and
process are retained for audit but do not become product requirements."""
)

INVENTORY_SCHEMA = object_schema(
    items=array_schema(
        object_schema(
            text=TEXT,
            kind=choice_schema("requirement", "context", "process"),
            source_ids=IDS,
        )
    )
)
SELECT_SCHEMA = object_schema(
    selections=array_schema(
        object_schema(
            requirement_id=TEXT,
            template_ids=IDS,
            rationale=TEXT,
        )
    )
)
BIND_SCHEMA = object_schema(
    decisions=array_schema(
        object_schema(
            requirement_id=TEXT,
            template_id=TEXT,
            applicability=choice_schema("yes", "no", "unknown"),
            reason=TEXT,
            instances=array_schema(
                object_schema(
                    prerequisites=array_schema(
                        object_schema(
                            id=TEXT,
                            source_ids=IDS,
                            source_quotes=array_schema(
                                object_schema(source_id=TEXT, quote=TEXT)
                            ),
                            support_explanation=TEXT,
                        )
                    ),
                    slots=array_schema(
                        object_schema(
                            name=TEXT,
                            kind=TEXT,
                            value=TEXT,
                            basis=choice_schema("instruction", "illustrative"),
                            source_ids=IDS,
                            source_quotes=array_schema(
                                object_schema(source_id=TEXT, quote=TEXT)
                            ),
                            support_explanation=TEXT,
                        )
                    ),
                )
            ),
        )
    )
)
DIRECT_SCHEMA = object_schema(
    criteria=array_schema(
        object_schema(
            requirement_ids=IDS,
            source_ids=IDS,
            text=TEXT,
        )
    )
)


def inventory(task: dict[str, Any], recorder: Recorder) -> dict[str, Any]:
    allowed = {span["id"] for span in task["source_spans"]}

    def validate(raw: dict[str, Any]) -> dict[str, Any]:
        items = []
        seen = set()
        cited = set()
        for i, row in enumerate(checked_rows(raw, "items"), 1):
            text = require_text(row, "text")
            if row.get("kind") not in {"requirement", "context", "process"}:
                raise ValueError("unknown inventory disposition")
            sources = checked_ids(row.get("source_ids"), allowed, "inventory sources")
            if not sources or (row["kind"], text) in seen:
                raise ValueError("empty evidence or duplicate inventory item")
            seen.add((row["kind"], text))
            cited.update(sources)
            items.append(
                {
                    "id": f"r{i:03}",
                    "text": text,
                    "kind": row["kind"],
                    "source_ids": sources,
                }
            )
        if cited != allowed:
            raise ValueError(
                f"inventory did not account for source spans {allowed - cited}"
            )
        if not any(item["kind"] == "requirement" for item in items):
            raise ValueError("no product requirements extracted")
        return {"items": items}

    return recorder.call(
        "inventory", INVENTORY_PROMPT, task, INVENTORY_SCHEMA, validate
    )


def requirements(inv: dict[str, Any]) -> list[dict[str, Any]]:
    return [item for item in inv["items"] if item["kind"] == "requirement"]


def select(
    task: dict[str, Any],
    inv: dict[str, Any],
    catalog: dict[str, Any],
    recorder: Recorder,
) -> list[dict[str, Any]]:
    allowed = {card["id"] for card in catalog["templates"]}
    required = {row["id"] for row in requirements(inv)}

    def validate(raw: dict[str, Any]) -> list[dict[str, Any]]:
        rows = checked_rows(raw, "selections")
        seen = set()
        for row in rows:
            rid = row.get("requirement_id")
            if rid not in required or rid in seen:
                raise ValueError("unknown or repeated selection requirement")
            seen.add(rid)
            checked_ids(row.get("template_ids"), allowed, "selected templates")
            require_text(row, "rationale")
        if seen != required:
            raise ValueError("selection must include every requirement, even no-match")
        return rows

    return recorder.call(
        "select",
        QUALITY
        + """
Nominate all potentially applicable templates for each requirement. Favor recall:
retain plausible candidates for binding to confirm, but do not match only on a
keyword. Several templates can apply; an empty list is a legitimate catalog miss.
Use the routed category supplied with each requirement. Match an include_prohibition
only to criteria that preserve the exact prohibited product behavior; do not turn
it into a positive requirement or prohibit additional behavior. Required rejection
of invalid input is an include requirement, not an include_prohibition. Context,
exclude, and unclear clauses are not product requirements and are never template
candidates.
Deep hierarchy history is not recursive object copying. Same value is not same
object. Sequence order does not establish conflicting-value precedence. Check
each card's prerequisites before nominating it; keep alternative templates for
the supported behavior. Return exactly one selection row for every requirement.""",
        {
            **task,
            "requirements": requirements(inv),
            "catalog": [
                {
                    **{key: card[key] for key in ("id", "name", "applies_when")},
                    "prerequisites": card.get("prerequisites", []),
                }
                for card in catalog["templates"]
            ],
        },
        SELECT_SCHEMA,
        validate,
    )


def validate_evidence(row: dict[str, Any], task: dict[str, Any]) -> None:
    """Check quotation provenance; semantic entailment still needs evaluation."""
    spans = {span["id"]: span for span in task["source_spans"]}
    ids = checked_ids(row.get("source_ids"), set(spans), "evidence sources")
    require_text(row, "support_explanation")
    quoted_ids = set()
    for item in checked_rows(row, "source_quotes"):
        sid = item.get("source_id")
        quote = require_text(item, "quote")
        if sid not in ids:
            raise ValueError(
                f"evidence quote must occur verbatim in cited span {sid}: {quote!r}"
            )
        located = locate_quote(spans[sid]["text"], quote)
        if located is None:
            raise ValueError(
                "evidence quote must be a contiguous substring of its cited "
                "source span (whitespace and backtick formatting differences "
                f"are allowed) {sid}: {quote!r}"
            )
        start, end = located
        item.update(
            quote=spans[sid]["text"][start:end],
            start=spans[sid]["start"] + start,
            end=spans[sid]["start"] + end,
        )
        quoted_ids.add(sid)
    if not ids or quoted_ids != set(ids):
        raise ValueError("every cited source span needs a separate exact quote")


def locate_quote(source: str, quote: str) -> tuple[int, int] | None:
    """Align formatting differences, preserving exact source text and offsets."""

    def normalize(text: str) -> tuple[str, list[int]]:
        chars: list[str] = []
        offsets = []
        for i, char in enumerate(text):
            if char == "`":
                continue
            char = " " if char.isspace() else char
            if char == " " and chars and chars[-1] == " ":
                continue
            chars.append(char)
            offsets.append(i)
        return "".join(chars), offsets

    direct = source.find(quote)
    if direct >= 0:
        return direct, direct + len(quote)
    normalized_source, offsets = normalize(source)
    normalized_quote, _ = normalize(quote)
    normalized_quote = normalized_quote.strip()
    if not normalized_quote:
        return None
    match = normalized_source.find(normalized_quote)
    if match < 0:
        return None
    start, end = offsets[match], offsets[match + len(normalized_quote) - 1] + 1
    while start > 0 and source[start - 1] == "`":
        start -= 1
    while end < len(source) and source[end] == "`":
        end += 1
    return start, end


def validate_bindings(
    raw: dict[str, Any],
    *,
    selected: list[dict[str, Any]],
    catalog: dict[str, Any],
    task: dict[str, Any],
) -> list[dict[str, Any]]:
    cards = {card["id"]: card for card in catalog["templates"]}
    expected = {
        (row["requirement_id"], tid) for row in selected for tid in row["template_ids"]
    }
    sources = {span["id"] for span in task["source_spans"]}
    rows = checked_rows(raw, "decisions")
    seen = set()
    for row in rows:
        pair = (row.get("requirement_id"), row.get("template_id"))
        if pair not in expected or pair in seen:
            raise ValueError("unknown or duplicate requirement/template decision")
        seen.add(pair)
        require_text(row, "reason")
        if row.get("applicability") not in {"yes", "no", "unknown"}:
            raise ValueError("unknown applicability")
        instances = checked_rows(row, "instances")
        if bool(instances) != (row["applicability"] == "yes"):
            raise ValueError("only yes decisions require nonempty instances")
        slot_specs = {slot["name"]: slot for slot in cards[pair[1]]["slots"]}
        provenance_error = None
        for instance in instances:
            expected_proofs = {
                item["id"] for item in cards[pair[1]].get("prerequisites", [])
            }
            proofs = checked_rows(instance, "prerequisites")
            supplied_proofs = set()
            for proof in proofs:
                pid = proof.get("id")
                if pid not in expected_proofs or pid in supplied_proofs:
                    raise ValueError("unknown or duplicate prerequisite evidence")
                supplied_proofs.add(pid)
                try:
                    validate_evidence(proof, task)
                except ValueError as exc:
                    provenance_error = str(exc)
                    break
            if provenance_error:
                break
            if supplied_proofs != expected_proofs:
                raise ValueError("yes binding lacks required prerequisite evidence")
            supplied = set()
            for slot in checked_rows(instance, "slots"):
                name = slot.get("name")
                if name not in slot_specs or name in supplied:
                    raise ValueError("unknown or duplicate slot")
                supplied.add(name)
                if slot.get("kind") != slot_specs[name]["kind"]:
                    raise ValueError(f"incorrect semantic slot kind: {name}")
                value = require_text(slot, "value")
                if re.search(r"\{[a-z_]+\}|\b(?:TBD|TODO)\b", value):
                    raise ValueError("unresolved placeholder in slot")
                evidence = checked_ids(slot.get("source_ids"), sources, "slot sources")
                if (
                    slot.get("basis") not in {"instruction", "illustrative"}
                    or not evidence
                ):
                    raise ValueError(
                        "slot needs a valid basis and instruction evidence"
                    )
                try:
                    validate_evidence(slot, task)
                except ValueError as exc:
                    provenance_error = str(exc)
                    break
            if provenance_error:
                break
            required = {name for name, spec in slot_specs.items() if spec["required"]}
            if not required.issubset(supplied):
                raise ValueError(
                    f"missing required slots: {sorted(required - supplied)}"
                )
        if provenance_error:
            # A broken citation cannot support a rendered criterion. Keep this
            # nominated pair as an explicit unknown and let requirement fallback
            # preserve the source behavior without failing the whole task.
            row["applicability"] = "unknown"
            row["reason"] = "Withheld because source evidence failed validation."
            row["instances"] = []
            row["provenance_validation_error"] = provenance_error
    if seen != expected:
        raise ValueError("binding must decide every nominated pair")
    return rows


def bind(
    task: dict[str, Any],
    inv: dict[str, Any],
    catalog: dict[str, Any],
    selected: list[dict[str, Any]],
    recorder: Recorder,
    batch_size: int,
) -> list[dict[str, Any]]:
    cards = {card["id"]: card for card in catalog["templates"]}
    reqs = {row["id"]: row for row in requirements(inv)}
    all_decisions = []
    for offset in range(0, len(selected), batch_size):
        batch = selected[offset : offset + batch_size]
        tids = {tid for row in batch for tid in row["template_ids"]}
        if not tids:
            continue
        decisions = recorder.call(
            f"bind-{offset // batch_size:03}",
            QUALITY
            + """
Decide applicability independently for every nominated requirement/template pair.
Respect the source route: include_prohibition criteria must state the precise
forbidden behavior, while include criteria state required product behavior.
Do not reverse or broaden either polarity. A required error/rejection for invalid
input is still include. Context and excluded clauses do not create criteria.
For yes, fill one or more instances (distinct cases can need the same template).
Fill each required slot; omit optional slots lacking support. Slot kinds must
match the card. For every filled slot, cite supplied source IDs, supply
source_quotes with one or more {source_id, quote} objects, and explain why those
clauses support the value in support_explanation. Each quote must be copied
verbatim from its named source span. Every cited span needs a quote. Use separate
entries for evidence from separate clauses/spans; never concatenate excerpts or
paraphrase a quote. Related subject matter is not sufficient.
For each yes instance, prove every card prerequisite in prerequisites with its
exact id, source IDs, source_quotes, and explanation. Cards without prerequisites
use an empty list. If a prerequisite lacks support, return no or unknown with
no instances; do not manufacture evidence. Read-only/equal contents do not mean
same reference. Array order does not mean a collision winner. Preserve operation
scope: a winner for one operation does not establish it for a different mode.
Illustrative fixtures
must illustrate a supported general rule, not add an obligation. Never put
'unspecified' into a required slot to force a match: choose unknown instead.
Use no for a false candidate match, unknown for insufficient evidence. Negative
decisions have no instances. Do not write final prose: the renderer owns that.
Write slot values as grammatical replacements in the supplied sentences.""",
            {
                **task,
                "requirements": [reqs[row["requirement_id"]] for row in batch],
                "selections": batch,
                "cards": [cards[tid] for tid in sorted(tids)],
            },
            BIND_SCHEMA,
            partial(validate_bindings, selected=batch, catalog=catalog, task=task),
        )
        all_decisions.extend(decisions)
    from .guarding import confirm_guards

    all_decisions = confirm_guards(task, inv, catalog, all_decisions, recorder)
    for decision in all_decisions:
        decision["route"] = reqs[decision["requirement_id"]].get("route")
        decision["polarity"] = reqs[decision["requirement_id"]].get("polarity")
    return all_decisions


def render_instance(card: dict[str, Any], instance: dict[str, Any]) -> str:
    values = {slot["name"]: slot["value"] for slot in instance["slots"]}
    clauses = [card["required_sentence"]]
    clauses.extend(
        sentence
        for sentence in card["optional_sentences"]
        if set(placeholders(sentence)).issubset(values)
    )
    return normalized(" ".join(sentence.format_map(values) for sentence in clauses))


def render_bindings(
    decisions: list[dict[str, Any]], catalog: dict[str, Any]
) -> list[dict[str, Any]]:
    cards = {card["id"]: card for card in catalog["templates"]}
    criteria = []
    for row in decisions:
        for instance in row["instances"]:
            criteria.append(
                {
                    "text": render_instance(cards[row["template_id"]], instance),
                    "requirement_ids": [row["requirement_id"]],
                    "route": row.get("route"),
                    "polarity": row.get("polarity"),
                    "source_ids": sorted(
                        {
                            source
                            for slot in instance["slots"]
                            for source in slot["source_ids"]
                        }
                    ),
                    "template_ids": [row["template_id"]],
                    "slots": instance["slots"],
                }
            )
    return deduplicate(criteria)


def deduplicate(criteria: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: dict[tuple[str | None, str], dict[str, Any]] = {}
    for row in criteria:
        key = (row.get("route"), normalized(row["text"]))
        if key in result:
            for field in ("requirement_ids", "source_ids", "template_ids"):
                result[key][field] = sorted(set(result[key][field]) | set(row[field]))
            result[key]["bindings"].append(row.get("slots", []))
        else:
            result[key] = {
                **row,
                "id": f"c{len(result) + 1:03}",
                "bindings": [row.get("slots", [])],
            }
    return list(result.values())


def direct(
    task: dict[str, Any], inv: dict[str, Any], recorder: Recorder, batch_size: int
) -> list[dict[str, Any]]:
    reqs = requirements(inv)
    all_rows = []
    for offset in range(0, len(reqs), batch_size):
        batch = reqs[offset : offset + batch_size]
        allowed = {row["id"] for row in batch}
        sources = {span["id"] for span in task["source_spans"]}

        def validate(
            raw: dict[str, Any],
            allowed: set[str] = allowed,
            sources: set[str] = sources,
        ) -> list[dict[str, Any]]:
            rows = checked_rows(raw, "criteria")
            for row in rows:
                require_text(row, "text")
                rids = checked_ids(
                    row.get("requirement_ids"), allowed, "direct requirements"
                )
                sids = checked_ids(row.get("source_ids"), sources, "direct sources")
                if not rids or not sids:
                    raise ValueError(
                        "direct criterion needs requirement/source evidence"
                    )
                row["template_ids"] = []
            return rows

        all_rows.extend(
            recorder.call(
                f"direct-{offset // batch_size:03}",
                QUALITY
                + """
Produce one or more acceptance criteria for each supplied requirement using your
own prose. Include required normal, invalid, exceptional, and boundary behavior
with observable expected outcomes. You may combine related predicates with a
shared setup but must preserve their conditions. Do not refer to templates.
If a required outcome is genuinely unspecified, preserve the ambiguity rather
than filling it with invented details.""",
                {**task, "requirements": batch},
                DIRECT_SCHEMA,
                validate,
            )
        )
    return deduplicate(all_rows)


def generate(
    task: dict[str, Any],
    inv: dict[str, Any],
    catalog: dict[str, Any],
    recorder: Recorder,
    arm: str,
    batch_size: int,
) -> dict[str, Any]:
    selected: list[dict[str, Any]] = []
    decisions: list[dict[str, Any]] = []
    if arm == "templates":
        selected = select(task, inv, catalog, recorder)
        decisions = bind(task, inv, catalog, selected, recorder, batch_size)
        criteria = render_bindings(decisions, catalog)
    else:
        criteria = direct(task, inv, recorder, batch_size)
    represented = {rid for row in criteria for rid in row["requirement_ids"]}
    residual = [row for row in requirements(inv) if row["id"] not in represented]
    return {
        "schema_version": SCHEMA_VERSION,
        "task_id": task["task_id"],
        "arm": arm,
        "prompt_version": PROMPT_VERSION,
        "status": "completed",
        "input_sha256": digest(task),
        "catalog_sha256": digest(catalog),
        "inventory_sha256": digest(inv),
        "inventory": inv,
        "criteria": criteria,
        "residual_requirements": residual,
        "selections": selected,
        "decisions": decisions,
        "calls": recorder.calls,
    }


def save_generation(directory: Path, result: dict[str, Any]) -> None:
    previous = directory / "generation.json"
    if previous.exists():
        prior = load_json(previous)
        if digest(prior) != digest(result):
            history = directory / "history" / digest(prior)
            history.mkdir(parents=True, exist_ok=True)
            for name in ("generation.json", "prompt.md"):
                if (directory / name).exists():
                    shutil.copy2(directory / name, history / name)
            review_path = directory / "review.json"
            if review_path.exists() and load_json(review_path).get(
                "generation_sha256"
            ) == digest(prior):
                shutil.copy2(review_path, history / "review.json")
    write_json(directory / "generation.json", result)
    lines = [
        f"# {result['task_id']}: {result['arm']}",
        "",
        "## Acceptance criteria",
        "",
    ]
    lines.extend(f"- {row['text']}" for row in result["criteria"])
    if result["residual_requirements"]:
        lines += ["", "## Requirements retained without generated criteria", ""]
        lines.extend(f"- {row['text']}" for row in result["residual_requirements"])
    if result.get("unresolved_route_items"):
        lines += ["", "## Requirements with unresolved routing", ""]
        lines.extend(f"- {row['text']}" for row in result["unresolved_route_items"])
    (directory / "prompt.md").write_text("\n".join(lines) + "\n")


def load_catalog(path: Path) -> dict[str, Any]:
    catalog = load_json(path)
    cards = catalog.get("templates")
    if not isinstance(cards, list) or len({card["id"] for card in cards}) != len(cards):
        raise ValueError("catalog requires unique template IDs")
    for card in cards:
        prerequisites = card.get("prerequisites", [])
        if len({item["id"] for item in prerequisites}) != len(prerequisites):
            raise ValueError("duplicate catalog prerequisites")
        for item in prerequisites:
            for field in ("id", "claim", "reject_inference"):
                require_text(item, field)
            if "relation_classes" in item:
                require_text(item, "question")
                classes = item["relation_classes"]
                if (
                    not isinstance(classes, dict)
                    or not {"unspecified", "uncertain"} <= classes.keys()
                    or item.get("required_relation") not in classes
                    or any(
                        not isinstance(v, str) or not v.strip()
                        for v in classes.values()
                    )
                ):
                    raise ValueError("invalid semantic relation classes")
        names = {slot["name"] for slot in card["slots"]}
        used = set(
            placeholders(
                " ".join([card["required_sentence"], *card["optional_sentences"]])
            )
        )
        if names != used or any(
            slot["required"]
            != (slot["name"] in placeholders(card["required_sentence"]))
            for slot in card["slots"]
        ):
            raise ValueError(f"inconsistent renderer slots: {card['id']}")
    return catalog
