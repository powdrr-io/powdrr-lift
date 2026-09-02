# Repository-shared durable work

## Proposal

The repository is the collaboration boundary for durable work direction. When
many people or agents work on the same project, they should share one reviewed,
versioned place for the intent and work products that direct that work.

The following artifacts belong in the repository:

- typed intent sources, clauses, contracts, and their supersession lineage;
- behavior rules that apply across executions;
- execution plans, including work units, scope, dependencies, and acceptance
  conditions; and
- workflow tasks, including ownership, assignment, handoff, and progress that
  must be visible to collaborators.

These artifacts should reference one another by stable IDs and versions. They
must not duplicate the original intent wording into every derived plan or task.
They are shared authored work products and should be reviewed and committed
like other repository changes.

## Execution boundary

`.powdrr` is reserved for materialized state specific to one execution. This
includes transient prompt context, event history, checkpoints, obligations,
evidence, retries, capability decisions, and other facts that do not represent
shared work direction.

An execution may materialize progress from a repository task into `.powdrr`,
but that materialization must not silently change the repository task, shared
intent, or behavior rules. A deliberate change to shared direction must be
authored as a reviewed repository change.

## Ownership model

```text
repository intent and policy
  -> repository plan
  -> repository workflow task
  -> one execution's .powdrr state and history
```

The repository artifacts are authoritative for what the work means and how it
should be coordinated. `.powdrr` is authoritative only for what happened in a
particular run and what that run still needs to complete.

## Consequences

- A new agent can discover the current intent, plan, and task without access to
  another agent's conversation.
- Multiple people can review and amend shared work direction through normal
  version control.
- Execution retries and transcripts do not pollute shared intent.
- Runtime state can remain disposable or locally recoverable without losing
  the project's durable rationale.
- The runtime must distinguish repository-owned artifacts from execution-local
  state and must not promote model narration or transient results into shared
  intent.

## Follow-up implementation

The implementation should move the typed intent registry and cross-execution
behavior rules out of execution-only storage, define repository locations for
plans and workflow tasks, and make runtime state reference those artifacts by
stable IDs and versions. Existing `.powdrr` data should remain supported as
execution-local state during migration, but it should not remain the source of
shared work direction.
