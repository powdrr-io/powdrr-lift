# Intent-Preserving Change Control PR Plan

## Purpose

This document turns the intent-preserving change-control design into an
implementation-ready pull-request sequence. It identifies the first production
boundary to enforce, defines each subsequent PR, and provides validation gates
another agent can follow without deciding which controls are optional.

It refines the broader sequencing in
[`intent-preserving-change-implementation-plan.md`](intent-preserving-change-implementation-plan.md).
That document remains the architectural inventory; this is the execution
checklist.

## Starting conclusion

Begin at the contract immediately before OpenCode:

> Introduce a fingerprinted `ProposalRevision` artifact and require a
> deterministic proposal-acceptance gate before `run_opencode`.

This is the smallest vertical change that establishes a durable identity for
the requested change and enforces it in production. Every later capability
depends on that identity:

- proposal-time hidden-intent review needs a stable proposal subject;
- implementation-start revalidation needs an accepted baseline fingerprint;
- targeted OpenCode packets need an authoritative operation set;
- post-implementation reconciliation needs a proposal fingerprint to fulfill;
- longitudinal drift audits need immutable lineage between revisions.

Do not begin with a drift dashboard, a broad prompt rewrite, or disconnected
state-model classes. Those would report or transmit claims that are not yet
bound to an accepted proposal transaction.

## Current production behavior

The checked-in `implement-feature` flow currently executes:

```text
ensure current Structrr
-> design interview
-> write Structrr diff
-> run OpenCode
-> run validation
-> review changed paths and diff hygiene
-> open pull request
```

The codebase already supplies useful foundations:

- Structrr produces validated repository snapshots, fingerprints snapshots,
  and classifies rebases.
- Procedrr owns checked-in phase ordering and validates single-decision judge
  outputs.
- `ExecutionPlan` and `ExecutionUnit` provide typed implementation units.
- `ImplementationRequest` bounds OpenCode by commit, paths, commands,
  acceptance criteria, and planned Structrr additions and deletions.
- Workrr independently observes changed paths and the resulting diff.
- Validation reports and execution evidence have durable identities.

The missing enforcement is specific:

1. The Structrr diff is committed before a proposal-acceptance gate.
2. Proposal and proposal-evaluation outputs do not control implementation.
3. The proposed-PR fingerprint is a slug-derived label, not a digest of
   accepted proposal content.
4. OpenCode invocation checks path and commit boundaries but not an accepted
   proposal receipt or current Structrr applicability.
5. Final review checks worker status, tests, path scope, and diff hygiene, but
   not intent fulfillment, preservation, or unexplained semantic drift.

## Cross-PR implementation rules

### Authority

```text
Structrr computes facts and pure state transitions.
Procedrr schedules mandatory checks and controls legal progression.
Workrr performs bounded semantic work and implementation orchestration.
OpenCode attempts edits under one immutable work order.
```

Neither Workrr nor OpenCode may choose whether a mandatory gate runs.

### Single-decision normal form

Every semantic decision evaluates one predicate for one subject. A judge may
return one value from a closed outcome set plus evidence and rationale. It may
not return a list of decisions or an aggregate gate result.

Procedrr derives the overall gate mechanically:

```text
gate passes iff every required decision has a current passing outcome
```

### Fail-closed progression

Missing, unknown, conflicting, stale, or unevaluable required evidence blocks
the transition it protects. A model timeout or malformed result is not a pass.

### Immutable revision identity

Accepted artifacts are never edited in place. Any semantic or applicability
change creates a successor revision with a parent fingerprint and repeats all
gates invalidated by the change.

### Evidence binding

Every decision and receipt identifies the exact proposal revision, Structrr
state, repository tree, predicate version, subject, and evidence fingerprint it
evaluated. Changing any input invalidates the result.

## PR 1: Accepted proposal revision and hard pre-worker gate

### Suggested title

`Enforce fingerprinted proposal revisions before implementation`

### Goal

No coding worker may run unless Powdrr has a canonical proposal revision, the
exact Structrr baseline against which it was prepared, explicit change
operations, a passing persisted proposal review, and fingerprints binding all
of those artifacts.

This PR must integrate the contract into the production feature flow. Adding
types without blocking `run_opencode` is incomplete.

### Canonical proposal model

Add a focused Structrr module such as
`src/powdrr_lift/structrr/change_control.py`, containing at least:

- `ProposalRevision`;
- `ProposalOperation`;
- `ProposalEvidenceRequirement`;
- `ProposalDecisionResult`;
- `ProposalReview`; and
- `ProposalGateResult`.

`ProposalRevision` must contain:

```text
schema_version
proposal_id
revision
parent_revision_fingerprint | null
structrr_baseline_fingerprint
source_request_fingerprint
operations[]
acceptance_criteria[]
expected_architecture_changes[]
allowed_paths[]
validation_profiles[]
open_decision_ids[]
fingerprint
```

Each `ProposalOperation` must contain:

```text
operation_id
subject_type
subject_id
action: add | alter | remove | preserve
before_fingerprint | null
after
rationale
source_refs[]
evidence_requirements[]
```

Required semantics:

- `add` introduces a subject absent from the accepted basis;
- `alter` requires and replaces identified prior state;
- `remove` requires and retires identified prior state;
- `preserve` names relevant intent or architecture implementation must leave
  satisfied;
- `alter` and `remove` require `before_fingerprint`;
- operation IDs are stable within the proposal lineage;
- the proposal fingerprint covers canonical serialized content and excludes
  only its own field; and
- no free-form change bucket may bypass typed operations.

Persist artifacts beneath the proposal directory:

```text
docs/proposals/<slug>/proposal-revision.json
docs/proposals/<slug>/proposal-review.json
```

Retain `structrr-diff.yaml` for compatibility. Treat it as proposal-compilation
input, not sufficient implementation authority.

### Pure deterministic functions

Add pure functions equivalent to:

```python
compile_proposal_revision(...)
validate_proposal_revision(...)
fingerprint_proposal_revision(...)
evaluate_proposal_gate(...)
```

Validation emits independently addressable findings. At minimum, evaluate:

1. The schema version is supported.
2. The stored fingerprint matches canonical content.
3. The Structrr baseline fingerprint is present and valid.
4. Operation IDs are unique.
5. Every operation uses a supported action.
6. Every alteration identifies prior state.
7. Every removal identifies prior state.
8. Every source reference resolves.
9. No subject has contradictory operations.
10. Every changing operation has an evidence requirement.
11. Acceptance criteria are non-empty.
12. Allowed paths are non-empty and safe.
13. Validation profiles are known.
14. No unresolved decision remains.
15. Applying operations produces a structurally valid candidate state.

A persisted finding should have a stable shape such as:

```json
{
  "decision_id": "proposal.operations.non_contradictory",
  "subject_ids": ["intent-123"],
  "outcome": "pass",
  "evidence_refs": ["proposal-revision.json#operations/3"]
}
```

The gate is derived from these findings; no model returns `accepted`.

### Procedrr integration

Change the production sequence to:

```text
ensure_current_structrr
-> design_interview
-> plan_structrr_diff
-> compile_proposal_revision
-> review_proposal_revision
-> gate proposal_review.accepted == true
-> revalidate_proposal_basis
-> gate proposal_basis.applicable == true
-> run_opencode
```

Keep review and basis revalidation distinct. Review asks whether the resulting
state is explicit and coherent. Basis revalidation asks whether that accepted
proposal is still applicable immediately before implementation.

PR 1 may initially support only `applicable` and `stale`. PR 3 expands this with
rebase and revision outcomes.

### Workrr boundary enforcement

Upgrade `ImplementationRequest` to a new schema version containing:

```text
proposal_id
proposal_revision
proposal_fingerprint
structrr_baseline_fingerprint
proposal_review_fingerprint
```

Before invoking `OpenCodeProvider`, Workrr verifies:

- proposal content matches its fingerprint;
- review content matches its fingerprint;
- the review is accepted;
- the review names the exact proposal fingerprint;
- current Structrr matches the accepted baseline fingerprint;
- current Git commit matches the request base commit; and
- no required decision remains unresolved.

This is defense in depth: Procedrr controls ordering; Workrr rejects direct,
stale, or malformed invocation.

### Transaction-wide fingerprint

Replace the slug-derived proposed-PR fingerprint with the proposal revision
fingerprint. Carry it through:

- proposal review;
- execution plan;
- implementation request;
- coding-agent attempt;
- validation report; and
- execution event log.

### Required validation gates

#### Schema gate

- Serialization round trips without changing identity.
- Mapping key order does not affect fingerprints.
- Collection ordering is canonicalized or documented as significant.
- Unknown schema versions fail closed.
- Missing required fields fail closed.

#### Proposal consistency gate

Add table-driven fixtures for:

- valid addition, alteration, and removal;
- contradictory add and remove operations for one subject;
- alteration without a prior-state fingerprint;
- removal of an absent subject;
- duplicate operation IDs;
- missing evidence requirements;
- unsafe allowed paths;
- unknown validation profiles;
- unresolved human decisions; and
- a candidate state violating a Structrr relationship.

#### Procedrr ordering gate

Evaluator tests prove:

- `run_opencode` is unreachable when proposal review fails;
- `run_opencode` is unreachable when applicability fails;
- a passing proposal reaches `run_opencode` exactly once; and
- removing or reordering either gate makes checked-in flow validation fail.

Use a fake OpenCode runner that raises immediately if called by a failure
fixture.

#### Tamper gate

After acceptance, independently mutate:

- `structrr-diff.yaml`;
- `proposal-revision.json`;
- current Structrr baseline;
- proposal review; and
- execution plan.

Every mutation must block worker invocation through a fingerprint or basis
mismatch.

#### Compatibility gate

- Proposal generation still writes a valid `structrr-diff.yaml`.
- Existing execution-plan tests pass.
- Existing coding-agent permission and path restrictions remain unchanged.
- A valid accepted proposal still completes the normal endpoint path.

#### Repository gate

Before opening the PR, run the complete pytest suite, `ruff format --check`,
Ruff lint, the configured type checker, changelog validation, and checked-in
Procedrr definition validation.

### Explicit non-goals

PR 1 excludes model classification of hidden intent changes, automatic
proposal repair, repository-wide targeted-intent discovery, semantic
implementation reconciliation, cumulative drift scoring, and UI work.

### Exit condition

A production feature run cannot invoke OpenCode without an accepted,
fingerprinted proposal revision bound to current Structrr and the Git commit.
Tampering with any accepted input deterministically blocks invocation.

## PR 2: Proposal-time hidden-intent review

### Suggested title

`Require explicit intent operations for proposal effects`

### Goal and flow

Detect when architecture or behavior could alter active intent without an
explicit `alter` or `remove` operation. For every proposal operation:

1. Resolve affected Structrr subjects deterministically.
2. Enumerate active intent clauses attached to those subjects.
3. Evaluate one operation and one clause at a time.
4. Record exactly one classification:

```text
preserved
explicitly_altered
explicitly_removed
possibly_altered_without_statement
not_applicable
```

5. Convert `possibly_altered_without_statement` into a durable obligation.
6. Block acceptance until an explicit operation or recorded human decision
   resolves the obligation.

Each judge receives one operation, one intent clause, relevant before and
proposed-after facts, and the closed enum. It may not rewrite the proposal,
return multiple classifications, or decide the overall gate.

### Required validation gates

- Procedrr rejects multiple decisions in one judge output.
- Every affected active clause receives exactly one classification.
- A clause cannot be both preserved and altered.
- Hidden alteration creates an open obligation.
- An explicit operation closes the exact obligation.
- Identical inputs produce byte-identical decision IDs and fingerprints.
- Acceptance is impossible with open, stale, unknown, or failed obligations.

### Exit condition

Any apparent change to retained intent is explicit or resolved by a recorded
human decision before proposal acceptance.

## PR 3: Implementation-start applicability and revision

### Suggested title

`Revalidate accepted proposals against current Structrr state`

### Goal and implementation

Immediately before implementation, choose exactly one outcome:

```text
applicable
mechanically_rebased
revision_required
incompatible
```

Reuse Structrr snapshot digest and rebase classifications. For each operation,
decide whether its subject and prior fingerprint still match, dependencies or
relevant intent changed, references can be mechanically remapped, and the
expected resulting state remains consistent.

Semantic changes create a successor:

```text
revision = prior revision + 1
parent_revision_fingerprint = prior fingerprint
```

Never overwrite an accepted revision. A successor repeats invalidated gates.

### Required validation gates

- Unchanged Structrr returns `applicable`.
- Stable remapping returns `mechanically_rebased` with evidence.
- Changed prior state returns `revision_required`.
- Missing prerequisites return `incompatible`.
- A successor cannot reuse its parent's acceptance receipt.
- OpenCode receives only the newest accepted and applicable revision.
- Rebase output is deterministic for the same inputs.

### Exit condition

Implementation cannot start from stale assumptions. Mechanical rebases are
evidenced, semantic changes create reviewed successors, and incompatibility
stops execution.

## PR 4: Targeted intent packets for OpenCode

### Suggested title

`Compile operation-scoped intent packets for coding workers`

### Packet contract

```text
packet_id
proposal_fingerprint
execution_unit_id
structrr_baseline_fingerprint
required_operations[]
must_preserve[]
explicitly_removed[]
acceptance_criteria[]
allowed_paths[]
forbidden_effects[]
validation_profiles[]
source_refs[]
selection_explanations[]
packet_fingerprint
```

Discover packet content by traversing explicit Structrr edges from operation
subject to owner, parent or container, relevant relationships, attached active
intent, invariants, and acceptance criteria. Do not use semantic similarity for
inclusion. Ambiguous or incomplete discovery creates a blocking obligation;
it does not trigger a dump of all historical intent.

### Required validation gates

- Every selected entry records why it was selected.
- Every operation has exactly one responsible execution unit.
- Every affected retained clause appears in `must_preserve`.
- Explicit removals are absent from `must_preserve`.
- Unrelated intent is absent from controlled fixtures.
- Packet fingerprint is verified immediately before invocation.
- Prompt rendering is deterministic and covered by golden tests.
- Size limits split work instead of dropping mandatory intent.

### Exit condition

OpenCode receives a fingerprinted, operation-scoped intent packet whose
completeness is mechanically explainable and irrelevant context is bounded.

## PR 4A: Lossless behavior contracts and repair feedback

### Suggested title

`Compile typed behavior scenarios and verifier-driven repair handoffs`

### Goal

Close the gap between a requirement that is mentioned in prose and the exact
behavior the coding worker must implement. A worker prompt must preserve not
only the desired success path, but also observable errors, continuation rules,
unsupported capabilities, cleanup behavior, and compatibility boundaries.

This slice is prompted by a failure mode where the prompt said that errors must
not halt later incremental items, but did not say that errors nested inside an
incremental item must be exposed on the yielded result. The worker continued
correctly while silently dropping the errors. A second failure exposed the
opposite omission: unsupported transports were never assigned a required
behavior, so the worker had no contract for whether to support or reject them.

### Typed behavior scenario

Each public behavior change must compile its requirements into scenarios with
these fields:

```yaml
scenario_id: incremental-item-errors
subject: session.execute_incremental
given:
  payload_shape: incremental item containing errors and a path
when:
  operation: consume the payload and then a later payload
then:
  yielded_result_errors: include every item error with its path
  later_payload: still yields a result and is merged
must_expose:
  - errors
must_preserve:
  - error message
  - error path
must_reject: []
must_continue:
  - later incremental items are processed
evidence:
  - focused executable test
```

The compiler must require an explicit value, including `not_applicable`, for
each of these dimensions:

- normal result and accumulated state;
- error location, shape, and propagation;
- continuation after an error;
- unsupported input, transport, or capability behavior;
- cancellation and cleanup;
- compatibility and preservation behavior; and
- negative or boundary cases.

`not_applicable` is a recorded contract decision, not an omitted field. The
compiler must reject an unresolved dimension before producing a worker prompt.

### Capability matrix

For an API or protocol extension, compile a capability matrix from the
repository's transport/provider inventory:

```text
capability           required behavior       evidence
HTTP multipart       support                 executable test
WebSocket            support                 executable test
LocalSchemaTransport reject with exception   executable test
unknown transport    reject with exception   executable test
```

The matrix must distinguish “not mentioned by the feature request” from
“explicitly unsupported.” Existing adapter behavior, public API conventions,
and compatibility tests may supply the authority; otherwise the dimension is
unresolved and blocks handoff rather than being guessed by the worker.

### Prompt projection

Render one concise behavior matrix into the coding-worker prompt. Each row
contains the scenario, input/payload shape, expected output, error behavior,
continuation behavior, and validator target. Do not render the same meaning
again as separate product-contract, acceptance-criterion, and focused-validator
paragraphs. The private manifest may retain the richer provenance and evidence
mapping.

The prompt/manifest gate must prove:

- every scenario has one prompt reference and one evidence reference;
- every `must_expose`, `must_reject`, and `must_continue` field is rendered;
- every negative capability has an executable assertion or an explicit typed
  exemption;
- no scenario is represented only by a vague phrase such as “handle errors”;
- prompt and manifest fingerprints are derived from the same contract revision;
- generated prompt size is bounded by deduplicating representations, never by
  dropping a behavior dimension.

### Structured validation failure

Normalize local validation and benchmark/verifier results into the same failure
record:

```yaml
failure_id:
stage: local_validation | verifier | scope | policy
test_id:
command: []
contract_refs: []
expected: ...
actual: ...
source_location: ...
evidence_path: ...
classification: product_failure | test_failure | environment_failure | policy_failure
repairability: repairable | blocked | human_required
```

The repair handoff must include the exact current failures, not only a generic
`coding_attempt_incomplete` status. It must include the relevant test names,
assertion or expected/actual values, contract references, and the current
candidate diff. Historical passing evidence is retained for diagnosis but is
not substituted for current failure evidence.

### Repair boundary

Repair is a new bounded evidence epoch over the existing candidate worktree.
The repair agent may edit and commit. Workrr records the repair base commit,
allows `HEAD` to advance, and validates the resulting diff from that base.
It rejects out-of-scope paths, destructive history rewrites, or changes not
covered by the repair scope; it must not reject a normal repair commit merely
because `HEAD` changed.

After repair, the runtime reruns all affected validation and verifier gates.
Evidence from before the repair cannot be reused as current proof. A repair
prompt may be concise, but it must contain the current failure records and the
original contract context needed to interpret them.

### Required validation gates

- A contract with an omitted error, negative-capability, continuation, or
  cleanup dimension cannot compile.
- A nested error is covered separately from the rule that later work continues.
- Every supported capability has a positive executable case.
- Every rejected capability has a negative executable case and defined error
  type/condition.
- A local validation failure and a verifier failure produce the same typed
  repair input shape.
- A repair commit advances `HEAD` without triggering a policy failure.
- Repair reruns the affected gates against a fresh candidate-tree fingerprint.
- Golden prompt tests prove each scenario appears exactly once in the worker
  prompt.

### Exit condition

The coding worker receives a lossless, scenario-based implementation contract;
the repair worker receives exact current failures; and repair commits are
validated by diff scope and fresh evidence rather than rejected as illegal
state changes.

## PR 5: Actualization and post-implementation reconciliation

### Suggested title

`Reconcile worker diffs against accepted intent operations`

### Goal

Replace shallow final review with an actualization report proving proposal
fulfillment and retained-intent preservation.

Workrr independently derives changed paths, Git diff fingerprint, changed
Structrr subjects, architecture additions/alterations/removals, validation
results, and evidence freshness. Worker claims remain untrusted assertions.

For each operation, record one of:

```text
fulfilled
not_fulfilled
partially_fulfilled
contradicted
not_evaluable
```

For each retained clause, record one of:

```text
preserved
violated
insufficient_evidence
```

For each unexplained change, record one of:

```text
implementation_detail
unplanned_architecture_change
unplanned_intent_change
generated_or_ephemeral
```

PR creation requires every operation fulfilled, every retained clause
preserved, no unexplained semantic changes, fresh evidence, and all existing
worker, path, validation, and diff-hygiene checks passing.

### Required validation gates

Adversarial fixtures must block PR creation when tests pass but behavior is
absent, a removal is omitted, an unrelated invariant is weakened, worker
claims disagree with the observed diff, extra architecture appears, evidence
belongs to an older diff, or allowed paths contain an intent violation.

### Exit condition

Passing tests and in-scope paths are insufficient; PR creation requires
fulfillment, preservation, and no unexplained semantic drift.

## PR 6: Cumulative lineage and drift audit

### Suggested title

`Add longitudinal intent lineage and drift audits`

### Required lineage

```text
source chat update
-> intent clause and version
-> proposal operation
-> accepted proposal revision
-> execution unit
-> targeted intent packet
-> observed implementation diff
-> validation evidence
-> accepted Structrr state
```

Add a deterministic audit that checks active-clause provenance, supersession
links, actualization evidence for accepted operations, evidence fingerprints,
current invariant satisfaction, and unresolved drift findings. Report concrete
findings and severity counts rather than a fuzzy score.

### Required validation gates

- Replaying history produces the same report.
- Missing lineage edges are reported.
- Superseded intent is not active.
- Stale evidence is rejected.
- Historical acceptance cannot mask a current invariant failure.
- The report identifies the first revision where drift appeared.
- Findings include enough artifact references to reproduce the result.

### Exit condition

Powdrr can prove or disprove current conformance to long-lived intent and trace
each finding to the first relevant accepted revision and evidence set.

## Dependency and merge order

```text
PR 1  Accepted proposal artifact and hard pre-worker gate
  |
PR 2  Explicit hidden-intent alteration review
  |
PR 3  Implementation-start revalidation and revision
  |
PR 4  Targeted OpenCode intent packets
  |
PR 4A Lossless behavior contracts and verifier-driven repair handoffs
  |
PR 5  Actualization and post-implementation reconciliation
  |
PR 6  Cumulative lineage and drift audits
```

Do not implement PRs 2 through 6 against unmerged predecessor contracts. They
exchange fingerprinted artifacts, and parallel invention would create
incompatible representations.

## Agent handoff checklist

For each PR, the implementing agent must:

1. Create a dedicated worktree and branch from current `origin/main`.
2. Read the control design and both implementation plans.
3. State the exact transition the PR makes impossible to bypass.
4. List every persisted artifact and its fingerprint inputs.
5. Implement pure validation before orchestration integration.
6. Add the Procedrr operation and explicit gate before worker behavior.
7. Add Workrr boundary checks so direct invocation fails closed.
8. For behavior changes, cover success, error propagation, continuation,
   unsupported capabilities, cleanup, every closed outcome, malformed output,
   stale evidence, tampering, and prohibited bypass.
9. Normalize local and verifier failures into repair records and test the
   repair handoff, including an allowed repair commit.
10. Run the complete repository verification suite.
11. Add and validate the PR changelog.
12. Inspect the final diff for scope and generated files.
13. Commit, push, and open a PR without merging it.

The PR description must identify the protected transition, scheduling
authority, atomic decisions, evidence freshness rules, failure and revision
behavior, bypass tests, and explicit non-goals.

## Program completion condition

The sequence is complete only when production enforces:

```text
chat update
-> explicit intent diff
-> self-consistent accepted proposal revision
-> current-state applicability proof
-> targeted immutable OpenCode work order
-> independently observed implementation
-> operation fulfillment and intent-preservation proof
-> accepted actual Structrr state
-> reproducible longitudinal drift audit
```

An LLM never decides whether one of these transitions requires its gate. Models
may supply bounded atomic judgments; Procedrr schedules them, Structrr defines
the facts, and Workrr supplies independently verified evidence.
