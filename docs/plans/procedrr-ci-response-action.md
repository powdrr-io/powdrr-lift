# Procedrr CI response action

Status: implementation plan, ready for review. This document authorizes no target-repository edits.

## Outcome

After publishing or updating an implementation PR, Powdrr observes the required
GitHub checks for the exact candidate commit, reproduces executable failures
locally where possible, researches applicable GitHub Actions constraints, and
dispatches a narrowly scoped correction to MiniSWE. It preserves the intended
behavior and verification obligations. External setup becomes a durable human
prerequisite with instructions and a continuation command. A green local run
alone never establishes a green GitHub Actions result.

The same response procedure can be started explicitly for an existing PR. It
can repair repository-owned CI configuration, application defects exposed by
CI, and test portability defects. It must not broaden the feature or weaken
tests, checks, permissions, or branch protection to obtain a green result.

## Existing integration points

- Service-owned process catalog:
  `packages/service/src/powdrr_service/resources/processes/execute-proposed-pr-workflow.yaml`
  calls `execute-proposed-pr`, then `finish-implementation-pr`. Extend the
  successful publication path with the new reusable response process.
- `execute-proposed-pr.yaml` delegates coding through the coding-agent
  capability. Reuse the existing MiniSWE execution boundary, inference bridge,
  execution journal, verification receipts, and publication machinery.
- `src/powdrr_lift/workrr/github_issue_queue.py` already supports
  `manual-prerequisite-checkpoint-v1`, an awaiting-human queue state, and
  `continue_manual_prerequisite_issue`. Extend this flow instead of creating
  another human-task queue. Extract new policy into the service; keep legacy
  entry points as adapters where necessary.
- `packages/agent/src/powdrr_agent/execution/execute_pr_tests.py` already
  fingerprints and checks frozen tests. Reuse its integrity mechanisms.
- `packages/service/src/powdrr_service/external_contract_research.py` already
  supports bounded retrieval, source capture, hashes, and excerpt selection.
  Reuse these primitives through service-owned Procedrr operations. Existing
  benchmark research restrictions remain in force; CI-response research is a
  distinct purpose and cannot feed benchmark planning inputs.
- Public contracts currently live in `powdrr_contracts/models.py`,
  `service.py`, `workspace.py`, and `verification.py`. Add `ci.py` for the
  transport types described below, using existing serialization conventions.

## Ownership and package boundaries

| Component | Responsibility |
| --- | --- |
| `powdrr_contracts/ci.py` | Versioned data types, discriminated results, action requests and receipts; no diagnosis or prompt policy |
| `powdrr_agent` | Authenticated GitHub reads/writes, local snapshots, isolated reproduction, MiniSWE process, filesystem enforcement, command receipts, Git publication |
| `powdrr_service/ci_response/` | Evidence normalization, fingerprints, reproduction planning/comparison, diagnosis rules, research selection, correction policy, prompts and acceptance decisions |
| Service process resources | Procedrr steps, branches, bounded iteration, small questions, human routing and terminal outcomes |
| `powdrr_local` and existing CLI adapters | Composition and transport only |

All proprietary workflow definitions, prompts, routing rules, research
selection, failure taxonomies and repair acceptance logic belong in the
service. The agent executes typed capabilities and enforces supplied
restrictions. It must not import the service, select diagnoses, generate
research queries, or assemble repair prompts. Provider credentials remain
service-owned; repository credentials remain with the agent. Send only
necessary, redacted observations across the boundary.

Proposed service modules: `evidence.py`, `reproduction.py`, `diagnosis.py`,
`research.py`, `repair_contract.py`, `prompt.py`, `acceptance.py`. Keep these
pure where practical and expose operations through the existing service
interface and Procedrr operation registry. Do not add a second Python workflow
loop that duplicates Procedrr branches.

## Durable records

Use versioned public records, with deterministic IDs assigned by code. Models
may select an existing ID; they never invent record IDs.

`CIObservation` binds repository, PR number, head SHA, tested SHA, merge-base
SHA where available, workflow path and content hash, workflow run ID, attempt,
job ID, matrix coordinates, check context/app, status, conclusion, runner
labels, assigned runner metadata, steps and annotations. Preserve GitHub's
native statuses and conclusions; derived categories are separate fields.
Include trigger, permissions and relevant environment metadata without values
of secrets. Resolve reusable workflows/action references when needed for the
failed surface, recording immutable revisions. Do not treat an absent API
field as a negative fact.

`FailureUnit` contains one job/step failure or one pre-job annotation, precise
evidence references, a normalized signature, affected workflow span, and the
smallest relevant command. Preserve raw evidence hashes. Fingerprints omit
timestamps, absolute checkout paths and incidental run IDs, but retain error
kind, test node ID, relevant versions, matrix and failure phase. A broad text
match such as "failed" is never a matching signature.

`LocalReproduction` has one of:

- `reproduced`: the same failure signature occurred under recorded conditions.
- `not_reproduced`: the comparable command reached the relevant test/phase
  and the signature did not occur. Include its outcome. A different failure
  counts only when evidence proves the target behavior was still exercised;
  an earlier setup failure makes this comparison inconclusive.
- `not_comparable`: OS, architecture, merge tree, service, permissions, secret,
  dependency or event differences prevent a meaningful comparison.
- `not_executable_locally`: the failure occurred before a command could run,
  such as runner provisioning or GitHub expression validation.
- `execution_error`: Powdrr could not perform the intended reproduction.

`RepairContract` binds the observation/failure/reproduction IDs, head SHA,
allowed paths and semantic targets, protected artifacts, required behavior,
verification commands, required GitHub contexts, research claim references,
and explicit stop conditions. It has a content digest and expiry conditions.

`CIResponseCheckpoint` binds those records to the issue and PR, current
Procedrr step, attempts, observed check generations, next action, and optional
human prerequisite. Keep a compact latest-state pointer in the issue body and
append concise transition comments. Use existing checkpoint/artifact storage
for large logs and research snapshots; do not load the complete comment history
or put raw logs into comments. Artifacts need durable retrievable references
and hashes; a machine-local path is insufficient for resume on another host.
Publish the artifact before advancing its checkpoint pointer. Persist state
before waiting, invoking MiniSWE, pushing, or requesting a human action.

## Procedrr procedure and deterministic transitions

Add `respond-to-pr-ci.yaml` plus reusable subprocesses for observation,
reproduction, research, correction and acceptance. Each step receives a small
typed projection of prior evidence, not the entire workflow transcript.

1. **Observe the candidate.** Collect the exact PR head, required contexts and
   matching check/run generations. Represent unavailable branch-protection
   data explicitly; do not silently assume there are no required checks. Use
   configured repository requirements when available; otherwise require the
   relevant triggered CI workflow jobs and record the source of that policy.
   Ignore successful checks for an earlier SHA. Track GitHub's tested PR merge
   SHA separately from the head SHA.
2. **Wait or identify failure units.** Queued/running checks cause a durable
   wait with backoff and deadline. A successful nonrequired job does not mask a
   required failure. Missing, skipped, neutral, stale, canceled and unavailable
   checks do not count as success. Cancellation alone has no diagnosis: inspect
   annotations and superseding runs before deciding whether to wait, retry or
   investigate. Missing workflow triggers and required-context naming mismatches
   are investigable configuration failures, not endless waits.
3. **Collect bounded evidence.** Fetch failing job metadata, annotations,
   relevant log spans, test reports, workflow YAML, referenced local scripts,
   dependency lockfiles and runtime versions. Read pre-job annotations even
   when steps and logs are empty. Paginate APIs completely or mark evidence
   incomplete. Attach integrity and truncation indicators to every excerpt.
4. **Reproduce locally.** Execute the service-selected, authorized command at
   the immutable candidate/tested tree in an isolated clean worktree. Resolve
   workflow `working-directory`, shell, matrix, setup versions and environment.
   Compare capability requirements before execution. Reproduce the tested
   merge tree when reconstructable; if unavailable, record the difference.
   Capture the actual command's exit status directly. Never infer success
   from a pipeline's final `tail`/`tee` process. Use structured test reports to
   match the failure. Setup/import/tooling failures are not reproduction of a
   different assertion failure. Run the failed shard first, then broader
   existing verification only when relevant.
5. **Route the unit.** Use deterministic rules for known evidence shapes:
   unsupported runner, unavailable secret/permission, workflow syntax,
   dependency/toolchain failure, test assertion failure, network outage and
   superseded cancellation. These are candidates, with evidence and confidence,
   not unconditional repair commands. Unknown cases use the scoped questions
   below. Distinguish repository-owned changes from external setup. A runner
   error does not establish that changing YAML is sufficient or that only an
   administrator can fix it.
6. **Research a specific unresolved external fact.** Invoke the research
   subprocess only if diagnosis or repair needs an external GitHub/action
   contract. Return bounded evidence-backed guidance or unresolved questions.
7. **Compile one correction contract.** Group units only when they share the
   same proven cause and edit surface. Serialize overlapping repairs. Pin the
   allowed patch scope and protected verification contract before MiniSWE.
   External setup enters the existing human-prerequisite path. Proven plan
   contradictions enter the existing plan-amendment process with evidence.
8. **Execute and validate one repair.** Start MiniSWE with the scoped prompt.
   Reject out-of-scope or verification-weakening patches deterministically
   where possible. Re-run the target reproduction and protected verification.
   Failed validation returns a small finding to the same response process.
9. **Publish and observe again.** Push accepted changes to the existing PR
   branch through the existing publication adapter. Observe the new exact
   head and required checks. Local success for a GitHub-specific failure is
   necessary but insufficient: GitHub must confirm the repaired behavior.
10. **Finish or pause.** Only current required successful checks produce
    `awaiting_review`. Human setup produces `awaiting_human`. Bounded unresolved
    failures produce `blocked` with evidence and next action. Preserve existing
    implementation PR and work. Never merge automatically.

Default policy: at most two accepted repair attempts per failure signature,
one bounded transient rerun when evidence supports it, and an explicit
observation deadline. Configure these in the service procedure. Retry
transport 429/5xx using existing backoff independently of repair attempts.
Repeated identical patches/signatures stop for review. A changed head
invalidates stale diagnosis and acceptance; reacquire evidence. GitHub may
complete asynchronous jobs after a timeout; resume observes their latest
generation before dispatching another action.

## Small model decisions

Deterministic evidence collection, capability comparison, signature comparison,
scope checks and routing rules run first. For ambiguity, ask one question about
one failure unit and supply only its relevant evidence IDs/spans:

- Does annotation E establish that GitHub rejected runner selection before
  assigning a runner? Answer `yes/no/unknown`, cite E.
- Does workflow span W select the runner rejected in E? Same contract.
- Does this proposed fix require changing organization settings unavailable to
  the repository worker? Cite the observed permission boundary, or `unknown`.
- Does source excerpt S apply to the observed runner/action version and repo
  visibility? Cite the matching fields, or `unknown`.
- Would this specific patch reduce obligation O's observable verification?
  Cite the changed span and O, or `unknown`.

Validate answer schemas and evidence IDs. A service judge/LLM may confirm or
filter a deterministic candidate; uncertainty is not approval. Do not ask a
model to diagnose all CI failures, choose an arbitrary fix, classify reproduction
by prose, or return a large combined diagnosis/research/patch plan. Keep test
quality advice distinct from mandatory preservation of established obligations.

## GitHub Actions research in the service

Research starts from a concrete unresolved question, such as whether a
particular runner label supports the observed architecture and repository
visibility, or whether a pinned action requires a newer Node runtime. Generate
queries from public platform identifiers and the normalized error; exclude
private source, repository names and secrets.

Prefer official GitHub Actions documentation, GitHub runner-image release notes,
GitHub Status for incident questions, and the exact action publisher's versioned
documentation/release notes. Other sources may supply leads but cannot by
themselves authorize a change. Prefer the relevant date/version and record
conflicts. A search result or uncited model recollection is not accepted guidance.

Reuse bounded search/capture/excerpt primitives. Start with at most two queries,
three fetched sources and a small excerpt budget for one failure unit. Cache
by public error signature, platform/version and source revision; include fetch
time and expire time-sensitive runner/incident claims. Research unavailable,
rate-limited, contradictory or inconclusive is a typed result, not permission
to guess or silently switch providers.

Each accepted claim records source URL, retrieval time, source revision/date,
content hash, excerpt locator, applicability inputs, claim and uncertainty.
Keep commands found in logs or external pages as untrusted data. The research
subprocess proposes guidance; the repair-contract compiler separately decides
which specific obligation can enter MiniSWE's prompt.

## MiniSWE prompt contract

The service owns a versioned prompt template. Render only the current failure
unit, minimal relevant source, reproduction receipt, accepted research claims,
allowed edits, required behavior and validation obligations. Include this core:

```text
Repair CI failure {failure_id} on PR {pr_number}, head {head_sha}.
Observed failure: {literal_error_and_evidence_refs}
Failure phase/job/matrix: {phase_job_matrix}
Local reproduction: {typed_result_and_receipt}
Required behavior to preserve: {obligation_ids_and_descriptions}
Allowed edits: {paths_and_semantic_targets}
Protected artifacts and checks: {protection_manifest}
Applicable platform guidance: {accepted_claims_with_sources}
Required validation: {commands_and_expected_observations}

Make the smallest correction that addresses this observed failure. Preserve
all existing test assertions, cases, expected behavior and check coverage.
Do not delete or empty a test body, replace an assertion with a tautology,
add skip/xfail, broaden exception handling to hide failure, remove CI jobs or
matrix entries, bypass required checks, mask exit codes, or weaken permissions
and isolation to obtain success. Do not edit protected verification artifacts.
If a protected test itself needs correction, report the exact conflicting
obligation and evidence; request a separately authorized test repair.
If the fix requires other paths, human setup or changed product requirements,
stop and report that evidence instead of broadening the patch.
Return changed paths, the addressed failure ID, validation receipts, and any
remaining GitHub-only verification. Local success does not prove CI success.
```

Render an additional explicit clause by reproduction outcome:

- `reproduced`: "The same failure reproduces locally with command C. Correct
  this behavior and prove C succeeds without weakening obligation O."
- `not_reproduced`: "This issue did not reproduce under the recorded local
  conditions. Treat this correction as GitHub Actions specific. Target the
  observed workflow/runner/event/setup difference. Do not rewrite passing
  application behavior or its tests to manufacture local agreement."
- `not_executable_locally`: "This is a GitHub Actions platform/configuration
  failure before local executable steps. There is no equivalent local test of
  runner provisioning. Validate YAML and contracts locally; GitHub must verify
  the runner/job starts and the required checks pass."
- `not_comparable` or `execution_error`: "Reproduction is inconclusive for
  reasons R. Do not claim the failure reproduces or does not reproduce. Only
  make an evidence-supported environment correction explicitly authorized by
  this contract; otherwise request the missing capability or human action."

## Preventing tautological repairs

Default to test/config protection before coding: hash existing verification
files and preserve an obligation manifest containing test node IDs, test count,
assertion/call structure, markers, expected outcomes, required jobs/matrix,
commands and action success conditions. Enforce allowed writes in the coding
sandbox and independently validate the resulting Git patch. Include renamed,
deleted, symlinked, untracked and ignored paths in mutation accounting.

CI configuration is an allowed edit surface only to the extent authorized by
the contract. A change from a real verification command to `true`, `echo`,
`--collect-only`, `|| true`, `continue-on-error`, zero selected tests, or a
condition that skips PR validation fails acceptance. Preserve coverage across
matrix changes using explicit equivalence obligations; do not merely compare
job counts. Detect dynamic/indirect commands and unresolved YAML expressions
as unproven rather than guessing their behavior.

An unchanged file hash is definitive for protected tests. AST checks catch
obvious weakening in a separately authorized test change but cannot prove
semantic equivalence. For test portability fixes, use a distinct Procedrr
subprocess: name the allowed setup/import/path/platform edit, keep behavior
assertions and expected values protected, demonstrate the original environment
failure and corrected collection/execution, retain obligation coverage, and
obtain an independent scoped review. Anything beyond this requires human
approval or a reviewed plan amendment. This is not a route to relabel a
feature regression as a bad test. Preserve existing generated-test correction
policy rather than adding unrestricted automatic rewriting.

## Human setup and continuation

A human prerequisite states the precise failed check and annotation, what
setting/resource is missing, who can change it if known, the observed required
permission, steps supported by evidence, and how Powdrr will verify completion.
If ownership is unknown, ask one specific ownership question. Never request
secret values in comments; ask for installation at the documented destination.

Continue observes the latest PR head and reruns only eligible failed checks
once, or waits for a newly triggered run. It must not rerun an entire workflow
with deployment/release side effects without an authorized action. It resumes
the same issue/PR procedure and remains awaiting human if setup is unresolved.
Repository configuration fixes use MiniSWE on the existing PR when authorized;
external administrative changes use the human route. Both require GitHub
confirmation after the change.

## Motivating fixture and regression scenarios

Capture sanitized API fixtures from Gaterr PR 270: head
`6c85b1e5c334a113b3db274b2c1b7a8f350b9707`, workflow `.github/workflows/ci.yml`
selecting `macos-14`, run `42107708170`, attempt 2, job `127527360223`, no
steps, runner ID 0, and annotation "The configuration
'macos-14-arm64-us-default' is not supported". This proves runner configuration
was rejected; it does not prove the correct replacement label, administrative
ownership, or that `macos-14` remains supported today. Research must establish
those facts. Expected reproduction result: `not_executable_locally`. Local
125-test success cannot turn this check green.

Build deterministic Procedrr scenarios with nontrivial GitHub payloads and
scripted narrow model answers. Required cases:

1. Gaterr pre-job annotation, absent logs/runner, research-backed repository
   fix versus separate external setup branches, then successful GitHub retry.
2. Matching local assertion failure; different local import failure; green
   comparable local run; incompatible OS/architecture; unavailable PR merge tree.
3. Stale green head, superseded cancellation, queued deadline, missing required
   check, skipped required job, matrix partial failure, permission-denied API,
   paginated/incomplete annotations and multiple independent failures.
4. MiniSWE proposes deleting a test body, `assert True`, skip/xfail,
   catch-and-pass, check-command substitution, `continue-on-error`, removed
   matrix coverage or masked pipeline exit status: each patch is rejected.
5. Authorized test import/path fix preserves all behavior assertions and gets
   independent validation; a changed assertion fails the same authorization.
6. Source prompt injection, stale documentation, contradictory runner support,
   unavailable research, poisoned cache and missing claim lineage do not grant
   broader edits or produce invented guidance.
7. Crash after publication, duplicate observation, duplicate push, resumed
   human task, changed head during repair and missing durable artifacts.
   Replays do not duplicate a PR/comment, charge another repair attempt, or
   accept evidence from a different head.
8. Shared interpreter/settings propagate into local subprocesses; required
   CI services/secrets unavailable locally produce `not_comparable`, never a
   misleading `not_reproduced` result.

Use the existing workflow-scenario suite and ownership inventory checks. Include
one integration scenario that crosses the serialized agent/service boundary
with an actual small temporary Git repository and shell reproduction command,
mocked GitHub transport, and scripted MiniSWE patch attempts. Test observable
state transitions and rejected patches, not just prompt substrings or helper
return values. No live provider or private GitHub access is required in CI.

## Implementation sequence

Deliver this in three substantial PRs; each includes its scenarios and updated
ownership inventory. New policy stays in service modules from its first commit.

1. **Observation, reproduction and durable procedure.** Add public CI records,
   agent transport/execution capabilities, service normalization/comparison,
   Procedrr observation/wait/reproduce branches and durable checkpoints. Add
   an explicit `respond-to-pr-ci --repository --pr --issue` entry point following
   existing CLI conventions. It can diagnose and pause without editing. Prove
   the motivating fixture and all reproduction states through the boundary.
2. **Research and protected corrections.** Add scoped service research,
   claim applicability decisions, contract/prompt compiler, generic agent
   enforcement, correction acceptance, MiniSWE dispatch and limited publication.
   Cover repository fixes and human prerequisites, plus adversarial weakening
   attempts. Prove a correction can reach green mocked GitHub checks without
   changing protected tests.
3. **Publication integration and continuation.** Call the procedure after
   `finish-implementation-pr` and PR updates, expose structured terminal
   outcomes to the queue, and extend the existing human continuation path.
   Preserve `pull_request_url` while adding the explicit CI outcome; the queue
   must not infer awaiting-review from an exit code alone. Add resume/race and
   existing-PR scenarios, operator guidance and complete required repository
   checks. Exercise Gaterr only through the product workflow after merge.

Completion requires an actual PR whose current required GitHub checks pass,
or an evidenced human prerequisite with a working continuation path; a locally
passing patch plus an ignored canceled check does not satisfy this plan.
