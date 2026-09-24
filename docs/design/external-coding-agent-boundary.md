# Single-Prompt mini-SWE-agent Boundary

## Purpose

After Powdrr completes and validates a feature design, it invokes
mini-SWE-agent once with one immutable implementation prompt. mini-SWE-agent is
an untrusted repository editor. It does not own intent interpretation, design,
workflow state, validation policy, completion, or publication.

The governing boundary is:

```text
resolved and validated design revision
  -> deterministic handoff compiler
       |-> one worker-facing prompt -> one mini-SWE-agent invocation -> diff -+
       |                                                                  |
       `-> one private obligation-validation-manifest-v1 -----------------+
                                                                          |
                                      manifest-driven Workrr validation <-+
  -> accepted | failed
```

There is no obligation-per-worker loop, coding-provider selection, automatic
continuation, or validation-derived repair prompt in this target design.

## Design handoff artifacts

The design phase emits exactly one worker-facing
`minisweagent-implementation-prompt-v1` containing:

- immutable prompt identity and fingerprint;
- exact design and repository revisions;
- the complete rendered prompt;
- references to every represented semantic contract and verification case;
- allowed durable and ephemeral paths;
- focused command contracts; and
- compiler and rendering revisions.

Only the rendered prompt is supplied as mini-SWE-agent's `--task` input. The
metadata remains with Workrr for provenance and validation.

The prompt is not the only persisted design artifact. The same canonical
design must also emit one private `obligation-validation-manifest-v1`. It maps
every actionable obligation to its verification cases, planned or existing
targets, scenario, operation, oracle, baseline expectation, required evidence,
durable-test requirement, relevant subject scope, and preservation checks.
mini-SWE-agent never receives the manifest. Workrr retains it as the immutable
specification for post-coding validation.

Prompt and manifest compilation must be atomic. They share one design
revision, base commit, contract set, verification-case set, and cross-artifact
fingerprints. The handoff cannot start implementation unless every prompt
obligation has exactly one manifest entry and every manifest obligation is
rendered in the prompt.

The prompt contains exactly these sections:

1. `Objective`
2. `Repository starting point`
3. `Required behavior`
4. `Required verification`
5. `Preserve and avoid`
6. `Allowed scope`
7. `Focused commands`
8. `Completion protocol`

Every actionable contract appears once. The prompt excludes classifier
outputs, proposal operations, Structrr diffs and baselines, internal IDs that
cannot guide implementation, model reasoning, artifact paths, and historical
conversation.

## Single invocation

Workrr starts one mini-SWE-agent process in the dedicated implementation
worktree:

```text
mini --task <exact compiled prompt> --yolo --exit-immediately ...
```

mini-SWE-agent may perform its normal internal sequence of model turns, file
inspection, edits, and focused test commands. Those turns do not create new
Powdrr prompts. The prompt fingerprint remains constant for the entire run.

Workrr must not:

- start one mini-SWE-agent process per obligation, test, file, or code task;
- send follow-up instructions into the same process;
- resume after timeout or step exhaustion with another model prompt;
- compile a repair prompt from failed validation;
- invoke OpenCode when mini-SWE-agent fails; or
- treat a provider exit as proof of success.

If prompt compilation exceeds a configured bound, the design phase reports a
prompt-compilation failure. It may deterministically remove duplicated views or
irrelevant repository facts, but it may not split the feature into multiple
worker prompts or omit required intent.

## Execution preconditions

Before invocation, Workrr proves:

- the prompt completeness receipt passed;
- every prompt input revision and fingerprint is current;
- worktree `HEAD` equals the prompt's base commit;
- the candidate worktree has the expected baseline state;
- all generated run artifacts are outside the candidate checkout;
- allowed paths and commands are syntactically and semantically valid;
- the configured coding provider is mini-SWE-agent; and
- no earlier implementation invocation exists for this run identity.

Failure of any precondition prevents model invocation.

## Observation and attempt artifact

Workrr persists one `minisweagent-implementation-attempt-v1` containing:

- prompt ID and fingerprint;
- resolved mini-SWE-agent package, model, and configuration revisions;
- exact starting commit and diff fingerprint;
- wall, model-call, command, and token budgets when available;
- trajectory reference and terminal mini-SWE-agent status;
- before/after durable repository state;
- attributable diff and changed paths;
- out-of-scope and generated-path findings; and
- Workrr terminal classification.

mini-SWE-agent's submission or zero process exit means only “return control to
Workrr.” Workrr independently classifies:

- `completed_with_progress`;
- `submitted_without_progress`;
- `limit_exceeded`;
- `policy_denied`;
- `provider_failed`;
- `cancelled`; or
- `invalid_attempt`.

Only `completed_with_progress` advances to validation.

## Validation

Workrr validates the observed diff without asking another model to edit it:

1. Verify attempt-local durable progress.
2. Reject changed paths outside the prompt scope.
3. Reject generated Powdrr/Structrr/Procedrr run artifacts in the candidate
   patch.
4. Load the frozen validation manifest and prove its design, prompt, and base
   fingerprints still match the implementation attempt.
5. For every obligation, bind each planned target to a collected test or other
   concrete validator. Missing, ambiguous, skipped, xfailed, deselected, or
   weakened targets fail.
6. Run every focused verification case against the candidate and retain fresh
   per-case evidence.
7. Run every adapter-materializable Workrr-owned probe outside the candidate
   checkout. mini-SWE-agent cannot inspect or alter these probes.
8. For newly requested behavior, run the candidate test source against the
   pre-implementation product baseline in an isolated shadow worktree. Require
   candidate pass plus an allowed baseline failure unless the manifest records
   a reviewed alternative evidence strategy.
9. Check that each test's setup, operation, and assertions implement the
   precompiled scenario and oracle. Use deterministic adapters where possible;
   otherwise ask one bounded read-only judge question for that mapping.
10. Select relevant candidate diff hunks for each obligation from its bound
   subject and changed-path closure.
11. Evaluate every obligation independently from its immutable source,
    contract, relevant diff, test source, candidate result, baseline result,
    and oracle-alignment evidence. The bounded judge returns only `pass`,
    `fail`, or `abstain` plus an explanation; Workrr binds identity and evidence.
12. Persist one `obligation-validation-receipt-v1` per obligation. Every receipt
    must pass; results are never averaged.
13. Run preservation and non-goal checks.
14. Run repository-required formatting, lint, type, and full test profiles.
15. Reconcile observed source impact with the accepted design.
16. Sanitize and fingerprint the final candidate patch.

Every actionable behavioral obligation must have an executable test mapping by
default. A typed design-time exemption may use static analysis, artifact
inspection, or human observation only when it names replacement evidence and
passes source-faithfulness review. A passing test alone is insufficient: the
test must be collected, execute successfully on the candidate, target the
planned operation and oracle, and discriminate the new behavior from the
baseline when applicable.

The manifest separately says whether each durable repository test must be
added, modified, or may be accepted as existing proven coverage. A new feature
cannot satisfy its regression-test requirement by attaching an unrelated
pre-existing passing selector. When an adapter can derive an independent probe
from the case specification, that probe is mandatory in addition to the
durable repository test.

Any failure terminates the implementation run as failed. The evidence is
durable and can inform a later user-approved or workflow-approved design
revision. That later revision receives a new run identity and compiles a new
single prompt; it is not a repair turn in the failed run.

## Permissions

mini-SWE-agent runs only inside the dedicated worktree. Its environment:

- permits repository reads and edits within the compiled path policy;
- permits only focused commands matching compiled command contracts;
- denies commits, pushes, GitHub publication, external worktrees, and run
  artifact mutation;
- receives only required provider credentials;
- has bounded wall and model-call budgets; and
- records a trajectory outside the candidate checkout.

Workrr still derives scope and progress from Git and validation evidence. Agent
permissions are defense in depth, not acceptance evidence.

## Failure and retry semantics

There is no automatic coding retry. Provider errors, incomplete trajectories,
limits, invalid diffs, and failed validation are terminal for the run.

Retrying means creating a new implementation run from a current accepted
design and repository revision. If failure evidence changes the design or its
repository bindings, the design must be recompiled and revalidated before a
new prompt can exist. Reusing the prior prompt is legal only when all inputs
and fingerprints are identical and an operator explicitly starts a new run.

## Required tests

The boundary requires deterministic tests proving:

- one design revision renders one stable prompt;
- all contracts and verification cases are represented exactly once in the
  prompt and private validation manifest;
- prompt and manifest references have exact parity and share the same accepted
  design revision;
- unresolved design fields prevent prompt creation;
- an obligation without an executable case or reviewed typed exemption
  prevents implementation;
- durable-test `add`, `modify`, and `existing_proven` dispositions are enforced
  against the candidate test diff and exact mapped assertion;
- only one provider invocation occurs;
- mini-SWE-agent receives the exact persisted prompt bytes;
- a timeout, limit exit, no-op, or failed validation causes no continuation or
  repair invocation;
- OpenCode is never selected by the target feature flow;
- out-of-scope and generated paths fail validation; and
- a newly written tautological test that also passes against the product
  baseline cannot satisfy a new-behavior obligation;
- adapter-materializable cases run through independent probes unavailable to
  the coding worker;
- missing, skipped, xfailed, deselected, ambiguous, or oracle-misaligned tests
  fail their obligation receipts; and
- a successful run requires an attributable diff plus a passing fresh receipt
  for every obligation.

An opt-in live test must execute the complete design-to-prompt-to-mini-SWE-agent
path on a small real repository and assert the invocation count, prompt
fingerprint, product diff, and validation evidence.
