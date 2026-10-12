# Decision instructions: current vs consistency reasoning vs examples

## Result and recommendation

**Use the revised question/instruction recipe as the baseline for the next
experiment. Fine-tuning is not supported as the immediate next step by these
results.** Flash recovered most missed decisions when asked to resolve scope,
corresponding operation behavior and consistency consequences. Four added
contrasting examples produced no further improvement in this sample.

The evaluation repositories were GraphQL `gql` and Bandit, separate from Koota
development. They contain **11 positive target decisions and two unsupported
numeric-cap controls**.

**Post-run source audit:** one manually authored choice (`g-extensions-lifetime`)
overstates its scope. Excluding that case gives ten positive evaluation decisions:
Flash improves **5/10 to 9/10**, Jev improves **7/10 to 10/10**, and consensus
improves **5/10 to 9/10**, with one unresolved disagreement. The example arm
has the same scores as revised wording. The table below retains the original
frozen-label results; it is not a claim that all rendered criteria are correct.

| Evaluation arm | Flash correct / 11 | Flash missing | Flash wrong | Jev correct / 11 | Consensus correct / 11 | Consensus missing | Consensus wrong |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Current wording | 6 | 4 | 1 | 8 | 6 | 4 | 1 |
| Revised questions/instructions | 10 | 0 | 1 | 11 | 10 | 1 | 0 |
| Revised plus four contrasting examples | 10 | 0 | 1 | 11 | 10 | 1 | 0 |

Every arm/model withheld the two evaluation cap controls. Across development
and evaluation, all three unsupported caps were withheld. That is only three
simple negative controls; it does not establish a general false-positive rate.

Koota development contained eight positive decisions and one cap control.
Current Flash selected none of those eight target behaviors and abstained on
all eight. Revised Flash selected all eight correctly, including metadata
container protection, shallow freezing/reference preservation, nested callback
grouping, equal-value notifications and the callback's first argument.

This is one sample per manually designed question/model/arm, with agent-authored
labels and known solutions. It is evidence for a better decision recipe on
these cases, not a model accuracy guarantee or coding-agent pass result.

## What changed

Both the question wording and the shared decision instructions change in the
revised arm. Original source packets, routing/splitting output, choices,
choice order, models, output schema and deterministic rendering remain fixed.
This evaluates the combined recipe; it does not isolate which individual
instruction sentence caused the improvement.

The current wording concentrates on whether an instruction expressly requires
a policy or existing code establishes it. Several current questions explicitly
ask whether the task specifies a detail. Under that framing, abstention can be
a reasonable answer while failing the downstream goal of a complete contract.

The revised wording asks for the behavior that best preserves the feature's
meaning and relationships:

1. Identify the operation, object, condition and scope of each statement.
2. Keep construction, membership, projection, writing and event delivery separate.
3. Inspect the corresponding existing operation, preserving compatible behavior
   and applying explicit new requirements where they override it.
4. Compare alternatives by observable consequences and consistency of the
   requested relationships.
5. Infer a coherent contract when those relationships support it; reserve
   unknown for unsupported guarantees and choices still genuinely unresolved.

Examples contrast constructor flattening with callback grouping, property
freezing with nested freezing, assignment notifications with mutation-callback
detection, and a fixed component definition with an unrequested reconfiguration
capability. They use synthetic vocabulary from development failure families;
they contain no evaluation repository names, selectors, streamed paths or
evaluation answers. Their full text is frozen in `experiment-definition.json`.

## Specific evaluation changes

| Decision | Current Flash | Revised Flash | Solution target |
| --- | --- | --- | --- |
| Earlier yielded GraphQL data | Unknown | Keep snapshots unchanged by later payload processing | Deep-copy accumulated data, including nested objects/lists |
| GraphQL result error lifetime | Unknown | Current-payload errors only | Do not accumulate prior payload errors |
| Supplied nonempty nosec selector resolving no tests | Unknown | No suppression | Normalize empty resolved/fallback selection to no effect |
| Compound-statement header suppression | Unknown | Do not extend to independent body statements | Restrict compound statement line range to its header |
| GraphQL stream index | Splice and shift | Splice and shift | Fill/overwrite addressed positions without shifting existing elements |

The same improvements occur with examples; no additional case changes.
Explicit requirements mostly work already. The largest gains are in inherited
and inferred behavior: across all tasks, Flash goes from one correct inferred
decision out of 11 to ten, and from zero inherited decisions out of two to two.

### Remaining streaming-index error

For `[A,B,C]`, streamed `[X]` at index 1 must produce `[A,X,C]` in the solution,
not `[A,X,B,C]`. Flash selects splicing in all three arms. Jev selects splicing
with current wording and addressed-position replacement with revised wording.

The revised agreement policy withholds this clarification and still emits the
prompt with original requirements. This avoids adding a known wrong explicit
policy, but leaves the original “insertion start index” wording ambiguous. It
does not ensure the coding agent chooses replacement. The next useful narrow
question concerns the before/after positions of existing elements and later
path references, rather than another general instruction to analyze carefully.

The experiment does not justify globally preferring Jev over Flash on conflicts.
Its 11/11 result here is a small sample on known, manually framed cases.

### Source audit: an over-specific answer choice

All arms/models chose the expected payload-local extensions policy. However,
the literal option says a payload without extensions yields `extensions=None`.
That is true at the new incremental helper's raw-response boundary, but is not
true for every wire payload: the solution's WebSocket parser puts `hasNext`
and `incremental` into `ExecutionResult.extensions`, and the common execution
path forwards those fields into the helper. See solution.patch:575–606 and
635–662. A transport payload without user extensions can therefore yield
nonempty control metadata in extensions.

The frozen cases, labels and nine prompts are retained as produced. This case
is excluded from the audited comparison in `source-audit.json`; it demonstrates
an error in manually supplied choices, not a model choosing the wrong option.
A suitable replacement meaning is: “Each result's extensions come from the
current payload processing; do not retain extensions from earlier payloads.”
An exact `None` assertion requires specifying the observation boundary first.
This correction has not been submitted to the models as a new experiment.

## Production routing/splitting and prompt assembly

Production sentence capture, Jev atomicity/routing and Flash splitting run for
GraphQL and Bandit. The previous production capture is reused for Koota.

| Task | Product clauses | Context | Excluded controller clauses |
| --- | ---: | ---: | ---: |
| Koota composite aspects | 25 | 1 | 2 |
| GraphQL incremental delivery | 23 | 0 | 2 |
| Bandit structured nosec | 34 | 3 | 2 |

Each decision sees the original instruction, original base-code excerpts and
routed inventory. Keeping the full original instruction alongside that inventory
isolates decision wording from potential routing omissions. The experiment does
not measure automatic question discovery or the effects of filtering context
out of a real downstream producer.

Nine complete-format prompts are generated: three tasks times three arms.
They preserve routed product clauses, place selected meanings directly in
acceptance criteria, retain original requirements for unresolved decisions,
and leave branching/commits to Powdrr. No contract rewrite or criterion
synthesis occurs. Unknown/disagreement never blocks prompt output.

- [Revised Koota prompt](prompts/revised/koota-composite-trait-aspects.md)
- [Revised GraphQL prompt](prompts/revised/gql-incremental-graphql-delivery.md)
- [Revised Bandit prompt](prompts/revised/bandit-structured-nosec-directives.md)

Only the 22 sampled decisions are scored. These prompts have not been audited
for every solution behavior or run through a coding agent. Conditions and
applicability outside each sampled facet—such as incomplete-entity aspect set
or exact callback arity—remain outside this comparison.

## Jev abstention mapping diagnostics

Four current-arm Jev calls initially raised a fail-closed ValueError before
the recorder received a response. These were retained as failures, rather
than counted as incorrect semantic answers. Flash's successful answers are
preserved independently.

The production request builder adds an `unresolved` choice. Its formatter
rejects that choice when the output is a single property named `choice`, even
when the experiment offers `unknown`. Four targeted diagnostic replays capture
the raw Jev response and normalize provider abstention to experiment unknown:

- GraphQL snapshot and Koota traits-array replays returned `unresolved`, directly
  confirming this mapping problem.
- Koota nested-freeze/reference replays returned the declared `unknown` label.
  Their initial raw responses were not captured, so the exact original failure
  choice cannot be proved.

Original failed checkpoints and diagnostic responses are both archived. The
reported semantic scores use normalized/replayed Jev answers, with this
limitation explicit. Original Flash answers needed no replays. Production
client code remains unchanged; normalization is isolated to the diagnostic.

## Boundaries and interpretation

- Gold choices and patch references are frozen before classification calls and
  stored separately. Classification payloads exclude labels and patches.
- Model inputs use original code at the task base commits. Retrieval, question
  design and choice meanings are manually authored by an agent who knows the
  solutions. This is cross-repository evaluation, not an untouched blind holdout.
- Candidate order is deterministic and shared by all arms. Neither model sees
  the other's answer; agreement is evaluated only after independent calls.
- A wrong choice on a required feature differs from an unsupported added
  guarantee. Missing/unknown, wrong, unsupported addition and provider failures
  have separate counts.
- Every selected meaning renders literally. Classification can still be wrong;
  deterministic rendering does not establish truth.
- Four hand-written examples do not improve this sample. That does not establish
  that larger or better selected example sets are useless.

The practical next step is to use revised wording while evaluating **automatic
question discovery and scope binding**. Fine-tuning should be reconsidered if
those tasks or remaining behavioral distinctions repeatedly fail across more
repositories after the task definition is stable. No fine-tuning or production
pipeline change is made here.

## Artifacts and reproduction

`cases.json`, `oracle.json`, `manifest.json` and `experiment-definition.json`
contain the frozen cases, separate labels, source hashes and arm definitions.
`sources/` and `routing/` preserve original packets and real production analysis.
`results/` contains all 66 paired decisions, including normalized diagnostics.
`scores.json` contains per-model, per-arm, split and decision-kind scores.
`prompts/` contains the nine complete-format prompt artifacts.
`source-audit.json` records the scope defect and scores excluding it.
`run-summary.json` records call totals and artifact verification.

Use the shared interpreter with this worktree's root and `src` on PYTHONPATH:
run `prepare.py`, then `run_experiment.py`; if the same single-field mapping
failure occurs, `diagnose_abstention.py` replays only failed mappings. Extract
`call-checkpoints.tar.gz` here first to replay saved calls without new requests.
Normalized diagnostics are reused only when messages, schema and model match.
Run `audit_artifacts.py` to verify saved artifacts and regenerate the source
audit and call summary without making model calls.

Verification uses scoped Ruff formatting/lint, mypy, frozen-input and
source/label boundary checks, exact rendering, score recomputation, checkpoint
hashes and credential checks. No tests or coding agents ran.
