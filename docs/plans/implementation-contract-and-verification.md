# Implementation contract and verification

Status: Proposed consolidation. Existing capabilities below are implemented;
remaining milestones require implementation and acceptance evidence.

Audited code baseline for this inventory: `feafe5b7b22bf7cd98dee9b06797ea87af558a4c`.

This plan replaces the intent-linked verification, evidence-backed prompt
contract closure, and bounded coding-worker plans in `superseded/`. Those
historical specifications explain earlier decisions; their implementation
sequences and benchmark stopping rules no longer define the backlog.

Migration policy: legacy implementation routes do not need compatibility
support. Remove them or make them explicitly unsupported; do not port their
retry, prompt, or validation behavior into the protected route. Keep only
non-implementation workflow capabilities that do not invoke a coding worker.

## Goal and authority

Carry accepted instructions and retained repository intent through one reviewed
design, an immutable implementation handoff, bounded execution, independent
verification, and an accepted candidate. Every mandatory obligation needs
inspectable evidence or an explicit blocked result. Structrr owns meaning and
revision identity, Procedrr owns procedure and gate ordering, Workrr executes
operations, and the coding worker implements the accepted contract.

Use the existing [semantic compilation design](../design/source-anchored-semantic-contract-compilation.md)
and [worker boundary](../design/external-coding-agent-boundary.md). This plan
consolidates remaining integration work; it does not authorize hosted deployment,
new models, a new test-generation pipeline, or benchmark-specific product rules.

## Current implementation

| Capability | Current code and evidence | Limit that remains |
| --- | --- | --- |
| Accepted source authority | `workrr/source_reference_dispatch.py::load_reviewed_source_design` reads a bounded regular blob from the exact Git base and validates plan/item identity. | This route requires the explicit reviewed `source_reference_design_file` selector. |
| Immutable handoff and isolated execution | `SourceReferenceRun.prepare`, `authorize`, `claim_invocation`, and `execute` seal inputs and invoke MiniSWE once under Seatbelt or bubblewrap. | This guarantee applies to prepared source-reference runs, not every provider or legacy entry point. |
| Source context and placement checks | Service `structrr/source_reader.py`, `source_observation.py`, `reference_validation.py`, and Workrr source-reference validation bind reads, owners, and canonical definitions. | Mechanical annotation coverage cannot establish behavioral correctness. |
| Metadata reconciliation | `SourceReferenceRun.reconcile_after_worker` accepts a trusted reconciliation receipt, verifies the candidate, compares Python AST snapshots, and rejects product syntax changes. | Reconciliation must preserve frozen authority and separately identify corrected metadata. |
| Story evidence | Service `verification/story_execution.py` compiles receipts and coverage reports. Dispatch authors and freezes story tests, executes protected checks, and requires verified pinned coverage before patch transfer. | The observer uses the pytest adapter; other frameworks and unsupported environments block. |
| Retained intent and verification models | Service `structrr/verification_contract.py`, `verification_obligations.py`, `verification_health.py`, `obligation_evidence.py`, and `invariant_review.py` provide contracts and freshness/review machinery. | Their existence does not prove that every production entry point invokes every applicable gate. |
| Attempt-local progress and scope | `workrr/coding_agent.py::run_coding_agent` compares before/after snapshots, detects protected/out-of-scope edits and rewritten history, and records attempt status. | The same module still exposes OpenCode, recovery, execution-unit requests, and repair prompts. Legacy flows remain active. |
| Accepted patch transfer | Protected dispatch checks candidate identity, exports and checks the patch, and rejects conflicting caller state before application. | Temporary run artifacts are not a permanent audit store. |

Regression evidence includes `tests/test_source_reference_dispatch.py`,
`test_source_reference_run.py`, `test_story_execution.py`,
`test_structrr_reference_validation.py`, and `test_coding_agent.py`.
These fixtures establish their exercised contracts; they do not establish live
model quality or completion of the integration milestones below.

## Remaining milestones, in dependency order

### 1. Inventory production routes and freeze the migration contract

Trace `task_agent.py`, `feature_endpoint.py`, issue workers, CI repair, CLI and
Harbor entry points through request compilation, provider invocation, validation,
and delivery. Publish a route matrix identifying accepted authority, prompt
identity, permitted invocation count, scope policy, required gates, and artifact
locations. Classify each route as protected or explicitly unsupported. Remove
legacy implementation routes instead of preserving or porting them. Keep
non-implementation workflow capabilities only where they do not invoke a coding
worker. The reviewed source-reference route is the only supported code-writing
route until another route is explicitly accepted in this plan.

Acceptance: executable fixtures demonstrate the protected route or explicit
unsupported result for every entry point and reject missing authority or silent
enforcement downgrade. Legacy provider/recovery entry points are removed or
unreachable. The matrix records any retained non-implementation workflow path.

Initial route audit: [implementation-route-matrix.md](implementation-route-matrix.md).
It records the current issue-worker selector and the legacy routes slated for
removal or explicit rejection, plus feature, CI repair, CLI, and Harbor/Pier
delegation paths. The existing source-reference selector fixture proves
protected dispatch and fail-closed handling for missing reviewed authority.
Cross-entry-point route fixtures and legacy-route removal are required before
this milestone meets its acceptance criteria.

### 2. Compile one complete, source-backed obligation closure

Reuse the instruction ledger and semantic-contract compiler. For each accepted
change, retain exact source clauses, confirmed decisions, affected public
surfaces, baseline behavior to preserve, relevant external contracts, and
observable required cases. Compile prompt text and private validation obligations
from the same revision and fingerprints. Keep unknown meaning explicit and route
material behavior choices to design confirmation.

Acceptance: fixtures cover omitted optional arguments, nested error continuation,
unsupported operations, negative requirements, conditional behavior, and shared
interfaces. Each required field has provenance; missing closure blocks handoff.
No solution patch or hidden verifier enters production compilation.

### 3. Connect retained-intent gates on the selected production routes

Compile applicable verification and invariant obligations using existing service
models. Require independent, candidate-bound results for implementation,
preservation, scope, and verifier changes. Reuse story receipt freshness and
verification-health reporting where applicable. Keep partial item success distinct
from complete story and feature acceptance.

Acceptance: stale, skipped, absent, weakened, duplicate, or unrelated evidence
cannot authorize completion. A passing test command cannot discharge an unlinked
obligation. A body-only regression fails even when source placement checks pass.
Approved intent amendments invalidate affected evidence and retain history.

### 4. Finish bounded execution and delivery migration

For the protected route selected in milestone 1, connect the accepted closure
and gates to the prepared worker boundary. Require one immutable prompt and one
coding invocation; failures return findings and require a new reviewed run where
appropriate. Remove unsupported legacy code-writing routes rather than migrating
them. Exclude runtime/planning artifacts from product patches and retain
attributable progress, validation, reconciliation, and delivery receipts.

Acceptance: timeout, policy denial, no progress, failed verification, sandbox
failure, input mutation, and candidate mutation prevent delivery. Pre-existing
changes cannot satisfy worker progress. Final candidate identity matches every
accepted receipt. Retry and duplicate-run fixtures prove invocation ownership.

### 5. Demonstrate the complete production path

Use an isolated repository fixture with accepted instructions, retained behavior,
a public integration boundary, a planted regression, and an approved amendment.
Exercise actual orchestration and adapters with deterministic provider fixtures.
Verify partial success, rejection, permitted implementation repair in a new run,
and evidence refresh after amendment. Exercise the same selected contracts from
normal and benchmark wrappers.

Acceptance: the route matrix and evidence inventory account for every mandatory
gate, and the planted defect is rejected despite focused unit tests passing.
Optional live-provider experiments report quality and cost separately. Hidden
benchmark scores and task-specific solve-rate thresholds are not product
completion criteria.

## Verification and completion

Each milestone needs meaningful regression fixtures at the changed boundary,
full repository checks required by AGENTS.md, and updated route/evidence reports.
Use the shared environment and current worktree source. Completion requires all
five milestones accepted, all selected production routes connected, and no
mandatory obligation satisfied by an unsupported or stale claim. Until then,
this consolidated plan remains proposed with implemented foundations.
