# Benchmark-safe external research

Status: future plan; no additional enforcement implemented by this document

## Goal

External research should improve instruction-to-prompt completeness by
recovering applicable public contract requirements without importing a
benchmark's answer, test oracle, or implementation hints. Each generated
requirement must be independently justified by a versioned contract source,
and the complete evidence-to-prompt path must be auditable after the run.

This is a process-integrity goal, not a promise that a model has no latent
knowledge of a solution. The enforceable guarantee is that planning inputs and
requirements are sourced only from the request, the captured base repository,
and independently applicable public contract material—not from benchmark
solutions, verifier feedback, or answer-shaped task mirrors.

## Threat model and boundary

The principal risk is not merely putting a solution in the final worker prompt.
If solution-bearing material reaches the planner while it is deciding what to
research or which sources to trust, the benchmark may already be contaminated.
Filtering only during final prompt assembly is therefore too late.

Treat as prohibited research inputs:

- benchmark/task pages, prompt mirrors, task-specific discussions, and
  benchmark datasets;
- expected patches, reference solutions, golden outputs, hidden/public
  verifier tests, harnesses, and grader feedback;
- post-run implementation diffs or failure diagnoses for the task being
  planned; and
- search snippets or page content that disclose any of the above, even when a
  title or URL does not identify it clearly.

An authoritative host is not sufficient by itself. A project issue, example,
or maintainer discussion may contain a task-specific answer rather than
independent evidence of the public contract. Normative specifications and
versioned API references are preferred; other sources need a documented reason
why they are the primary record of the applicable contract.

## Planned controls

### 1. Freeze a research manifest before retrieval

Before external search, persist a manifest bound to the base-repository
revision and the pre-research design. For every research subject, record:

- the request clause and tracked-repository evidence that triggered research;
- the public standard/API and exact version or profile;
- the unanswered contract question and why answering it is necessary for the
  requested capability;
- the bounded query text, with confirmation that it contains no task ID,
  private repository detail, verifier result, expected output, or solution
  clue; and
- the time and identity/version of the research procedure that made the plan.

Once retrieval starts, the subject and questions are fixed for that run. New
questions require a separately recorded reason from allowed request/repository
evidence, never a verifier failure or observed reference solution.

### 2. Enforce source eligibility before model exposure

Search is discovery, not evidence. Do not pass raw result snippets to a model
that is deciding what to retrieve. Apply deterministic URL and publisher
eligibility checks first, then expose only eligible metadata needed for source
selection. Prefer explicit standards-body and project-maintainer domains and
versioned documentation. Pattern-based rejection of words such as “benchmark”
is defense in depth, not proof of safety.

After fetch, inspect bounded source content for task/solution contamination
before any excerpt or derived claim is sent to a planning model. If source
content includes benchmark answers, expected behavior specific to an evaluation
task, or implementation patches, quarantine the source and all claims derived
from it. Do not try to salvage apparently normative passages from a contaminated
page unless a separate clean authoritative source independently establishes
them.

Only the smallest relevant contract span should enter claim assessment. Keep
examples and implementation guidance out unless the contract question cannot
be resolved without them; if used, mark them as explanatory rather than
normative evidence.

### 3. Keep evaluation feedback in quarantine

Separate planning/research artifacts from implementation and evaluation
artifacts. Before the prompt is frozen, the planner must not receive verifier
tests/results, benchmark solutions, agent diffs, or post-run diagnoses. After
the run, evaluation may measure success, but its output must not flow backward
into that run's research plan or source assessment.

If evaluation identifies a suspected missing contract requirement, start a
new research record. Re-establish it from the original request, the captured
base repository, and clean public contract sources without consulting the
failure details as evidence. Evaluation may flag a question to investigate;
it cannot answer that question or directly create a prompt obligation.

### 4. Preserve end-to-end audit traceability

For every accepted or rejected claim, retain a machine-readable lineage:

`request/repository trigger → frozen research question → query → eligible
source → captured content hash and revision → exact excerpt → applicability
decision → projected prompt obligation`.

The run artifact should include timestamps, base commit, procedure/schema
version, provider and query parameters, selected and rejected source metadata
with reasons, requested/final URLs, HTTP status, content hash, document
revision, exact excerpt offsets or stable anchors, claim text, applicability
rationale, conflicts/unresolved questions, projection result, and the final
prompt hash. Retain source snapshots where permitted so later page changes do
not make the audit irreproducible. Secrets and unnecessary private repository
content must not be recorded or sent to the search provider.

The audit should make it possible to answer: “Which evidence caused this exact
sentence to appear in this prompt?” If that chain is incomplete, the claim must
not be treated as externally established; record uncertainty and continue
prompt generation using the existing fallback behavior.

### 5. Make quarantine visible and testable

Use separate artifact namespaces or access boundaries for planning evidence
and evaluation data. Record attempted prohibited access as a policy event.
Add adversarial tests covering innocuous URLs with solution-bearing snippets,
official-host benchmark mirrors, task-specific issues, contaminated source
pages, verifier outputs appearing in context, and claims with missing or
invalid provenance. Tests should assert that prohibited material is blocked
before model exposure, not merely omitted from the final prompt.

## Rollout and acceptance criteria

Implement this in stages: define the provenance schema and threat policy;
enforce input/source gates; establish planning/evaluation quarantine; then add
adversarial integration tests and inspect real run artifacts. Keep the existing
guarantee that external search failure or source rejection does not prevent a
usable prompt from being generated.

Do not call the safeguard complete until:

1. No raw search snippet or fetched source reaches a planning model before
   eligibility and contamination checks.
2. No verifier, solution, or post-run implementation artifact is reachable
   from the planning context before prompt freeze.
3. Every externally derived prompt obligation has complete, reproducible
   source-to-prompt lineage.
4. Rejected and unresolved evidence remains visible in the audit record without
   being silently promoted to requirements.
5. Adversarial tests prove those properties at the boundary where data enters
   model context.

