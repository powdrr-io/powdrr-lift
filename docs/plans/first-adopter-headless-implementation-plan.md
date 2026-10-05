# First adopter: credentials, bootstrap, and headless feature execution

## Objective

Make Powdrr usable by another person on their own project without Harbor/Pier
or assistance from the maintainers. Deliver three public entry points:

1. Check credentials and access for the configured execution profile.
2. Bootstrap a repository and optionally publish the bootstrap as a PR.
3. Implement a feature headlessly, produce a review report, and optionally
   publish the implementation as a PR.

This document is an implementation handoff. The commands below are the proposed
interface, not commands that are already available. The implementing agent
should follow the phases in order and use the acceptance criteria to determine
completion.

## Architecture boundary

Git is the execution substrate for this agent. The agent runtime owns repository
inspection, clean-checkout enforcement, starting-commit resolution, dedicated
branch and worktree creation, retention, local commits, and optional publication.
This lifecycle applies to every project task, including bootstrap and feature
implementation.

Procedrr flows remain repository-operation agnostic. They receive the prepared
candidate worktree, its resolved base and scope, and the requested publication
mode as validated runtime context. They own planning, uncertainty decisions,
implementation, verification, and review. They must not create or manage Git
branches or worktrees, push commits, or open pull requests. Keep the lifecycle
at the agent/runtime boundary and pass its outcomes into the shared report.

## Accepted product decisions

- Headless execution does not stop to ask product clarification questions.
  Remember each ambiguity or undefined behavior, choose a concrete normative
  default, and report where the uncertainty occurred and what default was used.
- Defaults fill gaps in the request. They must preserve explicit requirements
  and must not create unrelated requirements or hypothetical edge cases.
- Both local headless execution and headless execution that creates a PR are
  required. Publishing is an explicit option.
- Credential checking is a standalone option and is reused as preflight by
  execution commands.
- Bootstrap-to-PR is a separate onboarding action, allowing the adopter to
  review the initial project context before requesting implementation.
- Every execution produces Markdown and JSON reports, including failed and
  interrupted runs. Reporting is based on persisted evidence and decisions.
- A passing test suite does not imply that every product interpretation was
  specified by the user. Reports must keep defaults visible even on success.

Operational failures, contradictory explicit requirements, invalid plans, and
failed verification remain failures. The normative-default policy handles
missing product meaning; it does not waive validation or authorization gates.

## Current implementation and gaps

Read these files before editing:

| Area | Existing implementation | Gap to address |
| --- | --- | --- |
| CLI | `src/powdrr_lift/cli.py`: `workrr-feature`, `harbor-feature`, `bootstrap-structrr` | Add the three adopter entry points and shared options. |
| Agent/runtime lifecycle | Agent integration and shared runtime entry points | Make Git and worktree handling a built-in agent responsibility for every task. Support a local start ref and independent publishing; pass prepared candidate context into Procedrr. |
| Feature orchestration | `src/powdrr_lift/workrr/feature_endpoint.py`: `FeatureEndpointConfig`, `run_feature_endpoint`, `run_feature_in_place` | Adapt the feature entry point to consume agent-prepared candidate context and return execution evidence. Keep Git lifecycle and publication out of Procedrr flows. |
| Shared flow | `docs/procedrr/skill-definitions/implement-feature.yaml` and `design-interview.yaml` | Normative defaults currently depend on `benchmark_mode`. Separate the uncertainty policy from benchmark behavior. |
| Procedrr flow commands | `src/powdrr_lift/workrr/command_catalog.py` | Default validation, consistency updates, and `normative-assumptions.json` are coupled to benchmark mode. Move Git effects out of flow commands; keep workflow operations focused on evidence and gates. |
| Typed defaults | `src/powdrr_lift/core/behavior_contract.py` | Reuse existing `validate_normative_assumptions`; add provenance without weakening the contract. |
| Bootstrap | `src/powdrr_lift/structrr/bootstrap.py` | Generates and validates a snapshot but does not provide a complete isolated bootstrap-to-PR lifecycle. |
| Existing bootstrap/publication skills | `skill-definitions/bootstrap-code-structure.yaml`, `create-pull-request.yaml` | Audit reusable discovery and publishing behavior; these older skills use a different artifact/flow contract from deterministic Structrr bootstrap. |
| Taxonomy | `src/powdrr_lift/core/entity_taxonomy.py` | Default loading assumes a taxonomy file exists in the target repository. Fresh projects need a packaged default. |
| Providers | `workrr/providers.py`, `provider_config.py`, `provider_runtime.py` | Credential resolution exists; resolution alone is not an authenticated access check. |
| Coding adapters | `src/powdrr_lift/workrr/coding_agent.py` | Planning and coding can use different models, credentials, and executables. Check the actual selected routes. |
| Artifacts | `src/powdrr_lift/workrr/run_artifacts.py` and feature endpoint result writers | Detailed JSON exists. Add a unified user report and persist defaults before final planning succeeds. |
| Packaging | `pyproject.toml` | Verify bundled flow definitions, taxonomy, and runtime resources in an installed distribution. |
| Acceptance harness | `bin/test-live-implement-feature`, `docs/current/real-implement-feature-smoke.md` | Extend acceptance to an installed package and an unfamiliar project, including local execution without a remote. |

Preserve the existing execution kernel, Structrr bootstrap, coding adapters,
verification receipts, and review flow. Extend these components rather than
building a second feature pipeline.

## Public command contract

```bash
powdrr-lift check-credentials --repo-root .
powdrr-lift check-credentials --repo-root . --open-pr --json

powdrr-lift bootstrap --repo-root . --open-pr

powdrr-lift implement --repo-root . --headless \
  --request-file feature.md --work-item-name example-feature

powdrr-lift implement --repo-root . --headless \
  --request-file feature.md --work-item-name example-feature --open-pr
```

### Shared configuration

Use the same configuration resolver in credential checks and execution. Support
the existing planning provider/model and coding agent/model overrides; preserve
backend-specific executable overrides. Resolve defaults once and record the
effective profile in run metadata. Show both planning and coding routes rather
than presenting them as one model. Avoid silently switching to an unrelated
provider when an explicitly selected profile fails.

Common options should include `--repo-root`, `--output-root`, `--json`, and
`--open-pr` where applicable. Execution should support `--base-ref` as its local
starting point; publishing should separately support `--base-branch` and
`--remote` (default `origin`). If `--base-ref` is omitted, use the starting
checkout's `HEAD`, resolve it to a commit, and record that commit. If a PR base
is omitted, resolve the remote's default branch with a clear error when it
cannot be determined. Check that the starting commit is appropriate for the
chosen PR base and report divergence rather than including unintended changes.

Feature input is exactly one of `--request-file` or `--feature-description`.
Reject empty input before model calls. Accept repeated `--allowed-path` options;
default to the repository scope and record that scope. Preserve the existing
validation-command override and automatic repository check discovery.

`--json` writes one machine-readable result to stdout. Progress and diagnostics
go to stderr. No command should print credentials or bearer tokens.

### Compatibility

Keep the existing commands and aliases working. Add the new commands as wrappers
around shared orchestration. The new local commands explicitly set
`push_changes=False` and `open_pr=False`; `--open-pr` enables both. Do not change
legacy publishing semantics incidentally. Document that legacy
`workrr-feature --no-open-pr` currently permits pushing.

## Phase 1: separate uncertainty policy from benchmark execution

Introduce an explicit uncertainty policy, such as
`clarify | normative_default`, in feature configuration and flow inputs.
`implement --headless` selects `normative_default`. Preserve clarification
behavior for existing interactive paths. Harbor also selects normative defaults,
while retaining its existing workspace and benchmark semantics.

Audit all uses of `benchmark_mode` in the feature endpoint, command catalog,
semantic compiler, external-contract research, worker packets, and flow
definitions. Classify each as workspace behavior, benchmark acceptance behavior,
or uncertainty handling. Change only the uncertainty uses to the new policy.
Do not set `benchmark_mode=True` for ordinary headless execution: that flag also
affects validation, fallback behavior, and result handling.

For every materially applicable dimension that the source leaves undefined:

1. Preserve the source clause and its stable identifier.
2. Record the exact uncertainty, including a repository location when relevant.
3. Select one definite behavior using this evidence order: explicit source and
   accepted design; supported repository conventions; identifiable official
   standards or ecosystem conventions; language/framework defaults; a
   conservative interoperable default.
4. Record the resolution, rationale, basis/reference, and confidence. If no
   identifiable normative source exists, state that explicitly. Never invent a
   citation or mark assumed behavior as user-specified behavior.
5. Bind the default into the behavior scenario, verification plan, and worker
   context so implementation and validation use the same decision.

Reuse the existing exact-coverage rules: one assumption for every unresolved
applicable dimension, none for source-resolved or non-applicable dimensions.
Retain uncertainty provenance after resolving the scenario for execution.

Persist a decision as soon as it is accepted, before subsequent planning or
implementation can fail. If consistency review revises it, retain the previous
decision and record the replacement. Capture defaults discovered by coding or
review as well as defaults produced during initial planning. Audit the existing
worker output formats and add structured decision output where necessary;
prose alone must not be the only record.

Acceptance: a headless task with unspecified behavior continues using a recorded
default; explicit requirements remain intact; strict production validation
still applies; no clarification prompt or stdin read occurs.

## Phase 2: credential and access checks

Add a reusable checker with typed per-check results and a CLI wrapper.

- Resolve the actual planning and coding profiles using the execution resolver.
- Distinguish missing credentials, authentication rejection, model access
  rejection, rate/quota limits, network failure, timeout, and missing executable.
- Make a small authenticated request to each required model route. An environment
  variable being present or a models list succeeding is insufficient proof of
  inference access. Deduplicate identical checks and record which roles they
  cover. Check required fallback routes or explicitly identify unverified ones.
- For external coding agents, verify executable availability and the effective
  provider configuration. Use a bounded adapter smoke check in a disposable
  directory when needed to confirm its credential path without editing the
  target repository.
- Use bounded timeouts and retries; report the selected provider/model, credential
  source name, status, and remediation without exposing credential contents.
- With `--open-pr`, check GitHub authentication, remote identity, and read-only
  evidence of push/PR permissions. Permission checks are best effort; report
  limitations and handle publication errors later. Do not create a test branch
  or PR as part of credential checking.
- Bootstrap uses only checks its execution requires. A deterministic bootstrap
  must not demand model credentials merely because implementation uses models.

The standalone check collects all applicable results instead of failing at the
first missing credential. Execution fails preflight before worktree creation or
expensive planning when a required check fails.

Acceptance: checks use the same effective configuration as execution, return
nonzero for required failures, and perform no target-repository mutation.

## Phase 3: isolated Git lifecycle and bootstrap-to-PR

Implement or extend the agent/runtime lifecycle so Git behavior is built into
the agent and shared across bootstrap and feature execution. Do not implement
branch, worktree, commit, push, or PR operations in Procedrr flows.

1. The agent resolves the repository and starting commit. Require a clean starting checkout;
   report the offending paths without cleaning or stashing user changes.
2. Validate run identifiers and branch/worktree collisions. Never overwrite or
   remove prior work automatically. Each attempt gets its own run directory.
3. Create a dedicated worktree and feature branch from the resolved commit.
   Local execution must work without `origin`, GitHub credentials, or fetching.
4. Keep telemetry outside the candidate diff using the existing exclusion
   mechanism. Do not add unrelated ignore-file edits.
5. The agent validates and commits only the intended artifacts. Retain the worktree for review
   on success and failure; print its location.
6. The agent publishes only when requested and after required gates pass. Push the feature
   branch and open a PR against the selected base. Never merge it.

Bootstrap wraps `bootstrap_structrr`: generate the source-anchored snapshot,
validate it, and commit the onboarding artifacts. Supply a packaged taxonomy
when the target has none, while respecting an existing project taxonomy or
explicit override. Record taxonomy provenance and ensure artifacts remain usable
after installation, without paths into the developer checkout.

For this first release, the bootstrap artifact is the validated Structrr snapshot
and its necessary taxonomy/context resources. Inspect the existing
`bootstrap-code-structure` skill for reusable discovery evidence, but do not
silently replace this contract with its older project-structure workflow. If
additional model-assisted discovery is needed, declare that dependency and
include its credential checks and uncertainty decisions. A deterministic
bootstrap remains usable without model credentials.

The bootstrap PR body describes what was indexed, validation/check discovery,
coverage limitations, defaults used, and generated files. If bootstrap produces
no changes, report a successful no-op and avoid an empty PR. Preserve existing
project artifacts; do not overwrite a project taxonomy or hand-authored context
without a supported update policy.

Acceptance: bootstrap succeeds on a fresh project without Powdrr-specific files;
local mode has no push or PR side effects; publishing creates a reviewable PR
whose diff contains only onboarding artifacts.

## Phase 4: evidence-based run reports

Add a shared versioned report model and deterministic Markdown renderer. Extend
existing metadata and results rather than replacing their formats incompatibly.
Use a per-attempt directory under `.powdrr/runs/<run-id>/` for the new commands,
or map it to the existing feature artifact directory through one shared helper.
Expose final artifact paths in CLI output and `run-result.json`.

Required artifacts:

- `report.json`: the canonical typed report.
- `report.md`: readable rendering of that same report.
- `uncertainty-decisions.json`: durable decisions, including revisions.
- Existing metadata, validation, review, failure, and diagnostic artifacts.

Each uncertainty record needs a stable ID, originating phase, clause/source
reference and quote, optional file/symbol/line location, uncertainty description,
selected default, rationale, basis/reference, confidence, and revision history.
After execution, attach affected implementation and verification references when
available. Mark unavailable links as unavailable; do not infer evidence.

Reports include the request, resolved starting commit, effective profile, branch,
worktree, scope, result status, change summary, all uncertainty decisions,
validation commands/results, review findings, remaining operational failures,
and publication outcome. Report measured usage when available and label unknown
cost as unknown. A successful report with no uncertainty says so explicitly.

Write reports for success, preflight failure, planning failure, validation/review
failure, publication failure, timeout, and handled interruption. Persist decisions
incrementally and use atomic final writes. A forced process kill cannot guarantee
a final report; leave enough durable state for inspection or a report-rendering
recovery command. Do not claim universal crash recovery or full execution resume.

The PR body includes the change summary, validation, and uncertainty decisions.
Do not link only to local absolute paths that reviewers cannot access. Include
the actual decision details in the PR body and indicate how full local reports
can be retrieved. Audit both PR creation and subsequent changelog/body updates
so later updates preserve the uncertainty section.

Acceptance: Markdown and JSON agree; recorded defaults survive late failure;
report status reflects verification and publication outcomes accurately.

## Phase 5: headless feature entry point

Wire `implement --headless` into the existing feature endpoint and shared
`implement-feature` flow with the new uncertainty policy and lifecycle controls.

- Load the request and run applicable preflight checks.
- Bootstrap/refresh required context in the implementation worktree using the
  existing feature bootstrap behavior. A separately reviewed bootstrap PR is
  recommended onboarding, not a mandatory manual file-creation step.
- Run planning, proposal review, verification planning, coding, validation,
  completeness/scope review, and supported repair with existing gates.
- Feed accepted defaults into implementation and verification. Keep a separate
  record that they originated as defaults.
- Enforce headless behavior even when stdin/stdout are terminals. Never launch a
  TUI, prompt for provider selection, or block waiting for clarification.
- Expose a bounded overall timeout and attempt/repair limit by extending existing
  runtime controls. On cancellation, stop owned subprocesses, preserve evidence,
  and finalize the report when possible. Record the effective limits.
- On verified success, commit locally. With `--open-pr`, publish and include the
  report details. If publishing fails, retain the verified local change and
  return a distinct failed publication result; do not describe the run as fully
  completed with a PR. The agent runtime performs commits and publication after
  the flow returns its verified candidate and review evidence.

Define and document result statuses for completed local work, PR opened, no-op,
failed execution, failed publication, timed out, and interrupted. Exit zero only
when the requested outcome succeeded. Use standard nonzero conventions for
errors and interruption, and expose detailed failure stages in JSON.

Acceptance: the same bounded feature works locally and with PR publication;
uncertainty does not trigger user interaction; failed gates prevent publication.

## Phase 6: installation and adopter documentation

Verify installation from a pinned revision or built distribution, outside the
source checkout. Ensure the chosen coding executable, Procedrr definitions,
taxonomy, and required runtime resources resolve from the installed package.
Remove assumptions that the target project contains this repository's templates,
skill files, or developer environment.

Add a short README quickstart showing credential checking, bootstrap-to-PR,
review/merge of the bootstrap by the user, local headless implementation, and
headless implementation with a PR. Document the supported initial execution
profile, dependencies, required environment variables, custom provider/model
options, reports, scope, limits, and inspecting/discarding retained worktrees.
Explain which project content is sent to configured providers and which local
artifacts persist. Do not promise execution resume unless tested end to end.

## Verification and acceptance matrix

Add meaningful regression coverage alongside existing CLI, feature endpoint,
bootstrap, behavior-contract, provider, and Harbor tests. Mock model/network
calls for the default suite. Keep real provider and external PR checks opt-in.

| Scenario | Required result |
| --- | --- |
| Missing planning or coding credential | Accurate role-specific failure before feature execution. |
| Credential exists but model rejects access | Access failure, not a successful credential check. |
| Different planning/coding providers | Both effective routes checked and reported. |
| Bootstrap on a fresh project | Packaged resources work; snapshot validates; no product-code edits. |
| Local project with no remote | Bootstrap and feature execution succeed without GitHub requirements. |
| Bootstrap with `--open-pr` | Isolated branch, validated onboarding commit, PR summary/report. |
| Ambiguous feature | One concrete accepted default per applicable gap; implementation continues. |
| Explicit behavior alongside ambiguity | Defaults preserve explicit behavior and scope. |
| Coding-stage uncertainty | Structured decision appears in the final report. |
| Consistency review changes a default | Prior decision and replacement remain traceable. |
| Validation or completeness review fails | No PR; partial work and report retained; nonzero exit. |
| Headless invocation from a terminal | No prompt, TUI, or stdin read. |
| Timeout/interruption | Owned processes stop; accepted decisions and available partial report survive. |
| PR push/create/update fails | Local verified work retained; publication failure reported. |
| Existing branch/worktree/run collision | Clear refusal or supported retry; no overwrite. |
| Existing legacy commands and Harbor runs | Existing workspace and benchmark contracts still pass. |

Run a real acceptance trial from the installed distribution on a small unfamiliar
Python project, using one documented planning/coding configuration. Exercise
credential checks, bootstrap-to-PR, a local ambiguous feature, and a headless
feature PR. Also exercise failed validation and cancellation. An authorized test
repository is required for real PR creation; deterministic tests use a fake
publication boundary. Capture artifact paths, results, versions, and limitations.

## Delivery sequence and repository checks

Prefer reviewable change sets in dependency order:

1. Shared profile resolution and credential/access checks.
2. Explicit uncertainty policy and incremental decision persistence.
3. Shared reports and failure/interruption finalization.
4. Local Git lifecycle and bootstrap-to-PR.
5. Headless feature CLI and PR report integration.
6. Installed-package acceptance and quickstart.

For each change set, follow `AGENTS.md`, use a dedicated worktree/feature branch,
and open a PR. Do not merge it. Follow the repository's changelog requirements
when applicable. Re-read current code before implementation; this plan describes
the inspected starting state, and signatures may change during delivery.

Use the shared environment for development checks:

```bash
export VIRTUAL_ENV=/Users/gregory/code/powdrr-lift/.venv
export PATH="$VIRTUAL_ENV/bin:$PATH"
export PYTHONPATH="$PWD/src"
export UV_PROJECT_ENVIRONMENT="$VIRTUAL_ENV"
export UV_NO_SYNC=1

rtk proxy "$VIRTUAL_ENV/bin/python" -m ruff format --check .
rtk proxy "$VIRTUAL_ENV/bin/python" -m ruff check .
rtk proxy "$VIRTUAL_ENV/bin/python" -m mypy src tests
rtk proxy "$VIRTUAL_ENV/bin/python" -m powdrr_lift.cli validate-workflow-definitions \
  skill-definitions templates --liveness \
  --baseline .github/workflow-liveness-baseline.json \
  --warning-budget .github/workflow-liveness-warning-budget.json \
  --warning-report /tmp/first-adopter-workflow-warnings.json
rtk proxy "$VIRTUAL_ENV/bin/python" -m pytest -n auto \
  --cov=powdrr_lift --cov-branch --cov-report=term-missing tests
rtk proxy "$VIRTUAL_ENV/bin/python" -m powdrr_lift.cli workflow-scenario-suite \
  --manifest workflow-evals/scenarios/manifest.yaml --repo-root "$PWD" \
  --report /tmp/first-adopter-workflow-scenarios.json
```

Run applicable workflow tuning checks when their definitions change, following
`.github/workflows/ci.yml`. Do not create a worktree-local environment or reinstall
the project into the shared environment. The isolated installed-distribution
acceptance trial is a separate packaging check, not routine environment setup.

Completion requires all three entry points, both feature publication modes,
traceable defaults, reports on handled failure, passing repository checks, and
evidence of the documented adopter journey outside Harbor/Pier. The user reviews
and merges the implementation PRs.
