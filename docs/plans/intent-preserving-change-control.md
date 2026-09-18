# Intent-Preserving Change Control

## Status

This document proposes the control protocol that joins Structrr, Procedrr,
Workrr, and OpenCode into one intent-preserving change system. It extends the
existing durable-intent and OpenCode execution designs. It does not replace the
accepted specification formats or the existing execution safety model.

The protocol is designed for codebases that may be changed over hundreds or
thousands of interactions. Its central requirement is that neither conversation
length nor model judgment determines whether previously accepted intent remains
in force.

## Problem

Powdrr already has important parts of the desired system:

- Structrr can bootstrap source-anchored snapshots and compare a proposal
  baseline with a newer snapshot.
- Procedrr can express deterministic operations, bounded model judgments, loops,
  branches, and single-decision validation.
- Workrr can compile an execution unit into a bounded implementation request,
  invoke OpenCode in an isolated worktree, constrain permissions and paths, and
  observe the resulting diff.
- The execution runtime can persist actions, obligations, evidence, findings,
  checkpoints, and phase transitions.

Those pieces do not yet establish one end-to-end intent transaction. In
particular:

1. A chat update is not always represented as an explicit diff against the
   accepted intent state.
2. A proposal can imply a behavioral or architectural intent change without
   declaring the intent alteration directly.
3. Proposal validation does not yet construct and validate the complete
   resulting Structrr state as one transaction.
4. A proposal can become stale between acceptance and implementation without a
   mandatory revalidation and revision protocol.
5. OpenCode receives a bounded objective and planned Structrr additions and
   deletions, but not a compiled, explainable closure of the exact active intent
   that governs one implementation operation.
6. Post-implementation review does not yet reconcile every accepted intent
   operation, every expected architecture operation, every actual change, and
   every affected retained intent.

The result can be locally correct work that gradually diverges from an older,
still-active instruction.

## Required outcome

Every accepted codebase change must be an explicit transition between two
self-consistent Structrr states:

```text
accepted state S0
  + accepted intent diff DI
  + accepted architecture diff DA
  -> proposed state Sp
  -> implementation
  -> observed candidate state S1
  -> reconciliation and evidence
  -> accepted state S1
```

The proposal remains an immutable statement of intended change. The observed
implementation remains an independently derived statement of actual change.
Acceptance requires evidence that the actual change fulfills the proposal and
preserves all active intent not explicitly altered by the proposal.

## Design principles

### State is authoritative; conversation is provenance

The original message is retained exactly, but future behavior is driven by the
accepted Structrr state and versioned intent clauses. Resuming a run must not
require replaying or summarizing the conversation.

### Architecture cannot alter intent implicitly

Every proposal operation that changes an externally observable guarantee,
authority, constraint, lifecycle, failure behavior, or priority must cite an
explicit intent operation. If the proposal intends to preserve behavior, that
preservation must be reviewable against the potentially affected active intent.

### The proposal and actual state never rewrite each other

An implementation cannot retroactively change its proposal to make a drifted
result appear correct. A changed proposal is a new immutable revision that
passes proposal review again.

### Procedrr owns control flow

Models may produce candidate structures, classifications, implementation work,
and evidence claims. They do not choose which gates run, when a transition is
legal, or whether evidence is sufficient.

### Every model judgment is atomic

One Procedrr judge answers one question about one subject and returns one finite
decision. Aggregate responses such as `complete: true` are not authoritative
when they hide multiple independent decisions.

### Unknown blocks

A semantic decision that cannot be established is `unknown`, not an inferred
pass. Procedrr routes unknown outcomes to clarification, proposal amendment, or
explicit human resolution.

### Prompt targeting and runtime enforcement are complementary

OpenCode should receive the smallest complete intent packet for its operation.
Rules that can be enforced mechanically must also constrain capabilities,
paths, transitions, and evidence outside the prompt.

## Terms

### Accepted Structrr state

The current versioned model of active intent, architecture, relationships,
source subjects, source bindings, verification contracts, and accepted
exceptions. It has a canonical content fingerprint and is tied to a repository
tree.

### Intent source

The exact user-authored wording, source message identity, author, timestamp,
source span, and content fingerprint. It is immutable provenance.

### Intent clause

A typed, independently lifecycle-managed meaning derived from an intent source.
Clauses may represent decisions, invariants, procedures, guidance, goals,
constraints, non-goals, or acceptance requirements.

### Intent operation

One explicit operation in an intent diff:

- `add`
- `remove`
- `replace`
- `narrow`
- `expand`
- `clarify`
- `reaffirm`

`Clarify` may resolve ambiguity but may not change observable meaning.
`Reaffirm` creates no new state but records that an at-risk clause was
considered and retained.

### Architecture operation

One explicit addition, removal, replacement, or change to a Structrr entity,
relationship, module, tool, contract, source subject, source binding, or
verification mechanism.

### Proposal revision

An immutable state transition based on a specific Structrr state and repository
tree. A revision contains intent operations, architecture operations,
preconditions, expected resulting state, and acceptance requirements.

### Actualization manifest

Workrr's structured claim about how an implementation attempt realized proposal
operations. It helps reconciliation locate evidence but is never accepted as
proof by itself.

### Decision obligation

A durable requirement to evaluate one predicate for one subject against
fingerprinted inputs. It is satisfied only by an allowed result with valid,
current evidence.

## Ownership boundaries

### Structrr owns meaning and state

Structrr owns:

- canonical accepted snapshots;
- intent source and clause identity;
- proposal and actual state diffs;
- pure diff application;
- reference, lifecycle, and graph consistency;
- affected-intent closure;
- state and operation fingerprints;
- rebase comparison and conflict classification; and
- state acceptance through optimistic version checks.

Structrr does not schedule work or call a model.

### Procedrr owns required decisions and transitions

Procedrr owns:

- expansion of each gate into atomic decisions;
- stable decision ordering;
- deterministic evaluators and bounded Workrr judge calls;
- pass, fail, unknown, retry, amendment, and escalation routes;
- evidence requirements and invalidation consequences;
- implementation eligibility;
- post-implementation acceptance eligibility; and
- replay of the same open decisions after restart.

Procedrr does not invent intent or edit the repository.

### Workrr owns bounded work

Workrr owns:

- candidate intent extraction from one message;
- candidate semantic classifications requested by Procedrr;
- read-only implementation-surface discovery;
- compilation of a bounded OpenCode handoff;
- OpenCode process execution and permission policy;
- observed repository-state capture;
- validation command execution;
- actualization manifests; and
- independent semantic review when a deterministic predicate is impossible.

Workrr cannot accept intent, revise an accepted proposal, advance a Procedrr
transition, or accept the resulting Structrr state.

### OpenCode owns only the attempted edit

OpenCode receives one bounded work order and may inspect or mutate only through
the capabilities provided for that attempt. It cannot commit, push, accept a
proposal, update accepted Structrr state, or declare its own evidence
sufficient.

## Canonical data contracts

### Structrr snapshot

The snapshot extends the existing source-anchored Structrr representation with
active intent and verification relationships:

```yaml
schema: https://powdrr.io/schema/structrr-state-v1
state_id: structrr-state-0042
state_fingerprint: sha256:...
repository_tree: git:...
parent_state_fingerprint: sha256:...

intent_sources: []
intent_clauses: []
entities: []
entity_relationships: []
source_subjects: []
source_bindings: []
verification_contracts: []
accepted_exceptions: []
```

The content fingerprint excludes presentation ordering but includes every value
that changes semantic state.

### Chat update

Every user message receives an immutable source record, including messages that
produce no intent change:

```yaml
schema: https://powdrr.io/schema/chat-update-v1
update_id: chat-update-0184
source_ref: conversation:123/message:184
exact_text: "..."
content_fingerprint: sha256:...
base_intent_state: sha256:...
classification: intent_change | clarification | reaffirmation | context_only
```

The entire non-whitespace source must be partitioned into typed source spans.
This structural coverage check does not prove semantic accuracy, but it prevents
content from disappearing silently during extraction.

### Proposal revision

```yaml
schema: https://powdrr.io/schema/intent-transition-proposal-v1
proposal_id: proposal-0042
revision: 3
proposal_fingerprint: sha256:...

base:
  structrr_state: sha256:...
  repository_tree: git:...
  intent_version: 42

source_updates:
  - chat-update-0184

intent_operations:
  - operation_id: intent-op-1
    operation: add
    target_id: intent-clause-proposal-gate
    source_ref: conversation:123/message:184
    source_span: [0, 147]
    before_fingerprint: null
    after: {}

architecture_operations:
  - operation_id: architecture-op-1
    operation: add
    target_ref: component:proposal-gate-compiler
    before_fingerprint: null
    after: {}
    intent_effect: alters
    supporting_intent_operations:
      - intent-op-1

acceptance_requirements:
  - requirement_id: acceptance-1
    operation_refs: [intent-op-1, architecture-op-1]
    verifier: contract-test
    evidence_contract_ref: verification:proposal-gate

expected_state_fingerprint: sha256:...
```

The expected fingerprint is computed by Structrr after applying the diff. It is
not authored by a model.

### Decision specification

```yaml
schema: https://powdrr.io/schema/decision-obligation-v1
decision_id: proposal-0042:r3:architecture-op-1:intent-effect
gate: proposal_consistency
subject_ref: architecture-op-1
predicate: operation_has_explicit_intent_effect
evaluator: deterministic
input_fingerprint: sha256:...
allowed_results: [pass, fail]
required_evidence_types: [structrr-validation]
on_pass: satisfy
on_fail: open:declare-intent-effect
```

A semantic decision differs only in its evaluator and result set:

```yaml
predicate: changes_observable_guarantee
evaluator: workrr
allowed_results: [yes, no, unknown]
on_yes: require:supporting-intent-operation
on_no: require:preservation-disposition
on_unknown: open:human-clarification
```

### OpenCode work order

The existing Workrr `ImplementationRequest` becomes or embeds an immutable
intent packet:

```yaml
schema: opencode-work-order-v1
work_order_id: work-0123
packet_fingerprint: sha256:...

bases:
  proposal_revision: proposal-0042:r3
  proposal_fingerprint: sha256:...
  structrr_state: sha256:...
  repository_tree: git:...

objective:
  execution_unit_id: unit-proposal-gate
  proposal_operation_ids: [architecture-op-1]
  statement: Add deterministic proposal intent-effect validation.

required_changes: []
must_preserve: []
prohibited_changes: []
non_goals: []

scope:
  readable_paths: []
  writable_paths: []
  target_symbols: []
  permitted_capabilities: []
  allowed_commands: []

acceptance: []
evidence_requirements: []

output_contract:
  actualization_manifest_required: true
  changed_path_mapping_required: true
  assumptions_required: true
  scope_expansion_requests_required: true
```

Each included intent clause carries a machine-readable relevance path. Intent
without a relevance path cannot enter a targeted packet except for explicitly
global invariants.

### Actualization manifest

```yaml
schema: https://powdrr.io/schema/actualization-manifest-v1
work_order_id: work-0123
packet_fingerprint: sha256:...
attempt_id: attempt-1

operation_results:
  - proposal_operation_id: architecture-op-1
    status: implemented
    changed_paths: []
    changed_symbols: []
    evidence_candidates: []

preservation_claims: []
assumptions: []
scope_expansion_requests: []
```

This manifest is an untrusted worker claim. Procedrr derives the actual Git and
Structrr diffs independently.

## Single-decision normal form

A Procedrr decision is in single-decision normal form when it has:

1. exactly one subject;
2. exactly one predicate;
3. immutable, fingerprinted inputs;
4. a finite result set;
5. one evidence contract;
6. deterministic consequences for every result; and
7. no unrelated mutation.

The following are invalid judge outputs:

- arrays of findings;
- maps keyed by multiple requirements;
- a `complete` Boolean covering several acceptance criteria;
- a combined decision plus proposed repair;
- a decision that chooses its own next workflow step; or
- an answer whose schema permits arbitrary additional decisions.

Collections are handled by deterministic `for_each` or worklist expansion. One
item creates one decision obligation. Repairs are separate operations that
invalidate and rerun the affected decisions.

## Gate 1: proposal consistency

### Trigger

The gate runs after a candidate proposal revision has been constructed and
before that revision can be accepted or compiled into implementation work.

### Phase A: source and operation coverage

Procedrr creates one deterministic decision for each condition:

- Every source update exists and its fingerprint matches.
- Every non-whitespace source span has exactly one classification.
- Every intent operation cites a source span or an accepted prior clause.
- Every removed, replaced, narrowed, or expanded clause exists in the base.
- Every operation precondition fingerprint matches the base.
- Every new identifier is unique.
- Every replacement names exactly what it supersedes.
- Every subtraction states what guarantee ceases to apply.

### Phase B: hidden intent-change review

For each architecture operation, Procedrr runs separate semantic decisions:

- Does this operation change observable behavior?
- Does it change authority or ownership?
- Does it narrow or expand a guarantee?
- Does it change lifecycle or failure behavior?
- Does it invalidate an accepted architectural assumption?
- Does it change a verification promise?

Each `yes` result requires a linked intent operation. Each `no` result requires
a preservation disposition for the affected active intent closure. `Unknown`
blocks proposal acceptance.

No model decides which questions to ask. The gate compiler emits the complete
question family for every architecture operation.

### Phase C: affected-intent closure

Structrr computes closure from:

```text
proposal operation
  -> changed entity or relationship
  -> module, source subject, path, and symbol
  -> inbound and outbound contracts
  -> active intent clauses
  -> procedures and verification contracts
```

The closure always includes global invariants. A separate Workrr challenge may
nominate a missing relationship, but nomination creates a review obligation; it
does not silently change applicability.

Every affected active clause receives one disposition:

- explicitly fulfilled;
- preserved unchanged;
- intentionally modified;
- superseded;
- revoked;
- unaffected with an evidence-backed relationship explanation; or
- unresolved and blocking.

### Phase D: resulting-state validation

Structrr applies the complete proposal diff in memory and validates the
candidate state:

- identifiers and references are valid;
- supersession graphs are acyclic;
- no active clause is orphaned;
- retained clauses are mutually consistent;
- removed entities have no live references;
- every changed guarantee has explicit intent authority;
- every acceptance requirement has a verifier;
- every verification contract resolves; and
- the resulting state fingerprint is stable.

The proposal is accepted only when every decision obligation is passing and
fresh for the proposal fingerprint.

## Gate 2: implementation-start applicability

### Trigger

The gate runs immediately before compiling or invoking implementation work. It
runs again after any interruption that may have advanced the accepted Structrr
state or repository base.

### Base comparison

The first decision is deterministic:

```text
proposal.base.structrr_state == current_structrr_state
and
proposal.base.repository_tree is an ancestor of the implementation tree
```

If both hold, the accepted proposal remains applicable.

If the Structrr fingerprint changed, Structrr compares the proposal's referenced
entities, relationships, subjects, bindings, and clauses with the intervening
accepted diff. Existing Structrr rebase classification remains the foundation,
extended to intent clauses and verification contracts.

### Rebase outcomes

- `clean`: no referenced semantic state changed.
- `mechanically_rebased`: identities moved or were renamed with an unambiguous
  source-backed mapping.
- `context_refresh_required`: implementation context changed but the intended
  state transition did not.
- `targeted_update_required`: proposal mechanics must change while intent may
  remain stable.
- `conflicted`: a referenced target changed incompatibly.
- `invalidated`: the intended transition is no longer meaningful or legal.

Mechanical rebases produce a new proposal revision and rerun Gate 1. Semantic
conflicts cannot be repaired inside an implementation attempt. They require an
explicit intent amendment or proposal rejection.

### No silent proposal edits

Workrr may propose a revision artifact, but Procedrr must leave the
implementation phase, validate the new revision through Gate 1, and then re-run
Gate 2. A worker cannot alter a stale proposal and continue in one transition.

## Targeted intent compilation for OpenCode

### Why a targeted packet is mandatory

Providing all historical intent makes salience accidental and consumes context
needed for implementation. Providing only the latest request loses old but
still-active constraints. Procedrr therefore compiles an exact intent packet
for each execution unit.

### Static resolution

The packet compiler starts from accepted proposal operation IDs and expands
typed relationships through Structrr. It includes:

1. intent explicitly added, removed, or modified by those operations;
2. active intent governing targeted entities, contracts, paths, and symbols;
3. active intent governing changed inbound and outbound interfaces;
4. global invariants;
5. relevant negative constraints and non-goals;
6. required procedures and ordering constraints;
7. acceptance and evidence requirements; and
8. open obligations inherited from predecessor units.

Selection is graph closure, not top-k semantic retrieval. Embeddings or models
may nominate missing links, but they cannot authorize omission.

### Packet validation

Before OpenCode is invoked, Procedrr checks one predicate at a time:

- The proposal revision is accepted.
- The proposal and Structrr bases remain current.
- The work order has exactly one bounded objective.
- Every required proposal operation appears.
- Every affected active clause has a disposition.
- Every included non-global clause has a relevance path.
- No unresolved intent conflict exists.
- Writable scope is explicit.
- Every writable target is covered by impact analysis.
- Acceptance criteria are present.
- Every criterion has a verifier.
- The rendered prompt hashes to the packet's recorded prompt fingerprint.
- The packet fits the configured context budget.

If mandatory content does not fit, Procedrr splits the execution unit. It does
not trim governing intent.

### Read-only discovery when scope is uncertain

If exact paths or symbols are unknown, Procedrr runs two Workrr attempts:

1. A discovery request with read-only capabilities returns candidate paths,
   symbols, dependencies, and assumptions.
2. Structrr validates the discovered surface and computes the expanded intent
   closure.
3. Procedrr compiles and validates a new implementation packet.
4. OpenCode receives edit capabilities only in the implementation attempt.

### Dynamic applicability before mutation

Before a proposed mutation is authorized, Workrr resolves intent using the
target path, symbol, entity, action, and capability. It compares that closure
with the packet:

- no new intent: allow the mutation;
- new compatible intent: suspend and create a revised packet;
- conflict or scope expansion: stop and open a proposal-amendment obligation.

If the OpenCode integration cannot expose a reliable pre-mutation hook, Workrr
must keep attempts short and use read-only discovery followed by one bounded
implementation operation. Post-hoc detection remains mandatory but is not a
substitute for pre-mutation resolution where hooks are available.

### Prompt and enforcement boundary

The packet's prose improves first-pass correctness. Mechanically enforceable
parts remain runtime policy:

| Packet statement | Runtime enforcement |
| --- | --- |
| Writable paths | Reject an observed out-of-scope diff and restrict edit tools when possible |
| Allowed commands | OpenCode permission policy denies undeclared commands |
| No commits or pushes | Permission policy denies Git/GitHub writes and Workrr verifies `HEAD` |
| Current base | Workrr rejects a request whose base commit differs |
| Required validation | Procedrr blocks reconciliation without fresh evidence |
| No proposal mutation | Proposal and accepted-state paths are excluded from implementation scope |
| No acceptance authority | OpenCode receives no state-acceptance capability |

## Gate 3: post-implementation reconciliation

### Trigger

The gate runs after every implementation attempt and required validation, before
the candidate change can be committed as accepted Structrr state or published.

### Independently observed actual diff

Workrr records:

- Git paths and hunks;
- changed symbols when available;
- changed specification records;
- added, removed, and changed tests;
- the candidate Structrr snapshot and semantic diff;
- validation outputs; and
- OpenCode's untrusted actualization manifest.

The actual diff is derived from repository and Structrr state, not from the
worker's narrative.

### Fulfillment decisions

Procedrr creates one decision per accepted operation:

- Does the candidate state fulfill this intent addition?
- Does it reflect this explicit subtraction?
- Does it correctly realize this replacement, narrowing, or expansion?
- Is this architecture operation present in the actual diff?
- Does the associated evidence contract pass for the candidate tree?

Subtraction is not established merely because code disappeared. The resulting
state must remove the intended guarantee while retaining unrelated guarantees.

### Drift decisions

Procedrr creates one decision per actual change and one per affected retained
intent:

- Is this actual change explained by an accepted proposal operation?
- Is the explanation specific rather than a generic implementation-detail
  label?
- Does this retained intent remain satisfied?
- Did this change invalidate prior evidence?
- Has every invalidated requirement received fresh replacement evidence?

Unexplained actual changes are drift. Missing actual realizations are
incompleteness. Actual changes with the wrong semantics are mismatches.

### Candidate-state decisions

Structrr validates that:

- the actual diff applies to the current accepted state;
- resulting identifiers and relationships are coherent;
- all active intent clauses have valid dispositions;
- no accepted invariant lost its enforcement or evidence silently;
- the candidate snapshot is bound to the observed repository tree; and
- the candidate fingerprint is stable.

### Acceptance

Acceptance is one optimistic transaction:

```text
expected accepted Structrr fingerprint
+ accepted proposal revision
+ reconciled actual diff
+ complete fresh evidence
-> new accepted Structrr snapshot
```

A stale expected fingerprint rejects the transaction and reruns Gate 2. The
proposal, actual diff, decision results, and evidence remain immutable audit
records.

## Evidence and invalidation

Every decision result is bound to:

- decision ID;
- predicate version;
- subject fingerprint;
- proposal fingerprint;
- Structrr state fingerprint;
- repository tree or diff fingerprint;
- evaluator identity and version; and
- evidence artifact fingerprints.

Changing any dependency marks the result stale. Procedrr reopens the decision
obligation automatically. A model's prior answer cannot be reused against new
inputs because its input fingerprint no longer matches.

Deterministic evidence is preferred in this order:

1. schema and graph validation;
2. capability and path enforcement;
3. static analysis;
4. contract and behavioral tests;
5. repository-state inspection;
6. independent semantic review; and
7. human resolution for irreducible ambiguity.

Semantic review must cite concrete before/after subjects and relevant intent.
Unsupported prose is not evidence.

## Drift monitoring across many changes

Per-proposal gates catch local drift. Long-running systems also need cumulative
reconciliation.

Structrr maintains, for every active intent clause:

- governing entities and relationships;
- implementation source bindings;
- verification contracts;
- latest successful evidence fingerprint;
- last verified repository tree;
- evidence freshness policy; and
- unresolved risks or accepted exceptions.

Procedrr schedules a full reconciliation when:

- a global invariant changes;
- a source binding or verification mechanism is removed;
- an intent clause loses all architecture links;
- evidence exceeds its freshness policy;
- a migration changes identity semantics;
- a release or other configured milestone is reached; or
- a bounded number of accepted transitions has elapsed.

The full audit iterates every active clause in single-decision form. It is a
backstop for incomplete relationship graphs, not a replacement for targeted
per-change closure.

## Failure and recovery behavior

### Proposal failure

A failed proposal decision opens a targeted repair obligation. Repair produces
a new proposal revision and invalidates decisions whose inputs changed.

### Stale implementation start

The implementation is not launched. Procedrr records the rebase report and
routes to context refresh, targeted revision, or incompatibility reporting.

### OpenCode scope expansion

OpenCode or Workrr may report that additional changes are necessary. Workrr
records a scope-expansion request and stops. Procedrr routes it back through
proposal revision and Gate 1.

### Partial or out-of-scope mutation

Workrr classifies the attempt as policy denied, preserves the observed diff for
audit, and restores or cleans through the existing checkpoint policy. No
completion evidence is issued.

### Semantic uncertainty

`Unknown` creates a clarification obligation. Repeated model calls do not turn
uncertainty into permission.

### Restart

The event log reconstructs the accepted proposal reference, current gate,
decision worklist, open obligations, evidence freshness, and packet
fingerprints. The next action is chosen by Procedrr from durable state.

## Observability and explanation

For every OpenCode attempt and every accepted transition, Powdrr should answer:

- Which user updates contributed intent?
- What intent operations were accepted?
- What active prior intent was considered?
- Why was each intent clause included or omitted from the work order?
- Which exact prompt and capability policy did OpenCode receive?
- What actual change was observed?
- Which proposal operation explains each actual change?
- Which checks established fulfillment and preservation?
- Which evidence was invalidated and replaced?
- Who or what accepted the transition?

These explanations are generated from IDs, relationships, events, and evidence;
they are not reconstructed from model narration.

## Security and trust model

- User-authored source text is untrusted input but authoritative provenance.
- Model-derived clauses and relationships are candidate structure until
  accepted.
- Proposal documents are untrusted until Gate 1 passes.
- OpenCode is an untrusted worker.
- Actualization manifests are untrusted claims.
- Tool and repository observations are evidence only when bound to exact inputs.
- Procedrr definitions are trusted control policy and require normal code review.
- Structrr accepted-state writes require optimistic locking and a complete
  acceptance receipt.

## Non-goals

- Perfect determinism from model generation.
- Giving every model the complete historical transcript.
- Treating vector similarity as authority for intent applicability.
- Replacing source code and tests with architecture prose.
- Allowing a worker to resolve policy conflicts by choosing a convenient
  interpretation.
- Automatically weakening old intent because it is expensive to preserve.

## Acceptance criteria for this design

The design is implemented when the production feature flow demonstrates all of
the following:

1. Every chat update is stored and classified against an accepted intent state.
2. A hidden intent change in an architecture proposal blocks proposal
   acceptance until an explicit intent operation is added.
3. Applying an accepted proposal produces a deterministic, self-consistent
   candidate Structrr fingerprint.
4. A proposal whose Structrr base changed cannot start implementation without a
   fresh applicability decision.
5. A semantic proposal revision reruns proposal review.
6. Every OpenCode implementation attempt receives a targeted, fingerprinted
   intent packet with explainable inclusion paths.
7. Mandatory intent cannot be dropped to satisfy a prompt budget.
8. Newly discovered dynamic intent suspends mutation and rebuilds the packet.
9. Post-implementation reconciliation checks every intent operation, every
   expected architecture operation, every actual change, and every affected
   retained intent separately.
10. Unexplained actual changes and stale evidence block acceptance.
11. Restart and replay reconstruct the same open decision obligations.
12. Only Procedrr can authorize progression and only Structrr can install the
    next accepted state.

