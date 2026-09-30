# Structrr diff and invariant repair loop

## Purpose and outcome

Implement a closed loop that translates instructions into a proposed Structrr
change, asks mini-SWE-agent to implement it, independently observes the resulting
code, and repairs discrepancies. Review invariants in a separate step because a
requirement can be satisfied or violated without producing a recognizable
Structrr operation. Finish only when the final candidate satisfies the proposal,
passes targeted invariant review, and has fresh validation evidence.

This document is an implementation specification. It does not claim that the
complete loop already exists. Keep the runtime in the existing Procedrr flow and
reuse existing instruction, proposal, implementation, and verification contracts.
Do not implement a second orchestration framework.

## Starting points in this repository

Inspect these files before changing their contracts. Paths refer to the current
repository; verify which workflow definition is loaded at runtime before editing
any definition or generated/documented copy.

| Component | Existing implementation | Required extension |
| --- | --- | --- |
| Instruction capture | `src/powdrr_lift/core/instruction_ledger.py` | Classify each obligation's evidence requirements without losing its source span or exact wording. |
| Instruction-to-design conversion | `src/powdrr_lift/workrr/feature_endpoint.py`, including `_apply_sentence_design_trace` and `_task_structrr_changes` | Preserve the evidence classification through proposal operations, prompts, and review. |
| Snapshot collection | `src/powdrr_lift/structrr/bootstrap.py` | Collect comparable original and candidate snapshots, separating observations from declared intent. |
| Proposal and retained intent | `src/powdrr_lift/structrr/proposal.py`, `intent.py`, `active_intent.py`, `verification_obligations.py` | Bind obligations and invariants to the accepted proposal and retained baseline intent. |
| Worker handoff | `src/powdrr_lift/core/implementation_packet.py`, `src/powdrr_lift/workrr/coding_agent.py` | Render implementation, repair, and final validation requests from authoritative contracts. |
| Mini continuation | `src/powdrr_lift/minisweagent_session.py` | Verify that implementation, repair, and final validation preserve conversation history and diagnostics. |
| Existing review | `src/powdrr_lift/workrr/actualization.py` | Integrate structural discrepancies and separate invariant review receipts rather than replace them with one general judgment. |
| Verification | `src/powdrr_lift/workrr/verification_evidence.py`, `evidence_reconciliation.py`, `differential_verification.py` | Bind execution results to the same candidate and detect changed or weakened verifiers. |
| Orchestration and benchmark policy | `src/powdrr_lift/workrr/feature_endpoint.py`, `src/powdrr_lift/integrations/harbor/powdrr_agent.py`, the active `implement-feature` definition | Add the explicit feedback loop and preserve the benchmark's checkout/submission lifecycle. |

The current bootstrap uses `git ls-files` and treats specification documents as
authoritative semantic evidence. The current actualization reconciler combines
diff-bound judgments. Neither fact establishes that a proposed semantic change
has independently been recovered from altered source. Implement the observation
and comparison contracts below before claiming that guarantee.

## Obligation classification

Keep normative strength separate from evidence expectations. A mandatory behavior
is not necessarily a mandatory structural addition. A recommendation to use a
helper is not permission to omit a mandatory behavior.

Every processed obligation must carry:

- Its stable obligation ID, instruction clause ID, source span, and exact source
  text, plus a normalized statement for prompts.
- `normative_strength`: `must`, `should`, or `may`, supported by the instruction
  context. Explicit acceptance requirements are mandatory even without the word
  "must". Preserve negative and conditional requirements.
- `diff_expectation`: `required`, `expected`, `none`, or `unresolved`.
- `review_routes`: any combination of `structural_diff`, `behavior_validation`,
  `invariant_review`, and `process_receipt`.
- Proposed operation IDs, target entity identities, applicable conditions,
  protected subjects, evidence selectors, and a classification rationale.
- The proposal fingerprint and classification schema/version.

Split clauses containing multiple requirements into atomic obligations while
preserving their dependency/group relationships. An obligation can use multiple
review routes. Do not force a choice between a test and a structural observation.

| Example | Diff expectation | Required evidence |
| --- | --- | --- |
| Add a public `reset_state_data` method with a specified signature | Required | Observed symbol/signature operation plus behavioral tests. |
| Add regression tests for state isolation | Required | New or modified test entities, collected tests, and executed results. |
| Reject an invalid value in an existing method | Expected | Behavioral cases are mandatory; a method-body change may be observable without a new entity. |
| Preserve existing callback ordering | None | Targeted invariant review of relevant paths plus existing/added ordering tests. |
| Never perform network calls during a local operation | None | Source/call-path review and a suitable observable test where feasible. |
| Prefer an internal helper, with equivalent implementations permitted | Expected | Recognized operation or an evidence-supported equivalent implementation. |
| Commit the result when finished | None | Git/process receipt under the benchmark policy; no product Structrr operation. |

`required` means that a specific structural consequence is part of acceptance.
`expected` means that a change is plausible but another implementation or an
already satisfied requirement can be valid. `none` means there is no required
positive operation; absence in the diff cannot prove preservation. `unresolved`
blocks proposal acceptance until classification is resolved or a versioned policy
supplies an explicit, recorded default. Do not silently downgrade it to `none`.

Use model judgments for ambiguous classification, validated against the schema
and source coverage. Deterministically enforce exact APIs, explicit file changes,
operation references, and receipt completeness. Allow repository configuration to
select review tools, extractors, and advisory thresholds. Configuration must not
downgrade a mandatory instruction or waive a known invariant violation.

For a `should` obligation, an unmet recommendation needs a recorded justification
under an explicit acceptance policy. Until that policy exists, route it as an
unresolved discrepancy. Optional suggestions must not become mandatory product
work merely because a classifier selected them.

## Runtime sequence

1. Capture the starting Git revision, clean-state check, submission policy, and
   artifact exclusions. Bootstrap the original code into immutable snapshot `B0`.
2. Process instructions into a source ledger and classified obligations. Include
   retained baseline invariants that might be affected by the requested change.
3. Compile and validate proposed Structrr operations `P`, test/behavior contracts,
   and invariant review obligations. Review instruction coverage and freeze the
   accepted proposal fingerprint.
4. Render one cohesive implementation prompt from `P`, the obligations, relevant
   baseline context, and required tests. Include invariant constraints in the
   prompt even though their assessment happens in a separate review step.
5. Have mini implement code and tests and run focused checks in the task checkout.
6. Materialize candidate `C1`, preferably as an intermediate local commit under
   the verified policy below. Bootstrap its observed implementation into `B1`.
7. Compute actual Structrr operations `A1 = semantic_diff(B0, B1)`.
8. Compare `A1` with `P`, using each obligation's diff expectation. Produce a
   structured discrepancy report rather than a text/YAML diff of two documents.
9. Run the separate targeted invariant review for the same candidate. Collect
   behavioral validation evidence required by the classified obligations.
10. If either review or validation is unresolved or failing, compile a corrective
    prompt and have mini continue the same conversation. Checkpoint the next
    candidate and repeat steps 6–9 against the original `B0` and accepted `P`.
11. When the candidate passes these gates, have mini perform the final discovered
    validation suite in the same conversation. Persist exact execution receipts.
12. If final validation fails, return to repair. If it changes source or tests,
    repeat candidate extraction and both reviews. Materialize final `HEAD` and
    finish only when all evidence describes the submitted product tree.

Skip corrective work when no discrepancy exists. Record every attempt and its
reason. The orchestrator controls transitions and completion; a worker's textual
claim that it is done is not an acceptance receipt.

## Comparable snapshots and actual operations

Use the same extraction version, taxonomy, supported language capabilities, and
normalization rules for `B0` and every `Bi`. Capture file contents from their
declared revisions, not from a moving `HEAD`. Keep a source manifest containing
path/content digests, extraction coverage, errors, and provenance. A source parse
failure or unsupported property must surface as unknown coverage, never as proof
that an entity was removed or an invariant preserved.

Separate two kinds of snapshot facts:

- Observations derived from code, such as files, symbols, signatures, imports,
  relationships supported by extraction, and collected test identities.
- Declarations from specifications, such as intended behavior and invariants.

Declarations supply review obligations; copying a declaration into the candidate
is not evidence that its implementation is correct. Do not use the newly written
proposal or bootstrap artifacts as evidence for fulfilling their own operations.

Normalize ordering and volatile metadata. Use stable qualified identities for
symbols and relationships. Treat moves and renames explicitly when enough
evidence exists; uncertain matching remains unresolved. Ignore source span shifts
as semantic changes while retaining spans as provenance. Preserve meaningful
signature, type, ownership, visibility, and relationship changes. Exclude runtime
telemetry and generated planning artifacts from the product observation manifest.

Existing entities can change behavior without changing their signature. Record
changed source/content anchors for affected entities and route the behavioral
requirement to tests/review. Do not label a body hash difference as proof of the
requested behavior.

Actual operations use the same vocabulary and identity rules as proposed
operations. Validate original before-values, candidate after-values, and source
anchors. An empty actual structural diff is valid only when every applicable
obligation can be discharged through its other declared evidence routes.

## Comparing the diffs

The comparison report is a relation between intended operations, observed
operations, and obligations. It is not literal equality between operation lists.
Classify findings as:

| Finding | Meaning and action |
| --- | --- |
| Fulfilled | Required consequence is observed and supported by its evidence. |
| Already satisfied | The baseline already met the requirement; baseline and candidate evidence confirm it. |
| Equivalent implementation | An expected operation differs, but the mandatory contract is demonstrably met. |
| Missing | A required consequence or verifier is absent; repair is needed. |
| Contradictory | Observed change conflicts with accepted intent; repair is needed. |
| Unexpected | An operation lacks a proposal explanation; classify it as necessary implementation detail or unplanned semantic change. |
| Not evaluable | Extraction, identity matching, or evidence is inadequate; improve evidence or stop with an explicit unresolved result. |

Require precise evidence for equivalence and already-satisfied decisions. Scope
review must allow necessary helpers and test changes, while rejecting unrelated
semantic additions/removals. A failed comparison must not be fixed by modifying
the accepted proposal to describe whatever mini happened to do. Any necessary
proposal amendment requires a separate reviewed revision and invalidates receipts
bound to the earlier proposal.

Each finding contains a stable ID, severity, obligation/operation references,
expected and observed facts, candidate fingerprint, evidence references, and a
minimal corrective objective. Deterministic checks own facts that can be computed;
bounded semantic judgments handle equivalence or scope with explicit evidence.

## Separate invariant review

Add an explicit workflow operation and receipt, for example
`review_candidate_invariants`, after actual-diff comparison and before acceptance.
It receives the candidate's source manifest, affected code, relevant unchanged
context, applicable invariants, validation evidence, and the accepted proposal.
It produces no edits and cannot amend an invariant or the proposal.

An invariant record includes its origin (`instruction` or retained baseline
intent), normative strength, applicability, protected entities/behavior, impact
selectors, review method, and required evidence. Explicit preserved behavior and
negative requirements must not disappear merely because the relevant file was
unchanged. Determine indirect impact from supported dependency/call relationships;
when coverage is uncertain, broaden the targeted context or leave the decision
unresolved rather than assume no impact.

Review each applicable invariant independently. Use executable targeted checks
where available and source review for behavior that cannot be established by
structural extraction alone. Supply the baseline behavior and candidate behavior,
including relevant unchanged paths. Keep model review bounded to one invariant
and evidence packet at a time. Positive declarations and worker explanations are
not substitutes for implementation evidence.

The receipt contains exactly one outcome per required invariant: `pass`, `fail`,
or `unknown`, with explanation and source/test references. Bind it to the invariant
contract, accepted proposal, extraction version, candidate product digest, and
reviewer/tool version. Missing, duplicate, stale, or unknown mandatory outcomes
block completion. Advisory outcomes need the recorded acceptance-policy decision.

Changes after review invalidate affected receipts. Begin with conservative
invalidation of all candidate receipts; introduce selective reuse only when the
dependency and fingerprint rules are tested. Never reuse a review merely because
the file names remained the same.

## Corrective prompts and shared mini conversation

Compile repair prompts from the current discrepancy report, invariant failures,
and validation failures. Include exact failure evidence, affected paths/entities,
expected behavior, the current candidate identity, relevant retained constraints,
and focused checks to rerun. Preserve correct work. Avoid a general instruction
to reimplement the feature or a blind deletion of every unexpected helper.

One initial implementation invocation should remain the normal benchmark shape.
Repairs and final validation are subsequent requests in the same logical mini
conversation. The existing continuation runner restores trajectory messages
between subprocesses; it does not preserve a continuously running process. Verify
message restoration, model/environment configuration, handling of terminal exit
messages, partial trajectories after timeout, and resumed accounting. Record
aggregate token/cost/time usage at the run level rather than resetting the repair
budget with each subprocess. Missing or malformed history must produce a clear
failure/recovery policy, not silently start a new conversation.

Final validation must be an explicit request with no expectation of gratuitous
edits. Mini executes required commands through observable tools. Receipts must
contain command, working directory, exit status, output/artifact references, and
candidate identity. Audit changed/deleted/skipped tests and verifier fingerprints;
passing altered tests do not automatically prove preserved contracts.

Bound repair attempts and total time/cost through existing runtime policy. Detect
repeated findings with unchanged product digests and oscillating candidates. Stop
with an explicit incomplete result and preserve artifacts when the budget is
exhausted. Do not change proposal expectations to force completion.

## Benchmark Git policy and intermediate commits

Inspection on 2026-09-30 established the following for the local DeepSWE task
`python-statemachine-state-data-scoping`:

- Its `pre_artifacts.sh` captures
  `git diff --binary 8d17ba9f6ba8420cf05fddb94013bc221ed9a222 HEAD` into
  `/logs/artifacts/model.patch`.
- The installed Pier trial runner executes the optional pre-artifacts script
  after the agent finishes and before artifact collection.
- The task environment checks out the fixed base on a real branch, removes its
  remote/future history, and disables commit hooks. The verifier applies the
  captured patch to base file preimages in a separate environment.
- Powdrr's existing in-place entrypoint already commits locally and disables
  push/PR behavior. See `BENCHMARKS.md`, `docs/harbor/powdrr-agent.md`, and
  `run_feature_in_place` in `feature_endpoint.py`.

These facts support intermediate local commits for this submission policy:
multiple checkpoints still yield one cumulative base-to-final patch. This is a
source-level finding, not a completed live benchmark result or a universal rule
for all tasks. The checked local task files are under
`~/code/powdrr-deep-swe/tasks/python-statemachine-state-data-scoping/`; keep verifier
implementation, hidden tests, reference solutions, and grading artifacts outside
the agent's planning/review context.

Implement this policy:

1. Capture immutable `submission_base` from the task's starting revision before
   any bootstrap/proposal/checkpoint commit. Store it with the run manifest.
2. Use the task's provided checkout/branch. Do not fetch, push, create a nested
   worktree, or open a PR inside the benchmark. Git/process instructions belong
   to orchestration, not product implementation obligations.
3. Keep snapshots, proposals, reviews, and telemetry in the captured output root
   and Git-exclude them before implementation. Never commit them as product work
   unless the task explicitly requests those deliverables.
4. After implementation or repair, enumerate and validate intended product/test
   paths, including new files and deletions, then stage those explicit paths and
   create an intermediate local checkpoint commit when changes exist. Do not use
   the existing blanket `git add -A` helper without establishing exact artifact
   exclusions and scope. Skip empty commits and retain the existing candidate.
5. Generate the candidate bootstrap from the checkpoint revision. A local commit
   makes new files discoverable through the tracked-file inventory and provides
   an immutable candidate. Continue all comparisons against original `B0` and
   `submission_base`, never against only the most recent checkpoint's parent.
6. After final validation, commit any remaining intended source/test changes and
   repeat affected gates. Confirm `HEAD` contains the complete accepted product
   tree and there are no unstaged or untracked product changes. A clean status
   alone cannot establish correctness; exclusions must not hide deliverables.
7. Check the exact collector contract for each new benchmark adapter/task. If it
   captures only the last commit or rejects history changes, use its compatible
   policy instead. If intermediate commits are prohibited, collect candidate
   working-tree content with an explicit manifest that includes new/deleted files
   and preserve the submission base. Do not assume `git ls-files` sees new files.

Keep candidate product digests separate from commit identities. A commit changing
only metadata must not invalidate otherwise identical product evidence; a product
edit must. Persist both, and verify that the final submitted patch includes all
checkpoints and excludes runtime artifacts. Powdrr development itself continues
to use dedicated feature worktrees and PRs under repository instructions.

## Artifacts and completion contract

Persist a versioned run manifest linking these artifacts and their fingerprints:

| Artifact | Required content |
| --- | --- |
| Original snapshot | `B0`, submission base, extractor/taxonomy versions, source manifest. |
| Classified obligations | Source ledger references, normative strength, evidence routes, applicability, rationale. |
| Accepted proposal | `P`, operation identities, behavior/test contracts, invariants, acceptance receipt. |
| Per-candidate snapshot | `Bi`, commit/revision, product digest, extraction coverage and source manifest. |
| Actual diff | `Ai`, original/candidate snapshot references, operations and provenance. |
| Comparison report | Fulfilled and unresolved operations/obligations with evidence. |
| Invariant receipt | Exactly one fresh outcome for each required applicable invariant. |
| Worker/validation receipts | Prompt and trajectory references, aggregate budgets, executed checks and candidate identities. |
| Completion receipt | Final `HEAD`, submission base, cumulative patch/product digest, all accepted gate receipts. |

The completion predicate requires a current accepted proposal, complete instruction
coverage, no blocking structural discrepancy, all mandatory applicable invariants
passing, all required behavioral/final validation evidence accepted, no unreviewed
verifier weakening, and a submitted tree matching these receipts. Failures in
extraction, review infrastructure, or validation setup are distinct from product
failures and must remain visible. If no runnable validation is discoverable, record
that limitation and apply an explicit benchmark policy rather than invent a pass.

## Implementation work packages

Execute these in dependency order, each as a scoped feature PR. Rebase onto the
current implementation; preserve existing run artifact/schema compatibility or
provide explicit versioned migration. Do not introduce unrelated refactors.

1. **Obligation evidence contract.** Add validated classification fields and
   compile invariant obligations. Preserve source coverage and conditional groups.
   Extend proposal validation and prompt projection. Acceptance: the examples
   above route correctly, and unresolved/mandatory obligations cannot vanish.
2. **Snapshot and checkpoint policy.** Separate observed/declarative facts, add
   immutable source manifests, and implement benchmark-aware explicit checkpoints.
   Acceptance: additions, deletions, and body changes are observed; the collector
   includes cumulative product changes and excludes planning artifacts.
3. **Actual diff and discrepancy comparison.** Implement deterministic operation
   extraction/matching and bounded semantic decisions for equivalence/scope.
   Acceptance: a missing required API blocks, a valid helper is explainable, and
   an unsupported behavior remains unknown rather than passing.
4. **Invariant review operation.** Add targeted evidence packets and a separate
   fail-closed receipt. Acceptance: an unchanged API with violated ordering fails
   invariant review despite passing structural comparison; stale receipts fail.
5. **Procedrr loop and mini final validation.** Wire reports into focused repairs,
   persist the logical conversation, enforce budgets, rerun both reviews after
   edits, and finalize the submitted tree. Acceptance: a repair can resolve one
   finding without dropping retained obligations, and final-validation edits
   cannot bypass re-review.
6. **Integrated benchmark acceptance.** Run a fresh Powdrr/Harbor/Pier task using
   the pushed exact source revision and the configured coding agent. Inspect the
   captured cumulative patch, run receipts, invariant outcomes, and external
   verifier result. Follow the `clean-deepswe-run` skill for a real run; do not
   manually repair the checkout or inject a prompt during it.

## Verification scenarios for the implementing agent

Use small deterministic fixtures for contract/loop checks, plus a meaningful mini
continuation integration fixture. Cover these cases before live benchmark work:

- Required API addition appears, required addition is absent, and an already
  satisfied baseline requirement is correctly recognized.
- A body-only behavioral change requires test evidence even when symbol inventory
  stays identical; a copied specification cannot discharge it.
- An unchanged signature masks an invariant violation; negative and conditional
  invariants receive the correct targeted review.
- New test files, deleted entities, renames, extraction errors, and unsupported
  language facts produce complete observations or explicit unknowns.
- Necessary helper changes pass scope review, while unrelated behavior changes
  and weakened/deleted verifiers block acceptance.
- Repair fixes one discrepancy but introduces another; the next iteration catches
  it against the original baseline. Repeated/no-progress repairs exhaust cleanly.
- Partial mini history survives a timeout according to policy; resumed requests
  receive earlier conversation messages and cumulative budget accounting.
- Final validation edits invalidate review receipts; missing/stale/duplicate
  evidence cannot complete the run.
- Two intermediate commits contribute to the final base-to-HEAD patch. Untracked
  product files block completion, and runtime snapshots never enter the patch.
- A benchmark that prohibits intermediate commits takes the explicit working-tree
  observation path without losing new files or changing submission expectations.

For implementation PRs, run the repository's full format, lint, type, test, workflow
definition, and deterministic workflow scenario checks. For live acceptance,
preserve fresh captured artifacts and report Powdrr gate results separately from
the benchmark verifier score. A successful source-level fixture or an agent's
completion message is not a successful benchmark result.
