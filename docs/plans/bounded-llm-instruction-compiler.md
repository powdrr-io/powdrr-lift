# Bounded-LLM Instruction Compiler Implementation Plan

Status: proposed for source capture and atomic-clause production; downstream
worker design superseded

## Downstream target amendment

This plan remains normative only through deterministic source capture,
segmentation, instruction-ledger construction, and atomic-clause production.
Its later `ImplementationPacket`, `RepairPacket`, OpenCode, repair-loop, and
delivery sections record the earlier architecture and must not be implemented.

The normative design-phase output is now defined by
`docs/design/source-anchored-semantic-contract-compilation.md`: one completed
design revision compiles exactly one worker-facing
`minisweagent-implementation-prompt-v1` plus one private
`obligation-validation-manifest-v1`. The downstream boundary in
`docs/design/external-coding-agent-boundary.md` sends only the prompt, exactly
once, to one mini-SWE-agent invocation and retains the manifest for independent
per-obligation validation. Deterministic post-implementation validation is
terminal for that run and cannot produce a continuation, repair, or fallback
prompt. A subsequent attempt requires a new run and a revalidated design
revision.

When this document conflicts with either normative downstream document, the
newer single-prompt design controls. Future implementation work should extract
the still-normative source-compilation material into its own document and then
archive this mixed historical plan.

## Purpose

This document defines an implementation-ready redesign of Powdrr's
instructions-to-implementation pipeline. The redesign makes large feature
requests tractable for language models while preserving complete traceability
from the user's original prose through design, code, tests, review, and repair.

The governing rule is:

> The language model may supply meaning, prose, and code. It must never invent
> identities, references, workflow structure, evidence contracts, validation
> configuration, or completion state.

Powdrr must become the compiler and authority for structure. Planning models
become bounded semantic judges. After design, Powdrr compiles one complete
prompt and mini-SWE-agent is the only target allowed to edit code.

This plan is written for an implementation agent. It identifies the current
failure, target contracts, ownership boundaries, exact workflow changes,
runtime changes, migration sequence, required tests, and completion gates.

## Problem statement

The current `implement-feature` flow asks planning models to do too many kinds
of work at once:

- construct a broad feature specification;
- populate many Structrr categories;
- create semantic identifiers and cross-document references;
- create test obligations and intent references;
- translate every instruction sentence into design;
- judge whether the generated plan reflects the sentence;
- review a large compiled proposal worklist;
- review implementation completeness and scope; and
- construct repair requests.

This creates several compounding failure modes:

1. The same input is interpreted repeatedly by independent model calls.
2. Models invent unstable identifiers such as `feature_data_ownership`.
3. Model-authored identifiers are accepted into documents before a
   deterministic authority can bind them to real objects.
4. The full feature description is copied into many Structrr objects, producing
   very large plans and review contexts.
5. A modest feature can compile into hundreds of proposal decisions.
6. Failures occur late, after expensive planning, and often identify malformed
   structure instead of the semantic issue that needs repair.
7. Repair asks the model to reconstruct broad context rather than repair one
   failed obligation.
8. Deterministic unit tests can pass while live providers still produce invalid
   references, because the tests supply idealized model output.

The DeepSWE `python-statemachine-state-data-scoping` run demonstrates the
failure concretely. The planning model emitted:

```yaml
required_test_cases:
  - id: test_data_ownership_lifecycle
    intent_refs:
      - feature_data_ownership
```

No active intent had that identifier. The deterministic proposal gate correctly
rejected the plan, but only after the model had generated a large, repetitive
Structrr document. The desired system must make this output structurally
impossible.

## Goals

The completed system must:

1. Preserve every instruction clause exactly once with a stable Powdrr-owned ID.
2. Decompose complex instructions into independently reviewable obligations.
3. Ask the planning model one small semantic question at a time.
4. Prevent all model-generated identifiers and references from entering
   authoritative records.
5. Compile design, intent, verification, and proposal records deterministically.
6. Give OpenCode a compact implementation packet containing concrete behavior
   and exact required test selectors.
7. Validate every structural fact without an LLM.
8. Review implementation semantics one obligation at a time.
9. Produce targeted repair packets for only the failed obligations.
10. Retain enough artifacts to replay and debug every decision.
11. Keep the normal pull-request flow and Harbor flow on the same core pipeline.
12. Prove the design against the complete DeepSWE state-data instructions with
    both deterministic and live-provider tests.

## Non-goals

This redesign does not:

- make OpenCode responsible for planning, review, or identifiers;
- remove Structrr, Procedrr, or verification contracts;
- treat sentence splitting alone as a design process;
- ask one model call to generate a complete plan;
- allow an LLM to choose providers, commands, profiles, selectors, fingerprints,
  applicability modes, or evidence references;
- accept successful tests as sufficient semantic review by themselves;
- require all prose sentences to become code changes;
- allow a failed obligation to be silently dropped or reclassified during
  repair; or
- preserve the current category-by-category LLM editing interface for
  compatibility.

## Authority boundaries

| Authority | Owns | Must not own |
| --- | --- | --- |
| User instructions | Original wording and requested outcome | Runtime IDs or repository-specific implementation details |
| Powdrr compiler | IDs, references, schemas, ordering, fingerprints, contracts, selectors, state transitions, completeness | Product semantics that require interpretation |
| Planning model | Bounded semantic classifications, behavioral descriptions, test scenarios, semantic verdicts, explanations | IDs, references, providers, selectors, workflow shape, evidence identity, edits |
| Structrr | Persisted design and intent projections compiled from canonical records | Raw unvalidated model output |
| Procedrr | Ordered bounded decisions, deterministic operations, gates, retries, and terminal behavior | Repository discovery or model-generated control flow |
| Workrr | Execution, persistence, context assembly, deterministic compilation, evidence collection | Reinterpreting user intent or weakening gates |
| OpenCode | Code and test edits requested by an implementation or repair packet | Planning, contract construction, final review, completion claims |

Any production API that permits a planning-model response to set an `id`,
`*_ref`, `selector`, `provider`, `profile`, `fingerprint`, `status`, or
`applicability` field violates this boundary.

## Target pipeline

```text
original feature description
    |
    v
deterministic source capture and clause candidates
    |
    v
bounded clause decomposition decisions
    |
    v
canonical instruction ledger (Powdrr IDs)
    |
    v
one semantic classification per clause
    |
    v
one design projection per obligating clause
    |
    v
deterministic obligation and test-contract compilation
    |
    v
deterministic Structrr/proposal compilation and validation
    |
    v
compact implementation packet for OpenCode
    |
    v
code edits + required tests
    |
    v
deterministic validation and evidence collection
    |
    v
one semantic implementation review per obligation
    |
    +---- pass ----> deterministic scope review and completion
    |
    +---- fail ----> targeted repair packet -> OpenCode -> revalidate
```

The model never returns the arrows, node identities, or references. Procedrr
and Workrr own those.

## Canonical artifacts

All new records must be immutable, versioned, serializable, and fingerprinted.
JSON examples are shown below, but implementation may use frozen dataclasses
with explicit `to_data` and `from_data` methods following existing repository
patterns.

### Instruction source

Capture the original request once without rewriting it:

```json
{
  "schema_version": "instruction-source-v1",
  "source_id": "instruction-source:python-statemachine-state-data-scoping",
  "work_item_name": "python-statemachine-state-data-scoping",
  "text": "States lack built-in data ownership...",
  "text_fingerprint": "sha256:..."
}
```

`source_id` is generated from the normalized work-item name. The original text
must remain byte-for-byte available in the artifact. A normalized view may be
stored separately for segmentation, but it must not replace the source text.

### Instruction clause

```json
{
  "schema_version": "instruction-clause-v1",
  "clause_id": "instruction-007",
  "source_id": "instruction-source:python-statemachine-state-data-scoping",
  "ordinal": 7,
  "text": "On exit, data is removed.",
  "source_span": {"start": 153, "end": 178},
  "parent_clause_id": null,
  "derivation": "deterministic-sentence-v1",
  "fingerprint": "sha256:..."
}
```

Required properties:

- `clause_id` is assigned by Powdrr from final ledger order.
- `source_span` addresses the original instruction text.
- Split child clauses retain `parent_clause_id` and the parent's source span.
- Clause IDs never come from model output.
- Renumbering is allowed while the ledger is being compiled, but IDs become
  immutable once the ledger fingerprint is persisted.

### Clause disposition

The model answers whether one clause creates an obligation. Powdrr supplies the
clause identity outside the model response.

```json
{
  "schema_version": "clause-disposition-v1",
  "clause_id": "instruction-007",
  "kind": "implementation_obligation",
  "explanation": "This sentence defines required exit lifecycle behavior.",
  "input_fingerprint": "sha256:...",
  "model_evidence_fingerprint": "sha256:..."
}
```

`kind` is a closed enum:

- `implementation_obligation`;
- `verification_obligation`;
- `constraint`;
- `non_goal`;
- `delivery_instruction`;
- `context_only`; and
- `clarification_required`.

Only `kind` and `explanation` come from the model response. Workrr adds every
identity and fingerprint.

An explicit delivery instruction such as "work on a new branch" must not become
a product implementation obligation. It is compiled into the Git wrapper when
applicable and ignored by the Harbor in-place wrapper when the harness owns the
checkout policy.

### Design projection

Each obligating clause receives one narrow semantic projection:

```json
{
  "schema_version": "design-projection-v1",
  "projection_id": "design:instruction-007",
  "clause_id": "instruction-007",
  "behavior": "State-owned data is unavailable after the state exits.",
  "acceptance_criterion": "After exit, querying that state's data returns no active data.",
  "test_scenario": "Enter a state, mutate its data, exit, then query its data.",
  "affected_concept": "state data lifecycle",
  "input_fingerprint": "sha256:...",
  "fingerprint": "sha256:..."
}
```

The planning model returns only:

```json
{
  "behavior": "...",
  "acceptance_criterion": "...",
  "test_scenario": "...",
  "affected_concept": "..."
}
```

The response schema must not contain any ID or reference fields.

### Feature obligation

Powdrr compiles one obligation for each disposition that requires product or
verification work:

```json
{
  "schema_version": "feature-obligation-v2",
  "obligation_id": "obligation:instruction-007",
  "clause_id": "instruction-007",
  "projection_id": "design:instruction-007",
  "obligation_kind": "behavior",
  "description": "State-owned data is unavailable after the state exits.",
  "acceptance_criterion": "After exit, querying that state's data returns no active data.",
  "status": "planned",
  "fingerprint": "sha256:..."
}
```

`obligation_id`, `projection_id`, `status`, and all references are generated by
Powdrr. The model cannot override them.

### Required test contract

Every product or verification obligation receives at least one contract:

```json
{
  "schema_version": "required-test-contract-v2",
  "contract_id": "test:obligation:instruction-007",
  "obligation_refs": ["obligation:instruction-007"],
  "description": "Enter a state, mutate its data, exit, then query its data.",
  "provider": "pytest",
  "profile": "pytest",
  "selector": "tests/test_state_data.py::test_state_data_removed_on_exit",
  "selector_state": "planned",
  "expectation": "pass",
  "applicability": {"mode": "affected_closure"},
  "status": "active",
  "fingerprint": "sha256:..."
}
```

Powdrr chooses the provider and profile from discovered inventory. For a new
test, Powdrr derives a deterministic candidate selector from the obligation ID
and repository test conventions. The planning model supplies only the test
scenario; OpenCode may move a planned selector only through an explicit
selector-reconciliation operation that updates the contract deterministically
and proves the replacement exists.

The first implementation should avoid selector reconciliation. Require
OpenCode to create the exact planned selector.

### Implementation packet

OpenCode receives a compact generated packet, not the entire Structrr document:

```json
{
  "schema_version": "implementation-packet-v1",
  "request_id": "implement:python-statemachine-state-data-scoping:1",
  "objective": "Add state-scoped data behavior.",
  "obligations": [
    {
      "obligation_id": "obligation:instruction-007",
      "source_text": "On exit, data is removed.",
      "behavior": "State-owned data is unavailable after the state exits.",
      "acceptance_criterion": "After exit, querying that state's data returns no active data.",
      "required_test": {
        "contract_id": "test:obligation:instruction-007",
        "selector": "tests/test_state_data.py::test_state_data_removed_on_exit",
        "scenario": "Enter a state, mutate its data, exit, then query its data."
      }
    }
  ],
  "repository_context": {
    "relevant_paths": ["statemachine/state.py", "tests/test_state.py"],
    "existing_tests": ["tests/test_state.py::test_state_entry"]
  },
  "allowed_paths": ["statemachine/**", "tests/**"],
  "validation_profiles": ["pytest"],
  "fingerprint": "sha256:..."
}
```

The packet must not repeat the complete feature description under every
obligation. Include source text by reference in persisted records and inline it
once per obligation only in the worker prompt.

### Obligation review

After deterministic evidence collection, ask one semantic question per
obligation. The model response is:

```json
{
  "verdict": "fail",
  "explanation": "Normal exits clear data, but transitions out of parallel regions do not."
}
```

Powdrr wraps it as:

```json
{
  "schema_version": "obligation-review-v1",
  "obligation_id": "obligation:instruction-007",
  "verdict": "fail",
  "explanation": "Normal exits clear data, but transitions out of parallel regions do not.",
  "evidence_refs": ["diff@sha256:...", "test@sha256:..."],
  "input_fingerprint": "sha256:...",
  "fingerprint": "sha256:..."
}
```

The model does not echo the obligation ID or evidence references. This avoids
identity-copy errors and stale evidence claims.

### Repair packet

Powdrr creates one repair packet per failed obligation:

```json
{
  "schema_version": "repair-packet-v1",
  "repair_id": "repair:obligation:instruction-007:1",
  "obligation_id": "obligation:instruction-007",
  "behavior": "State-owned data is unavailable after the state exits.",
  "failure": "Normal exits clear data, but transitions out of parallel regions do not.",
  "required_test": "tests/test_state_data.py::test_state_data_removed_on_exit",
  "relevant_diff": "...bounded diff hunks...",
  "validation_failures": [],
  "allowed_paths": ["statemachine/**", "tests/**"],
  "fingerprint": "sha256:..."
}
```

OpenCode receives this packet directly. Do not ask the planning model to write a
free-form repair prompt.

## Instruction-ledger compilation

### Stage 1: capture

Implement a deterministic `InstructionLedgerCompiler` that accepts the original
feature description and returns clause candidates with source spans.

Rules for the first version:

1. Normalize whitespace only for segmentation; preserve the original text.
2. Do not split on soft line wrapping.
3. Split on terminal punctuation followed by whitespace.
4. Preserve Markdown list items as candidate boundaries.
5. Preserve headings and colon-led introductory text with the following list
   when splitting would produce a meaningless fragment.
6. Do not split abbreviations, decimal numbers, or qualified identifiers solely
   because they contain a period.
7. Keep the original source span for every candidate.
8. Produce deterministic output independent of locale and model behavior.

The current `_decompose_feature_description` in
`src/powdrr_lift/workrr/feature_endpoint.py` is a temporary implementation. Move
this responsibility into a dedicated module, for example:

```text
src/powdrr_lift/core/instruction_ledger.py
```

### Stage 2: bounded atomicity decision

For each candidate, ask:

> Does this clause contain more than one independently verifiable requirement?

Allowed response:

```json
{"multiple": true}
```

If false, retain the candidate. If true, ask a second bounded construction
question:

> Rewrite this clause as the smallest list of independently verifiable
> statements without adding, removing, or interpreting requirements.

Allowed response:

```json
{
  "statements": [
    "Data initializes as a fresh copy of defaults on entry.",
    "Data is removed on exit."
  ]
}
```

Powdrr must validate:

- two or more non-empty statements are returned;
- the source candidate remains attached as the parent;
- no child IDs are accepted from the model;
- the result is bounded by a configurable maximum, initially eight children;
- a second split request for the same parent is prohibited; and
- failure to produce a valid split results in `clarification_required`, not
  silent acceptance of arbitrary output.

### Stage 3: disposition

Ask one classification question per final clause. Do not include the whole plan
or sibling clauses unless a small amount of neighboring text is required to
resolve pronouns.

The response schema contains only `kind` and `explanation`. Workrr binds the
response to the current clause and records the prompt input fingerprint.

Default policy should favor preserving user intent:

- requested behavior, APIs, errors, lifecycle rules, compatibility, persistence,
  serialization, and observable integrations are obligations;
- explicit test expectations are verification obligations;
- explicit prohibitions become constraints or non-goals;
- motivation may be `context_only`, but its relationship to subsequent clauses
  remains preserved in the source ledger;
- operational instructions become delivery instructions; and
- ambiguous requirements become `clarification_required`.

The flow must not advance while a required clause is
`clarification_required`. In noninteractive benchmark mode, compile a failed
artifact that identifies the exact clause instead of guessing.

### Stage 4: design projection

For every implementation, verification, constraint, and non-goal disposition,
ask one construction question for one clause. The model returns only semantic
fields. Powdrr validates non-empty prose and compiles IDs and links.

Do not run a second generic "repair the design consequence" model pass. Replace
it with deterministic response validation and, only on validation failure, a
retry containing the invalid response and one precise diagnostic.

The current flow performs a broad design generation pass and then a second
model repair pass for every sentence. This doubles inconsistency without adding
an authoritative check.

## Repository context and existing-test discovery

The planning model should receive relevant repository facts, but it must not
discover or name those facts itself.

After Structrr bootstrap and validation-profile discovery, gather a compact
repository context packet containing:

- language and component inventory;
- public symbols relevant to nouns and API names in the clause;
- existing tests selected from provider inventory;
- nearby tests in files that exercise selected source subjects;
- test naming conventions;
- declared validation profiles;
- relevant source files and source subjects; and
- repository-local constraints such as contributor instructions.

The packet should expose existing tests as stable inventory entries:

```json
{
  "inventory_id": "pytest:tests/test_state.py::test_state_entry",
  "provider": "pytest",
  "profile": "pytest",
  "selector": "tests/test_state.py::test_state_entry",
  "path": "tests/test_state.py",
  "subjects": ["python:statemachine/state.py::State"],
  "fingerprint": "sha256:..."
}
```

The model may classify whether the scenario needs a new test or whether an
existing inventory entry is semantically relevant. It must choose an existing
test only from opaque candidates supplied by Powdrr. Prefer an integer
candidate index in the response rather than copying a selector.

For the initial implementation, always compile one new exact selector per
obligation. Existing-test selection can be added after the new-test path is
reliable. Existing tests may still be included as context for style and fixture
reuse.

## Deterministic compilation rules

### IDs

Use functions, not prompts, for every ID:

| Record | ID rule |
| --- | --- |
| Source | `instruction-source:{slug}` |
| Clause | `instruction-{ordinal:03d}` |
| Projection | `design:{clause_id}` |
| Obligation | `obligation:{clause_id}` |
| Test contract | `test:{obligation_id}` |
| Active intent | `intent:{obligation_id}` |
| Repair | `repair:{obligation_id}:{attempt}` |

Persist an alias map only when migrating existing records. Do not fuzzy-match
model-generated names to canonical IDs.

### References

All references are created by constructors that require the referenced object
to exist. Constructors should accept objects or typed IDs, not arbitrary
strings. Deserialization must validate reference closure.

### Fingerprints

Fingerprint canonical serialized content after Powdrr has added structural
fields. Model response fingerprints are evidence inputs, not authoritative
record fingerprints.

### Structrr projection

Compile a minimal Structrr diff from canonical records:

- one source reference to the instruction ledger;
- one active intent clause per product obligation;
- one acceptance criterion per obligation;
- one required test contract per obligation;
- only actual repository entities selected by deterministic source context;
- explicit constraints and non-goals by reference; and
- no copy of the full feature description in each operation.

The generated Structrr document is a projection. The instruction ledger and
obligation registry are canonical for this flow.

Do not emit placeholder entities, features, guidance, invariants, tools, and
relationships solely because those sections exist. An empty section is better
than a model-authored fake object.

### Proposal compilation

Retain deterministic proposal compilation, but reduce semantic review volume.
Structural proposal checks must run without model calls:

- source files exist;
- all references resolve;
- all added obligations have contracts;
- all contracts refer to active obligations;
- selectors are syntactically valid and provider-compatible;
- planned selectors are explicitly marked planned;
- operations contain supported actions;
- allowed paths are valid; and
- all fingerprints are current.

Do not compile separate semantic review decisions for every repeated Structrr
projection of the same obligation. At most one pre-implementation semantic
review is needed per canonical obligation:

> Does this design projection preserve the meaning of this instruction clause?

The response is `preserved`, `altered`, or `unknown` plus an explanation.
Powdrr owns the obligation identity and evidence binding.

## Procedrr flow redesign

Replace the planning portion of
`docs/procedrr/skill-definitions/implement-feature.yaml` with the following
logical sequence. Exact operation names may change, but each operation must
have one responsibility and a typed return contract.

1. `ensure_current_structrr`
2. `discover_validation_profiles`
3. `compile_instruction_candidates`
4. for each candidate: `judge_clause_atomicity`
5. for each non-atomic candidate: `split_clause`
6. `finalize_instruction_ledger`
7. for each clause: `judge_clause_disposition`
8. `assert_clause_dispositions_complete`
9. `gather_clause_repository_context`
10. for each obligating clause: `construct_design_projection`
11. `compile_feature_obligations`
12. `compile_required_test_contracts`
13. `compile_structrr_projection`
14. `validate_compiled_feature_plan`
15. for each obligation: `review_design_projection`
16. `finalize_design_review`
17. `compile_implementation_packet`
18. `run_opencode`
19. `run_validation_profiles`
20. `validate_required_test_cases`
21. `run_verification_evidence`
22. for each obligation: `review_implemented_obligation`
23. `compile_repair_packets`
24. repair loop for failed obligations only
25. `review_changed_scope`
26. delivery wrapper: commit, PR, changelog, or Harbor result

Remove these current patterns:

- the broad `design-interview` call as the source of an authoritative plan;
- category-by-category LLM edits;
- asking models to return `intent_refs`;
- `apply_sentence_design_trace` as a mutation of model-authored plan sections;
- separate full-list `requirement_decisions`, `reflection_decisions`, and
  `repaired_design_decisions` passes;
- the hundreds-of-items generic proposal semantic review;
- whole-feature completeness review after per-obligation review;
- model-generated free-form completeness, scope, and intent repair prompts.

The final workflow must remain single-decision normal form: each judge receives
one explicit subject and one smallest useful question. Single-decision normal
form concerns the input decision boundary, not merely a small output schema.

## Workrr implementation changes

### New modules

Create focused modules rather than expanding `feature_endpoint.py` indefinitely:

```text
src/powdrr_lift/core/instruction_ledger.py
src/powdrr_lift/core/feature_obligation.py
src/powdrr_lift/core/implementation_packet.py
src/powdrr_lift/core/obligation_review.py
src/powdrr_lift/workrr/instruction_compiler.py
src/powdrr_lift/workrr/feature_plan_compiler.py
src/powdrr_lift/workrr/obligation_reviewer.py
```

Suggested responsibilities:

- `core/instruction_ledger.py`: immutable source, clause, disposition, and
  ledger contracts; IDs; fingerprints; reference validation.
- `core/feature_obligation.py`: design projection, obligation, and contract
  records.
- `core/implementation_packet.py`: worker and repair packets.
- `core/obligation_review.py`: semantic review result and aggregate status.
- `workrr/instruction_compiler.py`: deterministic segmentation and model
  response binding.
- `workrr/feature_plan_compiler.py`: obligation, contract, and Structrr
  projection compilation.
- `workrr/obligation_reviewer.py`: evidence packet construction and result
  binding.

`feature_endpoint.py` should orchestrate these services and adapt them to
Procedrr operations. It should no longer own their domain logic.

### Replace current functions

| Current function | Replacement |
| --- | --- |
| `_decompose_feature_description` | `InstructionLedgerCompiler.compile_candidates` |
| `_apply_sentence_design_trace` | remove; compile projections instead |
| `_update_plan_from_sentence_trace` | remove; compile a new plan from canonical records |
| `_compile_feature_obligations` | thin adapter over `FeaturePlanCompiler` |
| `_derive_feature_test_contracts` | replace with typed contract compiler that cannot accept model refs |
| `_materialize_feature_intents` | compile active intent directly from obligations |
| `_prepare_proposal_review` generic worklist | structural checks plus one semantic decision per obligation |
| `_aggregate_intent_review` | aggregate obligation reviews bound by Workrr |
| free-form repair request generation | deterministic `RepairPacketCompiler` |

### Model response binding

Introduce a general helper for bounded semantic decisions:

```python
def bind_model_result(
    subject: TypedSubject,
    response: Mapping[str, object],
    response_schema: ResponseSchema,
    input_fingerprint: str,
) -> BoundSemanticResult: ...
```

The subject is never serialized as an editable response field. The helper:

1. validates the semantic response;
2. adds the subject identity;
3. adds input and response fingerprints;
4. records provider/model metadata;
5. rejects unknown fields; and
6. persists the bound result.

This should replace prompts that instruct a model to "return identity fields
exactly as supplied." Identity copying is unnecessary work and a source of
failure.

## OpenCode integration

OpenCode remains the sole code-editing model. Change its prompt construction to
render `ImplementationPacket` or `RepairPacket` records.

Every implementation prompt must state:

- implement every listed obligation;
- create each exact required test selector;
- do not alter generated Powdrr artifacts;
- edit only allowed paths;
- use relevant existing tests and repository conventions supplied in context;
- run useful focused checks while editing; and
- do not claim completion when a listed selector is absent.

Do not send OpenCode:

- raw proposal worklists;
- repeated full Structrr sections;
- fingerprints it cannot act on;
- internal review machinery;
- sibling obligations unrelated to the current repair; or
- permission to modify contract IDs or selectors.

For initial implementation, one broad implementation packet may include all
obligations so OpenCode can build a coherent feature. Repairs must be scoped to
one failed obligation or a small connected obligation group.

## Validation and review

### Deterministic pre-implementation validation

The feature plan cannot reach OpenCode unless all of these pass:

1. Every instruction clause has exactly one disposition.
2. Every non-context disposition has one design projection.
3. Every required projection has one obligation.
4. Every obligation references an existing clause and projection.
5. Every obligation has at least one required test contract.
6. Every contract references only existing obligations.
7. Every provider and profile exists in discovered inventory.
8. Every selector is either discovered or explicitly `planned`.
9. No authoritative ID or reference originated in a model response.
10. Every original source span is covered by at least one final clause.
11. No final clause lacks a path to either an obligation or an explicit
    non-obligating disposition.

Failures must name the exact record and expected repair. Never collapse them
into `review_failed` without a durable diagnostic artifact.

### Deterministic post-implementation validation

After OpenCode edits:

1. Every planned selector must be discoverable from the provider inventory.
2. Every contract must execute and produce evidence.
3. Required validation profiles must pass.
4. The candidate tree must remain unchanged during read-only validation.
5. Changed paths must be within policy.
6. Every changed path and hunk must be associated with one or more obligations
   or their verification.
7. Generated planning and telemetry artifacts must not appear in the benchmark
   implementation patch.

The Harbor adapter should keep telemetry outside the repository candidate tree
or ensure it is ignored before diff capture. Benchmark patches must contain
product code and tests, not `.powdrr`, Structrr baselines, or proposal files.

### Semantic implementation review

For each obligation, build a bounded evidence packet containing:

- original instruction clause;
- design projection;
- acceptance criterion;
- relevant diff hunks selected by changed-path and source-subject closure;
- required test source;
- required test result;
- relevant validation failures; and
- dependencies on other obligations.

Ask exactly:

> Does this implementation evidence satisfy this one obligation?

Allowed response fields are `verdict` and `explanation`. The model cannot
return IDs or evidence references.

### Scope review

Scope review should be mostly deterministic:

- map changed files to implementation packets and source subjects;
- map test changes to contracts;
- flag unmapped hunks;
- permit repository-required incidental changes only through explicit rules;
- ask the model about one unmapped hunk at a time only when semantic judgment is
  required.

Do not ask one model call whether the entire diff is justified.

## Repair behavior

Repair is obligation-driven, not review-name-driven.

1. Gather failed deterministic checks and failed obligation reviews.
2. Group only failures that share source subjects or required selectors.
3. Compile one repair packet per independent group.
4. Run OpenCode with the repair packet.
5. Rediscover selectors and rerun affected contracts.
6. Run repository validation.
7. Rerun semantic review only for repaired obligations and obligations whose
   evidence closure changed.
8. Run final deterministic scope validation.

Never regenerate the instruction ledger, dispositions, or design projections
during implementation repair. A design change requires an explicit replan with
a new ledger or projection revision.

## Persistence and telemetry

Persist the following under the run output directory:

```text
instruction-source.json
instruction-ledger.json
clause-decisions/
  instruction-001-atomicity.json
  instruction-001-disposition.json
design-projections.json
feature-obligations.json
required-test-contracts.json
repository-context/
implementation-packet.json
implementation-attempts/
verification-evidence/
obligation-reviews/
repair-packets/
run-result.json
events.jsonl
```

Each artifact must include schema version, input fingerprint, output
fingerprint, and relevant upstream references. Store raw model responses beside
bound records for diagnosis, but never use raw responses as downstream inputs.

`run-result.json` must expose a typed failure stage and durable diagnostic, for
example:

```json
{
  "status": "planning_failed",
  "stage": "contract_compilation",
  "failures": [
    {
      "code": "obligation_missing_contract",
      "subject": "obligation:instruction-007",
      "message": "No required test contract was compiled."
    }
  ]
}
```

Do not report all planning, proposal, validation, and review failures as the
single status `review_failed`.

## Harbor and standard Git wrappers

Keep one shared core feature flow:

```text
shared instruction/design/implementation/validation/review core
    |
    +-- standard wrapper: fetch, worktree, branch, commit, push, PR, changelog
    |
    +-- Harbor wrapper: current checkout, local commit, artifact export, verifier
```

Delivery instructions from the feature text must be interpreted relative to
the wrapper. For example, "create a new branch" is already satisfied by the
standard wrapper and is not applicable inside a Harbor-owned task checkout.

The Harbor wrapper must:

- set the actual benchmark task ID as the work-item name;
- place Powdrr telemetry outside the candidate patch;
- export the complete run directory as an artifact even on failure;
- return a nonzero agent exit only after writing `run-result.json`;
- preserve the model patch and verifier result; and
- install the exact requested Powdrr revision with a recorded commit hash.

## Migration strategy

Implement this redesign as a sequence of end-to-end PRs. Every PR must leave
the production flow valid and add tests for its boundary.

### PR 1: Canonical instruction ledger

Implement:

- instruction source and clause types;
- deterministic segmentation with source spans;
- stable ID and fingerprint generation;
- atomicity and split response binding;
- disposition response binding;
- ledger persistence and validation; and
- DeepSWE instruction fixture tests.

Initially, adapt ledger output back into the existing flow so behavior remains
compatible.

Exit gate:

- the complete state-data description produces stable clauses across runs;
- wrapped lines do not create clauses;
- compound sentences split only through bounded decisions;
- every source span is covered; and
- no model response can set a clause ID.

### PR 2: Canonical design and obligation compiler

Implement:

- bounded design projections;
- feature obligation v2;
- deterministic active-intent projection;
- required test contract v2;
- exact planned selectors; and
- minimal Structrr projection.

Remove model-authored IDs and references from the planning path.

Exit gate:

- every required DeepSWE clause has an obligation and exact contract;
- no arbitrary model reference survives compilation;
- deleting one contract yields a precise deterministic failure; and
- Structrr output size grows approximately linearly with obligation count,
  without copying the full feature description into every operation.

### PR 3: Compact OpenCode implementation packets

Implement:

- repository context packets;
- existing-test inventory context;
- implementation packet rendering;
- exact-selector instructions; and
- telemetry for prompts and attempts.

Exit gate:

- a deterministic OpenCode double can create every requested selector;
- downstream provider discovery finds every selector;
- missing one selector fails with its contract ID; and
- OpenCode receives no model-authored identifier.

### PR 4: Obligation review and targeted repair

Implement:

- bounded evidence packets;
- one semantic review per obligation;
- bound review results without echoed IDs;
- deterministic repair packet compilation;
- affected-obligation re-review; and
- deterministic hunk scope mapping.

Remove whole-feature completeness and whole-diff scope prompts.

Exit gate:

- one failed obligation creates one targeted repair packet;
- unrelated passing obligations are not resent for repair;
- repaired evidence invalidates only affected reviews; and
- completion requires all obligations and deterministic checks to pass.

### PR 5: Harbor hardening and live-provider gate

Implement:

- complete failure artifact export;
- telemetry outside candidate patches;
- exact installed revision recording;
- benchmark task ID propagation;
- typed run failure stages; and
- an opt-in live DeepInfra integration test.

Exit gate:

- the state-data task reaches OpenCode with all obligations and contracts;
- the implementation patch excludes Powdrr planning artifacts;
- the verifier evaluates actual source and test changes; and
- a failure preserves enough artifacts to identify the exact clause,
  obligation, contract, model response, or validation command responsible.

## Test plan

### Unit tests

Add tests for:

- punctuation, Markdown lists, wrapped prose, abbreviations, and source spans;
- deterministic IDs and fingerprints;
- duplicate, missing, and stale references;
- model responses containing forbidden structural fields;
- invalid atomicity splits;
- disposition enums and clarification behavior;
- obligation compilation;
- selector derivation and collision handling;
- Structrr projection without repeated feature prose;
- implementation packet size and contents;
- review result binding;
- repair packet grouping; and
- typed failure serialization.

Property tests should generate arbitrary clause text and prove:

- IDs do not depend on model output;
- every final clause maps to its source;
- every obligation reference resolves;
- contract compilation is deterministic; and
- serialization round trips preserve fingerprints.

### Deterministic end-to-end test

Use the exact
`python-statemachine-state-data-scoping` instructions as a committed fixture.
Run them through the real implement-feature flow with:

- a schema-valid deterministic planning double that returns only semantic
  fields;
- a fixture repository with discoverable pytest profiles;
- a fake OpenCode worker that reads the implementation packet and creates every
  exact requested selector; and
- the real verification compiler and provider discovery.

Assert:

1. Every final clause has a disposition.
2. Every obligating clause has a design projection.
3. Every design projection has an obligation.
4. Every obligation has a required test contract.
5. Every contract has an exact canonical obligation reference.
6. The worker receives every obligation and selector.
7. Every selector exists after implementation.
8. Verification evidence passes.
9. Every obligation review passes.
10. The flow reaches `completed`.
11. The produced candidate patch excludes `.powdrr`, proposal, and baseline
    artifacts.

The test must not hard-code idealized LLM-created IDs because model responses no
longer contain IDs.

### Adversarial model-output tests

For every semantic model boundary, test responses that:

- include extra `id`, `intent_refs`, or selector fields;
- omit required semantic fields;
- contain empty prose;
- return stale copied context;
- return too many split statements;
- classify every sentence as context;
- produce identical generic design text for all clauses; and
- attempt to alter original instruction wording.

Forbidden structural fields must fail schema validation. Suspicious semantic
degeneration must produce a precise diagnostic or retry, never a valid plan.

### Live-provider integration test

Add an opt-in test, excluded from ordinary CI, that sends the complete DeepSWE
description through the real planning provider and stops before OpenCode.

The test validates structural invariants only. It does not assert exact model
wording. It must prove that arbitrary provider output cannot affect canonical
IDs or references and that the compiled plan validates.

Run this test manually before every benchmark rerun until the architecture is
stable.

### Harbor acceptance run

The final acceptance run must:

1. install the merged Powdrr commit explicitly;
2. pass DeepInfra credentials through Pier agent environment variables;
3. run the single state-data task in Docker;
4. capture the complete Powdrr run directory and command log;
5. produce a source-and-test patch;
6. run the DeepSWE verifier; and
7. retain artifacts regardless of pass or failure.

## Rollout and compatibility

Do not attempt to support both model-authored and compiler-authored references
inside the new flow. That would preserve the unsafe boundary.

During migration:

- keep old artifact readers for historical telemetry if necessary;
- version all new schemas;
- adapt v2 canonical records into existing Structrr consumers;
- switch `implement-feature` atomically to compiler-authored records once the
  deterministic end-to-end test passes; and
- remove dead design-interview generation code after confirming no other flow
  imports it.

Historical plans containing arbitrary references remain historical. Do not
fuzzy-migrate them into active obligations.

## Operational limits

Bound model and workflow work explicitly:

- maximum source description size before chunked capture;
- maximum clause candidates;
- maximum split children per candidate;
- one atomicity retry per clause;
- one design retry per obligation with a precise validation diagnostic;
- one semantic pre-implementation review per obligation;
- bounded diff hunks per implementation review;
- bounded repair attempts per obligation; and
- maximum total model activations with a durable exhaustion diagnostic.

Large features should remain linear in clause and obligation count. The system
must not create a Cartesian product between obligations and repeated Structrr
categories.

## Completion criteria

This redesign is complete only when all of the following are true:

- A planning model cannot inject an authoritative ID or reference through any
  accepted response schema.
- The complete DeepSWE state-data instructions compile into a valid instruction
  ledger, design, obligation set, and verification contract set.
- Every obligating instruction is traceable to at least one exact test contract.
- OpenCode receives all obligations and exact selectors in a compact packet.
- Required selectors are discovered and executed after editing.
- Semantic review operates one obligation at a time.
- Failed reviews create deterministic targeted repair packets.
- Scope validation identifies unmapped files and hunks.
- Structural failures identify a precise stage, code, and subject.
- The standard Git flow and Harbor flow reuse the same core pipeline.
- A real DeepInfra planning run cannot reproduce an arbitrary reference such as
  `feature_data_ownership` in an authoritative plan.
- The Harbor benchmark patch contains implementation code and tests, not
  Powdrr's internal planning artifacts.

## First implementation task

The first agent should implement PR 1 only. It should begin by adding the core
instruction-ledger records and deterministic segmentation tests using the exact
DeepSWE state-data description. It must adapt the ledger into the current flow
without yet rewriting OpenCode or review behavior.

Before opening that PR, the agent must run the repository's complete formatter,
lint, type-check, workflow-definition validation, and test suites. The PR must
include evidence that stable clause IDs and complete source-span coverage are
independent of all model responses.
