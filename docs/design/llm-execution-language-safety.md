# Safety and Liveness for the LLM Execution Language

## Purpose

Powdrr Lift should make broad classes of LLM execution failures impossible by
construction. A validated skill or workflow should carry explicit, useful
guarantees about what an LLM can do, what effects execution can produce, how
each step ends, and whether the workflow can become stuck or repeat forever.

The design goal is similar to a safe systems language: move correctness from
instructions and conventions into the language, compiler, and runtime. The LLM
may choose among typed possibilities, but it is never the authority for its own
permissions, for whether an effect occurred, or for whether completion is true.

The governing principle is:

> The LLM proposes; the execution kernel disposes.

This document defines the intended guarantees and authoring model. It does not
describe one particular implementation or migration sequence.

## What validation means

Validation must report a set of proven guarantees, not merely say that a file
is syntactically valid. A workflow can be capability-safe while its termination
remains unknown, or termination-safe while it still contains an overly broad
filesystem capability. These properties must remain distinguishable.

The compiler should therefore produce a safety certificate with an explicit
status for each guarantee:

```yaml
guarantees:
  schema_safe: proven
  action_safe: proven
  capability_safe: proven
  effect_safe: proven
  scope_safe: proven
  control_flow_safe: proven
  completion_safe: proven
  retry_safe: proven
  progress_safe: proven
  termination_safe: proven
  replay_safe: proven
```

`unknown` is different from `disproven`. Unknown means that the compiler lacks
enough information to prove the property. Disproven means that a reachable
counterexample exists. Neither is equivalent to proven.

## Safety guarantees

The language should provide the following guarantees.

| Guarantee | A successful proof makes this impossible |
| --- | --- |
| Schema safety | Malformed steps, actions, outcomes, outputs, guards, or transitions |
| Action safety | The LLM selecting an action outside the active step's closed action set |
| Capability safety | Invoking an undeclared tool or semantic operation |
| Argument safety | Passing values outside the operation's argument schema |
| Effect safety | An operation producing effects outside its declared effect summary |
| Scope safety | Reading, editing, staging, deleting, or publishing resources outside the authorized scope |
| Ownership safety | Making the LLM select or repeat a fixed mechanical operation owned by the runner |
| Control-flow safety | Entering an unreachable step or jumping to an undeclared target |
| Outcome safety | Finishing a step activation without exactly one recognized outcome |
| Handoff safety | Consuming a missing, stale, or incorrectly typed upstream value |
| Completion safety | Reporting success without required outputs, effects, obligations, and fresh evidence |
| Retry safety | Retrying through a path that cannot affect the failed condition |
| Progress safety | Repeating an action or cycle without changing material state |
| Termination safety | Following a legal path forever without reaching a terminal or suspended state |
| Resource safety | Exceeding declared limits for attempts, time, tokens, tool calls, or external cost |
| Replay safety | Re-execution duplicating a non-idempotent effect |
| Provenance safety | Treating model prose or an untrusted summary as authoritative evidence |
| Composition safety | A nested skill silently adding effects or weakening guarantees |
| Audit safety | Producing an effect that cannot be attributed to a definition, execution, and decision |

These guarantees are runtime properties as well as compiler properties. Static
validation is meaningful only when the execution kernel enforces the same
contract that the compiler analyzed.

## The execution model

A workflow is a typed state-transition system. Its authoritative parts are:

- a finite set of steps;
- typed inputs and outputs;
- a closed set of actions for each LLM boundary;
- runner-owned operations and their effect contracts;
- a closed set of outcomes for each step;
- transitions associated with those outcomes;
- completion and evidence requirements;
- finite resources or decreasing measures for cycles; and
- terminal states, including successful, failed, blocked, cancelled, and
  suspended states.

Natural-language details explain the judgment to make. They do not grant
authority, define a hidden transition, establish evidence, or override a
structural contract.

### Actions and outcomes are different

An action is an attempted operation within a step, such as reading a document,
editing a file, or invoking a tool. An outcome ends one activation of the step
and determines control flow.

A step may perform several actions before reaching one outcome. Conversely, a
tool invocation may lead to different outcomes depending on its structured
result. Conflating actions with outcomes makes it easy to repeat a successful
action while never advancing.

Every step should declare a closed outcome union:

```yaml
outcomes:
  completed:
    requires: [repair_output]
    goto: verify-repair

  no_change_needed:
    requires_evidence: [current_validation_passed]
    goto: complete

  retryable_failure:
    produces: [diagnosis]
    consumes: retry_budget
    goto: repair

  blocked:
    terminal: blocked
    produces: [blocking_reason]
```

The compiler should require that:

- exactly one outcome ends each activation;
- every outcome payload has a schema;
- every outcome transitions or is explicitly terminal;
- every possible runner result maps to an outcome;
- success outcomes depend on machine-observed postconditions;
- a failed effect cannot be represented as successful completion;
- there is no implicit fallthrough; and
- any compatibility form such as `next_step` compiles to an explicit outcome.

The runtime, not the LLM, commits the outcome and transition. The model may
propose a semantic decision, but it cannot assert that an action succeeded or
that a completion predicate is satisfied.

## Material progress

Progress must be defined structurally. Another model turn, different prose, or
a repeated read is not progress.

Material progress is one or more of:

- an irreversible control-flow advance;
- production of a new required typed output;
- satisfaction of an open obligation;
- consumption of an item from a finite work set;
- decrease of a retry or iteration budget;
- an authorized change to repository or remote state;
- replacement of stale evidence with fresh evidence;
- receipt of new human input; or
- entry into a terminal or explicitly suspended state.

State changes that a guard or completion condition cannot observe do not prove
useful progress for that guard. A retry path is useful only when it can change
something the failed condition reads.

## Liveness and termination

Local outcome completeness prevents stuck individual steps, but it does not
prove global termination. Two valid outcomes may alternate forever. The
compiler must analyze the reachable composed transition graph.

For every reachable state, it should prove that the state is one of:

- successfully terminal;
- explicitly failed, blocked, cancelled, or suspended; or
- able to take a legal transition.

For every cycle, it should prove at least one of:

- the cycle consumes a finite resource;
- the cycle decreases a well-founded variant;
- the cycle removes an item from a finite set that cannot be reinserted; or
- the cycle has a kernel-owned terminal transition that becomes mandatory from
  a machine-observed condition.

Useful variants include `remaining_attempts`, `unresolved_findings`,
`unprocessed_files`, `remaining_tasks`, and `unanswered_questions`. “The model
should eventually choose to stop” is not a termination argument.

LLM decisions are nondeterministic from the compiler's perspective. Strong
termination means all legal paths terminate. The existence of one path to
completion is insufficient when another legal path repeats forever.

Human input and unavailable external systems require explicit suspension
semantics. Waiting is not a running step and must not consume an unbounded
execution loop. A suspended execution records what event can resume it and may
also carry an expiration or cancellation policy.

## Effects and resource scopes

Every executable capability must declare a conservative effect contract. The
contract should include:

- semantic operation name;
- argument schema;
- resources read and written;
- determinism and idempotency;
- evidence produced;
- evidence invalidated;
- success and failure result variants;
- reversibility and checkpoint requirements;
- network, credential, and external-system access; and
- resource selectors such as repository, path set, branch, PR, host, or secret.

The effect vocabulary must be precise enough to distinguish operations that
need different authority. `workspace_write` alone cannot express “edit only
these three paths,” and `github_mutation` alone cannot express “comment on this
PR but do not merge it.”

Static and runtime effect descriptions must come from one authoritative
registry. If the compiler and runtime carry independent summaries, their
agreement becomes another unproven assumption.

## Nested skills and effect envelopes

Nested skills do not need artificially reduced functionality. They do need to
compose transparently.

Each skill has a transitive effect envelope inferred from its actions, runner
operations, and nested calls. Invoking a child incorporates that envelope into
the caller's effective contract:

```text
parent's direct effects
  + every reachable child's transitive effects
  = composed workflow effect envelope
```

The required relationship is:

```text
actual child effects
  subset of child declared effects
  subset of root execution authority
```

There is no requirement that a child's action names be a subset of the
immediate `uses_skill` step's action names. The call is a typed delegation
boundary, and the child retains its own validated action contract.

The compiler must nevertheless prevent silent authority amplification:

- a read-only-looking parent cannot conceal a child that commits or pushes;
- resource scopes passed to the child can narrow but not widen;
- capability exceptions are not inherited automatically;
- recursive call components require a decreasing measure or finite bound; and
- changes to a child's summary invalidate certificates of transitive callers.

Authors should normally receive inferred effect summaries rather than manually
restating every child effect. Explicit call-site restrictions are useful for
contextual authority such as editable paths or whether remote writes are
allowed.

## Evidence, obligations, and truth

Models may describe evidence, but only trusted runner operations may create
authoritative evidence records. Claims such as “tests passed,” “the files are
staged,” or “the review thread was resolved” must be backed by typed events.

Completion predicates should be expressed over:

- typed outputs;
- successful action records;
- current resource fingerprints;
- fresh evidence records;
- satisfied obligations; and
- disposed findings.

Evidence carries the fingerprint of the state it evaluated. A relevant
mutation invalidates that evidence automatically. A completion proof therefore
cannot reuse a test result from before the latest edit.

## Replay and exactly-once effects

Retries, restarts, and recovery are normal. The language must identify which
actions are naturally idempotent, keyed idempotent, reversible, or inherently
non-idempotent.

Non-idempotent effects require one of:

- an idempotency key recorded before execution;
- a durable intent-and-result journal;
- a query that can establish whether the effect already occurred; or
- a human decision when the external state cannot be determined safely.

The runtime should checkpoint before mutating effects where reversal is
possible. A crash between external execution and local recording must become an
explicit reconciliation state, not an automatic repeat.

## Controlled escape hatches

Strict safety needs explicit exceptions, but there should be no global
`unsafe: true` switch. Exceptions should resemble small unsafe blocks: local,
visible, reviewable, and unable to erase unrelated guarantees.

### Author-time exceptions

Use author-time exceptions for understood recurring cases, such as a shell
operation that cannot yet be represented by a bounded intrinsic. The checked-in
declaration must include:

- the exact operation or command pattern;
- conservative effects and resource scope;
- the guarantee that becomes unknown;
- owner and rationale;
- compensating controls;
- expiration; and
- a migration target.

The workflow certificate must show the weakened guarantee. A warning baseline
manages rollout debt; it does not convert unknown into proven.

### Runtime capability exceptions

Use runtime exceptions for an unforeseen operation required by one execution.
An exception must be bound to:

- execution and active unit;
- step and semantic operation;
- exact arguments or a narrow argument schema;
- manifest fingerprint and effects;
- resource selectors;
- identified approver;
- expiration and maximum uses; and
- compensating sandbox, checkpoint, or review requirements.

Exceptions are non-transitive and fail closed. They may grant a specific
capability, but they may not suppress failed evidence, forge success, disable
audit history, or waive kernel integrity checks.

### Relaxing liveness

Uncertainty about progress should not be handled with an unbounded retry. A
workflow may instead enter a bounded or suspended state:

- `blocked`: intervention is required;
- `suspended`: a named external event may resume execution;
- `partial`: explicitly declared outputs are valid, but global completion is
  not claimed;
- `failed`: execution ended without satisfying the goal; or
- `cancelled`: authority to continue was withdrawn.

These are safe terminal boundaries even when successful completion cannot be
proved.

## Safety profiles

Guarantees are multidimensional, but operational profiles can require useful
bundles:

- **strict** requires all designated guarantees to be proven;
- **bounded** permits selected unknowns while retaining hard effect, scope, and
  resource limits;
- **approval-gated** permits named exceptional effects only through a signed
  decision;
- **quarantined** allows unknown behavior in a disposable environment with no
  secrets or external writes; and
- **legacy** permits temporary warnings and makes no claim for the affected
  guarantees.

Profiles must never redefine a disproven property as proven. They determine
whether a certificate is acceptable for a particular environment.

## Authoring principles

- Put authority, effects, outcomes, guards, and completion in structured data.
- Use prose only for semantic judgment and explanation.
- Prefer runner-owned execution when no model judgment is required.
- Prefer explicit blocked or suspended outcomes over speculative retries.
- Give every cycle a finite bound or decreasing variant.
- Make success depend on current machine evidence.
- Infer transitive effects and show them to authors.
- Keep exceptions exact and temporary.
- Reject ambiguity at compile time when the runtime would otherwise guess.
- Preserve stable diagnostic codes and shortest counterexample paths.

## Non-goals

The language does not prove that an LLM's semantic judgment is wise, that an
external service is correct, or that arbitrary shell code has effects the
compiler cannot observe. It can still ensure that uncertain judgment occurs
inside a bounded, typed, scoped, and auditable execution.

The language also does not need to prove that every workflow succeeds. Safe
failure, blocking, suspension, and cancellation are legitimate outcomes.

## Definition of success

This direction is successful when a reviewer can inspect a compiled workflow
certificate and answer, without trusting prompt prose:

- What can the LLM choose at each boundary?
- What can the complete workflow read and mutate?
- Which resources are in scope?
- What outcomes can each step produce?
- Why can every retry help?
- Why does every cycle terminate?
- What evidence is required for success?
- Which guarantees are proven, unknown, or disproven?
- What exceptions are active, who approved them, and when do they expire?

At that point, a large class of current “agent reliability” problems become
ordinary compiler errors.
