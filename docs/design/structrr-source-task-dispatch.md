# Source reference tasks in Workrr

An `execute-proposed-pr` plan item can select protected source-reference execution
with `source_reference_design_file`. This is a normalized repository-relative path
to a reviewed, committed YAML or JSON document. The reviewed plan serializer,
service compiler, and workflow writer preserve the pointer. The queue and compiled
plan validator reject a workflow whose pointer differs from its pinned plan item.
Typed plan repair preserves the pointer rather than dropping enforcement.

For example, a reviewed execution plan item includes:

```yaml
source_reference_design_file: docs/structrr/accepted/retry.yaml
```

That file contains:

```yaml
schema_version: structrr-source-reference-task-v1
plan_id: retry-plan
item_id: retry-helper
design:
  reference_definitions: []
  source_reference_placements: []
  source_reference_preservations: []
```

The empty arrays above illustrate the envelope only. A runnable design must contain
the actual accepted definitions and obligations required by the
[handoff compiler](structrr-source-reference-handoff.md). The full desired source
reference state is authoritative for this task. Existing multi-PR proposal pointers
remain in generated workflow metadata for compatibility; this explicit selector
chooses the source-reference contract instead of the legacy re-bootstrap contract.
Do not put private test oracles, credentials, or review-only data in this committed
document.

## Dispatch and handoff

Workrr loads the design from the exact workflow base commit, checks its plan/item
identity, requires a regular Git blob, and bounds it to one million bytes. Candidate
edits, untracked files, and symlink targets cannot supply accepted intent. A new
design revision must be reviewed and committed before another implementation run.
The source path is an explicit reviewed selector; an LLM does not choose the mode.
Inline reference intent without a design pointer is rejected.

The task dispatcher branches before independent test authoring, prompt augmentation,
and legacy coding recovery. It uses the plan objective, allowed paths, and explicit
verification command to prepare a [SourceReferenceRun](structrr-reference-rollout.md).
Mandatory references always use `enforce`; an explicit downgrade is rejected.
Ordinary items without the selector retain the existing implementation flow.

The worker runs once in a dedicated clean detached candidate worktree at the exact
base. Workflow runtime files stay in the task checkout. Independent final validation
uses the task's existing workflow review client for structured source evidence and
the protected behavioral profile. No automatic test-authoring or repair invocation
is added. Source relationships requiring test collection still fail closed without
trusted candidate-bound collection evidence.

Only an accepted result is exported as a binary-capable patch, including new source
files. The orchestrator verifies the frozen inputs and candidate snapshot before
and after export, confirms that the task checkout has the same HEAD and clean
candidate paths, checks the patch, and applies it to the task checkout. It rejects
changes to the selected design, plan/proposal artifacts, and `.powdrr` runtime state.
A validation failure or transfer conflict leaves the task checkout untouched by
the dispatcher. Concurrent caller edits are retained. The existing task workflow
then handles review, commits, PR creation, and deployment policy.

## Results and limits

The operation returns `passed`, changed paths, validation, source outcome, terminal
artifact location, and a single attempt record. `retryable` is false and
`continuations` is zero. The external run identity binds repository, workflow, task,
base commit, and design path; repeating it cannot invoke another coding agent.
The candidate, accepted patch, dispatch receipt, and private validation evidence
are retained under the system temporary directory's `powdrr-source-tasks` directory.
If the process is interrupted, the reserved identity prevents an implicit restart;
inspect its artifacts before accepting a new design revision. These temporary
artifacts follow the host's retention policy and are not a permanent audit store.

The current protected adapter requires macOS Seatbelt and miniSWE. Unsupported
platforms and agents block without falling back. Story allocation/validation tasks
also block until their test-authoring and coverage stages have a protected adapter;
they cannot silently lose story proof by selecting this path. Legacy feature
endpoint and CI-repair dispatch retain their own contracts.

The offline native dispatch tests execute the real miniSWE environment, reader
bridge, behavioral checks, semantic review interface, and accepted patch transfer.
They cover new files, failed behavior, preserved workflow state, concurrent caller
edits, and duplicate dispatch. Separate tests check committed authority and task
routing. No paid provider call is needed for these checks.
