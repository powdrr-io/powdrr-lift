# GitHub feature visibility and agent-independent execution protocol

Status: proposed; implementation has not started.

Audited baseline: `bef24da7` on `origin/main`, 2026-11-17. Recheck the named
files against current main before implementing each milestone. This document
specifies new work; existing mechanisms listed below are foundations to extend.

## 1. Product outcome and mandatory boundaries

Add a Features view to the existing Powdrr agent UI. It shows the selected target
repository's feature inventory, planning progress, implementation progress, and
each associated PR's execution, checks, review, and merge status. Work performed
by another agent on another machine appears without creating a local Powdrr job.

GitHub owns the shared record. Define a public, versioned protocol that an agent
can implement using GitHub APIs or `gh`, without installing Powdrr or calling a
Powdrr service. Powdrr is one producer and consumer of that protocol. The browser
uses the existing local API for credentials, retrieval, and disposable caching.
No hosted Powdrr database, webhook receiver, or always-running coordinator is
required. Closing every Powdrr UI must not stop remote agents or lose state.

Keep the existing Conversations and Jobs views. Add Features in the same shell,
repository selector, session, theme, and navigation. Do not build a separate app.
Local conversations, unpublished drafts, active processes, and private execution
checkpoints remain local. Shared summaries must say when a checkpoint exists
only on its originating machine; shared visibility does not imply remote resume.

### Explicit non-goals for this plan

- Replace the planner, issue queue, worker, verification, or fulfillment rules.
- Build a GitHub Projects dependency, a hosted GitHub App, or a new chat service.
- Infer accepted features or fulfillment from a model's reading of source code.
- Allow UI users to start, cancel, or resume arbitrary remote agents. V1 is a
  visibility view with native GitHub links; existing local job controls remain.
- Store prompts, secrets, private verifier content, or unbounded logs remotely.
- Add compatibility infrastructure for obsolete state formats. Current supported
  queue records receive a direct adapter; new protocol records have one version.

## 2. Existing implementation and extension points

| Existing code | What exists / what to extend |
| --- | --- |
| `ui/src/App.tsx`, `App.css`, `index.css` | Repository selector, chat and job navigation; add a repository Features route here. |
| `ui/src/api/client.ts`, `types.ts` | Existing authenticated browser API client and workspace records; add separate feature projection DTOs. |
| `ui/src/components/JobStatus.tsx`, `JobTimeline.tsx`, `ArtifactLinks.tsx` | Reuse presentation where applicable without pretending remote attempts are local jobs. |
| `packages/contracts/src/powdrr_contracts/workspace.py` | Local job/conversation contracts; preserve their ownership and semantics. |
| `packages/local/src/powdrr_local/workspace.py` | SQLite local workspace and event store; shared feature state must not depend on these rows. |
| `packages/local/src/powdrr_local/workspace_api.py`, `web.py` | Existing session, repository authorization, API composition; add read endpoints. |
| `src/powdrr_lift/workrr/workspace_app.py` | Composition root for application adapters; inject GitHub reader here. |
| `src/powdrr_lift/workrr/github_issue_queue.py` | Current issue client, pinned plan pointers, labels, execution reports, fulfillment, local claims and recovery; extend rather than fork the queue. |
| `src/powdrr_lift/workrr/github_issue_plan_generation.py` | Planning publication; add feature links and reports for published planning activity. |
| `packages/service/src/powdrr_service/planning/plan_models.py`, `plan_document.py` | Reviewed plan schema; retain current plan/item identities and add optional feature association through an explicit schema change if needed. |
| `src/powdrr_lift/workrr/run_artifacts.py` | Existing execution outcomes/evidence; map these into public reports. |
| `packages/agent/src/powdrr_agent/execution/github_actions.py` | Existing GitHub CI transport; reuse transport and interpretation where appropriate. |
| `docs/plans/github-issue-plan-worker.md`, `browser-workspace.md`, `reliable-plan-execution.md` | Existing contracts and responsibilities; this plan adds shared discovery and remote coordination, not a competing workflow. |

Current queue status labels include ready, claimed, awaiting-review,
awaiting-human, awaiting-ci, blocked-on-dependency, and blocked. Current claims
use local file locks and heartbeat files; they do not establish ownership across
machines. Current fulfillment evidence is stronger than issue closure. Preserve
that distinction in every aggregate and dependency decision.

## 3. Identity and relationship model

The graph is repository -> feature -> plan revision -> item -> attempts and PRs.
An item may have several attempts and PRs. A PR may implement several explicitly
listed items. A feature may have several accepted historical plans and one
current change plan. An implemented feature can have a new plan under review.

Use these keys:

- Repository: GitHub repository node ID, plus current owner/name and host for
  display and API addressing. Local path and origin alias are not identities.
- Feature: caller-created UUID stored as `feature_id`; a readable slug is a
  separate editable field. Never derive identity from a title or path.
- Plan: retain existing `plan_id`; pair it with its accepted commit as the exact
  revision. Preserve the current rule requiring a new plan/item identity when
  accepted work changes meaning. Link revisions with `supersedes`.
- Item: `(plan_id, item_id)` within repository identity.
- Attempt: UUID allocated before execution; include `supersedes_attempt_id` when
  retrying, and retain failed/cancelled attempts rather than overwriting them.
- Event: UUID plus attempt-local increasing sequence and the committing Git
  object identity. Event creation is idempotent by UUID.
- PR and issue: repository node ID and GitHub node ID; number/URL are references.

Hashes bind reviewed content, candidates, and evidence. They are never the
identity of a feature, item, or attempt. Editing unrelated files must not break
associations. Renaming a feature or moving its specification keeps its ID.

## 4. What lives where

### Default branch: reviewed repository configuration and feature manifests

Proposed target-repository files:

```text
.powdrr/repository.yaml
.powdrr/features/<feature-id>.yaml
<existing reviewed plan/workflow/specification locations>
```

These paths are new conventions. Do not move existing execution plans to a new
location. A manifest references their actual reviewed paths.

```yaml
# .powdrr/repository.yaml
schema_version: 1
protocol: github-feature-protocol
feature_directory: .powdrr/features
state_branch: powdrr-state
feature_issue_label: powdrr:feature
```

```yaml
# .powdrr/features/<feature-id>.yaml
schema_version: 1
feature_id: 27d4c907-f3d0-40db-9dd5-c8a2cd24f577
slug: repository-visibility
title: Repository visibility
tracking_issue: 420
specification_refs:
  - path: docs/specifications/repository-visibility.md
plan_refs:
  - plan_id: repository-visibility-v1
    path: docs/plans/repository-visibility-execution-plan.yaml
    role: current
lifecycle: active
```

`lifecycle` is active or retired; it does not claim implementation completion.
Manifest changes require normal repository PR review. Plan acceptance is derived
from the pinned reviewed revision merged into the repository's default branch,
not merely a manifest entry or label. Resolve the default branch through GitHub;
never hardcode main in target-repository behavior.

Before a manifest merges, a tracking issue labeled `powdrr:feature` can register
an idea through a versioned metadata block containing the same feature UUID,
slug and title. Treat it as provisional, not accepted. After merge the manifest
wins for reviewed fields. Conflicting UUIDs or multiple tracking issues produce
a visible diagnostic; never silently merge records using similar names.

### GitHub Issues and PRs: native work and discussion

A feature tracking issue is the human entry point. Existing queue issues remain
implementation items, with their current pinned plan pointers. Mirror item
membership as native sub-issues when permissions and API support allow it; the
explicit protocol association is authoritative and must work without sub-issues.
Native dependency links mirror reviewed plan dependencies; they cannot change
accepted execution obligations. Preserve ordinary human discussion in bodies.

Add a strict metadata block to associated PR descriptions, separate from the
existing required PR sections. The block includes schema version, feature IDs,
plan/item keys, attempt ID, and publication ID. It contains no status claims.
Read explicit associations; do not match by branch name or title. Support
multiple items/features per PR with arrays. Validate all referenced IDs.

Use exact start/end markers (`<!-- github-feature:v1 -->` and
`<!-- /github-feature:v1 -->`) enclosing one fenced JSON object for new feature
and PR metadata. Reject duplicate or incomplete blocks. Existing queue issue
markers remain interpreted by their existing parser; do not rewrite issue
bodies or require two competing item pointers.

### Dedicated state branch: durable journal, reports, and coordination

The `powdrr-state` branch is an independent Git history containing only protocol
records. It must not be merged into product branches or run product CI. Creation
is an explicit initialization command, never a side effect of opening the UI.

```text
protocol.json
features/<feature-id>/planning/<attempt-id>.json
items/<encoded-item-key>/claim.json
attempts/<attempt-id>/summary.json
events/<attempt-id>/<sequence>-<event-id>.json
receipts/<receipt-id>.json
publications/<publication-id>.json
```

Specify an unambiguous item-key encoding in the protocol package: URL-safe base64
without padding of the UTF-8 JSON array `[plan_id,item_id]`. Validate decoded
values; never concatenate caller strings into file paths. Repository identity
lives in `protocol.json` and every report. Reject records for a different repo.

Each transition creates an immutable event and updates its summary in one Git
commit. Summary fields include the latest sequence; readers validate the event
binding. Events remain append-only by protocol; GitHub writers can still edit
history, so provenance records include the commit and actual authenticated API
actor. Do not describe this as cryptographic attestation or tamper-proof storage.
No automatic compaction in v1; add growth metrics and document a later archival
threshold without deleting evidence needed by active plans.

Issue comments and labels are human-readable projections. Failure to mirror them
does not roll back a committed report. Mark projection work pending and retry
idempotently. Do not maintain independent authoritative status in comments.

## 5. Public wire contracts

Publish JSON Schemas under `docs/protocols/github-feature-v1/schemas/`, together
with valid/invalid examples and a normative README. Make them accessible as
ordinary repository files; no installed Python package is needed to participate.
Use `additionalProperties: false`, bounded strings/lists, UTC RFC3339 timestamps,
explicit enums, and a documented maximum record size (64 KiB for v1). Reject
unknown versions with `unsupported_protocol_version`; do not guess a conversion.

Add Python wire types in `packages/contracts/src/powdrr_contracts/features.py`.
These types must not import application/local/service packages. Implement shared
validation and deterministic projection in proposed
`packages/service/src/powdrr_service/features/`. Put GitHub IO in agent transport
modules and target-repository composition in Workrr; local API code depends on
injected reader interfaces, never imports `powdrr_lift`.

An execution summary has these required fields:

```json
{
  "schema_version": 1,
  "repository_id": "R_target",
  "feature_id": "27d4c907-f3d0-40db-9dd5-c8a2cd24f577",
  "plan_id": "repository-visibility-v1",
  "item_id": "inventory",
  "attempt_id": "53819e35-7944-41ca-a6cc-7c1b91021396",
  "sequence": 3,
  "agent": {"name": "external-agent", "instance_id": "worker-17"},
  "claim_generation": 2,
  "stage": "validating",
  "outcome": "running",
  "observed_at": "2026-11-17T15:00:00Z",
  "reason_code": "validation_in_progress",
  "message": "Running repository verification.",
  "next_action": "Await verification result.",
  "pull_request_numbers": [421],
  "candidate_commit": "0123456789abcdef0123456789abcdef01234567",
  "evidence_refs": [],
  "recovery": {"availability": "origin_machine_only", "reference": null}
}
```

Separate sequence, stage, and outcome. Stages: preparing, coding, validating,
publishing, reviewing, repairing, reconciling. Outcomes: running, pr_created,
awaiting_ci, awaiting_human, blocked, cancelled, failed, fulfilled_without_pr.
Reuse current typed terminal outcomes through explicit mappings. A successful
attempt producing a PR does not mean its item or feature is fulfilled.

Planning summaries use the same envelope/identity rules but bind to a feature
and planning attempt, with nullable plan identity until allocated. Planning
states: drafting, awaiting_decisions, in_review, accepted, needs_amendment,
failed, cancelled. Include plan PR and source specification references. Preserve
the reviewed acceptance check as the authority for accepted, not producer text.

Every event contains event_id, attempt_id, sequence, previous_sequence,
event_type, timestamp and the bounded payload. Allowed transitions and required
fields must be exhaustively tabulated in the normative protocol during M1.
Publishing the same event ID and identical content succeeds without a duplicate;
reusing it with different content fails. Out-of-order writes fail with the
current sequence. Wall clock timestamps do not decide which event wins.

Evidence references distinguish Git commit paths, issue comments, check runs,
workflow artifacts and local-only records. Local-only paths must not render as
remote download links. Persist compact fulfillment receipts, accepted revision,
candidate identity, required obligation results and source references on the
state branch. Existing fulfillment validation decides whether a receipt proves
completion. Do not invent a second fulfillment policy.

## 6. Deterministic display and evidence precedence

The projection produces separate planning, execution, and delivery dimensions.

| Dimension | Values / rule |
| --- | --- |
| Planning | idea, drafting, awaiting_decisions, in_review, accepted, needs_amendment, failed, cancelled, unknown |
| Execution | not_started, ready, running, awaiting_ci, awaiting_human, awaiting_review, blocked, failed, cancelled, complete, unknown |
| Delivery | unimplemented, partially_implemented, implemented, retired, unknown |

A feature can be implemented while its next plan is drafting. Compute delivery
against explicitly identified accepted delivery scope, and show current change
plan progress separately. Register existing features with a reviewed baseline
receipt recording implemented scope and evidence. A manifest alone never proves
implementation. No active delivery scope and no baseline means unknown.

For each item, evaluate accepted fulfillment first, then the currently valid
attempt/report, then current queue facts. Closed issue without fulfillment stays
unverified; closed PR without merge does not complete anything. Valid accepted
no-op fulfillment can complete an item without a PR. Cancelled and superseded
items are excluded from the denominator only by reviewed scope changes, never
by closing their issues. Show counts, not misleading weighted percentages.

For aggregate execution: all accepted required items fulfilled -> complete;
otherwise active attempts -> running; otherwise blockers/failures -> blocked;
otherwise pending review -> awaiting_review; then awaiting_human, awaiting_ci,
ready, not_started. Include counts for every underlying state so priority never
hides a failed sibling. All remaining items cancelled -> cancelled. Missing or
contradictory authority -> unknown with diagnostics. Distinguish dependency
blocking from human setup, provider failures, permissions, and missing evidence.

For each PR, keep independent fields for lifecycle, draft, current head SHA,
required checks, review decision, mergeability, and reported agent stage. Load
checks/statuses for the current head, including non-Actions checks. Passing old
head checks are stale. No checks does not mean passing; unknown mergeability
stays unknown. Changes requested and draft status remain visible alongside CI.
The UI can offer a concise summary badge but must expose all underlying fields.

Native GitHub PR/review/check facts override contradictory agent claims for
those facts. Reports remain labeled reported activity. Verification receipts
remain labeled verified evidence. Labels are hints/indexes, not proof. Show
observation time and source links on details. A stale heartbeat changes activity
freshness to stale/unknown; it does not turn the last run into a failure.

## 7. Cross-machine claims and crash-safe publication

V1 defaults: renewal every 60 seconds; lease duration 10 minutes. These are
coordination settings, not a task/model timeout. A worker may run indefinitely
while it renews. Use GitHub server time when checking expiry; when time/ownership
cannot be established, stop authoritative publication and show unknown.

Claims contain item key, attempt ID, authenticated owner, generation,
acquired_at, renewed_at, expires_at, state, and allowed-path scope. Claim
acquisition/renewal/release and shared scope registry updates use the same state
branch transaction so conflicting item scopes are compared atomically. Reuse
current allowed-path overlap policy; do not build a second interpretation.

Implement each mutation with `createCommitOnBranch(expectedHeadOid=...)` or an
equivalent proven compare-and-swap. Read and validate current records, compute
changes, commit against the exact observed head. On conflict reread/re-evaluate;
never force push. Use bounded retries with jitter; exhaustion reports contention
and a retryable result. Initialization must handle two clients racing to create
the branch. Persist operation IDs to recover a response lost after commit.

Only the owning attempt/generation may renew or change its execution summary.
Takeover requires expired ownership and a new generation. Keep claim tombstones
so generations never reset. Check ownership before state publication, remote
branch push, PR creation/update, and fulfillment reporting. On lost ownership,
retain local diagnostics and stop further authoritative writes.

A GitHub claim cannot atomically fence an arbitrary PR API request. Use a
publication record with publication ID, item/attempt/generation, candidate SHA,
reserved/published state and PR reference. Reserve it before publishing; after
an ambiguous API response search for its exact PR metadata before retrying.
A takeover first reconciles outstanding publication reservations and any existing
PR. If it cannot determine the outcome, block for reconciliation rather than
create a competing PR. A nonconforming agent may still create a PR; display it
as external/unverified and never accept a stale generation as fulfillment.

Collaborators with repository write access can edit shared state, as they can
edit code. Define the supported threat model explicitly: this coordinates
cooperating authorized writers; it does not enforce isolation against malicious
repository writers. Reject unauthenticated/untrusted issue comment claims and
self-declared actor identities; use GitHub actor/permission observations.

## 8. Discovery, synchronization, and error behavior

1. Resolve host/repository identity from the explicitly registered repository.
   Handle SSH/HTTPS remotes and forks; require selection when the target remote
   is ambiguous. Do not automatically choose a fork's upstream repository.
2. Read config/manifests at a single observed default-branch SHA. Read summaries
   and receipts at a single observed state-branch SHA. Return both snapshot IDs.
3. Discover feature tracking issues, existing plan-scoped queue issues and PR
   associations; load native facts in pages. Existing queue records can appear
   under unassociated plans until explicitly linked to a feature.
4. Fetch bounded feature summaries first. Fetch attempts/events/check details on
   demand with cursors. Never load all historical comments/logs on every refresh.
5. Return per-source observation times and completeness. Multi-source GitHub
   reads are not one transaction; display pending reconciliation if they disagree.
6. Cache by repository identity, permission context, branch SHA and resource
   validators. Use conditional requests and incremental issue updates, plus full
   reconciliation to detect edits/deletions. Preserve pagination completeness.
7. Refresh an active visible view every 30 seconds by default, coalesce callers,
   pause when hidden, allow manual refresh, and obey GitHub Retry-After/backoff.
   Authentication failures, forbidden sources and rate limits get distinct UI
   states; never turn them into an empty feature inventory.

Proposed new local endpoints under the existing `/api/v1` root:

- `GET /repositories/{id}/features?cursor=...&limit=...&query=...`
- `GET /repositories/{id}/features/{feature_id}`
- `GET /repositories/{id}/features/{feature_id}/items?cursor=...`
- `GET /repositories/{id}/feature-attempts/{attempt_id}/events?cursor=...`
- `GET /repositories/{id}/feature-pull-requests/{number}`

List responses include records, next_cursor, source snapshot IDs, observed_at,
freshness, completeness and diagnostics. Bind cursors to query/snapshot context.
Reuse existing authorization and error envelope. A read may refresh a local
cache but must not write GitHub. Do not poll GitHub once per SSE subscriber.
An in-process coalesced reader is sufficient; background hosting is unnecessary.

## 9. Existing UI behavior to implement

Add `view=features` and `feature=<uuid>` URL state alongside current repository,
conversation and job navigation. Preserve browser back/forward, refresh, deep
links and repository switching. Add a Features navigation button in the existing
shell; do not bury it under the local running-jobs filter.

Feature list: title, planning state, execution state, delivery state, fulfilled /
required item counts, blocker summary, active agent count, last observation.
Filters: active/all/retired, planning state, execution state, search. State the
loaded coverage when a search is limited; prefer server-side paginated search.

Feature detail: specification links; plan revisions with current/superseded
labels; item table; dependency graph; attempts; linked PRs; evidence and diagnostic
panels. Selecting an item expands its dependencies, stage, owner, failure reason,
next action and all attempts. Render a useful dependency list before adding a
layout library; graph and table must convey the same relationships accessibly.

PR detail: native status dimensions, current head, checks and reviews, reported
stage and attempts, next action, evidence, and Open in GitHub. A lost agent and a
healthy PR are both visible. Do not expose remote start/retry/cancel buttons.
Link local jobs/conversations only through explicit stored associations; absence
of a local counterpart is normal. Keep local job controls in their existing view.

Include unassociated work with reason (no feature link, malformed metadata,
unknown plan) and a GitHub link. Provide clear not-configured, no-features,
loading, stale/offline, partial-permission and malformed-record states. Render
Markdown/URLs with existing sanitization; credentials stay in the local backend.

## 10. Ordered implementation milestones

Each milestone is one reviewable PR unless its explicit parts need splitting.
Update this table with PR/evidence links. Do not mark complete merely on PR open.
Dependent work begins after prerequisites merge. Preserve existing behavior
unless this plan explicitly changes its authority.

| Milestone | Depends on | Status |
| --- | --- | --- |
| M1 Protocol, schemas, examples, deterministic state rules | None | Proposed |
| M2 GitHub discovery transport and repository projection | M1 | Proposed |
| M3 Read-only Features view inside existing agent UI | M2 | Proposed |
| M4 Shared state journal and external-agent conformance | M1, M2 | Proposed |
| M5 Cross-machine claims and publication reconciliation | M4 | Proposed |
| M6 Powdrr producer integration and complete UI reconciliation | M3, M5 | Proposed |
| M7 End-to-end acceptance and operating documentation | M6 | Proposed |

### M1: make the protocol implementable without Powdrr

Files: new `docs/protocols/github-feature-v1/README.md`, `schemas/`, `examples/`;
new contracts `features.py`; new service feature validator/projector modules;
focused `tests/test_feature_protocol.py` and `test_feature_projection.py`.

Tasks: write normative schemas for config, feature registration/manifest, plan
association, planning/execution summary, event, claim, scope registry, receipt,
publication and PR metadata. Define required/null/optional fields, transitions,
conflict/error codes, size/path limits and evidence precedence. Specify exactly
which schema fields are producer declarations versus observed API facts.
Implement the pure projector and fixture corpus before transport or UI code.
Map existing plan, terminal outcome and fulfillment records; document each map.

Acceptance: valid examples pass, invalid ones fail with field-level diagnostics;
rename/move preserves identity; a feature with an implemented baseline and a new
draft plan renders both accurately; old checks and closed unfulfilled issues do
not complete work; multi-item PR and multiple attempts preserve relationships.
A reviewer can implement a producer from the README/examples alone.

### M2: read the target repository without local job rows

Files: proposed `packages/agent/src/powdrr_agent/execution/github_features.py` for
transport, service discovery/projector interfaces, local API reader/cache module,
`workspace_api.py`, Workrr composition, and `tests/test_feature_discovery.py`,
`test_workspace_features_api.py`. Reuse the existing issue client's supported
parsers through a shared package boundary; do not import application code into
service or local packages. Extract only genuinely shared transport/parsing.

Tasks: resolve repository identity and config; fetch manifests, current queue
records and native PR facts; implement page/cursor/cache/partial-error behavior;
expose endpoints from section 8. Missing state branch is supported before M4;
show unreported execution, not invented idle/success. Read existing supported
queue data directly; no migration of historical jobs is required.

Acceptance: an empty local workspace discovers a fixture repository's external
feature/issues/PRs; different local paths yield the same shared IDs. Test private
repo denial, missing config, repository rename, changed PR head, pagination,
malformed one-of-many records, rate limit and deletion reconciliation. Reading
and refreshing never creates issues, branches, comments or local jobs.

### M3: ship useful repository visibility in the existing UI

Files: existing App/API/style files; proposed `FeatureList.tsx`,
`FeatureDetail.tsx`, `FeaturePullRequest.tsx` and focused UI tests. Follow existing
component conventions; avoid a monolithic addition to App.tsx.

Tasks: implement section 9 routes/list/details using M2 data; navigation and
filtering; independent status dimensions; freshness/completeness; unassociated
work and GitHub links. Reuse the current shell and components where appropriate.
Render read-only execution reports when available, with unknown when absent.

Acceptance: external work appears with no local jobs; browser navigation and
repository switching work; a check failure and pending review remain visible
together; stale API data is labeled; keyboard/mobile use works; existing chat,
job creation, job actions and streaming tests still pass.

### M4: implement journal writes and an independent producer example

Files: extend protocol transport/service modules; add public raw `gh api` request
examples and a minimal standalone standard-library script under protocol
examples, which imports no Powdrr code. Add schema/transport conformance tests.
Optional Powdrr CLI reporting helpers are convenience adapters, never mandatory.

Tasks: explicit state-branch initialization; conditional Git commits; idempotent
journal transitions and summary updates; event sequencing; bounded report/receipt
storage; comment/label projections with retries. Protocol initialization reports
required permissions and repository/ruleset constraints. Do not bypass branch
protections; return actionable setup instructions if policy forbids state writes.
Accept external reports only for authorized publishers and valid relationships.
Until M5 adds leases, M4 reports are observations and cannot authorize execution
or claim exclusive ownership; expose that explicitly in summaries.

Acceptance: a standalone producer reports planning and execution visible through
M2/M3; duplicate write and response-loss replay create one event; conflicting
sequence or event content fails; concurrent independent writes retain both;
failed label/comment projection can be repaired without a duplicate event.

### M5: coordinate ownership across machines

Files: shared protocol claim/transaction modules, application queue adapter,
existing `_ItemClaim` integration, publication adapter and remote coordination
regressions. Inspect latest queue concurrency changes before editing; preserve
current dependency/scope policies.

Tasks: implement section 7 acquisition/renewal/expiry/release/takeover; atomic
scope registration; ownership checks and publication reservation/reconciliation.
Replace local-only ownership as authority for protocol-enabled work. Local OS
locks may protect one machine's files; they cannot substitute for remote claims.
No new task/model deadline is introduced. Stop/retain diagnostics on claim loss.

Acceptance: race two independent clients for one item (one valid owner), overlap
scopes (one allowed), disjoint scopes (both allowed), renew and take over expired
claims, reject old generation writes, recover after response loss and a PR API
ambiguity. Test unauthorized publishers and branch CAS contention. At least one
deterministic fixture must simulate an old worker reaching publication after a
takeover; the result is reconciliation, not accepted duplicate fulfillment.

### M6: have Powdrr use the same protocol as other agents

Files: planning publication, issue worker and supervisor application adapters,
run artifacts/reporting adapters, PR body composition, local-to-shared links and
feature refresh integration. Preserve existing strict PR templates.

Tasks: attach an explicit feature UUID to published plans/items; report planning
stages and execution transitions using current attempt IDs; publish bounded
public evidence/fulfillment receipts; link PR metadata and local job IDs. Publish
current stage on transition and heartbeat only while running. On restart replay
pending reporting idempotently before new work. Clearly mark unpublished local
planning and machine-local checkpoint recovery. Do not upload private state.

Acceptance: a Powdrr attempt and a standalone external attempt appear identically
in shared fields, distinguished by agent identity. A third workspace sees both.
GitHub PR changes refresh independently of producer reports. Reopening the UI
or recreating its cache reconstructs the same feature state. Existing queue
fulfillment, protected verification, cancellation and local recovery regressions
continue to pass; lack of remote recovery is shown honestly.

### M7: demonstrate and document the whole contract

Add `tests/test_feature_protocol_scenario.py` (or equivalent meaningful
integration suite), deployment/setup documentation and an acceptance report.
Use two distinct client instances and a third empty observer; merely changing an
agent-name field within one cached client is insufficient cross-client testing.

Scenario: register an existing implemented feature and a new feature; publish a
two-item dependent plan; accept the plan; have one agent claim item A while a
second loses that claim race; publish A's PR; change its head to invalidate old
checks; merge with valid evidence; unblock B; let another agent execute B; lose
its connection and take over/reconcile; complete valid fulfillment. Observe all
states from the third workspace. Add draft next-plan work to the implemented
feature and show delivery remains implemented while planning is drafting.

Repeat observer startup with its feature cache deleted, then with no local job
records. Test missing configuration, permission denial, rate limits, malformed
metadata, unsupported schema, unavailable evidence, superseded plans, duplicate
reports, human-closed issues, and unmerged closed PRs.

Use deterministic API fixtures in CI, then perform one authorized live run in a
disposable GitHub repository to validate actual CAS, permissions, PR association
and native API behavior. Record repository/commit/PR links and commands. Do not
write into unrelated repositories or claim live acceptance from mock tests.
If no authorized live repository exists, mark live acceptance pending explicitly.

## 11. Verification and delivery instructions

Start each implementation PR in a dedicated worktree from latest origin/main.
Use the shared environment; never create a new venv or run uv sync for checks.
Set PYTHONPATH to all current-worktree package sources, as below. Run focused
behavior checks first; before push complete repository-required verification.

```bash
export VIRTUAL_ENV=/Users/gregory/code/powdrr-lift/.venv
export PATH="$VIRTUAL_ENV/bin:$PATH"
export UV_PROJECT_ENVIRONMENT="$VIRTUAL_ENV"
export UV_NO_SYNC=1
export PYTHONPATH="$PWD/src:$PWD/packages/service/src:$PWD/packages/contracts/src:$PWD/packages/local/src:$PWD/packages/agent/src"
rtk proxy "$VIRTUAL_ENV/bin/python" -m ruff format --check .
rtk proxy "$VIRTUAL_ENV/bin/python" -m ruff check .
rtk proxy "$VIRTUAL_ENV/bin/python" -m mypy src tests
rtk proxy "$VIRTUAL_ENV/bin/python" -m powdrr_lift.cli validate-procedrr-processes
rtk proxy "$VIRTUAL_ENV/bin/python" scripts/verify-agent-distribution.py
rtk proxy "$VIRTUAL_ENV/bin/python" -m pytest -n auto --cov=powdrr_lift --cov=powdrr_service --cov=powdrr_contracts --cov=powdrr_local --cov=powdrr_agent --cov-branch --cov-report=term-missing tests
rtk proxy bash scripts/run-deterministic-workflow-suite.sh
rtk git diff --check
```

For UI changes run `rtk proxy npm --prefix ui test`, `typecheck`, `lint`, and
`build` with `run` before each script name. Follow current packaging instructions
for generated web assets; never hand-edit bundled JS/CSS. Validate new JSON
Schema examples against the exact committed schemas. Keep protocol conformance
fixtures accessible independently of Python UI types.

Open each PR against main; do not merge it. Include the milestone, user-visible
result, schema changes, actual checks/results and remaining limitations. Use the
repository changelog v2 process. Update package ownership inventory if new
modules require it. Keep unrelated cleanup out of these changes.

## 12. Completion checklist

- A documented GitHub-only protocol can be implemented using raw GitHub APIs.
- Existing features and future change plans have stable, separate identities.
- Native GitHub state and agent reports remain distinguishable in the UI.
- Another agent's work appears without a local job or running coordinator.
- Claims coordinate cooperating machines and stale workers cannot fulfill items.
- Essential evidence survives deletion/expiry of ephemeral workflow artifacts.
- Shared state is reconstructable after losing the UI cache and local job rows.
- Features is a view inside the existing agent UI, with existing workflows intact.
- Deterministic acceptance passes and the live acceptance report records actual
  results or names the outstanding blocker; do not mark this plan complete early.

## References

- [GitHub issue sub-issues](https://docs.github.com/en/rest/issues/sub-issues)
- [GitHub issue dependencies](https://docs.github.com/en/rest/issues/issue-dependencies)
- [GitHub commit mutation and expected head](https://docs.github.com/en/enterprise-cloud@latest/graphql/reference/commits)
- [Conditional requests and API usage](https://docs.github.com/en/rest/using-the-rest-api/best-practices-for-using-the-rest-api)
- [Actions artifact retention](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/remove-workflow-artifacts)
