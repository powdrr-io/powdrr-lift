# External coding-agent boundary

Workrr can use an external coding agent as an implementation worker after a
Structrr/Procedrr plan has been validated. The worker is not the owner of the
plan, product context, workflow state, or publication.

The first slice defines two versioned artifacts:

- `implementation-request-v1`: the bounded handoff to a worker. It carries the
  objective, base commit, plan fingerprint, allowed paths, acceptance criteria,
  validation profiles, and selected context references.
- `implementation-attempt-v1`: Workrr's observation of one worker invocation.
  It records the provider, terminal status, exit code, parsed JSON events,
  changed paths, out-of-scope paths, and a diff fingerprint.

`ImplementationRequest.from_execution_unit` is the first compiler boundary
from the existing validated execution-plan model into this worker protocol.
The request prompt is deliberately only a rendering of the typed request.

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

The fake-provider tests exercise the policy boundary without requiring an
OpenCode installation or model credentials. Review feedback, validation
execution, retry budgets, and Structrr reconciliation are subsequent slices.
