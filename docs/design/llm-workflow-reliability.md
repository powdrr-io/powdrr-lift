# Reliable LLM Workflow Execution

## Summary

Workflow reliability should come primarily from orchestration, typed state, and
machine-checkable transitions. Prompts should tell an LLM what judgment to make;
they should not make the LLM responsible for remembering the program counter,
repeating deterministic commands, or asserting that required work happened.

The proposed direction is to treat a workflow as a small executable language
with four kinds of node:

| Node | Owner | Example |
| --- | --- | --- |
| Deterministic action | Runtime | Run fuzzy match, an evaluator, or Git status |
| Deterministic branch | Runtime | Skip bootstrap when an artifact already exists |
| Judgment | LLM | Select a canonical feature or decompose work into PRs |
| Human decision | User | Resolve a genuinely ambiguous feature identity |

The runtime owns the program counter. The LLM is called only when the next state
depends on semantic judgment.

## Observed failure pattern

A September 2026 `start-implementing-feature` harness run reached roundtrip 46
and was still running validation when the outer harness timed out. The run
illustrated several recurring failure modes:

- The LLM selected a deterministic tool, interpreted its output, and then used a
  separate roundtrip to say `next_step`.
- Step prose sometimes required one exact operation while the mechanical action
  contract advertised several unrelated actions.
- `next_step` was model-visible even when advancing was a deterministic result of
  a completed operation.
- The model could claim that verification was complete without supplying all of
  the evidence described by the step. In one roundtrip it acknowledged that two
  workflow files had not been read and advanced anyway.
- After a Git failure, the model probed several sibling workflows and invented an
  undeclared inspection command. Runtime validation rejected the command, but
  only after several unnecessary model calls.
- Observer guidance improved recovery after drift, but did not remove the
  underlying opportunities to drift.

These are orchestration problems more than wording problems. Adding stronger
imperatives to the prompt leaves the same invalid paths available.

## Design principles

### Execute deterministic nodes without an LLM

A node with one fully declared operation should execute in the runtime. Once the
operation is recorded, the runtime advances. The LLM should not be called to
choose the only available operation or confirm that it ran.

Validation obligations should eventually follow the same pattern: the runtime
runs the full declared set and asks the LLM for a repair only when the aggregate
result contains actionable failures.

### Use exact schemas at judgment boundaries

Each LLM call should expose only the decision that can be made at that node. A
feature-selection response, for example, should have a fixed action discriminator,
an enumerated candidate, and evidence paths. A repair response should expose only
the mutation actions supported by that repair node.

JSON-object mode guarantees JSON syntax, but it does not guarantee the correct
action or fields. Provider support permitting, per-node Structured Outputs or
strict function schemas should replace a single broad action schema.

### Advance from evidence, not narrative

Freeform nodes should declare executable completion predicates. Examples include:

- every required output is present and type-valid;
- a particular invocation succeeded in the current step epoch;
- every expected document was read in the current step epoch;
- every discovered validation obligation passed after the latest mutation; and
- every proposed PR has exactly one workflow with matching dependency metadata.

`next_step` can then become an internal runtime event. A model response proposes
an artifact or decision; the runtime decides whether the transition is legal.

### Define error policies with the operation

Known results should map to declared transitions:

```yaml
on_result:
  success: advance
  already_exists: verify_existing_artifact
  merge_not_fast_forward: inspect_integration_state
  transient_provider_error: retry
  otherwise: request_repair_decision
```

This prevents exploratory retries such as trying every sibling PR after a shared
integration-branch failure.

### Keep the LLM packet local to the decision

A judgment request should contain the current goal, relevant inputs, exact output
schema, current failure when present, and acceptance criteria. Historical action
catalogs, generic tool tutorials, and previous-step instructions should be
omitted unless the decision actually depends on them.

This direction agrees with OpenAI's current guidance to define outcomes, success
criteria, evidence, and stopping rules; put tool-specific behavior near tool
definitions; use Structured Outputs for schema adherence; and avoid procedural
prompt detail when the exact procedure does not need to be model-selected:
<https://developers.openai.com/api/docs/guides/latest-model?model=gpt-5.5>.

## First experiment

### Hypothesis

Moving atomic tool execution and advancement into the runtime will reduce LLM
roundtrips and remove action-selection errors without reducing artifact quality.

### Change

This experiment makes `invoke_tool` skill steps runtime-owned in interactive and
nested-task execution. After their deterministic pre-step is recorded, the
runtime advances directly.

The first three `start-implementing-feature` discovery steps are converted from
broad freeform steps to atomic fuzzy-match nodes. One small judgment node extracts
`feature_query` from the user's request; the three searches consume that named
value and publish named result outputs for canonical feature selection.

This changes the ownership boundary without introducing a general completion-
predicate language. Existing output, tool-invocation, validation-gate, and
transition checks remain active for freeform steps. General declarative completion
predicates are follow-on work.

### Expected result

The former discovery sequence used six model roundtrips: three tool selections
and three confirmations. It now uses one model roundtrip to interpret the feature
query, followed by three runtime actions. Existing atomic tool steps also stop
requesting confirmation. For the observed 46-roundtrip baseline, the conservative
target is at most 36 model roundtrips with no new contract errors.

### Measurements

Record the following for each live harness run:

- completion status and elapsed time;
- total LLM roundtrips;
- contract-validation failures;
- repeated or semantically equivalent actions;
- whether deterministic discovery steps appeared in an LLM prompt; and
- final artifact validation status.

Mocked end-to-end coverage must additionally prove that the three discovery step
identifiers never appear as current steps in an LLM request.

### Results

The mocked end-to-end workflow passed and confirmed that none of
`discover-proposed-feature`, `discover-current-feature`, or
`discover-feature-workflows` appeared as the current step in an LLM request.
The runtime executed all three searches and recorded their named outputs.

One live harness run was then executed with a 40-turn configuration. Nested
skills maintain their own turn loops, so the complete transcript contained 57
LLM roundtrips. The workflow process returned zero and reached its final handoff,
but the harness classified the run as failed because it observed two correction
markers:

- an undeclared `git diff --cached --name-only` command was proposed during the
  nested pull-request workflow and rejected by the step contract; and
- pull-request creation was rejected because the execution readiness state did
  not contain an accepted `readiness_report` artifact.

The deterministic discovery boundary behaved as intended. No discovery search
required an LLM call, and the first validation obligation began at roundtrip 34;
the earlier observed run reached its first validation obligation at roundtrip
43. That nine-roundtrip difference is directional rather than a controlled
before-and-after measurement because the repository baseline changed between
runs.

The ≤36 total-roundtrip target was not met. The run demonstrates that converting
atomic steps removes those calls reliably, while the remaining freeform and
nested validation/publication workflows dominate the total. In particular,
validation still uses one model call for every obligation, and publication has a
readiness requirement that the active workflow cannot satisfy. Runtime-batched
validation and an explicit readiness-artifact producer should therefore precede
further prompt tuning.

## Follow-on work

1. Add declarative completion predicates and make `next_step` runtime-internal for
   all steps with objective exit criteria.
2. Batch validation obligations in the runtime and send only aggregate failures
   to repair nodes.
3. Generate strict, per-node response schemas instead of using one broad JSON
   action format.
4. Add workflow-definition lint rules for prose/action conflicts, implicit
   actions, missing completion predicates, and ambiguous output ownership.
5. Turn the live harness into a conformance evaluation that compares completion,
   roundtrips, rejections, unsupported transition claims, tokens, and latency
   across multiple runs and model providers.
