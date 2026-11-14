# Reliable planning and execution: implementation handoff

Status: proposed; implementation has not started.

Audited baseline: `f7c0becf`. Recheck each finding against latest `main` before
implementing its task. This document specifies intended behavior; it does not
claim that the audit demonstrated every possible failure experimentally.

## Objective and completion criteria

Powdrr must turn accepted feature intent into a reviewed plan and execute that
plan to implementation PRs consistently, with useful recovery and measured cost.
Every attempted item must end with a linked PR, an accepted no-op, a durable
waiting state, or a specific reason why there is no PR. A generic failure message
does not satisfy this contract.

Complete this plan only when the deterministic scenario in task 10 passes and
the live Gaterr acceptance in task 11 has an evidence report. Passing unit tests
alone does not establish completion. Report blocked acceptance honestly.

## Instructions for the implementing agent

1. Read `AGENTS.md`, this document, and the named production code and tests for
   the next task. Fetch latest `main` before starting work or launching a worker.
2. Use a dedicated worktree and feature branch based on current `origin/main`.
   Use the shared Python environment specified in `AGENTS.md`.
3. Execute tasks in the order below. Each numbered task is one independently
   reviewable PR. Do not merge your PR. Continue dependent work only after its
   prerequisites are merged; independent work may proceed separately.
4. Do not introduce a second planner, worker, verifier, or queue. Extend the
   existing service contracts and Workrr adapters. Service code must not import
   the application package. Put shared wire contracts in the contracts package
   when both layers need them.
5. Preserve protected test authoring, sandboxing, immutable accepted intent,
   candidate-bound evidence, and source lineage. Recovery must preserve these
   properties. Do not restore unsupported legacy implementation paths.
6. Honor the existing small supporting edit policy at every scope gate, including
   its protected-file exclusions and line-count semantics. Up to 20 edited lines
   outside the primary allowed set may be eligible under that policy. Do not
   impose a 20-file limit. Reuse the canonical policy and its tests rather than
   implementing another interpretation.
7. Feature changes may invalidate old behavior tests. Identify and update those
   tests, explain the changed expectation, and add coverage for new behavior.
   Preserve independent accepted oracles; an implementation failure alone does
   not authorize weakening them.
8. Do not add wall-clock task or model timeouts as a progress policy. Preserve
   user cancellation. Detect unchanged failed state, exhausted explicit budgets,
   invalid contracts, and unrecoverable failures with durable evidence. Inventory
   existing timeout settings and remove implicit task/model deadlines in task 4;
   document separately any deliberately retained tool-level timeout.
9. Open all PRs against `main`. Generate changelog v2 diffs through the supported
   publication route when required; never rewrite a bootstrap baseline to
   represent an implementation diff. Do not hand-edit Gaterr to bypass Powdrr.
10. Update the task status table with PR links and actual acceptance evidence.
    When a prerequisite already landed, verify its behavior and add only missing
    work. In particular, inspect the strict PR template change associated with
    PR #1513 before modifying publication formatting.

## Task order and status

| Task | Deliverable | Depends on | Status |
| --- | --- | --- | --- |
| 1 | Shared terminal result and durable failure reporting | None | Pending |
| 2 | Faithful planning-to-worker contract compilation | None | Pending |
| 3 | Shared execution preparation and plan executability checks | 2 | Pending |
| 4 | Controlled implementation correction and restart | 1, 3 | Pending |
| 5 | Verified candidate publication and safe preparation repair | 1, 4 | Pending |
| 6 | Fulfillment-backed dependencies and readiness explanations | 1, 3 | Pending |
| 7 | Validation inventory and evidence reuse | 3, 5 | Pending |
| 8 | Complete run telemetry and mechanical-call removal | 1, 7 | Pending |
| 9 | Recoverable claims and independent item concurrency | 4, 6 | Pending |
| 10 | Full deterministic planning/execution/recovery scenario | 1–9 | Pending |
| 11 | Live Gaterr acceptance and measured report | 10 | Pending |

## 1. Return a typed result and publish exact failure evidence

Read `src/powdrr_lift/workrr/task_agent.py`, `github_issue_queue.py`,
`planned_workflow_task.py`, `run_artifacts.py`, `error_logging.py`, and existing
task/checkpoint models. Locate every runner return consumed by the issue worker,
including manual-prerequisite continuation.

Implement:

- Introduce one serializable item result with a schema version, outcome, stage,
  repository/plan/item/workflow/attempt identities, accepted contract digest,
  base commit, candidate tree when available, PR URL when available, and evidence
  references. Use explicit outcomes: `pr_created`, `fulfilled_without_pr`,
  `awaiting_ci`, `awaiting_human`, `blocked`, `cancelled`, and `failed`.
- A failure includes its classification, exception type/message or subprocess
  exit code, command when applicable, a bounded stdout/stderr summary, artifact
  references, whether correction is permitted, and the next action. Distinguish
  plan, implementation, policy, verification, environment, provider, schema, and
  publication failures from evidence; do not guess provider failure from text.
- Keep process exit codes at the CLI boundary. Adapt internal callers to the
  typed result. Do not use `0` alone as proof of PR creation or fulfillment.
- Atomically persist the terminal result before changing queue labels. Render a
  human-readable GitHub issue comment from it. Local absolute paths alone are
  insufficient remote evidence: include a safe diagnostic summary and retained
  artifact identity. Exclude credentials and private verifier content.
- On reporting failure, retain a pending report record. Restart retries the
  report idempotently before starting new implementation work.

Acceptance: regressions exercise subprocess failure, schema failure, unexpected
exception, publication failure, cancellation, missing PR after purported success,
and GitHub failure during reporting. Each produces a durable classified result
and a precise reason for no PR. Replaying a result creates no duplicate comment.

## 2. Preserve accepted obligations in the worker design

Read service `planning/source_reference_design.py`, `driver.py`, `compiler.py`,
`semantic_contract_compiler.py`, `multipr_structrr.py`, and
`structrr/source_reference_handoff.py`, `reference_catalog.py`, and the existing
contract-closure implementation. Start with `tests/test_planning_compiler.py`,
`test_source_reference_handoff.py`, and `test_contract_closure.py`.

The audited compiler synthesizes scenarios from test prose, sets all dimensions
to `not_applicable`, and emits empty placement/preservation lists. Replace that
lossy conversion with compilation of the accepted typed records:

- Carry accepted cases, scenarios, dimensions, source clauses, faithfulness
  provenance, placements, preservation obligations, and canonical reference
  definitions through the per-item proposal and source design.
- Reuse current contract types and fingerprint functions. Compute digests in
  code. Do not ask a model to invent provenance or claim source fidelity.
- Empty obligations are valid only when that obligation category genuinely does
  not apply. Reject an omitted applicable obligation with its source/item ID.
- Preserve identity across serialization and reject duplicate, dangling,
  contradictory, or mismatched case/scenario/reference IDs.
- Existing accepted plans that lack necessary authority must return an explicit
  amendment/replan requirement. Never fabricate missing authority at execution.

Acceptance: compile a real-shaped accepted proposal containing positive,
negative, conditional, error, optional-argument, and preservation behavior.
Assert exact obligations/provenance survive into the worker handoff and private
verification closure. Removing one applicable obligation fails before coding.
Include an explicitly valid item with no applicable placement obligations.

## 3. Check executability using the same preparation as execution

Read `planning/compiler.py`, `workrr/planning_session.py`,
`source_reference_run.py::prepare`, `source_reference_dispatch.py`, and
`task_agent.py::_issue_worker_implementation_request`.

Extract side-effect-free preparation/validation shared by planning and runtime:

- Produce a prepared contract containing the accepted design/catalog/closure,
  command specifications, allowed scope policy, required evidence routes, and
  their fingerprints. Runtime must consume this contract or demonstrably
  recompile the identical value; it must not reinterpret accepted intent.
- Before publishing a plan, validate every item's artifacts and preparation
  requirements. For dependency items, distinguish current baseline facts from
  projected predecessor outputs. Validate references available in the projection
  and record unresolved runtime preconditions explicitly. Do not claim to have
  executed or observed future code.
- Return a per-item preparation report: `ready`, `waiting_for_dependencies`,
  `waiting_for_human`, or `invalid`, with reasons and evidence. Publishing an
  invalid plan fails; legitimate waiting items may be published.
- Before claiming an eligible item, refresh `main`, verify dependency fulfillment,
  resolve one exact execution base, and run preparation against that base.
  Classify material drift as a plan amendment requirement. Do not silently
  update accepted meaning or rerun the entire planner for unrelated changes.
- Separate plan revision identity from execution base identity. Record both.

Acceptance: planner-produced artifacts pass runtime preparation unchanged;
missing definitions, incompatible commands, omitted required scope, missing
verifier routes, invalid dependency projections, and material base drift fail
before a coding call. A valid dependency item becomes ready after its actual
predecessor lands. Include tests proving latest remote `main` is fetched.

## 4. Correct implementation failures without losing authority

Read `source_reference_run.py::authorize`, `claim_invocation`, `execute`,
`source_reference_dispatch.py`, `task_agent.py`, and processes
`execute-proposed-pr.yaml` and `execute-proposed-pr-workflow.yaml`.

- Replace the current one-invocation product stopping policy with a correction
  coordinator. Keep one invocation per immutable attempt; create a new linked
  attempt for each correction. Preserve prior candidate and failure evidence.
- Feed MiniSWE the exact failed checks, permitted candidate files, and required
  correction. Keep accepted intent, independent tests, private verifier boundary,
  protected files, and original base immutable across the correction chain.
- Revalidate scope (including small supporting edits), behavior, preservation,
  lineage, and candidate identity after each correction. A correction cannot
  self-authorize a plan amendment or weaken its verifier.
- Classify recoverable implementation failures for correction; environment and
  provider failures follow their applicable recovery policy; plan defects enter
  the typed amendment path. Ambiguous failures retain evidence and block.
- Fingerprint failure state and candidate state. Stop an unchanged repeat with a
  specific no-progress result. Use explicit configured attempt/cost budgets,
  recorded in the result, rather than an implicit elapsed-time limit.
- Persist the next transition before dispatch. Restart must recover the existing
  attempt/trajectory or reconcile its terminal artifacts; never launch a duplicate
  invocation because the coordinator lost its response.
- Inventory timeout defaults in CLI/config, planned task options, providers,
  coding worker, and verification adapters. Remove implicit task/model deadlines
  consistently. Retained tool-specific limits require documented semantics and
  a classified diagnostic rather than a generic task failure.

Acceptance: an incorrect first candidate is corrected and accepted; unchanged
failure stops; protected test modification is rejected; legitimate old-behavior
test updates work; cancellation and restart at each transition preserve lineage
and invoke no attempt twice. Do not disable native sandboxing in these tests.

## 5. Publish exactly the verified candidate

Read `finish-implementation-pr.yaml`, `finish-pr-prep.yaml`,
`create-pull-request.yaml`, `clean_pr_publisher.py`, `intrinsic_git_gh.py`, and
existing source-run lineage checks.

- Require a publication authorization bound to base, candidate content/tree,
  accepted contract, changed paths, required verification receipts, and lineage.
  Recheck content immediately before constructing the publication commit.
- Remove source editing from PR preparation recovery. Route required source
  repair through task 4, then regenerate candidate authorization. Recheck all
  affected obligations, not merely the command that first failed.
- Generate the final changelog v2 diff and required metadata through the existing
  supported flow. Include their final content in publication validation. Metadata
  reconciliation must not silently change product syntax or accepted intent.
- Assemble fixed PR headings and formatting in code from typed section bodies;
  reuse the strict-template implementation if merged. Missing fields trigger
  structured response correction with exact validation feedback.
- Persist a publication intent with a stable identity before the GitHub write.
  On restart or ambiguous API failure, inspect the existing branch/PR before
  creating another. Record PR URL and head durably before CI observation.

Acceptance: changing source after verification rejects publication; preparation
repair must obtain fresh evidence; malformed sections are corrected; changelog
diff exists and validates; an API response lost after PR creation produces one
PR after restart. Assert base branch `main` and no bootstrap baseline rewrite.

## 6. Require evidence of prerequisite fulfillment

Read `github_issue_queue.py::_dependency_is_closed`, promotion/reconciliation
functions, no-op handling, and the CI outcome observation/checkpoint code.

- A closed issue alone is insufficient. Fulfillment requires a merged PR tied
  to the dependency's accepted revision, or an explicit verified no-op receipt.
- Reuse checkpoint/lineage identity. Reject unrelated PRs, closed-unmerged PRs,
  manually closed issues with no fulfillment, and stale revision receipts.
- Preserve accepted fulfillment through plan amendments only when its contract
  remains applicable. A changed dependency invalidates affected readiness.
- Return machine-readable readiness reasons plus a concise explanation for each
  waiting item. `no_eligible_work` must include counts and blocking reasons.
- Preserve eventual-consistency handling: pending checks, draft PRs, and missing
  freshly created observations are waiting states when appropriate, not proof
  of failed implementation. Waiting state retains its continuation command.

Acceptance: manual issue closure never unblocks work; correct merged dependency
does; verified no-op does; stale/unrelated evidence does not. Repeated promotion
and repair reconciliation are idempotent. Exercise draft and delayed CI visibility.

## 7. Reuse validation with explicit freshness rules

Read `planning/driver.py::compile_accepted_plan`, validation profile discovery,
`finish-pr-prep.yaml`, `coding_agent_validation.py`, and CI validation inventory.

- Build one deterministic inventory of repository commands and obligation-specific
  checks. Map each criterion to meaningful evidence; do not automatically treat
  the repository command as behavioral evidence for every criterion.
- Record validation receipts keyed by candidate identity, verifier/test content,
  command/arguments, relevant configuration and environment identity, and input
  contract. Unknown environment identity makes a receipt non-reusable.
- Publication consumes fresh receipts; it runs missing checks and rechecks
  invalidated inputs. Retain a complete validation summary even for reused checks.
- Start with conservative exact-key reuse. Broader dependency-sensitive reuse
  requires its own evidence and tests; do not infer safety from file extensions.
- Replace the two successive model-based language-label derivations in
  `finish-implementation-pr`/`finish-pr-prep` with one deterministic inventory
  lookup. Explicitly handle mixed languages and unknown file types.

Acceptance: identical inputs execute a command once across implementation and
publication; source, test, config, environment, or contract changes invalidate
the appropriate receipt; missing required checks block publication. Existing
full repository checks remain required until equivalent reuse is demonstrated.

## 8. Measure the whole run and remove proven redundant calls

Extend existing planning telemetry and Workrr run artifacts; do not build a
parallel telemetry subsystem.

- Use one run/attempt lineage to record planning, repository lookup, test
  authoring, implementation, review, verification, correction, publication, and
  CI observation. Include stage wall time, provider calls by LLM/decision model,
  transport requests, logical questions, tokens/cost when available, subprocess
  executions, cache hits, and reasons for repeated work.
- Distinguish true provider batches, concurrent requests, and multiple questions
  inside one prompt. Represent unknown usage explicitly. Count batch corrections
  and failed requests. Do not sum overlapping durations as total wall time.
- Persist telemetry incrementally and print a terminal report on both success
  and failure. A telemetry write failure must be visible without losing the
  implementation result.
- Use the report to choose further batching/deduplication. Cache by the complete
  accepted inputs. Preserve per-answer validation and repair. Move mechanical
  decisions to code; move semantic judgments to decision models only after a
  representative quality evaluation demonstrates acceptable results.

Acceptance: instrumented fake clients/subprocesses yield exact known counts,
including batching, retries, resume, failure, and cache reuse. An interrupted run
retains its observed counts. A representative comparison reports both quality
and call/time changes; do not claim speed gains from call counts alone.

## 9. Recover claims and run independent items concurrently

Read `github_issue_queue.py::run_worker_once`, `_worker_lock`, existing shared
durable-work design, repository locks, and GitHub mutation adapters.

- Replace the repository-long execution lock with short coordination locks and
  per-item durable claims. A claim records owner, attempt identity, generation,
  heartbeat, and recovery state. Keep Git worktree and publication mutations
  isolated per item; retain narrow locks where Git operations require them.
- Use an authoritative atomic claim mechanism with compare-and-set semantics.
  GitHub labels/comments alone cannot implement this. For local deployment,
  reuse the common Git directory's lock/claim store across worktrees. Explicitly
  reject multi-host execution unless the configured shared store supports atomic
  claims; do not imply a local file lock protects multiple machines.
- Lease expiry permits reconciliation, not automatic duplicate execution. Fence
  the old owner from further commits/publication when ownership changes.
- Permit configurable concurrency only for ready items with compatible resource
  and write scopes. Refresh and revalidate the base before each item starts;
  reconcile later base changes before publication.

Acceptance: two contenders produce one claim/invocation; independent items can
progress concurrently; conflicting scopes serialize; owner crash is recoverable;
an old owner cannot publish after losing ownership; unsupported distributed
configuration fails explicitly. Never reclaim solely because a model is slow.

## 10. Prove the complete path with one deterministic scenario

Add an ordinary CI scenario using the existing scenario harness and a temporary
Git repository with a fake GitHub boundary. Use scripted model responses; invoke
the real planner compiler, preparation, coordinator, verifier, checkpointing,
and publisher. Native worker fixtures may substitute deterministic inference,
but must retain the real protected worker boundary.

The scenario must:

1. Accept source intent with retained behavior and produce a two-item dependency
   plan plus an independent item. Compile and validate the emitted artifacts.
2. Show that the dependent item waits with a precise reason and cannot run merely
   because its predecessor issue was closed manually.
3. Execute the first item with a deliberately wrong candidate. Return exact
   verification feedback and correct it through a linked second attempt.
4. Interrupt after verified-candidate persistence and resume without recoding.
5. Lose the API response after PR creation; resume to the same PR. Check the
   strict body, changelog diff, accepted candidate content, and base `main`.
6. Simulate review/merge externally. Refresh `main`, prove fulfillment, and run
   the dependent item against the new base. Exercise independent concurrency.
7. Finish with complete per-item outcomes, diagnostics, obligation receipts,
   lineage, readiness reasons, and exact scripted call/command counts.

Add negative variants for authority loss, stale evidence, failed correction,
scope denial, publication mutation, claim loss, and cancellation. A missing
native sandbox is an explicit untested/blocked acceptance result, not a passing
substitute. Keep optional live-model tests separate from deterministic CI.

## 11. Run live Gaterr acceptance and report the result

After prerequisite PRs merge, pull latest Powdrr and Gaterr `main`. Use the
`powdrr-gaterr-worker` skill and the user's configured provider. Inspect current
issues and prerequisites before choosing work; historical issue numbers may no
longer represent eligible work.

- Generate a fresh plan through Powdrr, publish it for review, and report its
  preparation/readiness results. Wait for the user's plan PR merge.
- Execute at least two dependent items through Powdrr. The user reviews/merges
  each PR. Preserve waiting state while review is pending; do not self-merge.
- Capture diagnostics and correction evidence for any actual failure. Do not
  deliberately damage live Gaterr to manufacture a correction demonstration;
  task 10 supplies that controlled evidence.
- Publish an acceptance report linking plan, item issues, PRs, checkpoints, exact
  Powdrr/Gaterr revisions, terminal results, manual interventions, and telemetry.
  Record calls by model class, repeated checks, cache reuse, stage wall times,
  and total elapsed execution time excluding user review waits.
- Compare efficiency with a comparable recorded baseline when one exists. If no
  comparable baseline exists, label this run the baseline. Do not invent a speed
  target or declare an unmeasured improvement.

Completion requires both dependent implementations to reach valid PRs, all
mandatory evidence to reconcile, and every failure to have an actionable durable
diagnosis. Review waits remain waiting, not completion. Any intervention needed
to repair Powdrr or manually reconstruct its state becomes explicit remaining
work before claiming consistent autonomous execution.

## Verification and handoff for every implementation PR

Run focused behavior tests first, then the repository-required suite before
pushing. Use the shared environment and current worktree imports for every
workspace package. Do not run `uv sync` or create an environment in a worktree.

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

Also run frontend checks when frontend behavior changes and preserve existing
portable-lock CI coverage. Record actual commands/results in each PR. If a
required check cannot run, report its concrete blocker and do not claim success.

Each task's PR description must explain the user-visible behavior, link this
plan and task number, identify contract/schema changes, summarize regressions
covered, and state remaining limitations. Update this plan with the PR and
acceptance evidence after implementation; do not mark a task complete merely
because a PR was opened.
