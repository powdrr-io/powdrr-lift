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

Safety is not enough. The language must also express useful adaptive work:
exhaustive branches over discovered facts, iteration over repository-derived
collections, bounded repair, and different validation obligations for different
changes. Those constructs belong in the typed control plane rather than in an
LLM's conversational memory.

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
  control_termination_safe: proven
  operation_termination_safe: proven
  termination_safe: proven
  resource_safe: proven
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
| Control termination safety | Following a legal orchestration path forever without reaching a terminal or suspended state |
| Operation termination safety | One LLM, tool, subprocess, or nested activation running or retrying without an enforced bound |
| Termination safety | Lacking either control termination or operation termination for any reachable execution |
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
- immutable collection snapshots, finite resources, or decreasing measures for
  every form of iteration; and
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

## A total control plane with powerful operations

An unrestricted Turing-complete control language cannot also admit a decidable
termination proof for every program. That is the halting problem, not a tooling
gap. Powdrr Lift should therefore make a deliberate separation:

- the orchestration language is a **total control plane** whose accepted
  programs are proven to terminate or suspend;
- operations may invoke Turing-complete implementations, compilers, test
  runners, or LLMs, but each invocation is contained by a timeout and finite
  resource grant; and
- a workflow using an opaque or unbounded operation cannot receive the strongest
  termination certificate, even when its control-flow graph is finite.

This is expressive enough for feature development because its useful dynamism
comes from data, not unrestricted recursion. The language needs the following
structured control forms:

| Form | Purpose | Required termination argument |
| --- | --- | --- |
| `sequence` | Run dependent stages in order | Finite child list |
| `match` | Choose from data-driven alternatives | Pure total guards, exhaustive cases, one selected case |
| `for_each` | Process discovered files, tasks, or obligations | Sealed finite snapshot and one terminal result per item |
| `worklist` | Admit dynamically discovered findings | Stable item keys, monotone admission, no reinsertion, and a cardinality bound |
| `repeat` | Re-run validation after a mutation creates a new evidence epoch | Finite epoch budget consumed before every repetition |
| `retry` | Recover from a result that a permitted action can change | Finite budget consumed on every back edge |
| `call` | Reuse a skill | Acyclic call graph, or an explicit decreasing argument for recursion |
| `suspend` | Wait for a human or external event | No running execution while suspended |

There is intentionally no general `while <LLM judgment>` construct and no
unbounded `goto`. An author can express a cycle only through a form whose
termination rule the compiler understands. Lower-level graph syntax may remain
as an intermediate representation, but validation must recover one of these
proofs for every back edge.

### Deterministic, exhaustive branches

Branch guards operate on typed, kernel-owned values and are pure: evaluating a
guard cannot call an LLM, read changing external state, or produce an effect.
Changing state must first be captured by an operation result. A `match` then
selects exactly one case from that result:

```yaml
- id: classify-change
  operation: analyze_change_surface
  bind: change_surface

- id: choose-validation-shape
  match:
    value: ${change_surface.risk}
    cases:
      - when: docs_only
        do: [docs-checks]
      - when: local_code
        do: [unit-checks, type-checks]
      - when: boundary_or_auth
        do: [unit-checks, integration-checks, security-review]
    otherwise: failed.unsupported_change_class
```

The compiler checks that `change_surface.risk` exists at this point, that its
type is a closed union, that cases do not overlap, and that the cases plus
`otherwise` are exhaustive. The runtime records the input fingerprint and
selected case. The model may produce a proposed classification when semantic
judgment is required, but the kernel validates the value and owns the branch.

### Finite snapshots and data-driven loops

`for_each` iterates over a sealed `CollectionSnapshot<T>`, not a live query.
The producing operation records the query, source fingerprint, stable item
keys, item count, and configured maximum. The kernel rejects duplicate keys or
an oversized result before entering the body. Items may run sequentially or in
parallel, but each item receives a durable status and can leave the body only
through a declared outcome.

```yaml
- id: discover-change-units
  operation: build_change_plan
  bind:
    change_units:
      type: snapshot<change_unit>
      key: $.id
      max_items: 64

- id: implement-change-units
  for_each:
    snapshot: ${change_units}
    item: change_unit
    max_parallel: 4
    body:
      call: implement_one_change
      with:
        unit: ${change_unit}
      retry:
        budget: 2
        on: [patch_rejected, correctable_test_failure]
    collect: implementation_results
    on_item_exhausted: failed.change_unit_not_implemented
```

The repository may contain one change unit or sixty-four; the same definition
handles both. It still terminates because discovery happens once for that
snapshot, its cardinality is bounded, and each item activation is bounded.
Mutation does not silently change the snapshot. If rediscovery is necessary,
the workflow creates a new epoch and consumes an explicit epoch budget.

A `worklist` supports findings discovered during review or validation. Unlike a
snapshot loop, it may admit new items while running, but only under a declared
finite universe or admission bound. Item identity is stable across rediscovery,
and an item moves monotonically through `pending -> active -> disposed`. A
disposed key cannot become pending again in the same epoch. A materially new
finding needs a new key and consumes one admission. This prevents an LLM from
renaming the same objection forever to manufacture apparent progress.

```yaml
worklist:
  name: findings
  item_type: finding
  key: $.fingerprint
  max_admissions: 40
  max_epochs: 3
  process: repair_or_dispose_finding
  rediscover_after_mutation: run_review_suite
  on_exhausted: failed.unresolved_findings
```

The lexicographic variant is
`(remaining_epochs, remaining_admissions, pending_items, item_attempts)`. Every
back edge must strictly decrease that tuple, or the definition is rejected.
The bounds are policy inputs included in the compiled contract fingerprint;
the LLM cannot raise them at runtime.

### Two termination guarantees

Termination has two layers, and a certificate must report both:

1. **Semantic termination**: all legal paths through the orchestration program
   reach a terminal or suspended state. This is the programming-language sense
   of termination and is proved from finite structure, exhaustive branching,
   well-founded variants, and bounded calls.
2. **Operational containment**: every activation, including an LLM or external
   tool call, has enforced limits for wall time, model turns, tokens, tool
   calls, output bytes, subprocesses, and external cost. Exhaustion maps to a
   declared outcome; it never grants another implicit attempt.

Neither implies the other. A three-node graph can hang forever inside an
unbounded subprocess, while a time-limited loop can be operationally contained
without having a semantic proof. The certificate exposes
`control_termination_safe` and `operation_termination_safe` separately;
`termination_safe: proven` requires both.

Each LLM judgment is therefore a single bounded activation, not an agent loop
hidden inside a step. The response schema contains a closed decision union. A
timeout, invalid response, provider error, or token limit becomes a typed result
handled by the kernel. Repairing an invalid response consumes a small declared
response-repair budget. When the budget reaches zero, the workflow fails,
blocks, or suspends according to its contract.

Containment requires an actual enforcement boundary. Subprocesses run in a
killable process group or sandbox, provider requests have kernel-owned
deadlines, and external operations that cannot be proven cancelled enter an
`ambiguous` reconciliation outcome. Merely checking elapsed time after an
in-process operation returns does not prove operation termination.

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

## End-to-end feature development example

Feature development is a good stress test because the path and amount of work
are discovered from the repository. A documentation correction may need no
security review; an authentication change may produce several implementation
units, test targets, invariants, and security findings. The workflow should
adapt to those facts without giving the LLM an open-ended agent loop.

The following example is illustrative source syntax. It shows the semantic
constructs the compiler must preserve; exact YAML spelling may evolve.

```yaml
workflow: develop-feature
inputs:
  request: feature_request
  repository: repository_ref

authority:
  read: ["repo://**"]
  write: ["repo://src/**", "repo://tests/**", "repo://docs/**"]
  git: [status, diff, add, commit, push_feature_branch]
  github: [create_pull_request]

limits:
  wall_time: 12h
  llm_activations: 512
  llm_tokens: 4000000
  tool_calls: 5000
  external_cost_usd: 200
  response_repairs_per_activation: 1
  repair_epochs: 3

body:
  - id: observe-repository
    operation: capture_repository_snapshot
    with: {repository: "${repository}"}
    bind: repository_snapshot

  - id: gather-context
    llm: identify_relevant_context
    with:
      request: "${request}"
      repository_index: "${repository_snapshot.index}"
    decision:
      schema: context_query
      max_items: 80
    then:
      operation: read_context_snapshot
      bind: context_snapshot

  - id: understand-change
    llm: derive_change_contract
    with:
      request: "${request}"
      context: "${context_snapshot}"
    decision:
      schema: change_contract
      requires:
        - in_scope
        - out_of_scope
        - acceptance_criteria
        - preserved_invariants
        - risk_class
    bind: change_contract

  - id: plan
    llm: plan_change_units
    with:
      contract: "${change_contract}"
      context: "${context_snapshot}"
    decision:
      schema: snapshot<change_unit>
      key: $.id
      max_items: 64
    validate:
      operation: validate_plan_coverage
      requires:
        covers: "${change_contract.acceptance_criteria}"
        preserves: "${change_contract.preserved_invariants}"
    bind: change_units

  - id: implement
    for_each:
      snapshot: "${change_units}"
      item: unit
      max_parallel: 4
      body:
        call: implement_change_unit
        with:
          unit: "${unit}"
          contract: "${change_contract}"
          writable_paths: "${unit.paths}"
        retry:
          budget: 2
          on: [patch_rejected, local_check_failed]
      collect: implementation_results
      on_item_exhausted: failed.implementation_incomplete

  - id: discover-verification
    operation: derive_verification_obligations
    with:
      baseline: "${repository_snapshot.commit}"
      current_tree: workspace
      acceptance: "${change_contract.acceptance_criteria}"
      invariants: "${change_contract.preserved_invariants}"
    bind:
      obligations:
        type: snapshot<verification_obligation>
        key: $.id
        max_items: 128

  - id: validate-and-repair
    repeat:
      budget: "${limits.repair_epochs}"
      body:
        - for_each:
            snapshot: "${obligations}"
            item: obligation
            body:
              operation: run_verification
              with: {obligation: "${obligation}"}
            collect: verification_evidence

        - parallel:
            max_parallel: 4
            branches:
              - call: review_code
              - call: review_feature_completeness
              - call: review_preserved_invariants
              - match:
                  value: "${change_contract.risk_class}"
                  cases:
                    - when_in: [boundary, authentication, authorization, secrets]
                      do: {call: review_security}
                  otherwise: {emit: no_security_review_required}
          collect: review_reports

        - operation: normalize_findings
          with:
            verification: "${verification_evidence}"
            reviews: "${review_reports}"
          bind:
            findings:
              type: snapshot<finding>
              key: $.fingerprint
              max_items: 40

        - match:
            value: "${findings.count}"
            cases:
              - when: 0
                break: validated
            otherwise:
              for_each:
                snapshot: "${findings}"
                item: finding
                body:
                  call: repair_finding
                  retry: {budget: 1, on: [patch_rejected]}
              then:
                operation: refresh_changed_evidence
      on_exhausted: failed.validation_not_converged

  - id: final-proof
    operation: evaluate_feature_completion
    requires:
      implementation_results: all_succeeded
      acceptance_criteria: all_covered
      verification_evidence: all_fresh_and_passing
      findings: none_open
      invariants: all_fresh_and_passing
      security: passing_or_structurally_not_required
      scope: observed_effects_within_authority
    outcomes:
      completed: {goto: publish}
      incomplete: {terminal: failed}

  - id: publish
    sequence:
      - operation: create_commit
      - operation: push_feature_branch
      - operation: create_pull_request
    outcomes:
      completed: {terminal: succeeded}
      ambiguous: {terminal: suspended, resume_on: publication_reconciled}
      failed: {terminal: failed}
```

The apparent flexibility comes from typed values:

- `context_query` determines which files are read, subject to path and item
  limits;
- `change_contract.risk_class` selects the validation and security branches;
- `change_units` determines the number of implementation activations;
- the actual diff, project manifests, acceptance criteria, and invariants
  determine `obligations`; and
- verification and four independent review perspectives determine `findings`.

The model makes semantic judgments inside those boundaries. It never controls
the program counter, loop budget, evidence freshness, finding count, or success
predicate.

### Why the example terminates

Compilation produces a proof obligation for every adaptive construct:

| Construct | Proof |
| --- | --- |
| Context gathering | One bounded LLM decision, then a snapshot of at most 80 paths |
| Planning | One bounded decision and at most 64 stable change-unit keys |
| Implementation | At most 64 items, each with two retries and bounded child operations |
| Verification | At most 128 obligations in an epoch, one bounded operation per item |
| Review | Four finite branches; the security branch is an exhaustive match |
| Repair | At most 40 findings per epoch, one repair retry per finding, and three epochs |
| Publication | Three finite operations; ambiguous remote state suspends instead of retrying |

The compiler can derive a conservative upper bound on activations and operation
calls from these products and sums, then verify it fits inside the workflow's
global resource limits. It need not predict whether validation will pass. If
the feature is too complex, verification keeps failing, or the model repeatedly
returns invalid decisions, execution reaches a declared failure or suspension
state within the bound.

### Freshness, fixed points, and feature completeness

A successful repair mutates the workspace and invalidates evidence whose
resource fingerprint intersects the mutation. `refresh_changed_evidence`
creates a new validation epoch and reruns affected obligations; it does not
pretend earlier test results remain current. The next review may discover new
findings, but doing so consumes the next finite repair epoch.

The final proof is a deterministic reduction over durable records. Relative to
the accepted `change_contract`, “feature complete” means every acceptance
criterion has a typed coverage edge to an implemented change and fresh passing
evidence. “Invariants preserved” and “secure” mean the declared review and
verification obligations have current passing evidence, not that an LLM said
the code looked good. No execution language can prove that a semantic contract
perfectly captured unstated user intent or that a review found every possible
vulnerability; those judgments remain explicit assumptions in the certificate.
The workflow may fail to achieve its declared facts, but it cannot report
success without them and cannot chase them forever.

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
- Express alternatives as exhaustive matches over typed snapshots.
- Iterate over sealed snapshots or monotone bounded worklists, never live
  model-controlled collections.
- Prefer explicit blocked or suspended outcomes over speculative retries.
- Give every cycle a finite bound or decreasing variant.
- Bound every individual LLM and operation activation as well as the enclosing
  control-flow graph.
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
- Which repository facts select each branch and determine each work set?
- Why can every retry help?
- Why does every control-flow cycle terminate, and what stops each individual
  LLM or tool activation?
- What evidence is required for success?
- Which guarantees are proven, unknown, or disproven?
- What exceptions are active, who approved them, and when do they expire?

At that point, a large class of current “agent reliability” problems become
ordinary compiler errors.
