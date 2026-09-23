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
- the conversion of a resolved contract into an executable verification input.

It does not specify code-task execution, coding-agent prompting, proposal
publication, or final implementation review except where those consumers
constrain the compiled contract.

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

## Authority boundaries

| Authority | Responsibility |
| --- | --- |
| Structrr | Durable intent sources, accepted definitions, product entities, relationships, aliases, populations, and semantic revisions |
| Procedrr | Ordered single-decision flow, branch conditions, bounded loops, required decision schemas, and terminal outcomes |
| Workrr | Source binding, inventory creation, candidate generation, provider dispatch, response binding, deterministic assembly, evidence capture, and artifact persistence |
| Semantic classifier | One label, exact quotation, or candidate relation for one supplied subject |
| Language adapter | Repository symbols, type relationships, references, tests, and executable validation capabilities |
| Human | Resolution of meaning that is not entailed by the source or accepted definitions |

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

### A10: derive projections

Powdrr renders, rather than asks a model to author:

- obligation text;
- acceptance text;
- population description;
- operation description;
- oracle text;
- evidence-case skeleton;
- Structrr proposal records; and
- Workrr verification inputs.

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

## Executable-contract compilation

A semantic contract does not directly contain test code. A language adapter
compiles it into one or more `verification-case-spec-v1` records:

```yaml
case_id: case:contract-instruction-001:user-record
contract_ref: contract:instruction-001
population_member_ref: python:src/models/user.py::UserRecord
fixture_ref: pytest:fixture:user_record
operation_ref: operation:python-pickle-round-trip
predicate_ref: predicate:semantic-equivalence
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

## Procedrr flow

The target logical flow is:

```text
for each immutable atomic proposition:
  classify disposition
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
  derive prose projections
  compile verification case specifications
```

Every `classify` or `extract` line is one judge activation. Every `compile`,
`retrieve`, `enumerate`, `finalize`, `derive`, and `validate` line is a Workrr
operation with no model discretion.

## Proposed operation interfaces

| Operation | Input | Output |
| --- | --- | --- |
| `prepare_semantic_decisions` | atomic proposition | sealed list of required decision specifications |
| `bind_semantic_decision` | decision specification and provider result | semantic decision envelope |
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
| `render_semantic_contract_views` | resolved contract and template revision | non-authoritative prose views |
| `compile_verification_case_specs` | contract, population, fixtures, adapters | case specifications |

## Suggested module boundaries

```text
src/powdrr_lift/core/semantic_decision.py
src/powdrr_lift/core/semantic_contract.py
src/powdrr_lift/core/semantic_ontology.py
src/powdrr_lift/core/repository_inventory.py
src/powdrr_lift/workrr/semantic_classifier.py
src/powdrr_lift/workrr/semantic_lookup.py
src/powdrr_lift/workrr/semantic_contract_compiler.py
src/powdrr_lift/workrr/verification_case_compiler.py
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
10. C11 proposition coverage.

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
- atomic split coverage, overlap, omission, and invention.

### Live validation

Live design-flow validation must record and inspect intermediate classifier
outputs, not merely the final YAML. For each fixture, assert:

- expected source spans;
- expected finite labels;
- expected unresolved fields;
- selected repository bindings;
- population members;
- authority used for every derived field; and
- resolved contract and verification-case fingerprints.

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

### Slice 5: executable verification contracts

- Compile member-specific or parameterized case specifications.
- Bind fixtures and existing tests from inventory.
- Run baseline evidence and verify population coverage.

Acceptance gate: universal contracts cannot pass with one synthetic
representative member.

### Slice 6: specialized classifiers

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
9. an end-to-end feature run can prove that no model-authored paraphrase became
   authoritative intent.
