# External coding-agent boundary

Workrr can use an external coding agent as an implementation worker after a
Structrr/Procedrr plan has been validated. The worker is not the owner of the
plan, product context, workflow state, or publication.

The worker boundary defines two versioned artifacts:

- `implementation-request-v1`: the bounded handoff to a worker. It carries the
  objective, base commit, plan fingerprint, allowed paths, acceptance criteria,
  validation profiles, and selected context references.
- `implementation-attempt-v1`: Workrr's observation of one worker invocation.
  It records the provider, terminal status, exit code, parsed JSON events,
  changed paths, out-of-scope paths, and a diff fingerprint.
- `coding-agent-validation-report-v1`: Workrr's observation of every declared
  validation profile after an implementation attempt. It records the exact
  argv, per-profile status, exit code, stdout, stderr, and any terminal error.

`ImplementationRequest.from_execution_unit` is the first compiler boundary
from the existing validated execution-plan model into this worker protocol.
The request prompt is deliberately only a rendering of the typed request.

## Persisted runner

`CodingAgentAttemptStore` persists requests and attempts as individual JSON
artifacts. The request is saved before provider invocation and the attempt is
saved for every terminal outcome, including policy denials and provider
failures. This gives Workrr an explicit record that the handoff occurred and
lets later validation or review load the exact request and observed result.

`CodingAgentRunner` owns that persistence boundary while keeping provider
invocation replaceable. It accepts an already-created clean worktree for this
slice; worktree creation and lifecycle management remain separate concerns.

The `run-coding-agent` CLI command is the initial operational entry point. It
loads a JSON or YAML implementation request, invokes the bounded OpenCode
provider, writes the request and attempt artifacts, and emits the terminal
attempt as JSON. It then runs every registered validation profile declared by
the request and writes a validation report beside the attempt. A non-completed
attempt, blocked validation, or failed validation exits nonzero, so callers
cannot mistake an unvalidated implementation for successful delivery.

Validation profiles are registered explicitly at the command boundary as
`NAME=COMMAND`. Commands are parsed into argv and run without a shell. Each
command must also match one of the request's allowed command prefixes. Unknown
profiles, unauthorized commands, and non-completed worker attempts produce
blocked per-profile results rather than being silently skipped.

## OpenCode adapter

The OpenCode adapter invokes `opencode run --format json --agent build` in the
dedicated worktree. It injects an attempt-specific permission policy that:

- denies unspecified actions;
- allows reading and editing within the worker worktree;
- denies questions, subagents, external directories, web fetches, commits,
  pushes, and GitHub operations; and
- allows only explicitly listed validation command prefixes.

Workrr still treats this as defense in depth. It records repository HEAD
before and after invocation and derives changed paths from Git state. A worker
that commits, changes an out-of-scope path, or starts from a dirty worktree is
not accepted as a normal completed attempt.

The fake-provider tests exercise the policy boundary, persistence, and
validation state machine without requiring an OpenCode installation or model
credentials. Review feedback, retry budgets, worktree lifecycle, and Structrr
reconciliation are subsequent slices.
