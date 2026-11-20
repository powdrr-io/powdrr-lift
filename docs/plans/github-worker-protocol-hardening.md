# GitHub worker protocol hardening

Status: proposed implementation plan.

## Outcome

The issue worker agent owns consistency for every work item it accepts, across
its own attempts, GitHub issues, branches, pull requests, and CI. A worker can
exit while GitHub retains an effect of that attempt. The next responsible agent
inherits the same obligation. An inconsistent item stays in the agent's work
inventory until it converges or reaches a documented authority boundary.

## Audit

Source reviewed at Lift commit
[`3c91ebf5`](https://github.com/powdrr-io/powdrr-lift/commit/3c91ebf50a00be5f86c2488ed5e74e0224053f6b).
The audit covers issue selection and claiming, task handoff, PR publication,
CI continuation, plan amendments, and dependency fulfillment.

| Boundary | Current behavior and exposure |
| --- | --- |
| Claim and abandonment | `run_worker_once` starts with a scan of open issues labeled claimed. `_mark_abandoned_claims_for_reconciliation` fences a locally abandoned claim, marks the issue blocked, and asks someone to reconcile and relabel it. A local claim and a GitHub label can each survive without the other. The locks coordinate processes sharing a Git common directory. |
| Publication and linkage | The task agent injects an issue closing reference, and clean PR finalization records an issue comment and attaches the PR changelog. Branch publication, PR creation, issue linkage, and changelog finalization have independent failure boundaries. An error returned to the caller can coexist with a live PR. |
| Workflow and CI handoff | `run_worker_once` enters CI observation on a zero task exit code. Its nonzero path reports implementation failure even if the task already published. CI continuation can use the publication marker when the completed checkpoint is missing, but requires an open issue with a qualifying label. The label can prevent discovery of the very work needed to correct it. |
| Checkpoint and queue projection | The task callback writes a local checkpoint, then writes an issue comment. CI observation, queue labels, and diagnostic comments also have separate writes. Updating labels from an observed label set risks overwriting intervening edits. The latest comment or local file can lag remote state. |
| Dependency fulfillment | The normal completed checkpoint path checks the input contract digest. The merged publication fallback accepts a same-repository merged PR with a closing reference and a merge SHA on a closed issue. That fallback does not bind the PR to the current input contract or verify that its merge is in the intended integration branch. An issue that remains open can hold back a dependency despite a merge. |
| Plan amendment and cleanup | Amendment reconciliation can update several issues, pointers, and dependency states independently. Closed-issue cleanup runs before the worker's reconciliation scans. Closure alone is too weak a signal for disposing of artifacts while an outcome or affected plan revision remains unresolved. |

The Gaterr incident demonstrates the publication boundary. Issue #909 first
recorded the link to PR #1037 and then reported implementation failure. A later
comment reported that CI response failed after publication while normalizing
an empty base field. GitHub records the PR as merged into `main`. A separate
issue comment supplied a completion checkpoint after the merge. This sequence
shows why a failure record describes the attempt, while publication and
fulfillment describe the work item.
[Publication link](https://github.com/powdrr-io/powdrr-gaterr/issues/909#issuecomment-6939499785),
[worker diagnostic](https://github.com/powdrr-io/powdrr-gaterr/issues/909#issuecomment-6939501871),
[incident follow-up](https://github.com/powdrr-io/powdrr-gaterr/issues/909#issuecomment-6946732328),
[merged PR](https://github.com/powdrr-io/powdrr-gaterr/pull/1037),
[completion checkpoint](https://github.com/powdrr-io/powdrr-gaterr/issues/909#issuecomment-6948206096).

The clean publication link, CI continuation fallback, and merged dependency
fallback already on main cover parts of this sequence. The remaining problem
is that each consumer interprets a different subset of the same state.

## Agent responsibility contract

Put the following instruction in the issue worker's durable task contract.
Expose the same responsibility and available evidence on every invocation,
including continuation, unattended scans, and replacement of an expired owner.

> You own the consistency of this work item across the local attempt and
> GitHub. Expect interruptions, partial writes, stale state, concurrent edits,
> and successful remote operations whose response was lost. Determine what
> actually happened from current evidence. Correct every discrepancy within
> your authority and verify the resulting state. A failed attempt, a stale
> label, or a missing checkpoint is a condition you own. Preserve valid
> effects and reviewed intent as you make progress. Keep ownership while
> waiting on a legitimate external condition, and reassess when it changes.
> Request only the decision or capability that requires another authority.

### Durable ownership across restarts

The coordinator keeps an authoritative work-item record in the shared worker
store, keyed by repository, issue number, plan ID, item ID, and accepted input
contract digest. The record survives worker process replacement and contains
the current owner generation, lease/heartbeat data, last confirmed remote
observations, unresolved discrepancies, pending effect identities, linked PR
identities, and the evidence version used for the last decision. Local
worktrees and GitHub labels remain projections.

An owner generation fences mutations. A replacement agent takes ownership
only after confirming that the previous owner no longer holds the shared
coordination lock. It reads the same work-item record and refreshes GitHub
observations before acting. It can adopt verified effects from an earlier
generation, but cannot let that generation make further writes. A live owner
keeps the record while waiting on CI, review, access, or a human decision.
The agent remains accountable for re-evaluating the item when the waiting
condition changes. The record is retired only when the work item is verified
fulfilled or an authorized cancellation is recorded.

For the current single-host deployment, the record and fencing token are
stored atomically under the shared Git common directory and coordinated by
the existing process lock. Any future multi-host deployment must provide
shared atomic storage and fencing before it can own queue items. If the
record is corrupt, missing, or cannot be written, the agent reports that
specific capability failure and keeps the item discoverable. It does not
silently declare the item unowned or fulfilled.

### Milestone pass conditions

Every milestone must satisfy its listed conditions in automated tests and
produce evidence that later milestones can consume.

1. **Define authoritative observations and outcomes.** Pass when the versioned
   model distinguishes attempt result from item state, represents unknown
   remote outcomes and pending effects, and binds evidence to work identity,
   contract digest, PR identity, head, and check policy. Golden cases for the
   Gaterr #909 sequence, duplicate PR candidates, stale CI, and legacy
   unbound markers must yield one expected state each. Unknown or conflicting
   evidence must not produce fulfillment.
2. **Make reconciliation part of agent execution.** Pass when an unattended
   replacement agent acquires a higher owner generation, finds work with
   missing or stale queue labels, refreshes observations, and either
   converges the item or keeps a concrete unresolved condition in its durable
   record. Tests must prove an expired generation cannot mutate after
   replacement, a live generation is not stolen, and an identical subsequent
   scan makes no remote writes.
3. **Harden remote effects and local durability.** Pass when fault injection
   at each GitHub mutation boundary, including a lost success response,
   results in at most one logical issue, branch publication, PR, and
   checkpoint for the operation identity. A retry must discover and bind an
   existing effect before creating another. Compare-and-swap tests must show
   that unrelated human edits survive and stale worker writes are rejected.
4. **Apply the model to CI, amendments, and dependency readiness.** Pass when
   a dependency is released only for a verified merge of the accepted
   contract into the intended integration history or an authorized no-op.
   Tests must hold it for closed-but-unmerged PRs, old-contract merges, stale
   heads, uncertain publication, and multiple matching PRs. A partial plan
   amendment must retain its full impacted item set and prevent promotion
   until each affected item is reconciled.
5. **Prove convergence before retiring legacy paths.** Pass when observation
   mode reports old-versus-new decisions for the same evidence snapshot,
   rollout thresholds are met across a documented sample, and discrepancy
   metrics show duplicate effects, stale writes, unresolved duration, and
   unexplained decisions within agreed bounds. Remove a legacy path only
   after all its observed outcomes have an equivalent decision in the shared
   model and fault-injection coverage passes.

Human authority includes merging where review is required, changing accepted
intent, resolving a substantive choice between competing candidates, and
granting missing access. The agent owns diagnosis, evidence preservation,
consistent reporting, and follow-through around that boundary. A GitHub
comment that asks a person to fix worker bookkeeping does not satisfy the
contract. The coding subprocess remains scoped to implementation. The issue
worker agent acts through tools that enforce reviewed scope and permissions.

## Protocol requirements

| Fact | Authority and invariant |
| --- | --- |
| Work identity | Bind the repository, issue, plan and item IDs, accepted input contract digest, and publication purpose. Distinguish implementation and amendment PRs. Attempts and claim generations describe ownership, while a stable operation identity describes a logical effect across attempts. |
| Publication | GitHub PR identity, refs, and state establish remote publication. A durable publication association binds the PR to accepted work identity. A closing reference, URL in a comment, or familiar branch name is a discovery hint. Each item and contract revision has at most one current implementation PR. Multiple plausible PRs remain explicit ambiguity. |
| CI and review | CI evidence applies to the observed PR head and applicable check policy. A changed head or policy invalidates approval to advance based on old CI. Pending CI, pending human review, CI repair, and a failed observation each retain the publication identity. |
| Fulfillment | Dependency completion requires a verified current-contract implementation merge into the intended integration history, or an authorized, verified no-op receipt. A closed unmerged PR, human issue closure, or an old-contract merge never establishes fulfillment on its own. |
| Ownership and writes | Ownership survives process replacement. Stale owners cannot publish or change protocol state. Mutations converge under repetition or an unknown response, preserve concurrent human edits outside worker-owned fields, and carry verifiable preconditions. Maintain the existing single-host limit until ownership can be fenced across hosts. |
| Queue and evidence | Labels, comments, and worktree state are projections or evidence hints. Distinguish a confirmed fact, missing evidence, and a failed observation. A cached queue result cannot authorize a mutation or unlock a dependency after the evidence changes. |

## Implementation plan

1. **Define authoritative observations and outcomes.** Introduce one versioned
   work-item state model shared by publication, queue selection, CI response,
   plan amendment, and dependency evaluation. Keep attempt outcomes separate
   from item outcomes. Model uncertainty, pending effects, and intentional
   human edits explicitly. Define legacy evidence acceptance and require
   corroboration where a legacy marker lacks current-contract identity.
2. **Make reconciliation part of agent execution.** Give the top-level agent
   the responsibility contract, an evidence view across local and remote
   state, and scoped tools to correct it. Have the coordinator retain
   ownership of outstanding items across restarts and discover them even when
   the projection label is absent or wrong. Use a bounded, paginated inventory
   and reuse observations during a scan. Keep eligibility checks and
   diagnostics on the same state model.
3. **Harden remote effects and local durability.** Give issue creation, branch
   updates, PR publication and finalization, checkpoint writes, and queue
   changes stable effect identities and independently observable completion
   criteria. Treat transport timeouts as unknown outcomes. Ensure the agent
   can bind existing valid effects, and that stale attempts and duplicate
   deliveries cannot create new effects or overwrite newer state. Keep
   artifacts referenced by unresolved work available across attempts.
4. **Apply the model to CI, amendments, and dependency readiness.** Make all
   consumers use the same identity and evidence rules. Capture the plan
   revision and impact set for partially applied amendments. Gate promotion
   on verified fulfillment of the accepted dependency revision. Use the
   reviewed DAG to limit which dependents need re-evaluation when a verified
   fact changes. Preserve the separate authority to merge or amend intent.
5. **Prove convergence before retiring legacy paths.** Compare old queue
   projections against the new model in observation-only mode. Roll out
   agent-owned corrections within the existing single-host boundary and
   replace overlapping fallback interpretations once legacy inputs converge.
   Record discrepancy type, item identity, action outcome, time unresolved,
   duplicate effects, and stale writes rejected. Keep capability failures and
   human decisions visible as distinct waiting reasons.

## Acceptance criteria

- Fault injection covers interruption before and after each remote mutation,
  lost success responses, checkpoint write failures, stale labels, and
  replacement of a crashed worker. An unattended agent converges without
  manual relabeling, reconstructing checkpoints, or duplicate publication.
- Reproduce the Gaterr #909 shape with a live linked PR and a failed later
  task or CI response. The item retains that PR, reflects its current CI and
  review state, and releases dependents once the accepted merge is verified.
- Concurrent edits, competing attempts, an unexpectedly closed PR, branch
  changes, stale CI, and a partial amendment preserve human intent and never
  release a dependent on stale or ambiguous evidence.
- A subsequent scan is a no-op for a consistent item. When evidence or access
  is temporarily unavailable, the discrepancy stays discoverable with a
  concrete reason. When a human decision is required, the agent requests
  that decision and retains responsibility for the item afterward.
