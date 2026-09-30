# Python instruction-to-prompt audit: six additional task families

This audit exercises Structrr bootstrap and the production `harbor-feature
--design-only` pipeline against six local DeepSWE Python tasks. It also reviews
their instructions for classifier examples beyond the state-data fixture.
Powdrr source revision: `abb954471d986736081cc7945cac68d22828b532`.

These are host-side compiler probes, not Harbor/Pier benchmark runs. No coding
agent, solution patch, verifier tests, or implementation scores were used.
Each attempt cloned the task's repository, created a dedicated feature worktree
at its declared base commit, copied the original instruction unchanged, and
used the shared Powdrr Python environment with this worktree's `src` on
`PYTHONPATH`. No task dependencies were installed into the shared environment.

## Run evidence

Fresh artifacts are retained under:

- `/private/tmp/python-instruction-audit-20260929-more-r1/`
- `/private/tmp/python-instruction-audit-20260929-more-r2/`

Each task directory contains `audit-result.json`, the unchanged `instruction.md`,
`setup.log`, `bootstrap.yaml`, `bootstrap.log`, and `pipeline.log`. The pipeline
directory retains its instruction ledger, semantic artifacts, Procedrr event
log, and failure record when those stages were reached. The companion
[evidence summary](python-instruction-prompt-audit-2026-09-29.json) preserves
base commits, instruction hashes, failure records, clause counts, and model
decision values without copying credentials or full request logs into Git.

All initial bootstraps passed. Initial HTTPX and returns attempts failed before
instruction classification because host pytest configuration imported missing
`trio` and `covdefaults`, respectively. Fresh second attempts set
`PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` and
`PYTEST_ADDOPTS='-o addopts= -o filterwarnings='` to isolate compiler behavior.
This changes test collection conditions; it does not establish a usable task
test environment. Missing task dependencies still prevent complete collection.

| Task | Attempt | First compiler stop |
| --- | --- | --- |
| `mashumaro-flattened-dataclass-fields` | r1 | Opening `flatten` declaration: unresolved `temporal_scope` |
| `dateutil-rfc5545-timezone-interop` | r1 | Opening RFC interoperability summary: unresolved `has_precondition`, `source_predicate`, and `temporal_scope` |
| `httpx-streaming-json-iteration` | r2 | Clause 2 subject extraction: repeated `interface` quote lacks an occurrence |
| `returns-validated-error-accumulation` | r2 | Opening new-type declaration: `entity` parent gets descriptive polarity and `must` strength |
| `cattrs-partial-structuring-recovery` | r2 | Clause 1 entailment response says resolved/entailed but supplies `source_ambiguous` reason |
| `numba-stencil-boundary-modes` | r2 | Clause 3 entailment response supplies a reason code on a resolved result |

All six semantic failures occur before canonical design completion. A
ledger's clause count is not the number of successfully compiled obligations.
The later clauses below were reviewed as source examples, not claimed as
successfully classified by a run that stopped earlier.

The current design-only flow ends at `compile_canonical_feature_design`;
despite the CLI help mentioning prompt evaluation, it does not itself render
or validate the final worker prompt. Thus these runs exercise the instruction
side of the pipeline and identify blockers to reaching prompt assembly. A
successful design-only result would still need a separate production prompt
compilation/check before it counted as an instruction-to-prompt success.

## Confirmed gaps

### Absence of an optional modifier is confused with missing source meaning

Mashumaro starts with adding `flatten` to `field_options` so nested dataclass
fields merge into the parent dictionary. Its root resolves to `feature`, but
the model returns unresolved/source_underspecified for temporal scope. The
source specifies no release or event boundary; `unspecified` is already an
allowed temporal result. The opening dateutil umbrella clause similarly blocks
before the detailed API and timezone bullets can be compiled.

Add examples distinguishing an absent modifier from an ambiguous modifier.
Also distinguish a high-level requested capability, which needs refinement
from later clauses, from a completely unstated success predicate. Do not invent
RFC behavior merely to get past the readiness gate.

### New entity requests do not establish consistent child decisions

Returns explicitly requests a new `Validated` type and `Valid`/`Invalid`
subtypes. The root selects `entity`, then children select descriptive polarity
and `must` strength. The consistency validator correctly rejects the pair.
The deterministic required-polarity default currently covers `feature`,
`interface`, and `invariant`, but not `entity`. Entity examples need to separate
existing domain descriptions from requests to introduce new types. Any change
to branch defaults should preserve genuinely descriptive entity declarations.

### Exact extraction requires occurrence-aware examples

HTTPX decomposes its iterator requirement into the generated clause
`The interface must be an iterator interface.` The subject extractor returns
`{"quote": "interface", "occurrence": null}` although the word appears twice.
Binding fails with `occurrence is required for a repeated source quote`.
The behavior quote is valid; this is a subject-span failure, not a JSON parsing
failure. Add a repeated-name extraction example and a bounded correction path
that supplies the intended occurrence without changing the source.

### Valid semantic values can still violate response metadata contracts

Cattrs creates a partial contract for adding `partial_structure`, then one
entailment response returns `status: resolved`, `value: entailed`, and
`reason_code: source_ambiguous`. The response violates the closed response
contract even though its selected semantic value is allowed. The compiler
correctly refuses it. This suggests schema enforcement and bounded response
correction, not a new behavior-family label.

Numba reaches three partial contracts before encountering the same response
metadata error during entailment. The recurrence across an API-extension task
and a numerical-kernel task makes this a shared provider-boundary finding.

### Atomic decomposition can leave compound output behind

The returns split of its container-hierarchy sentence includes
`Validated supports value_or. Validated supports from_value.` as one statement.
The original clause was identified as compound, but a returned child still
contains two independently testable APIs. Recheck split children for atomicity
and preserve the source parent while making further splits. This is separate
from the later entity-branch stop.

### Failure records lose identities that the event log already knows

The observed failures have `stage: unknown` and null clause, contract, and model
response references. The event logs identify the failing clause and preceding
provider result. Carry those identities into `failure.json` so a classifier,
extraction, entailment, and test-collection failure can be distinguished without
reconstructing the entire event log. Collection exceptions also need a typed
unavailable inventory rather than aborting all instruction work.

## Examples to add to the classification corpus

The interpretations below are analyst expectations for future probes. They
are not replacement model outputs and were not injected into these runs.
Multiple labels or contracts can be needed after atomic decomposition.

| Source pattern | Example from reviewed task | Classification distinction to preserve |
| --- | --- | --- |
| New named types | Returns requests `Validated`, `Valid`, and `Invalid` | Entity introduction is requested work; existing-type background remains descriptive |
| Paired public APIs | HTTPX adds `iter_json()` and `aiter_json()` | Separate interface obligations, shared sync/async behavior |
| Cross-module export | Cattrs adds `partial_structure` to the converter and top level | Method availability and package export are distinct subjects |
| Declaration-time validation | Mashumaro validates flatten collisions at class creation | Event-bound validation, not failure deferred until serialization |
| Mutually exclusive options | Mashumaro prefix and rename options cannot coexist | Prohibited combination plus its validation behavior |
| Option-dependent defaults | Mashumaro `flatten_prefix=True` derives a field-name prefix | Boolean sentinel semantics differ from a literal string |
| Parent/child configuration | Flattened children retain their own configuration | Preservation rule, not unconditional inheritance of parent settings |
| Returned value versus identity | Returns `from_validated` returns the same instance | Identity oracle; equality alone is insufficient |
| Ordered error accumulation | Returns combines self's errors before the other's | Stable order plus tuple representation; not set equality |
| Operation-specific laws | Returns accumulates in `apply`, short-circuits in `bind` | Contracts for different operations must coexist |
| Invalid algebraic law | Returns explains why double swap does not hold | Exclusion of an interface law, not prohibition of `swap` itself |
| Decorator metadata | Returns preserves the wrapped function's name | Observable metadata preservation separate from exception conversion |
| Fallback versus completeness | Cattrs defaults can produce a value for failed fields | Partial value and failure status are independent axes |
| Recursive versus atomic treatment | Cattrs nested records are partial; collections are atomic | Population-specific operations and contrasting failure oracles |
| Explicit field exclusion | Cattrs excludes `init=False` fields from both field sets | Product prohibition on two result fields, not process-only text |
| Recoverable invalid input | Cattrs extra keys make completeness false while retaining a value | Error status does not imply rejection or no result |
| Media-type population boundary | HTTPX accepts `application/*+json`, rejects other type trees | Positive population and negative boundary share a suffix predicate |
| Encoding fallback | HTTPX detects JSON encoding when charset is absent | Absence condition, explicit-charset precedence, invalid-codec error |
| Interior versus terminal empty records | HTTPX ignores interior empty JSON-seq records but rejects final ones | Position changes the result; broad 'ignore empty records' loses meaning |
| Resource lifecycle | Streaming HTTPX iteration closes and consumes the response | Cleanup, second-use error, and in-memory repeatability differ |
| Round-trip serialization | Dateutil parses its timezone-aware string output back | Semantic equivalence including generated defaults, not textual equality |
| Conflicting data-source precedence | Dateutil inline VTIMEZONE overrides `tzids` | Explicit priority between sources; neither can be silently dropped |
| Equality and hash consistency | Dateutil recurrence equality agrees with hashing | Coupled law spanning methods |
| Read-only ordered views | Dateutil component properties expose tuples in insertion order | Mutability, representation, and order are independent constraints |
| Numeric edge conventions | Numba reflect differs from symmetric at the edge | Domain-specific index oracle; names alone do not define correctness |
| Environment prerequisite | Numba notes an llvmlite version constraint | Route to execution setup; do not turn it into product behavior |
| Delivery instruction | All tasks request a new branch and a commit | Process receipt and execution routing, excluded from product contracts |

Several examples expose missing *contract dimensions*, rather than missing
top-level dispositions: identity, order, atomicity, boundary position, method
laws, and precedence. Prefer explicit source-bound fields and verification
cases over adding one broad classifier label for every Python library feature.

## Follow-up priority

1. Make absent modifiers and new-entity child decisions consistent, with exact
   fixtures from mashumaro and returns.
2. Add occurrence-aware extraction examples and schema-valid bounded correction
   for extraction and entailment responses. Retain every failed response.
3. Revalidate split children and improve failure identity propagation.
4. Rerun these six tasks in their prepared environments, through final prompt
   generation, before claiming coverage of the later boundary and law examples.
   Compare every original requirement against the final prompt, not just against
   ledger membership.

This change documents evidence and proposed probes. It does not change the
production classifiers or relax any acceptance gate.
