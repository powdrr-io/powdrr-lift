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
  operation_state_safe: proven
  capability_safe: proven
  effect_safe: proven
  effect_contract_safe: proven
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
| Operation-state safety | Advancing, retrying, or claiming an operation result without a durable kernel-owned record of its state and result |
| Capability safety | Invoking an undeclared tool or semantic operation |
| Argument safety | Passing values outside the operation's argument schema |
| Effect safety | An operation producing effects outside its declared effect summary |
| Effect-contract safety | An operation declaration granting authority by itself, or a supposedly enforced tool exercising an unmediated effect channel |
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

### Operations produce durable facts

An action proposal is not an operation result. Every operation invocation must
have a durable, kernel-owned lifecycle record before it can contribute to
progress, an output, evidence, completion, or a transition. The record is
identified by its execution, step activation, operation contract fingerprint,
normalized arguments, and idempotency key where applicable.

The language has a closed operation-state union such as:

```text
planned -> authorized -> executing -> succeeded | failed | ambiguous | denied | cancelled
```

Only the kernel and operation broker may advance this state. In particular,
`succeeded` means that the operation returned a recognized successful result,
its observed effects conformed to its authority, and its declared result
postconditions were recorded. `ambiguous` means that execution may have reached
an external system but success cannot be established; it is never silently
coerced into either success or a retry.

Each terminal operation record carries its structured result, observed effects,
produced and invalidated evidence, output bindings, and any reconciliation
requirement. Derived workflow state is a reduction of these records, not an
LLM-maintained checklist.

At every LLM boundary, the kernel supplies a read-only state projection for
the active step: applicable operation records and statuses, current typed
outputs, open obligations, valid evidence, and the actions still legal. This
lets the model make an informed next decision without trusting it to remember
whether an earlier operation succeeded. The model may request a retry only
when the recorded state and contract permit it; it may not repeat an operation
merely because its earlier prose is absent or uncertain.

Workflow generation from a template illustrates the rule. Template
instantiation is a runner-owned operation, not a conversational milestone. It
may produce a `workflow_instance` output only after the generated definition
has been persisted, parsed, validated against its template contract, compiled,
and fingerprinted. The next step receives the resulting instance reference in
its kernel-generated state projection. A validation failure instead creates a
typed failed result with diagnostics; no subsequent step can treat a proposed
or malformed workflow as generated successfully.

## Material progress

Progress must be defined structurally. Another model turn, different prose, or
a repeated read is not progress.

Material progress is one or more of:

- an irreversible control-flow advance;
- production of a new required typed output;
- a successful, state-changing operation record whose declared postconditions
  are current;
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

### Effect vocabulary and trustworthy contracts

MCP's `readOnlyHint`, `destructiveHint`, `idempotentHint`, and `openWorldHint`
are a useful common risk vocabulary, but they are deliberately hints rather
than a source of authority. In particular, an annotation supplied by an
untrusted server cannot make an operation safe. Powdrr Lift should preserve
those distinctions while expressing them as operation contracts that the
kernel enforces. See the [MCP specification](https://modelcontextprotocol.io/specification/2025-03-26/index)
and its [ToolAnnotations rationale](https://blog.modelcontextprotocol.io/posts/2026-03-16-tool-annotations/).

An operation contract must answer more than whether an operation is broadly
destructive. It identifies the effect family and its concrete target, for
example: write these paths, stage this path set, comment on this PR, or make a
request to this host. It also records whether repeating the same request is
safe and how the kernel can establish that an ambiguous request already
occurred.

The declaration never grants authority by itself. Before an operation runs,
the kernel intersects its declared effects and selectors with the active
workflow authority. During execution, every observable effect channel is
mediated and checked. The central invariant is:

```text
observed effects
  subset of declared operation effects
  intersection active workflow authority
```

For enforced operations, this is a prevention property rather than a
best-effort audit. Filesystem, process, network, Git/GitHub, secret, and
external-mutation interfaces must be brokered so an undeclared effect fails
closed. A raw unrestricted process capability necessarily weakens the claim:
it can use libraries, inherited credentials, hooks, sockets, or subprocesses
beyond what a post-hoc trace can prove. Prefer typed semantic operations over
ambient shell authority where a strong certificate is required.

Tools should carry an explicit trust tier:

| Tier | Meaning |
| --- | --- |
| Enforced | Every relevant effect channel is mediated; undeclared effects are denied by the execution boundary. |
| Tested | The operation has adversarial conformance tests, but one or more channels are observed rather than fully mediated. |
| Attested | A trusted provider supplies the contract; Powdrr Lift has not independently established it. |
| Opaque | No trustworthy contract exists; use broad authority, quarantine, or explicit approval. |

Only enforced operations may support the strongest effect and scope proofs.
An external MCP server's annotations normally begin as attested or opaque;
they do not become enforced merely by being present.

### Adversarial effect-contract conformance

Effect declarations need a verification program of their own. For each
operation, execute adversarial conformance cases through instrumented brokers
and compare the recorded trace to its contract. The oracle is structural, not
model behavior:

```text
observed_effects subset_of declared_effects intersection granted_effects
```

The suite should attempt to induce undeclared reads and writes, path escapes
and symlink escapes, unapproved subprocesses, network destinations, indirect
mutations through libraries or hooks, repeated allegedly idempotent requests,
and prompt-injected operation inputs. It should also use mutation testing:
intentionally introduce an undeclared write, network call, or subprocess in a
test double and verify that the broker and test harness reject or report it.
This validates the detector rather than merely exercising cooperative tools.

Prompt-injection benchmarks such as [AgentDojo](https://arxiv.org/abs/2406.13352)
are valuable outer regressions: they test that hostile tool output cannot steer
an LLM into an unauthorized operation. They complement, rather than replace,
deterministic operation conformance tests. The latter must hold even if the
model follows hostile instructions perfectly.

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
