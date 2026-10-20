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
source_document: docs/plans/feature-proposal.md
items:
  - id: foundational-change
    title: Foundational change
    objective: Add the reusable foundation.
    scope:
      - Parse the reviewed configuration contract.
      - Report stable rejection codes for malformed input.
    allowed_paths: [src/foundation, tests/test_foundation.py]
    implementation_steps:
      - Implement the parser for the reviewed configuration contract.
      - Add focused tests for valid input and malformed fields.
    acceptance_criteria: [Foundation behavior is covered by focused tests.]
    dependencies: []
    workflow_file: docs/workflows/feature-key/foundational-change.yaml
    workflow_id: foundational-change
```

Every item must have a unique stable ID, objective, scope, allowed paths,
acceptance criteria, and an existing workflow definition. `scope` describes
the product behavior in prose. `allowed_paths` is a list of repository-relative
filesystem paths that bounds the worker's edits; use `["."]` when the plan
does not identify narrower paths. Dependencies must name items in the same
plan and form an acyclic graph. Publish only after the plan review PR merges;
the publisher pins each issue to that merge commit.

`powdrr-lift plan` analyzes a Markdown proposal and writes a candidate document
using this schema. Each item also carries a bounded `implementation_steps`
list (up to eight concrete code or test changes) so the worker receives the
reviewed execution outline instead of reconstructing the intended slice from
the objective alone. The generator targets at most five scope entries and
twenty allowed paths per item and splits broad work into dependent,
independently verifiable PRs. The
top-level `source_document` field records the
repository-relative path to the Markdown proposal, keeping the generated plan
discoverably linked to its source. For every item, it writes one small workflow definition at
`docs/workflows/<plan-id>/<workflow-id>.yaml`. The definition names the packaged
`execute-proposed-pr-workflow` Procedrr process and records its complete input
object, including the proposed PR ID, reviewed item context, and verification
command. Each workflow definition also lists prerequisite
workflow IDs in its top-level `dependencies` field. The generator derives that
list from the plan item's dependencies, and the worker verifies the two lists
match before executing the workflow. The reviewed plan remains the source for
the item's objective, scope, acceptance criteria, and dependency graph. The
worker reads the definition and materializes a durable process-backed task when
it claims the issue. The worker resolves this process from Powdrr-Lift's bundled
process catalog; the target repository does not need to copy runtime process
definitions into its own `docs/procedrr/processes/`. At task execution time,
Powdrr-Lift also loads the target repository's process catalog for
repository-specific nested processes. Reviewers can revise the plan
decomposition and process inputs in the PR. The command creates a unique
branch, commits only the generated plan and workflow definitions, pushes it,
and opens a pull request against the current branch. Other staged changes are
excluded from the plan commit. The command does not create GitHub issues or
execute work, and it refuses to overwrite existing generated artifacts.

For example, the integration workflow names its prerequisite explicitly:

```yaml
schema: https://powdrr.io/schemas/plan-workflow-instance-v2
workflow_id: local-validation-attestation-v1-pr-02
process_id: execute-proposed-pr-workflow
dependencies:
  - local-validation-attestation-v1-pr-01
inputs:
  work_item_name: Local validation attestation
  proposed_pr_id: local-validation-attestation-v1-pr-02
  feature_description: Add an end-to-end validation attestation.
  proposed_pr_context:
    plan_id: local-validation-attestation-v1
    item_id: local-validation-attestation-v1-pr-02
    title: Add end-to-end attestation
    objective: Add an end-to-end validation attestation.
    scope: [Verify the signed validation envelope and reject invalid evidence.]
    allowed_paths: [attestation/verifier, attestation/tests]
    implementation_steps:
      - Implement the verifier behavior for the reviewed envelope.
      - Add focused valid and invalid envelope tests.
    acceptance_criteria: [The attestation verifies all required checks.]
    dependencies: [local-validation-attestation-v1-pr-01]
    verification_command: bash attestation/scripts/verify-all.sh
```

The process input includes the repository validation command and reviewed plan
item. Its implementation planner maps each implementation step to concrete
repository-relative files and expected results. The implementation fragment
must carry out those mapped actions, and its completeness check accounts for
every reviewed step. The task receives that context from the pinned plan; it
does not rediscover or reinterpret the proposal.

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

## Structrr and intent checks across PRs

Plans generated by `powdrr-lift plan` include a validated Structrr bootstrap and
one immutable proposal artifact. The proposal contains the complete expected
structural change for the plan and deterministic per-item diffs. Each item's
expected baseline includes the projected effects of earlier plan items.

Before an item starts coding, the worker bootstraps the latest merged checkout.
It rebases the item's proposal onto that snapshot. Unrelated changes from other
features carry forward. A change to a referenced entity, relationship endpoint,
source subject, active decision, or invariant blocks coding and requires plan
review. The worker sends the current active intent and decisions to the coding
agent as preservation context.

After coding and validation, the worker bootstraps the candidate and compares
the observed structural diff with the rebased item proposal. A mismatch creates
a focused repair task in the existing coding-agent session; the proposal stays
unchanged. The worker repeats bootstrap, comparison, and validation after each
repair. Candidate changes to an accepted decision or invariant also require
repair. A task cannot pass while bootstrap or comparison evidence is unknown.

Generated plans end with a final integration review item that depends on every
implementation item. It bootstraps the latest integrated checkout, checks the
full plan proposal, runs the declared verification command, and repairs gaps
before it can pass. It publishes a final Structrr comparison and validation
receipt with its review PR. This final check uses the latest repository
snapshot as its baseline, so unrelated work already merged is preserved and
does not count as plan drift.

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

When a workflow task finishes, the worker first commits and pushes its workflow
state, then appends a `Powdrr task checkpoint` comment to that item's issue. The
comment contains the plan, item, workflow, and task IDs; the declared output
state type and value; and the commit SHA containing the persisted workflow
state. These comments form a task-by-task history alongside the issue's queue
labels and claim/final-status comments. They are scoped to one issue; plan-level
dependencies continue to determine when an item becomes eligible to run.

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
