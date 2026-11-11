"""Durable interactive authoring of Structrr stories.

The service allocates identities and derives readiness. The caller supplies
actual user responses through a trusted adapter, never model-authored flags.
Draft checkpoints are not accepted repository design.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any, Literal

from powdrr_service.decision_obligation import content_fingerprint
from powdrr_service.planning.instruction_ledger import compile_instruction_ledger
from powdrr_service.structrr.proposal import ProposalRevision, compile_proposal_revision
from powdrr_service.structrr.story_semantic_review import (
    STORY_REVIEW_DIMENSIONS,
    STORY_REVIEW_POLICY,
    StoryReviewRequest,
    StorySemanticReviewer,
)
from powdrr_service.structrr.user_stories import (
    STORY_SECTIONS,
    StoryCatalog,
    StoryConfirmation,
    StoryContractError,
    StoryEditRequest,
    _data,
    _decode,
    allocate_story_edit,
    parse_story_catalog,
    preview_story_edits,
    story_contract_digest,
    validate_story_catalog,
)

STORY_INTERVIEW_SCHEMA = "structrr-story-interview-v1"


@dataclass(frozen=True, slots=True)
class StoryQuestion:
    question_id: str
    kind: Literal["refinement", "assumption", "confirmation"]
    dimension: str
    text: str
    contract_digest: str | None
    context_fingerprint: str
    proposed_assumption: str | None = None

    def to_data(self) -> dict[str, Any]:
        return _data(self)


@dataclass(frozen=True, slots=True)
class StoryUserResponse:
    """Caller-established user input, with the exact displayed question identity."""

    question_id: str
    displayed_text: str
    answer: str


def parse_story_interview_edit(raw: object) -> StoryEditRequest:
    """Decode a bounded edit; a new story has no allocated owner ID yet."""
    if not isinstance(raw, Mapping):
        raise ValueError("story edit requires an object")
    is_new = raw.get("section") == "user_stories" and raw.get("action") == "add"
    if is_new and raw.get("story_id") == "":
        request: StoryEditRequest = _decode(
            StoryEditRequest, {**raw, "story_id": "unallocated"}, "edit"
        )
        return replace(request, story_id="")
    request = _decode(StoryEditRequest, raw, "edit")
    return request


class StoryInterview:
    def __init__(self, data: Mapping[str, Any]) -> None:
        self._state: dict[str, Any] = json.loads(json.dumps(data))

    @classmethod
    def start(
        cls,
        session_id: str,
        work_item_name: str,
        original_request: str,
        baseline: Mapping[str, Any],
        *,
        source_revision: str,
        story_mode: Literal["create", "amend"] = "create",
        existing_story_ref: str | None = None,
        uncertainty_policy: str = "clarify",
        benchmark_mode: bool = False,
    ) -> StoryInterview:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", session_id) or not re.fullmatch(
            r"[a-z0-9][a-z0-9-]{0,99}", work_item_name
        ):
            raise ValueError("story session and work item require safe stable names")
        if story_mode not in {"create", "amend"} or (
            story_mode == "amend" and not existing_story_ref
        ):
            raise ValueError("amending requires an existing story reference")
        catalog = parse_story_catalog(baseline)
        validate_story_catalog(catalog).require_valid()
        if (
            existing_story_ref is not None
            and existing_story_ref not in catalog.stories_by_id
        ):
            raise StoryContractError(
                "story_reference_unknown",
                existing_story_ref,
                "story is not in the accepted catalog",
            )
        ledger = compile_instruction_ledger(work_item_name, original_request)
        sources: dict[str, Any] = {}
        for clause in ledger.clauses:
            ref = (
                f"{ledger.source.source_id}:"
                f"{ledger.source.text_fingerprint[7:19]}:{clause.clause_id}"
            )
            sources[ref] = {
                "id": ref,
                "kind": "instruction",
                "text": clause.text,
                "source_id": clause.source_id,
                "clause_id": clause.clause_id,
                "text_fingerprint": ledger.source.text_fingerprint,
            }
        for section in STORY_SECTIONS:
            for record in catalog.to_data()[section]:
                refs = [*record.get("source_refs", [])]
                if section == "user_stories" and record.get("confirmation"):
                    refs.append(record["confirmation"]["decision_ref"])
                for ref in refs:
                    sources.setdefault(
                        ref,
                        {
                            "id": ref,
                            "kind": "reviewed_reference",
                            "source_revision": source_revision,
                        },
                    )
        return cls(
            {
                "schema_version": STORY_INTERVIEW_SCHEMA,
                "session_id": session_id,
                "work_item_name": work_item_name,
                "source_revision": source_revision,
                "instruction_ledger": ledger.to_data(),
                "baseline": dict(baseline),
                "draft": catalog.to_data(),
                "story_mode": story_mode,
                "start_options": {
                    "story_mode": story_mode,
                    "existing_story_ref": existing_story_ref,
                },
                "story_id": existing_story_ref,
                "uncertainty_policy": uncertainty_policy,
                "benchmark_mode": benchmark_mode,
                "sources": sources,
                "edits": {},
                "questions": {},
                "answers": {},
                "reviews": {},
                "pending_question_id": None,
                "confirmation_evidence": None,
            }
        )

    @classmethod
    def from_data(cls, raw: Mapping[str, Any]) -> StoryInterview:
        data = {k: v for k, v in raw.items() if k != "fingerprint"}
        if data.get("schema_version") != STORY_INTERVIEW_SCHEMA or raw.get(
            "fingerprint"
        ) != content_fingerprint(data):
            raise ValueError("story checkpoint schema or fingerprint mismatch")
        parse_story_catalog(data["draft"])
        from powdrr_service.planning.instruction_ledger import InstructionLedger

        InstructionLedger.from_data(data["instruction_ledger"])
        for name in ("sources", "edits", "questions", "answers", "reviews"):
            if not isinstance(data.get(name), dict):
                raise ValueError(f"story checkpoint requires {name}")
        return cls(data)

    def to_data(self) -> dict[str, Any]:
        data = json.loads(json.dumps(self._state))
        return {**data, "fingerprint": content_fingerprint(data)}

    @property
    def catalog(self) -> StoryCatalog:
        return parse_story_catalog(self._state["draft"])

    @property
    def story_id(self) -> str:
        identity = self._state.get("story_id")
        if not isinstance(identity, str):
            raise StoryContractError(
                "story_missing", "interview", "create or select a story first"
            )
        return identity

    def select_story(self, story_id: str) -> dict[str, Any]:
        if self._state["edits"]:
            raise ValueError("select an existing story before editing")
        if story_id not in self.catalog.stories_by_id:
            raise StoryContractError(
                "story_reference_unknown", story_id, "unknown current story"
            )
        self._state["story_id"] = story_id
        self._state["story_mode"] = "amend"
        return self.status()

    def reference_catalog(self) -> dict[str, Any]:
        evidence = self._state.get("confirmation_evidence")
        return {
            **self._state["baseline"],
            "source_refs": [
                *self._state["sources"],
                *([evidence["id"]] if evidence else []),
            ],
        }

    def edit(self, request: StoryEditRequest, checkpoint_key: str) -> dict[str, Any]:
        request_hash = content_fingerprint(_data(request))
        previous = self._state["edits"].get(checkpoint_key)
        if previous is not None:
            if previous["request_fingerprint"] != request_hash:
                raise StoryContractError(
                    "story_checkpoint_conflict",
                    checkpoint_key,
                    "edit key already used for another request",
                )
            return {"allocated": previous["allocated"], **self.status()}
        if "source_refs" in request.content:
            refs = request.content["source_refs"]
            if (
                not isinstance(refs, list)
                or not refs
                or any(ref not in self._state["sources"] for ref in refs)
            ):
                raise StoryContractError(
                    "story_reference_unknown",
                    checkpoint_key,
                    "select source references from the supplied catalog",
                )
        if request.action == "add" and request.section == "user_stories":
            if self._state["story_id"] is not None:
                raise ValueError(
                    "one story per interview; start another interview for another goal"
                )
        elif request.story_id != self.story_id:
            raise StoryContractError(
                "story_owner_invalid",
                request.story_id,
                "edit is outside the selected story",
            )
        catalog = self.catalog
        allocated = allocate_story_edit(
            catalog,
            request,
            self._state["session_id"] + ":" + checkpoint_key,
            accepted_history_ids=frozenset(
                self._state["baseline"].get("retired_story_ids", [])
            ),
        )
        candidate = preview_story_edits(catalog, [allocated], catalog.fingerprint)
        if request.section == "user_stories" and request.action == "add":
            self._state["story_id"] = allocated.subject_id
        self._state["draft"] = candidate.to_data()
        self._state["edits"][checkpoint_key] = {
            "request_fingerprint": request_hash,
            "allocated": allocated.to_data(),
        }
        # Pending displays remain historical: accepting them after any semantic
        # change is rejected by the exact context fingerprint below.
        return {"allocated": allocated.to_data(), **self.status()}

    def record_prior_answer(self, question: str, answer: str, answer_key: str) -> None:
        """Import a real earlier interview answer from the trusted chat adapter."""
        if not answer.strip():
            return
        ref = (
            "interview:"
            + self._state["session_id"]
            + ":answer:"
            + content_fingerprint(answer_key)[7:23]
        )
        record = {
            "id": ref,
            "kind": "user_answer",
            "question": question,
            "text": answer,
        }
        if ref in self._state["sources"] and self._state["sources"][ref] != record:
            raise ValueError("interview answer identity conflict")
        self._state["sources"][ref] = record

    def _dependency_digests(
        self, current_snapshot: Mapping[str, Any] | None = None
    ) -> dict[str, str]:
        catalog = self.catalog
        story = catalog.stories_by_id[self.story_id]
        baseline = (
            self._state["baseline"] if current_snapshot is None else current_snapshot
        )
        related_catalog = (
            catalog
            if current_snapshot is None
            else parse_story_catalog(current_snapshot)
        )
        dependencies: dict[str, str] = {}
        for link in story.related_story_refs:
            dependencies[link.story_id] = story_contract_digest(
                catalog if link.story_id == story.id else related_catalog, link.story_id
            )
        selections = [("invariants", ref) for ref in story.invariant_refs]
        selections.extend(("modules", ref) for ref in story.related_modules)
        selections.extend(
            (ref.section, ref.subject_id)
            for step in catalog.user_story_steps
            if step.story_id == story.id
            for ref in step.functionality_refs
        )
        for section, identity in selections:
            record = next(
                (
                    record
                    for record in baseline.get(section, [])
                    if record.get("id") == identity
                ),
                None,
            )
            if record is None and section == "modules":
                record = next(
                    (
                        record
                        for record in baseline.get("entities", [])
                        if record.get("id") == identity
                    ),
                    None,
                )
            if record is None:
                raise StoryContractError(
                    "story_reference_unknown", identity, "dependency is absent"
                )
            dependencies[section + ":" + identity] = content_fingerprint(record)
        return dependencies

    def require_current_dependencies(self, snapshot: Mapping[str, Any]) -> None:
        expected = self._dependency_digests()
        try:
            actual = self._dependency_digests(snapshot)
        except (KeyError, StoryContractError) as error:
            self._state["dependency_failure"] = {
                "expected": expected,
                "reason": "A referenced subject is absent from the current snapshot.",
            }
            raise StoryContractError(
                "story_dependency_stale",
                self.story_id,
                "refresh the interview against current accepted context",
            ) from error
        if actual != expected:
            self._state["dependency_failure"] = {"expected": expected, "actual": actual}
            raise StoryContractError(
                "story_dependency_stale",
                self.story_id,
                "a referenced story, invariant or functionality changed; "
                "refresh the interview",
            )

    def _scoped_catalog(self, catalog: StoryCatalog) -> dict[str, Any]:
        identity = self._state.get("story_id")
        story = catalog.stories_by_id.get(identity)
        selected_ids = (
            {identity, *(ref.story_id for ref in story.related_story_refs)}
            if story
            else set()
        )
        return {
            section: [
                record
                for record in records
                if record.get("story_id", record["id"]) in selected_ids
            ]
            for section, records in catalog.to_data().items()
        }

    def _scoped_sources(self) -> list[dict[str, Any]]:
        refs = {
            ref
            for records in self._scoped_catalog(self.catalog).values()
            for record in records
            for ref in record.get("source_refs", [])
        }
        return [
            record
            for ref, record in self._state["sources"].items()
            if record["kind"] in {"instruction", "user_answer"} or ref in refs
        ]

    def retrieve_context(
        self, keywords: tuple[str, ...], module_ids: tuple[str, ...] = ()
    ) -> dict[str, Any]:
        """Retrieve bounded context and explicitly report incomplete results."""
        if any(not word.strip() for word in keywords) or any(
            not identity.strip() for identity in module_ids
        ):
            raise ValueError("context query requires nonempty catalog selections")
        baseline = self._state["baseline"]
        known_modules = {
            record["id"]
            for section in ("modules", "entities")
            for record in baseline.get(section, [])
        }
        if not set(module_ids) <= known_modules:
            raise ValueError("module context selection is absent from the catalog")
        records = []
        for section in (
            "user_stories",
            "entities",
            "features",
            "interfaces",
            "invariants",
            "modules",
        ):
            for record in baseline.get(section, []):
                text = json.dumps(record, ensure_ascii=False).casefold()
                if keywords and not all(word.casefold() in text for word in keywords):
                    continue
                if (
                    module_ids
                    and record.get("id") not in module_ids
                    and not set(record.get("related_modules", [])) & set(module_ids)
                ):
                    continue
                records.append({"section": section, "record": record})
        return {
            "records": records[:64],
            "complete": len(records) <= 64,
            "total_matches": len(records),
            "corrective_action": None
            if len(records) <= 64
            else "Narrow the keyword/module query; omitted records are not disproven.",
        }

    def review_request(self, dimension: str) -> StoryReviewRequest:
        if dimension not in STORY_REVIEW_DIMENSIONS:
            raise ValueError("unsupported semantic review dimension")
        catalog = self.catalog
        validate_story_catalog(catalog, self.reference_catalog()).require_valid()
        identity = self.story_id
        selected: dict[str, Any] = {}
        related = {
            ref.story_id for ref in catalog.stories_by_id[identity].related_story_refs
        }
        for section, records in catalog.to_data().items():
            selected[section] = [
                {key: value for key, value in record.items() if key != "confirmation"}
                for record in records
                if record.get("story_id", record["id"]) in {identity, *related}
            ]
        context = {
            "story": selected,
            "accepted_baseline": self._scoped_catalog(
                parse_story_catalog(self._state["baseline"])
            ),
            "sources": self._scoped_sources(),
            "source_refs": sorted(record["id"] for record in self._scoped_sources()),
            "dependency_digests": self._dependency_digests(),
            "dependency_records": [
                record
                for section in (
                    "invariants",
                    "modules",
                    "entities",
                    "features",
                    "interfaces",
                )
                for record in self._state["baseline"].get(section, [])
                if section + ":" + record.get("id", "") in self._dependency_digests()
            ],
            "dimension": dimension,
            "uncertainty_policy": self._state["uncertainty_policy"],
            "benchmark_mode": self._state["benchmark_mode"],
        }
        return StoryReviewRequest(
            identity,
            story_contract_digest(catalog, identity),
            self._dependency_digests(),
            dimension,
            context,
        )

    def review(self, dimension: str, reviewer: StorySemanticReviewer) -> dict[str, Any]:
        request = self.review_request(dimension)
        previous = self._state["reviews"].get(dimension)
        matching = previous and previous["input_fingerprint"] == request.fingerprint
        if matching and (
            not any(f["classification"] == "unavailable" for f in previous["findings"])
            or previous.get("attempt", 1) >= 3
        ):
            return previous
        finding = reviewer.review(request)
        review = {
            "story_id": self.story_id,
            "contract_digest": request.contract_digest,
            "dependency_digests": dict(request.dependency_digests),
            "reviewer_policy_version": STORY_REVIEW_POLICY,
            "input_fingerprint": request.fingerprint,
            "attempt": previous.get("attempt", 1) + 1 if matching else 1,
            "findings": [finding.to_data()],
        }
        review["fingerprint"] = content_fingerprint(review)
        self._state["reviews"][dimension] = review
        self._state.setdefault("review_history", []).append(review)
        return review

    def _context_fingerprint(self) -> str:
        # Confirmation binds displayed behavior and its authority context, not
        # allocator bookkeeping or derived case records that are not displayed.
        return content_fingerprint(
            {
                "story_contract_digest": story_contract_digest(
                    self.catalog, self.story_id
                ),
                "sources": self._scoped_sources(),
                "dependencies": self._dependency_digests(),
            }
        )

    def ask(
        self, dimension: str, text: str, *, proposed_assumption: str | None = None
    ) -> dict[str, Any]:
        if (
            dimension not in STORY_REVIEW_DIMENSIONS
            or not text.strip()
            or len(text) > 2000
        ):
            raise ValueError(
                "ask one bounded story question with a supported dimension"
            )
        if proposed_assumption is not None and (
            not proposed_assumption.strip() or len(proposed_assumption) > 2000
        ):
            raise ValueError("assumption requires a bounded concrete statement")
        identity = (
            "story-question:"
            + content_fingerprint(
                {
                    "session": self._state["session_id"],
                    "story": self.story_id,
                    "dimension": dimension,
                    "text": text,
                    "assumption": proposed_assumption,
                }
            )[7:31]
        )
        previous = self._state["answers"].get(identity)
        if previous:
            return {"answered": True, **previous}
        display = text
        if proposed_assumption:
            display += (
                "\nProposed behavior: "
                + proposed_assumption
                + "\nConfirm this behavior, reject it, or describe a different outcome."
            )
        question = StoryQuestion(
            identity,
            "assumption" if proposed_assumption else "refinement",
            dimension,
            display,
            story_contract_digest(self.catalog, self.story_id),
            self._context_fingerprint(),
            proposed_assumption,
        )
        self._state["questions"][identity] = question.to_data()
        self._state["pending_question_id"] = identity
        return {"answered": False, **question.to_data()}

    def pending_question(
        self, question_id: str, displayed_text: str, *, allow_answered: bool = False
    ) -> StoryQuestion:
        replayed = allow_answered and question_id in self._state["answers"]
        if self._state["pending_question_id"] != question_id and not replayed:
            raise StoryContractError(
                "story_question_stale", question_id, "question is not pending"
            )
        raw = self._state["questions"][question_id]
        if set(raw) - {
            "question_id",
            "kind",
            "dimension",
            "text",
            "contract_digest",
            "context_fingerprint",
            "proposed_assumption",
        }:
            raise ValueError("unknown story question fields")
        if (
            not isinstance(raw.get("text"), str)
            or not raw["text"].strip()
            or len(raw["text"]) > 100000
        ):
            raise ValueError("invalid story question display")
        question = StoryQuestion(
            _decode(str, raw["question_id"], question_id),
            _decode(
                Literal["refinement", "assumption", "confirmation"],
                raw["kind"],
                question_id,
            ),
            _decode(str, raw["dimension"], question_id),
            raw["text"],
            _decode(str | None, raw.get("contract_digest"), question_id),
            _decode(str, raw["context_fingerprint"], question_id),
            _decode(str | None, raw.get("proposed_assumption"), question_id),
        )
        if question.text != displayed_text or (
            not replayed and question.context_fingerprint != self._context_fingerprint()
        ):
            raise StoryContractError(
                "story_confirmation_stale",
                question_id,
                "the displayed candidate or evidence changed",
            )
        return question

    def accept_user_response(self, response: StoryUserResponse) -> dict[str, Any]:
        """Only an adapter with an actual user-channel response may call this."""
        previous = self._state["answers"].get(response.question_id)
        if previous is not None:
            self.pending_question(
                response.question_id, response.displayed_text, allow_answered=True
            )
            if previous["answer"] != response.answer:
                raise StoryContractError(
                    "story_answer_conflict",
                    response.question_id,
                    "the recorded answer cannot be overwritten by redelivery",
                )
            return {
                "source_ref": previous["source_ref"],
                "accepted": previous.get("accepted", True),
                **self.status(),
            }
        question = self.pending_question(response.question_id, response.displayed_text)
        if not response.answer.strip():
            raise ValueError("an empty response cannot resolve or confirm a story")
        if question.kind == "confirmation" and (
            self._state["benchmark_mode"]
            or self._state["uncertainty_policy"] != "clarify"
        ):
            raise StoryContractError(
                "story_confirmation_authority_invalid",
                self.story_id,
                "normative defaults and benchmark runs cannot grant user confirmation",
            )
        affirmative = response.answer.strip().casefold() in {
            "yes",
            "confirm",
            "confirmed",
            "approve",
            "approved",
            "i confirm",
        }
        ref = (
            f"interview:{self._state['session_id']}:answer:"
            f"{len(self._state['answers']) + 1}"
        )
        record = {
            "id": ref,
            "kind": "user_answer",
            "question": question.text,
            "text": response.answer,
            "question_id": question.question_id,
            "proposed_assumption": question.proposed_assumption,
            "assumption_confirmed": bool(affirmative and question.kind == "assumption"),
        }
        self._state["answers"][question.question_id] = {
            "question_id": question.question_id,
            "answer": response.answer,
            "source_ref": ref,
            "accepted": affirmative if question.kind == "confirmation" else True,
        }
        self._state["pending_question_id"] = None
        if question.kind == "confirmation":
            # The confirmation itself is authority evidence, not a new semantic
            # premise that would invalidate the already completed review.
            self._state["confirmation_evidence"] = {
                **record,
                "contract_digest": question.contract_digest,
                "context_fingerprint": question.context_fingerprint,
                "accepted": affirmative,
            }
            if affirmative:
                story = self.catalog.stories_by_id[self.story_id]
                confirmation = StoryConfirmation(
                    question.contract_digest or "", ref, "user_confirmation"
                )
                catalog = self.catalog
                self._state["draft"] = replace(
                    catalog,
                    user_stories=tuple(
                        replace(s, confirmation=confirmation) if s.id == story.id else s
                        for s in catalog.user_stories
                    ),
                ).to_data()
        else:
            self._state["sources"][ref] = record
        return {
            "source_ref": ref,
            "accepted": affirmative if question.kind == "confirmation" else True,
            **self.status(),
        }

    def _semantic_ready(self) -> tuple[bool, list[str]]:
        if self._state.get("dependency_failure"):
            return False, list(STORY_REVIEW_DIMENSIONS)
        missing = []
        for dimension in STORY_REVIEW_DIMENSIONS:
            request = self.review_request(dimension)
            review = self._state["reviews"].get(dimension)
            if (
                not review
                or review["input_fingerprint"] != request.fingerprint
                or any(
                    f["classification"] not in {"resolved", "not_applicable"}
                    for f in review["findings"]
                )
            ):
                missing.append(dimension)
        return not missing, missing

    def status(self) -> dict[str, Any]:
        catalog = self.catalog
        external = self.reference_catalog()
        evidence = self._state.get("confirmation_evidence")
        if evidence:
            external["source_refs"].append(evidence["id"])
        report = validate_story_catalog(catalog, external)
        status = "draft"
        missing = list(STORY_REVIEW_DIMENSIONS)
        identity = self._state.get("story_id")
        digest = None
        if identity in catalog.stories_by_id:
            digest = story_contract_digest(catalog, identity)
            if report.passed:
                semantic_ready, missing = self._semantic_ready()
                if semantic_ready:
                    status = "needs_confirmation"
                    if (
                        evidence
                        and evidence.get("accepted") is True
                        and evidence["contract_digest"] == digest
                        and not self._state["benchmark_mode"]
                        and self._state["uncertainty_policy"] == "clarify"
                    ):
                        # Review inputs intentionally exclude confirmation metadata.
                        if (
                            evidence["context_fingerprint"]
                            == self._confirmation_context()
                        ):
                            status = "ready"
        return {
            "status": status,
            "ready": status == "ready",
            "story_id": identity,
            "contract_digest": digest,
            "missing_review_dimensions": missing,
            "structural_findings": [_data(i) for i in report.issues],
            "unmapped_step_ids": list(report.unmapped_step_ids),
            "catalog": self._scoped_catalog(catalog),
            "available_stories": [
                {"id": story.id, "title": story.title, "goal": story.goal}
                for story in catalog.user_stories[:64]
            ],
            "story_catalog_complete": len(catalog.user_stories) <= 64,
            "total_stories": len(catalog.user_stories),
            "source_catalog": self._scoped_sources(),
            "default_source_ref": next(
                ref
                for ref, record in self._state["sources"].items()
                if record["kind"] == "instruction"
            ),
            "review_dimensions": list(STORY_REVIEW_DIMENSIONS),
            "dependency_findings": self._state.get("dependency_failure"),
        }

    def _confirmation_context(self) -> str:
        return self._context_fingerprint()

    def present(self) -> dict[str, Any]:
        current = self.status()
        if current["ready"]:
            return {
                "answered": True,
                "question_id": self._state["confirmation_evidence"]["question_id"],
                **current,
            }
        if current["structural_findings"] or current["missing_review_dimensions"]:
            raise StoryContractError(
                "story_review_unavailable",
                self.story_id,
                "resolve every structural/semantic finding before confirmation",
            )
        if (
            self._state["benchmark_mode"]
            or self._state["uncertainty_policy"] != "clarify"
        ):
            raise StoryContractError(
                "story_confirmation_authority_invalid",
                self.story_id,
                "this execution cannot grant user confirmation",
            )
        catalog = self.catalog
        self._state["draft"] = replace(
            catalog,
            user_stories=tuple(
                replace(s, confirmation=None) if s.id == self.story_id else s
                for s in catalog.user_stories
            ),
        ).to_data()
        digest = story_contract_digest(self.catalog, self.story_id)
        text = (
            self.render() + f"\n\nContract: {digest}\n"
            "Reply confirm to approve this exact story, or describe the changes needed."
        )
        identity = (
            "story-confirmation:"
            + content_fingerprint(
                {
                    "session": self._state["session_id"],
                    "context": self._context_fingerprint(),
                }
            )[7:31]
        )
        question = StoryQuestion(
            identity,
            "confirmation",
            "source_fidelity",
            text,
            digest,
            self._context_fingerprint(),
        )
        self._state["questions"][identity] = question.to_data()
        self._state["pending_question_id"] = identity
        return {"answered": False, **question.to_data()}

    def render(self) -> str:
        catalog = self.catalog
        story = catalog.stories_by_id[self.story_id]
        lines = [
            f"### {story.title}",
            story.narrative,
            f"Actor: {story.actor}",
            f"Goal: {story.goal}",
            "Preconditions: " + "; ".join(story.preconditions),
            "Scope: " + "; ".join(story.scope),
            "Entry: " + story.entry_step_id,
            "",
            (
                "| Step | Description and action | Transitions | Observations "
                "and state effects |"
            ),
            "| --- | --- | --- | --- |",
        ]

        def cell(value: str) -> str:
            return value.replace("|", "\\|").replace("\n", " ")

        for step in sorted(
            (s for s in catalog.user_story_steps if s.story_id == story.id),
            key=lambda s: s.id,
        ):
            action = (
                f" {step.action.actor}: {step.action.operation}; "
                f"input: {step.action.input}."
                if step.action
                else ""
            )
            branches = "; ".join(
                f"{e.id}: {e.condition.kind}: {e.condition.description} "
                f"→ {e.to_step_id}"
                for e in catalog.user_story_transitions
                if e.story_id == story.id and e.from_step_id == step.id
            )
            effects = "; ".join(
                f"{o.id}: {o.description} ({o.boundary})"
                for o in step.expected_observations
            )
            effects += "; ".join(
                f"{e.subject}: {e.before} → {e.after}; persistence: {e.persistence}"
                for e in step.state_effects
            )
            lines.append(
                "| "
                + " | ".join(
                    map(
                        cell,
                        [
                            step.id,
                            step.kind
                            + ": "
                            + step.description
                            + action
                            + (" Outcome: " + step.outcome if step.outcome else ""),
                            branches,
                            effects,
                        ],
                    )
                )
                + " |"
            )
        lines += [
            "",
            "Related stories: "
            + "; ".join(
                f"{r.story_id} ({r.relationship})" for r in story.related_story_refs
            ),
            "Invariants: " + "; ".join(story.invariant_refs),
            "",
            "Recorded proposed assumptions:",
        ]
        for source in self._state["sources"].values():
            if source.get("proposed_assumption"):
                lines.append(
                    f"- {source['proposed_assumption']} — "
                    f"user's response: {source['text']}"
                )
        return "\n".join(lines)

    def evidence_artifact(self) -> dict[str, Any]:
        from powdrr_service.structrr.snapshot import snapshot_digest

        data = {
            "schema_version": "story-interview-evidence-v1",
            "session_id": self._state["session_id"],
            "source_revision": self._state["source_revision"],
            "baseline_snapshot_fingerprint": snapshot_digest(self._state["baseline"]),
            "instruction_ledger": self._state["instruction_ledger"],
            "catalog": self._scoped_catalog(self.catalog),
            "sources": self._scoped_sources(),
            "reviews": self._state["reviews"],
            "review_history": self._state.get("review_history", []),
            "confirmation_evidence": self._state["confirmation_evidence"],
        }
        return {**data, "fingerprint": content_fingerprint(data)}

    def compile_proposal(self) -> ProposalRevision:
        if not self.status()["ready"]:
            raise StoryContractError(
                "story_not_ready",
                self.story_id,
                "current review and exact user confirmation are required",
            )
        before = parse_story_catalog(self._state["baseline"]).to_data()
        after = self.catalog.to_data()
        sections: dict[str, Any] = {}
        for section in STORY_SECTIONS:
            old, new = (
                {r["id"]: r for r in before[section]},
                {r["id"]: r for r in after[section]},
            )
            changes = []
            for identity in sorted(old.keys() | new.keys()):
                if old.get(identity) == new.get(identity):
                    continue
                action = (
                    "remove"
                    if identity not in new
                    else "add"
                    if identity not in old
                    else "change"
                )
                changes.append(
                    {
                        "id": identity,
                        "action": action,
                        "content": new.get(identity, {"id": identity}),
                    }
                )
            sections[section] = changes
        evidence = self._state["confirmation_evidence"]
        return compile_proposal_revision(
            self._state["work_item_name"] + ":story",
            self._state["baseline"],
            sections,
            acceptance_criteria=[],
            must_preserve=[],
            non_goals=[],
            allowed_paths=["docs/proposals/" + self._state["work_item_name"]],
            source_refs=[*self._state["sources"], evidence["id"]],
        )
