# Bounded Coding Worker Implementation Plan

Status: proposed

## Purpose

This document defines an implementation-ready redesign of Powdrr's coding
worker boundary. It is written for another implementation agent and makes the
architectural decisions that should not be rediscovered while coding.

The redesign keeps intent interpretation, workflow control, validation, and
completion authority in Structrr/Procedrr/Workrr. mini-SWE-agent is the single
target repository editor. One completed design compiles one prompt and starts
one mini-SWE-agent invocation. OpenCode, per-obligation sessions, automatic
continuations, and validation-derived repair prompts are outside the target
architecture.

The governing rule is:

> Model activity is not progress, a worker exit is not success, and an existing
> dirty diff is not evidence that the current work unit changed anything.

This plan complements `docs/plans/bounded-llm-instruction-compiler.md`,
`docs/design/source-anchored-semantic-contract-compilation.md`, and
`docs/plans/intent-linked-verification-implementation-plan.md`. The
source-anchored design is normative for instruction disposition, semantic
dimensions, cross-contract interactions, contrast cases, and prompt
projection. This plan must not accept merely present obligations and test
cases; it begins only after their source conservation and semantic-boundary
gates pass.

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

A later near-complete mini-SWE-agent implementation exposed the complementary
quality failure: 67 focused state-data tests passed and five failed because the
implementation conflated an ancestor-merged callback view with each state's
owned public snapshot and conflated an absent data declaration with explicit
`data={}`. The generated design also contained a lifecycle case whose setup
mutated data while its oracle incorrectly required values to remain unchanged.
These are not worker-liveness failures. They are design-to-prompt semantic-loss
failures and are equally normative regression cases for this plan.

## Goals

The completed system must:

1. Finish, fail, or produce a resumable partial result within a deterministic
   global coding budget smaller than Harbor's agent timeout.
2. Dispatch exactly one coherent implementation prompt for the complete
   feature and never issue a second coding prompt in that run.
3. Keep semantic obligations as a complete verification checklist without
   turning each obligation into an independent coding session.
4. Measure progress from repository and validator state, not transport events.
5. Make a timed-out, failed, policy-denied, or no-progress attempt incapable of
   satisfying an implementation gate.
6. Attribute every accepted diff transition to one worker attempt relative to
   that attempt's exact starting state.
7. Keep generated Powdrr, Structrr, Procedrr, and coding-agent artifacts outside
   the candidate repository diff.
8. Give the coding model one concise rendered prompt containing only the
   objective, actionable obligations, expected verification, repository facts,
   allowed scope, focused commands, and preservation constraints.
9. Preserve full worker trajectories, command results, diff fingerprints,
   liveness decisions, and budget consumption for debugging.
10. Support the same coding-worker contract in standard feature and Harbor
    flows.
11. Make mini-SWE-agent the fixed implementation target after design; model
    choice inside mini-SWE-agent remains configuration.
12. Prove the one-prompt, one-invocation behavior with deterministic tests and a live DeepInfra opt-in
    regression using the complete state-data-scoping task.
13. Preserve every source disposition and meaning-bearing semantic field
    through exact prompt ranges and private evidence mappings.
14. Identify contracts that are individually correct but easy to conflate,
    render their difference explicitly, and validate them with discriminating
    contrast cases.
15. Exclude process-only instructions only after a separate fail-closed safety
    decision, while preserving product non-goals and mixed clauses.

## Non-goals

This redesign does not:

- move design, intent classification, planning, review, or completion authority
  into the coding agent;
- replace the bounded instruction compiler or verification-contract work;
- let the worker choose its own budget, validation profile, allowed paths, or
  success criteria;
- require one coding session per sentence or obligation;
- issue a continuation, repair, or fallback coding prompt after the one
  invocation starts;
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

Powdrr must own a small `BoundedCodingSupervisor`. The supervisor owns prompt
shape, budgets, material-progress detection, validation transitions, and
terminal classification.

Use mini-SWE-agent through its Python API for:

- model/provider adaptation;
- one-action-per-turn command generation;
- environment command execution;
- command process-group timeout handling;
- message/observation formatting; and
- trajectory serialization.

Do not fork mini-SWE-agent initially. Pin a compatible release and subclass or
compose its documented `DefaultAgent`, model, and environment interfaces. Keep
Powdrr-owned records independent of mini-SWE-agent's internal schemas so a
future architecture revision does not require rewriting durable artifacts;
this isolation is not runtime provider selection or fallback authority.

### Obligations compile into one prompt

The canonical obligation list remains one record per requested behavior. The
prompt compiler renders every actionable obligation, verification case,
preservation rule, and non-goal exactly once into one feature prompt. One
mini-SWE-agent session implements the coherent feature.

If the prompt exceeds configured limits, prompt compilation fails before
implementation. Deterministic compaction may remove duplicate views and
irrelevant repository facts, but Powdrr must not split the feature across
worker sessions or omit mandatory intent.

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
| Workrr | Prompt compilation, budgets, supervisor, diff attribution, validation and artifacts | Reinterpreting user intent or generating follow-up prompts |
| mini-SWE-agent | Model turns, shell actions, command observation, edits, and trajectory serialization from one prompt | Scope, durable success, retry, or repair policy |
| Coding model | Product and test edits within the supplied boundary | Plans, IDs, selectors, completion status, publication |
| Harbor | Task checkout, outer timeout, artifact collection, benchmark evaluation | Feature decomposition or inner retries |

## Target control flow

```text
validated canonical design and verification obligations
    |
    v
compile private obligation-validation-manifest-v1
    |
    v
compile one immutable minisweagent-implementation-prompt-v1
    |
    v
prove prompt/manifest obligation and case parity
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
collect required tests and run focused validation
    |
    +---- pass ----> run final repository validation
    |
    +---- fail ----> terminal failed implementation result
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

### mini-SWE-agent implementation prompt

```python
@dataclass(frozen=True, slots=True)
class MiniSWEAgentImplementationPrompt:
    prompt_id: str
    design_revision: str
    repository_inventory_fingerprint: str
    rendering_revision: str
    prompt: str
    contract_refs: tuple[str, ...]
    semantic_dimension_refs: tuple[str, ...]
    interaction_refs: tuple[str, ...]
    contrast_case_refs: tuple[str, ...]
    verification_case_refs: tuple[str, ...]
    allowed_paths: tuple[str, ...]
    focused_commands: tuple[CommandContract, ...]
    base_commit: str
    schema_version: str = "minisweagent-implementation-prompt-v1"
```

The compiler accepts canonical obligations, verification cases, repository
facts, preservation constraints, and non-goals as inputs, then renders them
into `prompt`. Those source records remain separate provenance artifacts; they
are not fields mini-SWE-agent can inspect. The reference tuples prove coverage
without exposing internal IDs in the rendered prompt.

### Private obligation validation manifest

```python
@dataclass(frozen=True, slots=True)
class ObligationValidationManifest:
    manifest_id: str
    design_revision: str
    prompt_id: str
    prompt_fingerprint: str
    base_commit: str
    source_disposition_receipt_refs: tuple[str, ...]
    semantic_dimension_refs: tuple[str, ...]
    interaction_refs: tuple[str, ...]
    contrast_case_refs: tuple[str, ...]
    entries: tuple[ObligationValidationEntry, ...]
    preservation_case_refs: tuple[str, ...]
    full_validation_profiles: tuple[str, ...]
    schema_version: str = "obligation-validation-manifest-v1"
```

Each `ObligationValidationEntry` binds one actionable obligation to:

- its immutable source and contract references;
- an executable, static, artifact, or human-observation verification mode;
- whether a durable repository test must be added, modified, or may use proven
  existing coverage;
- every required verification case and expected target;
- the precompiled scenario, operation, and oracle for each target;
- the baseline expectation and allowed failure kinds;
- relevant symbols and paths used to select diff evidence; and
- the exact evidence kinds required for acceptance.

Executable verification is mandatory for behavioral obligations unless a
typed design-time exemption names replacement evidence and passes semantic
review. The prompt and manifest are compiled atomically from the same inputs,
but only the prompt is given to mini-SWE-agent.

`ExpectedTest` contains:

- a Powdrr-owned ID;
- a name prefix such as `test_state_constructor_accepts_data`;
- the scenario, operation, and oracle the test must demonstrate;
- its baseline expectation and allowed failure kinds;
- optional discovered test directory hints; and
- the obligation IDs it protects.

The expected name is a target contract rather than proof. A worker may append a
meaningful suffix, but Workrr must resolve exactly one collected target, bind it
to the manifest entry, and validate its behavior. Name matching by itself never
satisfies an obligation.

`RepositoryFact` is bounded and typed. Initial kinds are:

- `source_symbol`;
- `existing_test`;
- `validation_command`;
- `project_layout`; and
- `constraint`.

Do not include raw Structrr files. Compile only facts selected as relevant to
the prompt.

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

The one implementation invocation initially receives 40 model calls, 2,700
seconds wall time, 300 seconds per command, and five consecutive no-progress
calls. The complete feature run has a 4,500-second global budget, leaving at
least 1,800 seconds for final validation and Harbor overhead before the current
5,400-second outer timeout. Budget exhaustion is terminal and persisted; it
does not start a continuation or repair invocation.

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

Render the complete canonical design into one
`MiniSWEAgentImplementationPrompt` whose `prompt` field has exactly these
sections:

1. `Objective`
2. `Repository starting point`
3. `Required behavior`
4. `Required verification`
5. `Preserve and avoid`
6. `Allowed scope`
7. `Focused commands`
8. `Completion protocol`

The completion protocol says:

- inspect only what is needed to implement the prompt;
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
- previous model reasoning;
- validation findings from another attempt; or
- instructions to wait for a later repair turn.

The prompt is the sole worker-facing output of design. It is persisted before
invocation and its exact bytes are passed to mini-SWE-agent once. The private
validation manifest remains with Powdrr, and no other coding prompt exists in
the run.

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

- `model.py`: durable prompt, budget, state, action, and result types;
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

### One target worker

The feature flow invokes mini-SWE-agent only. OpenCode compatibility may remain
in lower-level diagnostic commands during migration, but it is not a target of
the completed design flow and is never an implementation fallback.

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
4. assert every changed path is allowed by the implementation prompt;
5. run `git diff --check`;
6. persist patch stats and changed-path classifications; and
7. block completion on any violation.

## Procedrr flow changes

Update `docs/procedrr/skill-definitions/implement-feature.yaml` while preserving
single-decision normal form.

### Replace coding iteration with one invocation

The current code-task population may remain as planning/verification data, but
the flow must not execute `run_code_task_agent` once per obligation. Replace
that implementation loop with these phases:

1. `compile_obligation_validation_manifest`
2. deterministic validation-readiness decisions, one predicate per decision
3. `compile_minisweagent_implementation_prompt`
4. deterministic prompt completeness and prompt/manifest parity decisions
5. `capture_coding_baseline`
6. `run_minisweagent_once`
7. deterministic attempt decisions, one predicate per decision
8. `collect_expected_tests`
9. `run_candidate_obligation_cases`
10. `run_independent_obligation_probes`
11. `run_baseline_differential_cases`
12. `review_test_oracle_alignment`
13. for each obligation: `review_implemented_obligation`
14. `finalize_obligation_validation_receipts`
15. existing preservation and scope review
16. `sanitize_candidate_patch`

Each LLM decision still receives one simple question. All worker supervision,
budgeting, diff comparison, test collection, and status classification are
deterministic operations.

### Terminal validation failures

Missing tests, failing selectors, scope violations, and incomplete obligation
evidence terminate the run. Persist exact findings for diagnosis and possible
future design input. Do not cluster them into additional worker requests.

### Gate corrections

Delete or replace the current postconditions that treat an attempt object's
presence and any nonempty worktree diff as success.

Required attempt decisions:

- provider result classification is `completed_with_progress`;
- attempt-local durable diff is nonempty;
- changed paths are within scope;
- generated paths are absent;
- expected tests are collected after the attempt;
- `git diff --check` passes; and
- evidence was captured from the current candidate fingerprint; and
- the invocation count for the run is exactly one.

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
For pytest, collect node IDs without running tests. Resolve each manifest target
contract to exactly one selector using the expected path, function prefix, test
scenario, and obligation mapping. Distinguish:

- collected;
- missing;
- collection error; and
- duplicate/ambiguous matches.

At least one collected executable test must map to each actionable behavioral
obligation before final completion unless its accepted manifest entry contains
a typed exemption and replacement evidence. One test may map to multiple
obligations only when the manifest identifies a distinct assertion, parameter,
or observable predicate for every mapping.

Enforce the manifest's durable-test disposition against the candidate diff:
`add` requires a new collected test, `modify` requires an attributable change
to the resolved test, and `existing_proven` requires fresh source and execution
evidence for the exact mapped assertion. An unrelated existing passing test
cannot be rebound after coding merely because its name is similar.

### Focused validation

Run only collected expected tests and any directly affected preservation tests.
Record per-selector statuses. `skipped`, `xfailed`, `deselected`, missing, and
timed out are failures, consistent with the verification-contract plan.

For each new-behavior case, create an isolated shadow worktree containing the
pre-implementation product baseline and candidate-authored test changes. Run
the resolved selector there. The normal acceptance pattern is:

- candidate implementation plus candidate test: pass; and
- baseline implementation plus candidate test: fail for a manifest-approved
  reason.

A test that passes in both trees is non-discriminating and cannot prove new
behavior. A collection, import, or symbol-missing baseline failure is accepted
only when that failure kind was declared before coding. Existing preservation
tests can be non-discriminating because their role is to prove no regression,
not the newly requested behavior.

When the language adapter can materialize a verification case directly from
its typed fixture, operation, and oracle bindings, run that Workrr-owned probe
from the artifact area against the candidate. Do not place it in the worker's
editable checkout or reveal its source in the implementation prompt. The probe
does not replace the durable repository test; it supplies independent evidence
that mini-SWE-agent did not define both the behavior and the only assertion
used to accept it.

### Per-obligation evidence review

Workrr builds one bounded evidence packet per manifest entry from:

- immutable source proposition and resolved contract;
- precompiled verification scenario and oracle;
- collected test source and exact assertion or parameter mapping;
- candidate and baseline execution evidence;
- independent probe evidence when the adapter supports it;
- relevant implementation hunks selected by subject/path closure; and
- applicable preservation and static-analysis results.

Deterministic adapters first decide collection, execution, differential, scope,
and known oracle-shape predicates. When semantic judgment remains, Procedrr
asks exactly one read-only question: “Does this implementation and verification
evidence satisfy this one obligation?” The response is only `pass`, `fail`, or
`abstain` plus a bounded explanation. Workrr adds the obligation identity and
evidence references and persists one
`obligation-validation-receipt-v1`. `abstain` fails closed.

Every required evidence predicate and every obligation receipt must pass.
Powdrr does not average verdicts or allow a whole-feature review to override a
failed obligation.

### Final validation

After focused evidence is green:

1. finalize all per-obligation validation receipts;
2. run all affected preservation and non-goal contracts;
3. run repository-required lint, formatting, and type checks;
4. run the repository's full test profile once;
5. rerun evidence collection if validation changed generated test artifacts;
6. perform final completeness and unmapped-diff scope reviews; and
7. sanitize and export the candidate patch.

## Configuration and compatibility

Add a typed coding-worker configuration rather than more unrelated CLI flags:

```python
@dataclass(frozen=True, slots=True)
class CodingWorkerConfig:
    model: str
    budget: CodingBudget
    global_wall_time_seconds: int
```

Retain existing CLI and environment names as compatibility inputs, normalize
them once, and persist the resolved config. Add explicit flags only where an
operator needs experimentation:

- `--coding-model-call-limit`;
- `--coding-wall-time-seconds`;
- `--coding-global-wall-time-seconds`.

Harbor and the standard feature flow use the pinned mini-SWE-agent provider.
The completed design flow has no coding-provider selector.

## Telemetry and artifacts

Write all artifacts below:

```text
<feature-run-root>/coding/
    config.json
    minisweagent-prompt.json
    minisweagent-prompt.txt
    obligation-validation-manifest.json
    validation-readiness-receipt.json
    baseline.json
    attempt/
        request.json
        trajectory.json
        events.jsonl
        material-states.jsonl
        commands.jsonl
        result.json
        diff.patch
    validation/
        collection.json
        independent-probes.json
        baseline-differential.json
        focused.json
        oracle-alignment.json
        obligations/
            <obligation-id>.json
        final.json
    patch-sanitization.json
```

Every event includes monotonic and wall-clock timestamps. Every model turn
records model/provider, token usage when available, action signature, bounded
output fingerprint, material-state before/after, progress classification, and
remaining budget. Never persist API keys or complete inherited environments.

Harbor must copy this directory to trial artifacts even on timeout,
cancellation, or provider failure.

## Implementation sequence

Each remaining phase below should be a separate pull request. Historical
OpenCode containment work may already be merged; the target state is always one
mini-SWE-agent prompt and invocation.

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
4. Add one invocation budget and one global run budget.
5. Translate provider exits into the closed attempt classifications.
6. Persist complete budget/progress telemetry.
7. Keep any compatibility worker under finite configured steps and an external
   process-group deadline until it is removed from the feature flow.

Required tests:

- heartbeats and model text do not reset material progress;
- different reads without a diff do not count as progress;
- a new diff fingerprint counts once;
- repeated command/output triggers one recovery then termination;
- absolute and global deadlines kill the process group;
- budget exhaustion cannot start a continuation or repair;
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

### PR 4: Semantic conservation and boundary compilation

Goal: make “close but not quite” interpretations structurally visible before a
worker prompt can be emitted.

Dependencies:

- immutable source propositions and atomic decomposition coverage;
- resolved source-anchored semantic contracts;
- repository inventory, ontology, and accepted-definition revisions; and
- language adapters capable of naming operations, views, lifecycle phases,
  fixture types, and observable predicates.

Implementation:

1. Add the versioned immutable schemas defined by the source-anchored design:
   `semantic-dimension-v1`, `semantic-conservation-row-v1`,
   `contract-interaction-v1`, `contrast-case-spec-v1`,
   `source-disposition-receipt-v1`, and `prompt-projection-map-v1`.
2. Add C12 nonactionable exclusion safety. A C01 `nonactionable` result cannot
   become prompt exclusion without a C12 `process_only` result. `mixed` returns
   to atomic decomposition, `product_semantics_present` returns to disposition
   classification, and unresolved blocks the design. Bind every process-only
   imperative to its registered Procedrr operation or policy; exclusion from
   product behavior must not mean ignoring a workflow requirement.
3. Compile one conservation row for every source proposition and every
   meaning-bearing field: polarity, quantifier, subject, operation, input,
   output, precondition, exception, explicit result, temporal scope,
   observation view, precedence, population, preservation rule, and non-goal.
4. Compile authority-backed semantic dimensions. Reject unproven partition
   values; route a required but underspecified value to clarification.
5. Generate bounded interaction candidates only from shared subject bindings,
   overlapping repository symbols, operation outputs, lifecycle resources, or
   explicit relationship edges. Persist the reason each pair was selected.
6. Resolve exact ontology relations mechanically. For unresolved candidates,
   invoke C13 once per pair and bind only its closed relation label.
7. Compile minimal discriminating contrast cases for distinct observations,
   ordered phases, precedence, mutual exclusion, and behaviorally different
   partition values. Include authority-backed plausible-but-wrong results.
8. Extend verification-case compilation with complete scenario, operation,
   oracle, expected result, forbidden result, semantic-dimension,
   interaction, and contrast references.
9. Extend design readiness so unresolved dispositions, uncovered conservation
   rows, unresolved interactions, or uncovered distinct partitions fail before
   any coding-worker request exists.
10. Persist all intermediate artifacts in the feature-run artifact root and
    expose them in live-validation summaries.

Expected touchpoints:

- `src/powdrr_lift/core/semantic_boundary.py`;
- `src/powdrr_lift/core/semantic_decision.py`;
- `src/powdrr_lift/core/semantic_contract.py`;
- `src/powdrr_lift/workrr/semantic_boundary_compiler.py`;
- `src/powdrr_lift/workrr/semantic_contract_compiler.py`;
- `src/powdrr_lift/workrr/verification_case_compiler.py`;
- design-interview and implement-feature Procedrr definitions;
- production design-flow fixtures; and
- artifact serialization and fingerprint tests.

Required deterministic tests:

- a pure pull-request instruction becomes a process-only exclusion receipt and
  binds the publication flow while appearing nowhere in product behavior;
- `Do not add retries` cannot be excluded as nonactionable and reaches a
  non-goal/preservation route;
- a mixed product/process sentence is re-split and every child proposition has
  one terminal disposition;
- every source proposition has exactly one terminal route and no actionable
  proposition can disappear;
- conditions, exceptions, output alternatives, temporal modifiers, and
  precedence fields each create conservation rows;
- an unsupported or underspecified partition blocks instead of being guessed;
- callback-effective data and owned snapshot data become
  `distinct_observations` with a contrast case;
- absent, explicit-empty, and nonempty declarations remain distinct;
- callback mutation persistence cannot compile into a value-immutability
  oracle;
- shallow and deep history receive separate variant coverage;
- non-dict declarations and non-string-key declarations remain independently
  attributable cases even though both raise the same exception;
- unrelated contracts do not produce quadratic contrast noise; and
- changing a source, ontology, inventory, adapter, or relation decision
  invalidates all dependent fingerprints.

Required mutation tests:

- flip `owned_state_snapshot` to `effective_callback_scope`;
- delete the absent-declaration partition;
- merge explicit-empty and absent declaration values;
- replace mutation persistence with equality before and after a callback;
- merge shallow and deep history cases;
- classify a product non-goal as process-only; and
- remove one modifier's conservation destination.

Every mutation must make design readiness fail with a finding naming the exact
contract, field, interaction, or source proposition. Snapshot differences
alone are not sufficient assertions.

Exit gate: the production DeepSWE fixture reaches design readiness only with
all known subtle distinctions represented by resolved interactions and
contrast cases. The same fixture with any required mutation fails before
worker invocation.

### PR 5: Terminal single-prompt compilation and invocation

Goal: eliminate obligation-per-worker execution.

Implementation:

1. Require a passing semantic-boundary readiness receipt from PR 4. The prompt
   compiler cannot accept legacy obligations or test descriptions that lack
   conservation, dimension, interaction, and contrast metadata.
2. Compile one private `obligation-validation-manifest-v1` from the complete
   canonical design, verification cases, baseline expectations, and evidence
   requirements.
3. Compile one `minisweagent-implementation-prompt-v1` from the same design,
   repository bindings, scope, and verification plans.
4. Render an `Interaction boundaries` section between required behavior and
   required verification. Render the complete scenario, operation, oracle,
   expected result, forbidden result, and contrast assertions for each case.
   Do not split free-form descriptions on marker text such as `Oracle:`.
5. Render the objective only from resolved actionable contracts. Never copy
   the raw feature text into the objective, because doing so can reintroduce
   process-only clauses and create an untracked parallel representation of
   intent.
6. Compile `prompt-projection-map-v1` while rendering. Record exact UTF-8 byte
   ranges and text fingerprints for every conservation row, interaction, and
   contrast case.
7. Add deterministic prompt/manifest parity and complete disposition,
   conservation, partition, interaction, obligation, and case coverage checks.
8. Replace every code-task, continuation, and repair implementation loop with
   one mini-SWE-agent phase.
9. Add exact expected-test collection, candidate execution, baseline
   differential execution, adapter-owned independent probes, and
   oracle-alignment validation.
10. Execute every required contrast case through an independent probe where an
    adapter exists. Require both positive observations and negative assertions
    against the recorded plausible conflation.
11. Produce one fail-closed validation receipt per obligation.
12. Make every attempt or validation failure terminal for the run.
13. Preserve source-to-contract, field-to-prompt-range,
    interaction-to-contrast, obligation-to-test, obligation-to-diff, and
    obligation-to-receipt traceability in evidence.

Required tests:

- 35 obligations compile into one prompt and one worker invocation;
- every obligation remains represented in the prompt, private manifest, and
  final evidence;
- every source proposition has one terminal disposition receipt;
- process-only text is absent from the objective and every worker-facing
  section;
- a product non-goal cannot be removed through nonactionable filtering;
- every condition, exception, explicit result, temporal modifier, observation
  view, and precedence rule maps to an exact verified prompt range;
- deleting or truncating an oracle fails prompt compilation even when the test
  description and selector remain present;
- replacing a mapped prompt fragment with semantically adjacent text fails its
  projection-map fingerprint and rendering-template test;
- every required interaction appears once in `Interaction boundaries` and
  every contrast appears once in `Required verification`;
- prompt and manifest obligation/case sets have exact parity;
- prompt and manifest interaction/contrast sets have exact parity;
- the prompt excludes all forbidden internal representations;
- expected test prefixes permit meaningful suffixes;
- missing, xfailed, skipped, and deselected expected tests fail;
- a candidate test that passes against the product baseline cannot prove a new
  behavior obligation;
- every adapter-materializable case runs through a Workrr-owned probe that the
  coding worker could not edit;
- `add`, `modify`, and `existing_proven` durable-test requirements are enforced
  against the attributable test diff;
- an oracle-misaligned or semantically irrelevant passing test fails its
  obligation receipt;
- one shared test cannot cover multiple obligations without distinct mapped
  assertions, parameters, or predicates;
- callback merged scope cannot satisfy owned-snapshot evidence;
- an explicit-empty declaration cannot satisfy the absent-declaration case;
- an equality-before/after assertion cannot satisfy callback mutation
  persistence;
- one generic history test cannot satisfy both shallow and deep restoration;
- every obligation receives an independent pass, fail, or abstain verdict and
  abstention fails closed;
- failed validation creates findings but no worker request;
- timeout, limit, and no-progress exits create no continuation request;
- full validation runs only after focused validation passes; and
- normal and Harbor wrappers invoke the same core flow.

Exit gate: a deterministic DeepSWE fixture reaches final validation using one
prompt and exactly one mini-SWE-agent invocation; each known subtle distinction
has a prompt range, a manifest mapping, an independent contrast where
materializable, and separately attributable evidence.

### PR 6: Live DeepSWE single-prompt proof

Goal: prove that the new path completes useful benchmark work before making it
the default.

Implementation:

1. Preserve the exact prompt, trajectory, and evaluator artifacts.
2. Assert one mini-SWE-agent process and one prompt fingerprint per run.
3. Run the complete `python-statemachine-state-data-scoping` task at least three
   times with mini-SWE-agent.
4. Diagnose and fix deterministic harness failures without adding follow-up
   worker prompts.
5. Retain and inspect source-disposition, conservation, dimension, interaction,
   contrast, projection-map, prompt, manifest, probe, and receipt artifacts for
   every run.

Acceptance gate:

- every run terminates before 4,500 seconds;
- every run invokes mini-SWE-agent exactly once;
- no run artifact is present in the candidate patch;
- every run changes at least one product file and one required test file;
- all expected tests are collected;
- no attempt exceeds its step or wall budget;
- the complete pre-existing suite has no regression;
- all semantic-boundary independent probes pass, including owned versus merged
  views and absent versus explicit-empty declarations;
- no run is accepted solely because broad tests pass while a mapped contrast
  assertion is missing;
- at least two of three runs pass the hidden feature evaluator; and
- a failed run produces a terminal, actionable classification rather than an
  outer Harbor timeout.

If the model cannot meet the quality gate, do not weaken completion criteria or
add repair prompts. Change the configured model for a new run or improve the
compiled design prompt. Harness correctness, prompt quality, and model
capability remain separate concerns.

## End-to-end regression fixture

Add the complete DeepSWE state-data-scoping instructions as a versioned test
fixture. The test must exercise production prompt compilation and the real
Procedrr definition. It must assert:

1. every source proposition has exactly one terminal disposition receipt;
2. C01 nonactionable clauses also pass C12 exclusion safety before they are
   omitted from the coding prompt;
3. product non-goals and mixed product/process clauses cannot disappear through
   nonactionable filtering;
4. all actionable obligations do enter the prompt;
5. compound instruction sentences remain decomposed into their separate
   obligations;
6. every condition, exception, result, temporal scope, observation view,
   precedence rule, and population modifier has a conservation row;
7. callback-effective merged data and per-state owned query data compile into
   distinct observations and a discriminating contrast case;
8. absent, explicit-empty, nonempty, and invalid declaration partitions remain
   distinct where their outcomes differ;
9. callback mutation persistence does not become value immutability;
10. shallow and deep history restoration remain separately verifiable;
11. non-dict and non-string-key invalid declarations remain separately
    attributable;
12. one mini-SWE-agent prompt and one worker invocation are produced;
13. the private validation manifest covers every actionable obligation with an
   executable case or reviewed typed exemption;
14. no internal proposal/Structrr artifact is present in the worker prompt;
15. the objective contains only resolved actionable product meaning and does
    not copy process-only source text;
16. prompt projection ranges cover every required conservation row,
    interaction, and contrast and match the rendered bytes;
17. prompt and manifest contract, case, interaction, and contrast references
    have exact parity;
18. generated planning files cannot satisfy diff progress;
19. a timeout cannot satisfy any code-task receipt;
20. validation failures produce durable terminal findings and no new prompt;
21. baseline-nondiscriminating and oracle-misaligned tests fail validation;
22. adapter-materializable cases run independent Workrr-owned probes;
23. OpenCode is never invoked; and
24. final completion requires a passing receipt for every obligation, passing
    global checks, and a sanitized patch.

Run mutation variants through the same production path. Each variant changes
one semantic fact while retaining IDs and structural shape: drop an oracle,
merge owned and callback views, remove the `None` outcome, merge absent and
empty declarations, invert persistence into immutability, merge shallow and
deep history, or mark a product non-goal process-only. Each variant must fail
at the earliest responsible gate with an exact finding. This prevents tests
that verify only schemas, counts, or snapshots from masking semantic loss.

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

1. Merge prompt completeness and one-invocation gates before removing legacy
   loops.
2. Remove coding-provider selection from the feature and Harbor design paths;
   diagnostic CLIs may retain compatibility temporarily.
3. Remove timeout continuation and validation repair dispatch from the feature
   flow.
4. Roll back by disabling the new feature flow or reverting its configuration,
   not by silently selecting OpenCode or adding another prompt.
5. Remove the legacy OpenCode and continuation paths after no supported caller
   depends on them.

## Risks and mitigations

### The single prompt is too large

Measure rendered characters and token estimates. Keep only one representation
of each obligation and repository fact. If the configured bound is exceeded,
fail prompt compilation with a coverage report. Improve deterministic
selection or require the design to be narrowed; do not split it into multiple
worker prompts.

### A model writes weak or tautological tests

Required test names prove presence, not semantic quality. Preserve the existing
semantic obligation review and hidden benchmark evaluation. Add deterministic
guards against empty tests, unconditional passes, all-mocked product behavior,
and test deletion, but do not pretend syntax alone proves quality. Execute
precompiled contrast cases through Workrr-owned probes and require exact
scenario/operation/oracle mappings. Candidate-authored tests supplement those
probes; they do not define the expected semantics.

### Boundary compilation creates invented edge cases

Every dimension value, relationship, forbidden result, and contrast must cite
an exact source span, accepted Structrr definition or invariant, repository API
contract, ontology rule, or language-adapter rule. If none resolves a necessary
distinction, suspend for clarification. Never turn model familiarity with a
framework into authoritative product behavior.

### Pairwise interaction analysis becomes quadratic

Generate pairs only from shared bindings, repository-symbol overlap, operation
output types, lifecycle resources, and explicit relationship edges. Persist
the candidate reason and test that unrelated contracts are not compared. Do
not cap candidates with a lossy numeric limit; partition the graph by connected
semantic component and process every justified edge.

### Focused commands differ by repository

Use validation bootstrap and provider adapters. The supervisor consumes typed
command contracts and must not hardcode pytest as the only framework.

### mini-SWE-agent API churn

Pin the dependency and isolate imports in one adapter module. Cover the adapter
with contract tests. Do not let its native trajectory schema become Powdrr's
public result schema.

### Partial changes are useful after timeout

Persist them and classify them, but do not mark the attempt successful. A
new run may explicitly use an accepted partial diff as its starting repository
state after design revalidation. The failed run does not continue and does not
receive another prompt.

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
- one coherent feature produces exactly one immutable prompt and one coding
  session;
- mini-SWE-agent runs through a Powdrr-controlled, pinned, bounded Python
  integration;
- progress is based on diff and validator state;
- repeated actions and event storms terminate deterministically;
- timeout, limit, no-progress, and validation failures create no continuation,
  repair, or fallback coding prompt;
- the prompt and private validation manifest have exact obligation and case
  parity;
- every source proposition has one terminal disposition receipt and no product
  meaning can be excluded without passing the nonactionable safety gate;
- every meaning-bearing field, semantic partition, and contract interaction
  has a verified prompt and evidence destination;
- prompt projection ranges prove that complete oracles and contrasts survived
  rendering without lossy description truncation;
- required tests are collected, candidate-passing, baseline-discriminating
  where applicable, and oracle-aligned before final completion;
- every actionable obligation has an independent passing validation receipt;
- all final obligation, preservation, scope, and patch-sanitization gates pass;
- normal and Harbor flows share the same implementation core;
- the DeepSWE regression fixture passes in CI;
- the DeepSWE regression fixture's known subtle distinctions pass independent
  contrast probes and all semantic mutation variants fail before invocation or
  acceptance, as appropriate;
- the live DeepInfra smoke terminates reliably; and
- at least two of three full state-data-scoping runs pass the hidden evaluator
  using one mini-SWE-agent invocation each.

Until those conditions hold, a run that merely emits model activity or a large
patch is not evidence that the coding system works.
