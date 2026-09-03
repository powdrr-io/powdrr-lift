"""Sparse, shared shadow observation for LLM-driven workflow execution."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
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
    {
        "repeated_action",
        "repeated_failure",
        "semantic_stall",
        "repair_regression",
        "human_prompt",
    }
)
_OBSERVER_COOLDOWN_ACTIONS = 4
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
    skill_definition: Mapping[str, object] = field(default_factory=dict)
    error_state: Mapping[str, object] = field(default_factory=dict)
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
    skill_definition: Mapping[str, object] = field(default_factory=dict)
    error_state: Mapping[str, object] = field(default_factory=dict)
    prior_decision: ObserverDecision | None = None


@dataclass(frozen=True, slots=True)
class ObserverDecision:
    verdict: ObserverVerdict
    reason: str
    guidance: tuple[str, ...] = ()
    expected_progress: str | None = None
    target_action: str | None = None
    target_step_id: str | None = None
    target_skill_name: str | None = None


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
        "skill_definition": packet.skill_definition,
        "error_state": packet.error_state,
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
    """Build a read-only diagnostic prompt with the complete execution contract."""
    example = {
        "verdict": "coach",
        "reason": "The same failing edit was attempted twice without progress.",
        "guidance": [
            "Re-read the latest validator result.",
            "Choose an action with a materially different target or operation.",
        ],
        "expected_progress": "The next action changes state or reduces errors.",
        "target_action": "read_document",
        "target_step_id": None,
        "target_skill_name": None,
    }
    return [
        {
            "role": "system",
            "content": (
                "You are a read-only workflow observer. Inspect the full skill, recent "
                "errors, and repository evidence. Diagnose whether the working agent "
                "remains aligned and how it could recover. Do not invoke tools, edit "
                "files, or claim that you did. Return exactly one JSON object. "
                "Allowed verdicts: continue, coach, redirect, block_transition, "
                "request_human. For a recommendation outside the current step "
                "contract, set target_action to the action kind the agent should "
                "try. For redirect, "
                "also set target_step_id to a prior step or target_skill_name to a "
                "catalog skill. Complete example:\n"
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
    target_action = payload.get("target_action")
    target_step_id = payload.get("target_step_id")
    target_skill_name = payload.get("target_skill_name")
    if expected_progress is not None and not isinstance(expected_progress, str):
        raise ValueError("Observer expected_progress must be a string or null.")
    if target_action is not None and (
        not isinstance(target_action, str) or not target_action.strip()
    ):
        raise ValueError("Observer target_action must be a non-empty string or null.")
    if target_step_id is not None and not isinstance(target_step_id, str):
        raise ValueError("Observer target_step_id must be a string or null.")
    if target_skill_name is not None and not isinstance(target_skill_name, str):
        raise ValueError("Observer target_skill_name must be a string or null.")
    return ObserverDecision(
        verdict=cast(ObserverVerdict, verdict),
        reason=reason.strip(),
        guidance=tuple(item.strip() for item in guidance),
        expected_progress=expected_progress,
        target_action=target_action.strip() if target_action is not None else None,
        target_step_id=target_step_id,
        target_skill_name=target_skill_name,
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
        self.state_path = log_root / "workflow-observer-state.json"
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
        self._last_observed_failure_signature: str | None = None
        self._pending_action: ObserverActionSummary | None = None
        try:
            self._restore_state()
        except (AttributeError, KeyError, TypeError, ValueError):
            # A corrupt observer state must not stop the workflow it observes.
            return

    def _restore_state(self) -> None:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(data, Mapping):
            return
        state = data.get("state")
        if isinstance(state, Mapping):
            decision = state.get("last_decision")
            self.state = ObserverState(
                last_fingerprint=(
                    state.get("last_fingerprint")
                    if isinstance(state.get("last_fingerprint"), str)
                    else None
                ),
                last_observed_action_index=(
                    state.get("last_observed_action_index")
                    if isinstance(state.get("last_observed_action_index"), int)
                    else None
                ),
                last_decision=(
                    parse_observer_decision(decision)
                    if isinstance(decision, Mapping)
                    else None
                ),
                intervention_pending=bool(state.get("intervention_pending")),
                observation_epoch=int(state.get("observation_epoch", 0)),
            )
        self._action_index = int(data.get("action_index", 0))
        self._last_action_signature = data.get("last_action_signature")
        self._last_failure_signature = data.get("last_failure_signature")
        self._last_step_id = data.get("last_step_id")
        self._last_material_progress_action = data.get("last_material_progress_action")
        self._last_validation_issue_count = data.get("last_validation_issue_count")
        self._last_observed_failure_signature = data.get(
            "last_observed_failure_signature"
        )
        for signature, count in (data.get("action_counts", {}) or {}).items():
            self._action_counts[str(signature)] = int(count)
        for signature, count in (data.get("failure_counts", {}) or {}).items():
            self._failure_counts[str(signature)] = int(count)
        self._actions.extend(
            ObserverActionSummary(
                int(item["index"]),
                str(item["action"]),
                item.get("made_progress"),
                str(item["outcome"]),
            )
            for item in data.get("recent_actions", ())
            if isinstance(item, Mapping)
        )
        self._failures.extend(
            ObserverFailureSummary(
                int(item["index"]),
                item.get("action"),
                str(item["error_type"]),
                str(item["error"]),
            )
            for item in data.get("recent_failures", ())
            if isinstance(item, Mapping)
        )

    def _persist_state(self) -> None:
        payload = {
            "schema_version": 1,
            "state": {
                "last_fingerprint": self.state.last_fingerprint,
                "last_observed_action_index": self.state.last_observed_action_index,
                "last_decision": (
                    asdict(self.state.last_decision)
                    if self.state.last_decision is not None
                    else None
                ),
                "intervention_pending": self.state.intervention_pending,
                "observation_epoch": self.state.observation_epoch,
            },
            "action_index": self._action_index,
            "last_action_signature": self._last_action_signature,
            "last_failure_signature": self._last_failure_signature,
            "last_step_id": self._last_step_id,
            "last_material_progress_action": self._last_material_progress_action,
            "last_validation_issue_count": self._last_validation_issue_count,
            "last_observed_failure_signature": self._last_observed_failure_signature,
            "action_counts": dict(self._action_counts),
            "failure_counts": dict(self._failure_counts),
            "recent_actions": [asdict(item) for item in self._actions],
            "recent_failures": [asdict(item) for item in self._failures],
        }
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=self.state_path.parent, delete=False
            ) as stream:
                json.dump(payload, stream, ensure_ascii=False)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
                temporary_path = stream.name
            os.replace(temporary_path, self.state_path)
        except OSError:
            return

    def response_failed(self, error: Exception) -> ObserverDecision | None:
        count = self._record_failure(None, error)
        if count >= 2:
            return self._trigger(
                ObserverTrigger(
                    kind="repeated_failure",
                    reason="The same LLM response failure occurred at least twice.",
                    priority="high",
                )
            )
        return None

    def action_failed(self, action: Any, error: Exception) -> ObserverDecision | None:
        signature = _action_signature(action)
        count = self._record_failure(signature, error)
        if _action_kind(action) in {"prompt_user", "get-human-input"}:
            return None
        if any(
            marker in str(error).casefold()
            for marker in ("prompt_user", "human_input", "question")
        ):
            return None
        if count >= 2:
            return self._trigger(
                ObserverTrigger(
                    kind="repeated_failure",
                    reason="The same action failure occurred at least twice.",
                    priority="high",
                )
            )
        return None

    def action_proposed(self, action: Any) -> ObserverDecision | None:
        """Review a human request before it can pause a struggling workflow."""
        if _action_kind(action) not in {"prompt_user", "get-human-input"}:
            return None
        if self.state.last_decision is None:
            # Human interaction is normal in a healthy chat. Phase 2 only
            # intercepts it after the observer has diagnosed a stuck execution.
            return None
        if self._failures and any(
            marker in self._failures[-1].error.casefold()
            for marker in ("prompt_user", "human_input", "question")
        ):
            # The ordinary action-repair loop owns malformed question shapes;
            # observer review is for a human request made because discovery
            # appears blocked, not for correcting its JSON contract.
            return None
        context = self._safe_context()
        if context is None:
            return None
        self._pending_action = ObserverActionSummary(
            index=self._action_index + 1,
            action=_compact_text(_action_signature(action)),
            made_progress=None,
            outcome="proposed",
        )
        try:
            return self._trigger(
                ObserverTrigger(
                    kind="human_prompt",
                    reason="The working agent requested human input.",
                ),
                context=context,
            )
        finally:
            self._pending_action = None

    def action_completed(
        self,
        action: Any,
        observation: WorkflowActionObservation,
    ) -> ObserverDecision | None:
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
            self.state.intervention_pending = False
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
            return None
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

        decision: ObserverDecision | None = None
        if diagnostic_trigger is not None:
            decision = self._trigger(diagnostic_trigger, context=context)

        action_kind = _action_kind(action)
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
        self._persist_state()
        return decision

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
        self._persist_state()
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
                    not in {
                        WORKFLOW_LLM_ERROR_LOG,
                        WORKFLOW_OBSERVER_LOG,
                        self.state_path.name,
                    }
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
            # The observer needs every step and contract to make a safe transfer;
            # only heterogeneous runtime/error metadata is compacted.
            skill_definition=context.skill_definition,
            error_state=compact_observer_mapping(context.error_state),
            recent_actions=tuple(
                (*self._actions, self._pending_action)
                if self._pending_action is not None
                else self._actions
            ),
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
            prior_decision=self.state.last_decision,
        )

    def _trigger(
        self,
        trigger: ObserverTrigger,
        *,
        context: ObserverExecutionContext | None = None,
    ) -> ObserverDecision | None:
        context = context or self._safe_context()
        if context is None:
            return None
        packet = self._packet(trigger, context)
        fingerprint = observer_packet_fingerprint(packet, self.worktree_root)
        if fingerprint == self.state.last_fingerprint:
            return None
        should_call = trigger.kind in _SHADOW_LLM_TRIGGERS
        new_failure_class = (
            trigger.kind == "repeated_failure"
            and self._last_failure_signature != self._last_observed_failure_signature
        )
        within_cooldown = (
            self.state.last_observed_action_index is not None
            and self._action_index - self.state.last_observed_action_index
            < _OBSERVER_COOLDOWN_ACTIONS
        )
        human_review_ready = (
            trigger.kind == "human_prompt" and self.state.last_decision is not None
        )
        if (
            should_call
            and within_cooldown
            and not new_failure_class
            and not human_review_ready
        ):
            return self._record_trigger(
                context,
                trigger,
                fingerprint,
                packet,
                decision=None,
                error=None,
                llm_invoked=False,
            )
        decision: ObserverDecision | None = None
        error: Exception | None = None
        if should_call:
            try:
                decision = parse_observer_decision(
                    self.client.complete_json(build_observer_messages(packet))
                )
                self.state.last_decision = decision
                self.state.intervention_pending = decision.verdict not in {
                    "continue",
                    "request_human",
                }
                self.state.last_observed_action_index = self._action_index
                self.state.observation_epoch += 1
                self._last_observed_failure_signature = self._last_failure_signature
            except Exception as observer_error:  # shadow failures are non-fatal
                error = observer_error
        self.state.last_fingerprint = fingerprint
        self._persist_state()
        return self._record_trigger(
            context,
            trigger,
            fingerprint,
            packet,
            decision=decision,
            error=error,
            llm_invoked=should_call,
        )

    def _record_trigger(
        self,
        context: ObserverExecutionContext,
        trigger: ObserverTrigger,
        fingerprint: str,
        packet: ObserverPacket,
        *,
        decision: ObserverDecision | None,
        error: Exception | None,
        llm_invoked: bool,
    ) -> ObserverDecision | None:
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
                "llm_invoked": llm_invoked,
            },
            packet=asdict(packet),
            decision=asdict(decision) if decision is not None else None,
            error=error,
        )
        return decision

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
