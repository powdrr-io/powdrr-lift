"""Sparse, shared shadow observation for LLM-driven workflow execution."""

from __future__ import annotations

import hashlib
import json
from collections import Counter, deque
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field, is_dataclass
from pathlib import Path
from typing import Any, Literal, Protocol, cast

from powdrr_lift.repository_state import repository_state
from powdrr_lift.workflow_error_logging import (
    WORKFLOW_LLM_ERROR_LOG,
    WORKFLOW_OBSERVER_LOG,
    record_workflow_observer_event,
)
from powdrr_lift.workflow_llm import (
    ProgressDecision,
    WorkflowActionObservation,
    WorkflowLLMClient,
)

ObserverTriggerKind = Literal[
    "repeated_action",
    "repeated_failure",
    "semantic_stall",
    "repair_regression",
    "human_prompt",
    "step_transition",
    "completion",
    "pull_request_creation",
]
ObserverVerdict = Literal[
    "continue",
    "coach",
    "redirect",
    "block_transition",
    "request_human",
]

_SHADOW_LLM_TRIGGERS: frozenset[ObserverTriggerKind] = frozenset(
    {"repeated_action", "repeated_failure", "semantic_stall", "repair_regression"}
)
_ALLOWED_VERDICTS: frozenset[str] = frozenset(
    {"continue", "coach", "redirect", "block_transition", "request_human"}
)


def _compact_text(value: str, *, limit: int = 1_200) -> str:
    if len(value) <= limit:
        return value
    return value[:limit] + f"... <{len(value) - limit} characters omitted>"


def compact_observer_mapping(value: Mapping[str, object]) -> Mapping[str, object]:
    """Bound heterogeneous adapter state before it enters packets and logs."""
    rendered = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    if len(rendered) <= 2_000:
        return value
    return {
        "truncated": True,
        "summary": _compact_text(rendered, limit=2_000),
    }


class ObserverContextProvider(Protocol):
    def __call__(self) -> ObserverExecutionContext: ...


@dataclass(frozen=True, slots=True)
class ObserverTrigger:
    kind: ObserverTriggerKind
    reason: str
    priority: Literal["normal", "high"] = "normal"


@dataclass(frozen=True, slots=True)
class ObserverActionSummary:
    index: int
    action: str
    made_progress: bool | None
    outcome: str


@dataclass(frozen=True, slots=True)
class ObserverFailureSummary:
    index: int
    action: str | None
    error_type: str
    error: str


@dataclass(frozen=True, slots=True)
class ObserverProgressState:
    action_index: int
    last_material_progress_action: int | None
    repeated_action_count: int
    repeated_failure_count: int


@dataclass(frozen=True, slots=True)
class ObserverExecutionContext:
    execution_mode: str
    root_intent: str
    skill_or_workflow: str
    current_step_id: str
    current_step_intent: str
    validation_state: Mapping[str, object] = field(default_factory=dict)
    handoff_state: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ObserverPacket:
    execution_mode: str
    trigger: ObserverTrigger
    root_intent: str
    skill_or_workflow: str
    current_step_id: str
    current_step_intent: str
    recent_actions: tuple[ObserverActionSummary, ...]
    recent_failures: tuple[ObserverFailureSummary, ...]
    changed_files: tuple[str, ...]
    validation_state: Mapping[str, object]
    handoff_state: Mapping[str, object]
    progress_state: ObserverProgressState


@dataclass(frozen=True, slots=True)
class ObserverDecision:
    verdict: ObserverVerdict
    reason: str
    guidance: tuple[str, ...] = ()
    expected_progress: str | None = None
    target_step_id: str | None = None


@dataclass(slots=True)
class ObserverState:
    last_fingerprint: str | None = None
    last_observed_action_index: int | None = None
    last_decision: ObserverDecision | None = None
    intervention_pending: bool = False
    observation_epoch: int = 0


def observer_packet_fingerprint(packet: ObserverPacket, repo_root: Path) -> str:
    """Return a stable digest of the material state presented to the observer."""
    changed_file_hashes: dict[str, str | None] = {}
    for relative_path in packet.changed_files:
        path = repo_root / relative_path
        try:
            changed_file_hashes[relative_path] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
        except OSError:
            changed_file_hashes[relative_path] = None
    material = {
        "execution_mode": packet.execution_mode,
        "trigger": packet.trigger.kind,
        "skill_or_workflow": packet.skill_or_workflow,
        "current_step_id": packet.current_step_id,
        "changed_file_hashes": changed_file_hashes,
        "validation_state": packet.validation_state,
        "handoff_state": packet.handoff_state,
        "latest_failure": (
            {
                "action": packet.recent_failures[-1].action,
                "error_type": packet.recent_failures[-1].error_type,
                "error": packet.recent_failures[-1].error,
            }
            if packet.recent_failures
            else None
        ),
    }
    encoded = json.dumps(material, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_observer_messages(packet: ObserverPacket) -> list[dict[str, str]]:
    """Build the deliberately compact shadow-observer prompt."""
    example = {
        "verdict": "coach",
        "reason": "The same failing edit was attempted twice without progress.",
        "guidance": [
            "Re-read the latest validator result.",
            "Choose an action with a materially different target or operation.",
        ],
        "expected_progress": "The next action changes state or reduces errors.",
        "target_step_id": None,
    }
    return [
        {
            "role": "system",
            "content": (
                "You are a read-only workflow observer. Diagnose whether the working "
                "agent remains aligned and how it could recover. Do not invoke tools, "
                "edit files, or claim that you did. Return exactly one JSON object. "
                "Allowed verdicts: continue, coach, redirect, block_transition, "
                "request_human. Complete example:\n"
                + json.dumps(example, ensure_ascii=False)
            ),
        },
        {
            "role": "user",
            "content": json.dumps(asdict(packet), ensure_ascii=False, default=str),
        },
    ]


def parse_observer_decision(payload: Mapping[str, Any]) -> ObserverDecision:
    verdict = payload.get("verdict")
    if verdict not in _ALLOWED_VERDICTS:
        raise ValueError(
            "Observer response verdict must be one of: "
            + ", ".join(sorted(_ALLOWED_VERDICTS))
        )
    reason = payload.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("Observer response reason must be a non-empty string.")
    guidance = payload.get("guidance", [])
    if not isinstance(guidance, list) or not all(
        isinstance(item, str) and item.strip() for item in guidance
    ):
        raise ValueError("Observer response guidance must be a list of strings.")
    expected_progress = payload.get("expected_progress")
    target_step_id = payload.get("target_step_id")
    if expected_progress is not None and not isinstance(expected_progress, str):
        raise ValueError("Observer expected_progress must be a string or null.")
    if target_step_id is not None and not isinstance(target_step_id, str):
        raise ValueError("Observer target_step_id must be a string or null.")
    return ObserverDecision(
        verdict=cast(ObserverVerdict, verdict),
        reason=reason.strip(),
        guidance=tuple(item.strip() for item in guidance),
        expected_progress=expected_progress,
        target_step_id=target_step_id,
    )


def _action_signature(action: Any) -> str:
    if is_dataclass(action) and not isinstance(action, type):
        value: Any = asdict(action)
    elif isinstance(action, Mapping):
        value = dict(action)
    else:
        value = str(action)
    return json.dumps(value, sort_keys=True, default=str)


def _action_kind(action: Any) -> str:
    if isinstance(action, Mapping):
        return str(action.get("kind") or action.get("action") or "")
    return str(getattr(action, "kind", ""))


def _is_pull_request_creation(action: Any) -> bool:
    if _action_kind(action) != "invoke_tool":
        return False
    tool = (
        action.get("tool")
        if isinstance(action, Mapping)
        else getattr(action, "tool", None)
    )
    parameters = (
        action.get("parameters", {})
        if isinstance(action, Mapping)
        else getattr(action, "parameters", {})
    )
    rendered = json.dumps(parameters, sort_keys=True, default=str).casefold()
    return tool == "gh" and "pr" in rendered and "create" in rendered


class ShadowWorkflowObserver:
    """Detect sparse observer triggers and log advisory decisions only."""

    def __init__(
        self,
        *,
        client: WorkflowLLMClient,
        model: str,
        provider: str,
        worktree_root: Path,
        log_root: Path,
        context_provider: ObserverContextProvider,
    ) -> None:
        self.client = client
        self.model = model
        self.provider = provider
        self.worktree_root = worktree_root
        self.log_root = log_root
        self.context_provider = context_provider
        self.state = ObserverState()
        self._actions: deque[ObserverActionSummary] = deque(maxlen=6)
        self._failures: deque[ObserverFailureSummary] = deque(maxlen=4)
        self._action_counts: Counter[str] = Counter()
        self._failure_counts: Counter[str] = Counter()
        self._last_action_signature: str | None = None
        self._last_failure_signature: str | None = None
        self._last_step_id: str | None = None
        self._last_material_progress_action: int | None = None
        self._action_index = 0
        self._last_validation_issue_count: int | None = None

    def response_failed(self, error: Exception) -> None:
        count = self._record_failure(None, error)
        if count >= 2:
            self._trigger(
                ObserverTrigger(
                    kind="repeated_failure",
                    reason="The same LLM response failure occurred at least twice.",
                    priority="high",
                )
            )

    def action_failed(self, action: Any, error: Exception) -> None:
        signature = _action_signature(action)
        count = self._record_failure(signature, error)
        if count >= 2:
            self._trigger(
                ObserverTrigger(
                    kind="repeated_failure",
                    reason="The same action failure occurred at least twice.",
                    priority="high",
                )
            )

    def action_completed(
        self,
        action: Any,
        observation: WorkflowActionObservation,
    ) -> None:
        self._action_index += 1
        signature = observation.signature or _action_signature(action)
        if signature == self._last_action_signature:
            self._action_counts[signature] += 1
        else:
            self._action_counts.clear()
            self._action_counts[signature] = 1
        self._last_action_signature = signature
        if observation.made_progress:
            self._last_material_progress_action = self._action_index
        self._actions.append(
            ObserverActionSummary(
                index=self._action_index,
                action=_compact_text(signature),
                made_progress=observation.made_progress,
                outcome=observation.decision.value,
            )
        )

        context = self._safe_context()
        if context is None:
            return
        if (
            self._last_step_id is not None
            and context.current_step_id != self._last_step_id
        ):
            self._trigger(
                ObserverTrigger(
                    kind="step_transition",
                    reason=(
                        f"Execution moved from {self._last_step_id!r} to "
                        f"{context.current_step_id!r}."
                    ),
                ),
                context=context,
            )
        self._last_step_id = context.current_step_id

        diagnostic_trigger: ObserverTrigger | None = None
        issue_count = _validation_issue_count(context.validation_state)
        if (
            issue_count is not None
            and self._last_validation_issue_count is not None
            and issue_count > self._last_validation_issue_count
        ):
            diagnostic_trigger = ObserverTrigger(
                kind="repair_regression",
                reason=(
                    "Validation issue count increased after the preceding "
                    "workflow action."
                ),
                priority="high",
            )
        self._last_validation_issue_count = issue_count

        if not observation.made_progress and self._action_counts[signature] >= 2:
            trigger_kind: ObserverTriggerKind = (
                "semantic_stall"
                if observation.decision == ProgressDecision.THRESHOLD
                else "repeated_action"
            )
            if diagnostic_trigger is None:
                diagnostic_trigger = ObserverTrigger(
                    kind=trigger_kind,
                    reason="The same action repeated without material progress.",
                    priority="high",
                )

        if diagnostic_trigger is not None:
            self._trigger(diagnostic_trigger, context=context)

        action_kind = _action_kind(action)
        if action_kind in {"prompt_user", "get-human-input"}:
            self._trigger(
                ObserverTrigger(
                    kind="human_prompt",
                    reason="The working agent requested human input.",
                ),
                context=context,
            )
        if _is_pull_request_creation(action):
            self._trigger(
                ObserverTrigger(
                    kind="pull_request_creation",
                    reason="The working agent requested pull-request creation.",
                ),
                context=context,
            )
        if action_kind == "complete":
            self._trigger(
                ObserverTrigger(
                    kind="completion",
                    reason="The working agent requested completion.",
                ),
                context=context,
            )

    def _record_failure(self, action_signature: str | None, error: Exception) -> int:
        self._action_index += 1
        failure_signature = json.dumps(
            {
                "action": action_signature,
                "error_type": type(error).__name__,
                "error": str(error),
            },
            sort_keys=True,
        )
        if failure_signature == self._last_failure_signature:
            self._failure_counts[failure_signature] += 1
        else:
            self._failure_counts.clear()
            self._failure_counts[failure_signature] = 1
        self._last_failure_signature = failure_signature
        self._failures.append(
            ObserverFailureSummary(
                index=self._action_index,
                action=(
                    _compact_text(action_signature)
                    if action_signature is not None
                    else None
                ),
                error_type=type(error).__name__,
                error=_compact_text(str(error)),
            )
        )
        return self._failure_counts[failure_signature]

    def _safe_context(self) -> ObserverExecutionContext | None:
        try:
            return self.context_provider()
        except Exception as error:  # observer diagnostics must never stop execution
            self._log_internal_failure("context", error)
            return None

    def _changed_files(self) -> tuple[str, ...]:
        try:
            state = repository_state(self.worktree_root)
            files = state.get("files", [])
            return tuple(
                sorted(
                    str(item["path"])
                    for item in files
                    if isinstance(item, Mapping)
                    and item.get("path")
                    and item.get("path")
                    not in {WORKFLOW_LLM_ERROR_LOG, WORKFLOW_OBSERVER_LOG}
                )
            )
        except Exception as error:  # observer diagnostics must never stop execution
            self._log_internal_failure("repository_state", error)
            return ()

    def _packet(
        self,
        trigger: ObserverTrigger,
        context: ObserverExecutionContext,
    ) -> ObserverPacket:
        action_count = (
            self._action_counts[self._last_action_signature]
            if self._last_action_signature is not None
            else 0
        )
        failure_count = (
            self._failure_counts[self._last_failure_signature]
            if self._last_failure_signature is not None
            else 0
        )
        return ObserverPacket(
            execution_mode=context.execution_mode,
            trigger=trigger,
            root_intent=_compact_text(context.root_intent),
            skill_or_workflow=context.skill_or_workflow,
            current_step_id=context.current_step_id,
            current_step_intent=_compact_text(context.current_step_intent),
            recent_actions=tuple(self._actions),
            recent_failures=tuple(self._failures),
            changed_files=(
                self._changed_files() if trigger.kind in _SHADOW_LLM_TRIGGERS else ()
            ),
            validation_state=compact_observer_mapping(context.validation_state),
            handoff_state=compact_observer_mapping(context.handoff_state),
            progress_state=ObserverProgressState(
                action_index=self._action_index,
                last_material_progress_action=self._last_material_progress_action,
                repeated_action_count=action_count,
                repeated_failure_count=failure_count,
            ),
        )

    def _trigger(
        self,
        trigger: ObserverTrigger,
        *,
        context: ObserverExecutionContext | None = None,
    ) -> None:
        context = context or self._safe_context()
        if context is None:
            return
        packet = self._packet(trigger, context)
        fingerprint = observer_packet_fingerprint(packet, self.worktree_root)
        if fingerprint == self.state.last_fingerprint:
            return
        should_call = trigger.kind in _SHADOW_LLM_TRIGGERS
        decision: ObserverDecision | None = None
        error: Exception | None = None
        if should_call:
            try:
                decision = parse_observer_decision(
                    self.client.complete_json(build_observer_messages(packet))
                )
                self.state.last_decision = decision
                self.state.last_observed_action_index = self._action_index
                self.state.observation_epoch += 1
            except Exception as observer_error:  # shadow failures are non-fatal
                error = observer_error
        self.state.last_fingerprint = fingerprint
        record_workflow_observer_event(
            self.log_root,
            execution_mode=context.execution_mode,
            trigger=trigger.kind,
            fingerprint=fingerprint,
            context={
                "skill_or_workflow": context.skill_or_workflow,
                "step_id": context.current_step_id,
                "model": self.model,
                "provider": self.provider,
                "shadow_mode": True,
                "llm_invoked": should_call,
            },
            packet=asdict(packet),
            decision=asdict(decision) if decision is not None else None,
            error=error,
        )

    def _log_internal_failure(self, phase: str, error: Exception) -> None:
        record_workflow_observer_event(
            self.log_root,
            execution_mode="observer",
            trigger="internal_failure",
            fingerprint="",
            context={"phase": phase, "model": self.model, "provider": self.provider},
            error=error,
        )


def _validation_issue_count(value: object) -> int | None:
    """Find a compact issue count in heterogeneous validation summaries."""
    if isinstance(value, Mapping):
        for key in ("issue_count", "error_count", "remaining_issue_count"):
            count = value.get(key)
            if isinstance(count, int) and not isinstance(count, bool):
                return count
        issues = value.get("issues")
        if isinstance(issues, Sequence) and not isinstance(issues, (str, bytes)):
            return len(issues)
        nested = [
            count
            for item in value.values()
            if (count := _validation_issue_count(item)) is not None
        ]
        return sum(nested) if nested else None
    return None
