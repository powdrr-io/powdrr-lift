# GitHub issue plan worker

## Ownership

The reviewed plan document owns the meaning of each work item: its objective,
scope, acceptance criteria, dependencies, and workflow reference. A merged plan
revision is immutable for the lifetime of its queue items. Give a revised plan a
new `plan_id` and new item IDs for work whose meaning changed.

GitHub Issues own mutable execution state. Their title is a stable ID, and their
body contains only a machine-readable pointer to the plan ID, item ID, document
path, and plan commit. Labels record `ready`, `blocked-on-dependency`, `claimed`,
`awaiting-review`, or `blocked`; closing an issue records that its work has been
accepted and merged. Do not copy objective or acceptance text into issue bodies.

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
    workflow_file: docs/workflows/feature-key/foundational-change.yaml
    workflow_id: foundational-change
```

Every item must have a unique stable ID, objective, scope, acceptance criteria,
and an existing workflow definition. Dependencies must name items in the same
plan and form an acyclic graph. Publish only after the plan review PR merges;
the publisher pins each issue to that merge commit.

`powdrr-lift plan` analyzes a Markdown proposal and writes a candidate document
using this schema. For every item, it writes one small workflow definition at
`docs/workflows/<plan-id>/<workflow-id>.yaml`. The definition names the packaged
`execute-proposed-pr` template and records its parameters, such as the proposed
PR ID and verification command. Each workflow definition also lists prerequisite
workflow IDs in its top-level `dependencies` field. The generator derives that
list from the plan item's dependencies, and the worker verifies the two lists
match before executing the workflow. The reviewed plan remains the source for
the item's objective, scope, acceptance criteria, and dependency graph. The
worker reads the definition and materializes the packaged task workflow when it
claims the issue. Reviewers can revise the plan decomposition and template
parameters in the PR. The command creates a unique branch, commits only the
generated plan and workflow definitions, pushes it, and opens a pull request
against the current branch. Other staged changes are excluded from the plan
commit. The command does not create GitHub issues or execute work, and it
refuses to overwrite existing generated artifacts.

For example, the integration workflow names its prerequisite explicitly:

```yaml
schema: https://powdrr.io/schemas/plan-workflow-instance-v1
workflow_id: local-validation-attestation-v1-pr-02
template: execute-proposed-pr
dependencies:
  - local-validation-attestation-v1-pr-01
parameters:
  proposed-pr-id: local-validation-attestation-v1-pr-02
  verification-command: bash attestation/scripts/verify-all.sh
```

The caller supplies the repository validation command because generated PR
workflows use it as their implementation-loop gate. The workflow's first task
receives the structured plan item from the pinned plan; it does not rediscover or
reinterpret the proposal.

```sh
powdrr-lift plan \
  --source docs/plans/feature-proposal.md \
  --plan-id feature-key-v1 \
  --verification-command "make test" \
  --output docs/plans/feature-key-execution-plan.yaml
```

The command prints the plan review PR URL and returns it in JSON output. Review
the plan, item context, and generated workflows in that PR; merge it before
publishing the plan items as GitHub issues.

## First worker boundary

`powdrr-lift publish-plan-issues` idempotently creates one issue per item. Items
with no dependencies start as `powdrr:ready`; items with dependencies start as
`powdrr:blocked-on-dependency`. Therefore, a plan with one root item has exactly
one ready issue when it is first published. The publisher also reconciles these
two dependency-derived labels when it is rerun, while preserving execution
states such as `claimed`, `awaiting-review`, and `blocked`. Each item issue
carries a deterministic plan label as an index. The worker asks GitHub for open
issues with `powdrr:ready` and `powdrr:blocked-on-dependency` labels, then loads
dependencies from the plan-scoped issue set. These filtered requests avoid
scanning unrelated repository issues. The body pointer remains the source of
truth and is checked after GitHub returns the indexed candidates.
`powdrr-lift github-issue-worker` promotes dependency-blocked issues to ready
when all prerequisite issues are closed, then selects the lowest-numbered
eligible item, marks it `claimed`, and invokes the existing
`process-workflow-task` runtime for the workflow named by the pinned plan.

`powdrr-lift list-plan-issues` reads the pinned plan and GitHub issue pointers,
then prints one result per plan item with its issue number, URL, state, and
labels. Items without a published issue appear in `missing_item_ids`; the
lookup does not create issues or change labels.

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
3. Run one worker item at a time. Issues with unmet dependencies remain
   `powdrr:blocked-on-dependency` until the worker observes that their
   prerequisite issues are closed. Close an issue only after its implementation
   PR is merged.
4. To retry blocked work, resolve the issue and replace its `powdrr:blocked`
   label with `powdrr:ready`.

```sh
powdrr-lift publish-plan-issues \
  --repository owner/repository \
  --plan docs/plans/feature-execution-plan.yaml \
  --plan-commit <merged-plan-commit>

powdrr-lift list-plan-issues \
  --repository owner/repository \
  --plan docs/plans/feature-execution-plan.yaml \
  --plan-commit <merged-plan-commit>

powdrr-lift github-issue-worker \
  --repository owner/repository \
  --repo-root .
```
