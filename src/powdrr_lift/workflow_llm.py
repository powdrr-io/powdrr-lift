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

from powdrr_lift.errors import (
    ExecutionCancelled,
    PersistenceCorruptionError,
    PowdrrExecutionError,
    ProgrammerInvariantError,
    ProviderExecutionError,
)
from powdrr_lift.execution.kernel import ActionKernel
from powdrr_lift.execution.runtime import ExecutionRuntime
from powdrr_lift.workflow_execution import (
    ProgressDecision,
    WorkflowExecutionController,
    no_progress_feedback,
)


class WorkflowLLMClient(Protocol):
    """Minimal provider surface used by every workflow runner."""

    def complete_json(self, messages: list[dict[str, str]]) -> dict[str, Any]: ...


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
StrategyActionT = TypeVar("StrategyActionT", contravariant=True)
_MAX_PROMPT_EVENTS = 32
_MAX_PROMPT_EVENT_CHARS = 8_000
_PROMPT_SIZE_CHARS_PER_TOKEN = 3
# A caller may opt into an unlimited loop for deterministic harnesses, but
# production entry points must always provide a finite budget.
DEFAULT_MAX_ROUNDTRIPS = 128


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
    item_index: int | None = None
    path: tuple[str, ...] = field(default_factory=tuple)
    value: Any = None


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


@dataclass(frozen=True, slots=True)
class WorkflowAction:
    """The action schema parsed for both chat and durable workflow tasks."""

    kind: str
    tool: str | None = None
    skill_name: str | None = None
    step_id: str | None = None
    file_path: str | None = None
    destination_path: str | None = None
    file_operation: str | None = None
    start_line: int | None = None
    end_line: int | None = None
    directory: str | None = None
    pattern: str | None = None
    recursive: bool = False
    text: str | None = None
    output_state: Any = None
    outputs: dict[str, Any] = field(default_factory=dict)
    parameters: dict[str, Any] = field(default_factory=dict)
    edits: tuple[WorkflowEdit, ...] = field(default_factory=tuple)
    file_edits: tuple[WorkflowFileEdits, ...] = field(default_factory=tuple)
    yaml_operations: tuple[WorkflowYamlOperation, ...] = field(default_factory=tuple)
    types: tuple[str, ...] = field(default_factory=tuple)
    feature_id: str | None = None
    keywords: tuple[str, ...] = field(default_factory=tuple)
    filters: dict[str, object] = field(default_factory=dict)
    decisions_and_context: str | None = None
    llm_type: str | None = None
    provider_role: Literal["normal", "adversarial"] | None = None
    clean: bool = False
    context: tuple[str, ...] = field(default_factory=tuple)
    # Durable task execution uses this only when persisting a human handoff.
    human_input: dict[str, Any] | None = None
    # True only when the agent explicitly accepts a matching observer transfer.
    observer_override: bool = False


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


class WorkflowExecutionObserver(Protocol):
    """Optional, failure-isolated observer of shared execution boundaries."""

    def response_failed(self, error: Exception) -> Any: ...

    def action_failed(self, action: Any, error: Exception) -> Any: ...

    def action_proposed(self, action: Any) -> Any: ...

    def action_completed(
        self,
        action: Any,
        observation: WorkflowActionObservation,
    ) -> Any: ...


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
    ) -> None:
        self.action_engine = WorkflowLLMActionEngine(
            max_stalled_roundtrips=max_stalled_roundtrips
        )
        self.observer = observer
        self.shadow_recorder = shadow_recorder
        self.runtime = runtime
        self.phase_type = phase_type
        self.actor_id = actor_id
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
            except (
                ProviderExecutionError,
                PersistenceCorruptionError,
                ProgrammerInvariantError,
                ExecutionCancelled,
            ):
                # These failures are not model-correctable action errors.
                raise
            except RuntimeError as exc:
                if self.observer is not None:
                    try:
                        self.observer.response_failed(exc)
                    except Exception:
                        pass
                strategy.record_response_error(exc, self.action_engine.last_payload)
                continue

            strategy.report_roundtrip(roundtrips, action)
            observer_override_checker = getattr(
                strategy, "observer_override_is_authorized", None
            )
            override_authorized = bool(
                callable(observer_override_checker)
                and observer_override_checker(action)
            )
            if self.runtime is not None:
                guidance = getattr(action, "decisions_and_context", None)
                if isinstance(guidance, str):
                    self.runtime.capture_explicit_guidance(
                        guidance,
                        source_ref=f"{self.runtime.execution_id}:roundtrip-{roundtrips}",
                    )
            proposal_errors = self.kernel.validate_proposal(action)
            if self.runtime is not None and not override_authorized:
                proposal_errors = (
                    *proposal_errors,
                    *self.runtime.validate_action(str(getattr(action, "kind", ""))),
                )
            if proposal_errors:
                error = PowdrrExecutionError(
                    " ".join(proposal_errors),
                    error_code="relationship_obligation_open",
                    action_kind=str(getattr(action, "kind", "action")),
                    remediation="perform the required follow-up action first",
                )
                strategy.record_action_error(action, error)
                self.kernel.fail(action, error)
                self._sync_runtime()
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
                if (
                    self.action_engine.record_action_failure(
                        action,
                        signature=signature,
                    )
                    == ProgressDecision.THRESHOLD
                ):
                    return strategy.action_failure_exit_code(action)
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
        return strategy.exhausted_roundtrips_exit_code()

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
