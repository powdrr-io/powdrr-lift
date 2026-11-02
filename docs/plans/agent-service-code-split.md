# Agent and service code split

Status: proposed execution plan; no implementation authorized by this document.

Date: 2026-10-28.

Audit base: `e1b8fb40cbcfc27d5e63b555a75f9a25b0b9c568`, including the bounded
planner and parallel planning frontier from PR #1166.

## 1. Deliverable and scope

Split the current application into an open source agent, private service logic,
public data contracts, and a local composition package. Keep the current user
flows working through an in-process implementation of the interface. Finish
with full verification of the resulting application and its package boundaries.

This is an extraction and behavior-preservation project. It does not implement
a hosted service. Do not build an HTTP server, HTTP client, authentication,
account system, cloud storage, task queue, deployment, container topology, or
provider proxy as part of these PRs. Do not add implementation milestones for
those systems. Do not change models, prompts, retry counts, quality gates,
timeouts, planning algorithms, or probe policies to make the extraction pass.

The agreed future integration constraint is retained: MiniSWE stays on the
user's machine. It will call an OpenAI-compatible service API using the same
Powdrr credential as other agent calls. Initially that API will forward inference
to DeepInfra using a service-held credential. This plan provides a local
inference interface and adapter only. It does not move MiniSWE's agent loop to
the service or implement the future endpoint.

At this milestone all four packages run on one machine. Service logic still
makes existing provider calls from that machine, because it is embedded locally.
The achievement is enforceable code ownership and a data boundary, not remote
execution or physical protection of private code. Do not describe the combined
local install as the distributable open source agent.

## 2. Product behavior to preserve

1. Design remains interactive. The agent displays questions and collects user
   answers; service logic selects questions and generates design artifacts.
2. Planning and re-planning remain autonomous. Preserve source accounting,
   accepted answers, parallel ready-question frontiers, dependency ordering,
   deterministic IDs, plan compilation, and plan-repair compare-and-swap checks.
3. PR implementation and review-feedback handling remain autonomous. Preserve
   worktree isolation, test/probe authoring before implementation where currently
   required, MiniSWE session continuation, validation, correction budgets, and
   current publication behavior.
4. Preserve existing CLI names, arguments, exit codes, artifact schemas, GitHub
   issue/PR markers, checkpoint formats, and resume compatibility.
5. Keep MiniSWE output as observed evidence. Never manually fix generated target
   implementation or tests to make a verification run pass. Repairs must use the
   existing product repair paths and be recorded as such.
6. Preserve baseline expected-error rules, empty-versus-failed outcomes, async
   probe handling, worker-authored commit handling, and existing acceptance
   semantics exactly as they exist on the implementation PR's base commit.

If an existing bug is discovered, reproduce and report it separately. A change
to that behavior needs a separate reviewed change; do not conceal it in a move.

## 3. Package layout and dependency rules

Use these import names and final directories:

```text
packages/
  contracts/
    pyproject.toml
    src/powdrr_contracts/
      operations.py       # public request/result types and interfaces
      repository.py       # pinned source observations and snapshots
      actions.py          # local action requests, receipts, preconditions
      interaction.py      # user questions, answers, progress
      artifacts.py        # artifact contents/references and digests
      inference.py        # MiniSWE inference request/response contracts
      errors.py           # public typed error envelopes
  agent/
    pyproject.toml
    src/powdrr_agent/
      cli/
      ui/
      repository/         # filesystem, Git, worktrees, source collection
      tools/              # local command execution, tests, editing
      coding/             # MiniSWE process/session/model adapters
      execution/          # authorization and local action journal
      adapters/           # existing local integration adapters
      client.py           # injected public service interface consumer
  service/
    pyproject.toml
    src/powdrr_service/
      application/        # use cases and public-interface implementation
      design/
      planning/
      verification/
      structrr/
      product/            # specification and lifecycle semantics
      processes/          # private process definitions and resources
      providers/
      integrations/       # GitHub APIs and external research
      persistence/        # private state/checkpoint abstractions
  local/
    pyproject.toml
    src/powdrr_local/
      bootstrap.py        # sole composition root
      bridge.py           # in-process, serialized data boundary
      cli.py              # existing combined application entry points
tests/
  architecture/
  contracts/
  integration/
  ...                     # existing tests may move incrementally
```

The root project remains the development/application workspace. Use workspace
dependencies for these four distributions. Preserve Python 3.12 compatibility.
Use the existing shared environment; do not run routine `uv sync` or install a
worktree into it. During migration, checks use a PYTHONPATH containing the four
package source roots and the legacy root `src` while it still exists. Record
this invocation once in a helper script and CI rather than copying it everywhere.

Allowed dependencies:

```text
powdrr_contracts -> standard library
powdrr_agent    -> powdrr_contracts + local-tool/UI/MiniSWE dependencies
powdrr_service  -> powdrr_contracts + private libraries/provider integrations
powdrr_local    -> powdrr_agent + powdrr_service + powdrr_contracts
```

Neither agent nor service imports the other. Neither imports the local package.
The service does not import agent repository tools even if the interface is
implemented in the same process. Contracts contain data structures, structural
validation, and transport-independent interfaces; they contain no prompts,
provider selection, planning rules, rubrics, or artifact acceptance algorithms.
An existing domain type belongs in contracts only if it is genuinely exchanged
and can be specified without exposing implementation policy. Use a public data
projection when an internal type contains more state than the consumer needs.

The service may retain `procedrr` and `procedrr_evaluator` as private packages or
private dependencies. The agent must not import them or load their private
process catalog. Their existing separation from product semantics remains intact.
This plan changes distribution ownership, not the meaning of those languages.

The root `powdrr-lift` and `powdrr-lift-mcp` entry points initially resolve through
`powdrr_local`. Agent-only tests construct the agent with a contract-conforming
fake service. The agent's CLI module accepts an injected interface; it must not
discover or import a local service implementation on its own.

## 4. Interface contract

### 4.1 Public service operations

Define one `PowdrrService` Protocol with the following application methods. These
are Python interfaces for this milestone, not HTTP endpoints:

```text
start(OperationRequest) -> OperationUpdate
advance(OperationInput) -> OperationUpdate
inspect(OperationReference) -> OperationUpdate
cancel(OperationReference) -> OperationUpdate
infer(ChatCompletionRequest) -> ChatCompletionResponse
```

`operation_kind` is an explicit registered name. Initially enumerate design,
plan, replan, implement, review_feedback, bootstrap, inspect_artifacts, and the
existing standalone verification/evaluation commands found in PR 1's inventory.
Do not implement a remote arbitrary-function-name dispatcher. New registered
operations may be added without changing the interface shape.

The service owns the detailed process state and internal question sequence.
`advance` crosses a user-input or local-action boundary, not every internal
classifier/LLM call. Existing bounded planning questions remain internal and
continue to run concurrently where currently permitted.

Every public object must serialize to JSON-compatible data and deserialize with
structural validation. Define `schema_version`, operation ID, revision, and
request/action correlation IDs explicitly. Do not use pickle or marshal.
Reject unsupported schema versions and mismatched input types with public errors.

### 4.2 Required data types

| Contract | Required information |
| --- | --- |
| OperationRequest | Kind, request ID, user objective/inputs, repository identity, artifact inputs, advertised local capabilities |
| RepositorySnapshot | Repository ID, branch, full base/head SHA, manifest digest, observed worktree digest, source artifact references |
| OperationUpdate | Operation ID, revision, status, ordered public progress records, pending local actions/user questions, output references, public failure details |
| OperationInput | Operation ID, expected revision, input ID, action receipts/user answers/evidence additions |
| LocalAction | Stable action ID, capability name/version, typed arguments, repository/worktree ID, preconditions, applicable budget |
| ActionReceipt | Action ID, payload digest, actual status, observed revisions, exit code/signal, stdout/stderr references, changes and artifact references |
| UserQuestion | Stable question ID, question text, answer shape/options, necessary user-visible context |
| UserAnswer | Question ID, answer data, explicit user decision metadata where currently recorded |
| Artifact | Schema/media type, bytes or explicit content encoding, SHA-256 digest; reference scope and resolution rules |
| ServiceError | Stable category, safe message, retryability, correlation ID; no raw exception objects |

Represent artifact bytes using an explicit binary-to-JSON encoding. Repository
paths are relative portable strings; local absolute paths are agent-owned.
Branch names alone do not identify source state. Include dirty and untracked
source only when current command semantics include it; state the inclusion
policy in the snapshot. Do not silently replace commit-pinned reads with reads
of the current worktree.

Use statuses `running`, `waiting_for_agent`, `waiting_for_user`, `succeeded`,
`failed`, and `cancelled`. Preserve existing blocked/suspended distinctions in
typed reason fields or explicit additional statuses when their behavior differs.
A blocked prerequisite must not appear as success. An empty valid result is
different from failed generation. Document each status transition and whether
an operation can advance from it.

### 4.3 Local actions

Implement capabilities for current needs, with closed argument schemas:

- Repository inventory, pinned file reads, bounded search, and symbol discovery.
- Worktree creation/selection/removal and observation of Git state.
- File materialization/patch application with expected content digests.
- Command/test execution, timeouts, signals, and result capture.
- Local Git mutations and fetch/push using the existing authorization policies.
- MiniSWE execution/continuation and its resulting changes/worker commits.
- User interaction and progress rendering.

The agent validates local authorization independently of service instructions.
Do not move safety checks protecting paths, existing tests, or user data into
contracts if they are semantic Powdrr policy; keep semantic checks in service
and generic local authorization in agent. Both can reject the same action for
different reasons, which must remain visible in errors.

Service logic requests evidence by descriptive capability requests and receives
data. It cannot hold a `Path` to the user's repository or call a Python callback
that closes over the agent's live runtime. Independent reads may be batched.
Dependent mutation/test/correction steps remain ordered.

### 4.4 MiniSWE inference contract

Keep MiniSWE's execution loop, local environment, subprocess/session lifecycle,
and trajectory persistence in `powdrr_agent.coding`. Keep Powdrr prompt building,
model/provider selection, and response-independent review rules in service.

Provide a `ModelClient` interface using serializable chat-completion requests and
responses. Requests carry model identifier, messages, tool definitions, and
supported generation options. Responses preserve assistant content, tool call
IDs/names/arguments, finish reason, usage, and the response information needed
by current trajectory/cost handling. Model routing identifiers supplied to the
agent are opaque; the agent does not choose DeepInfra fallbacks.

Add a MiniSWE model adapter implementing MiniSWE's model interface using this
client. Preserve MiniSWE's existing parsing and exception behavior through its
public utilities where possible. Do not implement a second coding agent or a
second probe authoring route. Do not reimplement the MiniSWE action loop.

For this stage the local composition adapter dispatches inference to the
existing service-owned provider code. A MiniSWE subprocess cannot share an
in-memory Python object: supply it a local IPC model adapter if retaining the
subprocess is required for current OS sandbox/timeout guarantees. Use a private
parent/child pipe with correlated JSON messages for this implementation, not an
HTTP listener or a TCP service. Define request/response frame size bounds, EOF,
parent cancellation, and serialization errors. Retain the existing subprocess
and sandbox boundary; switching MiniSWE to execute shell tools in the main
application process is not an equivalent extraction.

The parent consumes inference requests and calls `PowdrrService.infer` through
the same local bridge as other operations. MiniSWE receives no provider API
keys, provider endpoint, or provider fallback configuration. Include initial,
large-prompt, resumed, probe/test-authoring, and repair invocations in the tests.
Use a proper framed pipe implementation compatible with Linux and macOS; do not
assume stdin/stdout are free when the existing process monitor uses them.

The operation must yield a MiniSWE local action before the agent launches the
worker. While monitoring that child, the parent must also drain inference IPC
and dispatch `infer`; it cannot block solely waiting for subprocess completion.
Do not hold an operation-state lock across child execution or inference dispatch.
Test this ordering with an inference response that waits for parent progress,
and cancellation during an outstanding request, so a circular wait cannot hide
behind a fast stub response. Preserve streaming capability if exercised by the
current model adapter; do not force a new streaming policy during extraction.

This temporary local model adapter exists to verify the code boundary. The
accepted future OpenAI-compatible API can replace it without relocating the
MiniSWE loop. Do not build that API in this project.

### 4.5 The in-process bridge

`powdrr_local.bridge` implements the public service Protocol by wrapping the
service application. Serialize and deserialize every request and result, even
though both sides run locally. This catches accidental sharing of mutable
objects, callbacks, file handles, exceptions, and private runtime types.

The bridge may supply service-owned adapters for provider calls, external
integrations, and service state storage. It cannot inject the agent's entire
runtime into the service. Local actions return to the agent dispatch loop as
data; do not hide an agent import behind a bridge callback.

Support progress during a long-running local call using a public progress sink
at the composition boundary; deliver serialized progress records. It must not
carry an execution object or let service logic invoke the UI. No HTTP polling,
streaming server, or remote event infrastructure is required here.

### 4.6 State, cancellation, and compatibility

- Keep private process checkpoints and prompt/provider traces service-owned.
  The local service state adapter writes existing compatible formats during
  this extraction. The agent retains local action results and mutation records.
- Preserve planning tracker issue metadata and its existing accepted-answer
  journal. Extract storage adapters; do not replace this with a new journal
  system or publish extra proprietary prompt state to GitHub.
- Persist opaque service continuation references in agent state. The combined
  runtime resolves them using service-owned storage. Do not expose private
  checkpoints through an undocumented public `Any` field.
- Repeated input IDs return the previous result or a clear conflict if the
  content differs. Stale operation revisions fail before applying new effects.
- Repeated local action IDs use stored receipts where available. Reconcile
  interrupted Git/publication effects with the existing state before retrying.
  Do not promise exactly-once effects across crashes.
- Propagate cancellation to local commands, MiniSWE, and service provider calls
  using current cancellation behavior. Preserve the distinction between an
  interrupted operation and a completed failure.
- Read existing checkpoints and GitHub markers through explicit compatibility
  readers. Do not require users to discard in-progress tasks or restart plans.

## 5. Ownership map from the audited repository

This table is a starting map. PR 1 produces a complete machine-readable module
inventory; every production module and packaged resource needs a final owner.

| Existing source | Destination and required extraction |
| --- | --- |
| `cli.py`, `mcp_server.py`, `workrr/chat_tui.py`, `chat_io.py` | Agent UI/protocol adapters; mixed command handlers call service interface; combined entry points assembled by local package |
| `workrr/procedrr_chat.py`, `chat_context.py`, `task_agent.py` | Split UI/repository execution from service process selection, context policy, prompts, decisions, and corrections |
| `workrr/planning/contracts.py` | Private planning contracts by default; project only actual boundary types into public contracts |
| `planning/source.py`, `evidence.py`, `driver.py` | Source segmentation/coverage strategy/question ordering in service; pinned Git reads, rg, basedpyright queries in agent; replace repository-root access with supplied evidence |
| `planning/assembly.py`, `compiler.py`, `dependencies.py`, `structrr.py`, `repair.py`, `questions.py` | Service-owned planning algorithms and prompts |
| `planning/service.py`, `checkpoints.py`, `repair_service.py` | Service coordinator/state rules; separate GitHub storage and local file publication effects behind adapters |
| `github_issue_plan_generation.py`, `github_issue_queue.py`, `plan_sizing.py`, `multipr_structrr.py` | Service plan/queue semantics and GitHub API integration; local worktree/Git actions in agent |
| `feature_endpoint.py`, `feature_run.py`, `runner.py`, execution strategies | Service use-case orchestration; agent tool execution; no whole-file relocation without separating I/O |
| `coding_agent.py`, `minisweagent_session.py`, `minisweagent_monitor.py` | Agent worker/session/monitor; prompt composition and provider selection in service; shared worker DTO projections in contracts |
| `providers.py`, `provider_runtime.py`, `provider_config.py`, `llm.py`, `jev_classifier.py`, `semantic_classifier.py`, `local_model.py` | Service provider clients, credential lookup, LLM/classifier policy and retries |
| `postrun_probes.py`, `probe_repair.py`, `execute_pr_tests.py`, `differential_verification.py`, verification modules | Service authoring, quality and acceptance policy; agent subprocess/test runners returning observed receipts |
| `external_contract_research.py` | Service searches, source fetching and interpretation; no direct Tavily/LLM calls from agent |
| `structrr/*`, `core/*` product semantics, instruction ledger, faithfulness, contract closure | Service by default; split source collection from compilation/comparison; public projections only in contracts |
| `execution/*` | Agent local effect enforcement/journal vs service process transitions, compaction, persona/prompt and semantic acceptance; audit each module |
| `intrinsic_*`, `file_management.py`, `basedpyright_tools.py`, `workrr/git.py` | Extract mechanical local tools to agent; domain enrichment/validation stays service |
| `artifact_state.py`, changelog parsers, specification loaders | Service reconstruction and semantic validation; agent can collect bytes and display service projections |
| `powdrr_service.procedrr`, `powdrr_service.procedrr_evaluator`, and `powdrr_service.resources/processes` | Private service language/runtime/resources; service resource lookup independent of user's repository path |
| `openai_proxy.py` | Existing diagnostic tool stays in private development/local tooling; not the future authenticated service gateway |
| `integrations/harbor/*`, benchmark/evaluation CLI | Agent harness for repository operations; service-owned scoring/prompts/benchmark analysis; preserve verifier-as-tests boundary |

Keep GitHub API clients and semantics in service integrations for this split;
the local composition supplies existing GitHub credentials. Local Git transport
remains an agent capability. Do not design GitHub App onboarding here. For any
other external integration found during inventory, apply the same rule and
record its owner explicitly.

## 6. Execution sequence: one reviewed PR per step

The following PRs are sequential. Base each on merged main, inspect review
comments, and never merge your own PR. Keep the application runnable after every
PR. Mark each checklist item with implementation commit and verification
artifact references as work completes. A renamed directory is not a completed
step if the import graph or runtime still crosses the old boundary.

### PR 1 — Freeze behavior and inventory ownership

1. Record implementation base SHA and current CLI/MCP entry points, workflow
   catalogs, providers, packaged resources, external integrations, and resume
   formats. Capture CLI help and the deterministic workflow suite report.
2. Produce `docs/architecture/agent-service-module-ownership.json`: each source
   module/resource, proposed owner, imports, mixed-responsibility notes, and
   assigned extraction PR. Include root helper modules and scripts used by
   production entry points. Include tests whose monkeypatches depend on old paths.
3. Add representative fixtures for existing design, planning, re-planning,
   implementation, review feedback, Structrr inspection, and checkpoint resume.
   Reuse existing fixtures. Record current outputs and meaningful decisions,
   normalized only for time, random IDs, temp roots, and nondeterministic order
   where order is explicitly irrelevant. Keep source IDs and dependency order.
4. Run the full existing verification before changing behavior. Record failures
   as baseline failures; do not relabel them passes or weaken tests.

Acceptance: every production module/resource has an owner or an explicit split
assignment; the baseline suite and fixture outputs can be reproduced.

### PR 2 — Add public contracts and the serialized bridge

1. Add contracts, agent, service, and local package skeletons with workspace
   metadata. Keep legacy implementation functioning until each migration lands.
2. Implement section 4's public types, structural validation, service Protocol,
   capability registry, and bridge serialization. Contracts import only stdlib.
3. Add one read-only operation (artifact-state inspection) through the complete
   agent -> bridge -> service path. Collect source on the agent and perform
   reconstruction in service; return an explicit public projection.
4. Add architecture tests and JSON round-trip/invalid-version/aliasing tests.
   Introduce a temporary, enumerated legacy-import allowlist with owning PR IDs.

Acceptance: the read-only vertical slice uses the real boundary; the service
cannot obtain the agent's repository object through a callback or binding.

### PR 3 — Extract local tools and repository evidence

1. Move filesystem, worktree, Git, command/test execution, source inventory,
   bounded search, and basedpyright invocation into agent capabilities.
2. Move semantic repository interpretation and Structrr building into service
   functions consuming pinned source/evidence packets. Separate read/analyze/write
   operations in bootstrap, actual diff compilation, and artifact inspection.
3. Move generic local authorization and action receipts to agent. Keep private
   policy checks in service. Preserve existing command environments and timeouts.
4. Update private service call sites to request local actions instead of opening
   repository paths. Keep service resource/state I/O explicitly separate from
   repository I/O. Do not put `run_internal_python_function` in the capability set.

Acceptance: bootstrap and candidate-diff fixtures match baseline; dirty worktree,
wrong SHA, stale patch, forbidden path, and failed command cases are exercised.

### PR 4 — Extract planning and re-planning

1. Move the bounded planner's algorithms and internal question contracts into
   service. Preserve parallel frontier execution and single-writer checkpoint
   append ordering. Avoid replacing bounded internal questions with public RPCs.
2. Split `source.py`, `evidence.py`, and `driver.py` repository access using PR 3
   actions. Keep evidence search strategy and relevance decisions in service.
3. Extract checkpoint persistence, tracker-issue operations, repair publication,
   and local artifact materialization without changing persisted formats.
4. Route plan/start/resume/replan CLI handlers through public operations.
   Tests must cover legacy resume, changed source SHA, already-covered work,
   partial coverage, dependency repair, and plan compare-and-swap failure.

Acceptance: initial plans, workflows, proposals, repair scope, and ready-work
selection match baseline; accepted answers survive an interrupted continuation.

### PR 5 — Extract interactive design

1. Move process selection, interview question generation, specification prompts,
   and design decision state to service. Move private catalogs/resources there.
2. Retain CLI/TUI rendering, user input, cancellation, and terminal handling in
   agent. Replace direct accesses to process/provider objects with public updates.
3. Preserve explicit user choices, design answers, specification artifact
   lineage, and resume behavior. Repository-specific customer documents remain
   input artifacts; do not confuse them with Powdrr's private bundled processes.
4. Exercise an actual scripted conversation through the agent and local bridge,
   including follow-up input, cancellation, restart/resume, and generated files.

Acceptance: no agent module loads private process definitions or constructs
proprietary design prompts; baseline design fixtures and UI tests pass.

### PR 6 — Extract provider access and the MiniSWE inference boundary

1. Move all first-party provider/classifier clients, credential resolution,
   model mappings, external research, and retry policy into service modules.
2. Implement the local ModelClient and MiniSWE model adapter from section 4.4.
   Add parent/child inference IPC without weakening current subprocess, OS
   write-protection, monitoring, or timeout behavior. Catch child EOF/crash and
   cancellation without leaving a blocked parent or orphan worker.
3. Remove direct provider credentials and direct provider configuration from
   MiniSWE environments/config/argv. Keep normal user repository execution
   environment behavior; document deliberate credential exclusions.
4. Keep provider configuration arguments accepted by the combined application:
   parse in the CLI, pass as opaque configuration to local composition/service,
   and keep actual provider selection/credential lookup outside agent modules.
5. Exercise real MiniSWE initial, large-prompt, and resumed runs through the local
   inference interface with a deterministic provider-side test server/adapter.
   Do not replace MiniSWE with a fake process for the end-to-end proof.

Acceptance: MiniSWE sends messages/tool calls through the inference contract;
service invokes the provider; MiniSWE executes its local tools and submits; no
provider key appears in agent argv, model config, or trajectories. Agent tests
can run with provider credentials absent and direct provider egress denied.

### PR 7 — Extract implementation, verification, and review feedback

1. Split `task_agent.py` and `feature_endpoint.py` by responsibility using the
   inventory. Move process strategy, test/probe prompts, quality review,
   correction decisions, and final acceptance into service use cases.
2. Agent invokes MiniSWE and tests via local actions and returns actual changes,
   commits, exit codes, and test artifacts. No manually patched generated output.
3. Service computes Structrr and instruction comparisons from the returned
   evidence and drives existing repairs. Preserve current advice/blocking rules.
4. Move review-comment interpretation, plan-defect decisions, and PR/issue API
   integrations into service. Preserve local worktree and publication ordering.
5. Route headless workers, feature commands, review feedback, MCP, and Harbor
   harnesses through the same boundary. Do not leave a benchmark-only shortcut
   that imports private modules into the agent.

Acceptance: implementation and review-feedback vertical slices exercise real
Git/worktrees, MiniSWE, test commands, repairs, and service comparisons. A failed
required validation cannot be reported as success. Worker commits remain visible.

### PR 8 — Finish ownership, remove migration shims, and prove isolation

1. Migrate every remaining command/module/resource in the inventory: evaluation,
   template operations, changelogs, standalone validation, inspectors, and helpers.
   Update imports and monkeypatch targets explicitly; do not retain monkeypatch
   compatibility through global `sys.modules` aliases.
2. Remove the legacy-import allowlist and private compatibility shims. An old
   executable entry point may remain as a thin local-composition delegator;
   the public agent package must not depend on it.
3. Move private process data and first-party private libraries into service
   packaging. Ensure agent distribution metadata does not depend on service,
   local composition, provider libraries directly, or private resources.
   Third-party MiniSWE transitive dependencies are not first-party private code;
   test that our execution paths do not use their direct provider call surfaces.
4. Build agent/contracts distributions and inspect wheel AND sdist contents.
   Install them in a disposable isolated test environment outside the shared
   development environment. This is packaging verification, not routine setup.
5. Complete the full verification matrix in section 7. Commit a final ownership
   report and verification report. Document the exact local combined invocation.

Acceptance: zero first-party private imports/files/resources in the agent
distribution, all production surfaces classified and migrated, all required
behavior/parity/recovery checks pass. Hosting remains absent.

## 7. Verification contract

### 7.1 Architecture and distribution checks

Use AST/import graph checks plus installed-artifact runtime checks. A grep-only
test is insufficient: include imports from initializers, relative imports,
dynamic imports at known factory sites, entry points, and packaged resources.

- Contracts import neither application side nor private third-party libraries.
- Agent imports contracts and approved third parties, never service/local or
  legacy private code. Service imports no agent/local modules.
- Only local composition constructs both implementations. It contains wiring
  and serialization, not planning, prompting, or acceptance algorithms.
- Fail inventory checks if a new source/resource has no explicit owner.
- Inspect built wheel/sdist: no private prompts, rubrics, taxonomy policy,
  process YAML, provider configuration/client code, proprietary fixture answers,
  or private first-party libraries. Include tests/examples in the audit if
  they are packaged. Public schemas and generated user outputs are permitted.
- In an environment containing only agent/contracts and public dependencies,
  import all agent modules and run its CLI/UI/tool flows with a fake service.
  Make private packages unavailable, not merely unused in one invocation.
- Run service operations from outside a repository checkout, with source supplied
  only through contracts. Test adapters must fail on illicit repository reads.

### 7.2 Required behavior matrix

| Flow | Execute and assert |
| --- | --- |
| Interactive design | Scripted answers, follow-up, preserved user decisions, generated artifacts, cancellation and resume |
| Planning | Pinned source, evidence collection, parallel frontier, deterministic assembly, dependency order, workflows and proposal parity |
| Re-planning | Discovery -> scoped patch -> compilation -> artifact application; unchanged unrelated items; stale-plan conflict |
| PR implementation | Real temporary repository/worktree, pre-code tests/probes, real MiniSWE tool execution, candidate validation, recorded commit/PR intent |
| Review feedback | Existing PR/head + comment evidence -> focused correction -> validation -> updated publication request |
| Artifact state | Bootstrap plus changelists at branch/SHA; dirty workspace observation cannot masquerade as that SHA |
| MiniSWE lifecycle | Initial, large-prompt, resume, test/probe authoring, provider error, malformed response, child crash, cancellation, worker commit |
| Failures | Empty vs failed generation; expected baseline API errors; rejected local action; timed-out command; failed required validation; exhausted repairs |
| Recovery | Existing checkpoint load, interruption after accepted answer, duplicate input/action receipt, interrupted Git effect reconciliation |
| Integrations | MCP and Harbor entry points exercise the same boundary and preserve output/exit-code contracts |

Use deterministic provider responses to make these scenarios reliable, but keep
the service algorithms, bridge, agent loop, real filesystem/Git, local tools,
MiniSWE parsing, and subprocess execution real. Fake only external provider/API
responses and publication side effects in the deterministic suite. Include at
least one deliberate defect that causes a real test failure and real product
repair. Assert resulting repository content and receipts; a favorable judge
verdict or a mocked success object is not proof of implementation.

Existing live-provider tests remain necessary for final integration verification
where credentials and supported providers are available. Run one representative
design/planning/implementation/review sequence with live providers through the
local service implementation, in a disposable repo. External publication uses
a designated test repository only; use local publication receipts otherwise.
Record which edges were live and which were simulated. Missing credentials or
unavailable providers produce an explicit unverified requirement, not a full
verification claim. Do not create a hosted service to perform this check.

### 7.3 Existing repository checks

Before each implementation PR, run targeted tests and the repository-required
full checks. At the final milestone run the following current CI equivalents,
updating only package/source paths as necessary for the split:

```sh
# Set these for this shell; use the current worktree's absolute paths.
export PATH=/Users/gregory/code/powdrr-lift/.venv/bin:$PATH
export VIRTUAL_ENV=/Users/gregory/code/powdrr-lift/.venv
export UV_PROJECT_ENVIRONMENT=/Users/gregory/code/powdrr-lift/.venv
export UV_NO_SYNC=1
export PYTHONPATH="$PWD/packages/contracts/src:$PWD/packages/agent/src:$PWD/packages/service/src:$PWD/packages/local/src:$PWD/src"

rtk proxy python -m ruff format --check .
rtk proxy python -m ruff check .
rtk proxy python -m mypy packages src tests
rtk proxy python -m powdrr_local.cli validate-procedrr-processes
rtk proxy python -m pytest -n auto --cov=powdrr_agent --cov=powdrr_service --cov=powdrr_contracts --cov=powdrr_local --cov-branch --cov-report=term-missing --cov-report=xml tests
rtk proxy bash scripts/run-deterministic-workflow-suite.sh
```

The process catalog path changes to the service resource location after resource
migration; update the command and CI to validate the installed service catalog.
Remove obsolete `src` arguments only when its remaining compatibility modules
are removed. `powdrr_local.cli` must support module invocation. The scenario
script currently uses `uv run`; update it to invoke the current workspace's
combined CLI without reinstalling the shared environment or importing a stale
editable checkout. Make equivalent CI checks work from a clean workspace install.

Add the architecture, serialization, parity, isolated-package, MiniSWE IPC, and
recovery tests to mandatory CI. Update coverage collection so moving private
code does not silently remove it from measured sources. Do not lower existing
quality gates or waive unrelated failures to complete the extraction.

### 7.4 Final verification report

Write `docs/architecture/agent-service-split-verification.md` with:

- Final commit SHA and package/distribution versions tested.
- Complete ownership inventory and import graph results.
- Before/after flow matrix, output comparisons, and permitted normalizations.
- Commands, exit codes, test counts/skips, coverage, deterministic suite report.
- Isolated agent wheel/sdist contents and import smoke results.
- Real MiniSWE trajectories and receipts for initial/resume/repair cases.
- Live provider run IDs, model identifiers, exact source/candidate SHAs,
  validation outcomes, and disclosure of simulated external integrations.
- Resume and cancellation results, known limitations, and unresolved failures.

Keep credentials and private conversations out of public verification reports.
Store sensitive raw evidence privately and cite a safe identifier in the report.

## 8. Rules for the implementing agent

1. Read this document, repository AGENTS.md, and relevant existing architecture
   constraints before editing. Work only in dedicated worktrees/feature branches.
2. Implement the current PR's scope. Do not combine eight extractions into one
   unreviewable PR or implement HTTP/hosting ahead of this milestone.
3. Make mechanical moves separately from necessary interface changes where
   practical. Update callers and tests in the same runnable change set.
4. Every boundary method receives/returns public data. `Any`, a service locator,
   a callback closure, or a shared runtime object is not a substitute for a
   missing contract. Bounded JSON extension fields need an explicit schema owner.
5. Preserve actual current behavior, even where an older design document says
   something different. In particular, do not reintroduce checks removed by
   later PRs or alter advisory test-quality findings during relocation.
6. Do not fix generated MiniSWE artifacts by hand. Debug product infrastructure
   separately, then re-run the real authoring/execution path.
   Benchmark ground-truth verifiers remain test-only inputs; never feed their
   contents or expected solutions into planning, prompting, or probe generation.
7. Record baseline failures, test skips, and unverified requirements honestly.
   Do not call the split fully verified with required matrix rows outstanding.
8. Open a PR with changed ownership, interface contracts, parity evidence,
   verification, and remaining migration allowlist entries. The user merges.

## 9. Completion checklist

- [ ] All four packages have explicit ownership and an acyclic dependency graph.
- [ ] Design, planning/replanning, implementation, and review use the interface.
- [ ] Agent owns local tools, UI, MiniSWE execution, and local authorization.
- [ ] Service owns all first-party proprietary algorithms, prompts, policies,
      model/provider access, process definitions, and external API integrations.
- [ ] MiniSWE remains local and uses the injected local inference boundary.
- [ ] Public contracts contain no private decision algorithms or provider keys.
- [ ] No live repository/runtime objects cross the serialized bridge.
- [ ] Existing artifacts, CLI behavior, markers, and checkpoints remain usable.
- [ ] All required deterministic and live verification results are recorded.
- [ ] Agent/contracts build and run without private packages or resources.
- [ ] Temporary migration imports/shims are removed or limited to executable
      delegators in the private combined application.
- [ ] No hosted service, HTTP gateway, or deployment work was added.

The next stage can be specified after this milestone is reviewed and verified.
This plan ends at the clean, fully exercised local code split.
