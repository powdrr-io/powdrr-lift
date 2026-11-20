# Intent Preserving Scope Recovery

## Implementation handoff

Implement one Powdrr-Lift PR that lets a Gaterr issue worker resolve a mismatch
between a candidate diff and Powdrr's planned file list in the same run. Start
with the source reference execution route used by Gaterr #917. Use the same
decision and evidence types for the standard execution route. Land planner
prevention after execution recovery works. This handoff targets main at
`4693b120`, which includes the diagnostic change from PR #1624.

The acceptance condition is a validated implementation PR when the candidate
serves the accepted requirements and the repository checks pass. A better error,
a plan-only PR, or a retry with the same inputs does not satisfy this condition.
The worker still creates its usual PR changelog. Every GitHub operation must
work on GitHub Free for private repositories.

## Reproduce the failure

Use a local synthetic repository in automated tests. Reproduce these properties
of the real [Gaterr #917 attempt](https://github.com/powdrr-io/powdrr-gaterr/issues/917#issuecomment-7023274329):

1. An accepted behavior requires fixture coverage of toolchain metadata and a
   signed environment. The planned paths include the fixture generator and its
   tests. The generator writes a tracked `attestation/fixtures/golden.json`,
   which the tests and repository verification command read.
2. The coding agent exits zero and submits a candidate. The generated file is
   the only unplanned path and its diff adds 25 lines. In the recorded run,
   this exceeded the 20-line supporting edit threshold. MiniSWE's attempt
   recorded `policy_denied`, with the exact path in `out_of_scope_paths`.
3. The outer source result reported `attempt_completed` with an empty accepted
   changed-path list. The nested attempt still contained the scope evidence.
   The failure reviewer cited that evidence, inferred that editing the generator
   and tests was sufficient, and sent the run to the terminal
   `implementation_defect` branch. The enclosing task stayed locked and the
   queue surfaced a generic selection error. PR #1624 adds task state to that
   error. It adds no path recovery.

The generator relationship makes the file a strong recovery candidate. The
original attempt never completed normal validation, so the historical record
cannot establish that the candidate as a whole was correct.

## Authority and decision rules

Apply these rules in order. Make the decision for each exact repository path
in the observed diff, including paths that would fit the current small-edit
exception.

| Condition | Worker action |
| --- | --- |
| Explicit user restriction, protected path, protected test, escaping path, or disallowed file operation | Refuse authorization. Request a candidate repair within the existing boundaries. |
| Existing tracked generator output, with a verified producer, accepted requirement, and a reproducible candidate update | Authorize the exact file for this item and attempt. |
| Other supporting edit with concrete evidence of necessity | Request independent review of the exact diff and requirement evidence. Authorize only after validated approval. The first PR may route this case through evidence gathering and candidate repair. |
| Unrelated, excessive, or unsupported change | Ask the coding agent to remove or replace the exact change while retaining accepted work. |
| Evidence is incomplete | Gather repository facts, then try a viable implementation within the current scope. If recovery is exhausted, record the missing fact and a technical failure. |
| Evidence shows a material choice about behavior, compatibility, or authorization that existing instructions do not resolve | Persist a focused human question with the alternatives and consequences. Use the existing human task mechanism where available. |

Treat generated `allowed_paths` as a mutable execution constraint. Keep the
accepted requirements, acceptance criteria, explicit path restrictions, protected
paths, verification obligations, and review requirements authoritative. Trace
explicit restrictions to their source. Where legacy data does not let Powdrr
distinguish an explicit restriction from a derived path, require positive
evidence that adding an exact path is consistent with the accepted contract.
Missing provenance cannot authorize a hard policy override.

Use line count for review context. It neither authorizes tiny unrelated edits nor
disqualifies an otherwise justified fixture update. Feed repository text and
agent rationale to reviewers as untrusted evidence. The runtime must enforce
hard policy and verify evidence references before acting on a review result.

## Code map

| Area | Starting point |
| --- | --- |
| Raw candidate paths, status and supporting edit heuristic | `src/powdrr_lift/workrr/coding_agent.py`, `CodingAgentAttempt`, `permitted_supporting_edit_paths` |
| Source candidate staging and finalization | `src/powdrr_lift/workrr/source_reference_run.py`, `finalize_worker`, `_out_of_scope_paths` and the scope review route |
| Procedrr operation and common failure evidence | `src/powdrr_lift/workrr/task_agent.py`, `_normalize_execute_pr_coding_attempt`, `_execute_pr_failure_evidence_catalog`, and the coding agent dispatch |
| Existing bounded iterative repair | `src/powdrr_lift/workrr/coding_agent.py`, `run_coding_agent_recovery`, and its callers in `task_agent.py` and the source route |
| Existing typed plan changes | `packages/service/src/powdrr_service/planning/repair.py`, `amend-plan-and-open-pr.yaml`, `_run_plan_amendment_agent` and `_validate_issue_worker_plan_amendment` in `task_agent.py` |
| Top level routing and durable errors | `execute-proposed-pr.yaml`, `execute-proposed-pr-workflow.yaml`, `run_workflow_task`, `pr_execution_session.py`, and the issue queue's result writer |

The named YAML files live under
`packages/service/src/powdrr_service/resources/processes/`. Read the source and
standard routes before putting recovery in a shared function. They do not
finalize candidates in the same workspace. In particular, a scope denial can
leave the source candidate private and the integration worktree clean.

## PR 1 execution recovery

### 1. Propagate a structured scope finding

Add a runtime-owned finding, suggested type `CandidateScopeFinding`. It carries
the plan and item IDs, base commit, accepted contract fingerprint, candidate diff
fingerprint, attempt ID, observed changed paths, unplanned paths, protected
violations, and an opaque candidate checkpoint reference. Include the operation
kind and content digest for each questioned path. Keep observed paths separate
from accepted and published paths. An empty accepted path list must preserve
the paths observed in a denied candidate.

Serialize the finding at the coding agent boundary and expose it as a typed
result in both execution routes. For older saved records, recover observed facts
from the nested terminal attempt. If the quarantined candidate cannot be opened
and verified against those facts, start a fresh isolated correction from the
pinned base using the saved finding. Avoid fabricating a candidate fingerprint.
Add stable evidence IDs for each questioned path and each repository observation.

### 2. Collect evidence and resolve the exact paths

Start with a generated fixture rule. Prove that the file is a tracked regular
file at the baseline, its normalized path remains inside the candidate root,
and its mode and kind remain unchanged. Verify that a planned producer targets
that exact path. Use bounded source reads and source spans to identify the write
and a test or verification consumer. Link the output to a specific accepted
criterion or implementation step. A static producer reference alone does not
prove the candidate's exact bytes were generated.

Use an existing permitted validation command in the existing sandbox on a
disposable copy of the candidate when checking reproducibility. Remove only the
expected output in that copy, run the accepted generation or verification entry
point, and compare regenerated bytes with the submitted bytes. Check the
command's actual write set. If there is no permitted command, or output is
nondeterministic, use the evidence and candidate repair route. A model cannot
grant extra command, network, or filesystem permissions.

Represent the decision as one of `authorize_exact_path`,
`repair_candidate`, `needs_intent`, or `technical_failure`. A decision
includes the path, reason code, cited evidence IDs, and a short explanation.
For `needs_intent`, require the unresolved decision, its source reference, and
the consequences of each viable alternative. A review response citing missing
evidence or asking for a policy override cannot authorize a write. Resolve each
path independently so a bad path does not discard an otherwise valid repair.

### 3. Resume and validate the candidate

For authorized paths, let Powdrr compute a typed additive scope delta from the
observed exact files. Reuse the plan repair validation and compare-and-swap
machinery. Avoid asking a model to replace the item. Require the same item and
workflow IDs, requirements, criteria, dependencies, protections, and verification
settings. Use an item-local amended execution contract and record the delta in
the durable result and implementation PR. If linked artifacts encode the old
scope, recompile the affected plan, workflow, and source or Structrr contracts
through their owners. Apply the amendment at candidate scope checks and final
publication checks. A PR description alone does not update enforcement.

Keep scope authorization bound to the base commit, original contract, and
observed diff. If the base, candidate, claim owner, or approved file content
changes, recompute or revalidate the authorization before publication. Update
all affected enforcement points together. Persist the decision before continuing
so a restart can replay it idempotently. Where authorizing on the recorded
checkpoint is unsupported, rerun the coding agent on the amended exact scope
inside the same queue claim and recheck the resulting changes.

For a repair decision, pass the exact rejected paths and reasons through the
existing correction request. Preserve accepted work in the quarantined candidate
where possible. Reinspect its complete diff on the next attempt. Use the
existing recovery attempt budget, counting scope repair against that budget.
When the base, effective scope, diff and outstanding findings repeat with no
new supporting evidence, stop automated retries and persist a technical
`scope_recovery_no_progress` outcome.

Run existing source-reference and Structrr gates, protected test checks,
acceptance tests, repository verification, and PR preparation through their
normal entry points before reporting success. Publish only after those checks
and the final scope check accept the same candidate. Include any persisted plan
artifact changes required by the enforcement contract in the implementation PR.

### 4. Route failures without losing their cause

Handle the scope recovery outcome before the generic implementation defect
branch. Keep other failure classes on their existing routes. On exhaustion or
infrastructure failure, persist a structured diagnostic with stage, reason
code, attempt ID, affected paths, evidence or checkpoint reference, recovery
count, and resumption condition. Return it from the failed process boundary
before task selection can overwrite it with a locked-task message. Use bounded
diagnostics in GitHub comments. Keep raw trajectories and private evidence local.

Keep human intent decisions distinct from technical failures. Persist the
question and the response necessary to resume. Exhausting retries, a provider
error, a validation failure, or a model saying “unclear” is insufficient to
create a human intent decision by itself.

## Test and acceptance checklist

Add unit cases near `test_coding_agent.py`, `test_source_reference_run.py`,
`test_workflow_task_agent.py`, and `test_pr_execution_session.py`. Put a
workflow regression through the normal issue-worker entry point using a fake
GitHub client and coding provider. Verify call order, final state, and exact
files published. Tests must cover:

1. **Generated output over the line limit.** Use a 25-line generated change.
   The first candidate is quarantined. Powdrr verifies the producer, consumer
   and exact output. It records one exact path amendment, runs all gates, and
   opens one implementation PR with a changelog during the original worker run.
2. **Unrelated small change.** A one-line edit outside scope enters repair. The
   corrected candidate retains the intended work. The unrelated path never
   appears in the PR. Also test a mixed candidate with one justified output and
   one unrelated file.
3. **Protected or misrepresented change.** Test explicit exclusions, acceptance
   test tampering, path traversal, symlink or mode changes, and hand-edited
   fixture bytes that fail reproduction. Reject self-approval from the coding
   agent or a reviewer with missing evidence IDs.
4. **Consistency and resume.** Simulate a changed base, changed candidate digest,
   lease loss, and a restart after writing the amendment. Prevent stale
   publication and duplicate amendments or PRs. Verify older nested attempt data
   still yields the exact denied path.
5. **Failed recovery.** Repeat the same denied diff and verify bounded attempts
   and a resumable technical diagnostic that names the path. Simulate a command
   failure and a later failed acceptance gate. Neither yields a successful PR
   nor asks the user to clarify intent.
6. **Real intent choice.** Use a fixture where the two viable changes would
   make different compatibility promises and the accepted criteria leave that
   choice open. Verify one durable human question, no publication, and resume
   under the recorded answer.
7. **Free private repository.** Deny branch-policy access in the fake GitHub
   client and expose normal PR checks. Verify the worker uses the existing
   fallback and still requires readable successful check evidence.

Run the focused cases, then the repository's full verification suite with the
shared virtual environment and all package source paths on `PYTHONPATH`. After
merge, pull Powdrr-Lift main and use the normal Gaterr worker to resume #917
from a verified checkpoint or a fresh isolated attempt. Check whether Gaterr
main already satisfies the item before requeueing. Let Powdrr create the
implementation PR. Capture the exact scope decision and check results on that
PR, or the concrete remaining diagnostic if validation fails.

## Follow-up planning prevention

Teach the planner to carry producer and consumer relationships alongside
proposed paths. Start with a tracked artifact, a known write destination in
an in-scope generator, and a verified test or validation consumer.
Emit the exact output path, relationship evidence, and requirement reference
into the reviewed item and its linked artifacts. Fall back to execution
recovery where the output cannot be determined statically. Add a compiler
regression showing that the Gaterr-shaped fixture output enters the initial
plan without granting its directory. Track how many scope findings are resolved
automatically, require candidate repair, exhaust recovery, or require an intent
decision. Count completed validated PRs separately from scope authorizations.
