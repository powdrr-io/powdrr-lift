# GitHub issue plan worker

## Ownership

The reviewed plan document owns the meaning of each work item: its objective,
scope, acceptance criteria, dependencies, and workflow reference. A merged plan
revision is immutable for the lifetime of its queue items. Give a revised plan a
new `plan_id` and new item IDs for work whose meaning changed.

GitHub Issues own mutable execution state. Their title is a stable ID, and their
body contains only a machine-readable pointer to the plan ID, item ID, document
path, and plan commit. Labels record `ready`, `claimed`, `awaiting-review`, or
`blocked`; closing an issue records that its work has been accepted and merged.
Do not copy objective or acceptance text into issue bodies.

## Plan document contract

```yaml
schema: https://powdrr.io/schemas/github-issue-execution-plan-v1
plan_id: feature-key-v1
items:
  - id: foundational-change
    title: Foundational change
    objective: Add the reusable foundation.
    scope: [src/foundation, tests/test_foundation.py]
    acceptance_criteria: [Foundation behavior is covered by focused tests.]
    dependencies: []
    workflow_directory: docs/workflows/feature-key/foundational-change
    workflow_id: foundational-change
```

Every item must have a unique stable ID, objective, scope, acceptance criteria,
and an existing durable workflow. Dependencies must name items in the same
plan and form an acyclic graph. Publish only after the plan review PR merges;
the publisher pins each issue to that merge commit.

## First worker boundary

`powdrr-lift publish-plan-issues` idempotently creates one issue per item. It
does not rewrite an existing issue's tracking state. `powdrr-lift
github-issue-worker` selects the lowest-numbered open `ready` item whose
dependencies are closed, marks it `claimed`, and invokes the existing
`process-workflow-task` runtime for the workflow named by the pinned plan.

A successful execution moves the issue to `awaiting-review`; the worker does
not close it or merge a pull request. A failed execution moves it to `blocked`
and records a short failure comment. A human may return a blocked item to
`ready` after resolving the cause. The initial worker is a single process per
repository on one host; a lock in Git's shared directory serializes its
worktrees. A
multi-host worker deployment needs a shared claim/lease protocol before it is
enabled. If a worker stops while an issue is `claimed`, first verify that the
worker process is gone, then replace `powdrr:claimed` with `powdrr:ready` to
retry it.

## Operations

1. Submit the plan document in a review PR. Use inline review comments to revise
   the decomposition and dependencies, then merge the approved plan.
2. From the merged checkout, publish issues with the repository name, plan path,
   and merge commit. Repeating the command reuses issues by stable plan/item ID.
3. Run one worker item at a time. Issues with unmet dependencies remain ready
   and are skipped. Close an issue only after its implementation PR is merged.
4. To retry blocked work, resolve the issue and replace its `powdrr:blocked`
   label with `powdrr:ready`.

```sh
powdrr-lift publish-plan-issues \
  --repository owner/repository \
  --plan docs/plans/feature-execution-plan.yaml \
  --plan-commit <merged-plan-commit>

powdrr-lift github-issue-worker \
  --repository owner/repository \
  --repo-root .
```
