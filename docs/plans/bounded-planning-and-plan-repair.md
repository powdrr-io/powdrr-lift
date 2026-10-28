# Bounded planning and plan repair implementation plan

Status: proposed implementation contract

Date: 2026-10-25

## 1. Outcome

Replace the current whole-plan LLM rewrite loop with a sequence of small,
explicit questions. Python owns identities, references, assembly, constraints,
artifact generation, checkpointing, and publication. Models provide bounded
semantic answers about one source block, requirement, work unit, dependency,
validation, or structural subject at a time.

Add a separate repair operation. A repair starts with an existing plan revision
and a concrete discovery, proposes explicit changes, validates the resulting
plan, and produces a reviewable amendment. Initial planning, draft repair,
explicit replanning, and worker-discovered amendments use the same contracts,
question runner, plan compiler, and validation rules.

This document is normative for this change. It describes planner behavior and
plan artifact generation. Existing coding-agent execution and acceptance-test
semantics remain governed by their current contracts. Do not interpret a planner
repair as permission to introduce a second coding-agent implementation loop.

Completion means a real Gaterr planning run produces a complete, validated plan
review PR with concrete human setup tasks where required, plus a subsequent
repair PR that preserves unaffected work and reconciles its GitHub tracking.

## 2. Current code and observed failures

Read these entry points before implementing:

| File | Current responsibility | Required change |
| --- | --- | --- |
| `src/powdrr_lift/cli.py::_run_plan` | Bootstrap, create a provider client, generate a plan/proposal, write artifacts, open a PR | Call the shared planning service; add resume and repair entry points |
| `src/powdrr_lift/workrr/github_issue_plan_generation.py::generate_execution_plan_yaml` | Full draft, full adversarial rewrite, repeated full-plan correction, coverage audit, full reconciliation | Replace production use with bounded questions and deterministic assembly |
| Same module, `generate_multi_pr_structrr_proposal` | Ask for all Structrr operations together | Generate one item's structural subjects and then one subject operation per question |
| Same module, `write_execution_plan_artifacts` | Emit execution YAML, proposal JSON, workflow descriptors | Use as the starting point for one compiler shared by generation and repair |
| `src/powdrr_lift/workrr/plan_sizing.py` | CP-SAT grouping and prose-based runtime inference | Consume explicit work-unit constraints; remove runtime inference from prose |
| `src/powdrr_lift/workrr/github_issue_queue.py` | Plan parsing, issue pointers, checkpoints, amendment merge reconciliation | Read planning metadata and reconcile every affected issue after a repair merges |
| `src/powdrr_lift/workrr/task_agent.py::_run_plan_amendment_agent` | Ask a coding agent to edit plan, Structrr, and one workflow file | Submit a typed discovery to the shared repair service |
| Same module, `_validate_issue_worker_plan_amendment` | Validate an amendment limited to the original item | Validate an explicit repair impact set and all compiled artifacts |
| `docs/procedrr/processes/amend-plan-and-open-pr.yaml` | Run amendment coding agent and publish PR | Call shared repair operations and existing publication operations |
| `docs/procedrr/processes/execute-proposed-pr-workflow.yaml` | Classify execution failure and choose amendment path | Preserve failure classification; send evidence to shared repair |

The October 25 Gaterr runs demonstrated these cases. Preserve them as regression
scenarios, not merely examples in prompts:

1. A Swift sandbox task mentioned Python child processes in a test. A prose
   regex inferred Python implementation work and rejected the task as mixed
   runtime.
2. A refinement returned one work-item object instead of an `items` wrapper.
3. An audit returned one assessment object instead of an `assessments` wrapper.
4. An audit omitted `remaining_item` for work classified wholly missing.
5. Reconciliation invented `manual_prerequisite` entries titled “No manual
   prerequisite” and used the next PR ID as `unblocks`.
6. Structrr generation returned one operation instead of an `operations` list.
7. Drafts covered only an early portion of the large source document. A valid
   item schema by itself did not establish complete source coverage.

Recorded exchanges currently exist under
`/private/tmp/powdrr-gaterr-replan-20261025-llm/` and the sibling `-r2-llm/`
through `-r5-llm/` directories. Six exact response excerpts are preserved in
[`bounded-planning-failure-examples.json`](bounded-planning-failure-examples.json)
with their source exchange digests and selection paths. Copy those committed
examples into test fixtures in PR 01, preserving provenance. Tests must never
depend on the temporary paths. The examples intentionally retain invalid output;
they are inputs for rejection/correction tests, not valid target schemas.

Reuse these existing primitives instead of writing parallel implementations:

- `core/instruction_ledger.py`: `InstructionSource`, `InstructionClause`, source
  offsets/fingerprints, `compile_instruction_ledger`, and atomic split validation.
- `core/semantic_contract.py`: source-bound extraction and exact-key validation
  patterns; preserve conditional, ordered, alternative, and grouped obligations.
- `workrr/semantic_contract_compiler.py`: existing bounded source-context and
  extraction preparation/binding helpers where their contracts fit. Reuse pure
  helpers through adapters; do not introduce a mandatory Jev/provider dependency.
- `structrr/source_manifest.py`: pinned source observations, taxonomy fingerprint,
  exclusion policy, and complete-versus-incomplete source coverage.
- Existing task checkpoint/comment and provider backoff implementations. Planning
  records need their own reducer, but transport and authentication stay shared.

## 3. Decisions that implementations must preserve

- Keep the `plan` command and its final review PR behavior. Publish work-item
  issues only after plan review and merge through the existing publish flow.
- Initial generation and repair use one production implementation. Remove the
  superseded whole-plan generation/rewrite path when the new CLI is connected.
- Use the configured provider; Gaterr acceptance runs use `deepinfra-cheap`.
  Reuse existing transport retry/backoff. Do not switch providers to hide a bad
  answer. Jev approval is not a dependency of this work.
- The question context and output sizes below are per-answer bounds. They are
  not global model-call or tool budgets for a worker.
- Keep CP-SAT and its preference for useful PR size, approximately three work
  units with a maximum of four. Do not optimize for the fewest PRs.
- Source requirements, reviewed plan content, and operational state have
  distinct owners: source Markdown and plan artifacts are reviewed in Git;
  workflow and planning progress are recorded in GitHub issue comments.
- Generate one compact workflow descriptor per proposed PR. Runtime task state
  stays in issue comments. Do not introduce files per workflow task or question.
- A repair PR changes plan artifacts. Product implementation continues through
  the existing PR worker after the repair merges.
- Preserve stable item identities when meaning is preserved. Do not create a
  new plan ID or renumber every PR merely because one item changes.

## 4. Target modules and public service boundary

Create `src/powdrr_lift/workrr/planning/` with `__init__.py` and the following
modules. Keep
publication helpers importable from their existing location until their callers
are updated in the same PR; do not maintain two orchestration implementations.

| Module | Owns |
| --- | --- |
| `contracts.py` | Typed records, JSON schemas, canonical serialization, contract errors |
| `source.py` | Pinned source documents, deterministic Markdown blocks, source anchors |
| `questions.py` | Question catalog, prompt templates, context builder, answer validation |
| `evidence.py` | Repository snapshots, code lookup results, evidence IDs and validation |
| `assembly.py` | Requirement coverage, work-unit allocation, dependency graph, ready set |
| `compiler.py` | Reviewed plan, Structrr proposal, workflows, repair manifest |
| `repair.py` | Discoveries, impact sets, typed patch application and change validation |
| `checkpoints.py` | Immutable accepted answers, local checkpoints, legacy GitHub migration, local diagnostics |
| `service.py` | Initial planning, resume, repair, validation, publication coordination |

Expose these Python operations with keyword-only arguments:

```python
start_plan(source, repository_snapshot, options, checkpoint_store) -> PlanningRun
resume_plan(session_id, options, checkpoint_store) -> PlanningRun
propose_plan_repair(plan_revision, discovery, options, checkpoint_store) -> RepairRun
compile_plan(accepted_state, repository_snapshot) -> CompiledPlan
validate_compiled_plan(compiled, repository_snapshot) -> ValidationReport
```

`options` contains provider/model configuration and the verification command.
`CompiledPlan` contains the execution plan, Structrr proposal, workflow documents,
and an exact destination-path manifest. Model answers never contain arbitrary
file edits or publication commands.

Add bundled Procedrr processes `plan-execution.yaml` and
`repair-execution-plan.yaml`. Each drives the same service operations used by
the CLI. Python computes the next question and dependency readiness; Procedrr
drives question execution, checkpoint, validation, and publication transitions.
Do not duplicate question ordering in YAML and a separate CLI orchestration
loop. Register bounded planning operations through the existing Procedrr
operation-dispatch mechanism and test their authorization there.

## 5. Common records, identity, and validation

Use frozen dataclasses plus explicit parsers, matching existing repository
patterns. Do not add a validation library solely for this change. Produce JSON
schemas from shared contract definitions or test exact parity between schemas
and parsers. Every object rejects unknown fields. Use explicit tagged variants
so unrelated fields never need empty placeholders.

Code assigns all identities. Models can reference supplied IDs or return new
records without IDs; code assigns IDs after validation. Allocate monotonic IDs
within a planning session (`anchor-0001`, `req-0001`, `unit-0001`, `item-0001`,
`val-0001`, `setup-0001`, `evidence-0001`, `subject-0001`). Preserve the allocation counters
in checkpoints. Sorting or retrying cannot renumber accepted records. Record
each final workflow ID using the existing slug/hash logic.

All records below require their listed fields unless a tagged variant says
otherwise. Lists contain unique entries unless ordered steps are specified.
References must resolve before a stage can complete.

| Record | Fields and meaning |
| --- | --- |
| `SourceAnchor` | `id`, `source_blob_sha`, `block_id`, `clause_id` (nullable), `start_line`, `end_line`, `start_offset`, `end_offset`, `quote_sha256`; immutable reference to source text; lines are inclusive and character offsets use a half-open range into the decoded pinned blob |
| `Requirement` | `id`, `statement`, `kind` (`behavior`, `constraint`, `acceptance`, `human_setup`), `source_refs`, `source_group_ids`, `related_requirement_ids`; one independently checkable obligation; code binds ledger group IDs from its source clauses |
| `Evidence` | `id`, `base_commit`, `path`, `start_line`, `end_line`, `content_sha256`, `kind` (`implementation`, `test`, `configuration`, `execution_receipt`), `symbol` (nullable) |
| `WorkUnit` | `id`, `requirement_ids`, `deliverable`, `component`, `implementation_runtime`, `planned_writes`, `implementation_step`, `acceptance_criteria`, `validation_ids`, `prerequisite_unit_ids` |
| `PlannedWrite` | `path`, `role` (`implementation`, `test`, `configuration`, `documentation`), `runtime` (nullable), `source_refs`, `evidence_ids` |
| `Validation` | `id`, `description`, `kind` (`local_command`, `ci_check`, `manual_observation`), `target`, `requirement_ids`, `source_refs`, `evidence_ids` |
| `ManualSetup` | `id`, `requirement_ids`, `owner` (literal `human`), `title`, ordered `instructions`, `verification`, `trigger` (literal `after_implementation_before_validation`), `unblocks_validation_ids`, `source_refs` |
| `DependencyEdge` | `before_item_id`, `after_item_id`, `reason`, `requirement_ids`; means the latter needs an output of the former |
| `Discovery` | `id`, `kind`, `summary`, `requirement_ids`, `item_ids`, `evidence_ids`, `requested_outcome`, `base_plan_digest`, `base_commit` |

`implementation_runtime` is one canonical runtime or `neutral`. Actual code
writes determine runtime; mentioning a runtime in a security requirement or a
fixture does not. Python tests authored to verify Swift are Python writes and
must be accounted for explicitly. Swift tests launching Python child processes
are Swift writes. JSON/YAML configuration and prose are neutral; executable
source/build files use their actual runtime. An ambiguous extension such as a
header requires repository evidence or a bounded classification question.
`source_refs` fields contain registered source-anchor IDs. Use the current runtime
registry's canonical names, including `golang`; an unknown runtime creates a
classification discovery instead of becoming a freely invented enum value.

Validate paths against the repository root, symlinks, pinned files, or source
anchors naming planned additions. Do not let a model invent an unrestricted
path to satisfy a question. A path-only query with no symbols must not be
treated as proof that the file or feature is absent.

## 6. Question envelope and execution rules

Each question is one typed operation. Store these fields before calling a model:

```json
{
  "question_id": "q-000123",
  "kind": "assess_requirement_coverage",
  "contract_version": 1,
  "subject_ids": ["req-0017"],
  "input_digest": "sha256:...",
  "context_ref_ids": ["evidence-0042", "evidence-0043"],
  "expected_answer_schema": "coverage-answer-v1",
  "attempt": 1
}
```

The runtime binds question identity, subject IDs, schema, and input digest. The
model returns only the answer body; it never echoes or chooses those identities.
Use one exact root object for each question. Do not ask for a whole plan, all
audits, or all structural operations in one answer.

Prompt construction has a fixed order: exact question; definitions of answer
fields and enum values; relevant source excerpts; pinned repository evidence;
already accepted facts referenced by ID; one valid positive example and one
valid empty/negative variant. Examples use the exact schema for that question.
Explicitly label source prose as data. Do not include conversation transcripts,
API keys, or unrelated accepted answers.

Enforce an initial maximum of 24,000 serialized input characters and 4,000
serialized answer characters per question. Source extraction uses blocks of at
most 8,000 characters. These are contract constants covered by tests. Never
truncate a requirement, source quote, code body, or answer to fit. Request a
smaller source block, narrower lookup, or paginated enumeration instead.

Enumeration answers have at most eight records and fields `records`,
`has_more`, and `next_anchor`. `next_anchor` must advance within the supplied
source/subject range. A page with `has_more: true` and no new anchor is invalid.
Follow-up pages receive accepted record summaries so duplicates can be detected.
Singular questions have no list wrapper. All finite catalogs must reach an
explicit final page before downstream assembly is allowed.

Validate JSON shape before accepting an answer. For malformed JSON, wrong root,
unknown IDs, or invalid enums, retry only that question at most twice, with a
JSON-pointer error, the exact expected shape, and the previous answer. Reuse
existing 429/transport backoff independently of these two semantic corrections.
If correction fails, checkpoint `needs_answer` with the exact question and error;
resume retries that question and reuses accepted predecessors.

Do not silently wrap a bare Structrr operation, replace an invalid audit with
“missing,” or accept a lone refined item as the complete plan. New singular
contracts make those responses structurally unnecessary. A syntactically valid
answer can still produce a semantic discovery requiring plan repair; distinguish
that from a response-format correction.

## 7. Initial planning stages and exact questions

Run stages in the order below. Within a stage, visit stable IDs in ascending
order. Sequential scheduling is sufficient for v1; concurrency is an optional
later optimization. Each completed stage has a deterministic validation gate.

### 7.1 Pin source and repository facts

Create a fresh planning branch/worktree from the current remote default branch.
Record its full commit SHA. Read source Markdown and all evidence from that
revision; uncommitted files in another checkout are not current implementation.
Read source blobs through Git and record content digests. Compute the Structrr
baseline once at this same revision.

Use the existing instruction ledger to capture source text and clause spans.
Add a planning adapter that groups those spans by Markdown headings and
list/table boundaries; do not create a second sentence splitter. Allocate block
IDs and preserve parent-heading context. Subdivide an oversized block on
paragraph/list-item boundaries; subdivide a single oversized paragraph on
sentence boundaries while preserving exact line/offset anchors. Preserve fenced
blocks as anchored ranges; oversized fences require bounded excerpts with an
explicit continuation range. Detect instructions/tables spanning chunks through
parent and adjacent-block references rather than silently dropping them.

Inventory tracked implementation, test, configuration, and workflow files. Store
the existing verification command as a fact. Check recognizable CI invocations
deterministically. If a wrapper makes CI command equivalence uncertain, ask one
`ci_command_equivalence` question with just the workflow, wrapper, and command;
answer is `equivalent`, `different`, or `unknown`, with evidence IDs and reason.
`unknown` creates a discovery, not an instruction to overwrite existing CI.

### 7.2 Extract and account for source requirements

For each source block ask:

> Which independently checkable obligations does this block impose?

`extract_requirements` returns a bounded enumeration of `statement`, `kind`,
`source_refs`, and `related_source_refs`. Code assigns requirement IDs. Preserve
cross-reference constraints such as “all prior PRs” as obligations referencing
the relevant source ranges; resolve them during dependency assembly.

Then ask one `classify_source_block` question per block not already fully covered
by extraction. Answer is tagged `normative` with requirement IDs, `context` with
reason, or `duplicate` with existing requirement IDs and evidence. Every source
block must have a disposition. Do not use a global “looks complete” response to
replace this accounting.

For each extracted requirement ask `check_requirement_fidelity`:

> Does this statement preserve the cited obligation without adding or removing
> behavior?

Answer is `accept` or `revise` with one corrected statement and a reason. The
question cannot delete an obligation. Merge duplicates only after a pairwise
equivalence question; record both source anchors on the retained requirement.
Partial duplicates remain separate. Code creates a source-coverage manifest and
rejects unresolved normative blocks.

### 7.3 Assess one requirement against existing code

Retrieve implementation/test bodies using the existing code analysis tools and
bounded repository reads. Fix the current lookup problem: a request containing
a file path must first resolve/read that pinned file, then use symbol lookup for
symbols inside it. File paths are not workspace symbol names.

Ask one `assess_requirement_coverage` question for each requirement. Common
answer fields are `evidence_ids` and `reason`. Variants are:

```json
{"status":"missing","evidence_ids":[],"reason":"No matching implementation was found."}
```

```json
{"status":"complete","evidence_ids":["evidence-0042","evidence-0043"],"reason":"Implementation and relevant tests cover the obligation."}
```

```json
{"status":"partial","evidence_ids":["evidence-0042"],"reason":"Parsing exists; the size guard does not.","remaining_statement":"Reject inputs exceeding the configured size limit."}
```

Only `partial` has `remaining_statement`; no variant has `remaining_item`.
`complete` needs validated implementation/configuration evidence and relevant
tests, or an observed execution receipt that actually covers the obligation.
Tests existing in the tree do not establish that they passed. `partial` needs
implementation evidence and a concrete remainder. Unknown coverage stays
`missing` with an explanation; invalid evidence is a question failure, not proof
of absence. Human setup requirements use `required`, `satisfied`, or `unknown`
with administrator/setup evidence; product code cannot prove a credential is
installed. Treat unknown setup as required until an explicit verification.

Code retains completed requirements and their evidence in the ledger, removes
their work allocation, and creates residual obligations for partial coverage.
Residual IDs derive from the original requirement ID and an allocated revision;
never discard the original provenance.
Carry the instruction ledger's conditional, alternative, ordered, and
`all_together` group records into the planning manifest. Requirement allocation
must respect those records: an alternative is not several mandatory features,
and a condition cannot disappear from a residual obligation. An unresolved choice
or condition is a discovery. Do not silently pick a new product decision to make
coverage counts balance. Code binds `source_group_ids`; extraction models never
invent them.

### 7.4 Produce concrete work units and PR candidates

Before proposing units, ask `describe_requirement_validations` for one unresolved
requirement: which concrete observations establish its criteria? Return at most
eight validation descriptions per page using the exact schema in section 13.
Code registers their IDs; subsequent
questions reference only those registered validations. An acceptance criterion
with no matching observation remains unresolved and blocks compilation.

For one unresolved behavior/acceptance requirement, plus at most seven directly
related requirements, ask `propose_work_units`:

> What concrete code or test changes implement these remaining obligations?

Return bounded `WorkUnitProposal` records defined below. Each unit
contains one concrete implementation step and at most two acceptance criteria.
It must name actual planned writes and the runtime of authored code. If one
requirement requires several units, all must be enumerated before allocation.
Code assigns units and verifies every unresolved requirement has allocation.
Shared work may cover several requirements without duplicating the change.
`component` must reference a supplied component catalog derived from source
sections and repository module boundaries. An unresolved component gets a focused
classification question; free-form component names cannot quietly alter solver
constraints. Code assigns each accepted criterion a reference derived from unit
ID and criterion ordinal, such as `unit-0001:criterion-01`, and fingerprints its
statement. The validation-coverage question receives exactly that criterion
reference and statement. Changes to a criterion invalidate its prior answer.
The model-facing `WorkUnitProposal` has `requirement_ids`, `deliverable`,
`component`, `implementation_runtime`, `planned_writes`, `implementation_step`,
`acceptance_criteria`, `validation_ids`, and `required_outputs` only.
`required_outputs` is a list of concrete output descriptions with requirement
references; code resolves them to `prerequisite_unit_ids` in stage 7.5. A model
cannot reference an unallocated sibling unit by inventing its future ID.

Ask `check_unit_cohesion` for each unit. Answer is `accept` or `split` with up to
four replacement unit descriptions preserving the same requirement coverage.
Splitting cannot introduce unrelated requirements or lose validation obligations.

Run CP-SAT over units using explicit component/runtime/size constraints. Hard
constraints: each unit assigned once; at most four units per PR; at most three
scope entries and five criteria in its compiled item; one implementation runtime
plus neutral writes; standalone CI bootstrap when actually required; manual
handoff ownership preserved. A useful smaller PR is allowed. Objective: distance
from three units and cohesion penalties, with stable tie breaking. Do not add a
PR-count-minimization objective. An infeasible unit creates a targeted split or
constraint discovery; no prose-based runtime regex is allowed as a veto.

For one grouped PR ask `describe_item` for only `title`, `objective`, and up to
three scope descriptions referencing its unit IDs. Code copies steps, paths,
criteria, and requirement allocation from units. The description question cannot
change units, identities, runtime, dependencies, or prerequisite records.

### 7.5 Resolve dependencies, validations, and human setup

Each unit lists required outputs as prerequisite-unit references or a bounded
unresolved-output description. Resolve an unresolved description using one
`resolve_prerequisite_output` question against a paginated catalog of candidate
deliverables. Answer is an existing unit ID, `external` with concrete evidence,
or `unresolved` with a reason. Candidates include source cross-references and
every potentially relevant deliverable; component proximity is not permission to
exclude a real cross-component dependency.

For each candidate edge ask `confirm_dependency`:

> Does this consumer need an output of this producer before its work can be
> implemented or validated?

Answer is tagged `required` with output/reason and requirement IDs, or
`independent` with reason. Code projects unit edges into item edges, removes
self/duplicate edges, and topologically sorts. Cycles create a repair discovery;
never delete an edge just to make sorting pass. Add genuine CI bootstrap edges
deterministically. Default preference for Gaterr is one initially ready PR when
its foundations require that ordering; genuine independent work may be ready
together. Never invent dependencies solely to force a singleton ready set.

For each item, code unions the validations registered on its units. Ask one
`check_validation_coverage` question per criterion to bind it to those validation
IDs. If none establishes the criterion, use `describe_requirement_validations`
again for that criterion only and rebind its units. Reuse the supplied repository
verification command as a registered validation. Ask one
`requires_human_setup` question for each validation:

> What concrete setup outside the worker's authorized capabilities must a human
> perform before this validation can run?

The exact answer fields are `required` (boolean), `reason`, `source_refs`, and
`evidence_ids`. A true answer schedules one `describe_human_setup` question
returning exactly `title`, ordered `instructions`, `verification`, and
`source_refs`. Code supplies the setup ID, human owner, supported trigger,
requirement IDs, and the current validation ID as `unblocks_validation_ids`.
The model cannot put a PR ID into a validation-reference field because that
field is never model-authored. A false answer schedules no setup-description
question and creates no setup object. Consider external credentials, GitHub App
installation, protected environments, administrator installation, macOS signing
and notarization access, test hardware, and independent review when relevant to
that validation. Repository-readable absence does not establish remote setup
absence; record the need for verification rather than fabricate a secret state.

Use human setup only for an action the worker cannot perform within its granted
authority. Ordinary code changes, running a local test, dependency ordering, and
routine PR review are not setup. Reject
sentinels such as “none,” “No manual prerequisite,” and “not applicable.”
`unblocks_validation_ids` must resolve to validation records, never item IDs.

Deduplicate shared setup through a focused equivalence question. Assign it once
to the earliest consuming item and make later consumers depend on that item.
For several setup actions at the supported trigger on one item, compile one
ordered `manual_prerequisite` object with all instructions, verification methods,
and concrete validation targets. Distinct unsupported trigger stages produce a
discovery requiring a deliberate contract extension; do not silently move them.

Example expected Gaterr answer: after implementing the trusted controller,
before its real report-only GitHub integration check, a maintainer registers and
installs the GitHub App and configures its protected environment. `unblocks`
renders the integration check's name/command, not “PR-11.” A Swift unit test
launching Python does not require a human prerequisite or Python implementation.

### 7.6 Generate Structrr operations in bounded steps

For each item, ask `enumerate_structural_subjects` using its units, planned writes,
source anchors, and current projected baseline. A subject candidate states
`section`, existing subject reference or proposed new identity description,
intended action, and requirement/unit references. Enumerate up to eight subjects
per page. Code binds existing IDs and allocates new identities using the current
Structrr taxonomy rules. Require an explicit final page, including a justified
empty enumeration when no structural operation is needed.

For each bound subject ask `describe_structural_change`. The model returns a
tagged `unchanged` with reason or `change` with section-specific desired content.
Code supplies operation ID, subject ID, item ID, action, and duplicate content
identity fields. Validate the answer against the taxonomy and section schema.
The model cannot return a top-level operation list or choose another subject.

Compile operations with `compile_multi_pr_proposal`, projecting predecessor
changes in deterministic dependency order. Reject conflicting changes from
unordered items. Shared structural state must have an explicit owner/dependency.
Oversized subject content requires a source/subject decomposition discovery;
never truncate content or silently accept the first returned operation.

### 7.7 Compile, validate, and publish

The compiler renders existing queue/workflow fields from accepted typed records.
Extend execution-plan top-level parsing with an optional `planning` mapping
whose nested schema is `https://powdrr.io/schemas/planning-manifest-v1`.
Require it on all newly generated plans. Keep existing reviewed plan fields and
the workflow descriptor format; there is no second legacy planner.

The mapping stores pinned source/base digests, requirement/source dispositions,
coverage and evidence references, unit-to-item allocation, typed runtime facts,
validation/setup records, semantic revision digest, and latest repair summary.
Store each canonical record once and reference its ID elsewhere. Source prose
stays in its source document; large code excerpts and raw exchanges stay in run
diagnostics. This is one reviewed manifest inside the execution-plan file, not
additional files per question or workflow task.

Older reviewed plans without this metadata continue through the existing worker
parser. A first repair constructs the missing ledger from pinned source and
existing items and includes it in the amendment PR. Never assume old requirements
complete or recreate their issues merely because provenance metadata is absent.

Run the following gates before writing or staging generated artifacts:

1. Every source block is accounted for and every unresolved requirement has work
   allocation; completed/satisfied requirements have validated evidence.
2. IDs/references are unique and resolved; no partial enumeration is accepted.
3. Units meet size/runtime constraints; dependencies are acyclic; ready set is
   computed and printed with a reason for each blocked item.
4. Every human setup maps to an owning item and concrete validation; no sentinel
   or unresolved external authorization is represented as executable code.
5. Every Structrr subject enumeration is complete and operations compile against
   the pinned baseline and dependency projection.
6. Execution plan, workflow descriptors, dependency IDs, manual prerequisites,
   source reference, and Structrr item assignments agree exactly.
7. The source and base commit used to generate the plan still match the session.

Stage all artifacts atomically from a temporary output directory only after the
gates pass. Open one plan review PR with the source link, requirement coverage,
completed/reused work, proposed PR titles/dependencies, human steps, and observed
validation. Do not replace an existing verification script with an always-green
placeholder or add redundant CI when the pinned inventory already supplies it.

## 8. Separate repair and replanning contract

### 8.1 Entry points and discovery classification

`plan` creates a new plan. `plan-repair` changes an existing plan revision.
Replanning remaining work uses `plan-repair --refresh-evidence`; it preserves
fulfilled items and stable IDs, and does not require a new plan ID. Full product
scope replacement must be an explicit user-requested new plan.

Discoveries may come from a deterministic validation gate, updated source/code,
worker evidence, or a user request. Kinds are `source_change`,
`existing_implementation`, `scope_defect`, `dependency_defect`,
`human_setup_required`, `structrr_conflict`, and `external_constraint`.
Malformed model answers are question corrections, not plan discoveries.

For each discovery ask `locate_repair_impact`: which supplied requirements,
units, validations, setup records, structural subjects, and items are directly
affected, and why? Answer references existing IDs and source/evidence anchors.
Code expands the declared set to include incoming/outgoing dependency references,
shared Structrr subjects, and compiled artifacts requiring regeneration. Expand
impact because of concrete edges/references, not a model's vague “whole plan.”

Ask bounded repair questions for only affected subjects. Use the same extraction,
coverage, unit, dependency, validation/setup, and Structrr question contracts as
initial planning. A repair cannot rewrite the entire plan through a new prompt.

### 8.2 Typed patch and deterministic application

Code translates accepted replacement records into these operations:

| Operation | Required fields | Constraints |
| --- | --- | --- |
| `add_item` | `new_item_ref`, `unit_ids`, `reason`, `requirement_ids` | Code allocates stable item/workflow IDs |
| `update_item` | `item_id`, `expected_digest`, `replacement_unit_ids`, `reason` | Existing ID is preserved when its deliverable is preserved |
| `retire_item` | `item_id`, `expected_digest`, `disposition`, `reason`, `replacement_item_ids`, `evidence_ids` | `disposition` is `fulfilled` or `superseded`; never erase history |
| `set_dependency` | `before_item_id`, `after_item_id`, `present`, `expected_edge_digest`, `reason` | Validate endpoints and cycles on the final graph |
| `update_requirement` | `requirement_id`, `expected_digest`, `replacement_ref`, `source_refs`, `reason` | Scope removal requires changed source or explicit user decision |
| `assign_setup` | `setup_id`, `owner_item_id`, `validation_ids`, `expected_digest` | Exactly one owner; dependent consumers accounted for |

The patch envelope contains `schema: plan-repair-patch-v1`, `repair_id`,
`base_plan_digest`, `discovery_ids`, `affected_item_ids`, `operations`, and
`expected_result_digest`. The model never fabricates this envelope or its CAS
digests. Code computes them from accepted answers and canonical records.

Apply the entire patch to an in-memory copy, validate references and coverage,
then compile all affected artifacts. Any failure rejects the whole patch.
Unchanged items retain semantic digests and workflow IDs. Unrelated documents
remain byte-identical. Re-render the affected plan file deterministically and
preserve stable item ordering except where a valid dependency change requires a
topological move.
If CP-SAT is needed during repair, freeze unaffected and fulfilled assignments;
optimize only the affected units. It cannot regroup the entire plan to improve
the sizing score. Expanding the impact set requires a recorded discovery and
another validated patch, never an incidental solver side effect.

Splitting an uncompleted item retires its old scope with a successor mapping and
allocates new IDs for materially different deliverables. Do not reuse an old
issue for unrelated replacement work. A merged implementation's accepted
requirements remain fulfilled; new follow-up behavior gets a new item. A repair
cannot reopen completed work merely because new code evidence is unavailable.
Keep retired-item history in `planning`; exclude superseded items from the
executable top-level item list and redirect every active dependency to its
declared successors in the same atomic patch. Fulfilled items may stay in the
plan's item catalog and remain fulfilled through their issue outcome. A completed
item is never converted to a pending successor without new requirements.

Validate the complete graph, requirement allocation, and projected Structrr
proposal, including unaffected consumers of changed outputs. A discovery can
cause a second focused repair iteration; at most three semantic repair
iterations per invocation, each checkpointed. After that, report unresolved
discoveries for human review. Accepted earlier answers remain resumable.

### 8.3 Draft versus reviewed plans

Before initial publication, an accepted patch changes the draft and resumes from
the earliest invalidated question. There is still one final plan review PR.

For a reviewed/published plan, generate an amendment PR from fresh remote main.
Include a compact repair manifest in `planning.latest_repair`: repair ID, discovery
IDs, old/new semantic revision digests, per-item old/new content digests,
retire/successor mappings, and the affected set. PR prose explains the concrete
discovery, corrections, preserved work, dependency/ready-set changes, human
actions, and issue effects. Do not mutate work-item issue pointers to the
proposed revision until the amendment merges.

Use one repair service for CLI and worker-discovered repairs. Replace the direct
coding-agent edits in `_run_plan_amendment_agent` with this service. Preserve its
existing failure classification and publication behavior. The generated artifact
manifest, rather than the current fixed three-path tuple, determines allowable
paths when several linked items are affected.

### 8.4 GitHub issue and checkpoint reconciliation

Give amendment PRs machine-readable repair metadata; stop identifying repair
merges only by the PR title prefix. Reconcile every item in the explicit impact
manifest, not only the issue that discovered the defect.

While a reviewed repair is pending, append `plan-repair-pending-v1` comments to
affected published issues and mark their executable work blocked through the
existing queue-state mechanism. Preserve the previous state in the checkpoint.
Unaffected independent work may continue. A running affected worker must detect
the pending repair at its next durable boundary and pause before committing
further work or claiming completion. Publication uses a final revision check so
a late worker cannot publish against a superseded item after this pause.

After merge, perform an idempotent reconciliation keyed by `(repair_id,
item_id, merge_commit)`. Re-read remote merge state, validate the manifest and
compiled plan, then:

- Update affected issue pointers to the merged plan revision.
- Retain fulfilled/closed items and their implementation evidence.
- Create issues only for newly allocated items; link superseded issues to all
  successors and close them with a recorded superseded disposition.
- Recompute ready/blocked labels from the merged graph and dependency outcomes.
- Preserve task checkpoints only when their recorded input-contract digest
  equals the repaired task's digest. Changed task inputs invalidate that task
  and dependent task checkpoints with an explicit comment; archive rather than
  delete the old comments. Do not rerun unaffected tasks automatically.
- Preserve `awaiting_human` only when the setup and its blocked-validation inputs
  are unchanged. Otherwise invalidate that pause and regenerate instructions.

If a repair PR closes unmerged, restore the prior queue state only after checking
that its original plan revision is still current. If another revision has merged,
refresh the discovery/patch against that revision rather than overwriting it.
Partial GitHub API failure resumes from recorded per-item reconciliation steps.

## 9. Durable sessions, diagnostics, and CLI behavior

For CLI planning, create one planning-session issue before model calls, labeled
`powdrr:planning`, with pinned source/base references. This is a tracking issue,
not an executable plan-item issue: it has none of the ready/claimed queue labels.
Worker repairs use the originating work-item issue as their session journal and
link all additional affected issues. Do not store operational state in a PR that
exists only to checkpoint workflow progress.

Append versioned comments containing question identity, input digest, accepted
typed answer, validation result, state transition, and allocated ID counters.
Keep each comment below 48 KiB. Split large accepted-state checkpoints into
numbered immutable comment chunks and publish a manifest comment with their
digests last. An incomplete chunk set cannot become accepted state. Use an
existing issue-linked lock/claim where available and a parent checkpoint digest
to detect concurrent updates; validate the parent again before advancing state.

Local run artifacts live under Git's common directory in
`powdrr-lift/planning-runs/<session-id>/`; they are caches and diagnostics, not the
durable authority. Record raw request/response, schema version, provider/model,
usage when available, wall time, and validation errors for every question.
Exclude provider authentication headers and secret values. Store complete source
and code excerpt digests for reproducibility. A machine with no local cache can
resume from accepted answers in issue comments and the pinned Git revision.

The existing worker file lock coordinates one host only. Do not claim that a
read-then-append issue comment is a distributed compare-and-swap. V1 supports one
active coordinator per session and sequential resume on another machine. Detect
two checkpoint children of the same parent as a journal fork, mark the session
`conflicted`, and prohibit publication until an explicit resume selects the
accepted checkpoint. Derive a stable publication branch/key from session or
repair ID, deduplicate PR creation by that key, and require a final canonical
checkpoint check before any publication or issue reconciliation. Tests must
exercise a fork; a duplicate answer may be recorded but cannot authorize two PRs.

Add these CLI forms, keeping existing `plan` provider/model flags:

```text
powdrr-lift plan --source PATH --plan-id ID --verification-command COMMAND
powdrr-lift plan-resume --repository OWNER/REPO --session-issue NUMBER
powdrr-lift plan-repair --plan PATH --discovery PATH [--refresh-evidence]
```

`--discovery` is a JSON `Discovery` input with evidence references resolved by the
service; reject paths outside the repository/run artifact boundary. JSON output
includes `session_issue_url`, `status`, current question/discovery, accepted-answer
count, and final PR URL when published. Human progress reports name the stage and
subject, for example “coverage req-0017: accepted,” rather than only stream chunk
counts. A provider/shape failure prints its question ID and JSON-pointer error.

If main advances while a session runs, keep the original snapshot; never mix new
evidence into old accepted answers. Before publication, if remote main changed,
record an `existing_implementation`/baseline discovery, refresh affected facts,
and repair/recompile. If that cannot be completed within the repair iteration
bound, pause with a resumable session. Do not silently publish stale assumptions.
Use a final remote-head check immediately before publication; a later ordinary
GitHub merge conflict remains subject to normal PR review and rebasing.

## 10. Implementation PR sequence

Implement in this order. Each row is a separately reviewable PR with one primary
deliverable. Dependencies are mandatory. Keep new modules disconnected from
production until their integration PR; after the switch remove the old path.

| PR | Deliverable | Depends on | Files and concrete work | Required acceptance evidence |
| --- | --- | --- | --- | --- |
| 01 | Contracts and captured failure fixtures | — | Add `planning/contracts.py`; add sanitized response fixtures under `tests/fixtures/planning/`; define tagged question/answer and validation-error contracts | Exact schemas/parsers agree; the six malformed/sentinel examples fail or select the correct explicit variant; no provider credentials or temp-path dependencies |
| 02 | Bounded question runner | 01 | Add `questions.py`; reuse provider clients/backoff; exact prompts/examples, context/output bounds, per-question correction | A scripted provider fails one answer twice and then succeeds without repeating accepted predecessors; 429 uses existing backoff; overlarge context is decomposed, never truncated |
| 03 | Durable planning journal | 01, 02 | Add `checkpoints.py`; GitHub comment store, in-memory fake, local diagnostic cache, resume reducer | Crash before/after accepted-answer checkpoint, incomplete chunk manifest, duplicate delivery, and stale parent all recover safely; resume on another cache directory preserves IDs |
| 04 | Source obligation ledger | 02, 03 | Add `source.py`; pinned Markdown segmentation, requirement extraction/fidelity questions and block dispositions | Long source with late human setup and cross-references accounts for every normative block; source edits invalidate matching answers; duplicate requirements preserve both anchors |
| 05 | Requirement-level code coverage | 04 | Add `evidence.py`; fix file-path versus symbol lookup; one coverage question per requirement; residual records | Realistic schema/parser code and tests produce complete/partial/missing records correctly; invented paths/symbols fail; a missing `remaining_item` is structurally impossible in the new schema |
| 06 | Typed work units and CP-SAT inputs | 04, 05 | Add assembly allocation records; adapt `plan_sizing.py` to explicit runtimes/components/units; unit and item-description questions | Swift sandbox tests mentioning Python remain Swift; actual Python/Swift writes split; three-unit target/four-unit maximum and coverage conservation hold; no prose-runtime veto remains |
| 07 | Dependency graph and human prerequisites | 06 | Add output-reference resolution, edge confirmation, validation/setup questions, setup ownership compiler | Real dependencies remain edges; cycles surface discoveries; shared GitHub App setup appears once before its actual integration validation; sentinel setup and PR IDs in validation references fail |
| 08 | Structrr generation per subject | 02, 06, 07 | Replace all-plan operation generation with per-item enumeration and per-subject description; use current multi-PR compiler | More than eight subjects paginates completely; bare operation response fails its local schema; predecessor projections and unordered conflicts are verified |
| 09 | One artifact compiler and planning metadata | 05, 06, 07, 08 | Add `compiler.py`; update queue plan parser/serializer; generate plan/proposal/workflows from records; validation gates | Round-trip all artifacts; preserve source references and exact dependency/manual context; no partial output is staged; old reviewed items remain readable through the existing parser |
| 10 | Connect `plan` and durable resume through Procedrr | 03, 09 | Add `service.py`, `plan-execution.yaml`, operation registration, CLI dispatch and resume; remove whole-plan draft/refine/reconcile production code | CLI scenario opens one plan PR; interruption resumes one question; CI reuse is preserved; no model call requests the entire plan or audit; tracking issue never becomes queue work |
| 11 | Draft repair engine | 07, 09 | Add `repair.py` and `repair-execution-plan.yaml`; impact expansion, typed patches, CAS, selective invalidation | A coverage/dependency discovery patches a draft; unrelated item digests/IDs stay stable; split preserves requirement allocation; stale patch and cyclic result fail atomically |
| 12 | Reviewed replanning and worker amendments | 10, 11 | Add `plan-repair`; replace direct artifact-editing amendment agent with shared repair service; compile artifact path manifest and review PR | CLI and worker submit the same discovery and produce the same semantic patch; a multi-item repair changes exactly the declared impact set; product code stays untouched |
| 13 | Reconcile repair merges with all issues | 03, 12 | Extend `github_issue_queue.py`; machine-readable repair metadata, pending blocks, merged/closed PR handling, successor and checkpoint reconciliation | Repeated reconciliation is idempotent; affected workers pause; completed items remain fulfilled; changed checkpoints invalidate; partial API failure resumes; unrelated issues retain state |
| 14 | Full planning/repair scenario and Gaterr acceptance | 10, 12, 13 | Add scenario fixtures and end-to-end tests; run the live acceptance sequence below; document session/PR links | New Gaterr plan review PR and one focused repair PR validate; report actual prerequisite ownership, source coverage, ready set, and issue/checkpoint effects |

Each implementation PR runs repository-required tests, formatting, lint, and
type checks in the existing shared environment. Add focused tests with the PR
that introduces each contract; do not postpone all integration tests to PR 14.
Keep ordinary CI active from PR 01 through the final integration.

Use dedicated test modules so an implementation model can locate the relevant
behavior: `tests/test_planning_contracts.py` (01),
`test_planning_questions.py` (02), `test_planning_checkpoints.py` (03),
`test_planning_source.py` (04), `test_planning_evidence.py` (05),
`test_planning_assembly.py` (06–07), `test_planning_structrr.py` (08),
`test_planning_compiler.py` (09), `test_planning_cli.py` (10),
`test_plan_repair.py` (11–12), and `test_plan_repair_issue_reconciliation.py`
(13). PR 14 adds `test_planning_scenarios.py`. All names are under `tests/`;
reuse existing queue/Procedrr tests for their integration boundaries. The tests
are ordinary CI tests with fake providers/GitHub transport; live acceptance is
an additional observed run, not a prerequisite for ordinary test execution.

## 11. Required tests and acceptance scenarios

Use a scripted `WorkflowLLMClient` keyed by question kind and subject ID, not
call count. It must assert the expected schema/context and can return recorded
malformed output, a corrected response, transport errors, or semantic discoveries.
Tests must fail if the implementation asks a whole-plan question or includes an
unrelated subject in a bounded packet. Do not use a fake that blindly returns the
same ideal plan for draft, audit, repair, and Structrr requests.

Commit these scenarios with enough implementation/test bodies and source prose
to exercise real parsing and evidence validation:

1. A 13-section source plan with an obligation in the final section and a setup
   requirement in an operational subsection. Both appear in the final ledger.
2. Existing Python schemas/parsers/fixtures plus missing authorization behavior.
   The completed portions stay recorded and only the remaining work is proposed.
3. A Swift sandbox test names Python, shell, and Make child processes. Runtime is
   Swift. A separate authored Python verifier unit cannot share its code PR.
4. Existing macOS CI already invokes `bash attestation/scripts/verify-all.sh`.
   The generated plan reuses it; no placeholder-success script is proposed.
5. A GitHub App setup requirement appears after controller implementation and
   before its live report-only test. Instructions name real administrator steps
   and the exact blocked validation. Ordinary unit tests have no setup record.
6. The provider returns each recorded wrong-root answer. Only the current
   question is corrected; a corrected answer cannot erase other requirements.
7. A wholly missing coverage answer contains only its tagged fields. It cannot
   fail for absent fields belonging to partial coverage. Partial coverage with
   no concrete remainder or implementation evidence still fails.
8. A 12-subject Structrr enumeration requires two pages. Missing the final page,
   repeating the cursor, or returning only the first subject prevents publication.
9. A dependency cycle triggers a focused draft repair, with source/evidence
   explaining the replacement unit or corrected edge. Unrelated units persist.
10. A reviewed item discovers that an existing verifier must be reused. A repair
    updates that item's units and projected Structrr assignment, regenerates its
    descriptor, opens an amendment PR, and blocks affected issue execution until
    merge. It cannot overwrite the verifier with a duplicate implementation.
11. A repair splits a pending item into two successors while a different item is
    already fulfilled. The fulfilled issue stays closed, successor issues get
    stable new IDs, and dependent issues follow the replacement graph.
12. A human step is unchanged across a dependency repair: its `awaiting_human`
    checkpoint survives. Changing its instructions or validation inputs invalidates
    the checkpoint and stages updated instructions.
13. A crash after some issue pointers are updated resumes reconciliation exactly
    once per item. An unmerged-closed repair restores only still-current states.
14. Main advances during planning or another repair merges first. The stale
    patch cannot apply; refresh discovers affected work and preserves accepted
    answers whose input digests still match.

Add artifact consistency tests that parse the actual generated execution YAML,
proposal JSON, and every workflow descriptor. Inspect their requirement
allocation, dependency graph, source reference, and manual prerequisites. Mocking
publication is appropriate for these tests; bypassing the real compiler is not.

## 12. Live acceptance and handoff

After the implementation PRs merge, use a dedicated clean Gaterr worktree from
the latest remote main and the current source document:
`docs/plans/local-validation-attestation-implementation-plan.md`. Use
`deepinfra-cheap` and verification command
`bash attestation/scripts/verify-all.sh`. Preserve the existing user's dirty
checkout. Run the real CLI, checkpoint store, question runner, Structrr compiler,
and plan PR publisher.

Inspect the resulting plan and attach an observed acceptance receipt to the
planning-session issue containing:

- Pinned source blob and repository base SHA; planning-session issue and PR URL.
- Number of source blocks, obligations, completed requirements, unresolved
  requirements, units, items, and accepted/retried questions.
- Source coverage conservation and each missing/partial requirement's allocation.
- Reused existing CI/schema/verifier evidence, and any unresolved evidence limits.
- Actual human setup owners, instructions, trigger, and blocked validations.
- Dependency graph and computed initial ready set with explanatory edges.
- Complete Structrr subject enumeration and successful artifact consistency gates.
- A resume demonstration that does not reissue already accepted questions.

Then inject a concrete, testable discovery about reusing an existing verifier
or moving one human setup to its correct validation boundary. Run `plan-repair`
against the reviewed plan and inspect its PR. Record the exact affected set,
preserved semantic digests, ready-set changes, and expected issue transitions.
Merge remains the user's review step. Exercise issue reconciliation against the
merged revision only after that merge, using real queue commands and issue
comments; tests cover these transitions before live use.

Do not call the work complete because schemas parse, a mocked plan succeeds,
or an LLM says its own plan is good. Completion requires the two real reviewable
PR artifacts and the observed checks above. If live output is rejected, keep its
question trace and add a focused regression before changing the contract or
retrying that question. Do not repeatedly rerun the whole source plan to discover
the next formatting failure.

## 13. Exact question catalog and implementation recipe

Use the names below as the `kind` values in `Question`. Implement each schema
and its parser together. Common `reason` fields are nonempty strings of at most
1,000 characters; text fields cannot contain unresolved template placeholders.
Source/evidence/requirement/validation references resolve against the question's
supplied catalog. All examples in prompts must pass the same parser as answers.

| Kind | Subject | Answer root fields and variants |
| --- | --- | --- |
| `ci_command_equivalence` | One CI invocation and target command | `result` enum (`equivalent`, `different`, `unknown`), `evidence_ids`, `reason` |
| `extract_requirements` | One bounded source block/range | Enumeration envelope; records contain `statement`, `kind`, `source_refs`, `related_source_refs` |
| `classify_source_block` | One source block | `disposition` plus `requirement_ids` for `normative`/`duplicate`, or `reason` for `context`; duplicate also requires `reason` |
| `check_requirement_fidelity` | One requirement and its anchors | `decision: accept` with `reason`, or `decision: revise` with `statement`, `reason` |
| `compare_requirement_equivalence` | Two requirements | `equivalent` boolean, `reason`; only true permits deterministic union of their anchors |
| `assess_requirement_coverage` | One requirement, pinned evidence | The exact three tagged variants in section 7.3 |
| `assess_setup_state` | One human setup requirement | `status` enum (`required`, `satisfied`, `unknown`), `evidence_ids`, `reason` |
| `describe_requirement_validations` | One remaining requirement/criterion | Enumeration records contain exactly `description`, `kind`, `target`, `source_refs`, `evidence_ids`; code supplies ID and the subject's requirement references |
| `propose_work_units` | Up to eight related unresolved requirements | Enumeration of `WorkUnitProposal` records defined in section 7.4 |
| `check_unit_cohesion` | One work unit | `decision: accept`, `reason`; or `decision: split`, `replacements` (two to four proposals), `reason` |
| `describe_item` | One frozen CP-SAT group | `title`, `objective`, `scope` (one to three records of `description` and `unit_ids`) |
| `resolve_prerequisite_output` | One required output and a candidate page | `result: unit`, `unit_id`, `reason`; `result: external`, `evidence_ids`, `description`, `reason`; or `result: unresolved`, `reason`. An unresolved page advances to the next candidate page before creating a discovery |
| `confirm_dependency` | One producer/consumer pair | `result: required`, `output`, `requirement_ids`, `reason`; or `result: independent`, `reason` |
| `check_validation_coverage` | One criterion and item validation catalog | `covered` boolean, `validation_ids`, `reason`; false requires an empty ID list and a follow-up validation question |
| `requires_human_setup` | One validation and its authority/context facts | `required` boolean, `reason`, `source_refs`, `evidence_ids`; true schedules a description question |
| `describe_human_setup` | One validation with accepted human-setup need | Exactly `title`, ordered `instructions`, `verification`, `source_refs`; code binds identity, owner, trigger, requirements, and validation IDs |
| `compare_setup_equivalence` | Two concrete setup proposals | `equivalent` boolean, `reason`; true permits a shared setup ID, with both source references preserved |
| `enumerate_structural_subjects` | One item and projected baseline | Enumeration of candidates described in section 7.6; each candidate contains `section`, `subject_ref` (nullable), `new_identity_description` (nullable), `action`, `requirement_ids`, `unit_ids`, `source_refs`; exactly one identity field is populated |
| `describe_structural_change` | One bound structural subject | `result: unchanged`, `reason`; or `result: change`, `desired_content`, `reason`; the runtime supplies section-specific content schema |
| `locate_repair_impact` | One discovery and revision catalogs | Enumeration of `subject_kind`, `subject_id`, `reason`; the runtime computes the closure and patch envelope |

All enumeration envelopes contain `records` (zero to eight records), `has_more`
(boolean), and `next_anchor` (nullable source/catalog position). A false
`has_more` requires null `next_anchor`. Validate advancement against the supplied
range and keep accepted pages; a later malformed page cannot erase them. Setup
and requirement equivalence questions are singular; never compare every pair in
one large matrix answer.

Implement `next_question(state)` as a pure function with these return variants:
`Ask(Question)`, `RunDeterministicOperation(operation)`,
`NeedsRepair(Discovery)`, `ReadyToPublish(CompiledPlan)`, and `Paused(error)`.
Its inputs are accepted records, pending enumerations, and pinned digests. It
does not call providers, GitHub, Git, or the filesystem. The Procedrr adapter
executes the selected operation and checkpoints its typed result. This separation
makes scheduling, resume, and context-size assertions deterministic in tests.

Each accepted answer records the exact IDs and digests it consumed. Build a
reverse dependency index from those inputs. On repair, invalidate only answers
whose inputs changed and their transitive dependents; an answer must never remain
accepted merely because its subject ID is unchanged. Description-only edits do
not invalidate code coverage evidence unless the requirement meaning or evidence
digest changed. A changed provider/model may reuse accepted answers but never a
pending raw response or a different schema version's parsed result.

For PR 01, first write fixture-backed contract tests for the observed failures.
For every later PR, add a failing behavioral test for its row in section 10,
implement the smallest listed module/service change, run the required suite, and
open its feature PR. Do not rename existing worker states, replace provider
configuration, or redesign coding-agent boundaries as incidental cleanup.

Source-block accounting and reference validation guarantee structural coverage;
they do not prove that a model extracted every nuance correctly. Fidelity
questions, cross-reference preservation, the committed adversarial scenarios,
and review of the source-to-requirement manifest address that semantic risk.
Report unresolved disagreements in the review PR rather than describing model
judgments as deterministic proof.
