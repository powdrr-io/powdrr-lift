# Evidence-backed contract closure for implementation prompts

Status: proposed

## Goal

Improve the instructions-to-prompt path so a coding worker receives the full
behavioral contract implied by the user's instructions and the starting
repository. Keep the result general across tasks, auditable, and usable without
knowing the benchmark solution. Compile one coherent implementation prompt and
retain independent validation evidence.

This plan extends the current typed behavior-scenario path. It does not replace
the instruction ledger, `normative_defaults`, scenario consistency review, or
the single worker-facing prompt described in
`docs/design/source-anchored-semantic-contract-compilation.md`.

## Evidence and diagnosis

The 2026-09-28 real Mini-SWE run of the DeepSWE incremental-delivery task
produced 14/17 passing feature tests and 810/811 passing existing tests. The
generated prompt included 20 behavior scenarios and their related requirements.
The remaining failures reveal three different omissions:

| Observation from verifier | What the prompt conveyed | Missing contract work |
| --- | --- | --- |
| Two calls to `.defer(label=...)` failed | `.defer()` on both named DSL surfaces; optional arguments were explicit for `.stream()` | Examine the corresponding operation's established protocol and local API conventions before settling an omitted signature. A label on defer is an **inference to evaluate**, not an instruction clause to invent. |
| Errors in deferred fragments were absent | A broad rule that errors must not halt later items | Trace where nested producers emit errors, how consumers expose them, and how continuation works after an error. |
| An unsupported transport no longer raised | New incremental behavior across transports | Preserve the base or unsupported implementation's pre-existing failure contract while enabling only supported implementations. |

The run also encountered environment and dependency validation problems during
agent attempts. Those are separate from these three prompt coverage findings;
this proposal does not attribute every failed check to prompt generation.

The current handoff validates `BehaviorScenario` in
`src/powdrr_lift/core/behavior_contract.py`, renders it with
`render_behavior_matrix`, and places that text in
`ImplementationPacket.render()` in
`src/powdrr_lift/core/implementation_packet.py`. The flow builds a canonical
design in `src/powdrr_lift/workrr/command_catalog.py`, constructs the code task
in `_compile_code_task_plan`, and calls `_run_code_agent_phase` in
`src/powdrr_lift/workrr/feature_endpoint.py`. The code task's `must_preserve`
and `non_goals` fields are currently initialized empty. The renderer asks the
worker to trace affected paths, but does not hand it a checked, evidence-backed
map of those paths and their existing contracts.

## Target behavior

For every actionable design revision, compile a **contract closure record**
before the worker prompt. It answers four questions for each changed behavior:

1. Which public operations and repository implementations are affected?
2. What explicit behavior and accepted defaults must each surface provide?
3. Which existing behaviors, including rejection and error behavior, must
   survive?
4. Which observable cases distinguish a complete implementation from a
   partial or regressive one?

The record is a design artifact, not another worker invocation or a collection
of test names. The renderer presents the resolved requirements in compact
prose grouped by operation. Powdrr keeps the provenance, coverage map, and
validation obligations in machine-readable artifacts.

## Inputs and authority

Use only information available before implementation:

- The exact user instruction, atomic clauses, and accepted semantic contracts.
- Files tracked in the task's captured base commit, selected by the relevant
  language adapter. The current Python adapter reads only tracked Python
  files; it does not scan the mutable working tree and refuses capture when
  tracked files differ from the captured commit.
- Versioned repository or ecosystem specifications if explicitly supplied by
  the task or pinned in the repository. An external standard may be consulted
  only when the environment permits it; record its identity and version. No
  uncited model recollection counts as standard evidence.
- Accepted human clarifications and previously recorded repository intent.

Solution patches, verifier reports, hidden tests supplied outside the captured
base commit, validation output, and edits from any implementation attempt are
never inputs to closure or prompt construction. The inventory is read from the
Git tree at the captured base commit, so files added or changed by an agent or
benchmark run cannot enter it. Verifier reports and solution material may be
used only after the prompt is frozen, to evaluate whether closure succeeded.
This keeps benchmark measurement independent of prompt construction.

Authority order for a particular decision is: explicit instruction; accepted
human decision; applicable repository contract at the base commit; pinned
standard; documented ecosystem convention; conservative default. A lower
level cannot contradict a higher one. Post-run validation artifacts do not
participate in this authority decision.

With `clarification_policy=ask`, unresolved material choices produce the
existing clarification path. With `normative_defaults`, the compiler chooses
the best-supported compatible option, records its basis and uncertainty, and
continues. If no option has enough evidence, it records a narrow conservative
assumption and a discriminating check. A source omission alone does not
justify adding arbitrary API parameters or behavior.

## Closure artifact

Add `contract-closure-v1`, bound to the design revision, base commit,
repository inventory fingerprint, and compiler revision. Conceptually:

```yaml
schema_version: contract-closure-v1
design_revision: sha256:...
base_commit: git:...
inventory_fingerprint: sha256:...
operations:
  - operation_ref: contract:instruction-007
    source_clause_refs: [instruction-007]
    surfaces:
      - surface_ref: inventory:...
        role: public_api # or base, sync, async, adapter, caller, callee
        existing_contract_refs: [evidence:...]
        required_behavior_refs: [scenario:...]
        preservation_refs: [preservation:...]
    decisions:
      - decision_ref: decision:...
        question: <one bounded semantic question>
        outcome: <resolved behavior or signature>
        basis: repository_convention
        evidence_refs: [evidence:...]
        confidence: supported # or conservative_assumption
    flows:
      - flow_ref: flow:...
        producer_ref: inventory:...
        consumer_ref: inventory:...
        payload: <result, error, state, or cancellation signal>
        observable_outcome_ref: case:...
    cases:
      - case_ref: case:...
        scenario_refs: [scenario:...]
        surface_refs: [inventory:...]
        role: feature # or preservation, boundary, interaction
        assertion: <observable result>
```

IDs and references are compiler-owned. Model decisions may fill bounded
semantic values and explain them, but cannot create paths, symbols, IDs,
selectors, commands, or evidence citations. Validate references against the
frozen inventory and source spans. Store exact cited ranges and fingerprints
so a later commit cannot silently change the basis of a decision.

The artifact must distinguish `required_by_source`, `derived_from_repository`,
`derived_from_standard`, and `normative_assumption`. The prompt may phrase
them naturally, but the private record must retain that distinction.

## Compilation stages

### 1. Discover affected surfaces

Start with source-named operations and subjects. Reuse the existing
`RepositoryInventory` and subject lookup in
`src/powdrr_lift/core/repository_inventory.py`. Extend it, or add a companion
inventory, to capture operation signatures and relationships that the current
symbol records do not carry: declaration/override, caller/callee, wrapper,
sync/async counterpart, base/default implementation, protocol adapter, and
nearby tests. Language adapters supply facts; a bounded semantic decision may
judge whether a discovered surface participates in the requested behavior.

Use a bounded graph walk from source-bound symbols. Expansion edges require a
recorded reason and a depth/size budget. Report truncated exploration as
incomplete; do not equate missing inventory data with an unsupported surface.
If a source names multiple variants, all named variants require a terminal
surface disposition: change, preserve, irrelevant with evidence, or unresolved.

### 2. Capture existing contracts and omissions

For each relevant surface, extract current signatures, rejection behavior, and
documented guarantees from files tracked at the base commit. Do not read
solution patches or post-run validation artifacts. Compare the source facts to
the new behavior scenarios. Ask narrow semantic questions only when evidence does not
deterministically establish the relationship. Examples of question shapes:

- Does this base method reject the new operation unless an adapter overrides
  it?
- Does this parameter belong to the user-facing operation according to a
  cited local sibling or pinned protocol definition?
- Is the named error returned, raised, or lost at this boundary?

Do not ask a model to invent an entire API map or enumerate unseen tests.
The decision output selects from compiler-supplied evidence and candidates.
When an inferred signature is accepted, record the exact source of the
inference, the operation it applies to, and a testable consequence. Prefer
local API conventions over a generic industry guess when both apply.

### 3. Trace behavior through boundaries

Turn each cross-cutting scenario into a small flow graph. Track the producer,
transformers, consumer, and observable result for values, errors, continuation,
cancellation, and cleanup when relevant. Require an explicit terminal outcome
for each named error or negative path: surfaced, transformed with authority,
rejected, or not applicable with evidence. A generic continuation statement
does not close an error flow unless the error remains observable and subsequent
work has a separate acceptance case.

This stage is conditional on the source and discovered architecture. It does
not require every feature to have transport, async, or cancellation rows.

### 4. Build coverage and preservation cases

Produce a coverage map from each scenario and accepted derived decision to
its applicable surfaces and conditions. Include positive behavior,
representative boundary/error behavior, cross-surface interactions, and
preservation of pre-existing rejection behavior. Each case has a concrete
operation and observable assertion. A single test can cover multiple rows only
if its assertions distinguish them independently.

The private validation manifest records required observable checks, but it is
compiled from accepted source contracts and base-commit evidence, not from
verifier output. Do not generate a test selector from prose as proof that a
test exists. The worker prompt states behavior without exposing hidden
verifier facts.

### 5. Validate and render

Before prompt emission, reject a closure record if it has unresolved source
refs, stale repository evidence, an unaccounted named surface, an accepted
assumption without basis, a flow with no terminal observation, or a coverage
row with no verification route. Compare the resolved closure requirements to
the existing scenario consistency review, source conservation ledger, and
private validation manifest. A preservation contract cannot silently override
an explicit feature change; a feature case cannot silently erase a supported
preservation contract.

Render one prompt section per operation with: requested behavior, applicable
surfaces, important signatures/defaults, error and continuation behavior,
existing behavior to preserve, and focused checks. Deduplicate repeated prose
while retaining source clause and case references in a separate projection map.
The worker sees concise natural language and may inspect the repository before
editing. The prompt artifact remains independently capturable without Pier.

## Integration with the current flow

1. After `compile_canonical_feature_design` and scenario consistency review,
   compile the repository evidence snapshot and closure record for the same
   design revision. Introduce a Procedrr operation and readiness gate before
   `compile_code_task_plan` in
   `docs/procedrr/skill-definitions/implement-feature.yaml` (or place the
   operation at the equivalent design handoff if that flow is refactored).
2. Add typed schema and validation in a new core module, tentatively
   `src/powdrr_lift/core/contract_closure.py`. Keep repository discovery and
   bounded question preparation in Workrr, using language adapters and the
   existing semantic inventory where possible.
3. Pass accepted closure refs into the canonical design, code task, and
   `ImplementationPacket`. Populate `must_preserve` and `non_goals` from
   evidence-backed contracts where applicable. Extend
   `render_behavior_matrix` or the packet renderer to include closure
   requirements once, alongside the existing scenarios.
4. Save `repository-evidence.json`, `contract-closure.json`, and a prompt
   projection map next to the existing canonical design, normative assumptions,
   implementation packet, prompt capture, and validation artifacts. Bind all
   to the same base commit and design fingerprint.
5. Keep the accepted `ask` versus `normative_defaults` routing. Any new
   default enters the same assumptions audit and is visible in the prompt.
   Existing decision answers must not be silently rewritten by closure.

The design document calls for exactly one worker-facing prompt and a private
validation manifest. The first implementation should operate on this same
boundary. Changes to agent retries, repair behavior, package resolution, or
benchmark infrastructure are separate work.

## Verification strategy

Create fixtures from several unrelated base-commit trees: an API with variants,
a nested error/continuation flow, an explicitly unsupported base operation,
and a feature with no special boundary behavior. These exercise general
rules, not task-name branches. Unit tests should cover evidence binding,
authority precedence, conflict detection, stale fingerprints, bounded graph
walks, and prompt/manifest parity. A flow test should prove that the closure
record reaches the one worker prompt and that the old `ask` behavior remains.

For benchmark evaluation, freeze the prompt and closure artifacts before
looking at verifier outputs. Compare the existing generator and the new one on
the GraphQL and state-machine tasks, then on at least one held-out DeepSWE task.
Use the same base commit, instruction, policy, model, and post-freeze scoring
procedure for each comparison. Do not provide validation setup or outcomes to
the prompt generator. Prompt-only evaluation comes first; full Mini-SWE runs follow
only when prompt coverage and provenance pass. Report separately:

- explicit source coverage and unsupported inferred requirements;
- repository surface and preservation coverage;
- error/continuation flow coverage;
- prompt-to-manifest parity and prompt size;
- feature-test pass rate, existing-test regressions, and run failures.

Verifier tests and solution material may label misses **after** each prompt is
frozen. They must never become generator rules or pre-implementation inputs.
The held-out task is necessary to detect overfitting to either prior task.

## Delivery sequence

Prefer two substantial implementation PRs after this design document:

1. Build the frozen evidence inventory, closure schema, bounded surface and
   flow analysis, and readiness gate. Include meaningful unit and flow tests.
   Exit when every accepted closure row has current evidence, named surfaces
   have dispositions, and the artifact can be produced without running Pier.
2. Wire closure into the single prompt and private validation manifest,
   implement projection/parity checks, and evaluate captured prompts across
   the pilot and held-out tasks. Include a real Mini-SWE comparison with all
   run artifacts and separate infrastructure failures from behavior misses.
   Exit when the prompt contains all accepted contracts, no ungrounded
   task-specific requirements, and the held-out evaluation shows no loss of
   explicit source coverage or existing behavior.

Before either PR is pushed, run the repository's full tests, `ruff format
--check`, lint, and type checks. A benchmark result is reported as a measured
outcome, not as an automatic pass criterion that could encourage tailoring to
the verifier.
