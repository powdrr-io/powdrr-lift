"""Compile bounded source decisions into source-anchored semantic contracts."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from powdrr_lift.core.semantic_contract import (
    BoundSourceExtraction,
    PartialSemanticContract,
    SemanticContractError,
    SourceExtractionSpec,
    compile_partial_semantic_contract,
)
from powdrr_lift.core.semantic_decision import (
    DECISION_VALUES,
    SemanticDecision,
    SemanticDecisionProvider,
    SemanticDecisionSpec,
)
from powdrr_lift.core.semantic_faithfulness import (
    FieldEntailmentReview,
)
from powdrr_lift.core.semantic_faithfulness import (
    bind_field_entailment_reviews as bind_field_reviews,
)
from powdrr_lift.core.semantic_faithfulness import (
    finalize_source_faithfulness as finalize_faithfulness,
)
from powdrr_lift.core.semantic_faithfulness import (
    prepare_field_entailment_reviews as prepare_field_reviews,
)
from powdrr_lift.workrr.semantic_classifier import (
    resolve_deterministic_source_decision,
)

SOURCE_CLASSIFIER_REVISION = "source-classifier-v2-decision-tree"
SOURCE_EXTRACTOR_REVISION = "source-extractor-v1"


@dataclass(frozen=True, slots=True)
class ClassificationExample:
    proposition: str
    value: str | None
    context: str | None = None
    unresolved_reason: str | None = None


@dataclass(frozen=True, slots=True)
class ClassifierDefinition:
    question: str
    instructions: tuple[str, ...]
    examples: tuple[ClassificationExample, ...] = ()


CLASSIFIER_DEFINITIONS: dict[str, ClassifierDefinition] = {
    "disposition": ClassifierDefinition(
        "Which one disposition describes this exact proposition?",
        (
            "Classify product meaning as entity, feature, interface, invariant, "
            "guidance, or non_goal; choose context for a problem statement or "
            "background fact that introduces no requested behavior.",
            "Use nonactionable only for delivery, tooling, or process instructions "
            "with no product semantics.",
            "A product prohibition is non_goal, not nonactionable; a universal "
            "product rule is invariant.",
        ),
        (
            ClassificationExample("A Report is a domain record.", "entity"),
            ClassificationExample("The ExportJob represents one export.", "entity"),
            ClassificationExample("Users can export reports.", "feature"),
            ClassificationExample("The service creates a report.", "feature"),
            ClassificationExample("Expose get_state_data(state).", "interface"),
            ClassificationExample("Callbacks receive state_data.", "interface"),
            ClassificationExample("Every response has an ID.", "invariant"),
            ClassificationExample("State data is isolated per machine.", "invariant"),
            ClassificationExample("Prefer immutable defaults.", "guidance"),
            ClassificationExample("Document lifecycle behavior clearly.", "guidance"),
            ClassificationExample("Do not add CSV export.", "non_goal"),
            ClassificationExample("Keep retries out of this feature.", "non_goal"),
            ClassificationExample(
                "Open a pull request when finished.", "nonactionable"
            ),
            ClassificationExample(
                "Run the unit tests before submitting.", "nonactionable"
            ),
            ClassificationExample("A State owns its declared data.", "feature"),
            ClassificationExample("The library provides DataVar.", "entity"),
            ClassificationExample(
                "The setter raises InvalidDefinition on bad keys.", "feature"
            ),
            ClassificationExample(
                "Do not change the public callback signature.", "non_goal"
            ),
            ClassificationExample(
                "All active states reset data on re-entry.", "invariant"
            ),
            ClassificationExample(
                "Please keep the patch on the current branch.", "nonactionable"
            ),
            ClassificationExample(
                "States lack built-in data ownership, forcing manual variable "
                "management without scoping or lifecycle.",
                "context",
            ),
            ClassificationExample(
                "Without a lifecycle, callers manage values manually.", "context"
            ),
        ),
    ),
    "polarity": ClassifierDefinition(
        "Does this exact proposition require, prohibit, permit, or only describe "
        "behavior?",
        (
            "Choose required, prohibited, permitted, or descriptive from source "
            "wording only.",
        ),
        (
            ClassificationExample("The API must return a report.", "required"),
            ClassificationExample("All state data should survive pickle.", "required"),
            ClassificationExample(
                "The setter is required to reject unknown keys.", "required"
            ),
            ClassificationExample("The callback shall receive state_data.", "required"),
            ClassificationExample("Re-entry resets the declared defaults.", "required"),
            ClassificationExample("Do not add automatic retries.", "prohibited"),
            ClassificationExample(
                "The endpoint must not expose secrets.", "prohibited"
            ),
            ClassificationExample("Clients shall not mutate snapshots.", "prohibited"),
            ClassificationExample(
                "Never share state data across instances.", "prohibited"
            ),
            ClassificationExample("Archived reports cannot be exported.", "prohibited"),
            ClassificationExample("Clients may omit an optional field.", "permitted"),
            ClassificationExample(
                "A caller can choose either output format.", "permitted"
            ),
            ClassificationExample("The API allows empty labels.", "permitted"),
            ClassificationExample(
                "Users are allowed to cancel an export.", "permitted"
            ),
            ClassificationExample("A callback may return None.", "permitted"),
            ClassificationExample("The exporter is synchronous.", "descriptive"),
            ClassificationExample("The package currently uses SQLite.", "descriptive"),
            ClassificationExample("The state has a name attribute.", "descriptive"),
            ClassificationExample(
                "The existing callback accepts event_data.", "descriptive"
            ),
            ClassificationExample(
                "The default timeout is five seconds.", "descriptive"
            ),
        ),
    ),
    "quantifier": ClassifierDefinition(
        "What coverage quantifier does this exact proposition state for its subject?",
        (
            "Choose one, some, every, or unspecified; do not infer universal "
            "coverage from normative tone.",
        ),
        (
            ClassificationExample("One active report is selected.", "one"),
            ClassificationExample("Exactly one callback receives the value.", "one"),
            ClassificationExample("A single state owns this data.", "one"),
            ClassificationExample("Only one matching record is returned.", "one"),
            ClassificationExample("The selected report is exported.", "one"),
            ClassificationExample("Some reports can be archived.", "some"),
            ClassificationExample("A subset of states declares data.", "some"),
            ClassificationExample("Certain callbacks receive snapshots.", "some"),
            ClassificationExample("At least one user can cancel.", "some"),
            ClassificationExample("Several matching records are returned.", "some"),
            ClassificationExample("Every active report is exportable.", "every"),
            ClassificationExample(
                "All state instances receive fresh defaults.", "every"
            ),
            ClassificationExample("Each callback sees the active state data.", "every"),
            ClassificationExample("For each key, the setter checks its type.", "every"),
            ClassificationExample("All saved descendants are restored.", "every"),
            ClassificationExample("Reports support export.", "unspecified"),
            ClassificationExample("The API returns a snapshot.", "unspecified"),
            ClassificationExample("Callbacks receive event_data.", "unspecified"),
            ClassificationExample("Data is initialized on entry.", "unspecified"),
            ClassificationExample("The endpoint accepts JSON.", "unspecified"),
        ),
    ),
    "requirement_strength": ClassifierDefinition(
        "What normative strength does this exact proposition state?",
        (
            "Choose must, should, may, descriptive, or unspecified from the source "
            "modal.",
            "Preserve should separately from must even when both express required "
            "product behavior.",
        ),
        (
            ClassificationExample("The method must return a snapshot.", "must"),
            ClassificationExample("Keys must be strings.", "must"),
            ClassificationExample("The callback must receive state_data.", "must"),
            ClassificationExample("The API must reject undeclared keys.", "must"),
            ClassificationExample(
                "The state should reset defaults on re-entry.", "should"
            ),
            ClassificationExample("State data should survive pickle.", "should"),
            ClassificationExample("The diagram should show data variables.", "should"),
            ClassificationExample(
                "A history snapshot should preserve child data.", "should"
            ),
            ClassificationExample("Clients may omit the optional label.", "may"),
            ClassificationExample("The caller may cancel an export.", "may"),
            ClassificationExample("A callback may return a value.", "may"),
            ClassificationExample("The setter may accept None.", "may"),
            ClassificationExample("The exporter is synchronous.", "descriptive"),
            ClassificationExample("The package currently uses SQLite.", "descriptive"),
            ClassificationExample("The state has a name attribute.", "descriptive"),
            ClassificationExample("Callbacks receive event_data today.", "descriptive"),
            ClassificationExample(
                "State data is available to callbacks.", "unspecified"
            ),
            ClassificationExample("The endpoint returns a snapshot.", "unspecified"),
            ClassificationExample("Reports support export.", "unspecified"),
            ClassificationExample("The data mapping contains defaults.", "unspecified"),
        ),
    ),
    "has_precondition": ClassifierDefinition(
        "Does this exact proposition explicitly state a condition that must hold "
        "before or while the behavior applies?",
        ("Choose present or absent; do not extract or invent the condition.",),
        tuple(
            [
                ClassificationExample(text, "present")
                for text in (
                    "Active reports can be exported.",
                    "When a state is entered, defaults are copied.",
                    "If the key is declared, the setter accepts it.",
                    "While the machine is active, data can be read.",
                    "For authenticated users, the endpoint returns data.",
                    "After validation succeeds, the export starts.",
                    "Unless cancelled, the stream continues.",
                    "Given a saved snapshot, history restores values.",
                    "On a valid transition, callbacks receive state_data.",
                    "Only for compound states, the metaclass accepts data.",
                )
            ]
            + [
                ClassificationExample(text, "absent")
                for text in (
                    "Users can export reports.",
                    "The endpoint returns CSV.",
                    "Every state has a name.",
                    "The setter validates the key.",
                    "Callbacks receive state_data.",
                    "The diagram displays declared variables.",
                    "Data survives pickle.",
                    "The API creates a snapshot.",
                    "The event is emitted after transition.",
                    "Reports support export.",
                )
            ]
        ),
    ),
    "has_exception": ClassifierDefinition(
        "Does this exact proposition explicitly state an exception to its behavior "
        "or coverage?",
        (
            "Choose present or absent; do not treat an ordinary condition as an "
            "exception.",
        ),
        tuple(
            [
                ClassificationExample(text, "present")
                for text in (
                    "All reports except archived reports can be exported.",
                    "Export reports unless they are locked.",
                    "Every state, other than the final state, receives defaults.",
                    "The setter accepts any key except reserved names.",
                    "Callbacks run for all transitions excluding resets.",
                    "Data is copied unless the value is immutable.",
                    "All users may cancel, save administrators.",
                    "The API returns CSV but not for archived records.",
                    "Every child restores data, except direct children.",
                    "The export runs on entry, except during replay.",
                )
            ]
            + [
                ClassificationExample(text, "absent")
                for text in (
                    "Active reports can be exported.",
                    "Reports created after login can be exported.",
                    "When a state is active, its data is readable.",
                    "The endpoint accepts JSON.",
                    "Callbacks receive state_data on entry.",
                    "The setter validates declared keys.",
                    "All active states reset data.",
                    "Users can export reports.",
                    "Data persists while the state is active.",
                    "The event runs after validation.",
                )
            ]
        ),
    ),
    "has_explicit_result": ClassifierDefinition(
        "Does this exact proposition explicitly state the observable result of the "
        "behavior?",
        ("Choose present only when the result itself appears in the proposition.",),
        tuple(
            [
                ClassificationExample(text, "present")
                for text in (
                    "The endpoint returns CSV.",
                    "The setter raises InvalidDefinition for unknown keys.",
                    "A successful export creates a downloadable file.",
                    "The callback emits a StateChanged event.",
                    "On failure, the API returns status 422.",
                    "The method returns None when the state is inactive.",
                    "The operation yields a snapshot mapping.",
                    "The parser produces a literal Python value.",
                    "The transition records old and new data values.",
                    "The diagram displays each declared data key.",
                )
            ]
            + [
                ClassificationExample(text, "absent")
                for text in (
                    "All data should pickle.",
                    "Users can export reports.",
                    "The setter validates declared keys.",
                    "The callback receives state_data.",
                    "Data is reset on re-entry.",
                    "The endpoint supports JSON.",
                    "History restores child state data.",
                    "The package provides DataVar.",
                    "The renderer annotates states.",
                    "Retries are disabled by default.",
                )
            ]
        ),
    ),
    "temporal_scope": ClassifierDefinition(
        "What temporal applicability does this exact proposition explicitly state?",
        (
            "Choose current, future, current_and_future, event_bound, or unspecified.",
            "Do not infer future scope from every or all.",
        ),
        (
            ClassificationExample(
                "In the current release, exports return CSV.", "current"
            ),
            ClassificationExample("This version accepts string keys.", "current"),
            ClassificationExample(
                "Currently, callbacks receive event_data.", "current"
            ),
            ClassificationExample("The present API exposes get_state_data.", "current"),
            ClassificationExample("In a future release, exports return CSV.", "future"),
            ClassificationExample(
                "Starting next version, keys accept integers.", "future"
            ),
            ClassificationExample("The next release will expose DataVar.", "future"),
            ClassificationExample(
                "From version 3 onward, snapshots are immutable.", "future"
            ),
            ClassificationExample(
                "Now and in future releases, keys are strings.", "current_and_future"
            ),
            ClassificationExample(
                "The API continues to return CSV in later versions.",
                "current_and_future",
            ),
            ClassificationExample(
                "This behavior applies today and going forward.", "current_and_future"
            ),
            ClassificationExample(
                "Both current and future releases preserve pickle.",
                "current_and_future",
            ),
            ClassificationExample(
                "On state entry, defaults are copied.", "event_bound"
            ),
            ClassificationExample(
                "Whenever a transition exits, data is removed.", "event_bound"
            ),
            ClassificationExample(
                "At each macrostep boundary, changes are cleared.", "event_bound"
            ),
            ClassificationExample(
                "When history is recalled, saved data is restored.", "event_bound"
            ),
            ClassificationExample("The endpoint returns CSV.", "unspecified"),
            ClassificationExample("Every active state has data.", "unspecified"),
            ClassificationExample("The setter rejects unknown keys.", "unspecified"),
            ClassificationExample("Callbacks receive a snapshot.", "unspecified"),
        ),
    ),
    "source_predicate": ClassifierDefinition(
        "Does this exact proposition state the predicate that determines successful "
        "behavior?",
        (
            "Choose explicit, implied_by_registered_term, or not_stated.",
            "Use implied_by_registered_term only when supplied accepted context "
            "defines the term.",
        ),
        (
            ClassificationExample("The endpoint returns CSV.", "explicit"),
            ClassificationExample(
                "The setter raises InvalidDefinition on bad keys.", "explicit"
            ),
            ClassificationExample("The callback emits StateChanged.", "explicit"),
            ClassificationExample("The method returns None when inactive.", "explicit"),
            ClassificationExample(
                "A valid export creates a downloadable file.", "explicit"
            ),
            ClassificationExample("The parser produces a Python integer.", "explicit"),
            ClassificationExample(
                "The operation yields a mapping snapshot.", "explicit"
            ),
            ClassificationExample(
                "The report is pickleable.",
                "implied_by_registered_term",
                "Accepted context defines pickleable as a successful "
                "pickle round trip.",
            ),
            ClassificationExample(
                "The record is JSON-serializable.",
                "implied_by_registered_term",
                "Accepted context defines JSON-serializable as encoding and "
                "decoding to an equivalent value.",
            ),
            ClassificationExample(
                "The value is iterable.",
                "implied_by_registered_term",
                "Accepted context defines iterable as supporting iteration.",
            ),
            ClassificationExample(
                "The path is readable.",
                "implied_by_registered_term",
                "Accepted context defines readable as a successful read operation.",
            ),
            ClassificationExample(
                "The field is optional.",
                "implied_by_registered_term",
                "Accepted context defines optional as omission being valid.",
            ),
            ClassificationExample(
                "The result is idempotent.",
                "implied_by_registered_term",
                "Accepted context defines idempotent as repeated calls "
                "preserving the same observable state.",
            ),
            ClassificationExample(
                "The operation is reversible.",
                "implied_by_registered_term",
                "Accepted context defines reversible as an inverse operation "
                "restoring the original state.",
            ),
            ClassificationExample("All data should pickle.", "not_stated"),
            ClassificationExample("Users can export reports.", "not_stated"),
            ClassificationExample("The setter validates declared keys.", "not_stated"),
            ClassificationExample("Callbacks receive state_data.", "not_stated"),
            ClassificationExample("The state resets on re-entry.", "not_stated"),
            ClassificationExample("The diagram displays data variables.", "not_stated"),
        ),
    ),
    "nonactionable_exclusion_safety": ClassifierDefinition(
        "Does this exact proposition contain only process metadata, product "
        "semantics, or a mixture of both?",
        (
            "Choose process_only only when excluding the proposition cannot remove "
            "product behavior or a product non-goal.",
            "Choose product_semantics_present for any product behavior, constraint, "
            "interface, invariant, or prohibition.",
            "Choose mixed when process instructions and product semantics appear in "
            "the same proposition.",
        ),
        (
            ClassificationExample("Open a pull request.", "process_only"),
            ClassificationExample("Run pytest before submitting.", "process_only"),
            ClassificationExample(
                "Commit the changes on a feature branch.", "process_only"
            ),
            ClassificationExample("Update the changelog for this PR.", "process_only"),
            ClassificationExample("Use a dedicated git worktree.", "process_only"),
            ClassificationExample(
                "Send the review link when finished.", "process_only"
            ),
            ClassificationExample(
                "Do not add retries to exports.", "product_semantics_present"
            ),
            ClassificationExample(
                "Every active state owns isolated data.", "product_semantics_present"
            ),
            ClassificationExample(
                "The setter raises InvalidDefinition on bad keys.",
                "product_semantics_present",
            ),
            ClassificationExample(
                "Callbacks receive state_data.", "product_semantics_present"
            ),
            ClassificationExample(
                "Data resets on re-entry.", "product_semantics_present"
            ),
            ClassificationExample(
                "The endpoint returns CSV.", "product_semantics_present"
            ),
            ClassificationExample(
                "Run pytest and preserve the CSV output format.", "mixed"
            ),
            ClassificationExample("Open a PR and do not add retries.", "mixed"),
            ClassificationExample(
                "Commit the change and keep state data instance-local.", "mixed"
            ),
            ClassificationExample(
                "Use a feature branch; the callback must receive state_data.", "mixed"
            ),
            ClassificationExample(
                "Update the changelog and keep the endpoint backward-compatible.",
                "mixed",
            ),
            ClassificationExample(
                "Send the review link and preserve the existing error type.", "mixed"
            ),
            ClassificationExample(
                "Run the tests; archived reports must remain unavailable.", "mixed"
            ),
            ClassificationExample(
                "Create a PR, but do not expose secrets in its output.", "mixed"
            ),
        ),
    ),
    "behavior_family": ClassifierDefinition(
        "Which registered behavior family best describes this exact behavior phrase?",
        (
            "Choose only from the supplied behavior-family labels.",
            "Classify the quoted behavior, not a broader implementation you imagine.",
            "Use other when the requested behavior is clear but no registered family "
            "fits; use unresolved only when the source phrase itself is ambiguous.",
        ),
        (
            ClassificationExample("Create a new report.", "create"),
            ClassificationExample("Read the current report contents.", "read"),
            ClassificationExample("Change the report title.", "update"),
            ClassificationExample("Remove an expired report.", "delete"),
            ClassificationExample("List all active reports.", "list"),
            ClassificationExample("Find reports matching a query.", "search"),
            ClassificationExample("Reject keys that are not declared.", "validate"),
            ClassificationExample(
                "Convert the value into a normalized form.", "transform"
            ),
            ClassificationExample(
                "Serialize the state snapshot with pickle.", "serialize"
            ),
            ClassificationExample(
                "Deserialize the saved pickle snapshot.", "deserialize"
            ),
            ClassificationExample(
                "Pickle and restore the value without changing it.", "round_trip"
            ),
            ClassificationExample(
                "Persist state data across process restarts.", "persist"
            ),
            ClassificationExample("Retrieve the previously saved report.", "retrieve"),
            ClassificationExample(
                "Compare the current and saved revisions.", "compare"
            ),
            ClassificationExample("Invoke the callback for the transition.", "invoke"),
            ClassificationExample("Emit a StateChanged event.", "emit"),
            ClassificationExample("Receive events from the message queue.", "receive"),
            ClassificationExample(
                "Authorize access using the user's role.", "authorize"
            ),
            ClassificationExample(
                "Authenticate the caller's credentials.", "authenticate"
            ),
            ClassificationExample(
                "Retry a request after a transient failure.", "retry"
            ),
            ClassificationExample("Render state data in the diagram.", "render"),
            ClassificationExample(
                "Configure the machine's default timeout.", "configure"
            ),
            ClassificationExample(
                "DataVar and DataChangeInfo are importable from the package.",
                "other",
            ),
            ClassificationExample("Export StateData from the public module.", "other"),
            ClassificationExample(
                "Keep values scoped to the active invocation only.", "other"
            ),
            ClassificationExample(
                "Make state management better.",
                None,
                unresolved_reason="source_ambiguous",
            ),
            ClassificationExample(
                "Improve it.", None, unresolved_reason="source_underspecified"
            ),
        ),
    ),
}

SOURCE_DECISION_KINDS = (
    "disposition",
    "polarity",
    "quantifier",
    "requirement_strength",
    "has_precondition",
    "has_exception",
    "has_explicit_result",
    "temporal_scope",
    "source_predicate",
    "nonactionable_exclusion_safety",
)

EXTRACTION_DEFINITIONS: dict[str, ClassifierDefinition] = {
    "subject": ClassifierDefinition(
        "Copy the smallest exact phrase naming what this proposition applies to.",
        (
            "Return an exact case-sensitive substring, not a paraphrase.",
            "Do not return only a determiner or quantifier such as all, every, a, "
            "or the.",
            "Examples: 'All data should pickle' returns 'data'; 'Users can export "
            "reports' returns 'Users'.",
        ),
    ),
    "behavior": ClassifierDefinition(
        "Copy the smallest exact phrase naming the behavior, state, or prohibition.",
        (
            "Return an exact case-sensitive substring, not a paraphrase.",
            "Keep meaning-bearing result modifiers in the behavior phrase.",
            "Examples: return 'pickle', 'export reports as CSV', or 'add retries "
            "to report exports'.",
        ),
    ),
    "precondition": ClassifierDefinition(
        "Copy the smallest exact phrase stating the behavior's precondition.",
        (
            "Return an exact case-sensitive substring and include the complete "
            "condition.",
            "Example: 'Every active report can be exported' returns 'active'.",
        ),
    ),
    "exception": ClassifierDefinition(
        "Copy the smallest exact phrase stating the exception.",
        (
            "Return an exact case-sensitive substring and include the exception "
            "boundary.",
            "Example: 'All reports except archived reports' returns 'except archived "
            "reports'.",
        ),
    ),
    "explicit_result": ClassifierDefinition(
        "Copy the smallest exact phrase stating the behavior's observable result.",
        (
            "Return an exact case-sensitive substring; do not invent an unstated "
            "success predicate.",
            "Example: 'The endpoint returns CSV' returns 'CSV'.",
        ),
    ),
}


def prepare_source_semantic_decisions(
    clause: Mapping[str, Any], *, created_at: str | None = None
) -> dict[str, Any]:
    """Prepare the root disposition choice before dependent classifications."""
    clause_id, text, source_fingerprint = _clause_fields(clause)
    spec = _decision_spec(clause_id, text, source_fingerprint, "disposition")
    return {
        "resolved_decisions": [],
        "pending_specs": [
            _classifier_request(spec, CLASSIFIER_DEFINITIONS["disposition"])
        ],
    }


def prepare_dependent_source_semantic_decisions(
    clause: Mapping[str, Any],
    root_decisions: Sequence[SemanticDecision | Mapping[str, Any]],
    *,
    created_at: str | None = None,
) -> dict[str, Any]:
    """Create only decision-tree children applicable to the root disposition."""
    clause_id, text, source_fingerprint = _clause_fields(clause)
    roots = [
        item if isinstance(item, SemanticDecision) else SemanticDecision.from_data(item)
        for item in root_decisions
    ]
    if len(roots) != 1 or roots[0].decision_kind != "disposition":
        raise SemanticContractError(
            "decision tree requires exactly one disposition root"
        )
    root = roots[0]
    if root.result.status != "resolved" or root.result.value is None:
        raise SemanticContractError("decision tree root must be resolved")
    timestamp = created_at or _created_at()
    resolved = [root.to_data()]
    pending: list[dict[str, Any]] = []
    if root.result.value in {"context", "nonactionable"}:
        defaults = {
            "polarity": "descriptive",
            "quantifier": "unspecified",
            "requirement_strength": "descriptive",
            "has_precondition": "absent",
            "has_exception": "absent",
            "has_explicit_result": "absent",
            "temporal_scope": "unspecified",
            "source_predicate": "not_stated",
        }
        for kind, value in defaults.items():
            spec = _decision_spec(clause_id, text, source_fingerprint, kind)
            resolved.append(
                spec.bind(
                    provider=SemanticDecisionProvider(kind="deterministic-rule"),
                    provider_result={"status": "resolved", "value": value},
                    evidence_refs=(f"source-proposition:{clause_id}",),
                    created_at=timestamp,
                ).to_data()
            )
        if root.result.value == "context":
            spec = _decision_spec(
                clause_id, text, source_fingerprint, "nonactionable_exclusion_safety"
            )
            resolved.append(
                spec.bind(
                    provider=SemanticDecisionProvider(kind="deterministic-rule"),
                    provider_result={
                        "status": "resolved",
                        "value": "product_semantics_present",
                    },
                    evidence_refs=(f"source-proposition:{clause_id}",),
                    created_at=timestamp,
                ).to_data()
            )
        else:
            spec = _decision_spec(
                clause_id, text, source_fingerprint, "nonactionable_exclusion_safety"
            )
            request = _classifier_request(
                spec, CLASSIFIER_DEFINITIONS["nonactionable_exclusion_safety"]
            )
            request["allowed_values"] = ["process_only"]
            request["instructions"].append(
                "Independently verify that this exact clause contains no product "
                "behavior or product non-goal."
            )
            pending.append(request)
    else:
        for kind in SOURCE_DECISION_KINDS:
            if kind == "disposition":
                continue
            spec = _decision_spec(clause_id, text, source_fingerprint, kind)
            deterministic = resolve_deterministic_source_decision(kind, text)
            deterministic_value: str | None
            if (
                deterministic is None
                and kind == "polarity"
                and root.result.value in {"feature", "interface", "invariant"}
                and not any(
                    marker in text.casefold()
                    for marker in ("currently", "currently does", "already", "existing")
                )
            ):
                deterministic_value = "required"
            else:
                deterministic_value = deterministic.value if deterministic else None
            if deterministic_value is not None:
                decision = spec.bind(
                    provider=SemanticDecisionProvider(kind="deterministic-rule"),
                    provider_result={
                        "status": "resolved",
                        "value": deterministic_value,
                    },
                    evidence_refs=(f"source-proposition:{clause_id}",),
                    created_at=timestamp,
                )
                resolved.append(decision.to_data())
            else:
                request = _classifier_request(spec, CLASSIFIER_DEFINITIONS[kind])
                if root.result.value == "non_goal" and kind == "polarity":
                    request["allowed_values"] = ["prohibited"]
                request["instructions"].append(
                    f"The already-resolved root disposition is {root.result.value!r}; "
                    "do not choose a result that contradicts that branch."
                )
                pending.append(request)
    return {"resolved_decisions": resolved, "pending_specs": pending}


def bind_source_semantic_decisions(
    *,
    resolved_decisions: Sequence[Mapping[str, Any]],
    pending_specs: Sequence[Mapping[str, Any]],
    provider_results: Sequence[Mapping[str, Any]],
    created_at: str | None = None,
) -> list[SemanticDecision]:
    if len(pending_specs) != len(provider_results):
        raise SemanticContractError("semantic classifier result count is invalid")
    decisions = [SemanticDecision.from_data(item) for item in resolved_decisions]
    timestamp = created_at or _created_at()
    for request, result in zip(pending_specs, provider_results, strict=True):
        spec_raw = request.get("spec")
        if not isinstance(spec_raw, Mapping):
            raise SemanticContractError("semantic classifier request has no spec")
        spec = SemanticDecisionSpec.from_data(spec_raw)
        decisions.append(
            spec.bind(
                provider=SemanticDecisionProvider(kind="planning-llm"),
                provider_result=result,
                evidence_refs=(f"source-proposition:{spec.subject_ref}",),
                created_at=timestamp,
            )
        )
    ordered = sorted(
        decisions, key=lambda item: SOURCE_DECISION_KINDS.index(item.decision_kind)
    )
    if len(ordered) > 1:
        _validate_decision_tree(ordered)
    return ordered


def _validate_decision_tree(decisions: Sequence[SemanticDecision]) -> None:
    values = {item.decision_kind: item.result.value for item in decisions}
    disposition = values.get("disposition")
    if disposition == "context":
        if values.get("polarity") != "descriptive":
            raise SemanticContractError("context branch must remain descriptive")
        if values.get("requirement_strength") != "descriptive":
            raise SemanticContractError(
                "context branch cannot carry requirement strength"
            )
        if any(
            values.get(kind) != "absent"
            for kind in ("has_precondition", "has_exception", "has_explicit_result")
        ):
            raise SemanticContractError(
                "context branch cannot contain requirement modifiers"
            )
        return
    safety = values.get("nonactionable_exclusion_safety")
    if disposition == "nonactionable" and safety != "process_only":
        raise SemanticContractError(
            "nonactionable branch requires process-only evidence"
        )
    if disposition != "nonactionable" and safety == "process_only":
        raise SemanticContractError(
            "product branch conflicts with process-only evidence"
        )
    if disposition == "non_goal" and values.get("polarity") != "prohibited":
        raise SemanticContractError("non-goal branch requires prohibited polarity")
    polarity = values.get("polarity")
    strength = values.get("requirement_strength")
    if polarity == "descriptive" and strength in {"must", "should", "may"}:
        raise SemanticContractError(
            "descriptive polarity conflicts with normative strength"
        )
    if strength in {"must", "should"} and polarity not in {"required", "prohibited"}:
        raise SemanticContractError(
            "normative strength conflicts with non-normative polarity"
        )
    if polarity == "prohibited" and strength == "may":
        raise SemanticContractError(
            "prohibited polarity conflicts with permissive strength"
        )
    if polarity == "permitted" and strength in {"must", "should"}:
        raise SemanticContractError(
            "permitted polarity conflicts with mandatory strength"
        )


def prepare_source_extractions(
    clause: Mapping[str, Any], decisions: Sequence[SemanticDecision]
) -> list[dict[str, Any]]:
    clause_id, text, source_fingerprint = _clause_fields(clause)
    values = {item.decision_kind: item.result.value for item in decisions}
    kinds = ["subject", "behavior"]
    for decision_kind, extraction_kind in (
        ("has_precondition", "precondition"),
        ("has_exception", "exception"),
        ("has_explicit_result", "explicit_result"),
    ):
        if values.get(decision_kind) == "present":
            kinds.append(extraction_kind)
    requests = []
    for kind in kinds:
        spec = SourceExtractionSpec(
            extraction_id=f"extraction:{clause_id}:{kind}",
            extraction_kind=kind,
            subject_ref=clause_id,
            proposition_text=text,
            source_fingerprint=source_fingerprint,
            contract_revision=f"{SOURCE_EXTRACTOR_REVISION}:{kind}",
        )
        definition = EXTRACTION_DEFINITIONS[kind]
        requests.append(
            {
                "spec": spec.to_data(),
                "question": definition.question,
                "instructions": list(definition.instructions),
            }
        )
    return requests


def bind_source_extractions(
    *,
    requests: Sequence[Mapping[str, Any]],
    provider_results: Sequence[Mapping[str, Any]],
    created_at: str | None = None,
) -> list[BoundSourceExtraction]:
    if len(requests) != len(provider_results):
        raise SemanticContractError("source extraction result count is invalid")
    timestamp = created_at or _created_at()
    result: list[BoundSourceExtraction] = []
    for request, provider_result in zip(requests, provider_results, strict=True):
        spec_raw = request.get("spec")
        if not isinstance(spec_raw, Mapping):
            raise SemanticContractError("source extraction request has no spec")
        spec = SourceExtractionSpec.from_data(spec_raw)
        result.append(
            spec.bind(
                provider=SemanticDecisionProvider(kind="planning-llm"),
                provider_result=provider_result,
                created_at=timestamp,
            )
        )
    return result


def prepare_behavior_family_decision(
    clause: Mapping[str, Any],
    behavior: BoundSourceExtraction,
    decisions: Sequence[SemanticDecision | Mapping[str, Any]] = (),
) -> dict[str, Any]:
    clause_id, text, source_fingerprint = _clause_fields(clause)
    if behavior.subject_ref != clause_id or behavior.extraction_kind != "behavior":
        raise SemanticContractError("behavior extraction does not match the clause")
    spec = SemanticDecisionSpec(
        decision_id=f"decision:{clause_id}:behavior_family",
        decision_kind="behavior_family",
        subject_ref=clause_id,
        proposition_text=text,
        source_fingerprint=source_fingerprint,
        candidate_set_fingerprint=behavior.input_fingerprint,
        contract_revision=f"{SOURCE_CLASSIFIER_REVISION}:behavior_family",
    )
    request = _classifier_request(spec, CLASSIFIER_DEFINITIONS["behavior_family"])
    request["subject_text"] = behavior.span.text
    root = next(
        (
            item
            if isinstance(item, SemanticDecision)
            else SemanticDecision.from_data(item)
            for item in decisions
            if (
                item.decision_kind
                if isinstance(item, SemanticDecision)
                else item.get("decision_kind")
            )
            == "disposition"
        ),
        None,
    )
    if root is not None and root.result.value in {"context", "nonactionable"}:
        disposition = root.result.value
        request["instructions"] = [
            f"The root decision classified the clause as {disposition}, "
            "not a product obligation.",
            "Use other only as a non-actionable placeholder; do not infer behavior.",
        ]
        request["allowed_values"] = ["other"]
    return request


def bind_behavior_family_decision(
    request: Mapping[str, Any],
    provider_result: Mapping[str, Any],
    *,
    created_at: str | None = None,
) -> SemanticDecision:
    spec_raw = request.get("spec")
    if not isinstance(spec_raw, Mapping):
        raise SemanticContractError("behavior-family request has no spec")
    spec = SemanticDecisionSpec.from_data(spec_raw)
    return spec.bind(
        provider=SemanticDecisionProvider(kind="planning-llm"),
        provider_result=provider_result,
        evidence_refs=(f"source-proposition:{spec.subject_ref}",),
        created_at=created_at or _created_at(),
    )


def compile_source_contract(
    *,
    clause: Mapping[str, Any],
    decisions: Sequence[SemanticDecision],
    extractions: Sequence[BoundSourceExtraction],
    behavior_family: SemanticDecision,
) -> PartialSemanticContract:
    clause_id, text, source_fingerprint = _clause_fields(clause)
    all_decisions = (*decisions, behavior_family)
    _validate_decision_tree(all_decisions)
    return compile_partial_semantic_contract(
        source_ref=clause_id,
        source_fingerprint=source_fingerprint,
        proposition_text=text,
        decisions=all_decisions,
        extractions=extractions,
    )


def project_partial_contract_to_legacy_design(
    contract: PartialSemanticContract,
) -> dict[str, str]:
    """Render a deterministic, disposable view for legacy consumers."""
    result_text = _render_result(contract)
    return {
        "kind": contract.disposition,
        "description": _render_description(contract),
        "acceptance_criterion": _render_acceptance(contract),
        "expected_test": _render_expected_test(contract),
        "population": _render_population(contract),
        "operation": _render_operation(contract),
        "oracle": result_text,
        "evidence_case": _render_evidence_case(contract),
    }


def prepare_field_entailment_reviews(
    contract: PartialSemanticContract,
) -> list[dict[str, Any]]:
    requests = prepare_field_reviews(contract)
    context = {
        "disposition": contract.disposition,
        "polarity": contract.polarity,
        "requirement_strength": contract.requirement_strength,
        "scope": "Evaluate entailment only within the resolved decision-tree branch.",
    }
    for request in requests:
        request["decision_tree_context"] = context
        request.setdefault("instructions", []).append(
            "The supplied parent decisions are already resolved. Assess whether "
            "the source entails this field inside that branch; do not contradict "
            "the branch by reclassifying the source as descriptive/current-state "
            "unless it explicitly says it describes existing behavior."
        )
    return requests


def bind_field_entailment_reviews(
    *,
    requests: Sequence[Mapping[str, Any]],
    provider_results: Sequence[Mapping[str, Any]],
    created_at: str | None = None,
) -> list[FieldEntailmentReview]:
    return bind_field_reviews(
        requests=requests,
        provider_results=provider_results,
        created_at=created_at or _created_at(),
    )


def finalize_source_faithfulness(
    contract: PartialSemanticContract,
    reviews: Sequence[FieldEntailmentReview],
) -> dict[str, Any]:
    return finalize_faithfulness(contract, reviews).to_data()


def _render_description(contract: PartialSemanticContract) -> str:
    return f"{contract.subject.span.text} {contract.behavior.span.text}."


def _render_acceptance(contract: PartialSemanticContract) -> str:
    if contract.explicit_result is not None:
        return f"The operation produces {contract.explicit_result.span.text}."
    return "The requested behavior is observed for the resolved population."


def _render_expected_test(contract: PartialSemanticContract) -> str:
    return f"Test {contract.behavior.span.text} for {contract.subject.span.text}."


def _render_population(contract: PartialSemanticContract) -> str:
    return f"{contract.quantifier} {contract.subject.span.text}"


def _render_operation(contract: PartialSemanticContract) -> str:
    return f"{contract.behavior_family}: {contract.behavior.span.text}"


def _render_result(contract: PartialSemanticContract) -> str:
    if contract.explicit_result is not None:
        return contract.explicit_result.span.text
    return "the requested behavior is observed"


def _render_evidence_case(contract: PartialSemanticContract) -> str:
    return f"Source {contract.source_ref}: {contract.proposition_text}"


def _classifier_request(
    spec: SemanticDecisionSpec, definition: ClassifierDefinition
) -> dict[str, Any]:
    instructions = list(definition.instructions)
    if definition.examples:
        examples = []
        for example in definition.examples:
            if example.value is not None:
                result = {
                    "status": "resolved",
                    "value": example.value,
                    "reason_code": None,
                }
            else:
                result = {
                    "status": "unresolved",
                    "value": None,
                    "reason_code": example.unresolved_reason,
                }
            example_text = f"{example.proposition!r} => {json.dumps(result)}"
            if example.context is not None:
                example_text += f" (accepted context: {example.context})"
            examples.append(example_text)
        instructions.append(
            "Worked examples (source proposition => exact result JSON):\n- "
            + "\n- ".join(examples)
        )
    return {
        "spec": spec.to_data(),
        "question": definition.question,
        "instructions": instructions,
        "allowed_values": sorted(DECISION_VALUES[spec.decision_kind]),
        "subject_text": spec.proposition_text,
    }


def _decision_spec(
    clause_id: str, text: str, source_fingerprint: str, kind: str
) -> SemanticDecisionSpec:
    return SemanticDecisionSpec(
        decision_id=f"decision:{clause_id}:{kind}",
        decision_kind=kind,
        subject_ref=clause_id,
        proposition_text=text,
        source_fingerprint=source_fingerprint,
        contract_revision=f"{SOURCE_CLASSIFIER_REVISION}:{kind}",
    )


def _clause_fields(clause: Mapping[str, Any]) -> tuple[str, str, str]:
    clause_id = clause.get("clause_id")
    text = clause.get("text")
    source_fingerprint = clause.get("fingerprint")
    if not isinstance(clause_id, str) or not clause_id.strip():
        raise SemanticContractError("instruction clause has no ID")
    if not isinstance(text, str) or not text.strip():
        raise SemanticContractError("instruction clause has no proposition text")
    if not isinstance(source_fingerprint, str) or not source_fingerprint.strip():
        raise SemanticContractError("instruction clause has no source fingerprint")
    return clause_id, text, source_fingerprint


def _created_at() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


__all__ = [
    "CLASSIFIER_DEFINITIONS",
    "EXTRACTION_DEFINITIONS",
    "SOURCE_CLASSIFIER_REVISION",
    "SOURCE_DECISION_KINDS",
    "SOURCE_EXTRACTOR_REVISION",
    "bind_behavior_family_decision",
    "bind_field_entailment_reviews",
    "bind_source_extractions",
    "bind_source_semantic_decisions",
    "compile_source_contract",
    "prepare_behavior_family_decision",
    "prepare_dependent_source_semantic_decisions",
    "prepare_field_entailment_reviews",
    "prepare_source_extractions",
    "prepare_source_semantic_decisions",
    "project_partial_contract_to_legacy_design",
    "finalize_source_faithfulness",
]
