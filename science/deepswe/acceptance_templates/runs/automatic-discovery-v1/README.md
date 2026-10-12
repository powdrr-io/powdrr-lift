# Automatic decision discovery and scope binding

## Result

**The good classification result from manually supplied questions does not carry
over to automatic question/choice generation.** The discovery step misses useful
distinctions and often supplies no safe complete answer choice. Agreement between
Flash and Jev does not reliably catch those errors.

Three tasks generated 48 questions and 34 agreed clarifications. A manual source
audit finds:

| Measure | Result |
| --- | ---: |
| Questions with a valid answer pair | 31/48 |
| Questions whose two literal choices both omit necessary conditions or contradict the solution | 16/48 |
| Ill-posed question, left unscored | 1/48 |
| Correct clarifications rendered | 25/34 |
| Wrong selections from valid pairs rendered | 2/34 |
| Unsafe choices from defective pairs rendered | 7/34 |

On valid pairs, Flash chooses 28/31 correctly; Jev chooses 26/31 correctly.
Consensus chooses 25 correctly, withholds four and chooses two incorrectly.
That is conditional accuracy on this manually audited generated set, not an
independent model benchmark.

The 16 defective pairs include overbroad or ambiguous choices, not just plainly
opposite answers. Labels judge the complete literal criterion under its declared
condition/observation scope. For example, a fresh outer object does not guarantee
that mutations to nested referenced values are isolated.

## Comparison against the preceding experiment

Use the same 18 positive targets from the preceding frozen oracle, excluding its
known extensions-scope defect. Three unsupported-cap probes were never proposed
to discovery and are not counted as automatically withheld controls.

| Split | Positive targets | Correct manual revised consensus clarifications | Correct automatic consensus clarifications | Partial automatic coverage | Wrong automatic clarification | Missing automatic coverage |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Koota development | 8 | 8 | 1 | 1 | 0 | 6 |
| GraphQL/Bandit evaluation | 10 | 9 | 2 | 3 | 1 | 4 |
| All | 18 | 17 | 3 | 4 | 1 | 10 |

Discovery fully identifies four of these target distinctions, partially discusses
four, and misses ten. One of the four fully identified distinctions is answered
incorrectly by both models: stream overwrite versus shifting.

These counts measure **added clarifications for the sampled facets**, not whether
the whole final prompt contains the original requirements. Original product
requirements are preserved in all three prompts. Automatic discovery also finds
other useful behaviors outside this target list, so the comparison does not mean
only three of its 34 rendered statements are correct.

This is a comparison with a prior run, not a randomized paired causal study.
Selection uses the same consistency-reasoning recipe with extra emphasis on
condition and observation boundary; wording is not byte-identical to the previous
selector. GraphQL additionally uses the bounded recovery described below.

## What ran

1. Reuse the preceding experiment's original instruction/code packets and real
   production routing/splitting output for all three tasks.
2. Flash generates questions, candidate meanings, operation/object/condition/
   observation-scope records, and clause anchors. It sees no prior questions,
   choices, labels, solution patches or validation patches.
3. Flash and actual Typesafe.ai Jev independently answer each generated question.
   Alternative order is deterministic and shared. Neither sees the other's answer.
4. Equal substantive answers render the selected candidate prose literally.
   Unknown, disagreement or call failure withholds that clarification and leaves
   original requirements in the prompt. There is no synthesis, manual repair of
   behavioral candidates, stronger decision model or coding-agent execution.
5. After calls, audit every literal candidate pair against the solution and map
   questions/selected meanings to the preceding frozen target facets.

The generator writes structured scope fields, but **the simple renderer emits
only alternative prose**, so conditions present only in those fields do not reach
the final criterion. That is an additional limitation. The audit considers the
declared scope too; the candidate defects above are not solved just by prefixing
those fields to the prose.

Code retrieval remains manually supplied from the preceding experiment. In
particular, GraphQL DSL code and Koota public entity-operation return behavior
were not in its generation excerpts. Some poor questions/choices may therefore
reflect incomplete retrieval. This experiment automates question/choice/scope
discovery, not repository retrieval or the whole production pipeline.

## Concrete failure points

| Failure | Generated example | Correct solution behavior | What failed |
| --- | --- | --- | --- |
| Missed implicit behavior | No Koota metadata-container or shallow/deep protection questions | Protect metadata properties/containers while keeping nested references shallow | Discovery never exposes these decisions to either classifier |
| Wrong representation premise | Koota d007 asks how a callback's returned partial object is distributed | `updateEach` commits mutations to supplied callback state | Both choices assume an unsupported returned-object contract |
| Wrong complete choice set | Koota d011 offers sole entity argument or entity plus aspect | `callback(entity, undefined)` | Correct first-argument behavior is buried in two wrong exact-arity alternatives; Jev withholds |
| Lost type condition | GraphQL batch-04-q2 offers universal replacement or universal merge | Dict plus dict merges; other terminal values replace | Generator omits the condition deciding the behavior |
| Wrong payload scope | GraphQL batch-02-q1 offers `None` or empty data for any errors-only payload | A later errors-only payload preserves accumulated data | Initial versus later response is not separated |
| Wrong observation boundary | GraphQL batch-02-q3 repeats an unconditional `extensions=None` policy | WebSocket control metadata can populate extensions | Generator repeats the previous audit's defect despite explicit boundary instructions |
| Wrong yield unit | GraphQL batch-05-q2 promises a result immediately for an errored item | Process all incremental items, combine errors, then yield once per payload | Both alternatives assume per-item yielding |
| Existing split changes scope | Routed GraphQL r013 says “navigating through null values”; batch-04-q1 asks whether to construct intermediate containers | Null handling includes terminal null assignment, not arbitrary traversal through null parents | The splitter scopes “null values” under path traversal; discovery amplifies it |
| Agreement does not establish truth | Bandit d010 says an explicit nonempty selector covering all enabled tests counts as blanket `nosec` | It remains specific and counts as `skipped_tests` | Both classifiers choose the same wrong policy from a valid pair |
| Persistent stream distinction | GraphQL batch-03-q1 selects insertion with shifting | Addressed positions are overwritten without shifting | Both choose insertion; previous manually framed revised Jev chose overwrite |

Every generated decision, scope, raw answer, source reference and audit note is
available in `source-audit.json`. These examples were discovered by the post-call
audit; their corrections were not fed back into generation.

### Repetition

The local GraphQL batches rediscover overlapping questions: batch-05-q4 and
batch-06-q2 both ask about incremental messages without data. Batch-02-q5 and
batch-04-q1 revisit null-parent handling with defective premises. Several selected
statements restate explicit original requirements while missing harder conditions.

| Prompt | Original instruction words | Previous revised prompt words | Automatic prompt words |
| --- | ---: | ---: | ---: |
| Koota | 234 | 412 | 407 |
| GraphQL | 216 | 409 | 626 |
| Bandit | 435 | 598 | 630 |

The extra GraphQL length does not translate into sufficient clarification of the
previously identified snapshot, error-lifetime and progress-payload distinctions.

## Discovery failures and recovery

All three initial bulk calls failed the initial acceptance path:

- Koota and Bandit returned complete JSON with usable questions but included
  coverage rows for non-product clauses and inconsistent links. Preserve raw
  responses; filter non-product coverage and derive coverage links mechanically
  from the generated questions' declared anchors. `normalization/` records every
  change. No behavioral question, scope or alternative changes, and no new
  discovery call is needed for these two tasks. Self-reported coverage remains
  bookkeeping, not evidence that requirements are semantically covered.
- GraphQL exceeded the provider's 65,536-character structured-response guard
  after 238.627 seconds. No complete response was returned. Its failed request
  and diagnostic prefix are retained.
- After observing that size failure, use a generic recovery path: groups of four
  active product clauses, at most eight questions per group, shorter prose fields,
  other clauses retained as dependency context. Six fresh bounded calls produce
  26 GraphQL questions. This is an adaptive engineering recovery, not another
  independent preregistered arm or a retry of the same failed request.

The successful recovery makes prompt creation possible but does not repair
semantic errors. It also creates overlapping questions across batches.

Jev uses the production request builder and endpoint directly, preserving raw
provider responses. Provider `unresolved` maps to experiment `unknown` from the
start, avoiding the previously diagnosed single-field mapping error. There is
no model fallback and no new production client change.

Total: **105 provider calls: 57 DeepInfra and 48 Typesafe.ai**. The archive has
102 completed checkpoints and three initial failures (two local bookkeeping
validation failures and one provider output-bound failure). Summed provider
durations are 761.117 seconds; this is not elapsed wall time. Token usage is
unavailable in these receipts. Cached reprocessing makes no new provider calls.

## What to try next

**Keep the revised answering recipe, but test a different discovery decomposition
before fine-tuning.** The current generator combines identifying a distinction,
deciding its applicability, choosing representations and authoring complete
candidate criteria in one call. The output often looks well structured while
the decision itself is malformed.

The next experiment should separate those steps:

1. **Extract a source-bound behavior record first:** operation, input, condition,
   observation point and stated effect. Include exact instruction/code anchors.
   Ask whether the routed clause's scope is actually present in the original
   sentence; do not turn a list of supported cases into a traversal guarantee.
2. **Identify applicable missing conditions:** initial versus subsequent delivery;
   omitted versus empty versus nonempty-but-no-match; object versus scalar;
   terminal value versus traversed parent; outer container versus nested value.
   These are small reasoning dimensions, not a catalog of final criteria. Apply
   them only when supported by the operation and its data.
3. **Resolve one conditioned behavior at a time.** Do not force a binary pair
   when different conditions make both proposed global answers wrong. Allow
   “the candidates omit the actual behavior” as a distinct outcome from “the
   sources do not settle it.” Unknown still must not block prompt creation.
4. **Bind verified scope into the emitted criterion deterministically**, then
   remove equivalent repeated clarifications. Do not use another prose synthesis
   call to repair malformed reasoning.

Evaluate on the same frozen targets plus an audit of all emitted criteria,
reporting discovery omissions, malformed choices, classification errors and
extra wrong obligations separately. The source audit here supplies concrete
diagnostic cases, not answers to expose in a supposed unseen evaluation.

No fine-tuning or production pipeline change is justified by this experiment.
It does not prove that the proposed decomposition will work; that is the next
claim to test.

## Reproduction and boundaries

Use the shared Python environment with this worktree's root and `src` on
PYTHONPATH. Extract `call-checkpoints.tar.gz` in this directory before replaying
`run_experiment.py`; completed/usable saved responses are reused only when the
request fingerprint matches. GraphQL's bulk failure activates the saved bounded
recovery. Run `evaluate.py` after generation for audit artifacts and checks.

- `experiment-definition.json`: bulk discovery/selection recipe, schemas, models
  and input fingerprints frozen before calls.
- `inputs/`: generation-safe snapshots of original instructions/code/routing.
- `provenance.json`: verified original commits/excerpts, task patch hashes,
  reused routing hashes and generated-prompt fingerprints.
- `discovery/`, `normalization/`, `recovery/`: raw question provenance and explicit
  local/adaptive recovery accounts; full requests remain in the archive.
- `results/`: all 48 independent Flash/Jev decisions and literal selected prose.
- `prompts/`: the three complete-format prompts, retained as generated, including
  known errors. They are experiment artifacts, not recommended production prompts.
- `source-audit.json`, `target-mapping.json`, `evaluation.json`: post-call manual
  labels, patch evidence and reproducible counts.
- `call-checkpoints.json`, `call-checkpoints.tar.gz`, `run-summary.json`: request/
  response captures, hashes, providers, failure records and call totals.

The audit author knows the solutions and sees model answers. It is not blinded,
independent human adjudication or untouched held-out evaluation. It audits all
generated pairs and a fixed subset of solution facets, not every solution line.
Koota remains development; GraphQL/Bandit remain evaluation. No coding agent or
tests ran, so these results establish neither implementation success nor a
benchmark pass rate.

Scoped Ruff formatting/lint and mypy, input/hash checks, literal rendering checks,
archive fingerprint checks and environment credential scans pass. Tests remain
disabled at the user's request.
