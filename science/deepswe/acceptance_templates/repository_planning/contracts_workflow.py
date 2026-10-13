"""Explicit decisions separating existing contracts, desired behavior and criteria."""

from typing import Any

from science.deepswe.acceptance_templates.common import choice_schema, object_schema

TEXT = {"type": "string"}
STRINGS = {"type": "array", "items": TEXT}
CHOICE = object_schema(choice=choice_schema("supported", "contradicted", "unknown"))
FACTS = object_schema(
    facts={
        "type": "array",
        "maxItems": 4,
        "items": object_schema(
            category=choice_schema("owner", "compatibility", "explicit_change"),
            statement=TEXT,
            applies_to=STRINGS,
            citations=STRINGS,
        ),
    }
)
CRITERIA = object_schema(
    criteria={
        "type": "array",
        "minItems": 0,
        "maxItems": 2,
        "items": object_schema(
            given=TEXT, when=TEXT, then=TEXT, rejects=TEXT, query=TEXT
        ),
    }
)


def ref(name: str) -> dict[str, str]:
    return {"type": "reference", "value": name}


def op(name: str, bind: str, **kwargs: Any) -> dict[str, Any]:
    return {
        "operation": {
            "tool": "internal",
            "command": [name],
            "parameters": kwargs,
            "bind": bind,
        }
    }


def judge(
    name: str,
    question: str,
    instruction: str,
    context: list[str],
    schema: dict[str, Any],
    provider: str = "flash",
) -> dict[str, Any]:
    return {
        "judge": {
            "question": question,
            "subject": context[0],
            "prompt_system": "Return only the declared JSON object.",
            "instructions": [instruction],
            "context": context,
            "provider": provider,
            "output": {"name": name, "schema": schema},
            "validation": {"kind": "json_schema"},
        }
    }


def loop(
    items: str,
    item: str,
    collect: str,
    value: str,
    body: list[dict[str, Any]],
    maximum: int,
) -> dict[str, Any]:
    return {
        "for_each": {
            "snapshot": {"name": items, "max_items": maximum},
            "item_binding": item,
            "collect": {"binding": collect, "mode": "list", "value": value},
            "body": body,
        }
    }


def definition() -> dict[str, Any]:
    criteria_body = [
        op("check_examples", "example_check", criterion=ref("criterion")),
        judge(
            "premise",
            "Is this criterion's setup a valid use of the requested feature?",
            (
                "Check API receivers, signatures, complete inputs, wire "
                "envelopes and observation layer. New APIs requested by the "
                "instruction need not exist yet. Existing wire conventions are "
                "scoped to their specific operation; do not transfer an "
                "unrelated operation's format. supported=valid, "
                "contradicted=invalid, unknown=insufficient evidence. Do not "
                "answer the outcome question."
            ),
            ["instruction", "requirement", "criterion", "contracts", "example_check"],
            CHOICE,
            "jev",
        ),
        judge(
            "review",
            "Does this expected outcome unambiguously verify the requested behavior?",
            (
                "Evaluate the specified feature after implementation. The "
                "instruction defines the new behavior; cited existing contracts "
                "constrain compatibility. Missing implementation never "
                "invalidates a requested outcome. Check every assertion, "
                "observation point and whether rejects describes a genuinely "
                "incorrect implementation. supported=correct and "
                "discriminating, contradicted=incorrect/unsupported, "
                "unknown=underdetermined. Do not require exact words from the "
                "source, but do not invent policy."
            ),
            ["instruction", "requirement", "behavior", "criterion", "contracts"],
            CHOICE,
            "jev",
        ),
        op(
            "record_contract_criterion",
            "record",
            requirement=ref("requirement"),
            behavior=ref("behavior"),
            criterion=ref("criterion"),
            premise=ref("premise"),
            review=ref("review"),
            example_check=ref("example_check"),
        ),
    ]
    requirement_body = [
        op("requirement_contracts", "contracts", requirement=ref("requirement")),
        judge(
            "behavior",
            "After implementing this requirement, what observable behavior must hold?",
            (
                "Derive the intended post-change behavior from the original "
                "instruction, this requirement and relevant verified contracts. "
                "Routed clauses are an index: the original instruction governs "
                "scope and grammatical attachments; do not amplify a splitter "
                "attachment error. Code is not an oracle of new behavior. An "
                "explicitly requested feature has a required future behavior "
                "even when absent today. Resolve the full meaning of the "
                "requirement, including interactions that determine its "
                "behavior. Explain outcomes and observation boundaries, not "
                "implementation steps. Preserve only contracts actually scoped "
                "to the affected API. Distinguish support for a type/value from "
                "constructing absent structure or specifying a new error "
                "policy. Do not invent limits or error policies. Produce the "
                "normative behavior; do not assess whether existing code "
                "already implements it."
            ),
            ["instruction", "requirement", "contracts"],
            object_schema(behavior=TEXT),
        ),
        judge(
            "proposal",
            (
                "Which concrete acceptance criteria distinguish correct "
                "behavior from plausible incorrect implementations?"
            ),
            (
                "Generate one or two given/when/then criteria for the supplied "
                "behavior. Each criterion has one operation and a precise "
                "observation point. Fully specify relevant prior state and "
                "every input payload, including transport, fields and flags "
                "when relevant. Choose scenarios where a plausible wrong "
                "implementation produces a different observable result; "
                "describe that wrong implementation in rejects. Use collisions, "
                "retained values, scope or observation timing only when the "
                "behavior makes them relevant. Do not multiply identical easy "
                "examples. query is an exact syntactically valid GraphQL "
                "document only if needed; otherwise empty. Do not restate query "
                "in given. then states exact expected observations, without "
                "may/or alternatives. Existing absence is never a reason to "
                "omit a requested behavior. Do not transfer a legacy "
                "operation's wire envelope to a new operation without evidence."
            ),
            ["instruction", "requirement", "behavior", "contracts"],
            CRITERIA,
        ),
        op(
            "bind_contract_criteria",
            "criteria_list",
            proposal=ref("proposal"),
            requirement=ref("requirement"),
        ),
        loop(
            "criteria_list",
            "criterion",
            "requirement_records",
            "record",
            criteria_body,
            2,
        ),
        op(
            "record_requirement_coverage",
            "coverage",
            requirement=ref("requirement"),
            behavior=ref("behavior"),
            records=ref("requirement_records"),
        ),
    ]
    group_body = [
        op("retrieve_candidates", "candidates", index=ref("index"), batch=ref("batch")),
        judge(
            "selection",
            "Which candidate declarations establish relevant existing API contracts?",
            (
                "Select supplied symbol references for APIs, compatible "
                "options, result shapes and lifecycle behavior implicated by "
                "this requirement group. New API names may be absent; select "
                "relevant existing neighbors. Do not decide new behavior here."
            ),
            ["instruction", "batch", "candidates"],
            object_schema(symbols=STRINGS),
        ),
        op(
            "gather_evidence",
            "evidence",
            candidates=ref("candidates"),
            selection=ref("selection"),
            batch=ref("batch"),
        ),
        judge(
            "fact_proposal",
            (
                "What existing contracts are relevant to these requirements, "
                "and which are explicitly changed?"
            ),
            (
                "Return at most four useful cited facts, not a quota. owner "
                "identifies the existing API/class/layer and its signature; "
                "compatibility describes a relevant existing contract whose "
                "scope remains applicable; explicit_change identifies an "
                "instruction-mandated change. Scope each statement to its "
                "actual operation and applies_to requirement references. Cite "
                "source references from evidence; use instruction for explicit "
                "changes. Existing absence is not a compatibility constraint. "
                "Unrelated helpers and legacy wire formats are not contracts "
                "for a new operation. Prefer options, return shapes and cleanup "
                "obligations the coding agent must preserve. Do not prescribe a "
                "new implementation or infer requested outcomes at this step."
            ),
            ["instruction", "batch", "evidence"],
            FACTS,
        ),
        op(
            "bind_contract_facts",
            "fact_list",
            proposal=ref("fact_proposal"),
            batch=ref("batch"),
            evidence=ref("evidence"),
        ),
        loop(
            "fact_list",
            "fact",
            "fact_records",
            "fact_record",
            [
                judge(
                    "fact_review",
                    (
                        "Is this a correctly scoped and relevant "
                        "existing-contract fact or explicit instruction "
                        "override?"
                    ),
                    (
                        "Check statement and applicability against source and "
                        "instruction. supported=correct, relevant and properly "
                        "scoped; contradicted=incorrect scope, unrelated "
                        "detail, invented obligation or transfer of a different "
                        "operation's contract; unknown=insufficient evidence. "
                        "Existing absence never constrains implementing a "
                        "requested feature."
                    ),
                    ["instruction", "fact", "evidence"],
                    CHOICE,
                    "jev",
                ),
                op(
                    "record_contract_fact",
                    "fact_record",
                    fact=ref("fact"),
                    review=ref("fact_review"),
                ),
            ],
            4,
        ),
    ]
    requirement_body[1]["judge"]["instructions"].append(
        "Keep the analysis centered on this one requirement. Neighboring "
        "requirements supply necessary context, not a checklist to repeat."
    )
    requirement_body[2]["judge"]["instructions"].append(
        "Assert only the active requirement and necessary interactions. "
        "Omit unrelated fields and unstated default values."
    )
    return {
        "version": 1,
        "name": "requirement-contract-criteria",
        "inputs": [
            {"name": "instruction", "type": "string", "required": True},
            {"name": "batches", "type": "array", "required": True},
            {"name": "requirements", "type": "array", "required": True},
        ],
        "limits": {
            "llm_activations": 250,
            "tool_calls": 250,
            "context_chars": 120000,
            "context_value_chars": 100000,
        },
        "steps": [
            op("extract_symbols", "index"),
            loop("batches", "batch", "group_records", "fact_records", group_body, 10),
            loop(
                "requirements",
                "requirement",
                "coverage_records",
                "coverage",
                requirement_body,
                40,
            ),
            op("prepare_overlap_pairs", "overlap_pairs"),
            loop(
                "overlap_pairs",
                "pair",
                "overlap_records",
                "overlap_record",
                [
                    judge(
                        "overlap_decision",
                        (
                            "Does one criterion completely cover the other's "
                            "observable condition?"
                        ),
                        (
                            "Choose left_covers_right or right_covers_left only "
                            "when the keeper preserves every behavior, input "
                            "condition and observation boundary verified by the "
                            "other. Similar topic, wording or overlapping "
                            "requirements is insufficient. Different list "
                            "positions, payload layers, lifecycle times or "
                            "omitted fields can distinguish separate "
                            "conditions. Choose distinct when both add "
                            "coverage; unknown when equivalence is uncertain. "
                            "Do not rewrite either criterion."
                        ),
                        ["instruction", "pair"],
                        object_schema(
                            choice=choice_schema(
                                "left_covers_right",
                                "right_covers_left",
                                "distinct",
                                "unknown",
                            )
                        ),
                        "jev",
                    ),
                    op(
                        "record_overlap",
                        "overlap_record",
                        pair=ref("pair"),
                        decision=ref("overlap_decision"),
                    ),
                ],
                30,
            ),
            op("merge_exact_criteria", "merge_result"),
            op("render_prompt", "prompt"),
            {"terminal": "succeeded"},
        ],
    }
