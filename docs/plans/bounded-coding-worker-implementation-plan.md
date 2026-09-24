# Bounded Coding Worker Implementation Plan

Status: proposed

## Purpose

This document defines an implementation-ready redesign of Powdrr's coding
worker boundary. It is written for another implementation agent and makes the
architectural decisions that should not be rediscovered while coding.

The redesign keeps intent interpretation, workflow control, validation, and
completion authority in Structrr/Procedrr/Workrr. A coding agent remains an
untrusted repository editor. The recommended editor is a small Powdrr-specific
agent built on mini-SWE-agent's model and environment primitives. OpenCode
remains available temporarily as a comparison provider, but is not the target
default for Harbor runs.

The governing rule is:

> Model activity is not progress, a worker exit is not success, and an existing
> dirty diff is not evidence that the current work unit changed anything.

This plan complements `docs/plans/bounded-llm-instruction-compiler.md` and
`docs/plans/intent-linked-verification-implementation-plan.md`. Those plans own
instruction compilation and verification contracts. This plan begins after
actionable obligations and required test cases have been compiled.

## Failure being corrected

The DeepSWE `python-statemachine-state-data-scoping` run on 2026-09-23 exposed
four independent failures.

1. Thirty-five semantic obligations became thirty-five fresh coding-agent
   sessions. Each session repeated repository discovery and overlapping design
   work. At the current fifteen-minute OpenCode absolute deadline, the worst
   case was 525 minutes although Harbor allowed ninety minutes.
2. OpenCode emitted structured model and tool events indefinitely. Powdrr
   treated those events as progress even when the repository diff and focused
   test result did not change. Several attempts consumed the full absolute
   deadline.
3. The code-task postcondition `agent_completed` checked only that an attempt
   object existed. It did not require `attempt.status == completed`.
   `diff_nonempty` checked the complete worktree diff rather than the delta
   attributable to the current attempt. Timed-out or no-op tasks could
   therefore pass when earlier planning artifacts had already dirtied the
   worktree.
4. The submitted 43,243-line patch contained only generated planning files:
   `docs/proposals/harbor-task/proposal-revision.json`,
   `docs/proposals/harbor-task/structrr-diff.yaml`, and a generated Structrr
   baseline. It contained no product implementation. All 1,286 pre-existing
   tests passed, while all 72 feature tests failed.

The implementation must treat this run as a regression fixture. It is not
enough to show that an agent process starts or that a mocked provider returns a
successful exit code.

## Goals

The completed system must:

1. Finish, fail, or produce a resumable partial result within a deterministic
   global coding budget smaller than Harbor's agent timeout.
2. Dispatch one coherent implementation request for a feature, followed only
   by validation-derived repair requests.
3. Keep semantic obligations as a complete verification checklist without
   turning each obligation into an independent coding session.
4. Measure progress from repository and validator state, not transport events.
5. Make a timed-out, failed, policy-denied, or no-progress attempt incapable of
   satisfying an implementation gate.
6. Attribute every accepted diff transition to one worker attempt relative to
   that attempt's exact starting state.
7. Keep generated Powdrr, Structrr, Procedrr, and coding-agent artifacts outside
   the candidate repository diff.
8. Give the coding model a concise packet containing only the original feature
   description, actionable obligations, expected new test names, repository
   facts, allowed paths, and focused validation commands.
9. Preserve full worker trajectories, command results, diff fingerprints,
   liveness decisions, and budget consumption for debugging.
10. Support the same coding-worker contract in standard feature and Harbor
    flows.
11. Allow OpenCode and mini-SWE-agent to be compared behind one provider
    interface without provider-specific semantics leaking into Procedrr.
12. Prove the behavior with deterministic tests and a live DeepInfra opt-in
    regression using the complete state-data-scoping task.

## Non-goals

This redesign does not:

- move design, intent classification, planning, review, or completion authority
  into the coding agent;
- replace the bounded instruction compiler or verification-contract work;
- let the worker choose its own budget, validation profile, allowed paths, or
  success criteria;
- require one coding session per sentence or obligation;
- accept the worker's claim that it is finished as evidence;
- run the complete repository validation suite after every worker action;
- expose internal proposal documents, Structrr baselines, fingerprints,
  selectors, or review receipts to the coding model;
- implement a new model API client, shell sandbox, trajectory format, or command
  parser when mini-SWE-agent already supplies a suitable primitive; or
- guarantee that a weak model can solve every feature. It guarantees bounded,
  observable, correctly classified attempts.

## Architectural decisions

### Own the supervisor, reuse the agent substrate

Powdrr must own a small `BoundedCodingSupervisor`. The supervisor owns request
shape, budgets, material-progress detection, validation transitions, repair
selection, and terminal classification.

Use mini-SWE-agent through its Python API for:

- model/provider adaptation;
- one-action-per-turn command generation;
- environment command execution;
- command process-group timeout handling;
- message/observation formatting; and
- trajectory serialization.

Do not fork mini-SWE-agent initially. Pin a compatible release and subclass or
compose its documented `DefaultAgent`, model, and environment interfaces. Keep
Powdrr-owned types independent so another provider can be substituted later.

### Obligations are verification units, not execution units

The canonical obligation list remains one record per requested behavior. The
coding boundary receives all actionable obligations in one concise feature
packet. One primary coding session implements the coherent feature.

Only validation results create repair units. A repair unit may cover multiple
obligations when they share the same failing tests or code surface. The runtime
must not mechanically create one worker process per obligation.

If a future feature exceeds configured packet limits, compile at most five
coherent slices. Slices must be based on an explicit shared code surface or
dependency edge and must each list the obligations they cover. The default and
the DeepSWE regression use one primary slice.

### Powdrr decides when work is complete

The worker's submit action means only "return control to Powdrr." Powdrr then
checks the observed diff and runs focused validation. A successful worker exit
with no attributable diff is `completed_without_progress`, not success.

### Artifacts are out-of-tree

All generated planning, trajectory, telemetry, request, receipt, and evidence
files must be written below the configured feature-run output root. They must
not be copied into the target checkout before Harbor captures `model.patch`.

Committed product documentation requested by the feature is different from a
generated run artifact. Only an explicit implementation obligation may permit
such documentation in the candidate diff.

## Ownership boundaries

| Component | Owns | Must not own |
| --- | --- | --- |
| Structrr | Repository facts, active intent, verification contracts | Worker loop or completion |
| Procedrr | Ordered phases, branches, bounded retries, terminal gates | Provider event parsing |
| Workrr | Worker requests, budgets, supervisor, diff attribution, validation and artifacts | Reinterpreting user intent |
| mini-SWE-agent | Model turns, one shell action, command observation, trajectory serialization | Scope, durable success, repair policy |
| Coding model | Product and test edits within the supplied boundary | Plans, IDs, selectors, completion status, publication |
| Harbor | Task checkout, outer timeout, artifact collection, benchmark evaluation | Feature decomposition or inner retries |

## Target control flow

```text
validated canonical design and verification obligations
    |
    v
compile one concise CodingWorkPacket
    |
    v
capture clean candidate baseline and enforce artifact isolation
    |
    v
bounded primary coding session
    |
    v
classify exit + attributable diff
    |
    +---- no useful diff / policy failure ----> terminal worker failure
    |
    v
collect required test names and run focused validation
    |
    +---- pass ----> run final repository validation
    |
    +---- fail ----> compile current failure clusters
                         |
                         v
                   bounded targeted repair
                         |
                         +----> recapture diff -> rerun affected validation
    |
    v
final obligation evidence, scope review, and patch sanitation
    |
    v
Harbor returns a product/test-only candidate patch for evaluation
```

## Domain model

Add provider-neutral records under `src/powdrr_lift/workrr/`. Follow existing
frozen dataclass, `to_data`, `from_data`, schema-version, and fingerprint
patterns.

### Coding work packet

```python
@dataclass(frozen=True, slots=True)
class CodingWorkPacket:
    packet_id: str
    mode: Literal["primary", "repair"]
    feature_description: str
    obligations: tuple[CodingObligation, ...]
    expected_tests: tuple[ExpectedTest, ...]
    repository_facts: tuple[RepositoryFact, ...]
    allowed_paths: tuple[str, ...]
    focused_commands: tuple[CommandContract, ...]
    must_preserve: tuple[str, ...]
    non_goals: tuple[str, ...]
    base_commit: str
    starting_diff_fingerprint: str
    schema_version: str = "coding-work-packet-v1"
```

`CodingObligation` contains only the Powdrr-owned obligation ID, behavioral
description, acceptance criterion, and expected-test references. It does not
contain proposal internals or duplicated prose representations.

`ExpectedTest` contains:

- a Powdrr-owned ID;
- a name prefix such as `test_state_constructor_accepts_data`;
- the behavior the test must demonstrate;
- optional discovered test directory hints; and
- the obligation IDs it protects.

The expected name is a prefix. A worker may append a meaningful suffix. Workrr
must verify at least one collected test whose function name equals the prefix
or starts with `prefix + "_"`.

`RepositoryFact` is bounded and typed. Initial kinds are:

- `source_symbol`;
- `existing_test`;
- `validation_command`;
- `project_layout`; and
- `constraint`.

Do not include raw Structrr files. Compile only facts selected as relevant to
the packet.

### Budget

```python
@dataclass(frozen=True, slots=True)
class CodingBudget:
    model_call_limit: int
    wall_time_seconds: int
    command_time_seconds: int
    no_progress_call_limit: int
    repeated_action_limit: int
    output_bytes_limit: int
```

Default budgets:

| Mode | Model calls | Wall time | Command time | No-progress calls |
| --- | ---: | ---: | ---: | ---: |
| Primary | 30 | 1,800 seconds | 300 seconds | 5 |
| Repair | 10 | 600 seconds | 300 seconds | 3 |

The complete feature run has a separate global budget of 4,500 seconds,
leaving at least 900 seconds for final validation and Harbor overhead before
the current 5,400-second outer timeout. At most three repair sessions may run.
Budget exhaustion is terminal and persisted; it is not converted into another
retry.

### Material state and progress

```python
@dataclass(frozen=True, slots=True)
class CodingMaterialState:
    head: str
    durable_diff_fingerprint: str
    changed_paths: tuple[str, ...]
    collected_expected_tests: tuple[str, ...]
    focused_result_fingerprint: str | None
    focused_status: Literal[
        "not_run", "passed", "failed", "timed_out", "not_collected", "errored"
    ]
```

A turn makes material progress only when at least one of these changes:

- durable diff fingerprint;
- expected-test collection set; or
- focused validation result fingerprint or status.

Model text, streamed tokens, session heartbeats, file reads, searches, command
starts, and repeated command output are activity but not material progress.

### Action signature

Normalize each requested shell action into:

```text
command executable + normalized arguments + cwd + relevant environment keys
```

Hash the normalized action and the bounded command-output fingerprint. If the
same action/output pair occurs twice without material progress, append one
supervisor recovery observation requiring a different action. A third
occurrence terminates the session as `repeated_action`.

### Attempt result

Replace ambiguous success checks with a closed terminal classification:

- `completed_with_progress`;
- `submitted_without_progress`;
- `step_limit_exceeded`;
- `wall_time_exceeded`;
- `command_timed_out`;
- `no_material_progress`;
- `repeated_action`;
- `policy_denied`;
- `provider_failed`; and
- `cancelled`.

Persist the provider exit reason separately from Powdrr's classification.
Only `completed_with_progress` may advance to validation. It still does not
satisfy obligations until validation succeeds.

## Prompt contract

Render `CodingWorkPacket` into one compact prompt with exactly these sections:

1. `Original feature description`
2. `Required behaviors`
3. `Tests to add`
4. `Repository facts`
5. `Allowed paths`
6. `Focused commands`
7. `Preserve and avoid`
8. `Completion protocol`

The completion protocol says:

- inspect only what is needed to implement the packet;
- add the named tests and product behavior;
- use focused commands during development;
- do not run the whole suite;
- do not edit generated Powdrr artifacts;
- do not commit or publish; and
- submit control when the focused tests are expected to pass.

Do not include:

- proposal revisions;
- canonical design JSON;
- instruction ledgers;
- Structrr baselines or diffs;
- obligation-population reviews;
- fingerprints;
- internal artifact paths;
- previous model reasoning; or
- resolved repair findings.

A repair prompt is newly rendered from a repair packet. It includes only the
relevant obligations, current failing/missing test evidence, allowed paths,
current diff summary, and focused commands. It must not repeat the complete
primary packet.

## Customized mini-SWE-agent integration

### Dependency and pinning

Add mini-SWE-agent as a pinned runtime dependency using the repository's
existing packaging mechanism. Record both package version and supported
configuration schema in one module. Harbor installation must install the same
pinned version from the Powdrr distribution; it must not independently install
`latest`.

The implementation agent must verify the selected version's Python import
paths before coding. If the public API differs from this plan, add a narrow
adapter module rather than leaking version-specific imports across Workrr.

### Provider implementation

Replace the current generic CLI invocation in
`src/powdrr_lift/workrr/coding_agent.py` with a provider backed by a new module,
for example:

```text
src/powdrr_lift/workrr/coding_workers/
    __init__.py
    model.py
    supervisor.py
    minisweagent.py
    progress.py
    policy.py
```

Responsibilities:

- `model.py`: provider-neutral packet, budget, state, action, and result types;
- `progress.py`: Git/test state capture, fingerprints, and progress decisions;
- `policy.py`: allowed command/path validation and generated-artifact rules;
- `supervisor.py`: global/session budgets, repetition detection, terminal
  classification, telemetry, and validation handoff;
- `minisweagent.py`: mini-SWE-agent model/environment/agent adapters only.

Do not place workflow branching in `minisweagent.py`.

### Agent customization

Create a Powdrr-specific agent subclass or wrapper that:

1. checks Powdrr's budget before every model call;
2. requests exactly one shell action per turn;
3. checks command policy before execution;
4. executes through a process-group-safe environment timeout;
5. captures material state after the action;
6. records the action/output signature and progress decision;
7. injects at most one no-progress recovery observation;
8. terminates on repeated action, no progress, step limit, or wall limit; and
9. writes the trajectory atomically after every turn.

Do not depend solely on mini-SWE-agent's submission command. Catch its submitted
exit and translate it into a provider exit reason; the supervisor still
classifies repository progress.

### DeepInfra configuration

Continue accepting only `DEEPINFRA_API_TOKEN` or `DEEPINFRA_API_KEY` from the
Harbor environment. Normalize it inside Powdrr. Select the pinned DeepInfra
model from Powdrr configuration and pass it to the mini-SWE-agent model adapter.
Do not require Harbor tasks to know LiteLLM or mini-SWE-agent-specific variable
names.

Preserve existing model override support for experiments. Persist the resolved
provider, model, package version, model-call limit, and wall budget in run
metadata without persisting the secret.

### OpenCode containment during migration

Keep `OpenCodeProvider` behind the same provider-neutral interface until the
comparison rollout is complete. Add these containment measures immediately:

- generate an isolated OpenCode configuration with an explicit finite `steps`;
- retain the external process-group absolute deadline;
- classify repeated tool calls and no-diff activity in the shared supervisor;
- never let OpenCode transport events reset material-progress state; and
- do not make OpenCode the Harbor default after mini-SWE-agent passes the live
  acceptance gate.

OpenCode-specific event parsing remains diagnostic. It must not affect
provider-neutral success semantics.

## Artifact isolation and diff attribution

### Clean candidate baseline

Immediately before the primary coding session, record:

- `HEAD`;
- staged diff;
- unstaged diff;
- untracked durable paths;
- candidate diff fingerprint; and
- allowed generated paths, which must all be outside the checkout.

For Harbor, the candidate repository must be clean at this boundary unless the
task intentionally begins with benchmark-provided changes. Any pre-existing
changes must be explicitly classified as input changes and excluded from
worker-attributed progress.

### Attempt-local delta

After every worker attempt, calculate the delta between the complete before and
after snapshots, including modifications to already-dirty files. A set
difference of changed path names is insufficient: editing a file that was
already dirty must still count as a new content transition.

Store content hashes for each changed path or hash the binary diff against the
attempt baseline. `allow_existing_changes` must never mean that an unchanged
pre-existing diff satisfies progress.

### Generated-path policy

Add a generated-path deny policy to the coding boundary and final patch gate.
Initial denied candidate paths include run-created files below:

- `docs/proposals/harbor-task/`;
- generated files below `docs/structrr/current/`;
- `.powdrr-lift/`;
- `.opencode-data/`;
- `.opencode-cache/`; and
- the configured feature-run output root if it is accidentally placed below
  the checkout.

Do not hardcode these names as the only future mechanism. Compile the deny set
from artifact ownership metadata and retain the initial list as a migration
guard. A user-authored feature explicitly targeting one of these paths must be
rejected or handled by a separate trusted workflow, not silently allowed.

### Patch sanitation gate

Before Harbor completion or PR creation:

1. compute the candidate patch from the recorded task input baseline;
2. assert at least one product or test path changed;
3. assert no generated artifact path appears;
4. assert every changed path is allowed by the implementation packet;
5. run `git diff --check`;
6. persist patch stats and changed-path classifications; and
7. block completion on any violation.

## Procedrr flow changes

Update `docs/procedrr/skill-definitions/implement-feature.yaml` while preserving
single-decision normal form.

### Remove obligation-per-agent iteration

The current code-task population may remain as planning/verification data, but
the flow must not execute `run_code_task_agent` once per obligation. Replace
that implementation loop with these phases:

1. `compile_coding_work_packet`
2. deterministic packet decisions, one predicate per decision
3. `capture_coding_baseline`
4. `run_primary_coding_worker`
5. deterministic attempt decisions, one predicate per decision
6. `collect_expected_tests`
7. `run_focused_obligation_validation`
8. `compile_repair_clusters`
9. bounded `for_each` over current repair clusters, maximum three
10. after each repair, rerun affected validation and recompute clusters
11. `run_final_obligation_evidence`
12. existing semantic and scope review
13. `sanitize_candidate_patch`

Each LLM decision still receives one simple question. All worker supervision,
budgeting, diff comparison, test collection, and status classification are
deterministic operations.

### Repair cluster rules

Compile clusters deterministically from current evidence:

- missing expected tests with the same target test directory may cluster;
- failing selectors sharing the same source path or traceback root may cluster;
- obligations that reference the same expected test may cluster;
- unrelated failures remain separate;
- resolved findings are never copied into a later cluster; and
- no more than three clusters may be dispatched in one run.

If more than three clusters remain, select the three with the greatest
obligation coverage, then terminate as `repair_budget_exhausted` if the next
validation still has failures. Do not silently drop the remainder.

### Gate corrections

Delete or replace the current postconditions that treat an attempt object's
presence and any nonempty worktree diff as success.

Required primary-attempt decisions:

- provider result classification is `completed_with_progress`;
- attempt-local durable diff is nonempty;
- changed paths are within scope;
- generated paths are absent;
- expected tests are collected after the attempt;
- `git diff --check` passes; and
- evidence was captured from the current candidate fingerprint.

Required repair-attempt decisions are the same, except an attempt may complete
without a new expected test when it changes product code and improves current
focused evidence. An unchanged focused result and unchanged diff is never
progress.

## Validation strategy

### Worker-time validation

The worker may run focused commands only. Workrr supplies exact command forms
discovered by validation bootstrap. Command policy must support safe selector
suffixes without permitting arbitrary shell composition.

Do not tell a worker to run the complete suite. If it attempts to do so, either
deny it or count the command against its command budget and terminate it on
timeout. Workrr owns full validation after focused checks pass.

### Expected-test collection

Implement provider-specific collection behind existing verification adapters.
For pytest, collect node IDs without running tests. Match the function portion
against the expected prefix. Distinguish:

- collected;
- missing;
- collection error; and
- duplicate/ambiguous matches.

At least one collected test must map to each actionable obligation before final
completion unless an obligation's accepted verification contract explicitly
uses another provider.

### Focused validation

Run only collected expected tests and any directly affected preservation tests.
Record per-selector statuses. `skipped`, `xfailed`, `deselected`, missing, and
timed out are failures, consistent with the verification-contract plan.

### Final validation

After focused evidence is green:

1. run all affected verification contracts;
2. run repository-required lint, formatting, and type checks;
3. run the repository's full test profile once;
4. rerun evidence collection if validation changed generated test artifacts;
5. perform final implementation completeness and scope reviews; and
6. sanitize and export the candidate patch.

## Configuration and compatibility

Add a typed coding-worker configuration rather than more unrelated CLI flags:

```python
@dataclass(frozen=True, slots=True)
class CodingWorkerConfig:
    provider: Literal["minisweagent", "opencode"]
    model: str
    primary_budget: CodingBudget
    repair_budget: CodingBudget
    global_wall_time_seconds: int
    repair_limit: int
```

Retain existing CLI and environment names as compatibility inputs, normalize
them once, and persist the resolved config. Add explicit flags only where an
operator needs experimentation:

- `--code-agent`;
- `--coding-model-call-limit`;
- `--coding-wall-time-seconds`;
- `--coding-repair-limit`; and
- `--coding-global-wall-time-seconds`.

Harbor defaults to the pinned mini-SWE-agent provider after rollout. Standard
feature flow uses the same default. OpenCode remains opt-in.

## Telemetry and artifacts

Write all artifacts below:

```text
<feature-run-root>/coding/
    config.json
    packet.json
    baseline.json
    attempts/<attempt-id>/
        request.json
        prompt.txt
        trajectory.json
        events.jsonl
        material-states.jsonl
        commands.jsonl
        result.json
        diff.patch
    validation/
        collection.json
        focused-<round>.json
        final.json
    repairs/
        round-<n>-clusters.json
    patch-sanitization.json
```

Every event includes monotonic and wall-clock timestamps. Every model turn
records model/provider, token usage when available, action signature, bounded
output fingerprint, material-state before/after, progress classification, and
remaining budget. Never persist API keys or complete inherited environments.

Harbor must copy this directory to trial artifacts even on timeout,
cancellation, or provider failure.

## Implementation sequence

Each phase below should be a separate pull request. Do not switch the default
provider until the earlier correctness phases are merged and the live gate
passes.

### PR 1: Correct attempt attribution and terminal gates

Goal: make the current OpenCode path incapable of falsely advancing.

Implementation:

1. Introduce full before/after candidate snapshots and attempt-local diff
   fingerprints.
2. Remove the `allow_existing_changes` shortcut that accepts an unchanged
   existing diff as progress.
3. Make code-task postconditions inspect the typed attempt status and
   attempt-local delta.
4. Require `completed_with_progress` before validation.
5. Add generated-path detection and final patch sanitation.
6. Ensure a failed checkpoint terminates the code-task flow and cannot be
   overridden by the later receipt compiler.
7. Move run-generated proposal and Structrr outputs out of the target checkout
   for Harbor, or explicitly remove them before patch capture while preserving
   copies in the artifact root.

Expected touchpoints:

- `src/powdrr_lift/workrr/coding_agent.py`;
- `src/powdrr_lift/workrr/feature_endpoint.py`;
- `src/powdrr_lift/integrations/harbor/powdrr_agent.py`;
- `docs/procedrr/skill-definitions/implement-feature.yaml`;
- `tests/test_coding_agent.py`;
- `tests/test_feature_endpoint.py`; and
- Harbor integration tests.

Required tests:

- timeout plus pre-existing diff cannot pass;
- completed no-op plus pre-existing diff cannot pass;
- editing an already-dirty allowed file is detected as new progress;
- failed task checkpoint forces terminal failure;
- generated-only patch is rejected;
- product/test patch is accepted;
- artifact files remain available after rejection; and
- DeepSWE-shaped generated files cannot enter `model.patch`.

Exit gate: replaying the failed run's statuses and diffs produces a terminal
failure before task 2, with a diagnostic naming the timeout and lack of
attributable product change.

### PR 2: Shared material-progress supervisor and global budget

Goal: bound all coding providers by useful work rather than transport activity.

Implementation:

1. Add the provider-neutral models and `BoundedCodingSupervisor`.
2. Capture material state after every completed worker action when the provider
   exposes actions, and at a bounded polling interval otherwise.
3. Add action/output repetition detection.
4. Add primary, repair, and global budgets.
5. Translate provider exits into the closed attempt classifications.
6. Persist complete budget/progress telemetry.
7. Make OpenCode run under the supervisor with finite configured steps and an
   external process-group deadline.

Required tests:

- heartbeats and model text do not reset material progress;
- different reads without a diff do not count as progress;
- a new diff fingerprint counts once;
- repeated command/output triggers one recovery then termination;
- absolute and global deadlines kill the process group;
- budget exhaustion cannot start another repair;
- partial artifacts survive every terminal classification; and
- event storms terminate at the deterministic budget.

Exit gate: a fake provider emitting infinite unique events but no repository
change terminates as `no_material_progress` within the configured call/wall
budget.

### PR 3: Powdrr-specific mini-SWE-agent provider

Goal: replace the generic `mini` subprocess wrapper with a controlled Python
integration.

Implementation:

1. Pin mini-SWE-agent and its required adapter dependencies.
2. Add the version-isolation adapter module.
3. Implement the Powdrr agent/environment integration described above.
4. Integrate DeepInfra model and credential normalization.
5. Enforce one command per turn, command policy, process-group timeouts, and
   atomic trajectories.
6. Map submit, limit, timeout, format, provider, and cancellation exits into
   provider-neutral results.
7. Update Harbor installation and verification.

Required tests:

- exact model and DeepInfra credential mapping without secret persistence;
- step, wall, command, and output limits;
- process-group cleanup;
- malformed model action recovery is bounded;
- trajectory survives timeout and exception;
- allowed focused command executes;
- unapproved command and publication command are denied;
- target checkout is the only editable root; and
- provider result is classified from observed state, not submitted text.

Add an opt-in live smoke test using a tiny repository and DeepInfra. It must add
one named test and implementation, terminate within ten minutes, and leave a
product/test-only patch.

Exit gate: the live smoke passes twice consecutively and both trajectories show
bounded termination.

### PR 4: Cohesive primary implementation and validation-derived repair

Goal: eliminate obligation-per-worker execution.

Implementation:

1. Add `CodingWorkPacket` compilation from the canonical obligations and
   verification plans.
2. Render the concise prompt contract.
3. Replace the Procedrr code-task implementation loop with one primary worker
   phase.
4. Add expected-test collection and focused validation.
5. Add deterministic failure clustering and at most three repair rounds.
6. Recompute failures after every repair; never replay resolved findings.
7. Preserve obligation-to-test and obligation-to-diff traceability in evidence.

Required tests:

- 35 obligations compile into one primary packet and one worker invocation;
- every obligation remains represented in packet and final evidence;
- packet excludes all forbidden internal representations;
- expected test prefixes permit meaningful suffixes;
- missing, xfailed, skipped, and deselected expected tests fail;
- related failures cluster and unrelated failures remain separate;
- repairs receive only current failures;
- a repair that makes no progress terminates without another identical repair;
- full validation runs only after focused validation passes; and
- normal and Harbor wrappers invoke the same core flow.

Exit gate: a deterministic DeepSWE fixture reaches final validation using one
primary invocation and no more than three repair invocations.

### PR 5: Live DeepSWE comparison and default switch

Goal: prove that the new path completes useful benchmark work before making it
the default.

Implementation:

1. Add a documented A/B command that runs the same task with OpenCode and the
   customized mini-SWE-agent using the same model and compiled packet.
2. Preserve all trajectories and evaluator artifacts.
3. Run the complete `python-statemachine-state-data-scoping` task at least three
   times with mini-SWE-agent.
4. Diagnose and fix deterministic harness failures before changing defaults.
5. Switch Harbor and standard feature defaults only after the acceptance gate
   below passes.

Acceptance gate:

- every run terminates before 4,500 seconds;
- no run artifact is present in the candidate patch;
- every run changes at least one product file and one required test file;
- all expected tests are collected;
- no attempt exceeds its step or wall budget;
- the complete pre-existing suite has no regression;
- at least two of three runs pass the hidden feature evaluator; and
- a failed run produces a terminal, actionable classification rather than an
  outer Harbor timeout.

If the model cannot meet the quality gate, do not weaken completion criteria.
Keep the bounded worker and test another model. Harness correctness and model
capability are separate concerns.

## End-to-end regression fixture

Add the complete DeepSWE state-data-scoping instructions as a versioned test
fixture. The test must exercise production packet compilation and the real
Procedrr definition. It must assert:

1. nonactionable clauses do not enter the coding packet;
2. all actionable obligations do enter it;
3. compound instruction sentences remain decomposed into their separate
   obligations;
4. one primary worker request is produced;
5. required test prefixes cover every actionable obligation;
6. no internal proposal/Structrr artifact is present in the worker prompt;
7. generated planning files cannot satisfy diff progress;
8. a timeout cannot satisfy any code-task receipt;
9. validation failures produce bounded, current repair clusters; and
10. final completion requires collected passing tests and a sanitized patch.

The deterministic test uses scripted provider actions and real Git operations.
The opt-in live test uses DeepInfra and the pinned model. The deterministic test
must run in normal CI; the live test must retain artifacts and be runnable from
`BENCHMARKS.md`.

## Verification requirements for every PR

Before opening each PR, run:

- the focused tests added by that PR;
- the complete test suite;
- `ruff format --check`;
- repository linting;
- repository type checks; and
- the Procedrr definition validator.

Any PR changing Harbor installation must also build the Harbor image and run a
small local task through Pier. Any PR changing the worker prompt or loop must
run the opt-in live smoke when credentials are available and attach the artifact
path to the PR description.

## Migration and rollback

1. Merge correctness gates before adding or selecting a new provider.
2. Keep `--code-agent opencode` and `POWDRR_CODE_AGENT=opencode` available during
   comparison.
3. Do not maintain two completion semantics. Both providers use the same
   supervisor and result types.
4. If mini-SWE-agent integration regresses, switch the provider default back
   without reverting correctness gates, artifact isolation, cohesive task
   execution, or validation-driven repair.
5. Remove the legacy generic mini CLI adapter after one release with the new
   provider as default and no supported caller depending on it.
6. Consider removing OpenCode from the Harbor image only after benchmark and
   standard feature flows have used the new default successfully for a full
   release cycle.

## Risks and mitigations

### The primary packet is still too large

Measure rendered characters and token estimates. Keep only one representation
of each obligation and repository fact. If the configured bound is exceeded,
compile no more than five code-surface slices; do not return to one process per
obligation.

### A model writes weak or tautological tests

Required test names prove presence, not semantic quality. Preserve the existing
semantic obligation review and hidden benchmark evaluation. Add deterministic
guards against empty tests, unconditional passes, all-mocked product behavior,
and test deletion, but do not pretend syntax alone proves quality.

### Focused commands differ by repository

Use validation bootstrap and provider adapters. The supervisor consumes typed
command contracts and must not hardcode pytest as the only framework.

### mini-SWE-agent API churn

Pin the dependency and isolate imports in one adapter module. Cover the adapter
with contract tests. Do not let its native trajectory schema become Powdrr's
public result schema.

### Partial changes are useful after timeout

Persist them and classify them, but do not mark the attempt successful. A
subsequent repair may start from the partial diff only when deterministic diff
and policy checks accept it and the repair packet explicitly records that
baseline.

### Strong limits reduce solve rate

Budgets may be tuned through resolved configuration and benchmark evidence.
They must always remain finite and below the global Harbor budget. Never trade
termination correctness for an unbounded solve attempt.

## Definition of done

This plan is fully implemented only when all of the following are true:

- Powdrr cannot accept a timed-out, failed, policy-denied, or no-progress coding
  attempt;
- pre-existing or generated diffs cannot satisfy attempt-local progress;
- the final patch cannot contain run-generated planning or telemetry files;
- one coherent feature produces one primary coding session by default;
- mini-SWE-agent runs through a Powdrr-controlled, pinned, bounded Python
  integration;
- progress is based on diff and validator state;
- repeated actions and event storms terminate deterministically;
- repair requests derive only from current validation failures and are bounded;
- required tests are collected and passed before final completion;
- all final obligation, preservation, scope, and patch-sanitization gates pass;
- normal and Harbor flows share the same implementation core;
- the DeepSWE regression fixture passes in CI;
- the live DeepInfra smoke terminates reliably; and
- at least two of three full state-data-scoping runs pass the hidden evaluator
  before mini-SWE-agent becomes the default.

Until those conditions hold, a run that merely emits model activity or a large
patch is not evidence that the coding system works.
