# Workflow Definition Iteration Plan

## Objective

Create a fast, repeatable way to improve skill and workflow definitions so LLM
confusion can be reproduced, measured, corrected, and prevented without running
an entire live workflow for every wording change.

The system should turn failures observed during real executions into durable
regression cases, support inexpensive prompt-only replay, and provide full
scenario runs when repository mutations are necessary. It should make a change
to a step definition reviewable in terms of behavior: fewer invalid actions,
fewer repairs, fewer unnecessary human questions, and reliable traversal of
required gates.

## Problems to solve

The current feedback loop is expensive and imprecise:

- Many instruction failures appear only after several live LLM calls and tool
  actions.
- Reproducing a failure often requires recreating a worktree, proposal, branch,
  workflow state, and provider configuration.
- Error logs identify individual failures but do not yet provide a portable
  execution fixture.
- A corrected sentence may fix one run while introducing ambiguity elsewhere.
- Final success does not reveal whether the agent took a confused or wasteful
  path to get there.
- Existing feature-specific harnesses are useful but duplicate setup and
  assertions that should be shared.
- Static validation catches malformed definitions but cannot determine whether
  an LLM understands the intended first action or completion condition.

## Scope

This plan covers:

- Interactive skills executed by workflow chat.
- Durable tasks executed by the workflow task agent.
- Skill definitions under `skill-definitions/`.
- Workflow templates and instantiated workflow task definitions.
- Deterministic pre-steps, gates, dynamic validation obligations, and step
  input/output handoffs.
- LLM action parsing, repair guidance, observer interventions, and trajectory
  analysis.

The first implementation does not automatically rewrite workflow definitions,
merge pull requests, or treat an LLM judge as authoritative. It produces
evidence and suggested changes; deterministic validation remains authoritative.

## Design principles

- Reproduce the smallest failing decision before rerunning a complete workflow.
- Retain the full execution evidence, but provide only relevant state to replay.
- Use deterministic assertions for action validity and workflow invariants.
- Use an LLM reviewer only for ambiguity and intent analysis that cannot be
  expressed deterministically.
- Separate definition quality from provider availability and GitHub state.
- Compare old and new definitions against the same recorded inputs.
- Treat the complete action trajectory as a test result, not only the final
  files.
- Make fixtures safe to commit by redacting credentials and configurable
  sensitive values.
- Keep real-provider tests opt-in; normal CI must be deterministic.
- Use the same execution contracts as production so harness-only behavior does
  not hide production defects.

## Target workflow

The desired iteration loop is:

```text
real failure or authored scenario
  -> capture a replay bundle
  -> reproduce the failed decision
  -> edit the skill/workflow definition
  -> run static checks and prompt-only replays
  -> run repository scenarios that need tools
  -> compare baseline and candidate metrics
  -> review the generated report
```

A developer should be able to run the common loop with one command:

```bash
powdrr-lift tune-workflow \
  --definition templates/execute-proposed-pr.yaml \
  --scenarios workflow-evals/execute-proposed-pr \
  --replay-errors workflow-llm-errors.jsonl \
  --report workflow-definition-report.json
```

## Architecture

### 1. Replay bundle

A replay bundle is an immutable record of one LLM decision boundary. It must
contain enough state to rebuild the exact action prompt and validate a new
response without recreating the complete execution.

Suggested location for committed fixtures:

```text
workflow-evals/
  replays/
    <skill-or-workflow>/
      <case-id>.yaml
```

Suggested schema:

```yaml
schema_version: 1
id: create-pr-untracked-file-001
execution_mode: execute_selected_skill
definition:
  kind: skill
  path: skill-definitions/create-pull-request.yaml
  name: create-pull-request
step:
  id: commit-validated-changes
  index: 3
root_intent: Create a pull request for the validated files.
input_state:
  files_to_publish:
    - src/example.py
current_state:
  repository_status: "?? diagnostic-output.txt"
  execution_context: []
  handoff_records: {}
  durable_facts: {}
recent_actions:
  - action: invoke_tool
    tool: git
    parameters:
      operation: commit
      message: Implement example
latest_result:
  returncode: 1
  stderr: Nothing to commit; untracked files remain.
failed_response:
  action: invoke_tool
  tool: git
  parameters:
    operation: commit
    message: Implement example
expected:
  valid_actions:
    - file_management
  forbidden_actions:
    - complete
    - next_step
  max_repairs: 0
redactions: []
```

The bundle stores structured state rather than a pre-rendered prompt. Prompt
construction must use the production prompt builder and the candidate
definition being evaluated.

### 2. Scenario definition

A scenario tests a complete or partial trajectory and may use a temporary Git
repository fixture. Scenarios are appropriate when prompt-only replay cannot
prove the desired behavior because actions must mutate files or satisfy gates.

Suggested layout:

```text
workflow-evals/
  execute-proposed-pr/
    fixtures/
      valid-proposal/
    scenarios/
      valid-existing-spec.yaml
      failed-test-repair.yaml
```

Suggested schema:

```yaml
schema_version: 1
id: execute-valid-existing-spec
definition: templates/execute-proposed-pr.yaml
execution_mode: workflow_task
fixture: ../fixtures/valid-proposal
inputs:
  proposed_pr_id: pr-example-core
  feature_id: example-feature
provider:
  mode: scripted
expect:
  outcome: complete
  visited_steps:
    ordered:
      - gather-proposed-pr-context
      - implement-change
      - run-tests
      - create-pull-request
  required_actions:
    - action: gather_context
      step: gather-proposed-pr-context
    - action: invoke_tool
      tool: git
      operation: commit
  forbidden_actions:
    - action: prompt_user
  required_files:
    - src/example.py
  forbidden_files:
    - agent_error.txt
  max_roundtrips: 20
  max_repairs: 0
  max_repeated_action_count: 0
  all_gates_passed: true
```

Provider modes:

- `scripted`: deterministic responses authored in the fixture; required for CI.
- `recorded`: replay provider responses from an existing execution.
- `live`: call a configured provider; opt-in and never required for ordinary CI.
- `compare`: execute the baseline and candidate definition with the same
  provider, model, seed when supported, and fixture.

### 3. Trajectory recorder

Add a shared trajectory event format used by workflow chat and workflow task
execution. It should normalize existing transcript, execution event, observer,
validation, and repair data into events such as:

```text
execution_started
step_entered
prompt_built
llm_response_received
action_parsed
action_rejected
action_executed
material_state_changed
gate_evaluated
step_transitioned
observer_triggered
human_prompted
execution_completed
execution_failed
```

Every event should include:

- Execution and case identity.
- Definition path and content hash.
- Skill, workflow, task, and step identity where applicable.
- Roundtrip number across the parent execution.
- Provider and model metadata without credentials.
- Action signature and result fingerprint.
- Material-state fingerprint.
- Validation issue code and path.
- Whether the event represented progress.

The recorder should derive replay bundles from these events without changing
normal execution behavior.

### 4. Replay runner

The replay runner rebuilds one production prompt using:

- The candidate definition.
- Recorded current-step state.
- Recorded latest result or error.
- Recorded handoff and durable facts.
- The production action schema and repair guidance.

It then performs one of two operations:

1. Validate a recorded response against a changed runtime.
2. Request a new response from a configured model and validate it.

CLI examples:

```bash
powdrr-lift workflow-replay \
  --bundle workflow-evals/replays/create-pull-request/untracked-file.yaml
```

```bash
powdrr-lift workflow-replay \
  --bundle workflow-evals/replays/create-pull-request/untracked-file.yaml \
  --definition skill-definitions/create-pull-request.yaml \
  --provider deepinfra-cheap
```

The default operation must not modify the repository, create worktrees, invoke
GitHub, or run shell commands.

### 5. Scenario runner

The scenario runner creates a temporary isolated repository, installs the
fixture, executes the production agent loop, and evaluates trajectory
assertions.

It must:

- Use an explicit temporary directory outside the primary checkout.
- Initialize deterministic Git identity and branches.
- Stub GitHub operations unless the scenario explicitly opts into integration
  behavior.
- Capture all file, Git, action, validation, and observer events.
- Restore no shared state because each scenario owns its temporary repository.
- Produce a machine-readable result and a short human summary.
- Retain failed fixture state only when requested with `--keep-failed`.

CLI example:

```bash
powdrr-lift workflow-scenario \
  --scenario workflow-evals/execute-proposed-pr/scenarios/failed-test-repair.yaml
```

### 6. Static definition analyzer

Extend definition validation with LLM-confusion checks. These checks should be
deterministic and return exact paths and corrective guidance.

Checks should include:

- Each step has one primary action or is split into separate steps.
- Every allowed action has a syntactically valid complete JSON example.
- Example action fields match the current runtime action schema.
- Tool examples match the declared tool and parameter contract.
- Every placeholder in details, examples, pre-steps, and tool invocations maps
  to a declared input or deterministic runtime value.
- Every output consumed by a later step is declared and produced.
- Completion conditions are explicit for freeform steps.
- `goto_step` targets exist and point backward.
- Gate retry targets point backward and cannot bypass another required gate.
- A step does not tell the LLM to invoke a deterministic pre-step again.
- A step does not permit `complete` when later gates remain.
- Global guidance and step guidance do not prescribe incompatible fields or
  actions.
- Terms for identifiers and paths are consistent across description, inputs,
  outputs, examples, and tool templates.
- Placeholders are not passed literally to executable tool actions.
- Human questions are reserved for genuinely unresolved inputs.

The analyzer should distinguish errors from warnings. Errors make the
definition invalid. Warnings identify ambiguity that should be reviewed but
may be intentional.

### 7. Prompt snapshots

Provide a deterministic command that renders the exact prompt for each step
using minimal synthetic state:

```bash
powdrr-lift render-workflow-prompts \
  --definition skill-definitions/create-pull-request.yaml \
  --output-dir workflow-evals/snapshots/create-pull-request
```

Snapshots should include:

- System guidance.
- Current-step contract.
- Allowed action schemas and examples.
- Deterministic context shape.
- Input/output handoff shape.
- Gate and recovery guidance.

Tests should compare normalized prompt snapshots so reviewers can see the exact
instruction delta in a pull request. Volatile provider metadata, timestamps,
absolute paths, and token estimates must be omitted.

### 8. Ambiguity reviewer

Use the existing high-reasoning provider role to review definitions only when
requested or when deterministic checks find a warning. The reviewer receives
one step plus compact parent context and answers a structured questionnaire:

```json
{
  "first_action": {
    "action": "gather_context",
    "parameters": {
      "feature_id": "<feature_id>"
    }
  },
  "completion_condition": "proposed-pr-context-state is populated",
  "allowed_actions": ["gather_context", "next_step"],
  "missing_information": [],
  "conflicts": [],
  "ambiguous_phrases": [],
  "confidence": 0.97
}
```

The review fails when independent reviews disagree about the first action,
completion condition, required parameters, or whether human input is needed.
The reviewer may suggest wording, but its result is advisory.

### 9. Baseline and candidate comparison

The comparison runner executes identical replay bundles or scenarios against
two definitions:

- Baseline: the merge base or explicitly supplied definition.
- Candidate: the working-tree definition.

Metrics:

- Valid first-action rate.
- Parse and schema repair count.
- Tool validation failure count.
- Repeated semantic action count.
- Roundtrips to material progress.
- Total roundtrips.
- Unnecessary human prompts.
- Gate traversal and pass rate.
- Successful completion rate.
- Prompt input tokens and output tokens.
- Observer calls and interventions.
- Changed-file correctness.

The report should mark behavioral regressions even if both definitions
eventually complete.

### 10. Failure clustering

Add a report that groups existing workflow LLM errors by:

- Definition and step.
- Error code and action type.
- Missing or invalid field.
- Repeated action signature.
- Validation issue fingerprint.
- Repair strategy.
- Provider and model.

Example summary:

```text
create-pull-request / commit-validated-changes
  7 repeated commit actions with untracked files
  4 invalid file-management payloads

specify-a-feature / fill-architecture-specification
  8 invalid yaml_edit paths
  3 validator reruns without a material repair
```

The report should link each group to replay bundle candidates. A command should
be able to promote selected failures into committed fixtures after redaction.

## Command surface

Add the following CLI commands incrementally:

```text
workflow-replay
    Rebuild and evaluate one recorded decision.

workflow-scenario
    Execute one isolated trajectory scenario.

validate-workflow-definition
    Run static confusion-oriented checks in addition to schema validation.

render-workflow-prompts
    Produce normalized step prompt snapshots.

analyze-workflow-errors
    Cluster diagnostic records and identify replay candidates.

tune-workflow
    Orchestrate static checks, replays, scenarios, comparisons, and reporting.
```

All commands should support JSON output. Human-readable output should lead with
the failing definition, step, assertion, and corrective action.

## Result format

Use a shared result document:

```json
{
  "schema_version": 1,
  "definition": "skill-definitions/create-pull-request.yaml",
  "definition_hash": "...",
  "status": "failed",
  "static_validation": [],
  "replays": [],
  "scenarios": [],
  "comparison": {
    "baseline": {},
    "candidate": {},
    "regressions": []
  },
  "failure_clusters": [],
  "summary": {
    "cases": 24,
    "passed": 21,
    "failed": 3,
    "valid_first_action_rate": 0.875,
    "repair_count": 4
  }
}
```

Reports belong in a caller-selected path and should not be staged
automatically. CI may upload them as artifacts.

## Error capture and redaction

Extend workflow error capture so each error contains a stable reference to:

- Parent execution ID.
- Definition hash.
- Step ID and index.
- Prompt-builder version.
- Current action schema version.
- Material-state fingerprint.
- Latest relevant action and result.
- Repair attempt number.
- Observer trigger and guidance, when present.

Before a record becomes a committed fixture:

- Remove API keys, authorization headers, cookies, and provider request IDs.
- Replace absolute worktree paths with `<repo-root>`.
- Allow repository-specific secret patterns to be configured.
- Record every replacement in the bundle's `redactions` field.
- Reject fixture creation if a credential-shaped value remains.

## Phased implementation

### Phase 1: Shared replay capture and prompt-only replay

#### Deliverables

- Versioned replay bundle schema and loader.
- Shared trajectory event types.
- Hooks in workflow chat and workflow task execution.
- `workflow-replay` command.
- Conversion from selected diagnostic log entries to replay bundles.
- Redaction and fixture validation.
- Unit tests for prompt reconstruction and action validation.

#### Detailed work

1. Define replay and trajectory dataclasses in a shared module.
2. Add stable serialization with explicit schema versions.
3. Record definition hashes and prompt-builder versions.
4. Extract the minimum state needed by production prompt builders.
5. Add a no-tool replay execution mode.
6. Validate recorded and newly generated responses through the production
   parser and current-step transition validators.
7. Add `--provider`, `--model`, and `--no-llm` modes.
8. Add a redaction pass and fixture safety validator.
9. Seed fixtures from known recurring errors.
10. Produce concise pass/fail output with exact validation errors.

#### Acceptance criteria

- A captured invalid action is reproducible without a worktree or GitHub.
- Changing only step details changes the reconstructed prompt.
- Replay uses production parsing and transition validation.
- A replay cannot invoke tools or mutate repository files.
- Secrets and absolute paths are absent from committed fixtures.
- Ten or more known failures can run in under one minute without live LLM
  calls.

### Phase 2: Generic isolated scenarios and trajectory assertions

#### Deliverables

- Versioned scenario schema and loader.
- Temporary repository fixture builder.
- Scripted and recorded provider modes.
- `workflow-scenario` command.
- Shared trajectory assertion engine.
- Migration of existing feature-specific harness assertions into reusable
  components.

#### Detailed work

1. Implement fixture copying and deterministic Git initialization.
2. Add intrinsic Git and GitHub test doubles that retain production action
   contracts.
3. Execute workflow chat skills and durable workflow tasks through one scenario
   runner interface.
4. Assert ordered and unordered step visits.
5. Assert required, forbidden, and repeated actions.
6. Assert gate state, handoff records, files, commits, and terminal outcome.
7. Track parent-skill roundtrips rather than resetting counts per nested step.
8. Add `--keep-failed` and a failure reproduction command.
9. Port the specify-a-feature and start-implementing-feature harnesses to the
   shared scenario components without removing their convenient wrappers.
10. Add CI scenarios for the highest-risk workflow definitions.

#### Acceptance criteria

- Scenarios never modify the primary checkout.
- CI scenarios require no network or provider credentials.
- A failed assertion names the step and relevant trajectory events.
- The runner detects loops even when action payloads differ syntactically but
  have the same material effect.
- Existing harness behavior is preserved through shared infrastructure.

### Phase 3: Static confusion analysis and prompt snapshots

#### Deliverables

- Confusion-oriented validation rules.
- Exact action-example validation.
- Placeholder/input validation across details and commands.
- Prompt snapshot renderer.
- Snapshot tests for critical skills and workflow templates.

#### Detailed work

1. Inventory action schemas and generate example validators from runtime
   parsers where possible.
2. Parse JSON examples embedded in step details.
3. Resolve placeholders against declared inputs and deterministic runtime
   values.
4. Detect multi-action step descriptions and contradictory transition rules.
5. Render normalized prompts for each step state category.
6. Add snapshots for create-pull-request, specify-a-feature,
   start-implementing-feature, finish-pr-prep, and execute-proposed-pr.
7. Include static validation in generic branch evaluation.

#### Acceptance criteria

- Invalid embedded examples fail definition validation.
- Unbound placeholders identify their exact definition path.
- Prompt changes appear as ordinary reviewable diffs.
- Existing valid definitions remain valid or receive intentional migration
  changes with tests.

### Phase 4: Failure clustering and ambiguity review

#### Deliverables

- `analyze-workflow-errors` command.
- Error clustering and replay candidate generation.
- Structured high-reasoning ambiguity reviewer.
- Advisory wording suggestions.

#### Detailed work

1. Normalize historical and current error records.
2. Cluster by deterministic fingerprints before considering LLM analysis.
3. Rank clusters by frequency, blocked executions, and wasted roundtrips.
4. Generate draft replay bundles from representative records.
5. Ask the ambiguity reviewer only about changed or warning-producing steps.
6. Compare reviewer interpretations and flag disagreement.
7. Include source sentences responsible for each ambiguity finding.

#### Acceptance criteria

- The same underlying failure is not reported as dozens of unrelated entries.
- Every suggestion links to concrete evidence and a definition step.
- Healthy unchanged definitions do not cause high-reasoning calls.
- Reviewer failures do not fail deterministic validation or scenario execution.

### Phase 5: Baseline comparison and unified tuning command

#### Deliverables

- Baseline/candidate comparison engine.
- Metric thresholds and regression policy.
- `tune-workflow` orchestration command.
- Human-readable and JSON reports.
- Optional CI integration for changed definitions.

#### Detailed work

1. Resolve baseline definitions from a merge base or supplied ref.
2. Execute identical replay and scenario sets against both definitions.
3. Calculate quality, cost, and progress metrics.
4. Fail on deterministic regressions and configurable metric thresholds.
5. Report improvements separately from unchanged behavior.
6. Select scenarios based on changed definition paths.
7. Upload detailed reports while keeping console output concise.

#### Acceptance criteria

- A candidate cannot be called better solely because it eventually completes.
- The report identifies increased repairs, human prompts, loops, or token use.
- The default changed-definition suite remains fast enough for pull-request CI.
- Live-provider comparison remains opt-in and clearly separated from
  deterministic CI evidence.

## Test strategy

### Unit tests

- Replay and scenario schema parsing.
- Schema-version rejection and migration.
- Prompt reconstruction.
- Action parsing and transition validation.
- Placeholder resolution.
- JSON example extraction and validation.
- Trajectory fingerprinting and semantic action normalization.
- Redaction and secret detection.
- Metric calculation and comparison.
- Failure clustering.

### Integration tests

- Replay a malformed action and verify corrected guidance.
- Replay a forward `goto_step` and an early `complete` with a later gate.
- Run a scripted skill through nested skill completion.
- Run a durable task through file edits, tests, commit, push stub, and PR stub.
- Verify a failed repair causes all dynamic validation obligations to rerun.
- Verify a scenario catches an unnecessary human prompt.
- Verify a scenario catches a syntactically different repeated action.
- Compare a deliberately ambiguous definition with a corrected definition.

### Repository validation

- Every committed replay and scenario fixture validates.
- Every critical definition has at least one happy-path scenario.
- Every repaired production failure adds or updates a replay fixture.
- Prompt snapshots are regenerated intentionally and reviewed as diffs.

## Success metrics

Track these metrics per definition and across the repository:

- At least 95% valid first-action rate on committed live-eval samples.
- Zero deterministic scenario violations in CI.
- Zero forward `goto_step` or early gate-bypassing completion attempts accepted.
- Reduction in median repair requests per completed workflow.
- Reduction in repeated semantic actions.
- Reduction in unnecessary `prompt_user` actions.
- Reduction in roundtrips before first material progress.
- Stable or reduced prompt token use.
- A production instruction failure can become a replay fixture in under five
  minutes.
- Prompt-only regression suites complete in under one minute without network
  access.

Metrics based on live models should be reported with provider, model, sample
count, and date. They should not be treated as deterministic guarantees.

## CI policy

For every pull request that changes a skill or workflow definition:

1. Run schema and static confusion validation.
2. Render and compare relevant prompt snapshots.
3. Run matching prompt-only replay bundles.
4. Run matching scripted scenarios.
5. Produce a compact regression summary.

Nightly or explicitly requested jobs may run live-provider samples and compare
quality and token metrics. Provider overload or unavailability should mark the
live sample inconclusive, not fail deterministic CI.

## Migration strategy

- Begin with create-pull-request, specify-a-feature,
  start-implementing-feature, finish-pr-prep, and execute-proposed-pr because
  they have the richest failure history.
- Convert representative existing error records into replay bundles.
- Extract shared fixture and assertion logic from existing harnesses.
- Keep old harness commands as wrappers until their behavior is fully covered.
- Add static checks in warning mode first, repair definitions, then promote
  stable checks to errors.
- Require new production failure fixes to include a replay or scenario when the
  failure is reproducible.

## Risks and mitigations

### Fixtures become stale

Include schema, action-contract, prompt-builder, and definition versions.
Provide explicit migration tools and fail with actionable version errors.

### Recorded prompts preserve too much sensitive data

Store structured state, apply deterministic redaction, validate fixtures for
credential patterns, and require review before committing generated fixtures.

### Scripted responses overfit implementation details

Use trajectory assertions for intent, maintain some recorded and opt-in live
cases, and avoid asserting incidental prompt wording unless testing a contract.

### LLM evaluations are nondeterministic

Keep deterministic parsing, transition, gate, file, and trajectory assertions
authoritative. Report live samples statistically and never make ordinary CI
depend on one response.

### Scenario suite becomes slow

Select cases by changed definition, run prompt-only replays first, parallelize
isolated scenarios, and reserve live providers for opt-in jobs.

### Harness behavior diverges from production

Call production prompt builders, action parsers, transition validators, tool
contracts, and execution drivers. Keep harness-specific code limited to state
fixtures, provider doubles, and assertions.

## Suggested pull request sequence

1. Replay schema, trajectory events, and prompt-only replay CLI.
2. Error-log conversion, redaction, and initial replay corpus.
3. Scenario schema, temporary repository builder, and scripted provider.
4. Shared trajectory assertions and migration of existing harnesses.
5. Static confusion checks and embedded example validation.
6. Prompt snapshot rendering and critical-definition snapshots.
7. Error clustering and replay candidate report.
8. Ambiguity reviewer in advisory mode.
9. Baseline/candidate comparison and unified tuning command.
10. Changed-definition CI selection and regression policy.

Each pull request should include fixtures that prove its behavior and should not
depend on later phases for basic correctness.

## Definition of done

The plan is complete when:

- Any recorded LLM action failure can be replayed without rerunning the whole
  workflow.
- Any critical workflow can run as an isolated deterministic scenario.
- Definition changes produce reviewable prompt and behavior diffs.
- Static validation catches malformed examples, unbound placeholders,
  contradictory instructions, invalid transitions, and gate bypasses.
- Real failures are clustered and can be promoted into safe regression
  fixtures.
- Baseline comparison reports both correctness and execution quality.
- CI automatically selects and runs relevant deterministic cases for changed
  definitions.
- A developer can iterate from observed failure to verified instruction fix in
  minutes rather than repeatedly operating a full live workflow.
