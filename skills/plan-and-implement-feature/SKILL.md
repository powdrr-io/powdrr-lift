---
name: plan-and-implement-feature
description: Plan a feature from the codebase, then execute it with fresh subagents, a durable ledger, changelog validation, and plan-diff reconciliation.
---

# Plan And Implement Feature

Use this skill when you need to take a feature from discovery through implementation, changelog generation, and validation in the current repo.

## Durable Flow

- Keep progress in `docs/powdrr/runs/<work-item-name>/ledger.md`.
- If the ledger exists, read it first and resume at the first incomplete task.
- Update the ledger after every task, every block, and every review loop.
- Store per-task artifacts alongside the ledger, for example:
  - `task-N-review-codebase.md`
  - `task-N-plan-feature.md`
  - `task-N-implement-report.md`
  - `task-N-review-package.md`
- Treat the ledger as the durable source of truth when context gets thin.

## Model Selection

- Use the least powerful model that can handle the task.
- Use a cheap model for mechanical single-file tasks.
- Use a standard model for multi-file implementation and review tasks.
- Use the strongest available model for architecture, conflict resolution, and final branch-wide review.
- Always specify the model explicitly when spawning a subagent.

## Workflow

1. Preflight.
   - Read the request, inspect the repo state, and scan any existing system map, feature template, changelog, and plan diff for contradictions before dispatching task 1.
   - If you find a contradiction, present it as one batched question before execution begins.
   - Create or refresh the durable ledger and a session todo list.
2. Build the system map.
   - Dispatch a fresh subagent to own this phase.
   - Run `powdrr-lift system-map-specification --work-item-name <work-item-name>`.
   - If using MCP, call `create_system_map_specification`, which should look through the current index and prepopulate all sections from what exists on the current branch.
   - Check recent branches to see whether most of them already include a changelog or are very simple (<100 lines of source changed).
   - If so, have the produced file say, "This file is already complete, delete this line and then move on to the next step".
   - Otherwise keep the normal system-map instructions.
   - Fill the sections one at a time, verify each section, and remove instructions when done.
   - After filling the file, run `powdrr-lift evaluate-system-specification --work-item-name <work-item-name>` to make sure the file validates before moving on.
3. Build the feature and PR template.
   - Dispatch a fresh subagent to own this phase.
   - Run `powdrr-lift feature-pr-specification --work-item-name <work-item-name>`.
   - If using MCP, call `create_feature_pr_specification`.
   - Use the completed system map and the request to produce the implementation contract.
   - After filling the file, run `powdrr-lift evaluate-pr-specification --work-item-name <work-item-name>` to make sure the file validates before moving on.
4. Execute implementation tasks.
   - Split the work into small, disjoint tasks from the template, the changelog gaps, or the plan diff.
   - For each task:
     - write a task brief file with the exact task, constraints, interfaces, and acceptance criteria
     - dispatch a fresh implementer subagent using `implementer-prompt.md`
     - have the implementer edit only the owned files, write tests, commit, and self-review
     - when it reports DONE, generate a review package for the task range and dispatch a fresh reviewer subagent using `task-reviewer-prompt.md`
     - if the reviewer finds issues, dispatch one fix subagent for that task, update the report, and re-review
     - mark the task complete in the ledger only after the reviewer is clean
   - Never reuse an implementer subagent for a new task.
5. Prepare the PR changelog.
   - Run `powdrr-lift init --pr-number <num>`.
   - If the plan diff already exists, use it to guide the changelog fill instead of starting over from memory.
   - If using MCP, call the changelog template endpoint available in this repo.
   - Fill the changelog from the implemented code changes.
6. Generate the plan diff.
   - Run `powdrr-lift plan-diff --feature-plan-specification <path> --changelog <path>`.
   - If using MCP, call `create_plan_diff_specification`.
   - Compare the filled feature plan specification against the changelog files.
   - Dispatch a fresh feature reviewer subagent using `feature-reviewer-prompt.md` and have that subagent drive the diff cleanup loop.
   - Treat each difference as a code-only or changelog-only task and run it through the same implementer/reviewer loop.
   - Never change the specification at this point.
   - Keep iterating until the plan diff is clean.
7. Validate.
   - Run `powdrr-lift evaluate-pr-against-changelog --pr-number <num>`.
   - Keep iterating until the system map, feature template, implementation, changelog, plan diff, and validation all agree.
8. Finish.
   - Do not mark the work done until the ledger is complete and validation passes.
   - Before finishing, run one branch-wide review package through a fresh reviewer subagent using `branch-reviewer-prompt.md` to catch cross-task gaps.

## Handoff Rules

- Keep subagent tasks concrete, bounded, and independently reviewable.
- Pass only the brief and the artifact paths each subagent needs.
- Do not duplicate work between the controller and a subagent.
- If a task is tightly coupled or blocked by missing context, stop and clarify rather than guessing.
- Use the report file as the durable record of what the subagent changed and tested.

## Guardrails

- Do not skip the system map step.
- Do not skip the feature+PR template step.
- Do not skip the plan diff step.
- Do not rewrite the plan or changelog to hide a mismatch.
- Do not mark the work done until the changelog validates.
- Do not mark the work done until the plan diff is empty.
- Prefer the smallest change set that fully satisfies the requested feature.
