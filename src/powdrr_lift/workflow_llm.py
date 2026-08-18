"""Shared LLM request and workflow-action control primitives.

Both the interactive chat runner and the durable workflow-task runner use this
module for the parts of execution that must never drift: making a JSON LLM
request, retrying provider timeouts, parsing the proposed action, and deciding
whether a repeated action actually made progress.  The callers deliberately
keep only presentation and human-handoff policy.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, TypeVar, cast

from powdrr_lift.workflow_execution import (
    ProgressDecision,
    WorkflowExecutionController,
    no_progress_feedback,
)


class WorkflowLLMClient(Protocol):
    """Minimal provider surface used by every workflow runner."""

    def complete_json(self, messages: list[dict[str, str]]) -> dict[str, Any]: ...


class WorkflowLLMTimeoutExhausted(RuntimeError):
    """Raised when a provider request keeps timing out after its retry budget."""


class WorkflowLLMHTTPError(RuntimeError):
    """A provider response that includes an HTTP status code."""

    def __init__(self, provider: str, status_code: int, detail: str) -> None:
        self.status_code = status_code
        super().__init__(f"{provider} request failed with HTTP {status_code}: {detail}")


class WorkflowLLMExecutionAborted(RuntimeError):
    """Stop a shared execution loop after its adapter aborts a request."""

    def __init__(self, exit_code: int) -> None:
        super().__init__("Workflow execution was aborted by its adapter.")
        self.exit_code = exit_code


ActionT = TypeVar("ActionT")
StrategyActionT = TypeVar("StrategyActionT", contravariant=True)
_MAX_PROMPT_EVENTS = 32
_MAX_PROMPT_EVENT_CHARS = 8_000


@dataclass(frozen=True, slots=True)
class WorkflowEdit:
    """One line-based mutation in the shared workflow action contract."""

    kind: str
    start_line: int
    end_line: int | None = None
    text: str | None = None


@dataclass(frozen=True, slots=True)
class WorkflowFileEdits:
    """Line-based mutations for one file in a shared edit action."""

    file_path: str
    edits: tuple[WorkflowEdit, ...]


@dataclass(frozen=True, slots=True)
class WorkflowYamlOperation:
    """One structural mutation in a YAML workflow action."""

    operation: str
    section: str | None = None
    item_id: str | None = None
    path: tuple[str, ...] = field(default_factory=tuple)
    value: Any = None


@dataclass(frozen=True, slots=True)
class WorkflowAction:
    """The action schema parsed for both chat and durable workflow tasks."""

    kind: str
    tool: str | None = None
    skill_name: str | None = None
    file_path: str | None = None
    start_line: int | None = None
    end_line: int | None = None
    text: str | None = None
    output_state: Any = None
    parameters: dict[str, Any] = field(default_factory=dict)
    edits: tuple[WorkflowEdit, ...] = field(default_factory=tuple)
    file_edits: tuple[WorkflowFileEdits, ...] = field(default_factory=tuple)
    yaml_operations: tuple[WorkflowYamlOperation, ...] = field(default_factory=tuple)
    types: tuple[str, ...] = field(default_factory=tuple)
    keywords: tuple[str, ...] = field(default_factory=tuple)
    filters: dict[str, object] = field(default_factory=dict)
    decisions_and_context: str | None = None
    llm_type: str | None = None
    provider_role: Literal["normal", "adversarial"] | None = None
    clean: bool = False
    context: tuple[str, ...] = field(default_factory=tuple)
    # Durable task execution uses this only when persisting a human handoff.
    human_input: dict[str, Any] | None = None


def complete_json(
    client: WorkflowLLMClient,
    messages: list[dict[str, str]],
) -> dict[str, Any]:
    """Make the single provider call used by workflow execution.

    Keeping this call in one module makes the exchange boundary explicit.  The
    provider clients remain transport implementations; runners must use an
    engine rather than calling ``complete_json`` themselves.
    """
    return client.complete_json(messages)


def is_timeout_error(error: RuntimeError) -> bool:
    message = str(error).casefold()
    return "timed out" in message or "timeout" in message


def is_retryable_provider_error(error: RuntimeError) -> bool:
    """Return whether a provider failure is transient enough to retry."""
    return is_timeout_error(error) or (
        isinstance(error, WorkflowLLMHTTPError) and error.status_code == 429
    )


def complete_json_with_timeout_retry(
    client: WorkflowLLMClient,
    messages: list[dict[str, str]],
    *,
    model: str,
    stderr: Any,
    max_timeout_retries: int,
    timeout_backoff_seconds: float,
) -> dict[str, Any]:
    """Request JSON with the common exponential transient-error retry policy."""
    retries = 0
    while True:
        try:
            return complete_json(client, messages)
        except RuntimeError as exc:
            if not is_retryable_provider_error(exc):
                raise
            if retries >= max(0, max_timeout_retries):
                raise WorkflowLLMTimeoutExhausted(
                    f"LLM request failed after {retries} retries: {exc}"
                ) from exc
            retries += 1
            delay_seconds = timeout_backoff_seconds * (2 ** (retries - 1))
            reason = "timed out" if is_timeout_error(exc) else "provider is overloaded"
            print(
                f"LLM request {reason} for {model}; retrying in "
                f"{delay_seconds:g} seconds "
                f"(retry {retries}/{max_timeout_retries}).",
                file=stderr,
                flush=True,
            )
            time.sleep(delay_seconds)


@dataclass(frozen=True, slots=True)
class WorkflowActionObservation:
    """The common result of evaluating one proposed workflow action."""

    signature: str
    made_progress: bool
    decision: ProgressDecision
    correction: str | None = None


class WorkflowActionProgressStrategy(Protocol[StrategyActionT]):
    """Adapter hooks for state snapshots and runner-specific reporting."""

    def material_state(self, action: StrategyActionT) -> object: ...

    def record_no_progress(
        self,
        action: StrategyActionT,
        observation: WorkflowActionObservation,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class WorkflowActionRequest:
    """One fully specified request for the next workflow action.

    The runner owns the LLM exchange.  Adapters only provide the current
    context and the parser appropriate for their durable or interactive
    boundary.
    """

    client: WorkflowLLMClient
    messages: list[dict[str, str]]
    parser: Callable[[dict[str, Any]], Any]
    model: str
    stderr: Any
    max_timeout_retries: int
    timeout_backoff_seconds: float
    request_action: Callable[[], Any] | None = None


@dataclass(frozen=True, slots=True)
class WorkflowActionOutcome:
    """The adapter's result after the shared runner executes an action."""

    continue_running: bool = True
    exit_code: int | None = None


class WorkflowExecutionStrategy(WorkflowActionProgressStrategy[Any], Protocol):
    """Boundary between the shared execution loop and its presentation mode.

    A strategy owns only its input/output boundary: building the current
    context, presenting status, and translating terminal or human-input
    actions into its native outcome.  The shared driver owns roundtrips,
    parsing, timeout handling, action-failure accounting, and no-progress
    correction.
    """

    def next_request(self) -> WorkflowActionRequest | None: ...

    def report_roundtrip(self, roundtrip: int, action: Any) -> None:
        """Present one parsed LLM action; adapters may leave this as a no-op."""
        _ = roundtrip, action

    def execute_action(self, action: Any) -> WorkflowActionOutcome: ...

    def record_response_error(
        self,
        error: RuntimeError,
        payload: dict[str, Any] | None,
    ) -> None: ...

    def record_action_error(self, action: Any, error: Exception) -> None: ...

    def action_failure_exit_code(self, action: Any) -> int: ...

    def observe_outcome(
        self,
        action: Any,
        observation: WorkflowActionObservation,
        outcome: WorkflowActionOutcome,
    ) -> WorkflowActionOutcome: ...

    def exhausted_roundtrips_exit_code(self) -> int: ...


class WorkflowLLMExecutionDriver:
    """Run workflow action roundtrips through one shared control loop.

    This is deliberately the only loop that combines an LLM response with
    action execution.  Chat and durable-task adapters cannot independently
    drift in parsing, corrective-action thresholds, or no-progress behavior.
    """

    def __init__(self, *, max_stalled_roundtrips: int) -> None:
        self.action_engine = WorkflowLLMActionEngine(
            max_stalled_roundtrips=max_stalled_roundtrips
        )

    def run(
        self,
        strategy: WorkflowExecutionStrategy,
        *,
        max_roundtrips: int | None,
        signature: Callable[[Any], str],
    ) -> int:
        roundtrips = 0
        while max_roundtrips is None or roundtrips < max(1, max_roundtrips):
            roundtrips += 1
            request = strategy.next_request()
            if request is None:
                return 0
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
                    )
                )
            except WorkflowLLMTimeoutExhausted:
                raise
            except WorkflowLLMExecutionAborted as exc:
                return exc.exit_code
            except RuntimeError as exc:
                strategy.record_response_error(exc, self.action_engine.last_payload)
                continue

            strategy.report_roundtrip(roundtrips, action)
            before_state = strategy.material_state(action)
            try:
                outcome = strategy.execute_action(action)
            except (RuntimeError, ValueError) as exc:
                strategy.record_action_error(action, exc)
                if (
                    self.action_engine.record_action_failure(
                        action,
                        signature=signature,
                    )
                    == ProgressDecision.THRESHOLD
                ):
                    return strategy.action_failure_exit_code(action)
                continue

            observation = self.action_engine.observe_action(
                action,
                signature=signature,
                before_state=before_state,
                after_state=strategy.material_state(action),
            )
            if not observation.made_progress:
                strategy.record_no_progress(action, observation)
            outcome = strategy.observe_outcome(action, observation, outcome)
            if outcome.exit_code is not None:
                return outcome.exit_code
            if not outcome.continue_running:
                return 0
        return strategy.exhausted_roundtrips_exit_code()


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
    ) -> ActionT:
        """Make one LLM request and parse its sole workflow action."""
        payload = complete_json_with_timeout_retry(
            client,
            messages,
            model=model,
            stderr=stderr,
            max_timeout_retries=max_timeout_retries,
            timeout_backoff_seconds=timeout_backoff_seconds,
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
        terminal_kinds: frozenset[str] = frozenset(
            {"complete", "next_step", "gather_context"}
        ),
    ) -> WorkflowActionObservation:
        """Apply the same material-progress rule to either workflow adapter.

        An action is progress when it transitions a workflow stage, differs from
        the previous action, is the first action in a sequence, or changes the
        adapter's material state snapshot.  Status output and human handoff do
        not influence this decision.
        """
        action_signature = signature(action)
        kind = getattr(action, "kind", "")
        made_progress = (
            kind in terminal_kinds
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
        return self._controller.record_failure(signature(action))

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


def workflow_action_summary(action: object) -> str:
    """Return a short human-readable explanation of a proposed action."""
    kind = str(getattr(action, "kind", "action"))
    detail_by_kind = {
        "gather_context": "types=" + ",".join(getattr(action, "types", ())),
        "prompt_user": getattr(action, "text", None),
        "edit": getattr(action, "file_path", None),
        "yaml_edit": getattr(action, "file_path", None),
        "invoke_skill": getattr(action, "skill_name", None),
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
        prompt_events.append(prompt_event)
    return prompt_events
