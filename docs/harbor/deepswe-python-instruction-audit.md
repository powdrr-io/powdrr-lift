# DeepSWE Python instruction audit

This is a working catalog for local Powdrr runs against the Python tasks in the
local DeepSWE corpus. The goal is to find instruction patterns that planning,
implementation, validation, or reporting handles poorly, then group the gaps
into work that improves performance across many tasks.

## Run ledger

| Task | Shape | Status | Evidence |
| --- | --- | --- | --- |
| `python-statemachine-state-data-scoping` | API expansion, lifecycle, callbacks, serialization, parser and diagram support | Prior run reviewed; not rerun in this audit | [run analysis](deepswe-python-statemachine-state-data-scoping.md) |
| `fastapi-implicit-head-options` | HTTP routing behavior and response metadata | Parsed 33 clauses; stopped at scenario validation before worker prompt generation | Local Pier artifacts: `/private/tmp/powdrr-python-instruction-audit/fastapi-implicit-r4/python-audit-fastapi-implicit-r4/` |
| `igel-persist-feature-schema` | Feature schema persistence after fit | Adapter setup passed; all 24 clauses compiled, then non-goal classification stopped the run before worker prompt generation | Local Pier artifacts: `/private/tmp/powdrr-python-instruction-audit/igel-feature-schema-r2/python-audit-igel-feature-schema-r2/` |
| `cattrs-partial-structuring-recovery` | New conversion result type and partial/error accumulation semantics | Planned | |
| `numba-stencil-boundary-modes` | Numerical edge modes and compilation behavior | Planned | |

The planned cattrs and numba runs sample distinct instruction shapes. Expand
the sample if findings suggest the same failure modes do not generalize.

## Runner setup findings

The first two launches were setup failures: one used the wrong host source
path, and the next was blocked by the sandbox's Docker BuildKit cache. A later
run using the current pushed source revision passed adapter setup and reached
the task instruction. This matters because the earlier `_get_env` failure
came from explicitly installing a stale commit, while current source calls
Pier's one-argument API.

The Igel task then exposed another adapter setup defect. Its image activates a
Python virtual environment, where `pip install --user` fails with:

```text
Can not perform a '--user' install. User site-packages are not visible in this virtualenv.
```

The adapter now chooses `--user` only for system Python and omits it inside a
virtual environment. This should allow the adapter package and MiniSWE to
install in both image types.

## Capability catalog

Use these categories when reviewing each run. A task can touch several.

| Area to improve | What to inspect in a run |
| --- | --- |
| Requirement coverage | Are every requested API, behavior, constraint, and deliverable turned into explicit work and validation items? |
| Public API fidelity | Are exact names, signatures, imports, defaults, return shapes, and compatibility constraints retained? |
| Behavioral edge cases | Are boundaries, ordering, conflicts, failure paths, concurrency, and lifecycle spelled out and tested? |
| Cross-cutting changes | Can the plan handle work spanning modules, protocols, storage, runtime behavior, CLI, and docs without flattening it into one vague task? |
| Domain reasoning | Can the workflow identify the domain model and derive tests for scientific, security, networking, database, and framework tasks? |
| Validation discovery | Does it find the right focused and full test commands, preserve environment requirements, and explain unavailable checks? |
| Repository navigation | Can the agent locate extension points, follow existing patterns, and avoid generated state and unrelated files? |
| Instruction interpretation | Does it recognize ordered directives, negative constraints, setup commands, examples, and multiple deliverables? |
| Observability | Do artifacts preserve the source-to-plan trace, unresolved decisions, worker prompt, attempts, and reason for failures? |

## Python task inventory

The task summaries below are copied from the local task instructions. They are
an inventory, not evidence that a task has been run.

| Task | Instruction shape |
| --- | --- |
| `adaptix-name-mapping-aliases` | Alternative input aliases for fields in name mapping |
| `aiomonitor-task-snapshots-diff` | Capture and compare task state over time |
| `bandit-incremental-cache-control` | Cache unchanged files and handle circular imports |
| `bandit-interprocedural-taint-checks` | Follow user input through variables to injection sinks |
| `bandit-structured-nosec-directives` | Suppress findings over a code span or next statement |
| `cattrs-partial-structuring-recovery` | Partial conversion result with converted fields and errors |
| `dateutil-rfc5545-timezone-interop` | RFC 5545 timezone interoperability in recurrence rules |
| `fastapi-deprecation-response-headers` | Runtime response signals for deprecated routes |
| `fastapi-implicit-head-options` | Implicit HEAD controls and metadata-bearing OPTIONS response |
| `gql-incremental-graphql-delivery` | `@defer` and `@stream` incremental delivery |
| `httpx-multipart-response-parsing` | Parse multipart response bodies into parts |
| `httpx-streaming-json-iteration` | Incremental structured JSON iteration from responses |
| `igel-persist-feature-schema` | Persist selected raw feature schema after fitting |
| `ipython-session-bundle-replay` | Record and replay a session bundle |
| `kombu-single-active-consumer-priority` | Consumer selection, priorities, notifications, lifecycle tracking |
| `kombu-virtual-queue-dead-lettering` | Dead letter routing, TTL, and queue overflow |
| `koota-entity-snapshot-rollback` | Snapshot, diff, and rollback entity and world state |
| `langchain-request-coalescing` | Coalesce concurrent identical runnable requests |
| `mashumaro-flattened-dataclass-fields` | Flatten nested fields with configurable prefixes |
| `mnamer-daemon-watch-lifecycle` | Top-level scan and move behavior with explicit no-network/no-prompt constraints |
| `mobly-grouped-test-barriers` | Grouped execution and synchronization |
| `narwhals-rolling-window-suite` | Add rolling-window methods across expression and series APIs |
| `numba-stencil-boundary-modes` | Boundary modes for out-of-bounds stencil accesses |
| `powdrr-langchain-request-coalescing` | Explicitly requires invoking `powdrr-lift start-planning-feature` before anything else |
| `psd-tools-blend-range-api` | Typed API for reading and modifying layer blend ranges |
| `pwntools-tube-multiplexing` | Bidirectional logical channels over one tube |
| `python-statemachine-state-data-scoping` | Per-state data ownership, scoping, lifecycle, callbacks, history, SCXML, diagrams |
| `returns-validated-error-accumulation` | `Validated` result types and independent error accumulation |
| `skrub-duration-encoding` | Duration-column encoding across pandas and Polars |
| `sqlfmt-create-table-ddl-formatting` | Formatting requirements plus a new DDL module |
| `sqlite-utils-safe-import-checkpoints` | Transactional import checkpoints and post-write invariants |
| `textual-kitty-key-phases` | Keyboard protocol phases and stable key metadata |
| `textual-richlog-follow-state` | Scroll-follow state and full-width rendering behavior |
| `tomlkit-toml-table-converters` | Bidirectional conversion among TOML table representations |
| `vulture-persistent-analysis-cache` | Persistent incremental analysis cache |

## Confirmed finding from prior run

The statemachine task gives a concrete requirement-coverage failure. Its
instruction names APIs and independent behaviors, but the design output
collapsed them into one broad feature and one acceptance criterion. The
sentence trace was not persisted, and obligation compilation failed with a
generic no-obligations error. This means descriptive prose can survive in a
plan while implementation receives no actionable API-level obligations.

Follow-up work already identified from that run:

1. Persist each instruction sentence and its normalized requirement and
   reflection decisions before compilation.
2. Include sentence, required, reflected, and missing-plan counts in
   compilation diagnostics.
3. Gate implementation on every required sentence mapping to an explicit
   criterion, expected test, invariant, or obligation.
4. Preserve the generated plan and trace on failure.
5. Keep runtime/generated state out of the candidate patch.

## Confirmed finding from this audit

The FastAPI instruction reached semantic scenario generation but did not
produce a worker prompt. The model created a rejected capability for
`GET route with auto_head disabled`, invented a `405 Method Not Allowed`
outcome, and omitted the error field required for rejected capabilities. The
behavior-contract validator stopped the run before planning could produce an
implementation request. This surfaces two reusable gaps: the scenario prompt's
constraints did not prevent an unsupported rejection/error from being
invented, and invalid model output has no bounded correction path that can
preserve the instruction and continue to prompt generation. The task's F2P
score was 0 because no patch was produced; this is not an implementation
quality result.

The Igel instruction describes the selected schema as “not persisted,” a
product gap that the task asks the agent to address. A deterministic
disposition rule treated that wording as a product prohibition and labeled
the clause `non_goal`. The canonical design compiler correctly refused to
accept that unsupported exclusion, but stopped instead of restoring the
actionable requirement. The compiler now promotes explicit missing-persistence
statements to feature obligations when a semantic classifier labels them
`non_goal`. The regression test covers the exact task wording. This run also
ended before worker prompt generation and provides no implementation score.

## Triage notes

For every audited run, record the first stage where a requirement is lost:
instruction capture, planning, obligation compilation, worker prompt, code
change, validation discovery, review, or artifact collection. Separate an
instruction that is genuinely ambiguous from a workflow that failed to retain
clear instructions. Promote a finding to the capability catalog when it is
likely to affect more than one task family.

After the adapter compatibility issue is resolved, these are high-value
instruction probes for widening coverage:

| Task | Why it broadens coverage |
| --- | --- |
| `bandit-interprocedural-taint-checks` | Security reasoning across source-to-sink data flow |
| `httpx-streaming-json-iteration` | Streaming behavior, incremental parsing, and boundary handling |
| `sqlite-utils-safe-import-checkpoints` | Transactional behavior, rollback, and invariants |
| `sqlfmt-create-table-ddl-formatting` | Two deliverables and a numbered set of formatting requirements |
| `mnamer-daemon-watch-lifecycle` | Negative constraints: no network and no prompts |
| `powdrr-langchain-request-coalescing` | Ordered instruction requiring a specific CLI action first |
| `dateutil-rfc5545-timezone-interop` | Timezone and standards-specific edge cases |
