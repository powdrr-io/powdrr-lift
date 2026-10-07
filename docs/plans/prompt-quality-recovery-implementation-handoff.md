# Implementation handoff: recover measurable instruction-to-prompt quality

## 1. Objective and execution contract

Improve the generic instruction-to-prompt pipeline so its **captured worker
prompt** preserves required behavior, supplies useful acceptance checks, and does
not contradict the source. Completing additional classifier stages is not evidence
of success. Each implementation PR must demonstrate its effect in rendered prompts.

This document specifies future implementation. Do not implement all steps in one
PR. Execute PRs 1–5 in order, with the stop conditions below. Rebase each step on
the merged predecessor. Follow repository `AGENTS.md`, use a dedicated worktree,
open a PR, and let the user merge it. Do not start a coding benchmark as part of
this plan. Implementation of Powdrr itself is authorized when the user asks the
implementing agent to execute the plan; execution of MiniSWE/OpenCode on benchmark
tasks is a separate activity requiring an explicit request.

This is a recovery plan for the already implemented
[acceptance-criteria handoff](checkable-acceptance-criteria-implementation-handoff.md)
and [classifier handoff](classifier-semantic-quality-handoff.md). Reuse their
implemented structures and review stages. Do not restart those projects or add a
parallel pipeline.

The inspected implementation base is main
`5d6ffc2a6a52050151deb4d405b36023ce35cc35`. Recheck symbol names at your branch base.
The failing capture described below used PR #938's merge commit
`4394a2c88d0c76cf4b121df1212ee713bcbceeff`; do not describe that capture as having
been generated at the newer documentation base.

Nonnegotiable boundaries:

- Production generation sees the unchanged instruction, bootstrap, and permitted
  repository evidence. Solution patches, verifier patches, reference rubrics, and
  ideal prompts are offline evaluation inputs only.
- Context remains context and creates no obligations. Present tense alone does
  not establish context; a requested interface or invariant is often present tense.
- Parser/provider errors are processing failures, not evidence of source ambiguity.
- Powdrr owns task worktrees, branches, commits, PRs, and validation orchestration.
  Delivery instructions must not become worker product requirements.
- Preserve deterministic spans, source ancestry, compiler-owned IDs/fingerprints,
  existing assertion-faithfulness checks, and bounded repair limits.
- Do not add GraphQL-specific production predicates, symbol lists, or expected
  answers. GraphQL supplies regression examples, not implementation rules.
- Prompt capture may use planning/classification/research models. It must invoke
  zero coding-agent processes and must stop before worker execution and its repair
  loop. Do not use a full Pier run merely to obtain a prompt.
- Never print credentials, whole environment dumps, credential-bearing saved
  configs, or process arguments containing API keys. Use presence-only checks.

## 2. Evidence to preserve before editing implementation

Capture root, if still available:

```text
/private/tmp/pier-gql-provider-error-merged-20261006-retry3/
  2026-10-06__22-12-13/gql-incremental-graphql-delivery__5yZFPUQ/artifacts/
    powdrr-gql-provider-error-merged-20261006-retry3/
```

Keep compact, sanitized fixtures in
`docs/evaluations/prompt-quality-recovery/` and compiler regression fixtures in
`tests/fixtures/prompt_quality_recovery/`. These are proposed new directories.
Do not copy the entire run, its agent trajectory, environment, config, or lock file.

Retain these facts with file hashes, capture revision, task ID, and source excerpt:

1. `acceptance-criteria.json` reports `requirements: 16`, `source_only: 16`,
   `checkable: 0`, and `with_criteria: 0`. Every requirement has failure stage
   `criterion_generation`, reason `criterion is not valid JSON`, and two repair
   attempts in its coverage record. Also preserve the differing repair-counter
   fields; do not silently correct the historical capture.
2. The first criterion result is prose beginning
   `interface: session.execute_incremental(query) is an async generator; ...`.
   It passed the outer schema because `criteria.items` is a string. The compiler
   called `json.loads` on that string and rejected it. This failure is reproduced
   with saved input/output; it does not need another API call.
3. `semantic-contracts/instruction-009/partial-contract.json` describes the source
   requirement `The .data dict is accumulated across payloads, not raw deltas.`
   as `routing: exclude`, `disposition: context`, and records `invalid_response`
   for several interpretation fields. Do not assume the exact interpretation
   exception without extracting and reproducing its raw response.
4. The prompt labels `.data`, `.errors`, data accumulation, field overwrites,
   WebSocket forwarding, and DSL `.stream()` as context-only. It also repeats the
   original instruction containing those requirements, creating contradictory
   directions in one prompt.
5. `verification-obligations.json` says `complete: true`, `failures: []`, with
   16 obligations, despite the missing acceptance checks. That flag is not proof
   that acceptance coverage is complete.
6. All 399 recorded Procedrr provider attempts succeeded. This does not mean
   all outputs were semantically usable; some JEV calls were skipped separately.
7. The coding stage was started by mistake and then manually cancelled. There
   is no completed worker or verifier result. Do not use its unfinished patch as
   evidence for or against prompt quality.

Copy the **actual worker prompt** from `artifacts/prompts/index.json` and its
referenced text file. Preserve `implementation-prompt.md` separately as a design
projection. Evaluate the indexed worker prompt; the projection can differ from
what the worker receives. Include relevant raw criterion outputs, source contracts,
coverage summaries, and only the operation inputs required for local repro.

If temporary artifacts are missing, mark historical evidence unavailable. Rebuild
a fresh baseline with prompt-only mode and record the new revision. Never invent
the historical raw response or reconstruct a baseline from memory.

## 3. Existing implementation map

Paths are relative to `src/powdrr_lift/` unless otherwise specified.

| File or symbol | Current behavior / required use |
| --- | --- |
| `docs/procedrr/skill-definitions/design-interview.yaml` | Contains generation, review, and repair contracts. Both criterion generation and repair currently require JSON object strings. Source interpretation evidence/reason pairs are loose strings. |
| `workrr/acceptance_criterion_compiler.py`: `prepare_acceptance_criteria`, `bind_acceptance_criteria`, `_decode_object`, `_bind_criterion` | Prepares requests, parses encoded objects, binds indexes, and computes IDs. Already accepts mappings inside `_decode_object`. Preserve local membership/duplicate checks. |
| Same module: `prepare_acceptance_criterion_reviews`, `bind_acceptance_criterion_reviews`, `prepare_acceptance_criterion_repairs`, `bind_acceptance_criterion_repairs` | Reuse existing assertion review and two repair rounds; change generation and repair consistently. |
| `workrr/semantic_contract_compiler.py`: `CLASSIFIER_DEFINITIONS`, `prepare_source_semantic_decisions`, `prepare_dependent_source_semantic_decisions`, `_containing_source_sentence` | Routing/disposition prompts and source context. Currently containing-sentence context may be absent for an unsplit sentence and does not supply the preceding feature-setting sentence. |
| Same module: `prepare_source_interpretation`, `bind_source_interpretation` | Interpretation binding catches `SourceInterpretationError` in benchmark mode and produces unresolved fields with `invalid_response`, losing the precise exception. |
| `core/source_interpretation.py`: `SourceInterpretation.bind`, `validate_source`, `_parse_pairs` | Validates field evidence against source text; persisted evidence/reasons use `field\|value` strings. Keep source-faithfulness validation. |
| `core/instruction_ledger.py`, `core/classifier_input.py` | Reuse source ancestry and context construction for splitting and classification. Inspect existing functions before extending them. |
| `workrr/acceptance_contract_compiler.py`: `prepare_behavioral_contracts`, `bind_behavioral_contracts` | Separates product requirements from context before grouping. A wrong upstream exclusion removes a requirement from criterion generation. |
| `workrr/command_catalog.py`: `_attach_behavioral_contract_context` | Renders supporting context as `Context only; this is not an implementation requirement`. This label must only be used for genuinely classified context. |
| `core/behavior_contract.py`, `core/implementation_packet.py`, `workrr/coding_agent.py` | Criterion data and final worker rendering. Do not add a second renderer for evaluation. |
| `workrr/feature_endpoint.py`: `WorkrrFeatureConfig.capture_worker_prompts_only`, `_worker_prompt_capture_flow_source`, `_capture_worker_prompt` | Existing production prompt capture boundary; reuse it. |
| `cli.py`: `harbor-feature --capture-worker-prompts-only` | Existing CLI mode. `--design-only` does not replace capture of the actual worker prompt. |
| `workrr/semantic_prompt_cases.py`: `audit_prompt_claim_presence` | Phrase presence check only. It cannot establish semantic coverage or resolve contradictions. |
| `workrr/deepswe_design_evaluation.py`: `evaluate_deepswe_worker_prompt` | Existing final-prompt evaluator checks task/source/rubric identity, prompt hashes, and reference patch symbols/tests. Extend this path. |

Relevant tests: `test_acceptance_criterion_compiler.py`,
`test_acceptance_contract_compiler.py` if present (locate by binder symbol if not),
`test_semantic_contract_compiler.py`, `test_source_interpretation.py`,
`test_semantic_decision.py`, `test_instruction_ledger.py`, `test_classifier_input.py`
if present, `test_behavior_contract.py`, `test_feature_endpoint.py`, `test_cli.py`,
`test_semantic_prompt_cases.py`, `test_deepswe_design_evaluation.py`,
`test_implementation_packet.py`, and `test_procedrr.py`.

## 4. Shared output and failure contracts

### 4.1 LLM work versus deterministic work

The model interprets behavior, selects related requirements, and proposes observable
checks with evidence. Python owns IDs, hashes, schema versions, source-index
binding, deduplication, request envelopes, persistence, and coverage aggregation.
Do not ask the model to stringify JSON or generate those metadata fields.

Use provider-compatible JSON Schema for the actual objects. Keep `uniqueItems`
out of remote schemas; validate uniqueness locally. Preserve existing max sizes.
Prefer bounded objects/arrays and enums. Do not assume arbitrary JSON `expected`
values are supported by the provider's grammar; the concrete wire format below
avoids that problem.

### 4.2 Proposed criterion wire contract

This is a new provider wire contract, `acceptance-criterion-wire-v2`. Keep persisted
`AcceptanceCriterion` semantics unless an independent persisted version change is
actually needed. Do not inject the wire revision into the model output: the caller
owns it and includes it in replay/cache identity.

```json
{
  "criteria": [{
    "kind": "state_transition",
    "source_indexes": [0],
    "setup": "An incremental execution with two payloads contributing different fields.",
    "operation": "Consume session.execute_incremental(query).",
    "events": [
      "Receive a first payload contributing field a.",
      "Receive a second payload contributing field b."
    ],
    "assertions": [{
      "observation": "The second yielded result's data.",
      "relation": "satisfies",
      "expected": "Contains contributions a and b, rather than only b.",
      "source_indexes": [0],
      "basis": "source_derived"
    }],
    "unresolved_questions": []
  }]
}
```

For this wire version:

- Require exactly the seven existing criterion fields shown above; forbid extra
  properties. `criteria` has at most four entries. Each entry is an object.
- `kind` uses existing kinds; `source_indexes` is a nonempty integer array.
- `setup` is a nonempty string or null. `operation` is a nonempty string.
- `events` is an array of nonempty strings, maximum eight; empty is valid for a
  single-operation criterion. Assertions remain limited to four, minimum one.
- An assertion is an object with exactly the existing five fields. `relation`
  and `basis` use the existing enums. `expected` is a nonempty descriptive string
  for this wire version, not a JSON-encoded string. The observation and relation
  determine how it should be assessed. Existing persisted fixtures containing
  native JSON expected values remain readable.
- Keep assertion indexes nonempty, in range, and a subset of criterion indexes.
  Reject bools as indexes. Bind them to compiler-owned requirement IDs.
- `unresolved_questions` is an array of nonempty strings, maximum eight.
- Use bounded strings: 1,000 characters for setup/observation/expected, 500 for
  operation/events/questions. These are implementation limits; never truncate
  a required behavior silently to satisfy them.

Use the same shape for repair. Do not leave repair on the string-based contract.
Use a YAML anchor on the generation schema and an alias on the repair schema,
then test that the production workflow loader expands both to the same object
shape. If that loader rejects aliases, use an existing schema-reference mechanism
or a shared Python factory supported by the loader. Avoid a new general schema
framework just for this change.

Legacy replay compatibility: parse old valid JSON strings only when reading
explicitly legacy records. A prose string is invalid and is never repaired by
guessing at its punctuation. New live outputs must pass the typed wire schema.
Change schema fingerprints/cache keys so old string-schema responses cannot be
reused as v2 responses. Keep persisted fingerprints truthful.

### 4.3 Processing failures versus source uncertainty

Record processing failure category, stage, source/request ID, bounded exception
message, wire revision, attempt count, and whether repair recovered it. A valid
transport response is not a valid domain response.

Source ambiguity is a valid interpretation that cites source text and identifies
an open meaning. Bad JSON, missing fields, invalid enums, missing evidence, stale
references, and provider rejections are processing failures. Normative defaults
must never reinterpret these failures as product decisions.

Preserve successfully bound/reviewed assertions during a local repair. Scope a
malformed criterion's failure to its claimed requirements when those claims are
trustworthy; if indexes are unusable, conservatively mark the request's coverage
unknown. Do not allow one bad criterion to silently discard unrelated accepted
criteria, and do not claim coverage for rejected output.

## 5. PR 1 — typed criteria and reproducible local failures

Implement in this order:

1. Retain sanitized fixtures from section 2. Include the original prose result and
   a small hand-written **structurally valid** v2 response. The latter is a schema
   fixture, not evidence that the live model has improved.
2. Add the shared wire schema and its revision identity. Update both criterion
   generation and criterion repair in `design-interview.yaml` to return objects
   and remove all instructions about JSON object strings.
3. Adapt binders at the wire boundary; keep compiler IDs, review semantics,
   source subset checks, bounds, and persisted criterion behavior.
4. Preserve precise parse/schema failures and normalize repair counters so the
   nested quality record and requirement-coverage record agree. Add new fields
   additively or version their artifacts; legacy captures remain readable.
5. Replay the compact captured failure locally through the production binder.
   Then run targeted live criterion calls using the corrected schema, with source
   inputs only. Store sanitized response metadata and resulting criteria.
6. Capture a new GraphQL worker prompt with section 10's boundary. Compare it to
   the saved baseline. Do not proceed to PR 2 while criterion generation/repair
   still fails due to the wire format.

Required regressions:

| Case | Expected result |
| --- | --- |
| Captured prose in old string array | Repro reports the historical parse failure; never declares it checkable. |
| Valid typed criterion + assertion | Binds stable IDs and source refs and enters existing faithfulness review. |
| Typed repair response | Replaces only failed/missing checks; accepted assertions remain present. |
| Empty/unknown fields, enum errors, bool/out-of-range/duplicate indexes | Rejected locally with actionable stage/error. |
| One good and one malformed criterion | Good output survives; coverage affected by malformed output is honest. |
| Provider grammar error in HTTP 200 SSE | Existing PR #938 error handling remains intact; no empty-content retry. |
| Generation versus repair schemas | Structural equality and identical limits. |
| Changed wire revision with old replay entry | No stale cache/replay hit. |

Completion evidence: valid live criterion output reaches the final prompt; no
criterion JSON-string decoding failures remain in the new capture. Do not require
complete semantic coverage yet, since routing errors remain for PR 3. Report the
remaining requirement/check counts and why each uncovered requirement is missing.

## 6. PR 2 — usable source interpretations and bounded recovery

First reproduce an actual captured `SourceInterpretation.bind` failure. Record
the precise exception without dumping request credentials. Inspect whether it
comes from evidence/reason pair encoding, missing evidence, invalid fields, or a
different cause. The plan does not claim those failures all share one cause.

Then implement:

1. Introduce `source-interpretation-wire-v2`, a typed provider response with
   `field_evidence: [{field, quote}]` and
   `unresolved_fields: [{field, reason_code}]`. Use existing valid fields/reason
   codes and compiler-owned provenance. Do not allow arbitrary pipe strings as
   the new wire contract. Use one bounded call, not one model call per field.
2. Adapt to the existing persisted representation at a deterministic boundary
   or explicitly version it; keep old files readable. A quote containing a pipe
   must remain a quote rather than becoming an extra field.
3. Preserve `validate_source` checks. Resolved fields require real source evidence.
   Derived clauses may use validated inherited context with explicit parent
   provenance; do not accept a quote merely because it occurs elsewhere in the
   full instruction. If parent evidence needs a persisted schema extension, add
   a typed evidence-source reference and a reader migration in this PR.
4. Replace silent benchmark-mode invalid-response conversion with precise failure
   diagnostics and at most two targeted repairs. Each repair receives the valid
   source/context, previous response, and exact validation error. Preserve valid
   fields where feasible; never reuse an invalid result as a successful cache hit.
5. At exhaustion, keep source requirements, label interpretation unavailable due
   to processing failure, and propagate degraded readiness. Do not convert the
   source to context or synthesize a semantic decision from the failure.

Required cases: pipe-containing quotes; null field with and without its reason;
non-source quotation; missing evidence for a resolved field; one invalid field
without losing valid fields; valid ambiguity distinct from malformed output;
two-round exhaustion; success after repair; strict and benchmark modes with the
same source preservation semantics; unsupported behavior-family label with a
clear interpretable operation.

Completion evidence: the captured interpretation failures are reproduced and
explained, then recovered or explicitly reported as processing failures. Clear
source requirements no longer pass through interpretation as anonymous
`invalid_response` fields while the run claims full readiness.

## 7. PR 3 — preserve requested behavior during routing and splitting

### 7.1 Routing and disposition changes

Change the **actual classifier definitions and inputs**, not just renderer labels.
Use `CLASSIFIER_DEFINITIONS`, its decision revision/fingerprint path, and the
Typesafe/JEV adapter/registry path found by searching for those revision names.
Keep local and hosted classification behavior aligned. Do not claim a hosted
definition was updated until its configured revision and response evidence show it.

Decision rule:

- Include properties/interfaces/invariants required of the requested system,
  even when expressed declaratively or inherited from a parent request.
- Context describes existing background, an observed problem, or an explanatory
  example without demanding that behavior of the requested implementation.
- Tense and descriptive polarity are insufficient to choose context. Evaluate
  reference, discourse role, and the containing request.
- Explicit delivery directives are excluded from product semantics. Excluding
  them must also remove their copied text from worker-facing product sections.
- If ambiguity is real, preserve the source as unresolved and make the uncertainty
  visible. Never route every descriptive statement into include, and never use a
  parser error as evidence that context/exclusion is appropriate.

Add bounded context through `classifier_input.py` and existing ledger ancestry:
exact target, containing source sentence, and at most one preceding/following
sentence when they establish the requested feature or resolve a reference.
Default cap: 2,000 context characters excluding the target; trim complete optional
neighbor sentences first. Preserve required parent context or report its overflow.
Do not send the entire task repeatedly to every classifier. Feed compatible
context to routing, dependent disposition, and interpretation; include it and its
revision in cache identity.

The compiled representation may internally encode genuine context as
`routing: exclude, disposition: context`; do not change that normalization merely
to obtain prettier labels. These regression cases must cease being context.

### 7.2 Required contrast cases

Add cases to the existing corpus in its established family/domain/split scheme.
Do not weaken corpus minimums or move held-out domains to development to fit the
new examples. Each pair needs an expected classifier result **and final prompt
claims**, including forbidden context-only language for required behavior.

| Source / context | Expected product interpretation |
| --- | --- |
| `Implement execute_incremental ... The .data dict is accumulated across payloads, not raw deltas.` | Included sequence-dependent invariant, with the contrast preserved. |
| `Currently the transport emits only raw deltas; add incremental execution.` | Current raw-delta limitation is context; requested new behavior is included. |
| `Implement ... yielding result objects with .data, .has_next, .errors, and .extensions attributes.` | All four attributes inherit the same required interface predicate. |
| `The existing diagnostic result has an errors field. Add a separate progress API.` | Existing diagnostic fact is context; do not demand that field on unrelated progress values. |
| `The WebSocket transport must forward incremental payloads through the existing protocol.` | Included compatibility behavior, not context. |
| `The existing WebSocket protocol is documented below.` | Background context; no new support obligation. |
| `Extend the DSL: .defer() ... .stream() on list fields with optional label and initial_count parameters.` | Both requested DSL additions and stated optional parameters survive. |
| `For example, a tutorial uses a stream of log lines.` | Example does not introduce DSL support. |
| `Support nested paths navigating through lists by index, null values, field overwrites, and concurrent deferred/streamed fields.` | Preserve named capabilities; do not rewrite all items as path navigation. Null traversal behavior remains unspecified. |
| `Work on a new branch and commit everything.` | Powdrr process instruction; absent from worker product/acceptance sections. |

Add equivalent contrasts from existing state-machine, encoding, and query tasks.
Automated paraphrases may add variants, but require automated source-entailment
review and preserve variant-parent split membership. No task-name matching rules.

### 7.3 Splitting and propagation

Before changing splitting, inspect current `semantic_relations`,
`modifier_attachments`, sentence ancestry, and source spans. Reuse those data.
Shared predicates/modality must propagate to all named children. Do not turn
`support null values` into `navigate paths through null values` merely because it
follows a path phrase. Keep an unsplit source-backed group where attachment cannot
be resolved; do not invent a clearer English requirement.

Verify required clauses enter `prepare_behavioral_contracts` as requirement IDs,
then become criterion candidates, then appear as obligations/checks in the exact
worker prompt. Genuine context may support interpretation but never becomes an
asserted requirement through grouping. Context-only rendering must not contradict
an included obligation for the same source/span.

Completion evidence: every explicit GraphQL requirement in the contrast table
survives without a context-only negation, genuine context remains nonobligatory,
delivery commands are absent, and development cases improve without regressions
in already correct cases from other domains.

## 8. PR 4 — honest prompt readiness and executable final-prompt evaluation

### 8.1 Readiness report

Add an additive, separately versioned `prompt-quality-report.json` and reference
it from run metadata/result. Do not redefine the existing obligation `complete`
flag to mean criterion completeness without migrating its consumers.

Required report fields:

```text
schema_version, task_id, source_sha256, generation_revision,
worker_prompt_sha256, wire_revisions,
required_source_ids, preserved_source_ids, excluded_context_ids,
excluded_process_ids, missing_source_ids, conflicting_source_ids,
checkable_requirement_ids, source_only_requirement_ids,
processing_failures[{stage,source_refs,reason,attempts,recovered}],
status: ready | degraded | blocked
```

IDs and counts come from compiler artifacts, not an LLM's unverified total.
This production report measures structural consistency and processing health;
it does not prove classifier truth. Offline semantic evaluation remains necessary.

- `ready`: no missing required source IDs, contradictory routing/rendering, or
  unresolved processing errors. Any legitimate source ambiguity remains explicit.
- `degraded`: source preserved, but check derivation/interpretation processing
  failed or a material criterion remains source-only. Capture the prompt for
  diagnosis, but never count this run as an improved successful prompt.
- `blocked`: the source/prompt artifact is missing/stale or a contradiction/lost
  requirement prevents faithful rendering. Persist the diagnostic artifacts.
- Zero checks is not universally an error: a guidance-only/no-op task may not
  need behavior assertions. Explicit observable behavior that could not be checked
  because generation failed is degraded. Never invent assertions to raise a count.

Bounded repair should run before the final status. Avoid adding another unbounded
review loop. In prompt-only evaluation, capture degraded output for analysis;
automatic coding execution should not start on unresolved processing failures.
Expose this policy distinctly from legitimate product uncertainty.

### 8.2 Whole-prompt evaluation

Extend `evaluate_deepswe_worker_prompt`, using indexed provider-ready prompts and
their validated hashes. Scan all prompt sections, including copied product text,
source-only lists, constraints, and worker policy. A correct quote near the start
must not mask a later instruction to ignore the same behavior.

For every required meaning, report these separate dimensions:

- Preservation: present, missing, weakened, or contradicted.
- Acceptance: discriminating check, source restatement only, missing check, or
  legitimately not applicable.
- Evidence: exact source excerpt, exact worker-prompt quotes, supporting rubric
  evidence, and an explanation of a plausible wrong behavior the check rules out.
- Unsupported obligations: additions not entailed by source/permitted repository
  evidence. Classify illustrative fixture values separately from product rules.
- Conflicting process directions: worker text demanding branches/commits/PRs
  contrary to Powdrr's policy.

Use validated typed judge results. Check quoted evidence really occurs in the
prompt/source. Judge failure yields `evaluation_error`, not pass/fail product
evidence. Keep normalized phrase checks as inexpensive smoke checks only.

Test evaluator sensitivity with controlled mutations of an existing prompt:
remove accumulation, replace accumulation with raw deltas, remove extensions
replacement, mark a required clause context-only while retaining its source quote,
add a forbidden null-path rule, and inject branch/commit commands. The evaluator
must flag each relevant failure. A positive wording paraphrase must still pass.
These mutation fixtures are offline evaluator tests, not production instructions.

Completion evidence: the historical capture is explicitly degraded, the
contradictory context labels are detected even though the source is copied, and
the updated capture passes the applicable checks. A gate alone is not evidence
that generated prompts became better.

## 9. PR 5 — repeatable evaluation across tasks and limited human review

Reuse existing state-machine gold rubric and final-prompt evaluator. Add a GraphQL
rubric in the existing `deepswe-design-gold-v1` format rather than bypassing task
identity/source excerpt/reference-symbol validation. Add rubrics for skrub-duration
and ytt-jsonpath if their tasks/references are available. Keep GraphQL and
state-machine as development cases; designate at least one full additional task
as held out before tuning. Respect existing synthetic corpus domain splits.

For rubric creation:

1. Read the unchanged instruction, actual solution patch, and validation patch.
2. Generate candidate required meanings/checks automatically, each with exact
   instruction excerpts and supporting reference locations.
3. Classify each candidate as instruction-explicit, repository-supported, or
   reference-only. Reference-only details describe solution completeness but
   cannot count as missing instruction extraction unless permitted repository
   evidence supplies the requirement. Keep them in a separate report section.
4. Have a second assessment challenge omissions and invented requirements.
   Record disagreements and supporting quotes. Accept automated findings only
   after structural/source checks; request human review for unresolved conflicts
   and high-impact ambiguous expectations. If no independent configured judge is
   available, mark that limitation rather than inventing an adjudication result.
5. Use actual symbols/test names found in patches. Do not fabricate verifier
   names. Do not pass reference material into capture/generation processes.

Minimum GraphQL rubric meanings:

| Meaning | A useful acceptance check must distinguish |
| --- | --- |
| Async generator / result attributes | Awaiting a single final result or returning plain deltas instead of yielded result objects with all four attributes. |
| Accumulated data | After two distinct contributions, the second result includes both, not only the latest payload. |
| Payload-local extensions | With two different extensions payloads, the second result exposes the second payload's extensions, not accumulated metadata. |
| Deferred merge | An incremental data contribution updates the parent selected by its path, with existing unaffected fields retained. |
| Stream insertion | A start index inside an existing list distinguishes the stated indexed update from append-only behavior; do not invent replacement-versus-shift details beyond source/permitted evidence. |
| Missing path | Omitting path is treated as root path, not dropped or rejected. |
| Named path/value/update capabilities | List-index paths, null values, overwrites, and concurrent fields are retained without invented null-container traversal defaults. |
| Empty / hasNext-only payloads | A payload with empty incremental entries and one with hasNext only each still yield a result. |
| Error continuation | An error-bearing item does not prevent processing a following item. Error accumulation policy remains unspecified unless evidenced. |
| Transport coverage | HTTP multipart's stated parameters and WebSocket forwarding are checked on their respective paths; one path's pass cannot prove the other. |
| DSL | Both fragment types support defer; list fields support stream and the two named optional parameters. Do not invent optional-parameter defaults. |
| Process boundary | Source branch/commit demands are absent from worker instructions; Powdrr policy governs execution. |

Add a runner (proposed `scripts/evaluate-prompt-quality-recovery.py`) that accepts
explicit task dirs, capture roots, rubrics, generator revision, and configured
judge IDs. It must execute prompt capture/evaluation only, return nonzero on
critical semantic failures or evaluation errors, and emit a machine-readable
report plus a concise Markdown before/after table.

For each task, compare the old saved baseline to the new full capture. Replay is
valid for isolated component repro; do not use old routing/interpretation results
as proof of a fresh final pipeline run. Record all reused components explicitly.
Use fresh roots; keep exact instruction bytes, target base revision, model/config,
and research inputs as comparable as possible. Report changed evidence/settings.

Run one full capture per development task initially. If outputs vary or scores
disagree, run three captures with identical configuration and retain all results;
never choose only the best run. Do not repeatedly tune against held-out failures.
Changing the method based on a held-out example makes it a development example
and requires a new held-out case.

Release evidence must show:

- No critical source meaning missing, weakened, or contradicted on the selected
  development and held-out tasks.
- No newly invented product obligations or worker orchestration commands.
- Every rubric meaning marked checkable has at least one source-faithful,
  discriminating final-prompt check; guidance/real ambiguity is reported honestly.
- No unrecovered processing failures counted as successful prompt improvement.
- No regression on previously correct cases. Report all item-level results,
  rather than hiding a critical omission behind a higher average score.
- Model call count, retries, generation duration, and prompt size are reported.
  Quality cannot be declared improved merely because prompts got longer.

These are prompt-level quality claims. They do not establish that an agent will
pass every hidden test. Coding-agent outcome evaluation is a later, separately
authorized experiment after these prompt checks pass.

## 10. Exact prompt-only execution boundary

Use the existing `harbor-feature --capture-worker-prompts-only` mode. Ensure
`WorkrrFeatureConfig.capture_worker_prompts_only` is true; do not rely on
`--no-open-pr`, a missing executable, or later cancellation to stop coding.
Existing tests to extend include:

- `test_worker_prompt_capture_trims_real_implement_feature_at_worker_boundary`
- `test_prompt_capture_does_not_enter_coding_attempt_recovery_loop`
- `test_prompt_capture_persists_provider_ready_prompt_without_running_worker`

Use a worker test double that raises if invoked, and assert it receives no calls.
The capture should persist request, prompt index/hash, exact provider-ready prompt,
quality report, and `prompt_captured` status even for inspectable degraded output.

For a local isolated target checkout, invoke the CLI with a Python subprocess
argument array. This avoids shell interpolation of the instruction or credentials:

```python
# This snippet is a command recipe, not a new application entry point.
import os
from pathlib import Path
import subprocess

powdrr_worktree = Path("/absolute/path/to/the/implementation/worktree")
task_dir = Path.home() / "code/powdrr-deep-swe/tasks/gql-incremental-graphql-delivery"
target_checkout = Path("/private/tmp/gql-prompt-only-target")
output_root = Path("/private/tmp/gql-prompt-only-output-UNIQUE")
assert target_checkout.is_dir()
assert not output_root.exists(), "Use a fresh output root"
run_env = dict(os.environ)
run_env["PYTHONPATH"] = str(powdrr_worktree / "src")
run_env["VIRTUAL_ENV"] = "/Users/gregory/code/powdrr-lift/.venv"
run_env["PATH"] = run_env["VIRTUAL_ENV"] + "/bin:" + run_env["PATH"]
run_env["POWDRR_CLARIFICATION_POLICY"] = "normative_defaults"
command = [
    run_env["VIRTUAL_ENV"] + "/bin/python", "-m", "powdrr_lift.cli",
    "harbor-feature",
    "--feature-description", (task_dir / "instruction.md").read_text(),
    "--work-item-name", "gql-incremental-graphql-delivery",
    "--task-id", "gql-incremental-graphql-delivery",
    "--repo-root", str(target_checkout), "--allowed-path", ".",
    "--planning-provider", "deepinfra",
    "--capture-worker-prompts-only", "--output-root", str(output_root),
    "--json",
]
subprocess.run(command, env=run_env, cwd=target_checkout, check=True)
```

Run the recipe through `rtk proxy` and the shared interpreter. Prepare the target
checkout at the task's `metadata.base_commit_hash` first; locate repository URL
in `task.toml`. Use a fresh isolated clone/check-out and the normal validation
bootstrap. Do not use Powdrr's own repository as the GraphQL target. Do not apply
the solution or verifier patches to that generation checkout.

Verify required configured credentials with presence-only checks. Retain Typesafe
credentials (`TYPESAFEAI_API_KEY`, `TYPESAFE_API_KEY`, or `SYSTEM_ONE_API_KEY`) and
Tavily credentials through the environment, never CLI arguments. A correlated
Typesafe response marker establishes delivery; host-side presence alone does not.
If a container is actually necessary, forward those credentials and allowlist
`api.typesafe.ai` plus any override hostname and configured research endpoint;
inspect only `ALLOWLIST_DOMAINS`, never the complete environment object. Do not
use the full coding benchmark skill/workflow for this prompt-only task.

Read the capture's prompt index and compare task/source fingerprints before
evaluation. Then run:

```text
rtk proxy /Users/gregory/code/powdrr-lift/.venv/bin/python -m powdrr_lift.cli \
  evaluate-deepswe-prompt \
  --task-dir /Users/gregory/code/powdrr-deep-swe/tasks/gql-incremental-graphql-delivery \
  --run-dir /private/tmp/gql-prompt-only-output-UNIQUE \
  --rubric /absolute/path/to/the/GraphQL/rubric.yaml \
  --report /private/tmp/gql-prompt-only-evaluation-UNIQUE.json \
  --judge-provider deepinfra-cheap --json
```

Set `PYTHONPATH` to the implementation worktree's `src` when running this command.
Pass the GraphQL rubric explicitly: the CLI's default rubric is for state-machine
and must not be used for GraphQL. Provider/model overrides, if any, must be recorded.

## 11. Verification, PR contents, and stopping rules

For each PR, run focused regressions first, then repository-required format,
lint, type, and full test checks from `AGENTS.md`. Use the shared environment;
do not run `uv sync` or install a new editable checkout into it.

Example focused command for PR 1:

```text
rtk proxy /Users/gregory/code/powdrr-lift/.venv/bin/python -m pytest \
  tests/test_acceptance_criterion_compiler.py tests/test_procedrr.py \
  tests/test_feature_endpoint.py -q
```

Before any Python check, export worktree-specific `PYTHONPATH`, prepend the shared
environment's `bin` to `PATH`, and set its `VIRTUAL_ENV`. Use the same environment
for `ruff format --check .`, `ruff check .`, `mypy src tests`, and the required
full pytest command. Check current CI configuration for required flags/coverage.
Run targeted live calls only after deterministic tests pass. Do not blindly retry
permanent schema/request errors or restart the full capture to debug one binder.

Every implementation PR description must contain:

1. The exact failure fixed and before/after behavior, with artifact revision IDs.
2. A focused regression or captured-request repro that would fail before the fix.
3. Actual final-prompt quotes showing the intended improvement and any remaining
   missing/contradictory behavior. Before full reclassification is needed, identify
   component replay evidence as replay evidence, not a fresh full capture.
4. Coverage/readiness counts, processing errors, model calls, retries, duration,
   and prompt size when a new capture was performed.
5. Verification results and remaining limitations. Do not claim worker test success
   from prompt evaluation.

Stop the current implementation step and fix its underlying issue if:

- A transport/schema/parser failure is converted into semantic certainty.
- Repair still uses the legacy string contract after generation changed.
- A required behavior is excluded or negated as context in the final prompt.
- An evaluator passes contradictory directions because one source quote is present.
- Reference-only solution details enter production generation or expected routing.
- Any capture path launches MiniSWE/OpenCode or enters the coding repair loop.

Keep the user informed of the concrete failure and continue independent work
within the current step. Do not proceed to the next PR by declaring a degraded
capture successful. Persist useful failure artifacts and explain any external
blocking condition precisely.

## 12. Final deliverables checklist

- [ ] Sanitized historical fixture and honest baseline outcome limitation.
- [ ] Typed criterion generation **and repair**, compiler-owned metadata, valid
      provider-compatible wire contract, and legacy replay isolation.
- [ ] Typed interpretation evidence/reasons, preserved precise failures, and
      bounded recovery that does not lose source requirements.
- [ ] Generic routing/disposition/context improvements with contrast cases and
      source/modifier inheritance, including correct hosted classifier revision.
- [ ] Indexed worker prompt has no contradictory context labels or copied
      orchestration directives.
- [ ] Honest production readiness report distinct from traceability completeness.
- [ ] Final-prompt evaluator detects controlled omissions, weakening, contradictions,
      unsupported obligations, and process-policy conflicts.
- [ ] Before/after reports for development tasks and designated held-out task(s),
      with task/source/prompt/reference hashes and all runs retained.
- [ ] Zero coding-agent invocations during all prompt-only captures.
- [ ] All required repository checks pass; PRs are reviewable and unmerged by agent.
