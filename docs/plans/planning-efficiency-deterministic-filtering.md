# Planning Efficiency Through Deterministic Filtering

**Status:** Implementation handoff  
**Date:** 2026-11-01  
**Owner:** Powdrr-Lift planning service  
**Related change:** [PR #1283](https://github.com/powdrr-io/powdrr-lift/pull/1283) starts deterministic Structrr candidate selection for path-bound existing subjects.

## Objective

Reduce end-to-end planning time for large implementation plans to under two hours while preserving source fidelity, repository-state awareness, useful PR-sized work units, and explicit dependencies. The two-hour target is an outcome to measure against a recorded large-plan fixture; it is not a reason to weaken correctness checks or discard uncertain requirements.

The planner should derive candidate sets, identifiers, ownership, and mechanical classifications deterministically wherever the repository and plan already contain enough information. Jev or the configured LLM should answer small questions about those concrete candidates. Open-ended generation remains for work that requires semantic judgment, such as extracting an obligation from prose or describing a genuinely new structural change.

## Design rules

1. **Derive before asking.** Use source anchors, planned paths, baseline records, repository symbols, validation catalogs, and exact relationships to build candidate sets in code.
2. **Ask about one bounded decision.** A review question should identify one requirement, candidate subject, dependency pair, or small set of independent tasks. Do not ask Jev to discover generic categories such as “which entities changed?”
3. **Keep generation separate from confirmation.** A yes/no classifier can confirm that a candidate is relevant. A generative model supplies the desired fields only after a concrete candidate needs a change.
4. **Keep batches bounded.** Batch independent questions only through the existing question-batch contract. Keep each member's question ID, schema, digest, parser, and answer in the durable journal. Never combine unrelated decisions into one free-form response.
5. **Preserve an uncertain path.** Static analysis and deterministic rules may miss semantic relationships. Uncertain cases go to a targeted model question; they are never silently discarded.
6. **Make automatic decisions replayable.** A deterministic answer must be journaled with its question ID, input digest, algorithm/version, answer payload, and decision reason. Resume must rebuild state from this journal just as it does for model answers.
7. **Measure the whole run.** Count requests, batch members, pages, input/output tokens, retries, provider latency, deterministic outcomes, and time per planning phase. A reduction in model calls is not proof of a faster run until elapsed time and completion quality are measured.

## Current planning path

The source-ledger question machine currently moves through these phases:

1. Extract source obligations from each Markdown block, with pagination when a block has more than eight extracted records.
2. Classify each source block as normative, context, or duplicate; check each extracted requirement for source fidelity.
3. Search pinned repository evidence, then assess whether each requirement is already implemented.
4. Propose implementation work units for groups of up to four unresolved requirements.
5. Group work units into PR-sized items and ask for item title, objective, and scope.
6. Discover dependency candidates from planned outputs, paths, and text, then confirm candidate edges.
7. Derive planned file subjects, enumerate remaining Structrr subjects, and review structural changes.
8. Compile the accepted answers into the plan, dependency graph, and Structrr proposal.

The flow already batches fidelity, coverage, and some Structrr questions. Other independent question types are still returned individually. Work-unit proposals ask for several fields that the parser later derives or constrains to a single catalog choice. Source-block disposition repeats accounting that can often be derived from the requirement-to-anchor map. Coverage asks a model about requirements even when a completed repository search found no matches. Structrr enumeration still asks the model to discover subjects that static repository analysis may be able to name.

The planning service already has a bounded batch mechanism in `planning/questions.py`, a BasedPyright plus ripgrep evidence-search record in `planning/coverage.py`, deterministic work-unit/PR allocation in `planning/assembly.py`, and deterministic candidate pruning in `planning/dependencies.py`. Reuse those boundaries rather than introducing a second batching or search implementation.

## Work sequence

The tasks below are ordered by dependency and implementation risk. Each numbered milestone should be a separate reviewable PR unless implementation evidence shows two adjacent tasks are inseparable. PR #1283 is the first path-bound Structrr candidate slice; merge it before the later symbol-index expansion.

### Milestone 0: Record a baseline and expose planning cost

**Purpose:** Establish which phases dominate elapsed time and provide a before/after comparison for every optimization.

**Implementation:**

- Add a per-run summary keyed by planning phase and `Question.kind`.
- Record, for each question or batch: question IDs, batch-member count, input digest, input size, answer size, prompt/completion tokens when available, provider/model route, retries, validation failures, elapsed time, and whether the answer was deterministic or model-produced.
- Record pagination separately: source block or Structrr item ID, page ordinal, `has_more`, number of returned records, unique new records, and whether the page made progress.
- Record repository search facts already available in `RequirementEvidenceSearch`: BasedPyright query/match counts, ripgrep match counts, matching paths/lines, evidence count, and whether BasedPyright was available.
- Summarize counts and durations without persisting full prompt text or source excerpts in operational metrics. Preserve the existing durable planning journal as the authoritative answer history.
- Add a compact summary command/output for a completed or resumable planning run. The summary must report phase totals and the largest call/page contributors without loading or printing the entire answer journal.

**Acceptance criteria:**

- A synthetic run with extraction, coverage, work-unit, dependency, and Structrr questions produces totals by phase and question kind.
- A resumed run reports prior and resumed work separately and does not double-count a journaled answer as a new model call.
- Missing token or latency metadata is represented as unknown, not zero.
- Metrics do not include credentials, raw prompts, raw source, or repository evidence excerpts.
- A test proves the summary can be computed from a large journal without serializing it as one giant response.

**Dependency:** None. Complete before claiming an optimization met the two-hour target.

### Milestone 1: Batch independent questions across all phases

**Purpose:** Reduce provider round trips while retaining individually validated questions and durable answers.

**Implementation:**

- Batch pending first-page and continuation-page `extract_requirements` questions from different source blocks. A continuation page may be batched only after its own prior page is accepted and its cursor is known.
- Batch `classify_source_block` questions. Keep them in their own batch type, separate from fidelity questions.
- Batch `propose_work_units` questions from independent requirement groups. Preserve the current requirement coverage, page cursor, and supplemental single-requirement repair behavior.
- Batch `describe_item` questions from independent allocated groups.
- Batch only dependency confirmations emitted by `discover_dependency_candidates`. Do not replace candidate pruning with an all-output-by-all-consumer matrix.
- Batch `describe_human_setup` questions when several independent human setup requirements exist.
- Keep existing batching for `check_requirement_fidelity`, `assess_requirement_coverage`, and Structrr enumeration/change questions.
- Use `batch_question_requests` with its bounded maximum and input-size fallback. Do not raise the maximum to compensate for poor candidate pruning.
- Make `ready_questions` return batches grouped by compatible question kind. `QuestionRequest.parse_answer` must continue to expand a batch answer into member-keyed answers, and checkpoint persistence must store those member answers rather than only the outer batch response.

**Acceptance criteria:**

- With 16 independent questions of one kind and inputs below the batch limit, the planner emits four bounded batch requests. Every member ID is present exactly once.
- If one member would make a batch too large, the helper splits the batch without dropping or reordering any member.
- Batch and unbatched parsing yield the same typed member answers and the same downstream plan.
- A partially accepted batch can resume using accepted member answers without re-asking those members.
- Invalid or missing member answers identify the exact member question ID in the journal and retry only that member.
- Tests cover extraction pagination across multiple source blocks, work-unit groups, item groups, and discovered dependency candidates with nontrivial answers.

**Dependency:** Milestone 0 is preferred for measurement, but this can ship independently if instrumentation would delay batching.

### Milestone 2: Derive safe source-block dispositions

**Purpose:** Remove a per-block semantic classification call when accepted requirement extraction already establishes the disposition.

**Implementation:**

- Build a deterministic map from each source anchor to the IDs of requirements that cite it directly and separately to requirements that cite it only as related context.
- Automatically classify a block as `normative` when it has direct requirement citations. Attach exactly those known IDs.
- Use normalized exact-content fingerprints only to identify exact duplicate blocks. Do not use fuzzy similarity as a duplicate decision.
- Automatically classify a block as `duplicate` only when the exact duplicate fingerprint maps unambiguously to already accepted requirement IDs and there is no distinct direct obligation for that block.
- Classify a block as `context` automatically only when extraction found no direct obligation, no related requirement reference requires explanation, and it is not an ambiguous duplicate candidate.
- Send ambiguous cases to the existing narrow block-disposition question. Keep the model answer schema unchanged.
- Journal automatic dispositions with a stable rule ID, input digest, and reason so a resume does not rerun classification.

**Acceptance criteria:**

- Tests cover normative blocks with one and multiple requirements, empty context blocks, exact duplicate blocks, related-only references, and ambiguous duplicates.
- Ambiguous or malformed states continue to request model review; they are not coerced to context.
- The compiled ledger still has exactly one disposition per source block, and every disposition references only accepted requirement IDs.
- A golden fixture produces the same disposition mapping before and after the deterministic path for unambiguous cases.

**Dependency:** Milestone 1 is not required. The source extraction and accepted requirement map must already be complete.

### Milestone 3: Short-circuit coverage for completed zero-hit searches

**Purpose:** Avoid asking the model to confirm a result the current coverage contract already accepts as `missing`.

**Implementation:**

- Add a deterministic coverage constructor for the specific case where a repository search completed for the pinned base commit, produced no evidence, and reports zero matching lines and zero matching paths.
- Preserve model assessment whenever evidence exists, the search record is absent, the search did not complete, the base commit differs, or any match count is nonzero. In particular, a positive raw match count with no pinned evidence is not a zero-hit result.
- Reuse `bind_coverage_answer` and `create_requirement_coverage` to enforce the existing answer contract and provenance rules.
- Persist the deterministic `missing` answer under the same stable coverage question identity and input digest, with an explicit deterministic origin and search record. Resume should neither regenerate nor reinterpret it.
- Keep evidence-search scope and completeness visible in the journal. The current search is BasedPyright symbols plus ripgrep over tracked source, test, and configuration files; do not claim whole-repository semantic completeness.

**Acceptance criteria:**

- A completed zero-hit search produces `missing` with no LLM request and a stable explanation tied to the search record.
- A missing search record, failed search, nonzero line/path count, evidence result, or wrong base commit still requires assessment or raises the existing validation error.
- The deterministic answer survives journal replay and produces the same unresolved requirement and work-unit set as the equivalent valid model answer.
- Tests include nontrivial evidence records and incomplete-search cases, not only empty dictionaries.

**Dependency:** Milestone 0 should measure the fraction of requirements with a completed zero-hit search. The implementation can proceed only if search completion is explicit in the adapter contract.

### Milestone 4: Remove derived fields from work-unit model output

**Purpose:** Make the large work-unit proposal response smaller and reduce avoidable schema failures.

**Keep model-authored fields:**

- Which supplied unresolved requirements belong to the proposed work unit.
- A concise deliverable and implementation step.
- Actual planned write paths and their semantic roles.
- Source/evidence references that justify the planned writes.
- Observable acceptance criteria.
- Required outputs that a later work unit may consume, when there is a real contract to pass forward.

**Derive or bind in code:**

- Component: bind to the supplied component when the current catalog contains only `repository`.
- Per-file runtime and overall implementation runtime: derive from the planned paths and pinned repository facts using the existing runtime classifier. If a path remains ambiguous, ask a separate targeted runtime-resolution question or reject the proposal with the existing actionable error.
- Validation IDs: bind the single configured verification command deterministically while the catalog contains only that command. If the catalog later has multiple validations, add a separate applicability decision rather than asking the work-unit author to repeat catalog IDs in every proposal.
- All system IDs, ordinals, and references: continue assigning or validating them in code.

**Implementation details:**

- Version the work-unit question contract and update its schema, parser, examples, prompt, and proposal data model together.
- The parser must continue rejecting unknown requirement IDs, source refs, evidence IDs, paths, unsupported write roles, mixed runtimes, and uncovered unresolved requirements.
- Do not loosen the exact coverage invariant to make the shorter schema easier to answer.
- Retain a bounded maximum of four supplied requirements per question and the current page behavior. Batching from Milestone 1 handles independent groups.

**Acceptance criteria:**

- The model response no longer contains component, validation IDs, per-write runtime, or aggregate runtime when those are derivable from current supplied catalogs.
- Compiled WorkUnit output remains byte-for-byte equivalent for the same accepted semantic fields and repository facts, aside from explicit schema-version metadata.
- Tests cover Python, Swift, neutral/documentation paths, mixed-runtime rejection, ambiguous paths, exact requirement coverage, and setup/validation ownership.
- A recorded set of nontrivial mock responses proves the model can still propose cohesive work units and concrete writes under the reduced schema.

**Dependency:** Milestone 1 can be merged first so the revised groups use the batch path. This milestone must not change PR sizing policy or allow mixed-runtime units.

### Milestone 5: Expand deterministic Structrr candidate generation

**Purpose:** Reduce model-led subject enumeration while preserving an uncertainty route for semantic changes static tools cannot identify.

**Baseline:** PR #1283 provides the first slice by binding existing baseline entities associated with planned paths and relationships touching those entities, then reviewing each candidate with its owning plan unit and source excerpt.

**Implementation:**

- Reuse the repository's BasedPyright symbol/evidence adapter and pinned snapshot. Do not add a separate source parser or invoke a repository-wide scan once per work item.
- For each work item, start from its deterministic planned write paths. Query only symbols and references for those paths and directly referenced symbols.
- Produce candidate records for supported taxonomy entity types, including source files/modules/functions/classes/methods where those types are present in the Structrr taxonomy and baseline format.
- Build candidate relationships only from explicit evidence such as import/reference/call edges or existing Structrr edges touching a candidate. Do not infer a relationship from shared words alone.
- Bind each candidate to owning unit IDs, requirement IDs, source refs, and exact repository evidence in code.
- Deduplicate identical candidate IDs deterministically across paths and units before any model request.
- Ask Jev a narrow per-candidate relevance/impact question when its configured contract is suitable. Ask the configured generative LLM for desired section-specific content only for candidates confirmed to need a change. If the LLM can answer the combined candidate-state question with less total latency, retain a single per-candidate request and measure it.
- Keep a new-identity discovery request for changes not represented by symbols or the baseline index. It must be bounded, paginated, scoped to the owning units and evidence, and subject to cross-page duplicate and no-progress rejection.
- Preserve the path-bound implementation from PR #1283. Do not reintroduce deterministic file subjects into model enumeration.

**Acceptance criteria:**

- A fixture with planned writes to existing Python modules yields path-bound module, function, and relationship candidates from static evidence without an enumeration request for those known candidates.
- A fixture with a genuinely new symbol or semantic entity takes the bounded new-identity route and compiles correctly.
- A fixture with unrelated symbols and relationships does not expose them to candidate review.
- Candidate recall is measured against a hand-labeled nontrivial fixture. The deterministic index may overproduce candidates for targeted confirmation; it must not silently omit known changed symbols.
- Repeated candidates, empty non-final pages, unchanged cursors, and pages without a new subject are rejected or routed into repair with explicit errors.
- Existing Structrr compilation, item ownership, dependency ordering, and unordered-conflict rejection remain enforced.

**Dependency:** PR #1283 should be merged first. BasedPyright queries must use the pinned base commit and current repository adapter.

### Milestone 6: Improve dependency candidates and batch their confirmation

**Purpose:** Make dependency detection more evidence-based and control confirmation count as plans grow.

**Current behavior:** Same-file write ordering is deterministic. Semantic candidates are pruned using explicit path mentions and rare-token overlap, then each remaining pair is asked separately.

**Implementation:**

- Keep same-file write-order edges deterministic.
- Add deterministic candidates from explicit required-output references, shared requirement/source-group relations, planned-path references, and BasedPyright import/call/reference evidence.
- Retain token-overlap discovery as a supplemental candidate signal until labeled tests show the structural evidence has adequate recall. It must not be the only way to find semantic dependencies.
- Remove exact duplicate candidate pairs before building questions.
- Batch only the candidate pairs produced by the discovery stage, preserving one independently parsed answer per pair. Never ask the model to construct the entire dependency graph from all work units.
- Let Jev answer the targeted required/independent decision when its response contract supports that boolean classification. Use the configured generative LLM only when the output contract also needs an explanation or dependency artifact that cannot be derived from the supplied producer output.

**Acceptance criteria:**

- Tests include a true dependency with no shared rare tokens, a lexical false positive, a same-file ordering case, an unrelated pair, and a multi-step transitive chain.
- Every known dependency in the hand-labeled fixture is present in the candidate set. Candidate false positives are acceptable only when the focused reviewer rejects them.
- Output-to-consumer candidate counts and confirmation counts are recorded by the baseline metrics.
- The accepted dependency graph remains acyclic, and PR readiness still reflects prerequisite completion.

**Dependency:** Milestone 1 for batching. Milestone 5 is preferred for symbol evidence, but explicit outputs and path candidates can ship earlier.

### Milestone 7: Keep item descriptions small and preserve title quality

**Purpose:** Avoid spending a large response on fields that merely restate frozen work units while retaining clear PR titles.

**Implementation:**

- Derive complete scope membership from `AllocationGroup.unit_ids`; do not ask the model to assign every unit to scope groups if the compiler can provide one deterministic scope entry or a grouping based on existing components/runtime.
- Keep the model responsible for an outcome-oriented PR title and concise objective if quality evaluation shows these improve reviewer understanding.
- Batch independent item-description questions from Milestone 1.
- Add deterministic post-validation for title length, duplicates, generic titles, and correspondence to the group's concrete deliverables. Ask a bounded repair question only for a failing title.

**Acceptance criteria:**

- Every work unit appears exactly once in compiled scope by construction.
- Titles remain specific and distinguish neighboring PRs in a nontrivial multi-item fixture.
- A model answer cannot add, remove, reorder, or change work units.
- No extra title-repair call is made for a title that passes deterministic checks.

**Dependency:** Milestone 1. Consider after metrics show item-description calls or malformed scopes are material contributors.

## Questions that should remain semantic

The following tasks still need semantic judgment and should not be converted to brittle keyword-only rules:

- Turning prose into independently checkable obligations and preserving cross-clause meaning.
- Deciding how unresolved obligations combine into cohesive implementation steps and actual paths.
- Determining whether a concrete code candidate changes the intended behavior or structural contract.
- Confirming ambiguous producer/consumer dependencies when static evidence cannot prove the data flow.
- Writing desired content for genuinely new Structrr entities or relationships.
- Producing clear, outcome-oriented PR titles.

These tasks should receive deterministic candidates, narrow evidence, bounded schemas, and strict parsers. For source extraction, deterministic clause segmentation and exact duplicate detection should reduce the input set; uncertain clauses should still receive review rather than being silently discarded.

## Jev and LLM routing

- Jev is appropriate for specific classification decisions over supplied evidence, such as “does this candidate relate to this planned write?” or “does this output appear to be required by this named consumer?”
- Do not ask Jev broad discovery questions about which entity types, relationships, or repository areas changed.
- The generative LLM should create desired content only after deterministic analysis or Jev identifies a concrete candidate that needs a change.
- Route generative work through the configured DeepInfra provider and Jev decisions through the configured Jev provider. Do not add a silent provider fallback or route planning to OpenAI.
- Record provider, model, and decision stage in the per-run metrics so routing latency and quality can be compared.

## Scale and performance evaluation

Run three profiles with fixed source documents, pinned repository commits, and deterministic mock answers:

1. **Typical:** 40 source blocks, 40 requirements, 16 work units, and 5 PR items.
2. **Large:** 500 source blocks, 500 requirements, 150 work units, and 50 PR items.
3. **Regression stress:** 40 source blocks and 16 work units with 2,051 repeated/path-bound Structrr subjects.

For each profile, report:

- End-to-end wall time with deterministic mock providers and with the configured live providers where available.
- Number of requests and batch members by question kind, number of pages, input/output tokens, retries, and validation repairs.
- Time spent in repository evidence search, BasedPyright, ripgrep, Structrr projection, and model calls.
- Number of deterministic answers, model reviews, model-generated changes, and unresolved/uncertain cases.
- Semantic comparison of final requirements, work units, dependencies, and Structrr operations against the accepted baseline fixture.

Use the two-hour target only for a reference large-plan run with recorded provider settings and hardware. CI should enforce deterministic interaction-count and progress bounds, not a wall-clock threshold that varies with external provider or runner load.

### Initial deterministic profile results (2026-11-01)

Run with `python scripts/benchmark_planning_scale.py --profile all` using the
shared repository environment. All provider answers are deterministic fixtures;
the run makes no network calls. On the current development machine:

| Profile | Local elapsed | Requirements | Work units | Plan items | Question answers | Provider calls |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Typical | 0.42s | 40 | 16 | 5 | 112 | 50 |
| Large | 10.27s | 500 | 150 | 50 | 1,437 | 625 |
| Structrr stress | 16.17s | 40 | 16 | 5 | 2,263 | 781 |

Every run succeeded, used no more than four questions per provider call, and
had zero validation retries. The stress profile includes 2,156 individual
structural-review decisions and completes without oversized prompts. These
figures measure deterministic local overhead only; they do not establish the
two-hour target with live provider latency. The live-provider run still needs to
record provider/model settings, concurrency, token usage, and hardware.

## Cross-cutting test requirements

- Use mocked nontrivial provider responses. Do not use live-model calls as the only proof that a contract works.
- Exercise the same `QuestionRequest.parse_answer` path for individual and batched responses.
- Verify durable journal entries contain the individual member IDs and exact input digests; test resume from a partial batch and after a deterministic answer.
- Test malformed output, missing batch members, extra IDs, wrong source refs, wrong base commits, duplicate pages, no-progress pagination, and stale answers after input changes.
- Test both high-recall deterministic candidate generation and targeted rejection of irrelevant candidates.
- Include cases where evidence exists, no evidence exists after a completed search, and a search is incomplete or unavailable.
- Keep full repository verification in the PR workflow. Keep scale benchmarking deterministic and separate from ordinary CI when it would make CI slow or provider-dependent.

## Risks and controls

| Risk | Control |
| --- | --- |
| Deterministic filters miss a real requirement or dependency | Keep uncertain cases in a targeted review path; measure candidate recall on labeled fixtures before making a filter authoritative. |
| Batches become oversized or produce a malformed combined answer | Keep bounded batch size and input-size splitting; validate every member against its original schema. |
| Automatic decisions disappear on resume | Journal deterministic answers with stable IDs, digests, rule versions, and provenance. |
| Zero-hit search is mistaken for complete repository evidence | Short-circuit only on explicit completed search metadata for the pinned base commit with zero line/path matches and no evidence records. |
| Work-unit schema simplification weakens plan quality | Remove only code-derived fields; preserve exact requirement coverage, path/source ownership, runtime validation, and nontrivial response tests. |
| Symbol indexing creates too many candidates | Restrict analysis to planned paths and direct references; deduplicate before review; measure candidate-to-confirmed-change ratio. |
| Dependency pruning loses a real edge | Keep multiple candidate signals and require full recall on hand-labeled dependency fixtures before relying on any single signal. |
| Faster answers reduce the number of reviewable PR-sized changes | Preserve current sizing objective and evaluate item size and acceptance criteria alongside latency. |

## Definition of done

The optimization effort is complete when:

- A recorded large-plan run completes in under two hours using documented provider settings and repository hardware.
- Its phase-level metrics identify no unbounded or no-progress question loop.
- Independent requests use bounded batching, while journal replay continues to preserve per-question identity and resumability.
- No-hit coverage and unambiguous source accounting use deterministic answers with explicit provenance.
- Work-unit responses omit fields already derived from supplied catalogs and paths.
- Structrr and dependency candidate generation is evidence-backed, deduplicated, and covered by nontrivial recall fixtures.
- Final requirements, PR-sized work items, dependencies, and structural operations pass the same compiler and validation rules as before.
- Full tests, formatting, lint, type checks, changelog validation, and PR CI pass.
