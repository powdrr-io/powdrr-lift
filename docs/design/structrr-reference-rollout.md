# Protected source reference runs

`powdrr_lift.workrr.source_reference_run.SourceReferenceRun` implements the
initial local rollout of the source annotations plan. It accepts an accepted
design, exact clean Git base, allowed paths, explicit behavioral validation
profiles, a model, budgets, and an artifact directory outside the candidate
workspace. `prepare` compiles the catalog and handoff before any worker starts;
`execute` accepts the parent-side inference handler and performs one invocation.

## Authority and isolation

Each run has a new identity and two artifact directories. `private` contains the
catalog, handoff including private validation data, input identities, configuration,
Seatbelt profile, invocation record, reader events, validation reports, and terminal
result. `worker` contains temporary files, caches, the public miniSWE configuration,
and trajectory. Private files are hashed before execution and checked again after
execution and validation. Configuration includes explicit `enforce` mode, model,
budgets, reader limits, validation commands, dependency version, and request identity.
Changing these inputs invalidates the run.

The supported adapter is macOS `sandbox-exec` with Seatbelt. Preparation probes the
actual protection before authorizing execution. Worker and behavioral validation
processes cannot read private artifacts. Their writes are confined to the candidate
workspace and worker artifact directory, with Git metadata additionally protected.
Temporary and cache directories are redirected into worker artifacts. Parent-side
inference credentials and private reference configuration are removed from validation
environments. Unsupported platforms, unavailable protection, and unbound requests
fail before worker invocation; there is no unprotected fallback. Linux, containers,
and OpenCode need equivalent adapters before they can execute this contract.

This is a private-input and write boundary, not a general filesystem secrecy policy.
Other host files remain readable under the existing local execution model. The
worker sees the exact compiled public prompt, the concrete workspace, local check
command, and behavioral commands. It must not commit, push, or amend the design.

## One attempt and independent proof

There is one worker invocation, with no automatic repair, trajectory resume, or
fallback prompt. The completion payload is JSON with schema version
`structrr-reference-completion-v1`, `reference_owners`, and `design_gaps`.
Each owner names a reference, path, kind, and qualified name. Each gap names a
location and consequence. Gaps block acceptance and require a separately accepted
design before a new run.

The supported reader must deliver the full canonical context for every active
placement obligation. Private verified events record catalog identity and delivered
page intervals; the orchestrator checks their union for complete coverage. Visible
markers, plain shell reads, fabricated metadata, and incomplete pages do not prove
delivery. This does not claim interception of every shell read. The existing
reader supplies enclosing-owner context even when a body-only range is requested.

After the worker submits, the orchestrator validates completion and context delivery,
then runs explicit behavioral profiles and independently reconstructs final source
ownership and preservation. Worker claims are evidence to check, never authority.
Behavioral success cannot compensate for a misplaced reference. Semantic review
uses bounded evidence without private validation data. Relationships requiring
test-collection evidence still require a trusted, candidate-bound receipt; absent
evidence fails closed. A candidate snapshot before validation is compared afterward,
so test commands cannot change the accepted candidate. Frozen input checks and
whitespace validation must also pass.

`terminal.json` records the outcome, attempt, validation report, completion, context
delivery, immutable input identities, and fingerprint. Failures retain diagnostics
and never trigger another implementation invocation. Reusing a run identity or
calling `execute` twice is rejected. The accepted result is a candidate for the
existing repository integration and deployment flow; it does not merge or deploy.

## Compatibility and verification

Legacy requests remain explicitly `off`. Existing observer experiments remain
available, but mandatory source-reference handoffs require `enforce`; they cannot
downgrade to `observe` or `off`. Historical handoffs without a workspace field keep
their original prompt serialization.

`tests/test_source_reference_run.py` runs an offline fake-model relocation through
the real miniSWE environment, reader bridge, Seatbelt boundary, and final validators.
It also exercises incorrect ownership, missing verified context, reported design
gaps, and validation that mutates source. Native integration cases require macOS
and permission to create the nested sandbox. They make no provider calls.
The optional `scripts/smoke-source-reference-run.py` requires both `--live` and
`POWDRR_LIVE_SOURCE_REFERENCE_SMOKE=1`; it is never part of routine verification.

Workrr plan items can now select this execution path through a reviewed
`source_reference_design_file`. See [task dispatch](structrr-source-task-dispatch.md)
for committed authority, candidate isolation, patch transfer, and supported task types.
