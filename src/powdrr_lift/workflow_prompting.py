"""Shared action-selection prompts for workflow agents."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from powdrr_lift.basedpyright_tools import (
    BASEDPYRIGHT_STRUCTURE_TOOL,
    BASEDPYRIGHT_SYMBOL_TOOL,
)
from powdrr_lift.intrinsic_edit import APPLY_EDIT_TOOL, VALIDATE_EDIT_TOOL
from powdrr_lift.intrinsic_enrich import ENRICH_TOOL
from powdrr_lift.intrinsic_git_gh import GH_TOOL, GIT_TOOL
from powdrr_lift.process.catalog import SkillCatalogEntry
from powdrr_lift.process.step_behavior import behavior_for_step
from powdrr_lift.workflow_llm import prune_execution_events, workflow_action_signature
from powdrr_lift.workflow_paths import resolve_worktree_file_path
from powdrr_lift.workrr.context import WorkflowContext

_INTERNAL_TOOL = "internal"

_MAX_PROMPT_TRANSCRIPT_ENTRIES = 12
_MAX_PROMPT_TRANSCRIPT_CHARS = 12000
_MAX_PROMPT_TRANSCRIPT_MESSAGE_CHARS = 8000
_MAX_PROMPT_STEP_CONTEXT_ENTRIES = 24
_MAX_PROMPT_STEP_CONTEXT_CHARS = 16000
_MAX_PROMPT_FILE_LINES = 200
_MAX_PROMPT_FILE_CHARS = 16000


def _current_file_context(
    worktree_root: Path,
    current_file_path: Path | None,
    *,
    cache: dict[tuple[str, int, int], dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    if current_file_path is None:
        return None

    resolved_path = resolve_worktree_file_path(
        str(current_file_path),
        worktree_root,
    )
    if not resolved_path.exists():
        return {
            "path": str(resolved_path.relative_to(worktree_root)),
            "exists": False,
        }
    if not resolved_path.is_file():
        return {
            "path": str(resolved_path.relative_to(worktree_root)),
            "exists": False,
        }

    stat = resolved_path.stat()
    cache_key = (str(resolved_path), stat.st_mtime_ns, stat.st_size)
    if cache is not None and cache_key in cache:
        return cache[cache_key]
    lines = resolved_path.read_text(encoding="utf-8").splitlines()
    serialized_size = sum(len(line) for line in lines)
    content_omitted = (
        len(lines) > _MAX_PROMPT_FILE_LINES or serialized_size > _MAX_PROMPT_FILE_CHARS
    )
    prompt_lines = [] if content_omitted else lines
    context = {
        "path": str(resolved_path.relative_to(worktree_root)),
        "exists": True,
        "line_count": len(lines),
        "lines": [
            {
                "line_number": line_number,
                "text": line,
            }
            for line_number, line in enumerate(prompt_lines, start=1)
        ],
    }
    if content_omitted:
        context.update(
            {
                "content_omitted": True,
                "content_omitted_reason": (
                    "Use read_document to inspect the required line range."
                ),
            }
        )
    if cache is not None:
        cache.clear()
        cache[cache_key] = context
    return context


def _available_work_item_names(worktree_root: Path) -> tuple[str, ...]:
    specifications_root = worktree_root / "docs" / "proposals"
    if not specifications_root.is_dir():
        return ()
    return tuple(
        sorted(
            path.name
            for path in specifications_root.iterdir()
            if path.is_dir() and not path.name.startswith(".")
        )
    )


def _available_work_item_documents(
    worktree_root: Path,
    work_item_name: str,
) -> tuple[str, ...]:
    work_item_root = worktree_root / "docs" / "proposals" / work_item_name
    if not work_item_root.is_dir():
        return ()
    return tuple(
        sorted(
            str(path.relative_to(worktree_root))
            for path in work_item_root.rglob("*")
            if path.is_file()
        )
    )


def _effective_interaction_style(
    selected_skill: SkillCatalogEntry,
    current_step: Any,
    inherited_style: str | None = None,
) -> str | None:
    return (
        getattr(current_step, "interaction_style", None)
        or selected_skill.skill.interaction_style
        or inherited_style
    )


def _match_work_item_names(
    transcript: Sequence[dict[str, str]],
    work_item_names: Sequence[str],
) -> tuple[str, ...]:
    request_text = " ".join(
        message.get("content", "")
        for message in transcript
        if message.get("role") == "user"
    )
    request_tokens = _work_item_name_tokens(request_text)
    matches: list[str] = []
    for work_item_name in work_item_names:
        name_tokens = _work_item_name_tokens(work_item_name)
        if not name_tokens:
            continue
        token_count = len(name_tokens)
        contiguous_match = any(
            request_tokens[index : index + token_count] == name_tokens
            for index in range(len(request_tokens) - token_count + 1)
        )
        if contiguous_match or (
            token_count > 1 and all(token in request_tokens for token in name_tokens)
        ):
            matches.append(work_item_name)
    return tuple(matches)


def _work_item_name_tokens(value: str) -> tuple[str, ...]:
    return tuple(re.findall(r"[a-z0-9]+", value.casefold()))


def _workflow_handoff_inputs(
    step: Any,
    records: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    return {
        "declared": [input_spec.to_data() for input_spec in step.inputs],
        "resolved": {
            input_spec.name: records[input_spec.name]
            for input_spec in step.inputs
            if input_spec.name in records
        },
        "missing_required": [
            input_spec.name
            for input_spec in step.inputs
            if input_spec.required and input_spec.name not in records
        ],
    }


def _skill_step_to_data(step: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "description": step.description,
        "step_type": getattr(step, "step_type", "governed"),
        "details": step.details,
        "uses_skill": (
            step.uses_skill.to_data()
            if getattr(step, "uses_skill", None) is not None
            else None
        ),
    }
    if step.id is not None:
        data["id"] = step.id
    if step.tool_invocations:
        data["tool_invocations"] = [
            _tool_invocation_to_data(tool_invocation)
            for tool_invocation in step.tool_invocations
        ]
    if getattr(step, "pre_step", None) is not None:
        data["pre_step"] = step.pre_step.to_data()
    if getattr(step, "coding_loop", None) is not None:
        data["coding_loop"] = step.coding_loop.to_data()
    return data


def _tool_invocation_to_data(tool_invocation: Any) -> dict[str, Any]:
    return tool_invocation.to_data()


def _workflow_context_prompt_data(
    workflow_context: WorkflowContext | None,
) -> dict[str, object] | None:
    if workflow_context is None:
        return None
    data = {
        "branch_name": workflow_context.branch_name,
        "pr_number": workflow_context.pr_number,
        "pr_url": workflow_context.pr_url,
        "skill_name": workflow_context.skill_name,
        "request": workflow_context.request,
    }
    return {key: value for key, value in data.items() if value is not None}


def _execution_events_for_prompt(
    execution_events: Sequence[dict[str, Any]],
    current_step_index: int | None = None,
) -> list[dict[str, Any]]:
    """Return the event metadata needed for the next action decision.

    Event results are retained in the full execution summary, but are also
    copied into the transcript or execution context as they are produced.
    Sending both copies on every roundtrip needlessly grows prompts and makes
    large tool results increasingly expensive to serialize. Keep the prompt
    event stream as metadata while leaving the complete event stream intact
    for persistence and diagnostics.
    """
    events = (
        [
            event
            for event in execution_events
            if event.get("step_index") == current_step_index
        ]
        if current_step_index is not None
        else execution_events
    )
    return [
        {key: value for key, value in event.items() if key != "decisions_and_context"}
        for event in prune_execution_events(events, include_results=False)
    ]


def _successful_document_reads_for_prompt(
    execution_events: Sequence[Mapping[str, Any]],
    current_step_index: int | None = None,
) -> list[dict[str, Any]]:
    """Expose successful reads as durable repair context.

    Compact event metadata intentionally omits results. Repairs still need to
    know which documents already supplied context so they do not spend a retry
    rereading the same file instead of correcting the failed capability call.
    """
    reads: list[dict[str, Any]] = []
    for event in execution_events:
        if event.get("kind") != "read_document":
            continue
        if current_step_index is not None and event.get("step_index") not in {
            None,
            current_step_index,
        }:
            continue
        result = event.get("result")
        if not isinstance(result, Mapping):
            continue
        path = result.get("path")
        if not isinstance(path, str) or not path:
            continue
        reads.append(
            {
                "path": path,
                "requested_start_line": result.get("requested_start_line"),
                "requested_end_line": result.get("requested_end_line"),
                "returned_end_line": result.get("end_line"),
            }
        )
    return reads


def _latest_execution_event_for_prompt(
    execution_events: Sequence[dict[str, Any]],
    current_step_index: int | None = None,
) -> dict[str, Any] | None:
    """Retain the latest result separately from the compact event metadata."""
    if current_step_index is not None:
        execution_events = [
            event
            for event in execution_events
            if event.get("step_index") == current_step_index
        ]
    if not execution_events:
        return None
    latest = prune_execution_events(execution_events[-1:], include_results=True)
    if not latest:
        return None
    return {
        key: value for key, value in latest[0].items() if key != "decisions_and_context"
    }


_PROMPT_OBSERVATION_RESULT_KEYS = {
    "tool_result",
    "edit_result",
    "yaml_edit_result",
    "document_context",
}


def _is_prompt_observation_message(message: Mapping[str, str]) -> bool:
    """Identify action/result transcript entries represented by event state."""
    content = message.get("content", "")
    try:
        decoded = json.loads(content)
    except (TypeError, ValueError):
        return False
    if not isinstance(decoded, Mapping):
        return False
    if message.get("role") == "assistant":
        return isinstance(decoded.get("action", decoded.get("kind")), str)
    return bool(_PROMPT_OBSERVATION_RESULT_KEYS.intersection(decoded))


def _prompt_transcript(
    transcript: Sequence[dict[str, str]],
) -> list[dict[str, str]]:
    """Keep recurring prompts bounded while retaining the complete transcript."""
    conversational = [
        message for message in transcript if not _is_prompt_observation_message(message)
    ]
    if len(conversational) <= _MAX_PROMPT_TRANSCRIPT_ENTRIES:
        return conversational

    first = {
        **conversational[0],
        "content": _truncate_prompt_content(conversational[0].get("content", "")),
    }
    recent = [
        {
            **message,
            "content": _truncate_prompt_content(message.get("content", "")),
        }
        for message in conversational[-(_MAX_PROMPT_TRANSCRIPT_ENTRIES - 2) :]
    ]
    omitted = {
        "role": "user",
        "content": "[Earlier workflow transcript omitted from this prompt; "
        "full history remains in the execution summary.]",
    }
    compacted = [first, omitted, *recent]
    while (
        len(compacted) > 3
        and sum(len(message.get("content", "")) for message in compacted)
        > _MAX_PROMPT_TRANSCRIPT_CHARS
    ):
        compacted.pop(2)
    return compacted


def _prompt_step_context(
    execution_context: Sequence[str],
    durable_facts: Mapping[str, Mapping[str, Any]] | None = None,
    execution_events: Sequence[Mapping[str, Any]] = (),
    current_step_index: int | None = None,
) -> list[str]:
    """Bound recurring step context while retaining the newest handoff facts."""
    result_prefixes = (
        "Gathered context:\n",
        "Deterministic pre-step gather_context result:\n",
        "Document context: ",
        "Gate failed: ",
    )
    keep_current_gather = any(
        event.get("kind") == "gather_context"
        and event.get("step_index") == current_step_index
        for event in execution_events
    )
    execution_context = [
        value
        for value in execution_context
        if keep_current_gather
        and value.startswith("Gathered context:\n")
        or not value.startswith(result_prefixes)
    ]
    fact_values = {
        str(record.get("value"))
        for record in (durable_facts or {}).values()
        if record.get("value") is not None
    }
    execution_context = [
        value
        for value in execution_context
        if " ".join(value.split()) not in fact_values
    ]
    if len(execution_context) <= _MAX_PROMPT_STEP_CONTEXT_ENTRIES:
        recent = list(execution_context)
    else:
        recent = list(execution_context[-_MAX_PROMPT_STEP_CONTEXT_ENTRIES:])
    while (
        len(recent) > 1
        and sum(len(value) for value in recent) > _MAX_PROMPT_STEP_CONTEXT_CHARS
    ):
        recent.pop(0)
    return recent


def _prompt_durable_facts(
    durable_facts: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Return deduplicated durable facts in a compact, stable prompt shape."""
    facts = list(durable_facts.values())[-_MAX_PROMPT_STEP_CONTEXT_ENTRIES:]
    return [dict(fact) for fact in facts]


def _truncate_prompt_content(content: str) -> str:
    if len(content) <= _MAX_PROMPT_TRANSCRIPT_MESSAGE_CHARS:
        return content
    half_limit = _MAX_PROMPT_TRANSCRIPT_MESSAGE_CHARS // 2
    return (
        content[:half_limit]
        + "\n... [prompt transcript message truncated] ...\n"
        + content[-half_limit:]
    )


_INTERACTION_STYLE_GUIDANCE: dict[str, str] = {
    "engineering": (
        "Be concise and implementation-oriented. State assumptions, choose the "
        "smallest scoped change, preserve existing contracts, and verify changes "
        "with relevant tests or commands."
    ),
    "observational_review": (
        "Inspect evidence before making recommendations. Separate observations, "
        "inferences, risks, and recommendations. Cite concrete files, lines, tests, "
        "or command results. Do not edit unless the current step authorizes edits."
    ),
    "devils_advocate": (
        "Intentionally challenge the proposed change. Look for hidden assumptions, "
        "regressions, missing requirements, and simpler alternatives. Treat the "
        "change as untrusted until evidence supports it. Label counterarguments as "
        "risks or objections, and do not edit unless the current step authorizes it."
    ),
}


def interaction_style_prompt(style: str | None) -> str:
    if style is None:
        return ""
    guidance = _INTERACTION_STYLE_GUIDANCE[style]
    return (
        "Interaction style: "
        f"{style}.\n"
        "Style guidance: "
        f"{guidance}\n"
        "This guidance changes reasoning posture and communication only; the "
        "current step contract, allowed actions, gates, and validation rules remain "
        "authoritative.\n"
    )


def _step_needs_prompt_catalog(step: Any, capability: str) -> bool:
    configured_catalogs = getattr(step, "prompt_catalogs", ())
    if capability not in {"context_types", "skills", "actions"}:
        raise ValueError(f"Unknown prompt catalog capability: {capability}")
    return capability in configured_catalogs


def _action_system_prompt(*, current_step: Any | None = None) -> str:
    predicated_step = (
        current_step is not None and behavior_for_step(current_step).is_predicated
    )
    completion_guidance = (
        "- predicated completion: never return next_step. Choose one declared "
        "work action and include completed handoff values in the top-level "
        "outputs object, for example "
        '{"action":"emit_outputs","outputs":{"result":{}}}. '
        "The runtime advances automatically as soon as every required output "
        "is present; if more work is needed, omit that output and continue.\n"
        if predicated_step
        else "- next_step: choose this when the current step is complete and the next "
        "skill step should receive the accumulated context.\n"
    )
    context_completion_guidance = (
        ""
        if predicated_step
        else (
            "After gathering context, include the relevant findings in "
            "decisions_and_context and report next_step when the current step is "
            "complete; do not leave the gathered result only in the tool history.\n"
        )
    )
    goto_next_action_guidance = (
        ""
        if predicated_step
        else (
            "goto_step takes a step_id matching an id on a step in the current "
            "skill; next_step has no action-specific fields; "
        )
    )
    next_step_example = (
        ""
        if predicated_step
        else (
            '{"action":"next_step","decisions_and_context":"...",'
            '"llm_type":"standard_reasoning"}\n'
        )
    )
    required_action_guidance = ""
    if predicated_step:
        requirements = tuple(
            getattr(getattr(current_step, "completion", None), "required_actions", ())
        )
        if requirements:
            obligation_lines = []
            for requirement in requirements:
                cardinality = (
                    f" exactly {requirement.exactly} time(s)"
                    if requirement.exactly is not None
                    else " at least once"
                )
                parameters = (
                    " with parameters "
                    + json.dumps(requirement.parameters, sort_keys=True)
                    if requirement.parameters is not None
                    else ""
                )
                target = (
                    f" for every target from {requirement.targets_from}"
                    if requirement.targets_from is not None
                    else ""
                )
                obligation_lines.append(
                    f"  - {requirement.action}{cardinality}{parameters}{target}"
                )
            required_action_guidance = (
                "Before emit_outputs, complete every required action obligation "
                "below. Do not emit outputs early; the runtime will reject them.\n"
                + "\n".join(obligation_lines)
                + "\n"
            )
    if current_step is None or _step_needs_prompt_catalog(
        current_step, "context_types"
    ):
        context_type_lines = "\n".join(
            f"- {name}: {description}" for name, description in _context_type_catalog()
        )
    else:
        context_type_lines = (
            "Context-type descriptions are omitted because this step does not "
            "request context."
        )
    return (
        "Task: execute the current checked-in skill step using the current step, "
        "prior step context, transcript, execution events, available tools, and "
        "latest_action result, and current file context in the user message. "
        "Choose the single next action "
        "that makes the most progress without asking for information already "
        "available.\n"
        "If stalled_step_context is non-empty, the current step is a fresh retry "
        "after its prior actions were discarded. Treat each recorded stalled "
        "action and its reason as a hard constraint: do not return the same action "
        "again, even with different narrative context. Choose a materially "
        "different action or a different valid route through the current step.\n"
        "When current_step.uses_skill is present, that skill runs automatically "
        "in the same worktree before you continue the current step. Use invoke_skill "
        "only for an additional listed skill that the current step discovers it "
        "needs.\n"
        "Choose exactly one outcome and use it for the following reason:\n"
        "- gather_context: choose this when checked-in specifications or other "
        "repository context must be discovered before deciding or acting.\n"
        + context_completion_guidance
        + "When a feature's proposal must be scoped, pass its feature_id to "
        "gather_context. It includes current specifications and only YAML files "
        "under docs/proposals/<feature_id>. Do not use fuzzy-match to locate the "
        "feature proposal or substitute another feature's proposal.\n"
        "- prompt_user: choose this only when a specific human decision or fact "
        "is genuinely required to continue; ask exactly one clear question.\n"
        "- edit: choose this when the current file context is sufficient and the "
        "next action is a line-based file change.\n"
        "- file_management: choose this to move or rename one existing "
        "regular file. Paths must be relative to the current worktree, must not "
        "contain '..', and move/rename requires destination_path.\n"
        "- delete_file: choose this to delete one existing regular file; provide "
        "only its relative file_path.\n"
        "- invoke_skill: choose this when a listed skill should run as a nested "
        "workflow before continuing. It inherits the current context and LLM "
        "provider role by default, including the current skill's adversarial "
        "role. Pass the current decisions and context to the nested skill; "
        'set provider_role="adversarial" to '
        "run this skill and its descendants with the adversarial provider, or "
        'provider_role="normal" to return to the normal provider. '
        "its descendants. Set clean=true only when the skill must receive only the "
        "explicit context list (and decisions_and_context) and must not return "
        "its gathered context to the caller.\n"
        "- goto_step: choose this when the current step explicitly says to repeat "
        "work. Set step_id to the labeled target step and include the progress "
        "that proves why another iteration is needed. The target must be a prior "
        "step in this skill; never jump to the current or a later step. Use it "
        "until the current step's stated completion condition is satisfied. Never "
        "use an unknown step_id or jump without making progress.\n"
        "- invoke_tool: choose this only when the current step's explicitly "
        "listed tool_invocations support the tool needed for the next action.\n"
        "- read_document: choose this when you know the document path but need "
        "specific lines from that document before deciding the next action. "
        "- list_files: choose this when you need to discover exact files in a "
        "directory; provide directory, optional glob pattern, and recursive. "
        "Request only the smallest useful contiguous range. If read_document "
        "reports that a file does not exist, do not retry that same path. Use "
        "list_files on the existing parent directory and then read one exact "
        "returned path; if the error lists candidate files, choose only one of "
        "those exact paths. Never synthesize a filename from a task id, template "
        "id, package name, or related name.\n"
        + completion_guidance
        + required_action_guidance
        + "- complete: choose this when the skill has finished and no more action "
        "is required. Every later gate in this skill must already have passed; "
        "you cannot complete while a gate remains further ahead.\n"
        + "If the observer intervention recommends an action, treat that action as "
        "allowed for this step and choose it directly when appropriate.\n"
        "When the current step declares outputs, provide the completed values "
        "in an outputs object using exactly those declared names. A later step "
        "receives only validated handoff inputs; do not rely on hidden transcript "
        "history.\n"
        "Response: return exactly one JSON object with a top-level action field, "
        "matching exactly one of these outcome shapes. Include "
        "decisions_and_context when there is information "
        "a later step needs. Include llm_type only when the next roundtrip needs "
        "a different capability; otherwise use null or omit it.\n"
        "Response field requirements by outcome: gather_context requires a non-"
        "empty types array and may include keywords and filters mappings; "
        "prompt_user requires "
        "text containing exactly one clear English question ending in '?'; edit "
        "requires either file_path plus a non-empty edits array or a non-empty "
        "file_edits array, with each edit using add, remove, or replace and valid "
        "line numbers. Edit line numbers are 1-based: start_line and end_line "
        "must be positive integers (1 or greater), never 0; end_line must be "
        "greater than or equal to start_line. Prefer yaml_edit for .yaml or .yml "
        "files, but use edit as "
        "a fallback when a structural operation cannot express the repair. "
        "file_management requires operation (move or rename) and "
        "file_path; move and rename also require destination_path.\n"
        "invoke_tool requires a tool listed in the current step's "
        "tool_invocations. Shell and internal require parameters.command as a "
        "non-empty string or string array. The intrinsic git and gh tools use "
        "parameters.operation and never accept a shell command array. "
        "Every builtin tool accepts parameters.help = true without its "
        "normal command arguments; use it to discover that tool's parameters, "
        "examples, and when to use it. A help response is informational and does "
        "not satisfy a required successful tool invocation. "
        "For git use a registered operation such as status, add, commit, or push; "
        "for gh use only pr_view, pr_diff, pr_checks, pr_create, pr_edit, "
        "pr_comments, or pr_review_comment. "
        "For pr_create and pr_edit, provide only title and body. The runtime "
        "determines the repository, current branch, base branch, and edit target; "
        "never provide pr_reference, head, or base for those operations. "
        "basedpyright-symbol uses operation=resolve_symbol with parameters.query "
        "and optional parameters.limit; basedpyright-structure uses "
        "operation=inspect_structure with parameters.path; yaml_edit requires "
        "a .yaml or .yml file_path and a non-empty operations array; invoke_skill "
        "takes "
        "a skill name from available_skills; "
        + goto_next_action_guidance
        + "read_document requires file_path, non-negative "
        "start_line and end_line for a range of at most 2000 lines. Line 0 means "
        "the beginning of the document, and an end_line beyond EOF is clamped; "
        "complete may include a human-readable text; any action may include an "
        "outputs object when the current step declares outputs.\n"
        '{"action":"gather_context","feature_id":"display-related-photos",'
        '"types":["requirements"],"keywords":["photo"],"filters":{"entity_type":["Service"]},'
        '"decisions_and_context":"...","llm_type":"simple_task"}\n'
        '{"action":"prompt_user","text":"...","decisions_and_context":"...",'
        '"llm_type":"standard_reasoning"}\n'
        '{"action":"edit","file_path":"src/example.py",'
        '"edits":[{"kind":"replace","start_line":1,"end_line":2,'
        '"text":"..."}],"decisions_and_context":"...",'
        '"llm_type":"standard_reasoning"}\n'
        '{"action":"file_management","operation":"rename",'
        '"file_path":"src/old_name.py","destination_path":"src/new_name.py",'
        '"decisions_and_context":"Renamed the file."}\n'
        "For edits across multiple files, use one edit action with "
        '"file_edits":[{"file_path":"...","edits":[...]}].\n'
        '{"action":"yaml_edit","file_path":"docs/proposals/example/implementation-specification.yaml",'
        '"operations":[{"op":"upsert_item","section":"features",'
        '"id":"feature-capture","value":{"action":"added",'
        '"description":"Capture interactions",'
        '"functional_requirements":["Store input and output"]}}],'
        '"decisions_and_context":"...","llm_type":"standard_reasoning"}\n'
        '{"action":"invoke_tool","tool":"shell","parameters":{"command":["..."],"cwd":"...","env":{...}},"decisions_and_context":"...",'
        '"llm_type":"simple_task"}\n'
        '{"action":"invoke_skill","skill":"bootstrap-code-structure",'
        '"decisions_and_context":"...","llm_type":"standard_reasoning"}\n'
        '{"action":"invoke_skill","skill":"adversarial-review",'
        '"provider_role":"adversarial","clean":true,'
        '"context":["Review only this diff."],"decisions_and_context":"..."}\n'
        '{"action":"goto_step","step_id":"process-next-item",'
        '"decisions_and_context":"More items remain; continue with the next item."}\n'
        '{"action":"read_document","file_path":"docs/proposals/example/system-specification.yaml",'
        '"start_line":1,"end_line":80,"decisions_and_context":"...",'
        '"llm_type":"long_context"}\n'
        + next_step_example
        + '{"action":"complete","text":"...","decisions_and_context":"...",'
        '"llm_type":"high_reasoning"}\n'
        "Use gather_context when you need to discover information already "
        "specified in checked-in specs before deciding the next action.\n"
        "Use gather_context to discover what requirements are already "
        "specified, find related entities, inspect approach notes, or gather "
        "current features, decisions, risks, or proposed PRs.\n"
        "The supported context types are:\n"
        f"{context_type_lines}\n"
        "Use keywords to narrow results to items that mention one or more "
        "words. Use filters for exact field matching, such as "
        '{"entity_type":["Tool"],"labels":["python"]}.\n'
        "Do not use filters.work_item_name. Work-item scope comes from the "
        "document path and the current work-item context; gather_context "
        "already searches the relevant local and checked-in documents. Use "
        "keywords or item fields to narrow results within that scope.\n"
        "Use prompt_user only when you need more information to continue "
        "executing the current step.\n"
        "When work_item_context contains matches, treat those names as the "
        "canonical existing work items. Normalize case, spaces, underscores, "
        "and hyphens when matching the user's wording, reuse the exact "
        "canonical name, and do not ask the user to repeat it. Only ask for "
        "a work-item name when no available work item is a reasonable match "
        "and a new item is genuinely required.\n"
        "For start-implementing-feature, a unique normalized match under "
        "docs/proposals or docs/current establishes the canonical feature name: "
        "use the matched directory basename, even when the user's wording uses "
        "a nearby singular/plural or hyphenation variant. The execution workflow "
        "directory is deterministic: docs/workflows/<canonical-feature-name>. "
        "If it does not exist yet, report it as missing so instantiate-workflow "
        "can create it; never ask the user to choose a workflow directory or path.\n"
        "Do not ask for information already present in the transcript or "
        "execution context. Every prompt_user action must include a concise, "
        "properly formed English question in text. The question must contain "
        "meaningful words, cannot be empty or only whitespace, and must end "
        "with a question mark; never return an instruction or placeholder.\n"
        "Use edit when you know the current file should be changed and you "
        "have enough context to describe line-based removals, additions, or "
        "replacements.\n"
        "Prefer yaml_edit for YAML specification files. It preserves section keys "
        "and edits list items structurally: upsert_item uses section, id, and a "
        "complete value mapping; remove_item uses section and id, or section and "
        "index for a validator-reported boilerplate list entry; set_value uses "
        "a mapping-key path and value; remove_key deletes an exact mapping-key "
        "path. Never use set_value to delete a key or represent deletion with null. "
        "Use edit as a fallback when direct textual "
        "repair is necessary, and validate the resulting YAML afterward. Try to "
        "combine multiple independent edits "
        "to the same YAML file into one yaml_edit operations array. If yaml_edit "
        "reports a usage error, "
        "follow its corrective instructions and retry with the corrected shape.\n"
        "For YAML or JSON edits, preserve the surrounding document structure. "
        "When replacing a list item, start at the list item rather than its "
        "mapping key (for example, preserve `entities:` above `- id: ...`). "
        "For prose values containing embedded double quotes, colons, or other "
        "YAML-sensitive punctuation, use a single-quoted scalar or a `>-` "
        "block scalar; never place unescaped double quotes inside a double-"
        "quoted YAML value. "
        "After composing all line edits, ensure the complete resulting document "
        "remains valid before returning the action.\n"
        "When edit is available, current_file includes the file path and the "
        "current contents when the file is small enough to fit this prompt. "
        "For an omitted large file, use read_document to inspect the exact "
        "range before editing.\n"
        "Use invoke_skill for a listed nested skill; it runs in the same worktree "
        "and returns here when complete. Use invoke_tool for shell commands, "
        "or use the always-available intrinsic git and gh tools for repository "
        "state/staging/moves and pull-request creation/inspection. Examples: "
        '{"action":"invoke_tool","tool":"git","parameters":{"operation":"status"}} '
        "and "
        '{"action":"invoke_tool","tool":"gh","parameters":'
        '{"operation":"pr_view","pr_reference":"394"}}. '
        "fuzzy-match searches, or basedpyright "
        "symbol and structure queries.\n"
        "If unsure how to use any listed builtin tool, first invoke it with "
        'parameters {"help":true} (the tool\'s conventional --help option) '
        "and use the returned guidance.\n"
        "Use goto_step only with an id declared on a step in the current skill. "
        "The target step becomes current and receives accumulated context; the "
        "jump must identify the remaining item or changed condition requiring "
        "another pass.\n"
        "When a tool result reports validation failure, a non-zero validation "
        "status, or structured validation errors with corrective_action, do "
        "not invoke the same validation command again unchanged. First use the "
        "reported corrective_action to edit the affected document or gather the "
        "missing context; rerun validation only after a corrective action has "
        "changed or clarified the input.\n"
        "Use read_document instead of requesting or embedding an entire large "
        "document when only a section is needed. The returned line-numbered "
        "excerpt will be included in the next roundtrip context.\n"
        "The fuzzy-match tool executes in Python and returns structured JSON. "
        "Its command array starts with fuzzy-match followed by a search root and "
        "supports -name/-iname, -path/-ipath, -type f|d, -maxdepth, -mindepth, "
        "-threshold, and -print. Use -name for the natural-language query; it is "
        "fuzzy matched rather than treated as an exact glob.\n"
        "Before asking whether existing proposed PR specifications should be "
        "used, invoke fuzzy-match in the current feature specification directory "
        "with a query such as 'proposed PR specification'. Ask only after the "
        "tool result establishes whether matching files exist.\n"
        "For start-implementing-feature, the workflow template path is known and "
        "fixed: templates/execute-proposed-pr.yaml. Use it directly when invoking "
        "instantiate-workflow and never ask the user to supply or choose that path.\n"
        "A missing execute workflow is expected during start-implementing-feature: "
        "this skill creates it. If fuzzy-match finds no matching workflow, invoke "
        "instantiate-workflow immediately rather than asking the user for one.\n"
        "When the current step includes tool_invocations, choose one of those "
        "structured invocations and fill in its parameters unless the task "
        "description explicitly says otherwise. When it does not, "
        "do not return invoke_tool.\n"
        "Never return next_step or complete from a step with tool_invocations "
        "until you have invoked a declared tool for that step and received a "
        "successful result. A prose summary of the intended command is not a "
        "tool invocation; emit invoke_tool and wait for its result.\n"
        "Use next_step when the current step is complete and the next step "
        "should receive the accumulated context.\n"
        "When a step declares tool_invocations, next_step and complete are "
        "invalid until a declared tool has been invoked successfully for that "
        "step.\n"
        "Use complete when the skill is finished.\n"
        "For invoke_tool steps with a deterministic pre-step, the pre-step already "
        "ran. The deterministic_context field in the step prompt contains its "
        "result; do not invoke the pre-step again. Use the result and current step "
        "details before choosing next_step or complete.\n"
        "Always include decisions_and_context with the concise information "
        "future steps will need. Keep it to one short sentence explaining why "
        "you chose this action or what it enables next; it is shown in progress "
        "status after every roundtrip.\n"
        "Always include llm_type to select the model for the next roundtrip. "
        "Use high_reasoning for architecture, difficult reasoning, and final "
        "review; standard_reasoning for normal implementation; simple_task "
        "for mechanical work; fast_iteration for quick feedback; long_context "
        "for large specifications; and vision for image-oriented tasks.\n"
        "Do not output markdown."
    )


def _context_type_catalog() -> tuple[tuple[str, str], ...]:
    return (
        ("requirements", "discover what requirements are already specified"),
        ("approach", "discover the existing approach or solution shape"),
        ("entities", "discover the domain entities already described"),
        (
            "entity-relationships",
            "discover how entities are already related",
        ),
        ("invariants", "discover the rules that must always remain true"),
        ("guidance", "discover implementation guidance or cautions"),
        ("features", "discover the features already recorded or in scope"),
        (
            "human-decisions",
            "discover human decisions that must be preserved",
        ),
        ("intent", "discover the problem, goal, or reasoning already stated"),
        ("intents", "discover current-state intent records"),
        (
            "acceptance_criteria",
            "discover acceptance criteria already written down",
        ),
        ("expected_tests", "discover expected tests already listed"),
        ("required_test_cases", "discover required test cases already listed"),
        ("expected_outcomes", "discover expected outcomes already stated"),
        ("non_goals", "discover what is explicitly out of scope"),
        ("risks", "discover open risks or concerns"),
        ("decisions", "discover recorded decisions or tradeoffs"),
        ("proposed_prs", "discover proposed PR records and their status"),
        ("modules", "discover project modules and their locations"),
        ("tools", "discover project tools and validation commands"),
    )


def build_modular_action_system_prompt(
    current_step: Any,
    *,
    step_actions: Sequence[tuple[str, str]],
    include_context: bool,
    include_skills: bool,
    validation_gate_enabled: bool,
    interaction_style: str | None = None,
) -> str:
    """Build a compact action prompt with explicitly selected guidance sections."""
    action_names = {name for name, _ in step_actions}
    action_lines = "\n".join(
        f"- {name}: {instructions}" for name, instructions in step_actions
    )
    prompt = (
        "Task: execute the supplied details using the handoff inputs, latest action "
        "result, and available actions. Choose exactly one action.\n"
        "Available actions for this step (and only this step; choose exactly one):\n"
        + action_lines
        + "\nThe current-step contract below is authoritative. Do not use action "
        "instructions or action names from any previous step.\n"
        "next_step is always allowed and is listed with its default completion "
        "behavior below. Required outputs add exact handoff requirements.\n" + "\n"
        "If the current-step contract lists required outputs, the advancing action "
        "must also include an outputs object containing every required output under "
        "its exact declared name. A statement in decisions_and_context is not an "
        "output. For example, a step requiring work_item_name must return: "
        '{"action":"next_step","outputs":{"work_item_name":"interaction-file-log"},'
        '"decisions_and_context":"Captured the feature name."}.\n'
        "The outputs object is scoped to the current step only: never copy an "
        "output name produced by a previous step, and never include a name that "
        "is absent from the current-step contract.\n"
        "Return exactly one JSON object with a top-level action field. The action "
        "field is the discriminator. Include "
        "decisions_and_context when a later step needs it, and include outputs "
        "using the declared names. A completed step is represented as: "
        '{"action":"next_step","decisions_and_context":"The current step "'
        '"is complete."}.\n'
        "Use the field names required by the selected action and do not combine "
        "actions.\n"
        "If the user payload contains recovery_required, it is authoritative: the "
        "previous action was rejected. Do not repeat its action or parameters. "
        "Return one materially different action from allowed_actions, or use "
        "prompt_user when no safe contract-valid action is available.\n"
    )
    prompt += interaction_style_prompt(interaction_style)
    if "invoke_tool" in action_names:
        prompt += (
            "A declared internal command is represented as: "
            '{"action":"invoke_tool","tool":"internal","parameters":{"command":'
            '["powdrr-lift","system-specification","--work-item-name",'
            '"example-feature"]},"decisions_and_context":"Generated the '
            'system template."}.\n'
        )
    if "prompt_user" in action_names:
        prompt += (
            "prompt_user requires the question in the text field. Example: "
            '{"action":"prompt_user","text":"What specific success criteria '
            'should this feature meet?","decisions_and_context":"More information '
            'is required before continuing."}.\n'
        )
    if "file_management" in action_names:
        prompt += (
            "file_management uses operation move or rename plus a relative "
            "file_path; move and rename also require destination_path. Never use '..' "
            "or absolute paths.\n"
        )
    if "delete_file" in action_names:
        prompt += (
            "delete_file uses only a relative file_path. Never use '..' or "
            "absolute paths.\n"
        )
    if "goto_step" in action_names:
        prompt += "Use goto_step only with a declared prior step id.\n"
    if include_context:
        context_type_lines = "\n".join(
            f"- {name}: {description}" for name, description in _context_type_catalog()
        )
        prompt += (
            "Context guidance: use gather_context to discover checked-in specs. For a "
            "feature proposal, pass the exact feature_id and do not use fuzzy-match to "
            "substitute another proposal. After gathering, put relevant findings in "
            "decisions_and_context and report next_step when complete. Use keywords to "
            "narrow results and filters for exact fields; never use "
            "filters.work_item_name. Supported context types:\n"
            f"{context_type_lines}\n"
            "Use the exact token entity-relationships when requesting entity "
            "relationships; do not abbreviate it as relationships. Architecture "
            "context is requested with entities, entity-relationships, invariants, "
            "and guidance; do not use architecture as a context type.\n"
            'Example: {"action":"gather_context","feature_id":"display-related-photos",'
            '"types":["requirements"],"keywords":["photo"]}.\n'
        )
    if include_skills:
        prompt += (
            "Nested-skill guidance: use invoke_skill only for a listed skill. It "
            "inherits the current provider role by default; set provider_role to "
            "adversarial or normal when needed. Set clean=true only when the nested "
            "skill should receive only explicit context.\n"
            'Example: {"action":"invoke_skill","skill":"adversarial-review",'
            '"provider_role":"adversarial","clean":true}.\n'
        )
    nested_skill = getattr(current_step, "uses_skill", None)
    if nested_skill is not None:
        prompt += (
            "This step delegates to a nested skill; use invoke_skill, "
            "not invoke_tool or an internal CLI command. The only listed nested "
            f"skill for this step is {json.dumps(nested_skill.skill)}. For "
            "example: "
            '{"action":"invoke_skill","skill":'
            f"{json.dumps(nested_skill.skill)}"
            ',"decisions_and_context":"The nested skill should perform its '
            'declared work."}.\n'
        )
    if getattr(current_step, "pre_step", None) is not None:
        prompt += (
            "Deterministic context: the resolved pre_step template has already run. "
            "The deterministic_context field is the context for this step; do not "
            "invoke the pre-step again. invoke_tool is not allowed in this step. "
        )
        if "complete" in action_names:
            prompt += (
                "Use the result and choose next_step; choose complete only when the "
                "skill itself is finished.\n"
            )
        else:
            prompt += (
                "Use the result and choose next_step when this step is finished.\n"
            )
    if validation_gate_enabled:
        prompt += (
            "This step has a runtime validation gate. Run every discovered obligation "
            "using the exact action in validation_gate. You cannot choose next_step, "
            + ("goto_step, " if "goto_step" in action_names else "")
            + ("or complete " if "complete" in action_names else "")
            + "until every obligation passes in the current "
            "epoch. If any obligation fails, apply its corrective action; the runtime "
            "will reset the epoch and require every obligation to run again.\n"
            + (
                "gather_context remains allowed while repairing a failed obligation. "
                "Use it when the latest validator result reports a missing or unknown "
                "id; "
                "use the validator's suggested context types and keywords, then apply "
                "the correction.\n"
                if "gather_context" in action_names
                else ""
            )
            + "Validation repair protocol: a failed result is a diagnosis, not "
            "permission to repeat the same edit. First inspect the exact current "
            "file and full validator result, map each issue to its reported path, "
            "and apply a structural correction at that path. Never repeat an "
            "operation or semantically equivalent operation that produced the same "
            "issue fingerprint. Preserve fields not named by an issue, combine "
            "independent fixes in one yaml_edit, and wait for the deterministic "
            "obligation rerun before claiming progress. If the latest issue state "
            "is unchanged or worse, change the target or repair strategy; do not keep "
            "retrying the same action.\n"
        )
    coding_loop = getattr(current_step, "coding_loop", None)
    if coding_loop is not None:
        verification = json.dumps(
            [item.to_data() for item in coding_loop.verification], ensure_ascii=False
        )
        stopping = json.dumps(list(coding_loop.stopping_conditions), ensure_ascii=False)
        prompt += (
            "Coding-loop protocol: work toward the declared goal "
            f"{coding_loop.goal!r}, inspect the "
            "current implementation, make the smallest justified edits, and run "
            f"each declared verification item {verification}. Stop only when the "
            f"declared stopping conditions {stopping} are satisfied. This loop is "
            f"bounded to {coding_loop.max_iterations} model iterations; use the "
            "latest verification result as evidence and repair failures before "
            "choosing next_step. Do not claim verification passed without a tool "
            "result. If no verification result exists yet, invoke the declared "
            "command first so you can observe the baseline, including failures. "
            "When the latest verification result says all checks passed, choose "
            "next_step immediately; do not make another edit, reread files, or "
            "rerun an already-passing check. "
            "After a failed verification, prioritize an edit or a targeted test "
            "that addresses the reported failure; do not repeat the same "
            "read_document action unless the failure identifies information that "
            "the prior read did not contain. "
            "For shell/process tools, use an argv array and omit cwd or use a "
            "path relative to the active worktree; do not use shell operators, "
            "absolute paths, or commands that assume an unverified filename. "
            "Use the returned error/output to choose the next edit, then let the "
            "automatic coding-loop verification run after each edit.\n"
        )
    if "goto_step" in action_names or "complete" in action_names:
        prompt += (
            "Transition rules are enforced by the runtime: "
            + (
                "goto_step may target only a prior step in this skill, never the "
                "current or a later step. "
                if "goto_step" in action_names
                else ""
            )
            + (
                "complete is invalid while any gate remains later in this skill."
                if "complete" in action_names
                else ""
            )
            + "\n"
        )
    if "read_document" in action_names:
        prompt += (
            "read_document guidance: use the smallest useful range of a large "
            "document; it requires file_path, non-negative start_line and end_line "
            "for at most 2000 lines, with line 0 meaning the beginning and an "
            "end_line past EOF clamped.\n"
        )
    if "edit" in action_names:
        prompt += (
            "edit guidance: use valid line edits; prefer yaml_edit for YAML only "
            "when yaml_edit is also listed, but edit is allowed as a fallback when "
            "structural operations cannot express the repair.\n"
        )
    if "yaml_edit" in action_names:
        prompt += (
            "yaml_edit guidance: combine independent corrections in one operations "
            "array and preserve document structure. A set_value path is a JSON array "
            'of mapping keys, such as ["title"] or ["metadata","owner"], never a '
            "JSON pointer such as /title. For list sections, use one upsert_item per "
            "item with section, id, and a complete value mapping. Return yaml_edit "
            'directly with action="yaml_edit"; never wrap it in invoke_tool. '
            "Before proposing yaml_edit, verify that file_path exists using the "
            "declared read/list action or a generator result; yaml_edit cannot create "
            "a missing document, and inventing a filename is invalid.\n"
        )
    if current_step.tool_invocations and "invoke_tool" in action_names:
        prompt += (
            "Tool guidance: invoke one of the declared tool_invocations successfully "
            "before next_step or complete. A prose summary is not a tool invocation.\n"
        )
    if (
        not behavior_for_step(current_step).invokes_llm
        and getattr(current_step, "pre_step", None) is None
    ):
        prompt += (
            "This is an atomic invoke_tool step. Invoke its single declared tool, "
            "then choose next_step after the successful result; do not perform "
            "additional reasoning or edits in this step.\n"
        )
    prompt += (
        "The worktree is the command root; use relative paths. Do not output markdown."
    )
    return prompt


def _selected_skill_prompt_data(entry: SkillCatalogEntry) -> dict[str, Any]:
    return {
        "file": entry.path.name,
        "name": entry.skill.name,
        "adversarial": entry.skill.adversarial,
        "interaction_style": entry.skill.interaction_style,
    }


def _build_step_execution_messages(
    *,
    selected_skill: SkillCatalogEntry,
    current_step: Any,
    current_step_index: int,
    transcript: Sequence[dict[str, str]],
    execution_events: Sequence[dict[str, Any]],
    execution_context: Sequence[str],
    handoff_records: Mapping[str, Mapping[str, Any]] | None = None,
    durable_facts: Mapping[str, Mapping[str, Any]] | None = None,
    current_file_path: Path | None,
    worktree_root: Path,
    catalog: Sequence[SkillCatalogEntry],
    workflow_context: WorkflowContext | None = None,
    current_file_context_cache: dict[tuple[str, int, int], dict[str, Any]]
    | None = None,
    validation_gate: Mapping[str, Any] | None = None,
    stalled_step_context: Sequence[Mapping[str, Any]] = (),
    inherited_interaction_style: str | None = None,
    observer_intervention: str | None = None,
    runtime_prompt_context: Mapping[str, Any] | None = None,
    step_actions: Sequence[tuple[str, str]] = (),
    validation_gate_enabled: bool = False,
    pre_step_event: Mapping[str, Any] | None = None,
    failed_action: Any | None = None,
    failure_reason: str | None = None,
) -> list[dict[str, str]]:
    current_file_context = _current_file_context(
        worktree_root,
        current_file_path,
        cache=current_file_context_cache,
    )
    interaction_style = _effective_interaction_style(
        selected_skill,
        current_step,
        inherited_interaction_style,
    )
    available_work_items = _available_work_item_names(worktree_root)
    available_tools = sorted(
        {
            invocation.tool
            for invocation in current_step.tool_invocations
            if invocation.tool != "ref"
        }
    )
    tool_descriptions = {
        "shell": (
            "Execute a shell command in the current worktree. Commands run with "
            "the worktree as cwd; any explicit cwd must remain inside it. Set "
            "parameters.help=true for the tool's conventional --help guidance."
        ),
        _INTERNAL_TOOL: (
            "Execute a powdrr-lift CLI command. This tool is always available, "
            "but its command must invoke only the powdrr-lift binary and runs "
            "with the current worktree as cwd. Set parameters.help=true for the "
            "tool's conventional --help guidance and detailed examples."
            "detailed usage and examples."
        ),
        GIT_TOOL: (
            "Intrinsic Git tool; supports status, add, and move only. Example: "
            '{"action":"invoke_tool","tool":"git","parameters":'
            '{"operation":"status"}}. Set parameters.help=true for the tool\'s '
            "conventional --help guidance and detailed examples."
            "usage and examples."
        ),
        GH_TOOL: (
            "Intrinsic GitHub tool for pull-request creation, inspection, and "
            "inline review comments. "
            'Example: {"action":"invoke_tool","tool":"gh",'
            '"parameters":{"operation":"pr_view","pr_reference":"394"}}. '
            'Inline comment example: {"action":"invoke_tool","tool":"gh",'
            '"parameters":{"operation":"pr_review_comment",'
            '"repository":"owner/repo","pr_reference":"394",'
            '"body":"Finding","commit_id":"sha",'
            '"path":"docs/design.yaml","line":12,"side":"RIGHT"}}.'
            " Set parameters.help=true for the tool's conventional --help "
            "guidance and detailed examples."
        ),
        "fuzzy-match": (
            "Search worktree paths with find-like filters and fuzzy name matching. "
            "Set parameters.help=true for the tool's conventional --help "
            "guidance and detailed examples."
        ),
        BASEDPYRIGHT_SYMBOL_TOOL: (
            "Find Python symbols by name across the worktree. Set "
            "parameters.help=true for the tool's conventional --help guidance "
            "and detailed examples."
        ),
        BASEDPYRIGHT_STRUCTURE_TOOL: (
            "Discover the classes, functions, methods, and variables in a Python "
            "file. Set parameters.help=true for the tool's conventional --help "
            "guidance and detailed examples."
        ),
        ENRICH_TOOL: (
            "Convert a deterministic tool output into structured data. "
            "Use format pytest and pass the complete tool result as tool_output."
        ),
        VALIDATE_EDIT_TOOL: (
            "Validate a deferred edit without changing files. Pass the complete "
            "edit action in parameters.edit."
        ),
        APPLY_EDIT_TOOL: (
            "Apply a previously validated deferred edit. Pass the complete edit "
            "action in parameters.edit."
        ),
    }
    prompt_data: dict[str, Any] = {
        "execution_mode": "execute_selected_skill",
        "current_step_index": current_step_index,
        "current_step_count": len(selected_skill.skill.steps),
        "current_step": _skill_step_to_data(current_step),
        "handoff_inputs": _workflow_handoff_inputs(
            current_step,
            handoff_records or {},
        ),
        # Cross-step values must travel through declared handoff inputs. The
        # prompt helper retains explicit invocation context while removing
        # implicit tool and document results.
        "step_context": _prompt_step_context(
            execution_context,
            durable_facts,
            execution_events,
            current_step_index,
        ),
        "durable_facts": _prompt_durable_facts(durable_facts or {}),
        "available_tools": [
            {
                "name": tool,
                "description": tool_descriptions.get(tool, tool),
            }
            for tool in available_tools
        ],
        "worktree_root": ".",
        "previous_workflow_context": _workflow_context_prompt_data(workflow_context),
        "work_item_context": {
            "available": list(available_work_items),
            "matches": list(
                _match_work_item_names(
                    transcript,
                    available_work_items,
                )
            ),
        },
        "selected_skill": _selected_skill_prompt_data(selected_skill),
        "transcript": _prompt_transcript(transcript),
        "execution_events": _execution_events_for_prompt(
            execution_events,
            current_step_index,
        ),
        "latest_action": _latest_execution_event_for_prompt(
            execution_events,
            current_step_index,
        ),
        "successful_document_reads": _successful_document_reads_for_prompt(
            execution_events, current_step_index
        ),
        "stalled_step_context": [dict(item) for item in stalled_step_context],
        "current_file": current_file_context,
    }
    if observer_intervention is not None:
        prompt_data["observer_intervention"] = observer_intervention
    if runtime_prompt_context is not None:
        prompt_data["runtime_state"] = dict(runtime_prompt_context)
    if _step_needs_prompt_catalog(current_step, "context_types"):
        prompt_data["available_context_types"] = [
            {
                "name": context_type,
                "when_to_use": description,
            }
            for context_type, description in _context_type_catalog()
        ]
    if _step_needs_prompt_catalog(current_step, "skills"):
        prompt_data["available_skills"] = [
            {
                "name": entry.skill.name,
                "path": entry.path.name,
                "adversarial": entry.skill.adversarial,
            }
            for entry in catalog
        ]
    prompt_data["available_actions"] = [name for name, _instructions in step_actions]
    if failed_action is not None:
        successful_reads = _successful_document_reads_for_prompt(
            execution_events, current_step_index
        )
        prompt_data["recovery_required"] = {
            "rejected_action": json.loads(workflow_action_signature(failed_action)),
            "reason": failure_reason
            or "The previous action was rejected by the workflow contract.",
            "must_choose_different_action": True,
            "allowed_actions": prompt_data["available_actions"],
            "successful_document_reads": successful_reads,
            "instruction": (
                "Do not repeat the rejected action, even with different prose. "
                "Choose one materially different action from allowed_actions, "
                "or return prompt_user if no allowed action can safely resolve "
                "the reported issue."
            ),
        }
        if successful_reads:
            prompt_data["recovery_required"]["instruction"] += (
                " These documents were already read successfully; do not reread "
                "them unless the failed action specifically requires changed file "
                "contents: "
                + ", ".join(str(item["path"]) for item in successful_reads)
                + "."
            )
    if "edit" in prompt_data["available_actions"]:
        prompt_data["edit_contract"] = (
            "For edit, return exactly one JSON object with action=edit, a string "
            "file_path, and a non-empty edits array. Each edit must be an object "
            "with kind add, remove, or replace; replace requires positive integer "
            "start_line and end_line plus a string text. Do not use yaml_edit, "
            "file_edits, operations, or a nested parameters object."
        )
    required_output_names = [
        output.name for output in getattr(current_step, "outputs", ())
    ]
    if required_output_names:
        prompt_data["required_output_names"] = required_output_names
        prompt_data["output_contract"] = (
            "When choosing next_step, include outputs with exactly these names: "
            + ", ".join(required_output_names)
            + ". Every edit output must be present even when no changes are needed; "
            'use {"added":[],"deleted":[]} for no changes.'
        )
    if validation_gate is not None:
        prompt_data["validation_gate"] = dict(validation_gate)
    if pre_step_event is not None:
        bounded_pre_step_event = prune_execution_events(
            [pre_step_event], include_results=True
        )[0]
        prompt_data["deterministic_context"] = {
            "source": bounded_pre_step_event["action"],
            "scope": bounded_pre_step_event["template"],
            "result": bounded_pre_step_event["result"],
        }
    return [
        {
            "role": "system",
            "content": build_modular_action_system_prompt(
                current_step,
                step_actions=step_actions,
                include_context=_step_needs_prompt_catalog(
                    current_step, "context_types"
                ),
                include_skills=_step_needs_prompt_catalog(current_step, "skills"),
                validation_gate_enabled=validation_gate_enabled,
                interaction_style=interaction_style,
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                prompt_data,
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        },
    ]
