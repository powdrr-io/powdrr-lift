# Intent-Linked Verification Implementation Plan

Status: proposed

## Purpose

This plan turns durable product intent into executable, content-addressed verification. It is written as a sequence of independently reviewable pull requests that another agent can implement without making architectural choices that belong in this plan.

The target behavior is:

1. Structrr records the active intent of the repository and the verification contracts that protect it.
2. Procedrr computes which contracts a proposed feature can affect and requires fresh evidence for them.
3. Workrr discovers and executes repository-native verification tools and returns typed, per-contract evidence.
4. OpenCode implements the feature, but cannot define or certify the evidence that proves its own work correct.
5. Procedrr permits PR creation only when all applicable contracts have current, acceptable evidence or an explicit, reviewed disposition.

The resulting system supports forward-only development: a change can add or deliberately supersede intent, but it cannot silently discard an existing invariant or weaken its verifier.

## Starting Point

The implementation must build on, rather than replace, these facilities already on `main`:

- `docs/procedrr/skill-definitions/implement-feature.yaml` decomposes feature text sentence by sentence, compiles feature obligations, reviews the proposal before implementation, validates after implementation, repairs worker findings, and performs a content-addressed post-implementation intent review.
- `src/powdrr_lift/workrr/feature_endpoint.py` prepares proposal and post-implementation evidence fingerprints.
- `src/powdrr_lift/structrr/gate_compiler.py` supports evidence requirements and content fingerprints.
- `src/powdrr_lift/structrr/intent.py` stores intent captured through existing runtime paths.
- Structrr bootstrap discovers repository validation commands.
- `required_test_cases` is already part of the specification and changelog model.
- `src/powdrr_lift/workrr/coding_agent_validation.py` executes validation profiles and records command-level results.

The current gaps are:

- A normal feature worktree can have no populated `guidance/intents.json`, making an active-intent review vacuously pass.
- `required_test_cases` describes cases, but does not identify an executable selector, provider, protected intent, or evidence policy.
- Validation evidence is command-level, so Procedrr cannot prove which required test ran or distinguish passed, failed, skipped, deselected, or absent.
- Evidence identity is content-addressed, but its semantics are not strong enough to prove a durable intent remains protected.
- The implementation agent can change tests and implementation in the same diff without a deterministic control against weakening the verifier.

## Architectural Decisions

These decisions apply to every phase.

### Ownership

| Component | Owns | Must not own |
| --- | --- | --- |
| Structrr | Canonical active-intent view, verification contracts, stable IDs, relationships, applicability metadata, fingerprints | Running tests or deciding that implementation passed |
| Procedrr | Compiling affected obligations, sequencing execution and repair, enforcing evidence gates, recording dispositions | Framework-specific test discovery or self-authored proof from OpenCode |
| Workrr | Provider adapters, test inventory, execution, normalized evidence | Product-intent interpretation or waiver policy |
| OpenCode | Implementation changes requested by the work order | Final acceptance, evidence policy, or unreviewed verifier weakening |

### Canonical sources

- Do not create a second intent database. The active-intent resolver must compose the accepted Structrr baseline, feature objectives/invariants, and existing `IntentStore` records into one canonical view.
- Extend `required_test_cases`; do not introduce a disconnected list of intent tests.
- Store provider-neutral contracts in Structrr. Store provider-specific execution details behind Workrr adapters.
- Generated evidence is an artifact, never accepted state.

### Stable identity

Logical identity and executable location are separate:

- `contract_id` is durable across file moves and selector changes.
- `intent_refs` identify the clauses protected by the contract.
- `provider` and `selector` identify how Workrr locates the executable verifier today.
- A selector change updates the contract version and invalidates old evidence, but does not create a new logical contract unless the protected behavior changes.

### Mandatory invariants

1. An expected non-empty intent inventory cannot resolve to an empty review set without a blocking diagnostic.
2. Every new or materially altered objective or invariant must have at least one enforceable verification contract before implementation begins.
3. `passed` is the only successful execution status. `skipped`, `xfailed`, `deselected`, `not_collected`, `errored`, `timed_out`, and `missing` are not passes.
4. Evidence is valid only for the exact candidate tree, contract version, verifier source, command configuration, protected-input fingerprint, and provider version that produced it.
5. Changing or deleting a required verifier invalidates its evidence and triggers a separate verifier-change review.
6. OpenCode output cannot directly mark a verification obligation satisfied.
7. Deterministic missing or failing evidence cannot be overridden by a model-generated semantic judgment.
8. Provider behavior and unwanted-file detection must be discovered from repository state and contracts, not hardcoded filenames or framework assumptions in the core flow.
9. Unknown applicability blocks enforcement unless an explicit, policy-allowed disposition is recorded.
10. PR creation is downstream of the final evidence gate.

## Domain Model

Implement the following provider-neutral records. Use the repository's existing model library and serialization conventions.

### Active intent reference

```yaml
intent_id: intent.feature.transcript.persist-across-runs
kind: invariant
source:
  type: specification
  path: docs/specifications/transcript.yaml
  pointer: /invariants/0
statement: A completed Procedrr run retains its transcript.
status: active
supersedes: []
fingerprint: sha256:...
```

The resolver must reject duplicate IDs with different content. Superseded and retired clauses remain addressable for history but are not included in the active set.

### Verification contract

Extend each `required_test_cases` entry so it can represent:

```yaml
id: verify.transcript.persist-across-runs
description: A transcript remains available after the run process exits.
intent_refs:
  - intent.feature.transcript.persist-across-runs
provider: pytest
selector: tests/e2e/test_transcript.py::test_transcript_survives_process_exit
profile: e2e
expectation: pass
applicability:
  mode: affected_closure
protected_inputs:
  - powdrr_lift/transcript/**
  - docs/procedrr/skill-definitions/**
status: active
```

Required fields are `id`, `description`, `intent_refs`, `provider`, `selector`, `profile`, `expectation`, and `status`. Existing legacy entries without the new fields remain parseable during migration and are reported as incomplete contracts.

Supported initial expectations are only `pass` and `absent`. `absent` is for repository-policy checks such as forbidden generated artifacts and must be implemented through a provider, not a hardcoded list in Procedrr.

### Test inventory entry

```yaml
provider: pytest
selector: tests/e2e/test_transcript.py::test_transcript_survives_process_exit
source_path: tests/e2e/test_transcript.py
markers: [e2e]
profiles: [e2e]
fingerprint: sha256:...
```

### Verification obligation

An obligation is a content-addressed compilation of a contract for one feature run. It includes:

- contract ID and contract fingerprint;
- protected intent IDs and fingerprints;
- applicability result and explanation;
- base and candidate tree identities;
- provider, selector, profile, and expected result;
- verifier source fingerprint;
- protected-input fingerprint;
- evidence policy and freshness requirement.

### Verification evidence

```yaml
obligation_id: sha256:...
contract_id: verify.transcript.persist-across-runs
provider: pytest
selector: tests/e2e/test_transcript.py::test_transcript_survives_process_exit
status: passed
started_at: 2026-09-19T20:00:00Z
duration_ms: 824
candidate_tree: sha256:...
contract_fingerprint: sha256:...
verifier_fingerprint: sha256:...
protected_inputs_fingerprint: sha256:...
command_fingerprint: sha256:...
provider_version: pytest-8.x/adapter-v1
artifact_refs:
  - .powdrr/evidence/.../result.json
```

Evidence status is a closed enum: `passed`, `failed`, `skipped`, `xfailed`, `deselected`, `not_collected`, `errored`, `timed_out`, and `missing`.

### Disposition

A disposition is required for any contract not satisfied by fresh passing evidence. Initial allowed dispositions are:

- `superseded`: the protected intent was explicitly superseded in accepted state;
- `not_applicable`: deterministic applicability proves the contract is outside the affected closure;
- `waived`: a human-authorized exception with an expiry and reason.

OpenCode and model judges may propose a disposition but cannot authorize one. Until the repository has a trusted human-approval mechanism, `waived` must block automated PR creation.

## End-to-End Control Flow

The completed feature flow must use this ordering:

```text
ensure current Structrr
  -> resolve canonical active intent
  -> discover validation profiles and provider inventory
  -> decompose feature request and trace every sentence
  -> update accepted intent proposal
  -> compile affected intent closure
  -> compile verification obligations
  -> proposal and verifier-change gates
  -> invoke OpenCode with obligations in its work order
  -> execute repository validation and selected contracts
  -> normalize per-contract evidence
  -> deterministic evidence reconciliation
  -> repair all current implementation/evidence issues
  -> re-run affected validation and contracts
  -> reconcile only remaining issues
  -> semantic review only for residual claims not deterministically provable
  -> planning completeness and scope review
  -> final evidence freshness gate
  -> changelog and PR
```

Every repair round must attempt all current issues, then revalidate, then derive the next repair set only from the new results. Never append already-resolved issues merely because they were present in an earlier round.

## Implementation Sequence

Each phase below is one pull request. Do not combine phases unless a phase cannot compile independently. Every PR must include model/schema tests, flow-definition tests, migration coverage, documentation, a changelog, and the repository's complete validation suite.

The file lists below are expected touchpoints, not permission to edit every file listed. The implementing agent must confirm call sites on current `main` and keep each diff to the smallest coherent change.

### Phase 1: Canonical active-intent resolution

Goal: eliminate vacuous intent reviews and expose one deterministic active-intent inventory to all later phases.

Implementation:

1. Add a provider-neutral active-intent model under `powdrr_lift/structrr/`.
2. Add a resolver that reads the accepted Structrr baseline/specification, feature objective and invariant clauses, and existing `IntentStore` records.
3. Normalize each source to stable clause IDs, source pointers, status, supersession links, and fingerprints.
4. Resolve duplicates deterministically. Identical duplicate content may coalesce; conflicting content for the same ID is a blocking error.
5. Persist the resolved inventory as a generated Structrr artifact with a schema/section version. Regeneration must be handled by `ensure_current_structrr` when the section is absent, stale, or has the wrong version.
6. Replace direct `IntentStore(...).list()` use in proposal and post-implementation review preparation with the canonical resolver.
7. Add an explicit diagnostic for the case where accepted feature/specification content implies intent exists but resolution returns zero active clauses.

Expected touchpoints:

- `src/powdrr_lift/structrr/intent.py` for the shared clause representation or compatibility adapters;
- a new `src/powdrr_lift/structrr/active_intent.py` for resolution;
- `src/powdrr_lift/structrr/bootstrap.py` for the versioned generated section;
- `src/powdrr_lift/workrr/feature_endpoint.py` for both review packet builders;
- `tests/test_intent.py`, `tests/test_structrr_bootstrap.py`, and `tests/test_feature_endpoint.py`.

Tests:

- baseline-only repository;
- IntentStore-only repository;
- merged sources with identical clauses;
- conflicting duplicate IDs;
- active, superseded, and retired clauses;
- stale/missing Structrr section regeneration;
- proposal and post-implementation review receive the same ordered active set;
- non-empty accepted intent cannot produce an empty review packet.

Exit gate: both review paths consume the canonical inventory, and an empty inventory can only pass when the repository genuinely has no active intent.

### Phase 2: Verification contracts in `required_test_cases`

Goal: make the existing required-test concept executable and explicitly tied to intent.

Implementation:

1. Add the verification-contract model and closed enums described above.
2. Extend specification, changelog, parser, index, and template models for the new fields. Preserve legacy parsing.
3. Validate unique contract IDs, valid intent references, known providers, non-empty selectors, legal expectations, and active/superseded status transitions.
4. Add contract fingerprints that include semantic fields but exclude generated timestamps and evidence.
5. Add graph edges `intent -> protected_by -> contract` to Structrr's relationship representation.
6. Add an `incomplete` migration classification for legacy `required_test_cases` entries.
7. Update proposal/changelog rendering so contract additions, edits, deletions, and supersessions are visible as first-class changes.

Expected touchpoints:

- a new `src/powdrr_lift/core/verification_contract.py` for provider-neutral models;
- `src/powdrr_lift/core/pr_specification.py` and `src/powdrr_lift/core/feature_planning_specification.py` for specification parsing;
- `src/powdrr_lift/change_log_parser/parse_change_log.py`, `src/powdrr_lift/change_log_template.py`, and `src/powdrr_lift/change_log_validation.py` for changelog support;
- `src/powdrr_lift/core/code_index.py` for relationship indexing;
- the corresponding specification, parser, changelog, and index tests under `tests/`.

Tests:

- round-trip every new field;
- parse existing repositories unchanged;
- reject dangling intent references and duplicate IDs;
- changing selector preserves ID but changes fingerprint;
- changing description only has the documented fingerprint behavior;
- deleting an active contract without supersession is rejected;
- graph traversal returns all contracts protecting an intent.

Exit gate: every complete contract is resolvable from an active intent to an executable provider/selector pair, while legacy entries remain readable and are reported for migration.

### Phase 3: Workrr provider protocol and pytest inventory

Goal: discover executable verifiers without embedding framework logic in Procedrr or Structrr.

Implementation:

1. Define a Workrr verification-provider protocol with `detect`, `inventory`, `execute`, and `normalize` operations.
2. Define typed request/result envelopes; provider failures must be data returned to Procedrr, not unstructured exceptions where avoidable.
3. Implement the first adapter for pytest. Use a small pytest plugin or pytest-supported machine-readable hook to collect exact node IDs and outcomes; do not parse decorative console output.
4. Connect provider detection to the validation discovery already performed by Structrr bootstrap. Detection must use repository configuration and discovered commands, not only filenames.
5. Version provider inventory as a Structrr bootstrap section. `ensure_current_structrr` must regenerate it when its version or protected inputs change.
6. Match contracts to inventory using exact provider/selector identity. Return `not_collected` for an unmatched selector.
7. Keep the provider registry extensible; core code must not branch on `pytest` outside registration/bootstrap wiring.

Expected touchpoints:

- a new `src/powdrr_lift/workrr/verification_provider.py` protocol and registry;
- a new `src/powdrr_lift/workrr/providers/pytest.py` adapter;
- `src/powdrr_lift/structrr/validation.py` and `src/powdrr_lift/structrr/bootstrap.py` for detection and versioned inventory;
- new provider conformance tests plus `tests/test_validation_discovery.py` and `tests/test_structrr_bootstrap.py`.

Tests:

- repository with and without pytest;
- parametrized node IDs;
- duplicate human-readable test names in different files;
- markers/profiles;
- collection errors;
- moved or deleted selector;
- no tests collected;
- provider version/fingerprint changes invalidate inventory;
- a fake provider proves the core protocol is framework-neutral.

Exit gate: Workrr can produce a stable inventory containing the exact selector for each pytest contract and report absent selectors without invoking OpenCode.

### Phase 4: Compile affected verification obligations before implementation

Goal: decide what proof is required before OpenCode changes the repository.

Implementation:

1. Add an affected-intent closure compiler using the feature's traced clauses, proposal operations, Structrr relationships, protected inputs, and changed/anticipated entities.
2. Compile all contracts protecting the closure into verification obligations.
3. Add proposal decisions for:
   - every new or altered objective/invariant has a complete contract;
   - every selected contract exists in provider inventory;
   - every contract has deterministic applicability;
   - every contract edit/deletion is explicitly represented in the proposal.
4. Include obligations and their fingerprints in the pre-implementation gate packet.
5. Pass a concise obligation summary to OpenCode as part of its work order: protected intent, selector, expectation, and repair-relevant failure details. Do not ask OpenCode to judge whether evidence is acceptable.
6. Insert the compilation and gate steps in `implement-feature.yaml` after feature-obligation compilation and before the current proposal acceptance/OpenCode invocation.

Expected touchpoints:

- a new `src/powdrr_lift/structrr/verification_obligations.py` compiler;
- `src/powdrr_lift/structrr/gate_compiler.py` for evidence decisions;
- `src/powdrr_lift/workrr/feature_endpoint.py` for flow actions and work-order assembly;
- `src/powdrr_lift/workrr/coding_agent.py` only if its typed work-order envelope must carry obligations;
- `docs/procedrr/skill-definitions/implement-feature.yaml` and its flow-definition tests.

Tests:

- direct protected-input impact;
- transitive relationship impact;
- unrelated contract excluded with a recorded explanation;
- new invariant without contract blocks;
- missing selector blocks;
- contract change appears as a proposal decision;
- obligation fingerprints are stable for identical inputs and change for any protected input.

Exit gate: no implementation begins until Procedrr has a complete, content-addressed list of the evidence it will require afterward.

### Phase 5: Typed, per-contract execution evidence

Goal: prove that each required verifier actually ran and record its exact outcome.

Implementation:

1. Preserve existing command-level validation results for diagnostics, but add normalized per-contract evidence.
2. Execute obligations through Workrr provider adapters using the repository's discovered validation environment and profiles.
3. Record all evidence identity fields from the domain model, including stdout/stderr artifact references without embedding unbounded output in flow state.
4. Record skipped, xfailed, deselected, missing, collection-error, timeout, and process-error states distinctly.
5. Ensure progress events are emitted during collection and execution so OpenCode/Workrr monitoring treats test activity as progress.
6. Store evidence beneath the run artifact directory. Never write evidence into accepted Structrr state or treat the artifact path alone as identity.
7. Add evidence summaries to the existing validation result consumed by repair logic.

Expected touchpoints:

- a new `src/powdrr_lift/workrr/verification_evidence.py` for evidence models and artifact persistence;
- the provider protocol and pytest adapter from Phase 3;
- `src/powdrr_lift/workrr/coding_agent_validation.py` for orchestration alongside existing profile results;
- `src/powdrr_lift/workrr/feature_endpoint.py` for progress and state plumbing;
- validation, provider, monitor-progress, and artifact tests.

Tests:

- pass, fail, skip, xfail, deselect, no collection, collection error, timeout, and process crash;
- two contracts executed by one process receive distinct results;
- stale result files cannot be reused;
- candidate-tree or verifier-source change invalidates evidence;
- output truncation retains complete structured status and artifact reference;
- execution emits monitor progress events.

Exit gate: Procedrr can answer, for every obligation, whether the exact required verifier ran against the exact candidate and what happened.

### Phase 6: Deterministic reconciliation and repair loop

Goal: make evidence acceptance mechanical and make repairs converge on only remaining failures.

Implementation:

1. Add a deterministic reconciler that maps each obligation to one fresh evidence record or authorized disposition.
2. Treat any identity mismatch as stale evidence and any non-`passed` status as unsatisfied for `expectation: pass`.
3. Produce typed repair issues grouped by implementation failure, missing verifier, stale evidence, verifier execution error, and unauthorized disposition.
4. Feed all current issues into one OpenCode repair session/continuation where possible. Do not resend stable feature context already retained by that session; send only changed evidence and current issues.
5. After each repair attempt, rerun all impacted validation/contracts, rebuild evidence, and reconcile from scratch.
6. The next loop iteration must contain only currently unsatisfied issues. Bound attempts using the existing configurable repair policy, not a new hardcoded count.
7. Run model-based post-implementation intent review only for semantic claims that deterministic contracts cannot establish. A judge cannot turn failed deterministic evidence into acceptance.
8. Insert the reconciliation gate after validation and before final planning/scope review and PR creation.

Expected touchpoints:

- a new `src/powdrr_lift/workrr/evidence_reconciliation.py`;
- `src/powdrr_lift/workrr/feature_endpoint.py` for issue collection and loop state;
- `docs/procedrr/skill-definitions/implement-feature.yaml` for ordering and gate conditions;
- `tests/test_feature_endpoint.py` plus flow and end-to-end repair fixtures.

Tests:

- multiple failures repaired in one round;
- one remaining failure is the only issue in round two;
- resolved issue does not reappear from accumulated history;
- stale evidence produces a rerun rather than a semantic judgment;
- repair session retains original work context;
- deterministic failure cannot be overridden by `preserved` model output;
- exhausted repair attempts return the complete final issue set.

Exit gate: repair behavior is evidence-driven, convergent, and incapable of declaring deterministic failures resolved without fresh execution.

### Phase 7: Differential validation and verifier-change controls

Goal: distinguish pre-existing failures from regressions and prevent self-weakening tests.

Implementation:

1. Run selected contracts against both the merge base and candidate in isolated worktrees/environments when the contract or affected validation profile is not known clean on the base.
2. Classify results as `preserved_pass`, `fixed_existing_failure`, `new_regression`, `persistent_failure`, and `not_comparable`.
3. Never accept `persistent_failure` merely because it predates the feature when the contract protects an active intent; require repair or an authorized disposition.
4. Detect verifier changes by comparing contract definitions, selector inventory, and verifier source fingerprints between base and candidate.
5. Require a verifier-change decision describing why the change strengthens, relocates, replaces, or legitimately supersedes protection.
6. When verifier and implementation change together, run the base verifier against the candidate where technically compatible. If incompatible, require explicit `not_comparable` evidence and semantic review.
7. Reject deletion, deselection, broad skip/xfail, assertion removal, or expectation weakening when no accepted intent transition authorizes it.

Expected touchpoints:

- the obligation compiler for base/candidate identities;
- provider execution for isolated base and candidate requests;
- a new `src/powdrr_lift/workrr/differential_verification.py` for classification;
- `src/powdrr_lift/workrr/feature_endpoint.py` and `src/powdrr_lift/structrr/gate_compiler.py` for verifier-change decisions;
- differential and verifier-mutation integration tests.

Tests:

- clean base to failing candidate;
- failing base to passing candidate;
- same failure on both sides;
- selector move with equivalent test;
- deleted test;
- new unconditional skip/xfail;
- weaker assertion detected through verifier fingerprint/change review;
- incompatible base verifier produces blocking `not_comparable` rather than a false pass.

Exit gate: a candidate cannot improve its apparent status by weakening or removing the evidence that protects existing intent.

### Phase 8: Freshness, health audits, and migration enforcement

Goal: make the system sustainable after the initial feature run and migrate existing repositories safely.

Implementation:

1. Add an invalidation graph from intent, contract, provider inventory, verifier source, protected inputs, validation configuration, and candidate tree to evidence.
2. Add a repository health command/report that finds:
   - active intents with no contract;
   - incomplete legacy contracts;
   - contracts with no inventory match;
   - orphan contracts;
   - stale evidence;
   - unauthorized verifier weakening;
   - expired waivers.
3. Add three policy modes:
   - `observe`: report all findings and block only structurally invalid data;
   - `required_for_new`: enforce contracts for new or materially altered objective/invariant clauses and observe legacy debt;
   - `enforce`: require complete coverage for every active intent in the affected closure.
4. Default existing repositories to `required_for_new`. New repositories may default to `enforce` once bootstrap can generate a complete starter inventory.
5. Add a migration command that proposes stable IDs and candidate selectors for legacy `required_test_cases`; it must not invent intent links silently.
6. Include health deltas and newly introduced debt in proposal and PR evidence. A feature may not increase uncovered active intent in `required_for_new` or `enforce` mode.
7. Document how a human accepts supersession or a time-limited waiver.

Expected touchpoints:

- a new `src/powdrr_lift/structrr/verification_health.py`;
- the Structrr CLI registration in `src/powdrr_lift/cli.py`;
- bootstrap policy/schema and `ensure_current_structrr` integration;
- proposal/PR evidence assembly in `src/powdrr_lift/workrr/feature_endpoint.py`;
- health, invalidation, policy-mode, and migration tests.

Tests:

- every health finding category;
- evidence invalidation for each input edge;
- mode-specific behavior;
- legacy repository migration without data loss;
- new feature cannot add uncovered intent;
- unchanged legacy debt is reported but does not block in `required_for_new`;
- coverage/debt cannot regress;
- expired waiver blocks.

Exit gate: the repository can measure and prevent verification debt from increasing, while existing projects can adopt the system incrementally.

## First Supported Vertical Slice

Phases 1 through 6 must ship before claiming intent-linked verification is operational. The first supported slice is deliberately narrow:

- pytest is the only production provider;
- `expectation: pass` is required; `absent` may ship only if its provider is complete;
- enforcement applies to new or materially altered objectives and invariants;
- existing incomplete `required_test_cases` are visible migration debt;
- semantic model review remains for intent that cannot yet be expressed as deterministic verification;
- human waivers block unattended PR creation until a trusted approval channel exists.

Do not broaden to additional test frameworks until the fake-provider conformance tests and pytest adapter prove that no framework-specific behavior leaked into the core.

## Acceptance Scenario

After Phase 6, add one end-to-end fixture exercising this complete story:

1. A repository has an active invariant stating that a Procedrr transcript survives process exit.
2. A pytest contract protects that invariant.
3. A feature request changes transcript persistence code.
4. Structrr resolves the invariant and contract.
5. Procedrr includes the contract in the affected closure before invoking OpenCode.
6. OpenCode initially introduces a regression.
7. Workrr runs the exact selector and emits `failed` evidence.
8. Procedrr creates one typed repair issue and continues the same OpenCode session.
9. OpenCode repairs the implementation without changing the verifier.
10. Workrr reruns the selector and emits fresh `passed` evidence.
11. Reconciliation removes the issue, final evidence fingerprints match the candidate, and PR creation becomes eligible.

Add companion variants proving that deleting the test, skipping it, changing its assertion to a weaker form, or reusing evidence from the prior candidate all remain blocked.

## Verification Required for Every Phase

Before opening or updating each PR:

1. Regenerate any affected Structrr bootstrap sections and generated schema fixtures.
2. Run focused unit/integration tests for the changed phase.
3. Run flow-definition and schema validation.
4. Run the repository's complete pytest suite.
5. Run `ruff format --check` through the repository's configured environment.
6. Run the repository's configured lint command.
7. Run the repository's configured type checker.
8. Run `git diff --check`.
9. If the phase changes the feature flow, run the Powdrr end-to-end feature fixture and retain its evidence summary in the PR.

Use validation commands discovered by Structrr/bootstrap or repository configuration. The command names above describe required categories, not permission to hardcode a parallel validation stack.

## Agent Handoff Checklist

For each phase, the implementing agent must:

- create a fresh worktree and feature branch from current `origin/main`;
- read this plan and the current implementations named by the phase before editing;
- confirm earlier phase exit gates still hold on current `main`;
- keep provider-neutral types free of pytest imports or conditionals;
- update section/schema versions whenever persisted structure changes;
- include backward-compatible parsing and explicit migration findings;
- add tests for every failure state, not only the happy path;
- update `implement-feature.yaml` only at the ordering point specified here;
- ensure repair loops revalidate before collecting the next issue set;
- run the full repository validation suite;
- add the PR changelog and evidence summary;
- open a PR and stop for human review without merging it.

If current code makes a phase impossible without changing one of the architectural decisions, stop and propose an amendment to this plan. Do not silently create a second source of truth, hardcode a framework/file list into Procedrr, allow OpenCode to self-certify, or weaken an exit gate.

## Completion Definition

This plan is complete when all eight phases are merged and the acceptance scenario demonstrates that:

- every affected active intent is visible;
- every enforceable intent has a stable verification contract;
- every applicable contract produces fresh typed evidence;
- regression and verifier weakening are blocked;
- repairs operate on only current failures;
- evidence becomes stale whenever any protected input changes;
- legacy debt cannot grow; and
- PR creation occurs only after the final reconciled evidence gate passes.
