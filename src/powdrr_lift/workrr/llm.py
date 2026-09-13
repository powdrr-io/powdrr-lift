"""Shared LLM request and workflow-action control primitives.

Both the interactive chat runner and the durable workflow-task runner use this
module for the parts of execution that must never drift: making a JSON LLM
request, retrying provider timeouts, parsing the proposed action, and deciding
whether a repeated action actually made progress.  The callers deliberately
keep only presentation and human-handoff policy.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, TypeVar, cast

from powdrr_lift.errors import (
    ExecutionCancelled,
    PersistenceCorruptionError,
    PowdrrExecutionError,
    ProgrammerInvariantError,
    ProviderExecutionError,
)
from powdrr_lift.execution.kernel import ActionKernel
from powdrr_lift.execution.runtime import ExecutionRuntime
from powdrr_lift.workrr.actions import (
    WorkflowAction,  # noqa: F401 - compatibility export
    WorkflowEdit,  # noqa: F401 - compatibility export
    WorkflowFileEdits,  # noqa: F401 - compatibility export
    WorkflowYamlOperation,  # noqa: F401 - compatibility export
)
from powdrr_lift.workrr.loop import (
    WorkflowActionObservation,
    WorkflowActionOutcome,
    WorkflowActionProgressStrategy,
    WorkflowActionRequest,  # noqa: F401 - compatibility export
    WorkflowExecutionObserver,
    WorkflowExecutionStrategy,
)
from powdrr_lift.workrr.progress import (
    ProgressDecision,
    WorkflowExecutionController,
    no_progress_feedback,
)
from powdrr_lift.workrr.protocol import (
    SchemaAwareWorkflowLLMClient,  # noqa: F401 - compatibility export
    WorkflowLLMClient,  # noqa: F401 - compatibility export
)
from powdrr_lift.workrr.repair import (
    RepairContext,
    RepairDirective,
    RepairExhaustionReport,  # noqa: F401 - compatibility export
    RepairFailure,
    RepairFailureClass,
    RepairPolicy,
    RepairStage,
    classify_repair_failure,
)


class WorkflowLLMTimeoutExhausted(ProviderExecutionError):
    """Raised when a provider request keeps timing out after its retry budget."""


class WorkflowLLMHTTPError(ProviderExecutionError):
    """A provider response that includes an HTTP status code."""

    def __init__(self, provider: str, status_code: int, detail: str) -> None:
        self.status_code = status_code
        super().__init__(f"{provider} request failed with HTTP {status_code}: {detail}")


class WorkflowLLMExecutionAborted(ExecutionCancelled):
    """Stop a shared execution loop after its adapter aborts a request."""

    def __init__(self, exit_code: int) -> None:
        super().__init__("Workflow execution was aborted by its adapter.")
        self.exit_code = exit_code


ActionT = TypeVar("ActionT")
_MAX_PROMPT_EVENTS = 32
_MAX_PROMPT_EVENT_CHARS = 8_000
_PROMPT_SIZE_CHARS_PER_TOKEN = 3
# A caller may opt into an unlimited loop for deterministic harnesses, but
# production entry points must always provide a finite budget.
DEFAULT_MAX_ROUNDTRIPS = 128


def _prompt_profile_for_stage(stage: RepairStage) -> str:
    profiles = {
        RepairStage.TARGETED: "targeted_schema_correction",
        RepairStage.CLEAN_ROOM: "clean_room_replan",
        RepairStage.SELECT_ACTION: "constrained_action_selection",
        RepairStage.FILL_ACTION: "constrained_action_parameters",
        RepairStage.DETERMINISTIC: "deterministic_recovery",
        RepairStage.MODEL_FALLBACK: "clean_room_replan",
        RepairStage.HUMAN_HANDOFF: "human_recovery_question",
        RepairStage.EXHAUSTED: "repair_exhausted",
    }
    return profiles[stage]


class WorkflowRepairCoordinator:
    """Pure bounded state machine for semantic recovery decisions."""

    def __init__(self, policy: RepairPolicy | None = None) -> None:
        self.policy = policy or RepairPolicy()
        self.boundary_id: str | None = None
        self.attempts: list[RepairDirective] = []
        self._identities: set[tuple[str, str | None, str | None, str]] = set()
        self.last_context: RepairContext | None = None

    def begin_boundary(self, boundary_id: str) -> None:
        if boundary_id != self.boundary_id:
            self.boundary_id = boundary_id
            self.attempts.clear()
            self._identities.clear()

    def record_failure(
        self,
        failure: RepairFailure,
        *,
        allowed_actions: Sequence[str] = (),
        context: RepairContext | None = None,
    ) -> RepairDirective:
        self.last_context = context
        context_fingerprint = (
            context.material_state_fingerprint if context is not None else ""
        )
        identity = (
            failure.error_code,
            failure.action_signature,
            failure.target_signature,
            context_fingerprint,
        )
        if identity in self._identities:
            raise ProgrammerInvariantError(
                "Duplicate semantic repair failure was recorded.",
                error_code="duplicate_repair_attempt",
                remediation="Advance material state or change the repair strategy.",
            )
        self._identities.add(identity)
        stage = self._next_stage(failure)
        used = sum(item.stage == stage for item in self.attempts)
        limit = self._limit_for(stage)
        if used >= limit:
            return self._exhausted(failure, allowed_actions)
        directive = RepairDirective(
            stage=stage,
            attempt=len(self.attempts) + 1,
            reason=failure.message,
            allowed_actions=tuple(
                dict.fromkeys(
                    context.allowed_actions if context is not None else allowed_actions
                )
            ),
            prompt_profile=_prompt_profile_for_stage(stage),
            model_policy=(
                "backup_model"
                if stage == RepairStage.MODEL_FALLBACK
                else "current_model"
            ),
            failure_class=failure.classification,
            error_code=failure.error_code,
            target_signature=failure.target_signature,
            material_state_fingerprint=context_fingerprint,
            rejected_strategy_signatures=(
                context.rejected_strategies if context is not None else ()
            ),
        )
        self.attempts.append(directive)
        return directive

    def _next_stage(self, failure: RepairFailure) -> RepairStage:
        targeted_classes = {
            RepairFailureClass.RESPONSE,
            RepairFailureClass.RESPONSE_EMPTY,
            RepairFailureClass.RESPONSE_SYNTAX,
            RepairFailureClass.RESPONSE_SCHEMA,
        }
        first = (
            RepairStage.TARGETED
            if failure.classification in targeted_classes
            else RepairStage.CLEAN_ROOM
        )
        stages = (
            (
                first,
                RepairStage.CLEAN_ROOM,
                RepairStage.DETERMINISTIC,
                RepairStage.MODEL_FALLBACK,
            )
            if first == RepairStage.TARGETED
            else (
                RepairStage.CLEAN_ROOM,
                RepairStage.DETERMINISTIC,
                RepairStage.MODEL_FALLBACK,
            )
        )
        for stage in stages:
            if sum(item.stage == stage for item in self.attempts) < self._limit_for(
                stage
            ):
                return stage
        if self.policy.allow_human_handoff:
            return RepairStage.HUMAN_HANDOFF
        return RepairStage.EXHAUSTED

    def _limit_for(self, stage: RepairStage) -> int:
        if stage == RepairStage.TARGETED:
            return max(0, self.policy.targeted_attempts)
        if stage == RepairStage.CLEAN_ROOM:
            return max(0, self.policy.clean_room_attempts)
        if stage == RepairStage.MODEL_FALLBACK:
            return max(0, self.policy.model_fallback_attempts)
        if stage == RepairStage.DETERMINISTIC:
            return 1 if self.policy.deterministic_recovery else 0
        if stage == RepairStage.HUMAN_HANDOFF:
            return 1 if self.policy.allow_human_handoff else 0
        return 0

    def _exhausted(
        self, failure: RepairFailure, allowed_actions: Sequence[str]
    ) -> RepairDirective:
        return RepairDirective(
            stage=RepairStage.EXHAUSTED,
            attempt=len(self.attempts) + 1,
            reason=f"Recovery exhausted for {failure.error_code}: {failure.message}",
            allowed_actions=tuple(dict.fromkeys(allowed_actions)),
            prompt_profile=_prompt_profile_for_stage(RepairStage.EXHAUSTED),
            model_policy="stop",
            failure_class=failure.classification,
            error_code=failure.error_code,
            target_signature=failure.target_signature,
            material_state_fingerprint=(
                self.last_context.material_state_fingerprint
                if self.last_context is not None
                else ""
            ),
            rejected_strategy_signatures=(
                self.last_context.rejected_strategies
                if self.last_context is not None
                else ()
            ),
        )


def resolve_deterministic_repair(
    *,
    error_code: str,
    allowed_actions: Sequence[str],
    completion_satisfied: bool = False,
    output_state: Any = None,
    legacy_action: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Return only allowlisted repairs whose payload requires no model judgment."""
    allowed = set(allowed_actions)
    if completion_satisfied and "emit_outputs" in allowed:
        return {"action": "emit_outputs", "outputs": output_state}
    if completion_satisfied and "next_step" in allowed:
        return {"action": "next_step", "output_state": output_state}
    if (
        error_code == "deterministic_output_state_mismatch"
        and output_state is not None
        and "next_step" in allowed
    ):
        return {"action": "next_step", "output_state": output_state}
    if error_code == "legacy_action_shape" and legacy_action is not None:
        action = legacy_action.get("action")
        if isinstance(action, str) and action in allowed:
            return dict(legacy_action)
    return None


@dataclass(frozen=True, slots=True)
class RepairPromptManifest:
    """Auditable description of the information and contract in a repair prompt."""

    profile: str
    source_sections: tuple[str, ...]
    history_policy: str
    allowed_actions: tuple[str, ...]
    response_schema_fingerprint: str
    reasoning_mode: str
    model: str
    message_fingerprint: str
    structural_fingerprint: str
    estimated_tokens: int

    def to_data(self) -> dict[str, object]:
        return {
            "profile": self.profile,
            "source_sections": list(self.source_sections),
            "history_policy": self.history_policy,
            "allowed_actions": list(self.allowed_actions),
            "response_schema_fingerprint": self.response_schema_fingerprint,
            "reasoning_mode": self.reasoning_mode,
            "model": self.model,
            "message_fingerprint": self.message_fingerprint,
            "structural_fingerprint": self.structural_fingerprint,
            "estimated_tokens": self.estimated_tokens,
        }


def build_repair_prompt_manifest(
    messages: Sequence[Mapping[str, str]],
    *,
    profile: str,
    source_sections: Sequence[str] = (),
    history_policy: str,
    allowed_actions: Sequence[str] = (),
    response_schema: Mapping[str, Any] | None = None,
    reasoning_mode: str = "direct_action",
    model: str = "",
) -> RepairPromptManifest:
    """Describe a repair prompt using stable structural and content fingerprints."""
    serialized = json.dumps(list(messages), ensure_ascii=False, sort_keys=True)
    schema_serialized = json.dumps(
        response_schema or {}, ensure_ascii=False, sort_keys=True
    )
    structure = {
        "profile": profile,
        "source_sections": sorted(set(source_sections)),
        "history_policy": history_policy,
        "allowed_actions": sorted(set(allowed_actions)),
        "response_schema": schema_serialized,
        "reasoning_mode": reasoning_mode,
        "model": model,
    }
    return RepairPromptManifest(
        profile=profile,
        source_sections=tuple(sorted(set(source_sections))),
        history_policy=history_policy,
        allowed_actions=tuple(sorted(set(allowed_actions))),
        response_schema_fingerprint=_sha256(schema_serialized),
        reasoning_mode=reasoning_mode,
        model=model,
        message_fingerprint=_sha256(serialized),
        structural_fingerprint=_sha256(
            json.dumps(structure, ensure_ascii=False, sort_keys=True)
        ),
        estimated_tokens=_prompt_size_tokens(serialized),
    )


def assert_material_repair_prompt(
    previous: RepairPromptManifest,
    current: RepairPromptManifest,
) -> None:
    """Reject semantic repair prompts that only differ cosmetically."""
    if previous.message_fingerprint == current.message_fingerprint:
        raise ProgrammerInvariantError(
            "Repair prompt was identical to the previous prompt.",
            error_code="repair_prompt_not_distinct",
            remediation="Use a different repair prompt profile and context.",
        )
    material_change = previous.profile != current.profile and (
        previous.history_policy != current.history_policy
        or set(current.allowed_actions) < set(previous.allowed_actions)
        or previous.response_schema_fingerprint != current.response_schema_fingerprint
        or previous.reasoning_mode != current.reasoning_mode
        or set(current.source_sections) != set(previous.source_sections)
        or previous.model != current.model
    )
    if not material_change:
        raise ProgrammerInvariantError(
            "Repair prompt did not make a material structural change.",
            error_code="repair_prompt_not_materially_different",
            remediation=(
                "Change the prompt profile, remove history, narrow the action "
                "space, change the response schema, or change reasoning mode."
            ),
        )


def build_clean_room_repair_prompt(
    *,
    context: str,
    error_message: str,
    repair_instructions: str,
    response_schema: Mapping[str, Any] | None = None,
    allowed_actions: Sequence[str] = (),
    model: str = "",
) -> tuple[list[dict[str, str]], RepairPromptManifest]:
    """Build a recovery prompt from structured facts without conversation history."""
    recovery = {
        "execution_mode": "clean_room_repair",
        "context": context,
        "failure": error_message,
        "repair_instructions": repair_instructions,
        "allowed_actions": list(allowed_actions),
    }
    messages = [
        {
            "role": "system",
            "content": (
                "You are recovering a stalled workflow boundary using a clean-room "
                "repair. Do not continue "
                "the previous conversation. Choose one legal action that materially "
                "advances the supplied task. Return only the complete JSON object "
                "required by the supplied response contract."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(recovery, ensure_ascii=False, separators=(",", ":")),
        },
    ]
    return messages, build_repair_prompt_manifest(
        messages,
        profile="clean_room_replan",
        source_sections=(
            "context",
            "failure",
            "repair_instructions",
            "allowed_actions",
        ),
        history_policy="none",
        allowed_actions=allowed_actions,
        response_schema=response_schema,
        reasoning_mode="clean_room_action",
        model=model,
    )


def build_clean_room_action_selection_prompt(
    *,
    context: str,
    error_message: str,
    allowed_actions: Sequence[str],
    model: str = "",
) -> tuple[list[dict[str, str]], Mapping[str, Any], RepairPromptManifest]:
    """Build the first pass of constrained recovery: choose a legal action kind."""
    actions = tuple(dict.fromkeys(allowed_actions))
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": list(actions)},
        },
        "required": ["action"],
        "additionalProperties": False,
    }
    messages = [
        {
            "role": "system",
            "content": (
                "You are selecting a recovery action for a stalled workflow. "
                "Choose exactly one action name from the supplied enum. Do not "
                "provide parameters, prose, or a complete action object."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "execution_mode": "clean_room_repair",
                    "repair_stage": "action_selection",
                    "context": context,
                    "failure": error_message,
                    "allowed_actions": list(actions),
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        },
    ]
    manifest = build_repair_prompt_manifest(
        messages,
        profile="constrained_action_selection",
        source_sections=("context", "failure", "allowed_actions"),
        history_policy="none",
        allowed_actions=actions,
        response_schema=schema,
        reasoning_mode="action_selection",
        model=model,
    )
    return messages, schema, manifest


def build_clean_room_action_parameters_prompt(
    *,
    context: str,
    error_message: str,
    selected_action: str,
    response_schema: Mapping[str, Any],
    model: str = "",
) -> tuple[list[dict[str, str]], RepairPromptManifest]:
    """Build the second pass of recovery for one already-selected action."""
    messages = [
        {
            "role": "system",
            "content": (
                "You are supplying parameters for one previously selected workflow "
                "action. Return exactly one complete JSON action object. The action "
                f"must be {selected_action!r}; do not choose another action, add "
                "prose, or return markdown."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "execution_mode": "clean_room_repair",
                    "repair_stage": "action_parameters",
                    "selected_action": selected_action,
                    "context": context,
                    "failure": error_message,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        },
    ]
    manifest = build_repair_prompt_manifest(
        messages,
        profile="constrained_action_parameters",
        source_sections=("context", "failure", "selected_action"),
        history_policy="none",
        allowed_actions=(selected_action,),
        response_schema=response_schema,
        reasoning_mode="action_parameters",
        model=model,
    )
    return messages, manifest


def complete_two_pass_action(
    client: WorkflowLLMClient,
    *,
    selection_messages: list[dict[str, str]],
    selection_schema: Mapping[str, Any],
    parameter_messages_for: Callable[[str], list[dict[str, str]]],
    parameter_schema_for: Callable[[str], Mapping[str, Any]],
    parser: Callable[[dict[str, Any]], Any],
    allowed_actions: Sequence[str],
    model: str,
    stderr: Any,
    max_timeout_retries: int,
    timeout_backoff_seconds: float,
    fallback_client: WorkflowLLMClient | None = None,
    fallback_model: str | None = None,
) -> Any:
    """Select an action kind, then request only that action's payload.

    A complete payload from a scripted/legacy client remains accepted so the
    recovery contract can be introduced without breaking deterministic tests.
    Provider-backed recovery still uses two calls whenever the first response is
    only an action selection.
    """
    selection = complete_json_with_timeout_retry(
        client,
        selection_messages,
        model=model,
        stderr=stderr,
        max_timeout_retries=max_timeout_retries,
        timeout_backoff_seconds=timeout_backoff_seconds,
        response_schema=selection_schema,
    )
    selected = selection.get("action")
    actions = tuple(dict.fromkeys(allowed_actions))
    if not isinstance(selected, str) or (actions and selected not in actions):
        raise RuntimeError("Recovery action selection was not a legal action name.")
    if (
        len(selection) > 1
        or not actions
        or selected in {"next_step", "complete", "emit_outputs"}
    ):
        return parser(selection)
    parameter_messages = parameter_messages_for(selected)
    parameter_schema = parameter_schema_for(selected)
    try:
        payload = complete_json_with_timeout_retry(
            client,
            parameter_messages,
            model=model,
            stderr=stderr,
            max_timeout_retries=max_timeout_retries,
            timeout_backoff_seconds=timeout_backoff_seconds,
            response_schema=parameter_schema,
        )
        if payload.get("action") != selected:
            raise RuntimeError(
                f"Recovery parameter response selected {payload.get('action')!r}; "
                f"expected {selected!r}."
            )
        return parser(payload)
    except RuntimeError:
        if fallback_client is None or not fallback_model or fallback_model == model:
            raise
        fallback_payload = complete_json_with_timeout_retry(
            fallback_client,
            parameter_messages,
            model=fallback_model,
            stderr=stderr,
            max_timeout_retries=max_timeout_retries,
            timeout_backoff_seconds=timeout_backoff_seconds,
            response_schema=parameter_schema,
        )
        if fallback_payload.get("action") != selected:
            raise RuntimeError(
                f"Fallback recovery selected {fallback_payload.get('action')!r}; "
                f"expected {selected!r}."
            ) from None
        return parser(fallback_payload)


def constrain_action_response_schema(
    response_schema: Mapping[str, Any], selected_action: str
) -> dict[str, Any]:
    """Return a provider schema containing only the selected action's fields."""
    schema = json.loads(json.dumps(response_schema))
    properties = schema.get("properties", {})
    common = {name for name in ("action", "decisions_and_context", "llm_type")}
    fields_by_action = {
        "gather_context": {"types", "feature_id", "keywords", "filters"},
        "prompt_user": {"text"},
        "edit": {"file_path", "edits", "file_edits"},
        "yaml_edit": {"file_path", "operations"},
        "file_management": {"operation", "file_path", "destination_path"},
        "delete_file": {"file_path"},
        "invoke_skill": {"skill", "provider_role", "clean", "context"},
        "invoke_tool": {"tool", "parameters"},
        "read_document": {"file_path", "start_line", "end_line"},
        "list_files": {"directory", "pattern", "recursive"},
        "goto_step": {"step_id"},
        "next_step": {"output_state"},
        "complete": {"text"},
        "emit_outputs": {"outputs"},
    }
    keep = common | fields_by_action.get(selected_action, set())
    schema["properties"] = {
        name: value for name, value in properties.items() if name in keep
    }
    action_schema = schema["properties"].get("action", {})
    schema["properties"]["action"] = {**action_schema, "enum": [selected_action]}
    schema["required"] = [name for name in schema.get("required", []) if name in keep]
    if "action" not in schema["required"]:
        schema["required"].insert(0, "action")
    return schema


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def prompt_size_breakdown(messages: Sequence[Mapping[str, str]]) -> dict[str, Any]:
    """Estimate prompt size by top-level field without changing the prompt.

    Workflow prompts deliberately keep their execution mode and state in the
    JSON user message. Measuring those fields independently makes prompt
    reduction work evidence-based while retaining the exact message payload
    sent to the provider.
    """
    fields: dict[str, int] = {}
    execution_mode: str | None = None
    serialized_messages = json.dumps(
        list(messages), ensure_ascii=False, separators=(",", ":")
    )
    for index, message in enumerate(messages):
        content = message.get("content", "")
        if index == 0:
            fields["system_prompt"] = _prompt_size_tokens(content)
            continue
        try:
            decoded = json.loads(content)
        except (TypeError, ValueError):
            fields[f"message_{index}"] = _prompt_size_tokens(content)
            continue
        if not isinstance(decoded, Mapping):
            fields[f"message_{index}"] = _prompt_size_tokens(content)
            continue
        mode = decoded.get("execution_mode")
        if isinstance(mode, str):
            execution_mode = mode
        for key, value in decoded.items():
            fields[f"message_{index}.{key}"] = _prompt_size_tokens(
                json.dumps({key: value}, ensure_ascii=False, separators=(",", ":"))
            )
    return {
        "execution_mode": execution_mode or "unknown",
        "estimated_input_tokens": _prompt_size_tokens(serialized_messages),
        "fields": fields,
    }


def _prompt_size_tokens(value: str) -> int:
    return max(
        1,
        (len(value) + _PROMPT_SIZE_CHARS_PER_TOKEN - 1) // _PROMPT_SIZE_CHARS_PER_TOKEN,
    )


def complete_json(
    client: WorkflowLLMClient,
    messages: list[dict[str, str]],
    *,
    response_schema: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Make the single provider call used by workflow execution.

    Keeping this call in one module makes the exchange boundary explicit.  The
    provider clients remain transport implementations; runners must use an
    engine rather than calling ``complete_json`` themselves.
    """
    if response_schema is None or not _client_supports_response_schema(client):
        return client.complete_json(messages)
    schema_client = cast(SchemaAwareWorkflowLLMClient, client)
    return schema_client.complete_json(messages, response_schema=response_schema)


def _client_supports_response_schema(client: WorkflowLLMClient) -> bool:
    """Keep schema-aware requests compatible with legacy/test provider clients."""
    try:
        parameters = inspect.signature(client.complete_json).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(
        parameter.name == "response_schema"
        or parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in parameters
    )


def is_timeout_error(error: RuntimeError) -> bool:
    message = str(error).casefold()
    return "timed out" in message or "timeout" in message


def is_retryable_provider_error(error: RuntimeError) -> bool:
    """Return whether a provider failure is transient enough to retry."""
    return (
        is_timeout_error(error)
        or (isinstance(error, WorkflowLLMHTTPError) and error.status_code == 429)
        or any(
            phrase in str(error).casefold()
            for phrase in (
                "remote end closed connection",
                "remote disconnected",
                "connection reset",
                "connection aborted",
                "broken pipe",
            )
        )
    )


def complete_json_with_timeout_retry(
    client: WorkflowLLMClient,
    messages: list[dict[str, str]],
    *,
    model: str,
    stderr: Any,
    max_timeout_retries: int,
    timeout_backoff_seconds: float,
    response_schema: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Request JSON with the common exponential transient-error retry policy."""
    retries = 0
    while True:
        try:
            return complete_json(client, messages, response_schema=response_schema)
        except RuntimeError as exc:
            if not is_retryable_provider_error(exc):
                raise
            if retries >= max(0, max_timeout_retries):
                raise WorkflowLLMTimeoutExhausted(
                    f"LLM request failed after {retries} retries: {exc}"
                ) from exc
            retries += 1
            delay_seconds = timeout_backoff_seconds * (2 ** (retries - 1))
            reason = (
                "timed out"
                if is_timeout_error(exc)
                else (
                    "connection dropped"
                    if is_retryable_provider_error(exc)
                    and not isinstance(exc, WorkflowLLMHTTPError)
                    else "provider is overloaded"
                )
            )
            print(
                f"LLM request {reason} for {model}; retrying in "
                f"{delay_seconds:g} seconds "
                f"(retry {retries}/{max_timeout_retries}).",
                file=stderr,
                flush=True,
            )
            time.sleep(delay_seconds)


def _coding_loop_spec(strategy: Any) -> Any | None:
    step = getattr(strategy, "current_step", None)
    if step is None:
        step = getattr(strategy, "task", None)
    return getattr(step, "coding_loop", None)


def _coding_loop_limit(strategy: Any) -> int | None:
    spec = _coding_loop_spec(strategy)
    limit = getattr(spec, "max_iterations", None)
    return limit if isinstance(limit, int) else None


def _coding_loop_identity(strategy: Any) -> tuple[Any, ...]:
    return (
        getattr(strategy, "current_step_index", None),
        getattr(getattr(strategy, "current_step", None), "id", None),
        getattr(getattr(strategy, "task", None), "task_id", None),
    )


def _boundary_id(strategy: Any) -> str:
    """Return a stable task/step identity for semantic repair state."""
    task_id = getattr(getattr(strategy, "task", None), "task_id", None)
    if task_id is not None:
        return f"task:{task_id}:{getattr(strategy, 'current_step_index', None)}"
    skill = getattr(strategy, "selected_skill", None)
    return (
        f"skill:{getattr(skill, 'path', '<unknown>')}:"
        f"{getattr(strategy, 'current_step_index', None)}"
    )


class WorkflowShadowRecorder(Protocol):
    """Optional best-effort event sink used while the kernel is in shadow mode."""

    def record_action(
        self,
        event_type: str,
        action: Any,
        *,
        error_code: str | None = None,
    ) -> None: ...


class WorkflowStepRunner:
    """Run workflow steps through one shared control loop.

    This is deliberately the only loop that combines an LLM response with
    step execution. Chat and durable-task adapters cannot independently drift
    in parsing, corrective-action thresholds, or no-progress behavior.
    """

    def __init__(
        self,
        *,
        max_stalled_roundtrips: int,
        observer: WorkflowExecutionObserver | None = None,
        shadow_recorder: WorkflowShadowRecorder | None = None,
        runtime: ExecutionRuntime | None = None,
        legacy_compatibility: bool = False,
        phase_type: str = "build",
        actor_id: str = "workflow-agent",
        repair_policy: RepairPolicy | None = None,
    ) -> None:
        self.action_engine = WorkflowLLMActionEngine(
            max_stalled_roundtrips=max_stalled_roundtrips
        )
        self.observer = observer
        self.shadow_recorder = shadow_recorder
        self.runtime = runtime
        self.phase_type = phase_type
        self.actor_id = actor_id
        self.repair_coordinator = WorkflowRepairCoordinator(repair_policy)
        self.last_repair_directive: RepairDirective | None = None
        self._last_repair_failure: RepairFailure | None = None
        if runtime is None and not legacy_compatibility:
            raise ProgrammerInvariantError(
                "WorkflowStepRunner requires an ExecutionRuntime for normal execution.",
                error_code="execution_runtime_required",
                remediation=(
                    "Create an ExecutionRuntime, or explicitly opt into the "
                    "legacy compatibility runner."
                ),
            )
        self.kernel = runtime.kernel if runtime is not None else ActionKernel()

    def run(
        self,
        strategy: WorkflowExecutionStrategy,
        *,
        max_roundtrips: int | None,
        signature: Callable[[Any], str],
    ) -> int:
        roundtrips = 0
        coding_loop_identity: tuple[Any, ...] | None = None
        coding_loop_roundtrips = 0
        while max_roundtrips is None or roundtrips < max(1, max_roundtrips):
            coding_loop_limit = _coding_loop_limit(strategy)
            if coding_loop_limit is not None:
                identity = _coding_loop_identity(strategy)
                if identity != coding_loop_identity:
                    coding_loop_identity = identity
                    coding_loop_roundtrips = 0
                if coding_loop_roundtrips >= coding_loop_limit:
                    exhausted = getattr(strategy, "coding_loop_exhausted", None)
                    if callable(exhausted):
                        return exhausted(coding_loop_limit)
                    return 2
            request = strategy.next_request()
            if request is None:
                terminal_exit_code = getattr(strategy, "terminal_exit_code", None)
                return terminal_exit_code if isinstance(terminal_exit_code, int) else 0
            self.repair_coordinator.begin_boundary(_boundary_id(strategy))
            roundtrips += 1
            try:
                action = (
                    request.request_action()
                    if request.request_action is not None
                    else self.action_engine.request_action(
                        client=request.client,
                        messages=request.messages,
                        parser=request.parser,
                        model=request.model,
                        stderr=request.stderr,
                        max_timeout_retries=request.max_timeout_retries,
                        timeout_backoff_seconds=request.timeout_backoff_seconds,
                        response_schema=request.response_schema,
                    )
                )
            except WorkflowLLMTimeoutExhausted:
                raise
            except WorkflowLLMExecutionAborted as exc:
                return exc.exit_code
            except (
                ProviderExecutionError,
                PersistenceCorruptionError,
                ProgrammerInvariantError,
                ExecutionCancelled,
            ):
                # These failures are not model-correctable action errors.
                raise
            except RuntimeError as exc:
                directive = self._record_semantic_failure(
                    strategy,
                    RepairFailure(
                        classify_repair_failure(
                            getattr(exc, "error_code", type(exc).__name__),
                            default=RepairFailureClass.RESPONSE,
                        ),
                        getattr(exc, "error_code", type(exc).__name__),
                        str(exc),
                    ),
                )
                if self.observer is not None:
                    try:
                        self.observer.response_failed(exc)
                    except Exception:
                        pass
                strategy.record_response_error(exc, self.action_engine.last_payload)
                deterministic_outcome = self._apply_deterministic_repair(
                    strategy, directive, self._last_repair_failure
                )
                if deterministic_outcome is not None:
                    return deterministic_outcome.exit_code or 0
                continue

            strategy.report_roundtrip(roundtrips, action)
            if coding_loop_limit is not None:
                coding_loop_roundtrips += 1
            if self.runtime is not None:
                guidance = getattr(action, "decisions_and_context", None)
                if isinstance(guidance, str):
                    self.runtime.capture_explicit_guidance(
                        guidance,
                        source_ref=f"{self.runtime.execution_id}:roundtrip-{roundtrips}",
                    )
            relationship_errors = self.kernel.validate_proposal(action)
            contract_errors: tuple[str, ...] = ()
            if self.runtime is not None:
                contract_errors = self.runtime.validate_action(
                    str(getattr(action, "kind", ""))
                )
            proposal_errors = (*relationship_errors, *contract_errors)
            if proposal_errors:
                contract_violation = bool(contract_errors) and not relationship_errors
                error = PowdrrExecutionError(
                    " ".join(proposal_errors),
                    error_code=(
                        "step_contract_action_not_allowed"
                        if contract_violation
                        else "relationship_obligation_open"
                    ),
                    action_kind=str(getattr(action, "kind", "action")),
                    remediation=(
                        "Choose an action declared by the active step contract."
                        if contract_violation
                        else "perform the required follow-up action first"
                    ),
                )
                strategy.record_action_error(action, error)
                directive = self._record_semantic_failure(
                    strategy,
                    RepairFailure(
                        classify_repair_failure(
                            error.error_code, default=RepairFailureClass.PROPOSAL
                        ),
                        error.error_code,
                        str(error),
                        action_signature=signature(action),
                        target_signature=workflow_action_target_signature(action),
                    ),
                )
                self.kernel.fail(action, error)
                self._sync_runtime()
                deterministic_outcome = self._apply_deterministic_repair(
                    strategy, directive, self._last_repair_failure
                )
                if deterministic_outcome is not None:
                    return deterministic_outcome.exit_code or 0
                if self.observer is not None:
                    try:
                        proposal_decision = self.observer.action_failed(action, error)
                    except Exception:
                        proposal_decision = None
                    if proposal_decision is not None:
                        apply_decision = getattr(
                            strategy, "apply_observer_decision", None
                        )
                        if callable(apply_decision):
                            apply_decision(proposal_decision, action, None)
                continue
            self.kernel.propose(action)
            self._sync_runtime()
            self._record_shadow("action_proposed", action)
            proposal_decision = None
            if self.observer is not None:
                propose = getattr(self.observer, "action_proposed", None)
                if callable(propose):
                    try:
                        proposal_decision = propose(action)
                    except Exception:
                        proposal_decision = None
            if proposal_decision is not None:
                apply_decision = getattr(strategy, "apply_observer_decision", None)
                if callable(apply_decision) and apply_decision(
                    proposal_decision, action, None
                ):
                    continue
            before_state = strategy.material_state(action)
            try:
                self.kernel.start(action)
                self._sync_runtime()
                outcome = strategy.execute_action(action)
            except PowdrrExecutionError as exc:
                self.kernel.fail(action, exc)
                self._sync_runtime()
                self._record_shadow(
                    "action_failed",
                    action,
                    error_code=(
                        exc.error_code
                        if isinstance(exc, PowdrrExecutionError)
                        else type(exc).__name__
                    ),
                )
                strategy.record_action_error(action, exc)
                directive = self._record_semantic_failure(
                    strategy,
                    RepairFailure(
                        classify_repair_failure(
                            exc.error_code, default=RepairFailureClass.ACTION_EXECUTION
                        ),
                        exc.error_code,
                        str(exc),
                        action_signature=signature(action),
                        target_signature=workflow_action_target_signature(action),
                    ),
                )
                failure_decision = None
                if self.observer is not None:
                    try:
                        failure_decision = self.observer.action_failed(action, exc)
                    except Exception:
                        failure_decision = None
                if failure_decision is not None:
                    apply_decision = getattr(strategy, "apply_observer_decision", None)
                    if callable(apply_decision):
                        apply_decision(failure_decision, action, None)
                deterministic_outcome = self._apply_deterministic_repair(
                    strategy, directive, self._last_repair_failure
                )
                if deterministic_outcome is not None:
                    return deterministic_outcome.exit_code or 0
                if (
                    self.action_engine.record_action_failure(
                        action,
                        signature=signature,
                    )
                    == ProgressDecision.THRESHOLD
                ):
                    failure_exit_code = strategy.action_failure_exit_code(action)
                    if failure_exit_code is not None:
                        return failure_exit_code
                continue

            self.kernel.complete(action)
            self._sync_runtime()
            self._record_shadow("action_completed", action)

            observation = self.action_engine.observe_action(
                action,
                signature=signature,
                before_state=before_state,
                after_state=strategy.material_state(action),
            )
            if not observation.made_progress:
                strategy.record_no_progress(action, observation)
                directive = self._record_semantic_failure(
                    strategy,
                    RepairFailure(
                        classify_repair_failure(
                            "no_progress",
                            default=RepairFailureClass.NO_MATERIAL_PROGRESS,
                        ),
                        "no_progress",
                        observation.correction or "The action made no progress.",
                        action_signature=signature(action),
                        target_signature=workflow_action_target_signature(action),
                    ),
                )
                deterministic_outcome = self._apply_deterministic_repair(
                    strategy, directive, self._last_repair_failure
                )
                if deterministic_outcome is not None:
                    return deterministic_outcome.exit_code or 0
                if observation.decision is ProgressDecision.THRESHOLD:
                    stop_after_stall = getattr(
                        strategy, "no_progress_threshold_exit_code", None
                    )
                    if callable(stop_after_stall):
                        exit_code = stop_after_stall(action, observation)
                        if exit_code is not None:
                            return exit_code
            outcome = strategy.observe_outcome(action, observation, outcome)
            observer_decision = None
            if self.observer is not None:
                try:
                    observer_decision = self.observer.action_completed(
                        action, observation
                    )
                except Exception:
                    observer_decision = None
            if observer_decision is not None:
                apply_decision = getattr(strategy, "apply_observer_decision", None)
                if callable(apply_decision):
                    apply_decision(observer_decision, action, observation)
            if observation.made_progress and observer_decision is None:
                clear_intervention = getattr(
                    strategy, "clear_observer_intervention", None
                )
                if callable(clear_intervention):
                    clear_intervention()
            if outcome.exit_code is not None:
                return outcome.exit_code
            if not outcome.continue_running:
                return 0
        return 0

    def _record_semantic_failure(
        self,
        strategy: WorkflowExecutionStrategy,
        failure: RepairFailure,
    ) -> RepairDirective | None:
        self._last_repair_failure = failure
        try:
            allowed_actions = (
                self.runtime.allowed_actions() if self.runtime is not None else ()
            )
            directive = self.repair_coordinator.record_failure(
                failure,
                allowed_actions=allowed_actions or (),
                context=(
                    repair_context()
                    if callable(
                        repair_context := getattr(strategy, "repair_context", None)
                    )
                    else None
                ),
            )
        except ProgrammerInvariantError:
            return None
        self.last_repair_directive = directive
        apply_directive = getattr(strategy, "record_repair_directive", None)
        if callable(apply_directive):
            apply_directive(directive)
        return directive

    def _apply_deterministic_repair(
        self,
        strategy: WorkflowExecutionStrategy,
        directive: RepairDirective | None,
        failure: RepairFailure | None,
    ) -> WorkflowActionOutcome | None:
        if (
            directive is None
            or failure is None
            or directive.stage is not RepairStage.DETERMINISTIC
        ):
            return None
        deterministic = getattr(strategy, "deterministic_repair_action", None)
        if not callable(deterministic):
            return None
        action = deterministic(failure, directive)
        if action is None:
            return None
        relationship_errors = self.kernel.validate_proposal(action)
        contract_errors: tuple[str, ...] = ()
        if self.runtime is not None:
            contract_errors = self.runtime.validate_action(
                str(getattr(action, "kind", ""))
            )
        proposal_errors = (*relationship_errors, *contract_errors)
        if proposal_errors:
            error = PowdrrExecutionError(
                " ".join(proposal_errors),
                error_code="deterministic_repair_not_allowed",
            )
            strategy.record_action_error(action, error)
            self.kernel.fail(action, error)
            self._sync_runtime()
            return None
        self.kernel.propose(action)
        self._sync_runtime()
        self._record_shadow("action_proposed", action)
        try:
            self.kernel.start(action)
            self._sync_runtime()
            outcome = strategy.execute_action(action)
        except PowdrrExecutionError as error:
            self.kernel.fail(action, error)
            self._sync_runtime()
            self._record_shadow("action_failed", action, error_code=error.error_code)
            strategy.record_action_error(action, error)
            return None
        self.kernel.complete(action)
        self._sync_runtime()
        self._record_shadow("action_completed", action)
        return outcome

    def _record_shadow(
        self,
        event_type: str,
        action: Any,
        *,
        error_code: str | None = None,
    ) -> None:
        if self.shadow_recorder is None:
            return
        try:
            self.shadow_recorder.record_action(
                event_type,
                action,
                error_code=error_code,
            )
        except Exception:
            # Shadow observation cannot make a working legacy execution fail.
            return

    def _sync_runtime(self) -> None:
        if self.runtime is not None:
            self.runtime.sync_kernel(
                phase_type=self.phase_type,
                actor_id=self.actor_id,
            )


def _is_idempotent_success(action: Any) -> bool:
    """Treat successful repeated staging as progress, even when state is unchanged."""
    if getattr(action, "kind", None) == "invoke_tool":
        tool = getattr(action, "tool", None)
        parameters = getattr(action, "parameters", None)
    elif isinstance(action, Mapping):
        tool = action.get("tool")
        parameters = action.get("parameters")
        if (
            action.get("action") != "invoke_tool"
            and action.get("kind") != "invoke_tool"
        ):
            return False
    else:
        return False
    if not isinstance(parameters, Mapping):
        return False
    if tool == "git":
        return parameters.get("operation") in {"add", "commit", "switch"}
    return tool == "gh" and parameters.get("operation") == "pr_create"


class WorkflowLLMActionEngine:
    """Own JSON parsing and repeated-action accounting for a workflow session."""

    def __init__(self, *, max_stalled_roundtrips: int) -> None:
        self._controller = WorkflowExecutionController(max_stalled_roundtrips)
        self.last_payload: dict[str, Any] | None = None

    @property
    def previous_action_signature(self) -> str | None:
        return self._controller.previous_action_signature

    @property
    def stalled_roundtrips(self) -> int:
        return self._controller.stalled_roundtrips

    def request_action(
        self,
        *,
        client: WorkflowLLMClient,
        messages: list[dict[str, str]],
        parser: Callable[[dict[str, Any]], ActionT],
        model: str,
        stderr: Any,
        max_timeout_retries: int,
        timeout_backoff_seconds: float,
        response_schema: Mapping[str, Any] | None = None,
    ) -> ActionT:
        """Make one LLM request and parse its sole workflow action."""
        payload = complete_json_with_timeout_retry(
            client,
            messages,
            model=model,
            stderr=stderr,
            max_timeout_retries=max_timeout_retries,
            timeout_backoff_seconds=timeout_backoff_seconds,
            response_schema=response_schema,
        )
        self.last_payload = payload
        return parser(payload)

    def observe_action(
        self,
        action: ActionT,
        *,
        signature: Callable[[ActionT], str],
        before_state: object,
        after_state: object,
    ) -> WorkflowActionObservation:
        """Apply the same material-progress rule to either workflow adapter.

        An action is progress when it completes the task or workflow, differs
        materially from the previous action, is the first action in a sequence,
        or changes the adapter's material state snapshot. The durable-task
        adapter treats ``next_step`` as a terminal task-completion outcome, so
        it exits before a repeated-action check can misclassify it.
        """
        action_signature = workflow_action_failure_signature(
            action, signature=signature
        )
        kind = getattr(action, "kind", "")
        made_progress = (
            kind == "complete"
            or _is_idempotent_success(action)
            or self._controller.previous_action_signature is None
            or action_signature != self._controller.previous_action_signature
            or before_state != after_state
        )
        decision = self._controller.observe(
            action_signature,
            made_progress=made_progress,
        )
        return WorkflowActionObservation(
            signature=action_signature,
            made_progress=made_progress,
            decision=decision,
            correction=(
                no_progress_feedback(action_signature) if not made_progress else None
            ),
        )

    def begin_action(
        self,
        action: ActionT,
        *,
        strategy: WorkflowActionProgressStrategy[ActionT],
    ) -> object:
        """Capture the adapter's material state before executing an action."""
        return strategy.material_state(action)

    def complete_action(
        self,
        action: ActionT,
        *,
        before_state: object,
        signature: Callable[[ActionT], str],
        strategy: WorkflowActionProgressStrategy[ActionT],
    ) -> WorkflowActionObservation:
        """Observe an executed action and delegate only reporting to its adapter."""
        observation = self.observe_action(
            action,
            signature=signature,
            before_state=before_state,
            after_state=strategy.material_state(action),
        )
        if not observation.made_progress:
            strategy.record_no_progress(action, observation)
        return observation

    def record_action_failure(
        self,
        action: ActionT,
        *,
        signature: Callable[[ActionT], str],
    ) -> ProgressDecision:
        """Count a rejected action with the same threshold as stalled actions."""
        return self._controller.record_failure(
            workflow_action_failure_signature(action, signature=signature)
        )

    def reset_progress(self) -> None:
        self._controller.reset()


def workflow_action_signature(action: object) -> str:
    """Serialize an action consistently for corrective prompts and tracking."""
    if hasattr(action, "__dataclass_fields__"):
        from dataclasses import asdict

        value: object = asdict(cast(Any, action))
    else:
        value = action
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


def workflow_action_failure_signature[ActionT](
    action: ActionT,
    *,
    signature: Callable[[ActionT], str],
) -> str:
    """Normalize rejected actions so narrative changes do not evade the guard."""
    serialized = signature(action)
    try:
        value = json.loads(serialized)
    except (TypeError, ValueError):
        return serialized
    if not isinstance(value, dict):
        return serialized
    for field_name in ("decisions_and_context", "llm_type", "outputs"):
        value.pop(field_name, None)
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


def workflow_action_target_signature(action: object) -> str:
    """Return a stable resource-level identity for rejected action strategies."""
    kind = str(getattr(action, "kind", "action"))
    target_fields = {
        "file_path": getattr(action, "file_path", None),
        "file_paths": tuple(
            getattr(edit, "file_path", "") for edit in getattr(action, "file_edits", ())
        ),
        "tool": getattr(action, "tool", None),
        "skill_name": getattr(action, "skill_name", None),
        "step_id": getattr(action, "step_id", None),
        "destination_path": getattr(action, "destination_path", None),
        "file_operation": getattr(action, "file_operation", None),
    }
    target = {
        name: value
        for name, value in target_fields.items()
        if value not in (None, "", ())
    }
    return json.dumps(
        {"kind": kind, "target": target},
        sort_keys=True,
        ensure_ascii=False,
        default=str,
    )


def workflow_action_summary(action: object) -> str:
    """Return a short human-readable explanation of a proposed action."""
    kind = str(getattr(action, "kind", "action"))
    detail_by_kind = {
        "gather_context": "types=" + ",".join(getattr(action, "types", ())),
        "prompt_user": getattr(action, "text", None),
        "edit": getattr(action, "file_path", None),
        "yaml_edit": getattr(action, "file_path", None),
        "file_management": (
            f"{getattr(action, 'file_operation', None)} "
            f"{getattr(action, 'file_path', None)}"
        ),
        "invoke_skill": getattr(action, "skill_name", None),
        "goto_step": getattr(action, "step_id", None),
        "invoke_tool": getattr(action, "tool", None),
        "read_document": getattr(action, "file_path", None),
    }
    detail = detail_by_kind.get(kind)
    summary = kind if not detail else f"{kind} ({detail})"
    rationale = getattr(action, "decisions_and_context", None)
    if isinstance(rationale, str) and rationale.strip():
        summary += " — " + " ".join(rationale.split())
    return summary.rstrip()


def prune_execution_events(
    events: Sequence[Mapping[str, Any]],
    *,
    include_results: bool,
) -> list[dict[str, Any]]:
    """Bound recurring action context without losing durable diagnostics.

    Chat records substantive file and tool output separately in its transcript
    and execution context, so it only sends event metadata.  Durable tasks need
    the result of prior actions to decide their output state, but still receive
    a bounded representation.  Both paths consequently apply the same event
    count and per-event size limits.
    """
    prompt_events: list[dict[str, Any]] = []
    for event in events[-_MAX_PROMPT_EVENTS:]:
        prompt_event = dict(event)
        if not include_results:
            prompt_event.pop("result", None)
        elif "result" in prompt_event:
            result_text = json.dumps(
                prompt_event["result"], ensure_ascii=False, default=str
            )
            if len(result_text) > _MAX_PROMPT_EVENT_CHARS:
                prompt_event["result"] = {
                    "truncated": True,
                    "preview": result_text[:_MAX_PROMPT_EVENT_CHARS],
                }
        prompt_events.append(_bound_prompt_value(prompt_event))
    return prompt_events


def _bound_prompt_value(value: Any, *, depth: int = 0) -> Any:
    """Bound nested event fields, including templates and command metadata."""
    if depth > 6:
        return "<nested prompt value omitted>"
    if isinstance(value, str):
        return (
            value
            if len(value) <= _MAX_PROMPT_EVENT_CHARS
            else value[:_MAX_PROMPT_EVENT_CHARS]
        )
    if isinstance(value, Mapping):
        return {
            str(key): _bound_prompt_value(item, depth=depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        items = value[-32:] if len(value) > 32 else value
        return [_bound_prompt_value(item, depth=depth + 1) for item in items]
    return value
