# Source-Anchored Semantic Contract Compilation

## Purpose

This document specifies how Powdrr turns one immutable instruction clause into
a typed semantic contract without asking a model to design that contract in a
single response. It replaces a prose cascade such as:

```text
source clause
  -> model-authored obligation
  -> model-authored acceptance criterion
  -> model-authored population
  -> model-authored operation
  -> model-authored oracle
  -> model-authored evidence case
```

with source-anchored decisions and deterministic compilation:

```text
immutable source clause
  -> independent labels and exact-source extractions
  -> field-level entailment decisions
  -> partial semantic contract
  -> repository and ontology candidate lookup
  -> bounded candidate-match decisions
  -> resolved semantic contract
  -> deterministic prose and executable-contract projections
```

The target is reliable behavior from relatively weak models. The model's job
is limited to one classification, extraction, or candidate match at a time.
Powdrr owns identity, control flow, lookup, assembly, validation, provenance,
abstention, retries, and escalation.

This is an implementation specification. Names may be changed during
implementation only when the replacement preserves the contracts and
decision boundaries defined here.

## Scope

This design covers:

- atomic semantic decisions made after instruction clauses have been captured;
- exact-source extraction and finite-label classification;
- which decisions are candidates for specialized local classifiers;
- partial and resolved semantic-contract schemas;
- repository inventory construction;
- subject, behavior, predicate, and scope lookup;
- candidate generation, matching, scoring, selection, and abstention;
- deterministic contract assembly and derived prose;
- Procedrr control flow and Workrr operation responsibilities;
- provenance, invalidation, artifacts, evaluation, and rollout; and
- the conversion of all resolved contracts into one immutable mini-SWE-agent
  implementation prompt plus a private obligation validation manifest.

It does not specify code-task execution, interactive coding-agent control,
or proposal publication except where those consumers constrain the compiled
contract. It does specify the complete immutable prompt, the private validation
contract, and the required post-coding evidence flow at the
design-to-implementation boundary.

## Related documents

- `docs/plans/bounded-llm-instruction-compiler.md` owns source capture,
  sentence segmentation, and atomic-clause production.
- `docs/design/single-decision-feature-development.md` defines the governing
  single-decision normal form.
- `docs/plans/durable-intent-implementation-plan.md` defines durable source and
  semantic-contract ownership.
- `docs/design/structrr-procedrr-workrr-integration.md` defines authority and
  runtime context boundaries.
- `docs/design/external-coding-agent-boundary.md` defines the downstream worker
  boundary. This compiler supplies semantic inputs to that boundary; it does
  not move design judgment into the coding agent.

## Required properties

The implementation must satisfy these properties.

1. The original clause and source span remain immutable and authoritative.
2. No generated paraphrase becomes the source for a later semantic decision.
3. Each model activation makes exactly one semantic decision.
4. Exact extraction is preferred over generation; finite selection is
   preferred over open-ended construction.
5. Every semantic field records how it was obtained and what evidence supports
   it.
6. `unresolved` is a valid result and must never be silently replaced with a
   guess.
7. Repository facts come from versioned inventories, not model recollection.
8. Model-authored IDs, paths, symbols, selectors, and fingerprints are never
   accepted as authority.
9. Redundant obligation, acceptance, operation, and oracle prose is rendered
   deterministically from one typed contract.
10. Contract completion is a kernel-owned predicate over required fields and
    evidence, not a model opinion.
11. A classifier can be replaced by a specialized model without changing the
    Procedrr flow or the bound-result schema.
12. No contract advances to executable verification while a required semantic
    field is unresolved.
13. One completed design revision produces exactly one prompt for exactly one
    mini-SWE-agent invocation; obligations never become separate worker
    prompts, and validation never creates an in-run repair prompt.
14. The same design revision preserves a private, immutable validation manifest
    that maps every actionable obligation to the evidence required after
    coding. The prompt is the only worker-facing projection, not the only
    design artifact.
15. Every source proposition has exactly one terminal disposition receipt:
    represented as required behavior, represented as preservation or a
    non-goal, or excluded as proven process-only text. No proposition simply
    disappears between design and prompt compilation.
16. Every meaning-bearing modifier and every accepted derived semantic field
    reaches an identifiable prompt span and either an executable assertion or
    a typed replacement-evidence requirement.
17. Contracts that share a subject are compared before prompt compilation so
    distinct observation views, lifecycle phases, precedence rules, and
    boundary cases cannot be silently collapsed into one approximately similar
    behavior.
18. The implementation prompt is a lossless implementation projection of the
    canonical design. Concision may remove duplicate prose and internal
    metadata, but never a condition, exception, result, oracle, contrast, or
    preservation rule.

## Authority boundaries

| Authority | Responsibility |
| --- | --- |
| Structrr | Durable intent sources, accepted definitions, product entities, relationships, aliases, populations, and semantic revisions |
| Procedrr | Ordered single-decision flow, branch conditions, bounded loops, required decision schemas, and terminal outcomes |
| Workrr | Source binding, inventory creation, candidate generation, provider dispatch, response binding, deterministic assembly, evidence capture, and artifact persistence |
| Semantic classifier | One label, exact quotation, or candidate relation for one supplied subject |
| Language adapter | Repository symbols, type relationships, references, tests, and executable validation capabilities |
| Human | Resolution of meaning that is not entailed by the source or accepted definitions |
| mini-SWE-agent | Repository inspection, edits, and focused checks from the one compiled implementation prompt |

The same decision contract is used whether its provider is a general LLM,
Jev-like classifier, fine-tuned local encoder, or deterministic rule. Provider
choice is execution configuration and does not alter semantic meaning.

## Terminology

### Source proposition

A source proposition is one independently verifiable assertion preserved from
the user's instruction. Atomicity compilation produces propositions; semantic
compilation does not split or merge them.

### Exact-source extraction

An exact-source extraction copies a contiguous substring from the proposition.
The model returns text, not offsets. Workrr resolves the text to offsets and
rejects ambiguous occurrences unless the response also selects a
compiler-supplied occurrence index.

Span resolution is exact and deterministic:

```python
def resolve_span(source: str, quote: str, occurrence: int | None) -> Span:
    starts = every_exact_start(source, quote)
    if not starts:
        raise InvalidExtraction("quote is not an exact source substring")
    if len(starts) > 1 and occurrence is None:
        raise InvalidExtraction("occurrence is required for a repeated quote")
    selected = occurrence or 1
    if selected < 1 or selected > len(starts):
        raise InvalidExtraction("occurrence is outside the exact match set")
    start = starts[selected - 1]
    return Span(start=start, end=start + len(quote))
```

`every_exact_start` operates on the ledger's preserved proposition text. It
does not case-fold, trim, normalize punctuation, or perform fuzzy matching.

### Semantic label

A semantic label is a member of a closed, versioned enumeration. Labels are
stable API values rather than prompt-specific natural language.

### Partial contract

A partial contract contains source-derived fields and explicit unresolved
fields. It is valid as an intermediate artifact but cannot create an
implementation task.

### Repository binding

A repository binding connects a source concept to one or more inventory
records using evidence from an exact repository revision.

### Resolved contract

A resolved contract has all fields required for its disposition and can be
compiled into verification obligations. Resolution does not imply that the
feature is implemented.

## Common decision envelope

All semantic decisions are persisted in this provider-neutral envelope:

```yaml
schema_version: semantic-decision-v1
decision_id: decision:instruction-001:quantifier
decision_kind: quantifier
subject_ref: instruction-001
input_fingerprint: sha256:...
provider:
  kind: planning-llm | local-classifier | deterministic-rule | human
  name: optional-provider-name
  model_revision: optional-immutable-revision
result:
  value: every
  status: resolved | unresolved
  reason_code: null
evidence_refs:
  - source-span:instruction-001:0-3
created_at: 2026-09-22T00:00:00Z
```

Workrr adds every field except `result`. The provider cannot return identity,
provenance, evidence references, timestamps, or fingerprints.

`input_fingerprint` covers:

- decision kind and schema version;
- exact proposition text and source fingerprint;
- supplied candidate-set fingerprint, if any;
- accepted-definition revision, if any; and
- prompt or local-classifier contract revision.

A cached result is reusable only when this fingerprint is unchanged.

## Shared unresolved result

Every non-deterministic classifier supports abstention:

```json
{
  "status": "unresolved",
  "reason_code": "source_ambiguous"
}
```

Allowed reason codes are:

| Code | Meaning | Deterministic route |
| --- | --- | --- |
| `source_ambiguous` | More than one semantic reading is supported | Ask one human clarification question |
| `source_underspecified` | A required field is not stated | Consult accepted definitions, then ask the human |
| `no_candidate` | No supplied candidate matches | Expand deterministic lookup once, then clarify |
| `multiple_candidates` | More than one supplied candidate matches | Run disambiguation or clarify |
| `repository_evidence_missing` | The repository cannot currently substantiate a binding | Produce a discovery finding; do not code |
| `unsupported_concept` | No registered ontology or adapter supports the concept | Escalate as a capability gap |
| `classifier_abstained` | A classifier declined despite sufficient input | Try the configured fallback provider once |
| `invalid_response` | The response failed schema or extraction validation | Retry the same decision once with the exact diagnostic |
| `conflicting_evidence` | Sources or accepted definitions disagree | Block for semantic reconciliation |

Free-form reasons are diagnostic only and must not control routing.

## Classifier and extractor catalog

Each catalog entry is independently versioned. A Procedrr judge references the
decision kind, not a provider or prompt implementation.

### C01: disposition classifier

Purpose: determine whether a proposition describes product meaning or process
metadata.

Input:

```yaml
proposition_text: exact immutable text
neighbor_context: optional pronoun-resolution-only text
```

Output value:

```text
entity | feature | interface | invariant | guidance | non_goal |
nonactionable | unresolved
```

Rules:

- `nonactionable` is limited to process, delivery, and tool instructions that
  do not describe product behavior.
- An explicit prohibition on product behavior is `non_goal`.
- A universal rule over a population is `invariant`.
- The classifier does not produce an obligation or implementation task.

Specialized-classifier suitability: **high**. This is a small-label text
classification problem. Maintain separate per-label metrics because confusing
`non_goal` with `nonactionable` can erase intent.

### C02: polarity classifier

Purpose: identify whether the proposition requires, forbids, permits, or only
describes behavior.

Output value:

```text
required | prohibited | permitted | descriptive | unresolved
```

Examples:

| Source | Value |
| --- | --- |
| `All data should pickle.` | `required` |
| `Do not add retries.` | `prohibited` |
| `Clients may omit the field.` | `permitted` |
| `The current exporter is synchronous.` | `descriptive` |

Specialized-classifier suitability: **high**. Negation and modality examples
must be heavily represented in its evaluation set.

### C03: quantifier classifier

Purpose: identify the source-declared coverage of the subject population.

Output value:

```text
one | some | every | unspecified | unresolved
```

Rules:

- `every` covers explicit universal terms such as `all`, `every`, `always`, or
  an accepted domain construction known to be universal.
- Do not infer `every` merely because a requirement sounds normative.
- Conditions are represented independently by C06 and E03. For example,
  `Every active report can be exported` is `every` with precondition `active`,
  not a separate quantifier.

Specialized-classifier suitability: **high**. A deterministic lexical rule may
resolve obvious universal quantifiers before any model is called. A classifier
handles implicit or syntactically complex cases.

### C04: requirement-strength classifier

Purpose: preserve the source's normative strength separately from polarity.

Output value:

```text
must | should | may | descriptive | unspecified | unresolved
```

This field allows policy to distinguish hard requirements from preferences
without asking later prose generators to reinterpret modal verbs.

Specialized-classifier suitability: **high**. Resolve explicit modal tokens by
deterministic rule first.

### E01: subject extractor

Purpose: copy the smallest exact phrase naming what the proposition applies
to.

Output:

```json
{"quote": "data", "occurrence": 1}
```

Validation:

1. `quote` must be a non-empty exact substring of the proposition.
2. Matching is case-sensitive and Unicode-normalized exactly as the ledger.
3. `occurrence` is one-based and required only when the quote appears more
   than once.
4. The resolved span must not contain only a determiner or quantifier.
5. The span is stored by Workrr; the provider-supplied quote remains evidence.

Specialized-classifier suitability: **medium-high** as token-level sequence
labeling. Until a token classifier exists, use constrained extraction with
mechanical span validation.

### E02: behavior extractor

Purpose: copy the smallest exact phrase naming the behavior, state, or
prohibition.

Output and validation use the same contract as E01.

Examples:

| Source | Exact behavior quote |
| --- | --- |
| `All data should pickle.` | `pickle` |
| `Users can export reports as CSV.` | `export reports as CSV` |
| `Do not add retries to report exports.` | `add retries to report exports` |

Specialized-classifier suitability: **medium-high** as semantic-role or span
extraction. Evaluation must penalize spans that drop result modifiers such as
`as CSV`.

### C05: behavior-family classifier

Purpose: map an extracted behavior phrase to the nearest registered ontology
family without inventing operation semantics.

Initial values:

```text
create | read | update | delete | list | search | validate | transform |
serialize | deserialize | round_trip | persist | retrieve | compare |
invoke | emit | receive | authorize | authenticate | retry | render |
configure | other | unresolved
```

The catalog is intentionally broad. Repository-specific behavior concepts are
resolved later through ontology lookup. `other` preserves a supported but
uncatalogued behavior; `unresolved` means the classifier cannot determine the
family.

Specialized-classifier suitability: **high after sufficient labeled data**.
Use a hierarchical classifier if the catalog grows: first classify the broad
family, then select a repository ontology concept.

### C06: modifier-presence classifiers

There are three separate boolean-or-unresolved classifiers:

- `has_precondition`
- `has_exception`
- `has_explicit_result`

Each returns:

```text
present | absent | unresolved
```

They must not share one multi-field response. When `present`, Procedrr invokes
the corresponding exact-source extractor for one modifier. Multiple modifiers
are handled by a compiler-produced bounded candidate list and one inclusion
decision per candidate.

Specialized-classifier suitability: **high**.

### E03 through E05: modifier extractors

These extract exact spans for:

- precondition;
- exception; and
- explicit result.

The extractor returns one span at a time. Workrr obtains bounded candidate
segments from punctuation and conjunction analysis, then asks whether each
segment is the relevant modifier. The model is not asked to return an
unbounded list.

Specialized-classifier suitability: **medium**. A token-labeling model is a
natural replacement, but source-span validation remains mandatory.

### C07: temporal-scope classifier

Purpose: identify explicit temporal applicability.

Output value:

```text
current | future | current_and_future | event_bound | unspecified | unresolved
```

Do not derive `current_and_future` from `every` in the classifier. Structrr may
apply a separately versioned invariant policy after classification. That
derived scope must cite the policy rather than the source clause.

Specialized-classifier suitability: **medium-high**.

### C08: source-predicate classifier

Purpose: determine whether the source itself states a success predicate.

Output value:

```text
explicit | implied_by_registered_term | not_stated | unresolved
```

`implied_by_registered_term` is legal only when the input includes an exact
accepted-definition candidate. The classifier cannot rely on general world
knowledge to create a predicate.

Specialized-classifier suitability: **medium**. The boundary between lexical
implication and unstated design choice needs a carefully curated evaluation
set.

### C09: candidate-relation classifier

Purpose: evaluate one repository or ontology candidate against one source
concept.

Question represented by the decision:

> Does this one candidate denote the source concept in this proposition under
> the supplied accepted context?

Output value:

```text
matches | does_not_match | insufficient_evidence
```

It receives exactly one candidate. It must not rank a list or return a
candidate ID. Workrr binds the decision to the candidate under review.

Specialized-classifier suitability: **medium**. Lexical matches can be handled
deterministically. Semantic candidate relations may require a cross-encoder or
general model.

### C10: entailment classifier

Purpose: independently check one assembled field against one authoritative
source.

Output value:

```text
entailed | contradicted | not_stated | unresolved
```

Input includes one proposition and one normalized field assertion, for
example:

```yaml
source: All data should pickle.
field_assertion: The quantifier is every.
```

It never reviews a whole contract. Fields derived from repository evidence or
accepted definitions are reviewed against those authorities separately.

Specialized-classifier suitability: **medium-high** using a calibrated natural
language inference model. Because entailment mistakes can expand intent, use a
strict abstention threshold and retain a stronger-provider fallback.

### C11: proposition-coverage classifier

Purpose: verify atomic decomposition without comparing whole documents.

For one source proposition and one child proposition, output:

```text
fully_represented | partly_represented | not_represented | invented | unresolved
```

Workrr constructs a coverage matrix and enforces:

- every source proposition is fully represented by exactly one child, or by a
  declared non-overlapping set when it cannot be expressed atomically;
- no child is `invented`;
- no child is accepted solely as `partly_represented`; and
- polarity, quantifier, conditions, exceptions, and concrete names pass their
  own conservation checks.

Specialized-classifier suitability: **medium**. This is a semantic textual
similarity/NLI task and should initially retain a general-model fallback.

### C12: nonactionable-exclusion-safety classifier

Purpose: make exclusion of a proposition from the product prompt a separate,
fail-closed decision rather than a side effect of C01.

This classifier runs only when C01 returns `nonactionable`. It answers one
question:

> Does this exact proposition contain any requested product state, behavior,
> prohibition, compatibility rule, or observable result in addition to process
> or delivery instructions?

Output value:

```text
process_only | product_semantics_present | mixed | unresolved
```

Rules:

- `process_only` permits exclusion from product behavior while retaining an
  auditable source-disposition receipt and, for an imperative process
  instruction, a bound Procedrr operation or policy route.
- `product_semantics_present` invalidates the C01 result and routes the
  proposition back through disposition classification.
- `mixed` invalidates the proposition's atomicity. Workrr returns it to the
  atomic-clause compiler, which must produce separately covered child
  propositions before semantic compilation resumes.
- `unresolved` blocks prompt compilation.
- A source proposition classified as `non_goal`, `guidance`, or any actionable
  product disposition never reaches this classifier and cannot be excluded by
  it.

Examples:

| Proposition | Result | Route |
| --- | --- | --- |
| `Open a pull request after implementation.` | `process_only` | Exclude from product prompt; retain receipt |
| `Do not add retries.` | `product_semantics_present` | Reclassify as `non_goal` |
| `Add state data and then open a pull request.` | `mixed` | Re-split into two propositions |
| `Use a dictionary so callers can mutate values.` | `product_semantics_present` | Reclassify as product guidance or interface behavior |

Specialized-classifier suitability: **medium-high**. False `process_only`
results erase intent, so promotion requires high per-label recall for
`product_semantics_present` and `mixed`, calibrated abstention, and adversarial
fixtures containing product and process language in the same sentence.

### C13: contract-observation-relation classifier

Purpose: decide one semantic relation between two resolved contracts that
share a subject, repository binding, state value, or output type when ontology
metadata does not resolve the relation mechanically.

The classifier receives exactly two contracts, their exact source spans, and
one compiler-selected relation candidate. It does not receive a list to rank.

Output value:

```text
same_observation | distinct_observations | ordered_phases | precedence |
mutual_exclusion | preservation_boundary | independent | unresolved
```

Examples:

| Contract A | Contract B | Relation |
| --- | --- | --- |
| Callback receives ancestor-plus-child data | Snapshot returns each state's owned data | `distinct_observations` |
| Data exists during `on_enter` | Data is removed after `on_exit` | `ordered_phases` |
| Child key shadows equal parent key | Parent value is otherwise inherited by callback scope | `precedence` |
| State data can be pickled | Diagrams annotate data-bearing states | `independent` |

`same_observation` is legal only when both contracts bind the same operation,
view, lifecycle phase, scope, and predicate. Shared nouns or return types are
not sufficient. `unresolved` blocks boundary compilation and routes to one
human clarification question.

Specialized-classifier suitability: **medium**. Exact ontology relations and
operation identities should resolve deterministically first. Semantic
relations retain an LLM fallback until a pairwise held-out suite demonstrates
high recall for `distinct_observations`, `ordered_phases`, and `precedence`.

## Classifier execution policy

### Deterministic rule first

For each decision kind, Workrr may register a deterministic resolver. It can
return only:

```text
resolved(value, evidence) | no_decision
```

It must never return `unresolved`; lack of a rule match falls through to the
configured classifier. Examples include explicit `all` -> `every`, explicit
`must not` -> `prohibited`, and exact accepted aliases.

### Provider cascade

The initial cascade is:

1. deterministic rule;
2. specialized local classifier, when promoted for this decision kind;
3. configured planning LLM fallback;
4. human clarification or capability finding.

The cascade is declared by decision kind. A provider cannot choose its own
fallback. The same bound-result validator runs after every provider.

### Retry policy

- Retry once only for `invalid_response` or a transient provider failure.
- Include the invalid response and one exact schema diagnostic on a shape
  retry.
- Do not retry `source_ambiguous`, `source_underspecified`, `no_candidate`, or
  `multiple_candidates` with wording that pressures the provider to guess.
- Do not convert repeated disagreement into majority voting unless the
  classifier has a pre-registered ensemble policy.

### High-risk agreement

Polarity, universal quantification, exceptions, and prohibitions may be marked
`dual_confirmation`. Workrr runs two configured providers with independently
ordered labels. Agreement resolves the field. Disagreement becomes
`conflicting_evidence`; provider self-reported confidence does not break the
tie.

## Partial semantic contract

The compiler emits this canonical intermediate record:

```yaml
schema_version: partial-semantic-contract-v1
contract_id: contract:instruction-001
source_ref: instruction-001
source_fingerprint: sha256:...
disposition: invariant
polarity: required
requirement_strength: should
quantifier: every
subject:
  source_span: {start: 4, end: 8}
  source_text: data
  binding_status: unresolved
  binding_refs: []
behavior:
  source_span: {start: 16, end: 22}
  source_text: pickle
  family: serialize
  ontology_status: unresolved
  ontology_ref: null
preconditions: []
exceptions: []
explicit_result: null
temporal_scope:
  value: unspecified
  derivation: source
predicate:
  status: unresolved
  ontology_ref: null
field_provenance:
  polarity: decision:instruction-001:polarity
  quantifier: decision:instruction-001:quantifier
  subject: decision:instruction-001:subject
  behavior: decision:instruction-001:behavior
unresolved:
  - field: subject.binding_refs
    reason_code: repository_evidence_missing
  - field: behavior.ontology_ref
    reason_code: no_candidate
  - field: predicate
    reason_code: source_underspecified
fingerprint: sha256:...
```

Only Workrr's compiler can create this record. Inputs are bound semantic
decisions, never an arbitrary model-returned mapping.

## Repository inventory

Repository lookup operates against one immutable
`semantic-repository-inventory-v1` tied to the current commit, Structrr
revision, and adapter revisions.

### Required inventory records

Each record contains:

```yaml
inventory_id: python:src/models/user.py::UserRecord
kind: class
canonical_name: UserRecord
qualified_name: package.models.user.UserRecord
normalized_terms: [user, record]
aliases: [user record]
path: src/models/user.py
span: {start_line: 18, end_line: 71}
language: python
component_refs: [component:model]
structrr_refs: [entity:user-record]
relationship_refs:
  - relationship:implements:persisted-entity
capabilities: [construct, compare]
test_refs:
  - pytest:tests/models/test_user.py::test_user_record
evidence_fingerprint: sha256:...
```

The inventory must include:

- Structrr entities, aliases, taxonomic kinds, and declared populations;
- symbols from each supported language adapter;
- module and component ownership;
- inheritance, implementation, registration, containment, and call
  relationships;
- public APIs and interface operations;
- fixtures and constructible examples;
- test selectors and their referenced subjects;
- serialization, persistence, schema, and registration metadata; and
- accepted semantic definitions and behavior ontology entries.

Inventory producers own syntax recognition. The planning model never invents
symbols or parses source files to populate this artifact.

### Normalization

Workrr computes matching forms without changing authoritative names:

1. Unicode NFKC normalization.
2. Case folding.
3. Split snake case, kebab case, camel case, qualified names, and whitespace.
4. Preserve both singular and plural forms; use a language-independent
   inflection table only for candidate retrieval, never semantic acceptance.
5. Remove determiners for retrieval (`a`, `an`, `the`, `all`, `every`), but
   retain them in the source contract.
6. Do not remove domain nouns as stop words.
7. Expand aliases only from versioned Structrr or ontology records.

Normalization produces search keys. It does not prove a match.

## Subject lookup and population resolution

### Step 1: query construction

Construct a `semantic-lookup-query-v1` from:

- exact subject source text;
- its normalized terms;
- disposition and behavior family;
- explicitly named APIs or types elsewhere in the proposition;
- relevant accepted Structrr context; and
- the inventory fingerprint.

Do not include model-generated obligation prose.

### Step 2: deterministic candidate retrieval

Retrieve candidates in this order:

1. exact canonical-name match;
2. exact accepted-alias match;
3. exact qualified-name suffix match;
4. exact normalized-token-set match;
5. declared Structrr population name match;
6. explicitly declared relationship expansion from a matched entity;
7. referenced symbol expansion from matched interfaces or tests; and
8. lexical token overlap for recall.

Candidate retrieval may return at most 32 records before relationship
expansion and at most 64 afterward. If the limit is exceeded, Workrr records
`candidate_overflow` and narrows by component, explicit names, or accepted
context. It must not truncate silently.

### Step 3: deterministic evidence score

Scores order candidates; they do not establish semantic truth.

| Evidence | Points |
| --- | ---: |
| Exact canonical name | 100 |
| Exact accepted alias | 95 |
| Exact Structrr population name | 95 |
| Qualified-name suffix | 90 |
| Exact normalized token set | 85 |
| Explicit source binding from applicable Structrr entity | 80 |
| Declared registration or membership relationship | 75 |
| Existing test explicitly references subject | 60 |
| Same relevant component | 25 |
| Per-token lexical overlap | 5, maximum 20 |
| Contradictory kind or language evidence | -100 |

Deduplicate candidates by `inventory_id`, retaining all evidence reasons.
Sort by score descending and then `inventory_id` ascending.

The retrieval implementation follows this shape:

```python
def retrieve_candidates(query: LookupQuery, inventory: Inventory) -> CandidateSet:
    found: dict[InventoryId, CandidateEvidence] = {}
    for matcher in REGISTERED_MATCHERS_IN_PRECEDENCE_ORDER:
        for record, evidence in matcher.find(query, inventory):
            found.setdefault(record.id, CandidateEvidence()).merge(evidence)

    candidates = [
        Candidate(record=inventory.require(candidate_id), evidence=evidence)
        for candidate_id, evidence in found.items()
    ]
    candidates = reject_kind_contradictions(query, candidates)
    candidates = score_without_semantic_acceptance(candidates)
    candidates.sort(key=lambda item: (-item.score, str(item.record.id)))
    enforce_candidate_bounds_or_raise(candidates)
    return CandidateSet(
        query_fingerprint=query.fingerprint,
        inventory_fingerprint=inventory.fingerprint,
        candidates=tuple(candidates),
    )
```

`REGISTERED_MATCHERS_IN_PRECEDENCE_ORDER` is a versioned tuple. Adding,
removing, or reordering a matcher changes the lookup-contract revision and
invalidates candidate sets. Matchers return inventory records plus evidence;
they never return an authoritative semantic binding.

Automatic binding is allowed only for:

- one exact canonical or exact alias match with no contradictory evidence; or
- one applicable Structrr source binding that already declares the source
  phrase as an alias.

Scores alone never authorize automatic binding.

### Step 4: candidate relation decisions

When automatic binding is unavailable, evaluate one candidate at a time using
C09. Supply:

- the exact proposition;
- the exact subject span;
- one candidate's canonical name, kind, declared description, and evidence;
- directly relevant accepted context; and
- no other candidates.

Workrr aggregates results:

| Match results | Outcome |
| --- | --- |
| Exactly one `matches` | Bind that candidate |
| Zero `matches`, at least one `insufficient_evidence` | `repository_evidence_missing` |
| Zero `matches`, all negative | Expand lookup once, then `no_candidate` |
| More than one `matches` that represent the same declared population | Bind the population record |
| More than one unrelated `matches` | `multiple_candidates` |

The classifier cannot choose a winner from multiple matches. A separate
disambiguation decision compares one declared relationship at a time, or the
flow asks the human.

Aggregation is independent of candidate score:

```python
def finalize_binding(
    candidate_set: CandidateSet,
    decisions: Sequence[BoundCandidateDecision],
) -> BindingResult:
    require_exact_decision_coverage(candidate_set, decisions)
    matches = candidates_with_value(decisions, "matches")
    uncertain = candidates_with_value(decisions, "insufficient_evidence")

    if len(matches) == 1:
        return Bound(matches[0].candidate_id)
    if not matches and uncertain:
        return Unresolved("repository_evidence_missing")
    if not matches:
        return Unresolved("no_candidate")
    common_population = one_declared_population_containing_all(matches)
    if common_population is not None:
        return Bound(common_population.inventory_id)
    return Unresolved("multiple_candidates")
```

`require_exact_decision_coverage` verifies one current decision per candidate
fingerprint. Partial evaluation cannot accidentally select from an incomplete
candidate set.

### Step 5: population construction

Quantifier determines how the binding becomes a population:

- `one`: one bound inventory record;
- `some`: a bounded scenario requires an explicit subset rule or
  clarification;
- `every`: resolve a declared population and enumerate its current members;
- `unspecified`: retain a singular subject only when the grammar and binding
  establish one concrete member; otherwise clarify.

After this base population is resolved, preconditions filter its members or
invocation scenarios. They never alter the quantifier. `Every active report`
therefore means `every` member of the report population for which the compiled
`active` predicate is true.

A universal contract requires a `population-enumeration-receipt-v1`:

```yaml
population_ref: population:structrr-data-entities
inventory_fingerprint: sha256:...
membership_rule_ref: relationship:registered-as-data-entity
members:
  - python:src/a.py::A
  - python:src/b.py::B
complete: true
enumerated_at: 2026-09-22T00:00:00Z
```

`complete` can be true only when membership is defined by a deterministic
registry, type relation, Structrr relationship, schema inventory, or another
adapter-owned enumeration rule. A model-generated list is never complete.

Future-member applicability is represented by the membership rule, not by
pretending future members were enumerated. Current evidence covers current
members; a repository invariant or generated contract test enforces the rule
for future additions.

## Behavior ontology lookup

Behavior ontology records are versioned data:

```yaml
ontology_id: behavior:python-pickle-round-trip
family: round_trip
canonical_terms: [pickle round-trip]
aliases: [pickle, picklable, supports pickle]
operation_template:
  adapter: python
  operation: pickle.loads(pickle.dumps(subject))
predicate_options:
  - predicate: no_exception
  - predicate: same_type
  - predicate: equality
  - predicate: semantic_equivalence
required_capabilities: [python-runtime]
```

Aliases retrieve candidates but do not by themselves select a predicate. For
example, `supports pickle` may identify the pickle ontology while leaving the
success predicate unresolved.

Lookup uses exact term, accepted alias, behavior family, and repository
language. Candidate relation decisions use C09. Unsupported repository
languages produce `unsupported_concept`, not an improvised test command.

## Predicate resolution

Predicates are resolved in this precedence order:

1. An explicit result span in the proposition.
2. An accepted definition directly referenced by the proposition's term.
3. An active Structrr invariant or interface contract applicable to the bound
   subject and behavior.
4. A human clarification.

General model knowledge is not an authority.

Each predicate candidate is a typed record:

```yaml
predicate_id: predicate:semantic-equivalence
kind: semantic_equivalence
parameters: {}
authority_ref: definition:pickle-support:v2
render_template: The restored subject has the same observable semantic state as the original subject.
assertion_adapter: python-semantic-equivalence
```

If several predicates are available and no authority selects one, the flow
asks a clarification question listing their user-facing meanings. It does not
ask the classifier which quality level is preferable.

## Scope derivation

Scope fields can come from three authorities:

1. explicit source text;
2. accepted Structrr policy; or
3. repository membership structure.

Every derived field records one of:

```text
source | accepted_definition | structrr_policy | repository_evidence | human
```

For example, `all` establishes universal quantification, but it does not by
itself establish whether future entity types are covered. An accepted invariant
policy may derive `current_and_future`; its provenance must cite that policy.

## Assembly algorithm

The contract compiler performs the following deterministic stages.

### A1: validate source decisions

1. Require one current result for every classifier enabled by disposition.
2. Validate exact spans and enumeration values.
3. Reject duplicate decision kinds for the same subject and input fingerprint.
4. Reject stale results whose source or decision contract changed.
5. Preserve unresolved results rather than substituting defaults.

### A2: build the partial contract

Copy source-derived labels and spans into the canonical schema. Add provenance
references. Do not render obligation prose yet.

### A3: field-level source review

Construct one normalized assertion for every interpreted source field and run
C10 unless the field was resolved by a deterministic exact rule.

Routes:

- `entailed`: retain the field;
- `contradicted`: fail compilation with `intent_contradiction`;
- `not_stated`: remove the value and mark the field unresolved unless another
  declared authority supports it; and
- `unresolved`: invoke the configured fallback once, then clarify.

### A4: resolve subject and population

Run repository lookup, candidate relation decisions, and population
enumeration exactly as specified above. Store inventory and membership-rule
fingerprints.

### A5: resolve behavior

Run ontology lookup and bind one behavior concept. Confirm that a language
adapter supports its operation template for every population member category.

### A6: resolve predicate

Apply predicate precedence. Bind one assertion adapter and accepted authority.
Do not manufacture comparison semantics from the evidence example.

### A7: apply conditions and exceptions

Compile exact modifier spans into typed predicates using registered expression
adapters. If no adapter can compile a required modifier, mark the contract
`unsupported_concept`. Conditions narrow applicability; exceptions subtract
members or scenarios. Neither is prose pasted into a test prompt as a
substitute for compilation.

### A8: evaluate completion

The required-field matrix is disposition-specific:

| Disposition | Required fields |
| --- | --- |
| `entity` | subject binding and declared entity facts |
| `feature` | polarity, subject binding, behavior binding, predicate |
| `interface` | polarity, interface binding, operation, input/output predicate |
| `invariant` | polarity, quantifier, complete population rule, operation, predicate |
| `guidance` | subject or phase selector, preference value, enforcement level |
| `non_goal` | prohibited behavior, applicability scope, absence predicate |
| `nonactionable` | source trace and disposition only |

Completion is true only when every required field is resolved, every authority
reference exists, and every applicable adapter is available.

### A9: emit resolved contract

Example:

```yaml
schema_version: semantic-contract-v1
contract_id: contract:instruction-001
source_ref: instruction-001
disposition: invariant
polarity: required
requirement_strength: should
quantifier: every
subject:
  population_ref: population:structrr-data-entities
  membership_rule_ref: relationship:registered-as-data-entity
behavior:
  ontology_ref: behavior:python-pickle-round-trip
  family: round_trip
  mechanism: pickle
operation:
  adapter: python
  template_ref: operation:python-pickle-round-trip
predicate:
  predicate_ref: predicate:semantic-equivalence
  assertion_adapter: python-semantic-equivalence
scope:
  temporal: current_and_future
  derivation: structrr_policy
  authority_ref: invariant:data-serialization:v2
provenance:
  source_decisions: [decision:instruction-001:polarity, decision:instruction-001:quantifier]
  repository_inventory: sha256:...
  ontology_revision: sha256:...
  accepted_authorities: [invariant:data-serialization:v2]
fingerprint: sha256:...
```

### A10: compile semantic dimensions and partitions

Before rendering, Workrr derives the finite semantic dimensions that can alter
the observable result of each resolved contract. A dimension is accepted only
when its values come from an exact source modifier, a bound ontology concept,
an accepted Structrr definition or invariant, a repository API contract, or a
language-adapter rule. General model knowledge cannot create dimension values.

Dimension binding is deterministic where possible:

1. Collect exact precondition, exception, explicit-result, temporal-scope, and
   behavior spans from the partial contracts.
2. Ask the bound operation ontology and language adapter for candidate axes and
   values applicable to those spans; providers cannot invent candidates.
3. Bind exact lexical and accepted-definition matches mechanically.
4. For every remaining span/candidate pair, reuse C09 to decide only
   `matches`, `does_not_match`, or `insufficient_evidence`.
5. Aggregate accepted candidates. Zero matches for a meaning-bearing span is
   `unsupported_concept`; multiple incompatible matches is
   `multiple_candidates`. Both block compilation.
6. Apply accepted Structrr invariants and repository API contracts only after
   source-derived values, preserving separate authority references.

The immutable record is:

```yaml
schema_version: semantic-dimension-v1
dimension_id: dimension:state-data:declaration-presence
subject_binding_refs:
  - python:statemachine.state.State.data
axis: declaration_presence
authority_refs:
  - source-span:instruction-017:42-60
values:
  - value_id: absent
    predicate: no_data_argument_declared
  - value_id: explicit_empty
    predicate: data_argument_equals_empty_mapping
  - value_id: nonempty
    predicate: data_argument_has_members
completeness: complete_for_authority
fingerprint: sha256:...
```

Initial adapter-neutral axes are:

- declaration presence: absent, explicit empty, nonempty, and invalid forms
  stated by the design;
- activity or availability: active, inactive, before creation, and after
  removal when applicable;
- observation view: owned storage, effective callback scope, public snapshot,
  serialized form, or rendered form;
- structural position: root, ancestor, child, parallel sibling, or population
  member when those relationships are bound;
- lifecycle phase: before entry, during entry, active, during exit, after exit,
  ordinary reentry, and history restoration when applicable;
- polarity and validity: accepted input, rejected input, prohibited behavior,
  or permitted omission; and
- variant: each explicitly distinct mode such as shallow versus deep history.

This is not an instruction to generate a Cartesian product. The compiler keeps
only partitions that can change an operation's applicability or expected
predicate. Every retained value records its authority. If the source promises
`dict or None` but does not say which state produces `None`, and neither an
accepted API definition nor repository compatibility evidence resolves it,
the contract remains unresolved and Procedrr asks one targeted clarification;
the compiler does not guess.

### A11: compile the modifier-conservation ledger

Workrr creates one row for every meaning-bearing source span and every accepted
derived field:

Before creating field rows, Workrr emits one disposition receipt per source
proposition:

```yaml
schema_version: source-disposition-receipt-v1
receipt_id: disposition:instruction-031
source_ref: instruction-031
source_fingerprint: sha256:...
c01_decision_ref: decision:instruction-031:disposition
disposition: nonactionable
exclusion_safety_decision_ref: decision:instruction-031:exclusion-safety
terminal_route: process_only_exclusion
child_proposition_refs: []
prompt_inclusion: forbidden
procedural_route_ref: procedrr:publish-feature-pr
metadata_exclusion_reason: null
fingerprint: sha256:...
```

For actionable dispositions, `terminal_route` is
`required_behavior`, `preservation`, or `non_goal`, and
`exclusion_safety_decision_ref` is absent. For `mixed`, the parent receipt names
all child proposition references and is not terminal until C11 proves complete,
non-overlapping child coverage and every child has its own terminal receipt.
Exactly one terminal receipt is permitted for each leaf proposition.

A process-only imperative must bind `procedural_route_ref` to a registered
Procedrr operation or policy that owns it; for example, publication belongs to
the publication flow rather than the coding prompt. Descriptive delivery
metadata may instead set a closed `metadata_exclusion_reason`. Neither route
creates product code work. A process instruction with neither route remains
unresolved, ensuring that `nonactionable` means “handled outside product
semantics,” not “discarded.”

```yaml
schema_version: semantic-conservation-row-v1
row_id: conservation:instruction-017:result-none
source_ref: instruction-017
source_span: {start: 42, end: 60}
semantic_field_refs:
  - contract:instruction-017:predicate
authority_refs:
  - source-span:instruction-017:42-60
required_destinations:
  prompt: required_behavior
  verification: executable_assertion
prompt_fragment_ref: null
verification_assertion_refs: []
status: unresolved
fingerprint: sha256:...
```

Rows are required for polarity, quantifier, subject, operation, input and
output shapes, every precondition, exception, explicit result, temporal scope,
population rule, precedence rule, preservation constraint, and non-goal.
Rows derived from accepted definitions or repository contracts cite those
authorities instead of pretending the source stated them.

Disposition controls the required route:

| Disposition | Required terminal route |
| --- | --- |
| `entity` | Required design fact plus static, repository, or executable existence evidence |
| `feature`, `interface`, `invariant` | Required behavior plus verification or typed exemption |
| `guidance` | Required behavior or `Preserve and avoid`, according to enforcement level |
| `non_goal` | `Preserve and avoid` plus absence/preservation evidence |
| `nonactionable` | C12 `process_only` receipt and explicit prompt exclusion |
| unresolved or mixed | No handoff may be emitted |

No row may have zero routes or more than one conflicting terminal route. A
nonactionable row is covered by its exclusion receipt; it must not be counted
as an actionable contract and must not leak back through the objective,
repository summary, required tests, or completion protocol.

### A12: compile cross-contract interactions

Workrr generates bounded contract pairs using shared subject bindings,
overlapping repository symbols, operation output types, lifecycle resources,
and explicit relationship edges. It does not compare every contract to every
other contract.

For each candidate pair:

1. Resolve exact ontology relationships first.
2. Compare operation, observation view, phase, scope, preconditions,
   exceptions, precedence, and predicate.
3. If all fields are equal, record `same_observation` and permit deterministic
   deduplication only when source coverage remains one-to-one.
4. If a field differs, record the typed relation mechanically when possible;
   otherwise run C13 for that one candidate relation.
5. Reject `same_observation` when any meaning-bearing field differs.
6. Require a human clarification when C13 remains unresolved.

The resulting record is:

```yaml
schema_version: contract-interaction-v1
interaction_id: interaction:state-data:callback-vs-snapshot
contract_refs:
  - contract:callback-state-data
  - contract:state-data-values
shared_subject_refs:
  - python:statemachine.state.State.data
relation: distinct_observations
difference_fields:
  - observation_view
left:
  view: effective_callback_scope
  includes: [active_ancestor_data, own_data]
right:
  view: owned_state_snapshot
  includes: [own_data]
must_not_conflate: true
authority_refs:
  - source-span:instruction-008:...
  - source-span:instruction-017:...
status: resolved
fingerprint: sha256:...
```

Required interaction kinds are:

- `distinct_observations`: similar APIs or contexts expose intentionally
  different views;
- `ordered_phases`: behavior changes before, during, or after lifecycle events;
- `precedence`: both values apply but one shadows or overrides another;
- `mutual_exclusion`: exactly one mode or branch applies;
- `preservation_boundary`: a new behavior must not alter another contract;
  and
- `same_observation`: exact semantic duplication eligible for controlled
  rendering deduplication.

### A13: compile minimal discriminating contrast cases

Every `distinct_observations`, `ordered_phases`, `precedence`, or
`mutual_exclusion` interaction must produce at least one case in which an
implementation that collapses the distinction yields a different observable
result. Each source-derived partition with two behaviorally distinct values
must do the same.

```yaml
schema_version: contrast-case-spec-v1
case_id: contrast:state-data:callback-vs-snapshot
interaction_ref: interaction:state-data:callback-vs-snapshot
contract_refs:
  - contract:callback-state-data
  - contract:state-data-values
setup:
  parent_owned_data: {x: 1}
  child_owned_data: {y: 2}
observations:
  - operation: invoke_child_callback
    expected: {x: 1, y: 2}
  - operation: read_state_data_values_child
    expected: {y: 2}
discriminating_predicate:
  kind: unequal_observations
  forbidden_result:
    read_state_data_values_child: {x: 1, y: 2}
prompt_requirement: required
independent_probe_requirement: required
authority_refs:
  - interaction:state-data:callback-vs-snapshot
fingerprint: sha256:...
```

Contrast-case rules:

1. Use the smallest fixture that makes the semantic difference observable.
2. State both the required result and the plausible-but-wrong result when the
   wrong result can be derived from the neighboring contract.
3. Never invent a forbidden result merely to create a contrast.
4. Bind every setup value and operation through repository inventory or a
   separately accepted fixture-design decision.
5. Require an adapter-owned independent probe whenever the adapter can execute
   the contrast.
6. Preserve each side's separate contract mapping even when one durable test
   contains both assertions.
7. If two partition values are observably equivalent, record that result and
   do not manufacture a test solely for structural coverage.

For the state-data feature, this stage must produce at least:

- callback merged scope versus each state's owned snapshot;
- no data declaration versus explicit `data={}`;
- active declared data versus inactive declared data;
- mutation persistence through callbacks versus immutability across callbacks;
- shallow-history restoration versus deep-history restoration; and
- non-dict declarations versus mappings with non-string keys as independently
  attributable invalid cases.

The boundary compiler is a pure function over versioned inputs:

```python
def compile_semantic_boundaries(inputs: BoundaryInputs) -> BoundaryResult:
    dispositions = finalize_all_source_dispositions(
        inputs.source_ledger,
        inputs.disposition_decisions,
        inputs.exclusion_safety_decisions,
        inputs.atomic_coverage,
    )
    if findings := validate_terminal_disposition_conservation(dispositions):
        return Failed(findings)

    dimensions = compile_authority_backed_dimensions(
        contracts=inputs.contracts,
        ontology=inputs.ontology,
        repository_contracts=inputs.repository_contracts,
        candidate_relation_decisions=inputs.dimension_binding_decisions,
    )
    if findings := validate_dimension_authority_and_completeness(dimensions):
        return Failed(findings)

    ledger = compile_conservation_ledger(
        dispositions=dispositions,
        contracts=inputs.contracts,
        dimensions=dimensions,
    )
    candidates = generate_bounded_interaction_candidates(
        contracts=inputs.contracts,
        dimensions=dimensions,
        inventory=inputs.inventory,
    )
    interactions = finalize_interactions(
        candidates=candidates,
        ontology=inputs.ontology,
        decisions=inputs.contract_relation_decisions,
    )
    contrasts = compile_minimal_contrast_cases(
        dimensions=dimensions,
        interactions=interactions,
        fixtures=inputs.fixtures,
        adapters=inputs.adapters,
    )
    findings = validate_boundary_coverage(
        ledger=ledger,
        dimensions=dimensions,
        interactions=interactions,
        contrasts=contrasts,
    )
    return (
        Failed(findings)
        if findings
        else Completed(
            SemanticBoundaries(
                dispositions, ledger, dimensions, interactions, contrasts
            )
        )
    )
```

Expected design failures use stable codes so Procedrr routing and tests do not
parse prose:

| Code | Meaning |
| --- | --- |
| `source_disposition_missing` | A leaf proposition has no terminal route |
| `unsafe_nonactionable_exclusion` | Product meaning or mixed content would be excluded |
| `semantic_field_uncovered` | A conservation row lacks a required destination |
| `dimension_authority_missing` | A partition value has no accepted authority |
| `dimension_binding_ambiguous` | A source span matches incompatible dimensions |
| `behavioral_partition_uncovered` | Distinct values lack evidence or equivalence receipt |
| `interaction_unresolved` | A justified contract pair has no accepted relation |
| `contrast_case_missing` | A required relation has no discriminating case |
| `contrast_not_discriminating` | Both sides produce the same asserted observation |
| `prompt_projection_missing` | Canonical meaning has no final prompt range |
| `prompt_projection_mismatch` | The recorded range does not match final prompt bytes |
| `cross_projection_mismatch` | Prompt and manifest semantic reference sets differ |

Findings include the source, contract, field, dimension, interaction, or case
reference that failed, plus the exact expected route. They never recommend a
semantic repair that is not already supported by authority.

### A14: derive projections

Powdrr renders, rather than asks a model to author:

- obligation text;
- acceptance text;
- population description;
- operation description;
- oracle text;
- evidence-case skeleton;
- Structrr proposal records; and
- Workrr verification inputs; and
- one complete mini-SWE-agent implementation prompt after every contract in the
  design revision is resolved.

Templates are selected by disposition, behavior family, quantifier, and
predicate kind. Each rendering records the contract fingerprint and template
revision. Rendered prose is disposable and never becomes semantic authority.

The top-level compiler must remain visibly mechanical:

```python
def compile_contract(inputs: ContractCompilationInputs) -> CompilationResult:
    source_decisions = validate_current_source_decisions(inputs)
    partial = build_partial_contract(inputs.source, source_decisions)

    faithfulness = finalize_field_entailment(
        partial,
        require_complete_reviews(partial, inputs.entailment_reviews),
    )
    if faithfulness.has_contradiction:
        return Failed(faithfulness.findings)

    subject = finalize_binding(
        inputs.subject_candidates,
        inputs.subject_candidate_decisions,
    )
    if subject.unresolved:
        return Suspended(subject.reason_code)

    population = enumerate_population(
        subject=subject,
        quantifier=partial.quantifier,
        preconditions=partial.preconditions,
        inventory=inputs.inventory,
    )
    if not population.complete_for(partial.quantifier):
        return Suspended("repository_evidence_missing")

    behavior = finalize_binding(
        inputs.behavior_candidates,
        inputs.behavior_candidate_decisions,
    )
    if behavior.unresolved:
        return Suspended(behavior.reason_code)

    predicate = resolve_predicate_by_precedence(
        explicit_result=partial.explicit_result,
        accepted_definitions=inputs.accepted_definitions,
        applicable_intents=inputs.applicable_intents,
        human_decision=inputs.human_predicate_decision,
    )
    if predicate.unresolved:
        return Suspended(predicate.reason_code)

    resolved = assemble_resolved_contract(
        partial=partial,
        subject=subject,
        population=population,
        behavior=behavior,
        predicate=predicate,
    )
    validate_required_field_matrix(resolved)
    validate_reference_closure(resolved, inputs)
    return Completed(resolved)
```

The concrete implementation should use typed result variants rather than
exceptions for expected unresolved outcomes. Exceptions are reserved for
corrupt artifacts, invalid schemas, stale fingerprints, and violated compiler
invariants.

## Deterministic rendering example

Given the resolved pickle contract, templates produce:

```text
Obligation:
Every member of the Structrr data-entity population must support a Python
pickle round trip.

Acceptance:
For every current population member, the restored value has the same
observable semantic state as the original value.

Operation:
Construct one valid member and evaluate pickle.loads(pickle.dumps(value)).

Oracle:
Evaluate the registered semantic-equivalence assertion for the original and
restored values.
```

No model independently paraphrases these four representations.

## Single-prompt mini-SWE-agent compilation

A semantic contract does not directly contain test code or coding-agent prose.
Language adapters first compile contracts into internal
`verification-case-spec-v1` records:

```yaml
case_id: case:contract-instruction-001:user-record
contract_ref: contract:instruction-001
population_member_ref: python:src/models/user.py::UserRecord
fixture_ref: pytest:fixture:user_record
operation_ref: operation:python-pickle-round-trip
predicate_ref: predicate:semantic-equivalence
scenario:
  fixture_refs: [pytest:fixture:user_record]
  preconditions: [constructed_valid_member]
operation:
  operation_ref: operation:python-pickle-round-trip
oracle:
  predicate_ref: predicate:semantic-equivalence
  expected_result: restored_observably_equivalent_to_original
  forbidden_results: []
semantic_dimension_refs: []
interaction_refs: []
contrast_case_refs: []
test_target:
  provider: pytest
  path: tests/models/test_pickle.py
  selector: tests/models/test_pickle.py::test_user_record_pickle_round_trip
generation_status: planned
```

Case compilation rules:

1. Universal contracts produce a case for every current member or one
   parameterized case whose collected parameter set is proven equal to the
   population receipt.
2. Fixtures must come from inventory or a separately approved fixture-design
   decision.
3. Selectors and paths are produced by adapters, not models.
4. An existing case can satisfy a contract only when its subject, operation,
   and predicate bindings all match.
5. A representative example may guide fixture values but cannot substitute for
   population coverage.
6. Baseline execution must establish whether each case already passes, fails
   for the expected reason, or is blocked.
7. Scenario, operation, oracle, and expected result are separate required
   fields. Renderers may not truncate the case at the scenario boundary.
8. Every semantically relevant dimension value is covered by a case or a typed
   equivalence receipt proving that another value has the same observable
   result.
9. Every required interaction has a contrast case, and every contrast case
   records both the intended observation and any authority-backed
   plausible-but-wrong observation it is meant to reject.
10. A case that combines multiple contracts preserves separately attributable
    assertions and mappings; a broad passing result cannot satisfy every
    mapped contract.

These records are compiler inputs, not separate worker tasks. After every
contract and verification case in the design revision is resolved, Workrr
compiles a design handoff with two synchronized projections:

1. exactly one worker-facing implementation prompt; and
2. one Powdrr-private validation manifest used after coding.

The worker-facing projection is:

```yaml
schema_version: minisweagent-implementation-prompt-v1
prompt_id: prompt:feature-state-data:v1
design_revision: sha256:...
base_commit: git:...
repository_inventory: sha256:...
rendering_revision: minisweagent-single-prompt-v1
prompt: |-
  <complete rendered prompt>
contract_refs:
  - contract:instruction-001
semantic_dimension_refs:
  - dimension:state-data:declaration-presence
interaction_refs:
  - interaction:state-data:callback-vs-snapshot
contrast_case_refs:
  - contrast:state-data:callback-vs-snapshot
verification_case_refs:
  - case:contract-instruction-001:user-record
allowed_paths:
  - src/models/user.py
  - tests/models/test_pickle.py
focused_commands:
  - uv run pytest tests/models/test_pickle.py
fingerprint: sha256:...
```

The private projection is:

```yaml
schema_version: obligation-validation-manifest-v1
manifest_id: validation:feature-state-data:v1
design_revision: sha256:...
prompt_ref: prompt:feature-state-data:v1
prompt_fingerprint: sha256:...
base_commit: git:...
source_disposition_receipt_refs:
  - disposition:instruction-001
semantic_dimension_refs:
  - dimension:state-data:declaration-presence
interaction_refs:
  - interaction:state-data:callback-vs-snapshot
contrast_case_refs:
  - contrast:state-data:callback-vs-snapshot
obligations:
  - obligation_ref: contract:instruction-001
    source_proposition_ref: instruction-001
    verification_mode: executable_case
    durable_test_requirement: add
    case_refs:
      - case:contract-instruction-001:user-record
    conservation_row_refs:
      - conservation:instruction-001:round-trip-result
    semantic_dimension_refs: []
    interaction_refs: []
    contrast_case_refs: []
    required_evidence:
      - target_collected
      - candidate_passed
      - baseline_discriminated
      - test_oracle_aligned
      - implementation_satisfies_obligation
    baseline_expectation:
      status: fail
      allowed_failure_kinds:
        - assertion_failed
        - symbol_missing
    relevant_scope:
      - python:src/models/user.py::UserRecord
preservation_case_refs: []
full_validation_profiles:
  - format
  - lint
  - typecheck
  - test
fingerprint: sha256:...
```

The prompt and manifest are deterministic projections of the same canonical
contracts and verification cases. Their shared design revision, references,
and fingerprints let Workrr prove that every obligation shown to the worker is
also retained for independent validation. Only the prompt's `prompt` field is
sent to mini-SWE-agent. The worker never sees Structrr diffs, classifier
decisions, validation verdicts, fingerprints, proposal worklists, or parallel
representations of the same requirement.

The compiler also emits a private `prompt-projection-map-v1`. It binds
canonical meaning to exact UTF-8 byte ranges in the final rendered prompt; it
is not sent to mini-SWE-agent.

```yaml
schema_version: prompt-projection-map-v1
prompt_ref: prompt:feature-state-data:v1
prompt_fingerprint: sha256:...
fragments:
  - fragment_id: fragment:state-data-values:owned-view
    prompt_section: required_behavior
    byte_range: {start: 1842, end: 1967}
    rendered_text_fingerprint: sha256:...
    semantic_field_refs:
      - contract:state-data-values:observation-view
    conservation_row_refs:
      - conservation:instruction-017:owned-view
  - fragment_id: fragment:callback-vs-snapshot:contrast
    prompt_section: required_verification
    byte_range: {start: 3110, end: 3372}
    rendered_text_fingerprint: sha256:...
    interaction_refs:
      - interaction:state-data:callback-vs-snapshot
    contrast_case_refs:
      - contrast:state-data:callback-vs-snapshot
fingerprint: sha256:...
```

Prompt coverage is established from this map, not by the presence of hidden
contract IDs beside arbitrary prose. Workrr verifies each range against the
final prompt bytes and checks that every conservation row, interaction, and
required contrast case has the required destination. This makes transformations
such as dropping everything after `Oracle:`, replacing an owned view with a
merged view, or retaining a test name while deleting its expected result fail
deterministically.

### Validation-manifest rules

Every actionable behavioral obligation must have at least one executable
verification case unless the design records a typed exemption such as
`documentation_only`, `static_artifact`, or `human_observation`. An exemption
must name its replacement evidence and pass the same source-faithfulness
review; free-form claims that an obligation is “not testable” are invalid.

The obligation-to-case relationship is many-to-many:

- one obligation may require several cases for different population members,
  preconditions, or failure modes;
- one case may protect several obligations only when it identifies a distinct
  assertion, parameter, or observable predicate for each mapping; and
- every mapping records the scenario, operation, oracle, expected selector,
  and baseline expectation compiled before coding.

Each behavioral entry declares `durable_test_requirement` as `add`, `modify`,
or `existing_proven`. Post-coding validation checks the test diff for `add` and
`modify`; `existing_proven` requires fresh evidence that the exact existing
assertion already covers the obligation. A typed exemption is required when no
durable repository test is appropriate.

Whenever a language adapter can materialize the precompiled scenario,
operation, and oracle, Workrr also creates an independent validation probe
outside the candidate checkout. This probe is derived before coding and is not
authored or editable by mini-SWE-agent. It is preferred evidence because it
does not trust the worker to define both the implementation and its judge. The
durable repository test is still required for regression protection; the
independent probe validates the implementation directly.

A planned feature test defaults to `baseline_expectation.status: fail`. After
coding, Workrr runs the candidate test against the candidate implementation and
also runs the candidate test source against the pre-implementation product
baseline in an isolated shadow worktree. The case is discriminating only when
the candidate passes and the baseline fails for an allowed reason. Existing
preservation tests may legitimately pass on both revisions, but they cannot be
the sole evidence for newly requested behavior. When differential execution is
impossible, the design must declare that fact before coding and require another
independent evidence kind.

Test existence and passing execution are necessary, not sufficient. The
manifest also requires an oracle-alignment check proving that the test's setup,
operation, and assertions correspond to the precompiled verification case.
Deterministic adapters perform this check when they can; otherwise Procedrr
asks one bounded, read-only semantic question for that one mapping.

### Prompt sections

The renderer emits these sections once, in this order:

1. `Objective` — one compiler-rendered statement of the complete requested
   final state.
2. `Repository starting point` — exact relevant files, symbols, existing
   tests, language/toolchain facts, and known baseline failures.
3. `Required behavior` — every resolved contract rendered once and ordered by
   dependency, not by conversation order.
4. `Interaction boundaries` — each required distinction, lifecycle ordering,
   precedence rule, and authority-backed plausible-but-wrong conflation to
   avoid.
5. `Required verification` — tests to add or update, full scenario, operation,
   oracle, expected and forbidden observations, population coverage, and
   contrast cases.
6. `Preserve and avoid` — applicable invariants, non-goals, compatibility
   requirements, and forbidden generated paths.
7. `Allowed scope` — durable paths and explicitly permitted new paths.
8. `Focused commands` — exact commands mini-SWE-agent may use while working.
9. `Completion protocol` — inspect only relevant code, implement the complete
   design, run focused checks, do not commit or publish, and submit once.

The renderer must not repeat the original feature description after the
objective when its content has already been compiled into requirements. Exact
user terms that carry unresolved domain meaning remain quoted in the relevant
requirement with their accepted definition.

The objective is rendered only from resolved actionable contracts. It is not a
copy of the original instruction and cannot reintroduce process-only text.
`Required behavior` renders every condition, exception, result, temporal scope,
and observation view from canonical fields. `Required verification` renders
the entire case contract; shortening a case to its setup or stripping its
oracle is forbidden. The renderer may suppress internal IDs, provenance, and
duplicate prose only when the projection map proves that all canonical meaning
still has one destination.

For the DeepSWE state-data fixture, the following distinction is a normative
golden rendering target. Exact wording may change only with a rendering
revision; the represented fields and contrasts may not change:

```text
Required behavior — state-data views
- Each active state that declared data owns an independent data dictionary.
- get_state_data(state) returns that state's own active dictionary.
- An active state with no data declaration returns None.
- An inactive state returns None.
- Explicit data={} is a declaration and returns an empty dictionary.
- state_data_values maps active state identifiers to each state's own data.
- The state_data argument injected into a child callback is a different,
  effective view: active ancestor data merged with child-owned data, with child
  keys taking precedence.

Interaction boundaries
- Ancestor merging applies to callback injection, not to get_state_data or
  state_data_values. Do not implement all three with the same merged view.
- Persistence through on_enter and on_exit means data remains available and
  callback mutations persist. It does not require values to remain unchanged.

Required verification
- Given parent data {x: 1} and child data {y: 2}, the child callback receives
  {x: 1, y: 2}, while state_data_values[child] equals {y: 2} and does not
  contain x.
- An active state without a data declaration returns None; an active state
  explicitly declared with data={} returns {}.
- Data initialized before on_enter can be changed during on_enter, remains
  changed while active and during on_exit, and is removed only after on_exit.
```

This text is assembled from separate contracts, dimensions, interactions, and
contrast cases. It is not generated as one model-authored paragraph.

### Single-invocation rule

Workrr invokes mini-SWE-agent exactly once with the prompt artifact. The agent
may use its normal internal multi-turn inspect/edit/test loop, but Powdrr does
not send another model prompt during that implementation run.

- No obligation-per-agent calls.
- No per-file or per-test worker calls.
- No automatic continuation after timeout or step exhaustion.
- No validation-derived repair prompt.
- No fallback invocation of OpenCode.

After mini-SWE-agent submits or terminates, Workrr captures the attributable
diff and runs deterministically orchestrated, manifest-driven validation. A
failure is a terminal implementation result. Its evidence may be supplied to a
future design revision, which can compile a new prompt under a new run
identity; it is never appended as a second prompt to the existing run.

### Prompt completeness gate

The design handoff can be emitted only when deterministic checks prove:

1. every source proposition has exactly one current terminal disposition
   receipt;
2. every C01 `nonactionable` result has a matching C12 `process_only` result,
   and mixed or product-bearing propositions have been reprocessed rather than
   excluded;
3. every actionable source proposition reaches one resolved contract;
4. every resolved contract is represented exactly once in `Required behavior`;
5. every conservation row reaches its disposition-specific prompt and
   verification destinations;
6. every universal population has a complete current enumeration rule;
7. every predicate has an authority and executable assertion strategy;
8. every retained semantic dimension is complete for its authority and every
   behaviorally distinct value has case coverage;
9. every required contract interaction is resolved and represented exactly
   once in `Interaction boundaries`;
10. every required contrast case is represented exactly once with its intended
    and forbidden observations;
11. every required verification case is represented exactly once with its
    scenario, operation, oracle, and expected result intact;
12. all relevant preservation constraints and non-goals are included;
13. process-only propositions appear in no worker-facing prompt section;
14. every named path, symbol, test, and command comes from the bound repository
   inventory or an adapter-owned planned target;
15. allowed scope covers every planned target and no unrelated path;
16. no required field, interaction, or disposition is unresolved;
17. no internal artifact path or model reasoning appears in the prompt;
18. every projection-map UTF-8 byte range and text fingerprint matches the
    final prompt bytes; and
19. rendering the same versioned inputs produces the same prompt fingerprint.

The validation manifest must additionally prove:

1. every actionable obligation has one manifest entry;
2. every manifest entry has an accepted verification mode;
3. every behavioral obligation has an executable case or a typed, reviewed
   exemption with replacement evidence;
4. every case mapping has a scenario, operation, oracle, target contract, and
   baseline expectation;
5. every contrast case maps separately to each contract and assertion it
   distinguishes;
6. universal obligations cover the complete bound population;
7. every behaviorally distinct semantic partition has evidence coverage;
8. preservation obligations and non-goals have explicit checks;
9. every required evidence kind has a registered collector or judge, and every
   adapter-materializable case has an independent probe;
10. prompt contract references equal manifest obligation references;
11. prompt verification-case references equal manifest case references;
12. prompt interaction and contrast references equal manifest interaction and
    contrast references; and
13. the prompt, projection map, manifest, conservation ledger, and canonical
    design share one design revision and
    base commit.

### Post-coding obligation validation

After the single mini-SWE-agent invocation, Workrr evaluates the frozen
manifest without changing it:

1. Bind every planned target to a collected test or other concrete validator.
2. Reject missing, ambiguous, skipped, xfailed, deselected, or weakened cases.
3. Run each required case against the candidate and retain fresh evidence.
4. Run each adapter-materializable independent probe against the candidate.
5. Run every required contrast case and prove both observations, including the
   negative assertion that rejects the recorded plausible conflation.
6. For new behavior, run candidate-authored test code against the product
   baseline and verify the declared discriminating result.
7. Check each test mapping against its precompiled scenario, operation, and
   oracle.
8. Check every conservation row against its prompt fragment and collected
   evidence; a passing broad test cannot substitute for a missing mapped
   assertion.
9. Select relevant diff hunks by bound subject and changed-path closure.
10. For each obligation, ask at most one final read-only semantic question:
   “Does this implementation and verification evidence satisfy this one
   obligation?” The judge returns only `pass`, `fail`, or `abstain` plus a
   bounded explanation; Workrr supplies the identity and evidence references.
11. Emit one immutable `obligation-validation-receipt-v1` per obligation.
12. Run preservation, scope, formatting, lint, type, and full-suite checks.
13. Accept the implementation only when every required receipt and global
    check passes. There is no averaging and no “mostly complete” outcome.

An obligation receipt records the contract and manifest fingerprints, target
collection status, candidate result, baseline result, oracle-alignment result,
relevant diff evidence, semantic verdict when required, and exact failure
findings. It is validation evidence, never a new worker instruction.

## Procedrr flow

The target logical flow is:

```text
for each immutable atomic proposition:
  classify disposition
  if disposition is nonactionable:
    classify nonactionable exclusion safety
    if mixed, return to atomic decomposition and re-check coverage
    if product semantics are present, reclassify disposition
    if process-only, issue terminal exclusion receipt and continue with the
      next proposition
    otherwise suspend
  classify polarity
  classify requirement strength
  classify quantifier
  extract subject span
  extract behavior span
  classify behavior family
  classify precondition presence
  extract each detected precondition
  classify exception presence
  extract each detected exception
  classify explicit-result presence
  extract each detected result
  classify temporal scope
  compile partial contract

  for each interpreted source field:
    classify entailment
  finalize source-faithfulness gate

  construct repository lookup query
  retrieve subject candidates
  for each candidate when no exact binding exists:
    classify candidate relation
  finalize subject binding
  enumerate population
  finalize population-completeness gate

  retrieve behavior ontology candidates
  for each candidate when no exact binding exists:
    classify candidate relation
  finalize behavior binding

  resolve predicate authorities
  if no unique authority exists:
    suspend for one human decision

  compile resolved semantic contract
  validate required-field matrix

after all propositions complete:
  compile semantic dimensions and partitions
  compile modifier-conservation ledger
  generate bounded cross-contract interaction candidates
  for each interaction candidate not resolved by ontology:
    classify one contract observation relation
  finalize interaction graph
  compile minimal discriminating contrast cases
  derive prose projections
  compile verification case specifications
  compile one obligation validation manifest
  validate disposition, modifier, partition, interaction, obligation, and
    evidence coverage
  compile one mini-SWE-agent implementation prompt
  compile prompt projection map
  validate prompt completeness, byte-range fidelity, and
    prompt-to-manifest parity
  emit one design handoff with one worker-facing prompt
```

Every `classify` or `extract` line is one judge activation. Every `compile`,
`retrieve`, `enumerate`, `finalize`, `derive`, and `validate` line is a Workrr
operation with no model discretion.

## Proposed operation interfaces

| Operation | Input | Output |
| --- | --- | --- |
| `prepare_semantic_decisions` | atomic proposition | sealed list of required decision specifications |
| `bind_semantic_decision` | decision specification and provider result | semantic decision envelope |
| `finalize_source_disposition` | C01 result, C12 result when required, and atomic coverage | actionable route, process-only exclusion receipt, re-split request, or unresolved result |
| `resolve_exact_source_span` | source and extraction result | validated source span or diagnostic |
| `compile_partial_semantic_contract` | bound source decisions | partial contract |
| `prepare_field_entailment_reviews` | partial contract | one review specification per interpreted field |
| `finalize_source_faithfulness` | reviews | accepted partial contract or exact findings |
| `build_semantic_repository_inventory` | repository, Structrr, adapter revisions | immutable inventory |
| `construct_semantic_lookup_query` | partial contract field and inventory | lookup query |
| `retrieve_semantic_candidates` | query and inventory | ordered candidate set |
| `prepare_candidate_relation_decisions` | source concept and candidate set | one decision specification per candidate |
| `finalize_candidate_binding` | candidate set and bound decisions | one binding or unresolved result |
| `enumerate_contract_population` | binding, quantifier, relationships | population receipt |
| `lookup_behavior_ontology` | behavior span, family, languages | candidate ontology records |
| `resolve_predicate_authority` | source result, definitions, applicable intents | predicate binding or clarification request |
| `compile_semantic_contract` | partial contract and resolved bindings | resolved contract |
| `compile_semantic_dimensions` | contracts, authorities, ontology, repository API contracts | accepted finite dimensions and partitions |
| `compile_conservation_ledger` | source ledger, contracts, dispositions, and derived fields | one terminal-route row per source proposition and semantic field |
| `generate_interaction_candidates` | contracts, bindings, dimensions, relationships | bounded contract-pair candidates with reasons |
| `prepare_contract_relation_decisions` | unresolved interaction candidate | one C13 decision specification |
| `finalize_contract_interactions` | candidates, ontology facts, and bound C13 results | resolved interaction graph or exact findings |
| `compile_contrast_case_specs` | dimensions, interactions, fixtures, and adapters | minimal discriminating contrast cases |
| `render_semantic_contract_views` | resolved contract and template revision | non-authoritative prose views |
| `compile_verification_case_specs` | contract, population, fixtures, adapters | case specifications |
| `compile_obligation_validation_manifest` | complete design revision, cases, baseline expectations, evidence collectors | one immutable private validation manifest |
| `validate_obligation_validation_manifest` | manifest, contracts, cases, adapters | readiness receipt or exact findings |
| `compile_minisweagent_prompt` | complete design revision, cases, inventory, scope, commands | one immutable prompt artifact |
| `compile_prompt_projection_map` | rendered prompt, contracts, ledger, interactions, and cases | exact prompt ranges bound to canonical semantic inputs |
| `validate_minisweagent_prompt` | prompt, projection map, manifest, conservation ledger, and canonical design inputs | completeness, byte-level fidelity, and cross-projection receipt or exact findings |

## Suggested module boundaries

```text
src/powdrr_lift/core/semantic_decision.py
src/powdrr_lift/core/semantic_contract.py
src/powdrr_lift/core/semantic_ontology.py
src/powdrr_lift/core/semantic_boundary.py
src/powdrr_lift/core/repository_inventory.py
src/powdrr_lift/workrr/semantic_classifier.py
src/powdrr_lift/workrr/semantic_lookup.py
src/powdrr_lift/workrr/semantic_contract_compiler.py
src/powdrr_lift/workrr/semantic_boundary_compiler.py
src/powdrr_lift/workrr/verification_case_compiler.py
src/powdrr_lift/workrr/minisweagent_prompt_compiler.py
```

Core modules own immutable schemas, IDs, fingerprints, and validation. Workrr
modules own provider dispatch, repository lookup, compilation, and artifacts.
The feature endpoint should remain orchestration glue.

## Specialized-classifier migration

### Candidate priority

Implement specialized classifiers in this order:

1. C02 polarity;
2. C03 quantifier;
3. C04 requirement strength;
4. C01 disposition;
5. C06 modifier presence;
6. C05 behavior family;
7. E01/E02 subject and behavior token extraction;
8. C10 entailment;
9. C09 candidate relation; and
10. C11 proposition coverage;
11. C12 nonactionable exclusion safety; and
12. C13 contract observation relation.

The first six have small labels and strong lexical signals. The last three
require more semantic comparison and should retain an LLM fallback longer.

### Training records

Every reviewed production decision can become a training record containing:

```yaml
decision_kind: quantifier
contract_revision: quantifier-v1
source_text: All data should pickle.
context: {}
gold_value: every
adjudication: human | accepted-dual-agreement | deterministic
split: train | validation | held_out
```

Do not train on unreviewed model outputs. Deduplicate by normalized source and
source lineage so paraphrases from one feature do not leak across train and
held-out sets.

### Promotion gates

A specialized classifier begins in shadow mode. Workrr records its output but
uses the existing provider's bound decision. Promotion requires:

- a versioned held-out dataset with difficult boundary examples;
- per-label precision and recall, not only aggregate accuracy;
- zero known systematic polarity inversions;
- measured false-expansion and false-narrowing rates;
- calibrated abstention behavior;
- deterministic inference for a pinned model revision; and
- replay compatibility with the common decision envelope.

For high-risk labels (`prohibited`, `every`, explicit exceptions), target at
least 99% precision conditional on non-abstention before using the classifier
without dual confirmation. If that threshold is not attainable, prefer more
abstention rather than lowering the gate.

### Runtime contract

A local classifier receives only the decision-specific input and returns the
same minimal result schema as an LLM. It has no repository tools, mutable
state, transcript, or control-flow choice. Model weights, tokenizer, label map,
and inference code contribute to `model_revision`.

## Prompt construction before classifier replacement

General-model prompts must use a stable format:

1. one decision question;
2. one authoritative subject;
3. finite labels with one-sentence definitions;
4. `unresolved` and its allowed reason codes;
5. representative positive examples;
6. boundary and counterexamples;
7. a schema-only response instruction; and
8. no request for explanations, IDs, confidence, or implementation advice.

Examples should cover semantic boundaries rather than repeat easy synonyms.
Changing examples changes the decision contract revision and invalidates cached
results.

## Invalidation and replay

Recompute only affected stages:

| Change | Invalidated artifacts |
| --- | --- |
| Source text or atomic split | All decisions and contracts for affected propositions |
| Classifier contract revision | Decisions of that kind and downstream contracts |
| Structrr alias or population revision | Subject candidates, bindings, populations, and downstream cases |
| Repository commit | Inventory-derived candidates, bindings, populations, fixtures, and cases |
| Ontology revision | Behavior and predicate bindings using changed records |
| Accepted definition | Predicate and scope fields derived from that definition |
| Rendering template | Derived prose only |

Every compiler is a pure function of versioned inputs. Replay must either
produce the same fingerprint or identify the exact changed dependency.

## Artifact layout

For one work item:

```text
artifacts/semantic-contracts/
  instruction-001/
    source.json
    decisions/
      disposition.json
      polarity.json
      quantifier.json
      subject.json
      behavior.json
      ...
    partial-contract.json
    entailment-reviews/
      quantifier.json
      behavior-family.json
      ...
    repository-query.json
    repository-candidates.json
    candidate-decisions/
      candidate-001.json
      ...
    population-receipt.json
    behavior-candidates.json
    resolved-contract.json
    rendered-views.json
    verification-cases.json
  minisweagent-prompt.json
  minisweagent-prompt.txt
  obligation-validation-manifest.json
  validation-readiness-receipt.json
  obligation-validation-receipts/
    contract-instruction-001.json
```

Artifacts are diagnostic and replay inputs. Structrr stores accepted durable
meaning by reference after proposal acceptance.

## Worked example: `All data should pickle.`

### Source decisions

```yaml
disposition: invariant
polarity: required
requirement_strength: should
quantifier: every
subject_quote: data
behavior_quote: pickle
behavior_family: serialize
has_precondition: absent
has_exception: absent
has_explicit_result: absent
temporal_scope: unspecified
```

The partial contract correctly leaves subject binding and predicate
unresolved. It does not silently convert `pickle` to semantic round-trip
equivalence.

### Repository lookup

Suppose the inventory contains:

- Structrr population `data-entities`, alias `data`;
- registration relation `registered-as-data-entity`;
- members `StateData`, `DataVar`, and `TransitionData`; and
- Python pickle ontology aliases `pickle` and `picklable`.

The exact accepted alias auto-binds the population. The membership relation
enumerates all three current members and provides the future-membership rule.
The ontology alias selects the pickle behavior concept but still exposes four
predicate options.

### Required clarification

Because the source does not state the success predicate, Procedrr asks one
question:

```text
What must "should pickle" guarantee for each data entity?

1. Serialization completes without error.
2. Serialization and deserialization complete without error.
3. The restored value has the same type and compares equal.
4. The restored value has equivalent observable semantic state.
```

The answer becomes a human decision with provenance. Future uses of the same
accepted definition can resolve deterministically.

### Final assembly

If option 4 is accepted, the compiler emits the resolved contract, enumerates
three current verification cases, and emits one invariant case ensuring the
test parameterization remains equal to the registry membership set.

## Worked example: explicit interface result

Source:

> The export endpoint accepts a report ID and returns CSV.

Atomic decomposition must produce separate propositions for accepted input and
returned format if they can fail independently. For the return proposition:

```yaml
disposition: interface
polarity: required
quantifier: unspecified
subject_quote: The export endpoint
behavior_quote: returns CSV
has_explicit_result: present
result_quote: CSV
```

Repository lookup binds the endpoint only from route/interface inventory.
Predicate compilation uses the explicit `CSV` result and the registered HTTP
response contract. It does not ask a model to invent a status code, filename,
delimiter, or encoding.

## Worked example: prohibition

Source:

> Do not add retries to report exports.

```yaml
disposition: non_goal
polarity: prohibited
quantifier: every
subject_quote: report exports
behavior_quote: add retries
behavior_family: retry
predicate:
  kind: absence
  observed_count: 0
```

The absence predicate is derived from the registered `retry` ontology. Subject
lookup binds export operations. Verification observes invocation count across
failed export cases; it does not create an implementation task named “do not
add retries.”

## Test strategy

### Unit tests

- one test per label and unresolved route for every classifier schema;
- exact-span occurrence and Unicode handling;
- deterministic normalization and candidate ordering;
- automatic-binding restrictions;
- candidate aggregation outcomes;
- universal population completeness;
- predicate precedence;
- disposition-specific completion matrices;
- source-disposition conservation and exclusion safety;
- semantic-dimension authority and partition completeness;
- bounded interaction candidate generation and pairwise relation aggregation;
- minimal discriminating contrast-case generation;
- projection-map byte ranges and fingerprint verification;
- provenance and fingerprint invalidation;
- deterministic rendering; and
- artifact round trips.

### Contract tests

Run every classifier provider against the same decision fixtures and require
identical bound-result shapes. Provider-specific metadata may differ; semantic
values and abstention codes use the common schema.

### Gold semantic suite

Maintain at least these families:

- explicit and implicit quantifiers;
- nested negation and prohibitions;
- requirement versus guidance;
- product non-goal versus process instruction;
- conditions versus separate requirements;
- exceptions and unless-clauses;
- explicit versus inferred results;
- domain shorthand with and without accepted definitions;
- ambiguous repository nouns;
- singular symbols versus declared populations;
- current versus future applicability; and
- atomic split coverage, overlap, omission, and invention;
- process-only versus product non-goal versus mixed product/process clauses;
- absent versus explicitly empty declarations;
- owned data versus inherited or merged observation views;
- availability and mutation persistence versus value immutability;
- before, during, and after lifecycle ordering;
- shallow versus deep restoration behavior;
- independently invalid input classes that share one exception type; and
- pairs of similar contracts whose one differing field changes the oracle.

### Live validation

Live design-flow validation must record and inspect intermediate classifier
outputs, not merely the final YAML. For each fixture, assert:

- expected source spans;
- expected finite labels;
- expected unresolved fields;
- selected repository bindings;
- population members;
- authority used for every derived field; and
- resolved contract and verification-case fingerprints;
- disposition and process-only exclusion receipts;
- semantic dimensions and their authority-backed values;
- contract interactions and relation decisions;
- generated contrast cases and plausible-but-wrong results;
- conservation-ledger terminal routes; and
- exact prompt projection ranges for every required semantic field.

At least one live fixture must intentionally remain unresolved and demonstrate
the correct clarification route.

## Implementation sequence

### Slice 1: contracts and source classifiers

- Add decision envelopes, partial contracts, and schemas.
- Implement deterministic polarity, modal, and explicit-quantifier rules.
- Replace prose obligation generation with source classifiers and extractors.
- Persist intermediate decisions.
- Keep existing downstream projections behind an adapter.

Acceptance gate: the gold source-classification suite passes, and no partial
contract contains generated paraphrase as authority.

### Slice 2: source-faithfulness and compilation

- Add field-level entailment decisions.
- Add required-field matrices and unresolved routing.
- Add deterministic obligation and acceptance rendering.
- Remove the serial obligation-to-acceptance-to-oracle prose dependency.

Acceptance gate: intentionally invented fields are rejected or marked
`not_stated`.

### Slice 3: repository inventory and subject binding

- Implement inventory adapters for currently supported languages.
- Add Structrr aliases, populations, and relationship expansion.
- Implement candidate retrieval, scoring, C09 decisions, and aggregation.
- Add population enumeration receipts.

Acceptance gate: `All data should pickle` resolves every current declared data
member without a model-generated member list.

### Slice 4: ontology and predicate resolution

- Add behavior and predicate ontology records.
- Implement authority precedence and human clarification artifacts.
- Add language-adapter capability checks.

Acceptance gate: underspecified success semantics block instead of being
invented, while an accepted definition resolves deterministically.

### Slice 5: semantic boundaries and conservation

- Add semantic dimension, conservation row, contract interaction, contrast
  case, and prompt projection-map schemas.
- Compile authority-backed partitions after all contracts resolve.
- Add bounded pair generation and deterministic interaction resolution.
- Add C12 exclusion safety and C13 pairwise relation decisions where exact
  rules do not decide.
- Compile minimal contrast cases and adapter-owned independent probes.
- Fail closed on unresolved interactions, uncovered modifier rows, or
  behaviorally distinct partitions without verification.

Acceptance gate: the complete DeepSWE state-data fixture deterministically
distinguishes callback-effective scope from owned query state, absent from
explicitly empty declarations, persistence from immutability, shallow from
deep history, and each separately invalid declaration class. Process-only
instructions have exclusion receipts and appear nowhere in worker-facing text.

### Slice 6: executable verification and single-prompt compilation

- Compile member-specific or parameterized case specifications.
- Bind fixtures and existing tests from inventory.
- Run baseline evidence and verify population coverage.
- Compile the private obligation validation manifest, including required
  evidence, baseline expectations, typed exemptions, and preservation checks.
- Compile all resolved contracts, repository facts, verification cases, scope,
  and focused commands into one mini-SWE-agent prompt.
- Render the full scenario, operation, oracle, expected result, interaction
  boundary, and contrast case; do not use lossy description splitting.
- Compile exact prompt UTF-8 byte ranges into a projection map.
- Add deterministic disposition, conservation, partition, interaction,
  manifest, prompt, cross-projection parity, and forbidden-content checks.

Acceptance gate: universal contracts cannot pass with one synthetic
representative member; every actionable obligation has a ready validation
entry; every canonical semantic field reaches a verified prompt range and
evidence mapping; and one complete design revision emits exactly one
worker-facing prompt containing every actionable contract exactly once.

### Slice 7: specialized classifiers

- Export adjudicated decision datasets.
- Implement shadow execution and comparison reports.
- Promote classifiers one decision kind at a time through the stated gates.

Acceptance gate: switching a provider does not change Procedrr or canonical
artifact schemas.

## Explicit non-goals

- Do not train one model to emit the entire semantic contract.
- Do not accept model-generated repository identities.
- Do not use embedding similarity as proof of semantic equivalence.
- Do not use self-reported model confidence as an acceptance gate.
- Do not hide ambiguity by adding more prompt examples until one answer wins.
- Do not treat one representative evidence case as universal coverage.
- Do not make rendered obligation prose canonical.
- Do not invoke a coding agent to perform read-only repository discovery.
- Do not allow classifier replacement to alter flow control or persisted
  semantic values.
- Do not create one coding-agent prompt per obligation, test, file, repair, or
  validation failure.
- Do not invoke OpenCode after the design phase; mini-SWE-agent is the single
  target worker.

## Completion criteria for this design

The design is implemented when:

1. the design interview produces source-anchored partial contracts;
2. every model-derived semantic field has a bound decision and authority;
3. required ambiguity suspends rather than guesses;
4. repository populations are adapter-enumerated with receipts;
5. resolved contracts deterministically produce all downstream prose;
6. verification cases cover resolved populations;
7. intermediate classifications are visible in live validation artifacts;
8. at least one specialized classifier can replace an LLM through the common
   provider contract; and
9. the completed design deterministically emits one immutable mini-SWE-agent
   prompt containing the complete implementation and verification contract;
10. the same design emits a private validation manifest that preserves every
    obligation, case, oracle, baseline expectation, and required evidence kind;
11. an end-to-end feature run invokes mini-SWE-agent once with that exact
    prompt and creates no repair or continuation prompt; and
12. the run produces a passing receipt for every obligation and proves that no
    model-authored paraphrase became authoritative intent;
13. every source proposition has exactly one terminal disposition receipt and
    process-only exclusions cannot erase product semantics;
14. every meaning-bearing modifier, partition, and interaction has a verified
    prompt projection and evidence route; and
15. the DeepSWE state-data fixture rejects prompts or implementations that
    conflate owned and merged views, absent and empty declarations, persistence
    and immutability, shallow and deep history, or distinct invalid-input
    classes.
